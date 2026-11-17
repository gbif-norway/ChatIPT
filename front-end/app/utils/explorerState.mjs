// Pure helpers for the table explorer: which table, view, column and filters are shown,
// how that is written to and read from the page address, and the schema links between tables.
import { addFilter, normalizeFilters } from './rowFilters.mjs'

export const VIEW_COLUMNS = 'columns'
export const VIEW_ROWS = 'rows'
export const EXPLORER_VIEWS = [VIEW_COLUMNS, VIEW_ROWS]
export const DEFAULT_VIEW = VIEW_COLUMNS

// Query-string keys owned by the explorer. Anything else in the address is left alone.
export const EXPLORER_PARAMS = ['table', 'view', 'column', 'filters']

// Order of the DwC-DP tables in lists and which one opens first.
export const TABLE_PRIORITY = [
  'event',
  'occurrence',
  'taxonomy-taxon',
  'material',
  'identification',
  'organism',
  'survey',
  'media',
  'agent',
]

export const isExplorerView = (view) => EXPLORER_VIEWS.includes(view)

export const sameId = (a, b) => a !== null && a !== undefined && b !== null && b !== undefined && String(a) === String(b)

export const emptyExplorerState = (view = DEFAULT_VIEW) => ({
  tableId: null,
  view: isExplorerView(view) ? view : DEFAULT_VIEW,
  column: null,
  filters: [],
})

// Switching table keeps the view (Columns or Rows) and starts with no column picked and no filters.
export const selectTable = (state, tableId) => {
  const current = state || emptyExplorerState()
  if (sameId(current.tableId, tableId)) return current
  return { ...current, tableId: tableId ?? null, column: null, filters: [] }
}

export const setExplorerView = (state, view) => {
  if (!isExplorerView(view) || state.view === view) return state
  return { ...state, view }
}

// "Show these rows" from the Columns view: switch to Rows with that filter added to the others.
export const showRowsWithFilter = (state, filter) => ({
  ...state,
  view: VIEW_ROWS,
  filters: addFilter(state.filters || [], filter),
})

// Following a link to another table (or to other rows of the same one): Rows view with exactly these filters.
export const openRelatedState = (state, { tableId, filters }) => ({
  ...(state || emptyExplorerState()),
  tableId,
  view: VIEW_ROWS,
  column: sameId(state?.tableId, tableId) ? state.column : null,
  filters: normalizeFilters(filters),
})

// Filters travel in the address as compact arrays: [["sex","is","female"],["eventDate","blank"]].
export const encodeFilters = (filters) => {
  const clean = normalizeFilters(filters)
  if (!clean.length) return ''
  return JSON.stringify(clean.map((filter) => (
    filter.value === undefined ? [filter.column, filter.op] : [filter.column, filter.op, filter.value]
  )))
}

// Accepts the compact form and plain {column, op, value} objects. Anything unreadable becomes no filters.
export const decodeFilters = (text) => {
  if (!text) return []
  let parsed
  try {
    parsed = JSON.parse(text)
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []
  return normalizeFilters(parsed.map((entry) => {
    if (Array.isArray(entry)) return { column: entry[0], op: entry[1], value: entry[2] }
    return entry
  }))
}

const readParam = (params, key) => {
  if (!params) return null
  if (typeof params.get === 'function') return params.get(key)
  return params[key] ?? null
}

// Reads the explorer part of the address. Values are only checked for shape here;
// resolveExplorerState checks them against the tables that actually exist.
export const decodeExplorerState = (params) => {
  const table = String(readParam(params, 'table') ?? '').trim()
  const view = readParam(params, 'view')
  const column = readParam(params, 'column')
  return {
    tableId: /^\d+$/.test(table) ? table : null,
    view: isExplorerView(view) ? view : null,
    column: column ? String(column) : null,
    filters: decodeFilters(readParam(params, 'filters')),
  }
}

// Keeps only what still makes sense: a table that exists, a column and filters on columns that table has.
// Returns null when the address names no table we know, so the caller falls back to its default.
export const resolveExplorerState = (decoded, tables, fallbackView = DEFAULT_VIEW) => {
  if (!decoded?.tableId) return null
  const table = (tables || []).find((entry) => sameId(entry.id, decoded.tableId))
  if (!table) return null
  const columns = new Set(Array.isArray(table.columns) ? table.columns : [])
  return {
    tableId: table.id,
    view: decoded.view || (isExplorerView(fallbackView) ? fallbackView : DEFAULT_VIEW),
    column: decoded.column && columns.has(decoded.column) ? decoded.column : null,
    filters: normalizeFilters(decoded.filters).filter((filter) => columns.has(filter.column)),
  }
}

// After the table list is refreshed: keep the open table when it still exists (dropping a column or
// filters it no longer has, and keeping the same object when nothing changed), otherwise open the first table.
export const explorerAfterTablesChange = (current, nextTables) => {
  if (current.tableId !== null && current.tableId !== undefined) {
    const kept = resolveExplorerState({ ...current, tableId: String(current.tableId) }, nextTables, current.view)
    if (kept) {
      const unchanged = kept.column === current.column
        && JSON.stringify(kept.filters) === JSON.stringify(normalizeFilters(current.filters))
      return unchanged ? current : { ...current, column: kept.column, filters: kept.filters }
    }
  }
  return selectTable(current, orderTables(nextTables)[0]?.id ?? null)
}

// "Open on main page" from the package explorer: show that table with the view, column and filters it had there,
// keeping only a column and filters the main page's copy of the table has. When the main page does not know the
// table (its list is out of date), nothing changes.
export const openTableWithState = (current, tables, tableId, state = null) => {
  const base = current || emptyExplorerState()
  if (tableId === null || tableId === undefined) return base
  const resolved = resolveExplorerState({
    tableId: String(tableId),
    view: isExplorerView(state?.view) ? state.view : null,
    column: state?.column ?? null,
    filters: Array.isArray(state?.filters) ? state.filters : [],
  }, tables, base.view)
  if (!resolved) return base
  if (!state) return selectTable(base, resolved.tableId)
  return { ...base, ...resolved }
}

// Writes the explorer state into a query string, keeping unrelated parameters. Returns the new string without "?".
export const explorerSearchString = (currentSearch, state) => {
  const params = new URLSearchParams(currentSearch || '')
  EXPLORER_PARAMS.forEach((key) => params.delete(key))
  if (state?.tableId !== null && state?.tableId !== undefined && state.tableId !== '') {
    params.set('table', String(state.tableId))
    params.set('view', isExplorerView(state.view) ? state.view : DEFAULT_VIEW)
    if (state.column) params.set('column', state.column)
    const filters = encodeFilters(state.filters)
    if (filters) params.set('filters', filters)
  }
  return params.toString()
}

// DwC-DP tables first in the standard's usual order, then source files in the order the server gave.
// `resource` (the DwC-DP resource name) is used for the order when the shown title differs from it.
export const orderTables = (tables) => {
  const list = Array.isArray(tables) ? tables : []
  const rank = (table) => {
    const index = TABLE_PRIORITY.indexOf(String(table.resource || table.title || ''))
    return index === -1 ? TABLE_PRIORITY.length : index
  }
  const published = list
    .map((table, index) => ({ table, index }))
    .filter(({ table }) => table.is_dwc_dp)
    .sort((a, b) => rank(a.table) - rank(b.table) || a.index - b.index)
    .map(({ table }) => table)
  return [...published, ...list.filter((table) => !table.is_dwc_dp)]
}

export const groupTables = (tables) => {
  const ordered = orderTables(tables)
  return {
    published: ordered.filter((table) => table.is_dwc_dp),
    sources: ordered.filter((table) => !table.is_dwc_dp),
  }
}

// Package-explorer model helpers. Nodes carry `tableId`; edges point between node ids.
export const nodeForTable = (model, tableId) => (
  (model?.nodes || []).find((node) => sameId(node.tableId, tableId)) || null
)

// A table's examples from the standard ("`a`; `b`, `c`") as plain text, the first `limit` of them.
// Each example is quoted in backticks; text without backticks is split on semicolons.
export const tableExamples = (value, limit = 2) => {
  const text = String(value || '')
  const quoted = [...text.matchAll(/`([^`]+)`/g)].map((match) => match[1])
  const examples = quoted.length ? quoted : text.split(';')
  return examples.map((example) => example.trim()).filter(Boolean).slice(0, limit)
}

const nodeById = (model) => new Map((model?.nodes || []).map((node) => [node.id, node]))

const singleField = (fields) => (Array.isArray(fields) && fields.length === 1 ? fields[0] : null)

// Link health per foreign-key column of this table, for the Columns view.
export const buildTableLinks = (model, tableId) => {
  const node = nodeForTable(model, tableId)
  if (!node) return null
  const nodes = nodeById(model)
  const links = {}
  for (const edge of model?.edges || []) {
    if (edge.source !== node.id) continue
    const field = singleField(edge.sourceFields)
    if (!field || links[field]) continue
    links[field] = {
      targetTitle: nodes.get(edge.target)?.title || edge.target,
      predicate: edge.predicate,
      kind: edge.kind,
      // "skipped": the package was too large to check this link; the counts are null, not zero.
      coverage: edge.coverage || 'checked',
      populatedRows: edge.populatedRows,
      linkedRows: edge.linkedRows,
      unmatchedRows: edge.unmatchedRows,
      blankRows: edge.blankRows,
    }
  }
  return links
}

// Links in both directions, for the record view and for clickable foreign-key cells.
export const buildTableRelations = (model, tableId) => {
  const node = nodeForTable(model, tableId)
  if (!node) return null
  const nodes = nodeById(model)
  const outgoing = []
  const incoming = []
  for (const edge of model?.edges || []) {
    const sourceField = singleField(edge.sourceFields)
    const targetField = singleField(edge.targetFields)
    if (!sourceField || !targetField) continue
    const source = nodes.get(edge.source)
    const target = nodes.get(edge.target)
    if (!source || !target) continue
    if (edge.source === node.id && target.tableId !== null && target.tableId !== undefined) {
      outgoing.push({
        column: sourceField,
        targetTableId: target.tableId,
        targetTitle: target.title,
        targetColumn: targetField,
      })
    }
    if (edge.target === node.id && source.tableId !== null && source.tableId !== undefined) {
      incoming.push({
        column: targetField,
        sourceTableId: source.tableId,
        sourceTitle: source.title,
        sourceColumn: sourceField,
      })
    }
  }
  return { outgoing, incoming }
}

// The first outgoing link of a column, used to make its cells clickable.
export const outgoingByColumn = (relations) => {
  const map = new Map()
  for (const relation of relations?.outgoing || []) {
    if (!map.has(relation.column)) map.set(relation.column, relation)
  }
  return map
}

// A key that changes whenever the published tables change, so the package model can be refetched.
export const publishedTablesKey = (tables) => (Array.isArray(tables) ? tables : [])
  .filter((table) => table.is_dwc_dp)
  .map((table) => `${table.id}:${table.updated_at || ''}:${table.row_count ?? ''}`)
  .sort()
  .join('|')

// Appends asked-about text to whatever is already typed in the chat box.
export const appendToComposer = (current, text) => {
  const addition = String(text || '').trim()
  if (!addition) return current || ''
  const existing = String(current || '').replace(/\s+$/, '')
  return existing ? `${existing}\n\n${addition}` : addition
}
