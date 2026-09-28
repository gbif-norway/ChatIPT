import assert from 'node:assert/strict'
import test from 'node:test'

import {
  filterTablesByPackage,
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

test('filters tables by publication package', () => {
  const tables = [
    { id: 1, package: 'dwc-dp' },
    { id: 2, package: 'dwca' },
    { id: 3, package: '' },
  ]
  assert.deepEqual(filterTablesByPackage(tables, 'all').map((table) => table.id), [1, 2, 3])
  assert.deepEqual(filterTablesByPackage(tables, 'dwc-dp').map((table) => table.id), [1])
  assert.deepEqual(filterTablesByPackage(tables, 'dwca').map((table) => table.id), [2])
})

test('normalizes lightweight table summaries', () => {
  assert.deepEqual(
    normalizeTableList([{ id: 1, row_count: '12', columns: ['eventID'] }]),
    [{ id: 1, row_count: 12, columns: ['eventID'], package: '' }],
  )
  assert.throws(() => normalizeTableList({ results: [] }), /invalid/)
})

test('rejects malformed table pages', () => {
  assert.deepEqual(
    normalizeTablePage({ count: '2', columns: ['id'], results: [{ id: 'a' }] }),
    { count: 2, columns: ['id'], results: [{ id: 'a' }] },
  )
  assert.throws(() => normalizeTablePage({ results: [] }), /invalid/)
})
