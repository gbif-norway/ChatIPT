'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import config from '../config.js'
import { useTheme } from '../contexts/ThemeContext'
import { pluralize } from '../utils/datasetPresentation'

const NODE_PRIORITY = [
  'event',
  'occurrence',
  'material',
  'identification',
  'organism',
  'survey',
  'media',
  'agent',
]

const formatResourceName = (name) => String(name || '')
  .split('-')
  .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
  .join(' ')

const formatValue = (value) => {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

const joinKey = (row, fields) => {
  if (!row || !Array.isArray(fields) || fields.length === 0) return null
  const values = fields.map((field) => {
    const value = row[field]
    if (value === null || value === undefined) return null
    const text = typeof value === 'object' ? JSON.stringify(value) : String(value).trim()
    return text || null
  })
  return values.some((value) => value === null) ? null : values.join('\u001f')
}

const graphCategory = (name) => {
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

const recordSignature = (node, row, rowIndex) => {
  const identityFields = node?.primaryKey?.length
    ? node.primaryKey
    : (node?.weakPrimaryKey?.length ? node.weakPrimaryKey : [])
  const identity = joinKey(row, identityFields)
  return identity || `row-${rowIndex}`
}

const parseExamples = (value) => String(value || '')
  .split(';')
  .map((example) => example.replaceAll('`', '').trim())
  .filter(Boolean)

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

export default function PackageExplorer({ datasetId, tables, onOpenTable }) {
  const graphRef = useRef(null)
  const cyRef = useRef(null)
  const loadControllerRef = useRef(null)
  const { isDark } = useTheme()
  const [model, setModel] = useState(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [visible, setVisible] = useState(false)
  const [selection, setSelection] = useState(null)
  const [hoveredNodeId, setHoveredNodeId] = useState(null)
  const [trace, setTrace] = useState(null)

  const tableMap = useMemo(
    () => new Map((tables || []).map((table) => [table.title, table])),
    [tables]
  )
  const nodeMap = useMemo(
    () => new Map((model?.nodes || []).map((node) => [node.id, node])),
    [model]
  )

  const loadModel = useCallback(() => {
    if (!datasetId) return

    loadControllerRef.current?.abort()
    const controller = new AbortController()
    loadControllerRef.current = controller

    const load = async () => {
      setLoading(true)
      setError('')
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

        setModel(data)
        const firstNode = [...(data.nodes || [])].sort((a, b) => {
          const aIndex = NODE_PRIORITY.indexOf(a.id)
          const bIndex = NODE_PRIORITY.indexOf(b.id)
          if (aIndex === -1 && bIndex === -1) return a.title.localeCompare(b.title)
          if (aIndex === -1) return 1
          if (bIndex === -1) return -1
          return aIndex - bIndex
        })[0]
        setSelection(firstNode ? { type: 'node', id: firstNode.id } : null)
        setTrace(null)
      } catch (loadError) {
        if (loadError.name !== 'AbortError' && loadControllerRef.current === controller) {
          setError(loadError.message)
        }
      } finally {
        if (loadControllerRef.current === controller) setLoading(false)
      }
    }

    load()
  }, [datasetId])

  useEffect(() => {
    const modal = document.getElementById('packageExplorerModal')
    if (!modal) return undefined
    const onShow = () => loadModel()
    const onShown = () => setVisible(true)
    const onHidden = () => {
      setVisible(false)
      setHoveredNodeId(null)
    }
    modal.addEventListener('show.bs.modal', onShow)
    modal.addEventListener('shown.bs.modal', onShown)
    modal.addEventListener('hidden.bs.modal', onHidden)
    return () => {
      modal.removeEventListener('show.bs.modal', onShow)
      modal.removeEventListener('shown.bs.modal', onShown)
      modal.removeEventListener('hidden.bs.modal', onHidden)
      loadControllerRef.current?.abort()
    }
  }, [loadModel])

  const relationshipIndexes = useMemo(() => {
    const indexes = new Map()
    for (const edge of model?.edges || []) {
      const sourceRows = tableMap.get(edge.source)?.df || []
      const targetRows = tableMap.get(edge.target)?.df || []
      const sourceIndex = new Map()
      const targetIndex = new Map()
      sourceRows.forEach((row, rowIndex) => {
        const key = joinKey(row, edge.sourceFields)
        if (!key) return
        if (!sourceIndex.has(key)) sourceIndex.set(key, [])
        sourceIndex.get(key).push({ row, rowIndex })
      })
      targetRows.forEach((row, rowIndex) => {
        const key = joinKey(row, edge.targetFields)
        if (!key) return
        if (!targetIndex.has(key)) targetIndex.set(key, [])
        targetIndex.get(key).push({ row, rowIndex })
      })
      indexes.set(edge.id, { sourceIndex, targetIndex })
    }
    return indexes
  }, [model, tableMap])

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
      minZoom: 0.35,
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
            'font-size': 11,
            'font-weight': 500,
            height: 56,
            label: 'data(label)',
            shape: 'roundrectangle',
            'text-halign': 'center',
            'text-max-width': 130,
            'text-wrap': 'wrap',
            'text-valign': 'center',
            width: 148,
          },
        },
        { selector: 'node[category = "evidence"]', style: { 'background-color': palette.evidence } },
        { selector: 'node[category = "context"]', style: { 'background-color': palette.context } },
        { selector: 'node[category = "claims"]', style: { 'background-color': palette.claims } },
        {
          selector: 'edge',
          style: {
            'curve-style': 'bezier',
            'font-size': 9,
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
        {
          selector: '.is-muted',
          style: {
            opacity: 0.16,
            'text-opacity': 0.08,
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
      ],
      layout: {
        name: model.nodes.length <= 14 ? 'concentric' : 'cose',
        animate: false,
        avoidOverlap: true,
        componentSpacing: 45,
        concentric: (node) => node.degree(),
        idealEdgeLength: 72,
        levelWidth: () => 2,
        minNodeSpacing: 80,
        nodeRepulsion: 95000,
        padding: 55,
        randomize: true,
        spacingFactor: 1.15,
      },
    })

    cy.on('tap', 'node', (event) => {
      setSelection({ type: 'node', id: event.target.id() })
    })
    cy.on('tap', 'edge', (event) => {
      setSelection({ type: 'edge', id: event.target.id() })
    })
    cy.on('mouseover', 'node', (event) => setHoveredNodeId(event.target.id()))
    cy.on('mouseout', 'node', () => setHoveredNodeId(null))
    cyRef.current = cy
    requestAnimationFrame(() => {
      cy.resize()
      cy.fit(cy.elements(), 55)
    })

    return () => {
      cy.destroy()
      cyRef.current = null
    }
  }, [visible, model, isDark])

  useEffect(() => {
    const cy = cyRef.current
    if (!cy) return
    cy.elements().removeClass('is-muted is-path')

    if (trace) {
      cy.elements().addClass('is-muted')
      trace.nodeIds.forEach((id) => cy.getElementById(id).removeClass('is-muted').addClass('is-path'))
      trace.edgeIds.forEach((id) => cy.getElementById(id).removeClass('is-muted').addClass('is-path'))
    } else if (selection?.type === 'node') {
      const node = cy.getElementById(selection.id)
      cy.elements().addClass('is-muted')
      node.closedNeighborhood().removeClass('is-muted')
      node.select()
    } else if (selection?.type === 'edge') {
      const edge = cy.getElementById(selection.id)
      cy.elements().addClass('is-muted')
      edge.removeClass('is-muted')
      edge.connectedNodes().removeClass('is-muted')
      edge.select()
    }
  }, [selection, trace])

  const followRecord = useCallback((originNodeId, originRow, originRowIndex) => {
    const visited = new Map()
    const queue = []
    const traversedEdges = new Set()

    const addRecord = (nodeId, row, rowIndex, depth) => {
      const node = nodeMap.get(nodeId)
      if (!node) return false
      const signature = recordSignature(node, row, rowIndex)
      if (!visited.has(nodeId)) visited.set(nodeId, new Map())
      const nodeRecords = visited.get(nodeId)
      if (nodeRecords.has(signature) || nodeRecords.size >= 8) return false
      const entry = { row, rowIndex, depth, signature }
      nodeRecords.set(signature, entry)
      queue.push({ nodeId, ...entry })
      return true
    }

    addRecord(originNodeId, originRow, originRowIndex, 0)
    while (queue.length > 0 && [...visited.values()].reduce((sum, rows) => sum + rows.size, 0) < 40) {
      const current = queue.shift()
      if (current.depth >= 2) continue
      for (const edge of model?.edges || []) {
        const indexes = relationshipIndexes.get(edge.id)
        if (!indexes) continue

        if (edge.source === current.nodeId) {
          const key = joinKey(current.row, edge.sourceFields)
          const matches = key ? (indexes.targetIndex.get(key) || []) : []
          matches.forEach((match) => addRecord(edge.target, match.row, match.rowIndex, current.depth + 1))
          if (matches.length > 0) traversedEdges.add(edge.id)
        }
        if (edge.target === current.nodeId) {
          const key = joinKey(current.row, edge.targetFields)
          const matches = key ? (indexes.sourceIndex.get(key) || []) : []
          matches.forEach((match) => addRecord(edge.source, match.row, match.rowIndex, current.depth + 1))
          if (matches.length > 0) traversedEdges.add(edge.id)
        }
      }
    }

    const groups = [...visited.entries()]
      .map(([nodeId, records]) => ({
        node: nodeMap.get(nodeId),
        records: [...records.values()],
        minimumDepth: Math.min(...[...records.values()].map((record) => record.depth)),
      }))
      .sort((a, b) => a.minimumDepth - b.minimumDepth || a.node.title.localeCompare(b.node.title))
    setTrace({
      originNodeId,
      originSignature: recordSignature(nodeMap.get(originNodeId), originRow, originRowIndex),
      groups,
      nodeIds: groups.map((group) => group.node.id),
      edgeIds: [...traversedEdges],
    })
    setSelection({ type: 'node', id: originNodeId })
  }, [model, nodeMap, relationshipIndexes])

  const resetTrace = useCallback(() => {
    setTrace(null)
  }, [])

  const fitGraph = useCallback(() => {
    const cy = cyRef.current
    if (cy) cy.animate({ fit: { eles: cy.elements(':visible'), padding: 55 }, duration: 250 })
  }, [])

  const selectedNode = selection?.type === 'node' ? nodeMap.get(selection.id) : null
  const selectedEdge = selection?.type === 'edge'
    ? model?.edges?.find((edge) => edge.id === selection.id)
    : null
  const hoveredNode = hoveredNodeId ? nodeMap.get(hoveredNodeId) : null
  const selectedTraceGroup = selectedNode && trace
    ? trace.groups.find((group) => group.node.id === selectedNode.id)
    : null
  const selectedRows = selectedNode
    ? (selectedTraceGroup?.records || (tableMap.get(selectedNode.id)?.df || []).slice(0, 6).map((row, rowIndex) => ({ row, rowIndex })))
    : []
  const totalRows = (model?.nodes || []).reduce((sum, node) => sum + node.rowCount, 0)
  const examples = selectedNode ? parseExamples(selectedNode.examples).slice(0, 2) : []

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
              <div className="package-explorer-kicker">Darwin Core Data Package</div>
              <h5 className="modal-title" id="packageExplorerModalLabel">Explore how your data connects</h5>
            </div>
            <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
          </div>

          <div className="modal-body package-explorer-body">
            {loading && (
              <div className="package-explorer-state">
                <div className="spinner-border text-success" role="status">
                  <span className="visually-hidden">Loading package explorer…</span>
                </div>
                <span>Building your package map…</span>
              </div>
            )}
            {!loading && error && (
              <div className="alert alert-danger m-3" role="alert">{error}</div>
            )}
            {!loading && !error && model?.nodes?.length === 0 && (
              <div className="package-explorer-state">
                Package resources will appear here once ChatIPT has organised the source data.
              </div>
            )}
            {!loading && !error && model?.nodes?.length > 0 && (
              <>
                <div className="package-explorer-summary">
                  <div>
                    <strong>
                      {pluralize(model.nodes.length, 'linked table')} containing {pluralize(totalRows, 'package row')}
                    </strong>
                    <span>
                      {pluralize(model.edges.length, 'schema relationship')} in DwC-DP schema {model.schema?.version || 'Unknown'}
                    </span>
                  </div>
                  <span className="badge text-bg-success">
                    <i className="bi bi-check-circle me-1" aria-hidden="true"></i>
                    Current package
                  </span>
                </div>

                {trace && (
                  <div className="package-explorer-journey" role="status">
                    <div>
                      <i className="bi bi-signpost-split me-2" aria-hidden="true"></i>
                      <strong>Record journey:</strong>{' '}
                      {pluralize(trace.groups.length, 'connected table')} highlighted
                    </div>
                    <button type="button" className="btn btn-sm btn-outline-secondary" onClick={resetTrace}>
                      Clear journey
                    </button>
                  </div>
                )}

                <div className="package-explorer-workspace">
                  <section className="package-graph-panel" aria-label="Interactive package relationship map">
                    <div className="package-graph-toolbar">
                      <div className="package-graph-legend" aria-label="Table type legend">
                        <span><i className="legend-dot legend-records"></i>Core records</span>
                        <span><i className="legend-dot legend-evidence"></i>Evidence</span>
                        <span><i className="legend-dot legend-context"></i>Context</span>
                        <span><i className="legend-dot legend-claims"></i>Claims</span>
                      </div>
                      <button type="button" className="btn btn-sm btn-outline-secondary" onClick={fitGraph}>
                        <i className="bi bi-arrows-fullscreen me-1" aria-hidden="true"></i>
                        Fit
                      </button>
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
                              onClick={() => setSelection({ type: 'edge', id: edge.id })}
                            >
                              {nodeMap.get(edge.source)?.title || formatResourceName(edge.source)}{' '}
                              <strong>{edge.predicate}</strong>{' '}
                              {nodeMap.get(edge.target)?.title || formatResourceName(edge.target)}
                            </button>
                          </li>
                        ))}
                      </ul>
                    </details>
                  </section>

                  <aside className="package-inspector" aria-live="polite">
                    {selectedNode && (
                      <>
                        <div className="package-inspector-heading">
                          <div className={`package-node-mark package-node-${graphCategory(selectedNode.id)}`}>
                            <i className="bi bi-table" aria-hidden="true"></i>
                          </div>
                          <div>
                            <span>{selectedNode.id}</span>
                            <h6>{selectedNode.title}</h6>
                            <small>
                              {pluralize(selectedNode.rowCount, 'row')} · {pluralize(selectedNode.columnCount, 'column')}
                            </small>
                          </div>
                        </div>
                        <p className="package-inspector-description">{selectedNode.description}</p>
                        {examples.length > 0 && (
                          <div className="package-examples">
                            <strong>For example</strong>
                            {examples.join('; ')}.
                          </div>
                        )}

                        {trace && (
                          <div className="package-trace-groups">
                            {trace.groups.map((group) => (
                              <button
                                type="button"
                                key={group.node.id}
                                className={group.node.id === selectedNode.id ? 'active' : ''}
                                onClick={() => setSelection({ type: 'node', id: group.node.id })}
                              >
                                <span>{group.node.title}</span>
                                <small>{pluralize(group.records.length, 'connected record')}</small>
                              </button>
                            ))}
                          </div>
                        )}

                        <div className="package-inspector-section">
                          <div className="package-inspector-section-title">
                            <strong>
                              {trace ? 'Connected records (up to 6 shown)' : 'Data preview (first 6 rows)'}
                            </strong>
                            <button
                              type="button"
                              className="btn btn-sm btn-link"
                              data-bs-dismiss="modal"
                              onClick={() => onOpenTable?.(selectedNode.tableId)}
                            >
                              Open full table
                            </button>
                          </div>
                          {selectedRows.length > 0 ? (
                            <div className="table-responsive package-preview-table">
                              <table className="table table-sm align-middle mb-0">
                                <thead>
                                  <tr>
                                    <th scope="col" className="package-follow-cell"><span className="visually-hidden">Follow</span></th>
                                    {selectedNode.fields.map((field) => (
                                      <th scope="col" key={field.name}>
                                        <span>{field.name}</span>
                                        {(field.primary || field.weakPrimary) && (
                                          <i
                                            className={`bi ${field.primary ? 'bi-key-fill' : 'bi-key'} ms-1`}
                                            title={field.primary ? 'Primary key' : 'Public identifier'}
                                            aria-label={field.primary ? 'Primary key' : 'Public identifier'}
                                          ></i>
                                        )}
                                      </th>
                                    ))}
                                  </tr>
                                </thead>
                                <tbody>
                                  {selectedRows.slice(0, 6).map((entry) => (
                                    <tr key={recordSignature(selectedNode, entry.row, entry.rowIndex)}>
                                      <td className="package-follow-cell">
                                        <button
                                          type="button"
                                          className="btn btn-sm btn-outline-success"
                                          title="Follow this record through connected tables"
                                          aria-label="Follow this record through connected tables"
                                          onClick={() => followRecord(selectedNode.id, entry.row, entry.rowIndex)}
                                        >
                                          <i className="bi bi-signpost-split" aria-hidden="true"></i>
                                        </button>
                                      </td>
                                      {selectedNode.fields.map((field) => (
                                        <td key={field.name} title={formatValue(entry.row[field.name])}>
                                          {formatValue(entry.row[field.name])}
                                        </td>
                                      ))}
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          ) : (
                            <p className="text-muted small mb-0">This table has no rows to preview.</p>
                          )}
                          <p className="package-preview-hint">
                            <i className="bi bi-signpost-split me-1" aria-hidden="true"></i>
                            Use the signpost to follow a record through its connected tables.
                          </p>
                        </div>

                        <details className="package-field-guide">
                          <summary>Fields in this table</summary>
                          <dl>
                            {selectedNode.fields.map((field) => (
                              <div key={field.name}>
                                <dt>
                                  {field.name}
                                  {field.primary && <span className="badge text-bg-success ms-2">Primary key</span>}
                                  {field.weakPrimary && <span className="badge text-bg-secondary ms-2">Public ID</span>}
                                </dt>
                                <dd>{field.description || field.title}</dd>
                              </div>
                            ))}
                          </dl>
                        </details>
                      </>
                    )}

                    {selectedEdge && (
                      <>
                        <div className="package-edge-heading">
                          <span>Relationship</span>
                          <h6>
                            {nodeMap.get(selectedEdge.source)?.title}{' '}
                            <em>{selectedEdge.predicate}</em>{' '}
                            {nodeMap.get(selectedEdge.target)?.title}
                          </h6>
                        </div>
                        <div className="package-edge-route">
                          <code>{selectedEdge.source}.{selectedEdge.sourceFields.join(', ')}</code>
                          <i className="bi bi-arrow-right" aria-hidden="true"></i>
                          <code>{selectedEdge.target}.{selectedEdge.targetFields.join(', ')}</code>
                        </div>
                        <p className="package-inspector-description">
                          Rows in {nodeMap.get(selectedEdge.source)?.title} use these fields to connect to{' '}
                          {nodeMap.get(selectedEdge.target)?.title}.
                        </p>
                        <div className="package-link-coverage">
                          <div>
                            <strong>{selectedEdge.linkedRows.toLocaleString()}</strong>
                            <span>linked rows</span>
                          </div>
                          <div>
                            <strong>{selectedEdge.unmatchedRows.toLocaleString()}</strong>
                            <span>unmatched</span>
                          </div>
                          <div>
                            <strong>{selectedEdge.blankRows.toLocaleString()}</strong>
                            <span>without a link</span>
                          </div>
                        </div>
                        <div className={`alert ${selectedEdge.unmatchedRows > 0 ? 'alert-warning' : 'alert-success'} small`}>
                          {selectedEdge.unmatchedRows > 0
                            ? `${pluralize(selectedEdge.unmatchedRows, 'populated reference')} could not be matched in the related table.`
                            : `Every populated reference matches a record in ${nodeMap.get(selectedEdge.target)?.title}.`}
                          {selectedEdge.kind === 'weak' && ' This is an optional weak relationship in the standard.'}
                        </div>
                      </>
                    )}
                  </aside>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
