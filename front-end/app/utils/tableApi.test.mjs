import assert from 'node:assert/strict'
import test from 'node:test'

import {
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

test('normalizes lightweight table summaries', () => {
  assert.deepEqual(
    normalizeTableList([{ id: 1, row_count: '12', columns: ['eventID'] }]),
    [{ id: 1, row_count: 12, columns: ['eventID'] }],
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
