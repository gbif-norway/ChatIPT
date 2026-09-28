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
  return `${baseUrl}/api/tables/${tableId}/rows/?${params}`
}

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
