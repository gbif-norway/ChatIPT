import assert from 'node:assert/strict'
import test from 'node:test'

import {
  appendToComposer,
  buildTableLinks,
  buildTableRelations,
  decodeExplorerState,
  decodeFilters,
  emptyExplorerState,
  encodeFilters,
  explorerAfterTablesChange,
  explorerSearchString,
  groupTables,
  nodeForTable,
  openRelatedState,
  openTableWithState,
  orderTables,
  outgoingByColumn,
  publishedTablesKey,
  resolveExplorerState,
  selectTable,
  setExplorerView,
  showRowsWithFilter,
} from './explorerState.mjs'

const tables = [
  { id: 327, title: 'dwca_resource_relationship', is_dwc_dp: false, columns: ['a'], row_count: 3, updated_at: 't1' },
  { id: 322, title: 'occurrence', is_dwc_dp: true, columns: ['occurrence_pk', 'event_fk', 'sex'], row_count: 10, updated_at: 't2' },
  { id: 318, title: 'material', is_dwc_dp: true, columns: ['materialEntity_pk'], row_count: 4, updated_at: 't3' },
  { id: 315, title: 'event', is_dwc_dp: true, columns: ['event_pk', 'eventID'], row_count: 2, updated_at: 't4' },
  { id: 400, title: 'wasps.xlsx', is_dwc_dp: false, columns: ['Species'], row_count: 9, updated_at: 't5' },
]

const model = {
  nodes: [
    { id: 'event', tableId: 315, title: 'Event' },
    { id: 'occurrence', tableId: 322, title: 'Occurrence' },
    { id: 'identification', tableId: 316, title: 'Identification' },
    { id: 'organism-interaction', tableId: 323, title: 'Organism Interaction' },
  ],
  edges: [
    {
      id: 'occurrence:foreign:0:event', source: 'occurrence', target: 'event', predicate: 'happened during', kind: 'foreign',
      sourceFields: ['event_fk'], targetFields: ['event_pk'], populatedRows: 10, linkedRows: 9, unmatchedRows: 1, blankRows: 0,
    },
    {
      id: 'identification:foreign:5:occurrence', source: 'identification', target: 'occurrence', predicate: 'based on', kind: 'foreign',
      sourceFields: ['occurrence_fk'], targetFields: ['occurrence_pk'], populatedRows: 5, linkedRows: 5, unmatchedRows: 0, blankRows: 2,
    },
    {
      id: 'organism-interaction:weak:2:occurrence', source: 'organism-interaction', target: 'occurrence', predicate: 'with', kind: 'weak',
      sourceFields: ['relatedOccurrenceID'], targetFields: ['occurrenceID'], populatedRows: 1, linkedRows: 1, unmatchedRows: 0, blankRows: 0,
    },
    {
      id: 'occurrence:foreign:9:event', source: 'occurrence', target: 'event', predicate: 'two fields', kind: 'foreign',
      sourceFields: ['a', 'b'], targetFields: ['c', 'd'], populatedRows: 1, linkedRows: 1, unmatchedRows: 0, blankRows: 0,
    },
  ],
}

test('switching table keeps the view and clears the column and filters', () => {
  const state = { tableId: 1, view: 'rows', column: 'sex', filters: [{ column: 'sex', op: 'blank' }] }
  assert.deepEqual(selectTable(state, 2), { tableId: 2, view: 'rows', column: null, filters: [] })
  assert.equal(selectTable(state, '1'), state)
  assert.deepEqual(emptyExplorerState('bogus'), { tableId: null, view: 'columns', column: null, filters: [] })
})

test('changes view only to a known view', () => {
  const state = emptyExplorerState()
  assert.equal(setExplorerView(state, 'columns'), state)
  assert.equal(setExplorerView(state, 'chart'), state)
  assert.equal(setExplorerView(state, 'rows').view, 'rows')
})

test('a filter from the Columns view opens Rows with it added once', () => {
  const state = { tableId: 1, view: 'columns', column: 'sex', filters: [{ column: 'caste', op: 'blank' }] }
  const next = showRowsWithFilter(state, { column: 'sex', op: 'is', value: ' female ' })
  assert.equal(next.view, 'rows')
  assert.equal(next.column, 'sex')
  assert.deepEqual(next.filters, [{ column: 'caste', op: 'blank' }, { column: 'sex', op: 'is', value: 'female' }])
  assert.equal(showRowsWithFilter(next, { column: 'sex', op: 'is', value: 'female' }).filters.length, 2)
})

test('following a link opens the other table in Rows with exactly those filters', () => {
  const state = { tableId: 322, view: 'columns', column: 'sex', filters: [{ column: 'sex', op: 'blank' }] }
  assert.deepEqual(
    openRelatedState(state, { tableId: 315, filters: [{ column: 'event_pk', op: 'is', value: 'e1' }] }),
    { tableId: 315, view: 'rows', column: null, filters: [{ column: 'event_pk', op: 'is', value: 'e1' }] },
  )
  assert.equal(openRelatedState(state, { tableId: 322, filters: [] }).column, 'sex')
})

test('encodes filters compactly and reads both forms back', () => {
  const filters = [{ column: 'sex', op: 'is', value: 'female' }, { column: 'date', op: 'blank' }, { column: '', op: 'is' }]
  const text = encodeFilters(filters)
  assert.equal(text, '[["sex","is","female"],["date","blank"]]')
  assert.deepEqual(decodeFilters(text), filters.slice(0, 2))
  assert.deepEqual(decodeFilters('[{"column":"sex","op":"contains","value":"fem"}]'), [{ column: 'sex', op: 'contains', value: 'fem' }])
  assert.equal(encodeFilters([]), '')
})

test('ignores unreadable filters in the address', () => {
  assert.deepEqual(decodeFilters('not json'), [])
  assert.deepEqual(decodeFilters('{"column":"sex"}'), [])
  assert.deepEqual(decodeFilters('[["sex","explode","x"],["sex","is"],null,7]'), [])
  assert.deepEqual(decodeFilters(''), [])
})

test('reads the explorer state from search params', () => {
  const params = new URLSearchParams('table=322&view=rows&column=sex&filters=%5B%5B%22sex%22%2C%22is%22%2C%22female%22%5D%5D&other=1')
  assert.deepEqual(decodeExplorerState(params), {
    tableId: '322', view: 'rows', column: 'sex', filters: [{ column: 'sex', op: 'is', value: 'female' }],
  })
  assert.deepEqual(decodeExplorerState(new URLSearchParams('table=abc&view=chart')), {
    tableId: null, view: null, column: null, filters: [],
  })
  assert.deepEqual(decodeExplorerState({ table: '7' }).tableId, '7')
  assert.deepEqual(decodeExplorerState(null).tableId, null)
})

test('drops a stale table, column or filter from the address', () => {
  const decoded = {
    tableId: '322', view: null, column: 'gone',
    filters: [{ column: 'sex', op: 'is', value: 'female' }, { column: 'gone', op: 'blank' }],
  }
  assert.deepEqual(resolveExplorerState(decoded, tables, 'rows'), {
    tableId: 322, view: 'rows', column: null, filters: [{ column: 'sex', op: 'is', value: 'female' }],
  })
  assert.equal(resolveExplorerState({ ...decoded, tableId: '999' }, tables), null)
  assert.equal(resolveExplorerState({ ...decoded, tableId: null }, tables), null)
  assert.equal(resolveExplorerState({ ...decoded, column: 'sex', view: 'columns' }, tables).column, 'sex')
})

test('writes the explorer state into the query string and keeps other parameters', () => {
  const search = explorerSearchString('?foo=1&table=3&filters=x', {
    tableId: 322, view: 'rows', column: 'sex', filters: [{ column: 'sex', op: 'is', value: 'a&b' }],
  })
  const params = new URLSearchParams(search)
  assert.equal(params.get('foo'), '1')
  assert.equal(params.get('table'), '322')
  assert.equal(params.get('view'), 'rows')
  assert.equal(params.get('column'), 'sex')
  assert.deepEqual(decodeFilters(params.get('filters')), [{ column: 'sex', op: 'is', value: 'a&b' }])
  assert.equal(explorerSearchString('foo=1&view=rows', { tableId: null }), 'foo=1')
  assert.equal(explorerSearchString('', { tableId: 5, view: 'columns', column: null, filters: [] }), 'table=5&view=columns')
})

test('round-trips state through the address', () => {
  const state = { tableId: 322, view: 'columns', column: 'sex', filters: [{ column: 'event_fk', op: 'not_blank' }] }
  const decoded = decodeExplorerState(new URLSearchParams(explorerSearchString('', state)))
  assert.deepEqual(resolveExplorerState(decoded, tables), state)
})

test('orders DwC-DP tables by the standard priority, then source files as given', () => {
  assert.deepEqual(orderTables(tables).map((table) => table.id), [315, 322, 318, 327, 400])
  const groups = groupTables(tables)
  assert.deepEqual(groups.published.map((table) => table.title), ['event', 'occurrence', 'material'])
  assert.deepEqual(groups.sources.map((table) => table.id), [327, 400])
  assert.deepEqual(orderTables(null), [])
  const titled = [
    { id: 1, title: 'Occurrence', resource: 'occurrence', is_dwc_dp: true },
    { id: 2, title: 'Event', resource: 'event', is_dwc_dp: true },
  ]
  assert.deepEqual(orderTables(titled).map((table) => table.id), [2, 1])
})

test('builds link health for single-field foreign keys of the table', () => {
  assert.deepEqual(buildTableLinks(model, 322), {
    event_fk: {
      targetTitle: 'Event', predicate: 'happened during', kind: 'foreign', coverage: 'checked',
      populatedRows: 10, linkedRows: 9, unmatchedRows: 1, blankRows: 0,
    },
  })
  assert.deepEqual(buildTableLinks(model, 315), {})
  assert.equal(buildTableLinks(model, 400), null)
  assert.equal(buildTableLinks(null, 322), null)
})

test('builds outgoing and incoming relations in both directions', () => {
  assert.deepEqual(buildTableRelations(model, 322), {
    outgoing: [{ column: 'event_fk', targetTableId: 315, targetTitle: 'Event', targetColumn: 'event_pk' }],
    incoming: [
      { column: 'occurrence_pk', sourceTableId: 316, sourceTitle: 'Identification', sourceColumn: 'occurrence_fk' },
      { column: 'occurrenceID', sourceTableId: 323, sourceTitle: 'Organism Interaction', sourceColumn: 'relatedOccurrenceID' },
    ],
  })
  assert.deepEqual(buildTableRelations(model, 315).incoming.map((entry) => entry.column), ['event_pk'])
  assert.equal(buildTableRelations(model, 999), null)
  assert.equal(nodeForTable(model, '322').id, 'occurrence')
  assert.equal(outgoingByColumn(buildTableRelations(model, 322)).get('event_fk').targetTableId, 315)
  assert.equal(outgoingByColumn(null).size, 0)
})

test('the published-tables key changes only when DwC-DP tables change', () => {
  const key = publishedTablesKey(tables)
  assert.equal(key, publishedTablesKey([...tables].reverse()))
  assert.notEqual(key, publishedTablesKey(tables.map((table) => (table.id === 322 ? { ...table, updated_at: 'new' } : table))))
  assert.equal(key, publishedTablesKey(tables.map((table) => (table.id === 400 ? { ...table, updated_at: 'new' } : table))))
  assert.equal(publishedTablesKey(undefined), '')
})

test('appends asked-about text to what is already typed', () => {
  assert.equal(appendToComposer('', 'Why is sex blank?'), 'Why is sex blank?')
  assert.equal(appendToComposer('Hello  \n', ' Why? '), 'Hello\n\nWhy?')
  assert.equal(appendToComposer('Hello', '   '), 'Hello')
  assert.equal(appendToComposer(undefined, 'Hi'), 'Hi')
})

test('a refresh keeps the open table, view and filters, and copes with a table disappearing', () => {
  const state = { tableId: 322, view: 'rows', column: 'sex', filters: [{ column: 'sex', op: 'is', value: 'female' }] }
  const refreshed = tables.map((table) => ({ ...table, updated_at: 'later' }))
  assert.equal(explorerAfterTablesChange(state, refreshed), state)
  const fewerColumns = refreshed.map((table) => (table.id === 322 ? { ...table, columns: ['occurrence_pk'] } : table))
  assert.deepEqual(explorerAfterTablesChange(state, fewerColumns), { tableId: 322, view: 'rows', column: null, filters: [] })
  const without = refreshed.filter((table) => table.id !== 322)
  assert.deepEqual(explorerAfterTablesChange(state, without), { tableId: 315, view: 'rows', column: null, filters: [] })
  assert.deepEqual(explorerAfterTablesChange(state, []), { tableId: null, view: 'rows', column: null, filters: [] })
  assert.equal(explorerAfterTablesChange(emptyExplorerState(), tables).tableId, 315)
})

test('opens a package-explorer table on the main page with only what that table has', () => {
  const current = { tableId: 315, view: 'columns', column: 'eventID', filters: [] }
  assert.deepEqual(
    openTableWithState(current, tables, 322, {
      view: 'rows',
      column: 'gone',
      filters: [{ column: 'sex', op: 'is', value: 'female' }, { column: 'gone', op: 'blank' }],
    }),
    { tableId: 322, view: 'rows', column: null, filters: [{ column: 'sex', op: 'is', value: 'female' }] },
  )
  assert.deepEqual(openTableWithState(current, tables, '322', { view: 'rows', column: 'sex', filters: [] }).column, 'sex')
  // Unknown table: nothing changes.
  assert.equal(openTableWithState(current, tables, 999, { view: 'rows' }), current)
  // No state: a plain table switch.
  assert.deepEqual(openTableWithState(current, tables, 318), { tableId: 318, view: 'columns', column: null, filters: [] })
})
