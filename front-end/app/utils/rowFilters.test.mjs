import assert from 'node:assert/strict'
import test from 'node:test'

import {
  PICKABLE_FILTER_OPS,
  addFilter,
  askAboutRecordText,
  emptyColumnNames,
  filterLabel,
  filterNeedsValue,
  findRecordIndex,
  filtersKey,
  incomingLinkLabel,
  isBlankValue,
  matchingRowsLabel,
  nextSort,
  normalizeFilter,
  normalizeFilters,
  numericColumnNames,
  orderRecordFields,
  recordKey,
  recordSummary,
  removeFilter,
  statsMatchVersion,
  usableStats,
  visibleColumns,
} from './rowFilters.mjs'

test('labels filters in plain language', () => {
  assert.equal(filterLabel({ column: 'sex', op: 'is', value: 'female' }), 'sex is female')
  assert.equal(filterLabel({ column: 'sex', op: 'is_not', value: 'female' }), 'sex is not female (includes blank)')
  assert.equal(PICKABLE_FILTER_OPS.find((entry) => entry.op === 'is_not').label, 'is not (includes blank)')
  assert.equal(filterLabel({ column: 'caste', op: 'blank' }), 'caste is blank')
  assert.equal(filterLabel({ column: 'caste', op: 'not_blank' }), 'caste is not blank')
  assert.equal(filterLabel({ column: 'occurrenceID', op: 'duplicated' }), 'occurrenceID has duplicates')
  assert.equal(filterLabel({ column: 'sex', op: 'variant_of', value: 'Male' }), 'sex has values like "Male"')
  assert.equal(filterLabel({ column: 'locality', op: 'contains', value: 'kloof' }), 'locality contains "kloof"')
  assert.equal(filterLabel({ column: 'sex', op: 'variant_of', value: 'Male ' }), 'sex has values like "Male "')
  const long = filterLabel({ column: 'remarks', op: 'is', value: 'x'.repeat(80) })
  assert.ok(long.length < 60 && long.endsWith('…'))
})

test('normalizes filters and drops ones that cannot be sent', () => {
  assert.deepEqual(normalizeFilter({ column: 'sex', op: 'is', value: ' female ' }), { column: 'sex', op: 'is', value: 'female' })
  // Headers keep their spacing: " Species " and "Species" can both exist.
  assert.deepEqual(normalizeFilter({ column: ' Species ', op: 'is', value: 'a' }), { column: ' Species ', op: 'is', value: 'a' })
  assert.deepEqual(
    normalizeFilters([{ column: ' Species ', op: 'blank' }, { column: 'Species', op: 'blank' }]),
    [{ column: ' Species ', op: 'blank' }, { column: 'Species', op: 'blank' }],
  )
  assert.equal(normalizeFilter({ column: '   ', op: 'blank' }), null)
  assert.deepEqual(normalizeFilter({ column: 'event_fk', op: 'equals', value: ' 001 ' }), { column: 'event_fk', op: 'equals', value: '001' })
  assert.ok(!PICKABLE_FILTER_OPS.some((entry) => entry.op === 'equals'))
  assert.deepEqual(normalizeFilter({ column: 'sex', op: 'blank', value: 'ignored' }), { column: 'sex', op: 'blank' })
  assert.deepEqual(normalizeFilter({ column: 'sex', op: 'variant_of', value: 'Male ' }), { column: 'sex', op: 'variant_of', value: 'Male ' })
  assert.equal(normalizeFilter({ column: 'sex', op: 'is', value: '   ' }), null)
  assert.equal(normalizeFilter({ column: 'sex', op: 'between', value: '1' }), null)
  assert.equal(normalizeFilter({ op: 'blank' }), null)
  assert.equal(normalizeFilter({ column: 'n', op: 'is', value: 0 }).value, '0')
  assert.deepEqual(
    normalizeFilters([{ column: 'a', op: 'blank' }, { column: 'a', op: 'blank' }, null, { column: 'b', op: 'nope' }]),
    [{ column: 'a', op: 'blank' }],
  )
  assert.equal(filterNeedsValue('contains'), true)
  assert.equal(filterNeedsValue('duplicated'), false)
  assert.ok(!PICKABLE_FILTER_OPS.some((entry) => entry.op === 'variant_of'))
})

test('adds without duplicates and removes by index or value', () => {
  const one = addFilter([], { column: 'sex', op: 'is', value: 'female' })
  assert.deepEqual(one, [{ column: 'sex', op: 'is', value: 'female' }])
  assert.equal(addFilter(one, { column: 'sex', op: 'is', value: ' female' }), one)
  assert.equal(addFilter(one, { column: 'sex', op: 'is', value: '' }), one)
  const two = addFilter(one, { column: 'caste', op: 'blank' })
  assert.equal(two.length, 2)
  assert.deepEqual(removeFilter(two, 0), [{ column: 'caste', op: 'blank' }])
  assert.deepEqual(removeFilter(two, { column: 'caste', op: 'blank' }), one)
  assert.equal(filtersKey(two), filtersKey([...two]))
  assert.notEqual(filtersKey(one), filtersKey(two))
})

const stats = {
  table: 12,
  columns: [
    { name: 'id', kind: 'text', populated: 3 },
    { name: 'count', kind: 'number', populated: 2 },
    { name: 'caste', kind: 'empty', populated: 0 },
    { name: 'notes', kind: 'text', populated: 0 },
  ],
}

test('finds empty columns and hides them only when something else is left', () => {
  const columns = ['id', 'count', 'caste', 'notes', 'unknown']
  assert.deepEqual(emptyColumnNames(columns, stats), ['caste', 'notes'])
  assert.deepEqual(visibleColumns(columns, stats, false), ['id', 'count', 'unknown'])
  assert.deepEqual(visibleColumns(columns, stats, true), columns)
  assert.deepEqual(visibleColumns(columns, null, false), columns)
  assert.deepEqual(visibleColumns(['caste', 'notes'], stats, false), ['caste', 'notes'])
  assert.deepEqual([...numericColumnNames(stats)], ['count'])
})

test('ignores stats that belong to another table', () => {
  assert.equal(usableStats(stats, 12), stats)
  assert.equal(usableStats(stats, '12'), stats)
  assert.equal(usableStats(stats, 13), null)
  assert.equal(usableStats({ columns: [] }, 13).columns.length, 0)
  assert.equal(usableStats(null, 12), null)
})

test('cycles header sorting through ascending, descending and off', () => {
  assert.deepEqual(nextSort(null, 'a'), { column: 'a', order: 'asc' })
  assert.deepEqual(nextSort({ column: 'a', order: 'asc' }, 'a'), { column: 'a', order: 'desc' })
  assert.equal(nextSort({ column: 'a', order: 'desc' }, 'a'), null)
  assert.deepEqual(nextSort({ column: 'a', order: 'desc' }, 'b'), { column: 'b', order: 'asc' })
})

test('treats null, NaN and whitespace as blank but keeps zero and false', () => {
  assert.ok(isBlankValue(null) && isBlankValue(undefined) && isBlankValue(NaN) && isBlankValue('  '))
  assert.ok(!isBlankValue(0) && !isBlankValue(false) && !isBlankValue('0'))
})

test('orders record fields key first then table order, with blanks apart', () => {
  const columns = ['scientificName', 'occurrenceID', 'eventID', 'sex', 'http://rs.tdwg.org/x']
  const row = { scientificName: 'Apis', occurrenceID: 'o-1', eventID: 'e-1', sex: ' ', 'http://rs.tdwg.org/x': 'y' }
  const fields = [
    { name: 'eventID', primary: false, weakPrimary: true },
    { name: 'occurrenceID', primary: true },
    { name: 'http://rs.tdwg.org/x', title: 'Extra' },
  ]
  const { filled, blank } = orderRecordFields(columns, row, fields)
  assert.deepEqual(filled.map((entry) => entry.name), ['occurrenceID', 'eventID', 'scientificName', 'http://rs.tdwg.org/x'])
  assert.equal(filled[3].label, 'Extra')
  assert.deepEqual(blank.map((entry) => entry.name), ['sex'])
  assert.deepEqual(recordKey(columns, row, fields), { column: 'occurrenceID', value: 'o-1' })
  assert.deepEqual(recordKey(['a', 'b'], { a: '', b: 5 }, []), { column: 'b', value: '5' })
  assert.equal(recordKey(['a'], { a: null }, []), null)
})

test('summarizes linked records and writes plain sentences', () => {
  assert.deepEqual(
    recordSummary(['a', 'b', 'c', 'd'], { a: 'x', b: '', c: 3, d: 'z' }, 2),
    [{ name: 'a', value: 'x' }, { name: 'c', value: '3' }],
  )
  assert.equal(incomingLinkLabel(3, 'Identification'), '3 Identification rows point to this record')
  assert.equal(incomingLinkLabel(1, 'Identification'), '1 Identification row points to this record')
  assert.equal(incomingLinkLabel(0, 'Identification'), 'No Identification rows point to this record')
  assert.equal(matchingRowsLabel(1, 731), '1 matching row of 731')
  assert.equal(matchingRowsLabel(1200, 5000), '1,200 matching rows of 5,000')
  assert.equal(matchingRowsLabel(2, undefined), '2 matching rows')
  assert.equal(
    askAboutRecordText('occurrence', { column: 'occurrenceID', value: 'o-1' }),
    'Tell me about the record in the occurrence table where occurrenceID is "o-1".',
  )
  assert.equal(askAboutRecordText('', null), 'Tell me about this record in this table.')
})

test('finds the open record again after the page reloads', () => {
  const columns = ['occurrence_pk', 'sex']
  const fields = [{ name: 'occurrence_pk', primary: true }]
  const before = [{ occurrence_pk: 'o1', sex: 'f' }, { occurrence_pk: 'o2', sex: 'm' }]
  // By key, even when the row moved and was edited.
  assert.equal(findRecordIndex(columns, before[1], 1, [{ occurrence_pk: 'o2', sex: 'male' }, { occurrence_pk: 'o1', sex: 'f' }], fields), 0)
  // Key gone: the record is no longer on this page.
  assert.equal(findRecordIndex(columns, before[1], 1, [{ occurrence_pk: 'o1', sex: 'f' }, { occurrence_pk: 'o3', sex: 'm' }], fields), null)
  // No key field: found only by identical content; never replaced by whatever row is now in its place.
  assert.equal(findRecordIndex(columns, before[1], 1, [{ occurrence_pk: 'o2', sex: 'm' }, { occurrence_pk: 'o9', sex: 'x' }], null), 0)
  assert.equal(findRecordIndex(columns, before[1], 1, [{ occurrence_pk: 'o1', sex: 'f' }, { occurrence_pk: 'o2', sex: 'male' }], null), null)
  // Alice, Bob, Carol: Bob is open and deleted. Carol must not take his place.
  const people = [{ name: 'Alice' }, { name: 'Bob' }, { name: 'Carol' }]
  assert.equal(findRecordIndex(['name'], people[1], 1, [people[0], people[2]], null), null)
  // Reordered rows still find the same record.
  assert.equal(findRecordIndex(['name'], people[1], 1, [people[2], people[1], people[0]], null), 1)
  assert.equal(findRecordIndex(columns, before[1], 1, [{ occurrence_pk: 'o1', sex: 'f' }], null), null)
  assert.equal(findRecordIndex(columns, before[0], 0, [], fields), null)
  assert.equal(findRecordIndex(columns, undefined, 0, before, fields), null)
})

test('only stats for the version on screen may hide columns', () => {
  const stats = { table: 1, version: '2026-11-17T10:00:00.123456Z', columns: [] }
  assert.equal(statsMatchVersion(stats, '2026-11-17T10:00:00.123456Z'), true)
  assert.equal(statsMatchVersion(stats, '2026-11-17T10:00:00.123Z'), true)
  assert.equal(statsMatchVersion(stats, '2026-11-17T10:05:00Z'), false)
  // Unknown versions (a missing timestamp, the package modal's old reload counter) never hide columns.
  assert.equal(statsMatchVersion(stats, ''), false)
  assert.equal(statsMatchVersion(stats, '7'), false)
  assert.equal(statsMatchVersion({ ...stats, version: undefined }, '2026-11-17T10:00:00Z'), false)
  assert.equal(statsMatchVersion(null, '2026-11-17T10:00:00Z'), false)
  // An empty column the agent has just filled stays visible while the new stats load.
  const old = { table: 1, version: '2026-11-17T10:00:00Z', columns: [{ name: 'a', populated: 2 }, { name: 'b', populated: 0 }] }
  const current = statsMatchVersion(old, '2026-11-17T10:05:00Z') ? old : null
  assert.deepEqual(visibleColumns(['a', 'b'], current, false), ['a', 'b'])
})
