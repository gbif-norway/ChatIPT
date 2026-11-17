'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import config from '../config.js'
import { useTheme } from '../contexts/ThemeContext'
import { pluralize } from '../utils/datasetPresentation'
import {
  emptyExplorerState,
  explorerForRequest,
  nodeForTable,
  openRelatedState,
  reconcileExplorer,
  sameId,
  selectTable,
} from '../utils/explorerState.mjs'
import TableExplorer, { rememberedView } from './TableExplorer'
import TableList from './TableList'

const GRAPH_PADDING = 40

const formatResourceName = (name) => String(name || '')
  .split('-')
  .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
  .join(' ')

const graphCategory = (name) => {
  if (name === 'taxonomy-occurrence-links') return 'claims'
  if (name.startsWith('taxonomy-extension-')) return 'evidence'
  if (name.includes('assertion') || name.includes('relationship')) return 'claims'
  if (
    name.includes('agent')
    || name.includes('provenance')
    || name.includes('policy')
    || name.includes('reference')
    || name === 'bibliographic-resource'
  ) return 'context'
  if (
    name.includes('protocol')
    || name.includes('media')
    || name.includes('nucleotide')
    || name.includes('geological')
    || name.includes('chronometric')
  ) return 'evidence'
  return 'records'
}

const graphPalette = (isDark) => ({
  background: isDark ? '#181c20' : '#f7f9f8',
  foreground: isDark ? '#f3f5f4' : '#1d2823',
  muted: isDark ? '#aab5af' : '#5f6e66',
  edge: isDark ? '#829189' : '#819088',
  records: isDark ? '#4fc39c' : '#168563',
  evidence: isDark ? '#7da9ee' : '#4779bd',
  context: isDark ? '#d8a45f' : '#ad6c19',
  claims: isDark ? '#bd8ce3' : '#8556aa',
  selected: isDark ? '#ffffff' : '#15251d',
})

// The package model describes a node; the explorer wants a table. The node's own save time is its
// version (stats are matched to it); the model's reload counter is only a fallback for old payloads.
const tableFromNode = (node, version) => ({
  id: node.tableId,
  title: node.title,
  row_count: node.rowCount,
  columns: (node.fields || []).map((field) => field.name),
  updated_at: node.updatedAt || version,
  description: node.description,
  is_dwc_dp: true,
})

// Dims everything except the open table (and its neighbours) or the chosen relationship.
const highlightGraph = (cy, target) => {
  if (!cy) return
  cy.elements().removeClass('is-muted is-path').unselect()
  if (target?.type === 'node') {
    const node = cy.getElementById(target.id)
    if (node.empty()) return
    cy.elements().addClass('is-muted')
    node.closedNeighborhood().removeClass('is-muted')
    node.select()
  } else if (target?.type === 'edge') {
    const edge = cy.getElementById(target.id)
    if (edge.empty()) return
    cy.elements().addClass('is-muted')
    edge.removeClass('is-muted')
    edge.connectedNodes().removeClass('is-muted')
    edge.select()
  }
}

async function getModal() {
  const bootstrap = await import('bootstrap/dist/js/bootstrap.bundle.min.js')
  const modalElement = document.getElementById('packageExplorerModal')
  return modalElement ? { element: modalElement, modal: bootstrap.Modal.getOrCreateInstance(modalElement) } : null
}

const OPEN_REQUEST_EVENT = 'package-explorer:open'

// Opens the explorer modal mounted by <PackageExplorer>. The dataset page and the conversion page both use it.
// Optionally on a table: {tableId, state: {view, column, filters}}, or {focus: 'package'} for the linked tables
// and {focus: 'sources'} for the source tables (each keeps the open table when it is already of that kind).
// isCurrent(), when given, is asked again once Bootstrap has loaded: a request that went stale meanwhile (the
// page moved on to another dataset) is dropped, as is one whose modal left the page.
// Used directly as a click handler too, so anything else (such as a click event) opens it as it was.
export async function openPackageExplorer(request) {
  const element = document.getElementById('packageExplorerModal')
  if (!element) return
  const isCurrent = typeof request?.isCurrent === 'function' ? request.isCurrent : null
  const bootstrap = await import('bootstrap/dist/js/bootstrap.bundle.min.js')
  if (!element.isConnected || (isCurrent && !isCurrent())) return
  const wanted = request && typeof request === 'object' && (request.tableId != null || request.focus)
  if (wanted) {
    const { tableId = null, state = null, focus = null } = request
    element.dispatchEvent(new CustomEvent(OPEN_REQUEST_EVENT, { detail: { tableId, state, focus } }))
  }
  bootstrap.Modal.getOrCreateInstance(element).show()
}

/**
 * The package explorer modal. It loads the package model itself when it opens, unless the page already has
 * one: then pass `model` (null while there is none yet) with `modelLoading`, `modelError` and `onReloadModel`,
 * and it uses that instead of fetching the same heavy model again.
 * publishedUrl: the dataset's GBIF Sandbox page once it is published; the summary links to it.
 * sourceTables: the dataset's other tables ({id, title, row_count, columns, updated_at, description}), listed
 *   under "Your source files" and shown without the map while there is no package yet.
 * onExplorerStateChange(state): the open table, view, column and filters while the modal is open; null once closed.
 */
export default function PackageExplorer({
  datasetId,
  publishedUrl = null,
  onAsk,
  model: sharedModel,
  modelLoading = false,
  modelError = '',
  onReloadModel,
  sourceTables: sourceTablesProp,
  onExplorerStateChange,
}) {
  const graphRef = useRef(null)
  const cyRef = useRef(null)
  const highlightRef = useRef(null)
  const loadControllerRef = useRef(null)
  const onShowRef = useRef(null)
  const { isDark } = useTheme()
  const shared = sharedModel !== undefined
  const [ownModel, setOwnModel] = useState(null)
  const [ownLoading, setOwnLoading] = useState(false)
  const [ownError, setOwnError] = useState('')
  const model = shared ? sharedModel : ownModel
  const loading = shared ? Boolean(modelLoading) : ownLoading
  // A failed refresh of a shared model keeps showing the model it already has.
  const error = shared ? (sharedModel ? '' : modelError || '') : ownError
  const [modelVersion, setModelVersion] = useState(0)
  const [seenModel, setSeenModel] = useState(null)
  const [visible, setVisible] = useState(false)
  // Open from the moment the modal starts showing until it has hidden (visible waits for the animation).
  const [open, setOpen] = useState(false)
  // A chosen relationship replaces the table on the right until a table is chosen again.
  const [edgeId, setEdgeId] = useState(null)
  const [explorer, setExplorer] = useState(() => emptyExplorerState(rememberedView()))
  const [hoveredNodeId, setHoveredNodeId] = useState(null)
  const [mapExpanded, setMapExpanded] = useState(false)
  // Set when a link from a record opens a table, so that table's heading gets focus.
  const [focus, setFocus] = useState(null)
  const sourceTables = useMemo(
    () => (Array.isArray(sourceTablesProp) ? sourceTablesProp.filter((table) => !table.is_dwc_dp) : []),
    [sourceTablesProp]
  )
  const sourceKey = sourceTables.map((table) => `${table.id}:${table.updated_at || ''}`).join('|')
  const [seenSourceKey, setSeenSourceKey] = useState(null)
  // An openPackageExplorer() request still waiting for the package model (see explorerForRequest).
  const [pending, setPending] = useState(null)
  const latestRef = useRef({ model, sourceTables, loading, explorer })
  useEffect(() => {
    latestRef.current = { model, sourceTables, loading, explorer }
  })

  // A new model: the tables shown are a new version, and a chosen relationship is dropped when it left the
  // package. A new model or new source tables: the open table stays as long as it still exists.
  if (model !== seenModel) {
    setSeenModel(model)
    if (model) {
      setModelVersion((value) => value + 1)
      setEdgeId((current) => (current && (model.edges || []).some((edge) => edge.id === current) ? current : null))
    }
  }
  if (model !== seenModel || sourceKey !== seenSourceKey) {
    const modelArrived = model !== seenModel && Boolean(model)
    setSeenSourceKey(sourceKey)
    setExplorer((current) => reconcileExplorer(current, { model, sourceTables, pending, waiting: !modelArrived }))
    // A model answers a request for a table; Explore keeps waiting until a model has linked tables.
    if (modelArrived && pending && !(pending.focus === 'package' && !model.nodes?.length)) setPending(null)
  }

  const nodeMap = useMemo(
    () => new Map((model?.nodes || []).map((node) => [node.id, node])),
    [model]
  )
  const selectedNode = nodeForTable(model, explorer.tableId)
  const selectedEdge = edgeId ? model?.edges?.find((edge) => edge.id === edgeId) || null : null
  const highlight = selectedEdge
    ? { type: 'edge', id: selectedEdge.id }
    : selectedNode ? { type: 'node', id: selectedNode.id } : null
  const highlightKey = highlight ? `${highlight.type}:${highlight.id}` : ''

  // Opening a table or relationship yourself cancels an openPackageExplorer() request still waiting for the model.
  const pickTable = useCallback((tableId) => {
    setPending(null)
    setEdgeId(null)
    setFocus(null)
    setExplorer((current) => selectTable(current, tableId))
  }, [])

  const showNode = useCallback((nodeId) => {
    const node = nodeMap.get(nodeId)
    if (node) pickTable(node.tableId)
  }, [nodeMap, pickTable])

  const showEdge = useCallback((id) => {
    setPending(null)
    setEdgeId(id)
  }, [])

  const openRelated = useCallback(({ tableId, filters }) => {
    if (!nodeForTable(model, tableId)) return
    setPending(null)
    setEdgeId(null)
    setExplorer((current) => openRelatedState(current, { tableId, filters }))
    setFocus((current) => ({ tableId, request: (current?.request || 0) + 1 }))
  }, [model])

  const onExplorerChange = useCallback((next) => {
    setExplorer((current) => (sameId(current.tableId, next.tableId) ? next : current))
  }, [])

  const loadModel = useCallback(() => {
    if (!datasetId) return

    loadControllerRef.current?.abort()
    const controller = new AbortController()
    loadControllerRef.current = controller

    const load = async () => {
      setOwnLoading(true)
      setOwnError('')
      try {
        const response = await fetch(`${config.baseUrl}/api/datasets/${datasetId}/package-explorer/`, {
          credentials: 'include',
          signal: controller.signal,
        })
        const data = await response.json()
        if (!response.ok) {
          throw new Error(data?.detail || data?.error || 'The package model could not be loaded.')
        }
        if (controller.signal.aborted) return
        setOwnModel(data)
      } catch (loadError) {
        if (loadError.name !== 'AbortError' && loadControllerRef.current === controller) {
          setOwnError(loadError.message)
        }
      } finally {
        if (loadControllerRef.current === controller) setOwnLoading(false)
      }
    }

    load()
  }, [datasetId])

  // Opening the modal loads its own model, or with a shared one asks for it only when there is none yet.
  useEffect(() => {
    onShowRef.current = () => {
      if (!shared) loadModel()
      else if (!sharedModel && !modelLoading) onReloadModel?.()
    }
  })

  useEffect(() => {
    const modal = document.getElementById('packageExplorerModal')
    if (!modal) return undefined
    const onShow = () => {
      setOpen(true)
      onShowRef.current?.()
    }
    const onShown = () => setVisible(true)
    const onHidden = () => {
      setOpen(false)
      setVisible(false)
      setHoveredNodeId(null)
    }
    // openPackageExplorer({tableId, state} | {focus}) chooses what opens.
    const onOpenRequest = (event) => {
      const { model: currentModel, sourceTables: currentSources, loading: modelBusy, explorer: current } = latestRef.current
      const next = explorerForRequest(current, event.detail, {
        model: currentModel,
        sourceTables: currentSources,
        modelSettled: Boolean(currentModel) && !modelBusy,
      })
      setEdgeId(null)
      setFocus(null)
      setPending(next.pending)
      setExplorer(next.explorer)
    }
    modal.addEventListener('show.bs.modal', onShow)
    modal.addEventListener('shown.bs.modal', onShown)
    modal.addEventListener('hidden.bs.modal', onHidden)
    modal.addEventListener(OPEN_REQUEST_EVENT, onOpenRequest)
    return () => {
      modal.removeEventListener('show.bs.modal', onShow)
      modal.removeEventListener('shown.bs.modal', onShown)
      modal.removeEventListener('hidden.bs.modal', onHidden)
      modal.removeEventListener(OPEN_REQUEST_EVENT, onOpenRequest)
      loadControllerRef.current?.abort()
    }
  }, [loadModel])

  // Reports the open table while the modal is open, and null once when it closes; changes while it is closed
  // (new tables, a new model) are not reported, so they cannot clear an address the page is restoring.
  const reportedOpenRef = useRef(false)
  useEffect(() => {
    if (open) {
      reportedOpenRef.current = true
      onExplorerStateChange?.(explorer)
    } else if (reportedOpenRef.current) {
      reportedOpenRef.current = false
      onExplorerStateChange?.(null)
    }
  }, [open, explorer, onExplorerStateChange])

  useEffect(() => {
    highlightRef.current = highlight
  })

  useEffect(() => {
    if (!visible || !graphRef.current || !model?.nodes?.length) return undefined
    const palette = graphPalette(isDark)
    const elements = [
      ...model.nodes.map((node) => ({
        group: 'nodes',
        data: {
          id: node.id,
          label: `${node.title}\n${node.rowCount.toLocaleString()} ${node.rowCount === 1 ? 'row' : 'rows'}`,
          category: graphCategory(node.id),
        },
      })),
      ...model.edges.map((edge) => ({
        group: 'edges',
        data: {
          id: edge.id,
          source: edge.source,
          target: edge.target,
          label: edge.predicate,
          kind: edge.kind,
        },
      })),
    ]

    const cy = cytoscape({
      container: graphRef.current,
      elements,
      minZoom: 0.25,
      maxZoom: 2.5,
      wheelSensitivity: 0.18,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': palette.records,
            'border-color': palette.background,
            'border-width': 3,
            color: '#ffffff',
            'font-size': 18,
            'font-weight': 600,
            height: 64,
            label: 'data(label)',
            shape: 'roundrectangle',
            'text-halign': 'center',
            'text-max-width': 150,
            'text-wrap': 'wrap',
            'text-valign': 'center',
            width: 168,
          },
        },
        { selector: 'node[category = "evidence"]', style: { 'background-color': palette.evidence } },
        { selector: 'node[category = "context"]', style: { 'background-color': palette.context } },
        { selector: 'node[category = "claims"]', style: { 'background-color': palette.claims } },
        {
          selector: 'edge',
          style: {
            'curve-style': 'bezier',
            'font-size': 13,
            label: 'data(label)',
            'line-color': palette.edge,
            'target-arrow-color': palette.edge,
            'target-arrow-shape': 'triangle',
            'text-background-color': palette.background,
            'text-background-opacity': 0.88,
            'text-background-padding': 2,
            'text-rotation': 'autorotate',
            color: palette.muted,
            width: 1.5,
          },
        },
        { selector: 'edge[kind = "weak"]', style: { 'line-style': 'dashed', opacity: 0.7 } },
        // Tables not linked to the open one stay readable: a pale tint of their colour with dark text.
        {
          selector: 'node.is-muted',
          style: {
            'background-opacity': 0.22,
            color: palette.foreground,
          },
        },
        {
          selector: 'edge.is-muted',
          style: {
            opacity: 0.35,
          },
        },
        {
          selector: 'node.is-path',
          style: {
            opacity: 1,
            'border-color': palette.selected,
            'border-width': 5,
          },
        },
        {
          selector: 'edge.is-path',
          style: {
            opacity: 1,
            'line-color': palette.selected,
            'target-arrow-color': palette.selected,
            width: 3,
          },
        },
        {
          selector: 'node:selected',
          style: {
            'border-color': palette.selected,
            'border-width': 5,
          },
        },
        {
          selector: 'edge:selected',
          style: {
            'line-color': palette.selected,
            'target-arrow-color': palette.selected,
            width: 3,
          },
        },
      ],
      layout: {
        name: model.nodes.length <= 14 ? 'concentric' : 'cose',
        animate: false,
        avoidOverlap: true,
        componentSpacing: 45,
        concentric: (node) => node.degree(),
        idealEdgeLength: 72,
        levelWidth: () => 2,
        minNodeSpacing: 36,
        nodeRepulsion: 95000,
        padding: GRAPH_PADDING,
        randomize: true,
        spacingFactor: 0.9,
      },
    })

    cy.on('tap', 'node', (event) => showNode(event.target.id()))
    cy.on('tap', 'edge', (event) => showEdge(event.target.id()))
    cy.on('mouseover', 'node', (event) => setHoveredNodeId(event.target.id()))
    cy.on('mouseout', 'node', () => setHoveredNodeId(null))
    cyRef.current = cy
    highlightGraph(cy, highlightRef.current)

    // The map panel changes size (Expand, window size, fullscreen on small screens): redraw and refit.
    let frame = 0
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
      window.cancelAnimationFrame(frame)
      frame = window.requestAnimationFrame(() => {
        cy.resize()
        cy.fit(cy.elements(), GRAPH_PADDING)
      })
    })
    observer?.observe(graphRef.current)
    frame = window.requestAnimationFrame(() => {
      cy.resize()
      cy.fit(cy.elements(), GRAPH_PADDING)
    })

    return () => {
      window.cancelAnimationFrame(frame)
      observer?.disconnect()
      cy.destroy()
      cyRef.current = null
    }
  }, [visible, model, isDark, showNode, showEdge])

  useEffect(() => {
    highlightGraph(cyRef.current, highlightRef.current)
  }, [highlightKey])

  const fitGraph = useCallback(() => {
    const cy = cyRef.current
    if (cy) cy.animate({ fit: { eles: cy.elements(), padding: GRAPH_PADDING }, duration: 250 })
  }, [])

  // Asking from inside the modal: close it first, then put the question in the chat box it was covering.
  const askFromModal = useCallback(async (text) => {
    const found = await getModal()
    if (!found) {
      onAsk?.(text)
      return
    }
    found.element.addEventListener('hidden.bs.modal', () => onAsk?.(text), { once: true })
    found.modal.hide()
  }, [onAsk])

  const hoveredNode = hoveredNodeId ? nodeMap.get(hoveredNodeId) : null
  const totalRows = (model?.nodes || []).reduce((sum, node) => sum + node.rowCount, 0)
  const taxonomyOnly = model?.packageType === 'taxonomy-data-package'
  const pickerTables = useMemo(() => [
    ...(model?.nodes || []).map((node) => ({
      id: node.tableId,
      title: node.title,
      resource: node.id,
      row_count: node.rowCount,
      is_dwc_dp: true,
    })),
    ...sourceTables.map((table) => ({ ...table, is_dwc_dp: false })),
  ], [model, sourceTables])
  const selectedSource = selectedNode
    ? null
    : sourceTables.find((table) => sameId(table.id, explorer.tableId)) || null
  const selectedTable = useMemo(
    () => (selectedNode ? tableFromNode(selectedNode, String(modelVersion)) : selectedSource),
    [selectedNode, selectedSource, modelVersion]
  )
  const hasPackage = (model?.nodes?.length || 0) > 0
  const sourcesOnly = !hasPackage && sourceTables.length > 0
  const showWorkspace = (!error && hasPackage) || (sourcesOnly && Boolean(selectedSource))
  const titleOf = (id) => nodeMap.get(id)?.title || formatResourceName(id)

  const renderEdge = (edge) => {
    const sourceNode = nodeMap.get(edge.source)
    const sourceField = edge.sourceFields.length === 1 ? edge.sourceFields[0] : null
    return (
      <div className="package-edge-detail">
        <div className="package-edge-heading">
          <span>Relationship</span>
          <h3 className="h6">
            {titleOf(edge.source)}{' '}
            <em>{edge.predicate}</em>{' '}
            {titleOf(edge.target)}
          </h3>
        </div>
        <div className="package-edge-route">
          <code>{edge.source}.{edge.sourceFields.join(', ')}</code>
          <i className="bi bi-arrow-right" aria-hidden="true"></i>
          <code>{edge.target}.{edge.targetFields.join(', ')}</code>
        </div>
        <p className="package-inspector-description">
          Rows in {titleOf(edge.source)} use these fields to connect to {titleOf(edge.target)}.
        </p>
        {edge.coverage === 'skipped' ? (
          <div className="alert alert-secondary small">
            These links were not checked: the package is too large to compare every row here.
            {edge.kind === 'weak' && ' This is an optional weak relationship in the standard.'}
          </div>
        ) : (
        <>
        <div className="package-link-coverage">
          <div>
            <strong>{edge.linkedRows.toLocaleString()}</strong>
            <span>linked rows</span>
          </div>
          <div>
            <strong>{edge.unmatchedRows.toLocaleString()}</strong>
            <span>unmatched</span>
          </div>
          <div>
            <strong>{edge.blankRows.toLocaleString()}</strong>
            <span>without a link</span>
          </div>
        </div>
        <div className={`alert ${edge.unmatchedRows > 0 ? 'alert-warning' : 'alert-success'} small`}>
          {edge.unmatchedRows > 0
            ? `${pluralize(edge.unmatchedRows, 'populated reference')} could not be matched in the related table.`
            : `Every populated reference matches a record in ${titleOf(edge.target)}.`}
          {edge.kind === 'weak' && ' This is an optional weak relationship in the standard.'}
        </div>
        </>
        )}
        <div className="package-edge-actions">
          <button type="button" className="btn btn-sm btn-outline-secondary" onClick={() => showNode(edge.source)}>
            <i className="bi bi-table me-1" aria-hidden="true"></i>Open {titleOf(edge.source)}
          </button>
          <button type="button" className="btn btn-sm btn-outline-secondary" onClick={() => showNode(edge.target)}>
            <i className="bi bi-table me-1" aria-hidden="true"></i>Open {titleOf(edge.target)}
          </button>
          {sourceField && edge.blankRows > 0 && sourceNode && (
            <button
              type="button"
              className="btn btn-sm btn-outline-secondary"
              onClick={() => openRelated({
                tableId: sourceNode.tableId,
                filters: [{ column: sourceField, op: 'blank' }],
              })}
            >
              Show the {pluralize(edge.blankRows, `${titleOf(edge.source)} row`)} without a link
            </button>
          )}
        </div>
      </div>
    )
  }

  return (
    <div
      className="modal fade"
      id="packageExplorerModal"
      tabIndex="-1"
      aria-labelledby="packageExplorerModalLabel"
      aria-hidden="true"
    >
      <div className="modal-dialog package-explorer-dialog modal-fullscreen-lg-down">
        <div className="modal-content package-explorer-modal">
          <div className="modal-header package-explorer-header">
            <div>
              <div className="package-explorer-kicker">
                {sourcesOnly ? 'Your data' : taxonomyOnly ? 'Taxonomy data package' : 'Darwin Core Data Package'}
              </div>
              <h5 className="modal-title" id="packageExplorerModalLabel">
                {sourcesOnly ? 'Your tables' : 'How the tables connect'}
              </h5>
            </div>
            <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>

          <div className="modal-body package-explorer-body">
            {loading && !model && !selectedSource && (
              <div className="package-explorer-state">
                <div className="spinner-border text-success" role="status">
                  <span className="visually-hidden">Loading package explorer…</span>
                </div>
                <span>Loading…</span>
              </div>
            )}
            {!loading && error && (
              <div className="alert alert-danger m-3 d-flex align-items-center gap-2" role="alert">
                <span>{error}</span>
                <button
                  type="button"
                  className="btn btn-sm btn-outline-danger ms-auto"
                  onClick={() => (shared ? onReloadModel?.() : loadModel())}
                >
                  Retry
                </button>
              </div>
            )}
            {!loading && !error && model?.nodes?.length === 0 && !sourcesOnly && (
              <div className="package-explorer-state">
                Package resources will appear here once ChatIPT has organised the source data.
              </div>
            )}
            {sourcesOnly && selectedSource && (
              <div className="package-explorer-summary">
                <div>
                  <strong>{pluralize(sourceTables.length, 'table')} from your uploads</strong>
                  <span>The linked Darwin Core tables and their map will appear here once ChatIPT has organised your data.</span>
                </div>
              </div>
            )}
            {!error && model?.nodes?.length > 0 && (
              <>
                <div className="package-explorer-summary">
                  <div>
                    <strong>
                      {pluralize(model.nodes.length, 'linked table')} containing {pluralize(totalRows, 'package row')}
                      {publishedUrl && (
                        <>
                          ,{' '}
                          <a href={publishedUrl} target="_blank" rel="noopener noreferrer">
                            published on the GBIF Sandbox
                            <i className="bi bi-box-arrow-up-right ms-1" aria-hidden="true"></i>
                            <span className="visually-hidden"> (opens in a new tab)</span>
                          </a>
                        </>
                      )}
                    </strong>
                    <span>
                      {pluralize(model.edges.length, 'table relationship')}{taxonomyOnly ? '' : ` · DwC-DP schema ${model.schema?.version || 'Unknown'}`}
                    </span>
                  </div>
                  {loading ? (
                    <span className="badge text-bg-secondary">
                      <span className="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>
                      Updating…
                    </span>
                  ) : (
                    <span className="badge text-bg-success">
                      <i className="bi bi-check-circle me-1" aria-hidden="true"></i>
                      Current package
                    </span>
                  )}
                </div>
              </>
            )}
            {showWorkspace && (
              <>
                <div className={`package-explorer-workspace${mapExpanded ? ' is-map-expanded' : ''}${hasPackage ? '' : ' is-sources-only'}`}>
                  {hasPackage && (
                    <section className="package-graph-panel" aria-label="Package relationship map">
                      <div className="package-graph-toolbar">
                        <div className="package-graph-legend" aria-label="Table type legend">
                          <span><i className="legend-dot legend-records"></i>{taxonomyOnly ? 'Checklist' : 'Core records'}</span>
                          <span><i className="legend-dot legend-evidence"></i>{taxonomyOnly ? 'Attached tables' : 'Evidence'}</span>
                          {!taxonomyOnly && <>
                            <span><i className="legend-dot legend-context"></i>Context</span>
                            <span><i className="legend-dot legend-claims"></i>Claims</span>
                          </>}
                        </div>
                        <div className="package-graph-actions">
                          <button type="button" className="btn btn-sm btn-outline-secondary" onClick={fitGraph} title="Fit the whole map in view">
                            <i className="bi bi-bullseye me-1" aria-hidden="true"></i>
                            Fit
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm btn-outline-secondary"
                            aria-pressed={mapExpanded}
                            onClick={() => setMapExpanded((value) => !value)}
                            title={mapExpanded ? 'Give the table more room again' : 'Make the map bigger'}
                          >
                            <i className={`bi ${mapExpanded ? 'bi-arrows-angle-contract' : 'bi-arrows-angle-expand'} me-1`} aria-hidden="true"></i>
                            {mapExpanded ? 'Shrink' : 'Expand'}
                          </button>
                        </div>
                      </div>
                      <div className="package-graph-stage">
                        <div ref={graphRef} className="package-graph-canvas" />
                        {hoveredNode && hoveredNode.id !== selectedNode?.id && (
                          <div className="package-graph-hover-card" aria-hidden="true">
                            <strong>{hoveredNode.title}</strong>
                            <span>{pluralize(hoveredNode.rowCount, 'row')}</span>
                            <small>{hoveredNode.description}</small>
                          </div>
                        )}
                      </div>
                      <details className="package-relationship-list">
                        <summary>Relationships as a list</summary>
                        <ul>
                          {model.edges.map((edge) => (
                            <li key={edge.id}>
                              <button
                                type="button"
                                aria-current={edge.id === selectedEdge?.id ? 'true' : undefined}
                                onClick={() => showEdge(edge.id)}
                              >
                                {titleOf(edge.source)}{' '}
                                <strong>{edge.predicate}</strong>{' '}
                                {titleOf(edge.target)}
                              </button>
                            </li>
                          ))}
                        </ul>
                      </details>
                    </section>
                  )}

                  <div className="package-inspector">
                    {selectedEdge && renderEdge(selectedEdge)}
                    {!selectedEdge && selectedTable && visible && (
                      <TableExplorer
                        key={selectedTable.id}
                        table={selectedTable}
                        state={explorer}
                        onStateChange={onExplorerChange}
                        packageModel={model}
                        onOpenRelated={openRelated}
                        onAsk={onAsk ? askFromModal : undefined}
                        headingLevel={3}
                        focusRequest={focus && sameId(focus.tableId, selectedTable.id) ? focus.request : null}
                        kicker={selectedNode ? (
                          <>
                            <i className={`legend-dot legend-${graphCategory(selectedNode.id)}`} aria-hidden="true"></i>
                            {selectedNode.id}
                          </>
                        ) : (
                          <span title="A working copy of what you uploaded. ChatIPT may have tidied it; your original files stay unchanged.">
                            <i className="bi bi-file-earmark-spreadsheet me-1" aria-hidden="true"></i>
                            Working table
                          </span>
                        )}
                        picker={pickerTables.length > 1 ? (
                          <TableList tables={pickerTables} selectedId={selectedTable.id} onSelect={pickTable} />
                        ) : null}
                      />
                    )}
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
