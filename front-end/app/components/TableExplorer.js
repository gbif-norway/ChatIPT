'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import config from '../config.js'
import { pluralize } from '../utils/datasetPresentation'
import {
  VIEW_COLUMNS,
  VIEW_ROWS,
  buildTableLinks,
  buildTableRelations,
  nodeForTable,
  outgoingByColumn,
  showRowsWithFilter,
} from '../utils/explorerState.mjs'
import { cellText, isBlankValue, normalizeFilters } from '../utils/rowFilters.mjs'
import { apiErrorMessage, columnStatsUrl, tableLineageUrl } from '../utils/tableApi.mjs'
import TableColumnsView from './TableColumnsView'
import TableRowsView from './TableRowsView'
import '../table-explorer.css'

const LONG_DESCRIPTION = 180
const VIEW_MEMORY_KEY = 'chatipt.tableExplorer.view'
let sessionView = null

// The last view a person chose (Columns or Rows), remembered for this browser session.
export const rememberedView = () => {
  if (sessionView) return sessionView
  try {
    const stored = window.sessionStorage.getItem(VIEW_MEMORY_KEY)
    if (stored === VIEW_COLUMNS || stored === VIEW_ROWS) sessionView = stored
  } catch {
    // Storage can be unavailable (private mode, blocked site data); the default is fine then.
  }
  return sessionView || VIEW_COLUMNS
}

export const rememberView = (view) => {
  if (view !== VIEW_COLUMNS && view !== VIEW_ROWS) return
  sessionView = view
  try {
    window.sessionStorage.setItem(VIEW_MEMORY_KEY, view)
  } catch {
    // Not remembered beyond this page; nothing else to do.
  }
}

// Fetches one JSON resource per `key`, abortable, keeping the previous result while a new version loads.
// Results are tagged with the request they answer, so an older result reads as "still loading".
function useTableResource(url, key, enabled = true) {
  const [attempt, setAttempt] = useState(0)
  const requestKey = `${key}#${attempt}`
  const [state, setState] = useState({ key: null, requestKey: null, data: null, error: '', status: 'idle' })

  useEffect(() => {
    if (!enabled || !url) return undefined
    const controller = new AbortController()
    fetch(url, { credentials: 'include', signal: controller.signal })
      .then(async (response) => {
        const payload = await response.json().catch(() => null)
        if (!response.ok) {
          const failure = new Error(apiErrorMessage(payload, response.status))
          failure.status = response.status
          throw failure
        }
        setState({ key, requestKey, data: payload, error: '', status: 'done' })
      })
      .catch((error) => {
        if (error.name === 'AbortError') return
        setState((current) => ({ ...current, key, requestKey, status: 'error', error: error.message || 'Could not load.' }))
      })
    return () => controller.abort()
  }, [url, key, requestKey, enabled])

  const retry = useCallback(() => setAttempt((value) => value + 1), [])
  const status = enabled && state.requestKey !== requestKey ? 'loading' : state.status
  return { ...state, status, retry }
}

function TableDescription({ text }) {
  const [open, setOpen] = useState(false)
  if (!text) return null
  const long = text.length > LONG_DESCRIPTION
  return (
    <p className={`te-description${long && !open ? ' is-clamped' : ''}`}>
      <span>{text}</span>
      {long && (
        <button type="button" className="btn btn-link btn-sm te-description-toggle" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          {open ? 'Less' : 'More'}
        </button>
      )}
    </p>
  )
}

/**
 * The one table viewer: header, a Columns | Rows switch and the chosen view.
 *
 * table: {id, title, row_count, columns, updated_at, description, is_dwc_dp}
 * state: {view, column, filters} for this table, controlled by the parent through onStateChange.
 * packageModel: the package-explorer model, when known, for field descriptions, key badges, link health
 *   and linked records. Source tables have no node in it and simply get no schema information.
 * onOpenRelated({tableId, filters}): the parent shows that table in Rows with those filters.
 * onAsk(text): optional; when absent the "Ask ChatIPT" buttons are hidden.
 * focusRequest: when it changes to a new truthy value (and on mount with one), focus moves to the table's
 *   heading, so people who followed a link from a record land somewhere sensible.
 */
export default function TableExplorer({
  table,
  state,
  onStateChange,
  packageModel = null,
  onOpenRelated,
  onAsk,
  picker = null,
  kicker = null,
  actions = null,
  headerExtra = null,
  headingLevel = 2,
  focusRequest = null,
}) {
  const headingRef = useRef(null)
  const tableId = table.id
  const versionKey = `${tableId}|${table.updated_at || ''}`
  const node = useMemo(() => nodeForTable(packageModel, tableId), [packageModel, tableId])
  const fields = node?.fields || null
  const links = useMemo(() => buildTableLinks(packageModel, tableId), [packageModel, tableId])
  const relations = useMemo(() => buildTableRelations(packageModel, tableId), [packageModel, tableId])
  const title = node?.title || table.title || 'Table'
  const description = node?.description || table.description || ''
  const columns = useMemo(() => (Array.isArray(table.columns) ? table.columns : []), [table.columns])
  const view = state?.view === VIEW_ROWS ? VIEW_ROWS : VIEW_COLUMNS
  const filters = useMemo(() => normalizeFilters(state?.filters), [state?.filters])

  const statsResource = useTableResource(columnStatsUrl(config.baseUrl, tableId), versionKey)
  const stats = statsResource.data && String(statsResource.data.table) === String(tableId) ? statsResource.data : null
  const statsLoading = statsResource.status === 'loading'
  const statsError = statsResource.status === 'error'
    ? 'The column summary could not be loaded.'
    : ''

  // Lineage is optional: a missing endpoint, an error or "not available" all mean no lineage shown.
  const lineageResource = useTableResource(tableLineageUrl(config.baseUrl, tableId), versionKey, table.is_dwc_dp !== false)
  // Like the stats, the last lineage of this table stays shown while a newer version loads.
  const lineageForTable = lineageResource.key !== null && String(lineageResource.key).split('|')[0] === String(tableId)
  const lineage = lineageForTable && lineageResource.status !== 'error' && lineageResource.data?.available
    ? lineageResource.data.columns || null
    : null

  // The pinned bar's height, so each view's search box can pin itself directly underneath it.
  const sectionRef = useRef(null)
  const barRef = useRef(null)
  useEffect(() => {
    const bar = barRef.current
    const section = sectionRef.current
    if (!bar || !section || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(() => {
      section.style.setProperty('--te-bar-h', `${bar.offsetHeight}px`)
    })
    observer.observe(bar)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    if (focusRequest) headingRef.current?.focus()
  }, [focusRequest])

  // Rows stay mounted once opened, so switching back keeps their search, sort and page.
  const [rowsOpenedFor, setRowsOpenedFor] = useState(view === VIEW_ROWS ? tableId : null)
  if (view === VIEW_ROWS && rowsOpenedFor !== tableId) setRowsOpenedFor(tableId)
  const rowsMounted = rowsOpenedFor === tableId

  const update = useCallback((patch) => onStateChange?.({ ...state, ...patch }), [onStateChange, state])
  const setView = (next) => {
    rememberView(next)
    update({ view: next })
  }
  const onSelectColumn = useCallback((column) => update({ column: column || null }), [update])
  const onFiltersChange = useCallback((next) => update({ filters: normalizeFilters(next) }), [update])
  const onFilter = useCallback((filter) => {
    rememberView(VIEW_ROWS)
    onStateChange?.(showRowsWithFilter({ ...state, filters }, filter))
  }, [onStateChange, state, filters])

  const fkByColumn = useMemo(() => outgoingByColumn(relations), [relations])
  const renderCell = useMemo(() => {
    if (!fkByColumn.size || !onOpenRelated) return undefined
    return function renderLinkCell(column, value) {
      const relation = fkByColumn.get(column)
      if (!relation || isBlankValue(value)) return null
      const text = cellText(value).trim()
      return (
        <button
          type="button"
          className="btn btn-link btn-sm p-0 te-fk-link"
          tabIndex={-1}
          title={`Open the ${relation.targetTitle} row where ${relation.targetColumn} is ${text}`}
          onClick={() => onOpenRelated({
            tableId: relation.targetTableId,
            filters: [{ column: relation.targetColumn, op: 'equals', value: text }],
          })}
        >
          {cellText(value)}
        </button>
      )
    }
  }, [fkByColumn, onOpenRelated])

  const rowCount = stats && Number.isFinite(Number(stats.row_count)) ? Number(stats.row_count) : Number(table.row_count) || 0
  const Heading = `h${Math.min(Math.max(headingLevel, 2), 6)}`

  return (
    <section className="te" ref={sectionRef} aria-label={`${title} table`} data-table-id={tableId}>
      {/* Pinned while the panel scrolls: which table, and which view. */}
      <div className="te-bar" ref={barRef}>
        <div className="te-heading">
          {kicker && <div className="te-kicker">{kicker}</div>}
          {picker ? (
            <>
              <Heading ref={headingRef} tabIndex={-1} className="visually-hidden">{title}</Heading>
              {picker}
            </>
          ) : (
            <Heading ref={headingRef} tabIndex={-1} className="te-title">{title}</Heading>
          )}
        </div>
        <div className="te-header-side">
          <div className="te-view-toggle" role="group" aria-label="Show this table as">
            <button
              type="button"
              className={view === VIEW_COLUMNS ? 'is-active' : ''}
              aria-pressed={view === VIEW_COLUMNS}
              onClick={() => setView(VIEW_COLUMNS)}
              title="One line per column: how filled it is and which values it holds"
            >
              <i className="bi bi-layout-three-columns me-1" aria-hidden="true"></i>Columns
            </button>
            <button
              type="button"
              className={view === VIEW_ROWS ? 'is-active' : ''}
              aria-pressed={view === VIEW_ROWS}
              onClick={() => setView(VIEW_ROWS)}
              title="The records themselves, with search and filters"
            >
              <i className="bi bi-table me-1" aria-hidden="true"></i>Rows
              {filters.length > 0 && (
                <span className="te-filter-count">
                  {filters.length}
                  <span className="visually-hidden"> {filters.length === 1 ? 'filter' : 'filters'} on</span>
                </span>
              )}
            </button>
          </div>
          {actions}
        </div>
      </div>
      <header className="te-header">
        <div className="te-meta">
          <span>{pluralize(rowCount, 'row')}</span>
          <span aria-hidden="true">·</span>
          <span>{pluralize(columns.length, 'column')}</span>
          <span aria-hidden="true">·</span>
          <span>{table.is_dwc_dp ? 'Published table' : 'Source file'}</span>
        </div>
        <TableDescription key={tableId} text={description} />
        {headerExtra}
      </header>

      <div className="te-view" hidden={view !== VIEW_COLUMNS}>
        <TableColumnsView
          tableId={tableId}
          tableTitle={title}
          stats={stats}
          statsLoading={statsLoading}
          statsError={statsError}
          onRetryStats={statsResource.retry}
          fields={fields}
          links={links}
          lineage={lineage}
          selectedColumn={state?.column || null}
          onSelectColumn={onSelectColumn}
          onFilter={onFilter}
          onAsk={onAsk}
        />
      </div>
      {rowsMounted && (
        <div className="te-view" hidden={view !== VIEW_ROWS}>
          <TableRowsView
            key={tableId}
            tableId={tableId}
            tableTitle={title}
            columns={columns}
            totalRows={table.row_count}
            version={table.updated_at || ''}
            filters={filters}
            onFiltersChange={onFiltersChange}
            stats={stats}
            fields={fields}
            relations={relations}
            onOpenRelated={onOpenRelated}
            onAsk={onAsk}
            renderCell={renderCell}
          />
        </div>
      )}
    </section>
  )
}
