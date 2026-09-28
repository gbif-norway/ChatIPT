'use client'

import { useEffect, useState } from 'react'
import DataTable from 'react-data-table-component'
import config from '../config.js'
import { pluralize } from '../utils/datasetPresentation'
import {
  DEFAULT_TABLE_PAGE_SIZE,
  TABLE_PAGE_SIZE_OPTIONS,
  normalizeTablePage,
  tableRowsUrl,
} from '../utils/tableApi.mjs'

const SEARCH_DELAY_MS = 300

export const formatCell = (value) => {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

/**
 * One paginated, searchable table page loaded from the rows API.
 * Remount with a new `key` to start from a different `initialFilter`
 * (for example `{ search: 'ev-1', column: 'eventID', exact: true }`).
 */
export default function TableRowsView({
  tableId,
  columns,
  totalRows,
  initialFilter = null,
  renderHeader,
  renderCell,
}) {
  const [searchText, setSearchText] = useState(initialFilter?.search || '')
  const [search, setSearch] = useState(initialFilter?.search || '')
  const [scope, setScope] = useState(
    initialFilter?.column ? { column: initialFilter.column, exact: Boolean(initialFilter.exact) } : null
  )
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(DEFAULT_TABLE_PAGE_SIZE)
  const [resetPage, setResetPage] = useState(false)
  const [rows, setRows] = useState([])
  const [count, setCount] = useState(totalRows || 0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [reloadToken, setReloadToken] = useState(0)

  useEffect(() => {
    const timeout = window.setTimeout(() => setSearch(searchText.trim()), SEARCH_DELAY_MS)
    return () => window.clearTimeout(timeout)
  }, [searchText])

  useEffect(() => {
    setPage(1)
    setResetPage((value) => !value)
  }, [search, scope])

  useEffect(() => {
    if (!tableId) return undefined
    const controller = new AbortController()
    const load = async () => {
      setLoading(true)
      setError('')
      try {
        const response = await fetch(
          tableRowsUrl(config.baseUrl, tableId, page, pageSize, {
            search,
            column: scope?.column,
            exact: scope?.exact,
          }),
          { credentials: 'include', signal: controller.signal },
        )
        const payload = await response.json()
        if (!response.ok) throw new Error(payload?.detail || `HTTP error! status: ${response.status}`)
        const tablePage = normalizeTablePage(payload)
        setRows(tablePage.results)
        setCount(tablePage.count)
      } catch (loadError) {
        if (loadError.name === 'AbortError') return
        console.error('Error loading table rows:', loadError)
        setRows([])
        setError('This table page could not be loaded. Please retry.')
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }
    load()
    return () => controller.abort()
  }, [tableId, page, pageSize, search, scope, reloadToken])

  const clearSearch = () => {
    setSearchText('')
    setScope(null)
  }

  const isSearching = Boolean(search)

  return (
    <div className="table-rows-view">
      <div className="table-rows-toolbar">
        <div className="input-group input-group-sm">
          <span className="input-group-text"><i className="bi bi-search" aria-hidden="true"></i></span>
          <input
            type="search"
            className="form-control"
            placeholder={scope ? `Search ${scope.column}…` : 'Search rows…'}
            aria-label="Search rows"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
          {scope && (
            <button
              type="button"
              className="btn btn-outline-secondary"
              onClick={() => setScope(null)}
              title="Search every column"
            >
              {scope.exact ? `${scope.column} =` : `in ${scope.column}`}
              <i className="bi bi-x ms-1" aria-hidden="true"></i>
            </button>
          )}
          {(searchText || scope) && (
            <button type="button" className="btn btn-outline-secondary" onClick={clearSearch}>
              Clear
            </button>
          )}
        </div>
        {isSearching && !loading && (
          <small className="text-muted text-nowrap">
            {pluralize(count, 'match', 'matches')}
            {totalRows ? ` of ${totalRows.toLocaleString()}` : ''}
          </small>
        )}
      </div>
      {error && (
        <div className="alert alert-danger mx-2" role="alert">
          {error}{' '}
          <button
            type="button"
            className="btn btn-sm btn-outline-danger ms-2"
            onClick={() => setReloadToken((value) => value + 1)}
          >
            Retry
          </button>
        </div>
      )}
      <DataTable
        columns={columns.map((column) => ({
          name: renderHeader ? renderHeader(column) : column,
          selector: (row) => formatCell(row[column]),
          cell: renderCell
            ? (row) => renderCell(column, row[column], row) ?? formatCell(row[column])
            : undefined,
        }))}
        data={rows}
        progressPending={loading}
        noDataComponent={(
          <div className="p-3 text-muted small">
            {isSearching ? 'No rows match this search.' : 'This table has no rows.'}
          </div>
        )}
        conditionalRowStyles={scope?.exact && isSearching
          ? [{ when: () => true, classNames: ['table-row-linked'] }]
          : []}
        pagination
        paginationServer
        paginationTotalRows={count}
        paginationResetDefaultPage={resetPage}
        paginationPerPage={pageSize}
        paginationRowsPerPageOptions={TABLE_PAGE_SIZE_OPTIONS}
        onChangePage={setPage}
        onChangeRowsPerPage={(nextPageSize) => {
          setPageSize(nextPageSize)
          setPage(1)
        }}
        dense
      />
    </div>
  )
}
