import assert from 'node:assert/strict'
import test from 'node:test'

import {
  apiErrorMessage,
  columnStatsUrl,
  columnValuesUrl,
  tableLineageUrl,
  normalizeTableList,
  normalizeTablePage,
  tableRowsUrl,
} from './tableApi.mjs'

test('builds bounded row-page URLs from one-based pages', () => {
  assert.equal(
    tableRowsUrl('https://example.org', 42, 3, 50),
    'https://example.org/api/tables/42/rows/?offset=100&limit=50',
  )
})

test('adds row search parameters only when there is search text', () => {
  assert.equal(
    tableRowsUrl('', 7, 1, 25, { search: '  ', column: 'eventID', exact: true }),
    '/api/tables/7/rows/?offset=0&limit=25',
  )
  assert.equal(
    tableRowsUrl('', 7, 2, 25, { search: 'ev 1', column: 'eventID', exact: true }),
    '/api/tables/7/rows/?offset=25&limit=25&search=ev+1&column=eventID&exact=true',
  )
})

test('sends column filters as JSON and sorting with a safe order', () => {
  const url = new URL(tableRowsUrl('https://x.org', 7, 1, 25, {
    search: 'wasp',
    filters: [{ column: 'sex', op: 'is', value: 'female' }],
    sort: 'individualCount',
    order: 'desc',
  }))
  assert.equal(url.searchParams.get('search'), 'wasp')
  assert.deepEqual(JSON.parse(url.searchParams.get('filters')), [{ column: 'sex', op: 'is', value: 'female' }])
  assert.equal(url.searchParams.get('sort'), 'individualCount')
  assert.equal(url.searchParams.get('order'), 'desc')
  assert.equal(
    tableRowsUrl('', 7, 1, 25, { filters: [], sort: 'a', order: 'sideways' }),
    '/api/tables/7/rows/?offset=0&limit=25&sort=a&order=asc',
  )
})

test('builds column stats, values and lineage URLs', () => {
  assert.equal(columnStatsUrl('', 3), '/api/tables/3/column-stats/')
  assert.equal(tableLineageUrl('', 3), '/api/tables/3/lineage/')
  assert.equal(
    columnValuesUrl('', 3, { column: 'sex', search: ' fem ', offset: 0, limit: 50, sort: 'value' }),
    '/api/tables/3/column-values/?column=sex&search=fem&offset=0&limit=50&sort=value',
  )
  assert.equal(columnValuesUrl('', 3, { column: 'a b' }), '/api/tables/3/column-values/?column=a+b')
})

test('normalizes lightweight table summaries', () => {
  assert.deepEqual(
    normalizeTableList([{ id: 1, row_count: '12', columns: ['eventID'] }]),
    [{ id: 1, row_count: 12, columns: ['eventID'], is_dwc_dp: false }],
  )
  assert.throws(() => normalizeTableList({ results: [] }), /invalid/)
})

test('lists DwC-DP resource tables first', () => {
  const tables = normalizeTableList([
    { id: 1, is_dwc_dp: false },
    { id: 2, is_dwc_dp: true },
    { id: 3, is_dwc_dp: false },
    { id: 4, is_dwc_dp: true },
  ])
  assert.deepEqual(tables.map((table) => table.id), [2, 4, 1, 3])
})

test('rejects malformed table pages', () => {
  assert.deepEqual(
    normalizeTablePage({ count: '2', columns: ['id'], results: [{ id: 'a' }] }),
    { count: 2, columns: ['id'], results: [{ id: 'a' }] },
  )
  assert.throws(() => normalizeTablePage({ results: [] }), /invalid/)
})

test('reads DRF error bodies into one message', () => {
  assert.equal(apiErrorMessage({ detail: 'Unknown sort column: sex' }, 400), 'Unknown sort column: sex')
  assert.equal(
    apiErrorMessage({ search: ['Ensure this field has no more than 200 characters.'] }, 400),
    'Search: Ensure this field has no more than 200 characters.',
  )
  assert.equal(apiErrorMessage({ non_field_errors: ['Bad.', 'Worse.'] }, 400), 'Bad. Worse.')
  assert.equal(apiErrorMessage(['Nope.'], 400), 'Nope.')
  assert.equal(apiErrorMessage('Plain text', 500), 'Plain text')
  assert.equal(apiErrorMessage(null, 502), 'HTTP error! status: 502')
  assert.equal(apiErrorMessage({ search: [] }, 400), 'HTTP error! status: 400')
})
