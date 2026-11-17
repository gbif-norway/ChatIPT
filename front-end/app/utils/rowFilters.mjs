// Pure helpers for the table Rows view: column filters, empty columns, sorting and the record view.
// Filter shape everywhere: { column, op, value? }.

export const FILTER_OPS = [
  { op: 'is', label: 'is', needsValue: true },
  // The backend keeps blank rows for "is not" (blank is not "female"), so the label says so.
  { op: 'is_not', label: 'is not (includes blank)', needsValue: true },
  { op: 'contains', label: 'contains', needsValue: true },
  { op: 'blank', label: 'is blank', needsValue: false },
  { op: 'not_blank', label: 'is not blank', needsValue: false },
  { op: 'duplicated', label: 'has duplicates', needsValue: false },
  { op: 'variant_of', label: 'has values like', needsValue: true, hidden: true },
  // Following a link between tables: the identifier exactly ("1" never finds "001").
  { op: 'equals', label: 'is exactly', needsValue: true, hidden: true },
]

const OPS_BY_NAME = new Map(FILTER_OPS.map((entry) => [entry.op, entry]))

// The conditions a person can pick in the "+ Filter" form (variant_of only arrives from the Columns view).
export const PICKABLE_FILTER_OPS = FILTER_OPS.filter((entry) => !entry.hidden)

export const filterNeedsValue = (op) => Boolean(OPS_BY_NAME.get(op)?.needsValue)

export const isBlankValue = (value) => {
  if (value === null || value === undefined) return true
  if (typeof value === 'number') return Number.isNaN(value)
  if (typeof value === 'string') return value.trim() === ''
  return false
}

export const cellText = (value) => {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number' && Number.isNaN(value)) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

// Returns a clean filter, or null when it cannot be sent (unknown op, no column, missing value).
export const normalizeFilter = (filter) => {
  if (!filter || typeof filter !== 'object') return null
  // Column labels are kept exactly: a spreadsheet header " Species " is a different column from "Species".
  const column = String(filter.column ?? '')
  const op = String(filter.op ?? '')
  const entry = OPS_BY_NAME.get(op)
  if (!column.trim() || !entry) return null
  if (!entry.needsValue) return { column, op }
  const raw = filter.value === null || filter.value === undefined ? '' : String(filter.value)
  // Cells are compared stripped, so a stripped value is what matches; variant_of keeps the exact text it came with.
  const value = op === 'variant_of' ? raw : raw.trim()
  if (value.trim() === '') return null
  return { column, op, value }
}

export const normalizeFilters = (filters) => {
  if (!Array.isArray(filters)) return []
  const seen = new Set()
  const result = []
  for (const filter of filters) {
    const clean = normalizeFilter(filter)
    if (!clean) continue
    const key = filterKey(clean)
    if (seen.has(key)) continue
    seen.add(key)
    result.push(clean)
  }
  return result
}

export const filterKey = (filter) => JSON.stringify([filter.column, filter.op, filter.value ?? null])

export const filtersKey = (filters) => JSON.stringify(normalizeFilters(filters).map(filterKey))

export const sameFilter = (a, b) => {
  const left = normalizeFilter(a)
  const right = normalizeFilter(b)
  return Boolean(left && right && filterKey(left) === filterKey(right))
}

// Adds a filter unless an identical one is already active. Returns the same array when nothing changes.
export const addFilter = (filters, filter) => {
  const current = Array.isArray(filters) ? filters : []
  const clean = normalizeFilter(filter)
  if (!clean || current.some((existing) => sameFilter(existing, clean))) return current
  return [...current, clean]
}

export const removeFilter = (filters, filterOrIndex) => {
  const current = Array.isArray(filters) ? filters : []
  if (typeof filterOrIndex === 'number') return current.filter((_, index) => index !== filterOrIndex)
  return current.filter((existing) => !sameFilter(existing, filterOrIndex))
}

const LABEL_VALUE_LIMIT = 40

const shortValue = (value) => {
  const text = String(value ?? '')
  return text.length > LABEL_VALUE_LIMIT ? `${text.slice(0, LABEL_VALUE_LIMIT - 1)}…` : text
}

const quoted = (value) => `"${shortValue(value)}"`

// Plain values read naturally ("sex is female"); quote them when spacing would otherwise be invisible.
const plainOrQuoted = (value) => {
  const text = String(value ?? '')
  return text === '' || text !== text.trim() ? quoted(text) : shortValue(text)
}

export const filterLabel = (filter) => {
  const column = filter?.column || 'column'
  switch (filter?.op) {
    case 'is': return `${column} is ${plainOrQuoted(filter.value)}`
    case 'equals': return `${column} is ${plainOrQuoted(filter.value)}`
    case 'is_not': return `${column} is not ${plainOrQuoted(filter.value)} (includes blank)`
    case 'contains': return `${column} contains ${quoted(filter.value)}`
    case 'blank': return `${column} is blank`
    case 'not_blank': return `${column} is not blank`
    case 'duplicated': return `${column} has duplicates`
    case 'variant_of': return `${column} has values like ${quoted(filter.value)}`
    default: return `${column} ${filter?.op || ''}`.trim()
  }
}

// Stats are only trusted when they describe this table (the parent may still hold the previous table's stats).
export const usableStats = (stats, tableId) => {
  if (!stats || !Array.isArray(stats.columns)) return null
  if (stats.table !== undefined && stats.table !== null && tableId !== undefined && tableId !== null
    && String(stats.table) !== String(tableId)) return null
  return stats
}

// Whether the stats describe this version of the table. While newer stats load (or if they fail),
// the old ones must not decide which columns are hidden as empty: the agent may just have filled one.
// Without a readable version on both sides nothing is hidden automatically.
export const statsMatchVersion = (stats, version) => {
  if (!stats) return false
  // Only ISO timestamps are versions here (the package explorer passes a counter instead).
  const timestamp = (text) => (/^\d{4}-\d{2}-\d{2}T/.test(String(text ?? '')) ? Date.parse(String(text)) : NaN)
  const wanted = timestamp(version)
  const have = timestamp(stats.version)
  if (!Number.isFinite(wanted) || !Number.isFinite(have)) return false
  return wanted === have
}

export const statsByColumn = (stats) => new Map(
  (stats?.columns || []).map((column) => [column.name, column])
)

export const emptyColumnNames = (columns, stats) => {
  const byName = statsByColumn(stats)
  return (columns || []).filter((column) => {
    const columnStats = byName.get(column)
    if (!columnStats) return false
    return columnStats.kind === 'empty' || Number(columnStats.populated) === 0
  })
}

// Empty columns are hidden unless asked for, or unless every column is empty (then there is nothing to hide them behind).
export const visibleColumns = (columns, stats, showEmpty) => {
  const all = columns || []
  if (showEmpty) return all
  const empty = new Set(emptyColumnNames(all, stats))
  if (empty.size === 0 || empty.size === all.length) return all
  return all.filter((column) => !empty.has(column))
}

export const numericColumnNames = (stats) => new Set(
  (stats?.columns || []).filter((column) => column.kind === 'number').map((column) => column.name)
)

// Header click cycle: not sorted -> ascending -> descending -> not sorted.
export const nextSort = (current, column) => {
  if (!current || current.column !== column) return { column, order: 'asc' }
  if (current.order === 'asc') return { column, order: 'desc' }
  return null
}

export const fieldsByName = (fields) => new Map((fields || []).map((field) => [field.name, field]))

export const fieldLabel = (name, field) => (
  String(name).startsWith('http') && field?.title ? field.title : name
)

const keyRank = (field) => {
  if (field?.primary) return 0
  if (field?.weakPrimary) return 1
  return 2
}

// Key fields first (primary, then public identifiers), then table order. Blank fields are listed separately.
export const orderRecordFields = (columns, row, fields) => {
  const byName = fieldsByName(fields)
  const ordered = (columns || [])
    .map((name, position) => ({ name, position, field: byName.get(name) || null, value: row?.[name] }))
    .sort((a, b) => keyRank(a.field) - keyRank(b.field) || a.position - b.position)
    .map(({ name, field, value }) => ({
      name,
      label: fieldLabel(name, field),
      field,
      value,
      isKey: keyRank(field) < 2,
      blank: isBlankValue(value),
    }))
  return {
    filled: ordered.filter((entry) => !entry.blank),
    blank: ordered.filter((entry) => entry.blank),
  }
}

// The value that names this record: its primary key, else its public identifier, else the first filled column.
export const recordKey = (columns, row, fields) => {
  const { filled } = orderRecordFields(columns, row, fields)
  const entry = filled.find((item) => item.isKey) || filled[0]
  return entry ? { column: entry.name, value: cellText(entry.value) } : null
}

// After the rows of the page are reloaded (the table was saved again), where the open record is now.
// Rows with a key field are found by that key; otherwise by identical content. Returns null when the
// record cannot be found on this page: without a key, a changed or removed row cannot be told apart
// from its neighbours, and showing the next row in its place would describe a different record.
export const findRecordIndex = (columns, oldRow, oldIndex, nextRows, fields) => {
  const list = Array.isArray(nextRows) ? nextRows : []
  if (!oldRow || !list.length) return null
  const { filled } = orderRecordFields(columns, oldRow, fields)
  const key = filled.find((entry) => entry.isKey)
  if (key) {
    const wanted = cellText(key.value)
    const found = list.findIndex((row) => cellText(row?.[key.name]) === wanted)
    return found === -1 ? null : found
  }
  const text = JSON.stringify(oldRow)
  const same = list.findIndex((row) => JSON.stringify(row) === text)
  return same === -1 ? null : same
}

export const recordSummary = (columns, row, limit = 4) => (columns || [])
  .filter((name) => !isBlankValue(row?.[name]))
  .slice(0, limit)
  .map((name) => ({ name, value: cellText(row[name]) }))

export const askAboutRecordText = (tableTitle, key) => {
  const table = tableTitle ? `the ${tableTitle} table` : 'this table'
  if (!key) return `Tell me about this record in ${table}.`
  return `Tell me about the record in ${table} where ${key.column} is "${key.value}".`
}

export const incomingLinkLabel = (count, sourceTitle) => {
  const n = Number(count) || 0
  const noun = `${sourceTitle || 'linked'} ${n === 1 ? 'row' : 'rows'}`
  if (n === 0) return `No ${noun} point to this record`
  return `${n.toLocaleString()} ${noun} ${n === 1 ? 'points' : 'point'} to this record`
}

export const matchingRowsLabel = (count, total) => {
  const n = Number(count) || 0
  const head = `${n.toLocaleString()} matching ${n === 1 ? 'row' : 'rows'}`
  return Number.isFinite(Number(total)) && total !== null && total !== undefined
    ? `${head} of ${Number(total).toLocaleString()}`
    : head
}
