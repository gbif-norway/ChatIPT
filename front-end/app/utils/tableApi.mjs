export const DEFAULT_TABLE_PAGE_SIZE = 50
export const TABLE_PAGE_SIZE_OPTIONS = [25, 50, 100, 200]

export const tableRowsUrl = (baseUrl, tableId, page, pageSize, filter = {}) => {
  const safePage = Math.max(Number(page) || 1, 1)
  const safePageSize = Math.max(Number(pageSize) || DEFAULT_TABLE_PAGE_SIZE, 1)
  const offset = (safePage - 1) * safePageSize
  const params = new URLSearchParams({ offset: String(offset), limit: String(safePageSize) })
  const search = String(filter.search || '').trim()
  if (search) {
    params.set('search', search)
    if (filter.column) params.set('column', filter.column)
    if (filter.exact) params.set('exact', 'true')
  }
  const filters = Array.isArray(filter.filters) ? filter.filters.filter(Boolean) : []
  if (filters.length) params.set('filters', JSON.stringify(filters))
  if (filter.sort) {
    params.set('sort', filter.sort)
    params.set('order', filter.order === 'desc' ? 'desc' : 'asc')
  }
  return `${baseUrl}/api/tables/${tableId}/rows/?${params}`
}

const capitalize = (text) => (text ? text.charAt(0).toUpperCase() + text.slice(1) : text)

const messagesOf = (value) => (Array.isArray(value) ? value : [value])
  .flatMap((item) => (item && typeof item === 'object' && !Array.isArray(item) ? Object.values(item) : [item]))
  .filter((item) => typeof item === 'string' && item.trim())

// A readable message from an API error body: DRF's {detail}, field errors ({search: ["..."]}),
// non_field_errors, a list or plain text. Falls back to the HTTP status.
export const apiErrorMessage = (payload, status) => {
  if (typeof payload === 'string' && payload.trim()) return payload.trim()
  if (Array.isArray(payload)) {
    const messages = messagesOf(payload)
    if (messages.length) return messages.join(' ')
  } else if (payload && typeof payload === 'object') {
    if (typeof payload.detail === 'string' && payload.detail.trim()) return payload.detail.trim()
    const parts = Object.entries(payload).flatMap(([field, value]) => {
      const messages = messagesOf(value)
      if (!messages.length) return []
      if (field === 'non_field_errors' || field === 'detail' || field === 'error') return [messages.join(' ')]
      return [`${capitalize(field)}: ${messages.join(' ')}`]
    })
    if (parts.length) return parts.join(' ')
  }
  return `HTTP error! status: ${status}`
}

export const columnStatsUrl = (baseUrl, tableId) => `${baseUrl}/api/tables/${tableId}/column-stats/`

export const columnValuesUrl = (baseUrl, tableId, { column, search, offset, limit, sort } = {}) => {
  const params = new URLSearchParams({ column: String(column ?? '') })
  const searchText = String(search || '').trim()
  if (searchText) params.set('search', searchText)
  if (offset !== undefined && offset !== null) params.set('offset', String(Math.max(Number(offset) || 0, 0)))
  if (limit !== undefined && limit !== null) params.set('limit', String(Math.max(Number(limit) || 1, 1)))
  if (sort) params.set('sort', sort)
  return `${baseUrl}/api/tables/${tableId}/column-values/?${params}`
}

export const tableLineageUrl = (baseUrl, tableId) => `${baseUrl}/api/tables/${tableId}/lineage/`

// DwC-DP resource tables come first; source and working tables keep their order after them.
export const normalizeTableList = (payload) => {
  if (!Array.isArray(payload)) throw new Error('The table list response was invalid.')
  const tables = payload.map((table) => ({
    ...table,
    row_count: Number(table.row_count) || 0,
    columns: Array.isArray(table.columns) ? table.columns : [],
    is_dwc_dp: Boolean(table.is_dwc_dp),
  }))
  return [...tables.filter((table) => table.is_dwc_dp), ...tables.filter((table) => !table.is_dwc_dp)]
}

export const normalizeTablePage = (payload) => {
  if (!payload || !Array.isArray(payload.results) || !Array.isArray(payload.columns)) {
    throw new Error('The table page response was invalid.')
  }
  return {
    ...payload,
    count: Number(payload.count) || 0,
    results: payload.results,
  }
}
