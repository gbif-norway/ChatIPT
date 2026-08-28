export const DEFAULT_TABLE_PAGE_SIZE = 50
export const TABLE_PAGE_SIZE_OPTIONS = [25, 50, 100, 200]

export const tableRowsUrl = (baseUrl, tableId, page, pageSize) => {
  const safePage = Math.max(Number(page) || 1, 1)
  const safePageSize = Math.max(Number(pageSize) || DEFAULT_TABLE_PAGE_SIZE, 1)
  const offset = (safePage - 1) * safePageSize
  return `${baseUrl}/api/tables/${tableId}/rows/?offset=${offset}&limit=${safePageSize}`
}

export const normalizeTableList = (payload) => {
  if (!Array.isArray(payload)) throw new Error('The table list response was invalid.')
  return payload.map((table) => ({
    ...table,
    row_count: Number(table.row_count) || 0,
    columns: Array.isArray(table.columns) ? table.columns : [],
  }))
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
