import assert from 'node:assert/strict'
import test from 'node:test'

import {
  askBlankText,
  askMergeText,
  askValueText,
  barPercent,
  canPageValues,
  columnValuesUrl,
  dateHistogramText,
  dateRangeText,
  displayValue,
  fieldBadges,
  filterAndSortValues,
  filterColumns,
  formatPercent,
  inlineValues,
  lineageText,
  linkSummary,
  mergeValuePages,
  nextValueOffset,
  numericBinLabel,
  variantGroupFilterValue,
  nextSort,
  numericHistogramText,
  numericSummary,
  scaleHistogram,
  sortColumns,
  uniqueSummary,
  variantFlagText,
  variantGroupHiddenCount,
  variantGroupRowCount,
  variantGroupValueCount,
} from './columnStats.mjs'

const column = (overrides = {}) => ({
  name: 'sex',
  position: 0,
  kind: 'text',
  populated: 300,
  blank: 431,
  unique: 2,
  distinctness: 'repeats',
  duplicated_values: 2,
  duplicated_rows: 300,
  top_values: [
    { value: 'female', count: 210, truncated: false },
    { value: 'male', count: 90, truncated: false },
  ],
  numeric: null,
  date: null,
  variant_groups: [],
  variant_group_count: 0,
  ...overrides,
})

test('formats percentages without claiming 0% or 100% falsely', () => {
  assert.equal(formatPercent(0, 731), '0%')
  assert.equal(formatPercent(731, 731), '100%')
  assert.equal(formatPercent(300, 731), '41%')
  assert.equal(formatPercent(1, 10000), '<0.1%')
  assert.equal(formatPercent(3, 1000), '0.3%')
  assert.equal(formatPercent(9999, 10000), '>99.9%')
  assert.equal(formatPercent(998, 1000), '99.8%')
  assert.equal(formatPercent(5, 0), '0%')
})

test('keeps tiny bars visible and scales histograms to the tallest bin', () => {
  assert.equal(barPercent(0, 100), 0)
  assert.equal(barPercent(1, 100000), 0.75)
  assert.equal(barPercent(50, 100), 50)
  assert.equal(barPercent(200, 100), 100)
  const bins = scaleHistogram([{ count: 10 }, { count: 0 }, { count: 5 }, { count: 0.1 }])
  assert.deepEqual(bins.map((bin) => bin.height), [100, 0, 50, 4])
  assert.deepEqual(scaleHistogram(null), [])
})

test('sorts columns stably with table order as the tie-break', () => {
  const columns = [
    column({ name: 'b', position: 0, populated: 5, unique: 3 }),
    column({ name: 'A', position: 1, populated: 9, unique: 3 }),
    column({ name: 'c', position: 2, populated: 5, unique: 1 }),
  ]
  assert.deepEqual(sortColumns(columns, 'position').map((c) => c.name), ['b', 'A', 'c'])
  assert.deepEqual(sortColumns(columns, 'name', 'asc').map((c) => c.name), ['A', 'b', 'c'])
  assert.deepEqual(sortColumns(columns, 'populated', 'desc').map((c) => c.name), ['A', 'b', 'c'])
  assert.deepEqual(sortColumns(columns, 'populated', 'asc').map((c) => c.name), ['b', 'c', 'A'])
  assert.deepEqual(sortColumns(columns, 'unique', 'desc').map((c) => c.name), ['b', 'A', 'c'])
  assert.deepEqual(nextSort({ key: 'name', direction: 'asc' }, 'name'), { key: 'name', direction: 'desc' })
  assert.deepEqual(nextSort({ key: 'name', direction: 'asc' }, 'populated'), { key: 'populated', direction: 'desc' })
})

test('finds columns by name or schema title and can hide empty ones', () => {
  const columns = [
    column({ name: 'eventDate' }),
    column({ name: 'notes', populated: 0, distinctness: 'empty', kind: 'empty' }),
    column({ name: 'x1' }),
  ]
  const fields = [{ name: 'x1', title: 'Collector' }]
  assert.deepEqual(filterColumns(columns, 'date').map((c) => c.name), ['eventDate'])
  assert.deepEqual(filterColumns(columns, 'collect', { fields }).map((c) => c.name), ['x1'])
  assert.deepEqual(filterColumns(columns, '', { hideEmpty: true }).map((c) => c.name), ['eventDate', 'x1'])
})

test('explains the unique count in plain words', () => {
  assert.deepEqual(
    uniqueSummary(column({ populated: 0, unique: 0, distinctness: 'empty' })).text,
    'no values',
  )
  assert.equal(uniqueSummary(column({ distinctness: 'all_distinct', unique: 300 })).text, 'all distinct')
  const constant = uniqueSummary(column({ distinctness: 'constant', unique: 1, top_values: [{ value: 'Kenya', count: 300 }] }))
  assert.equal(constant.text, 'same value in every filled row')
  assert.equal(constant.value, 'Kenya')
  assert.equal(
    uniqueSummary(column({ distinctness: 'constant', unique: 1, blank: 0, top_values: [{ value: 'Kenya', count: 731 }] })).text,
    'same value in every row',
  )
  // One filled row: no "same value in every row", the value is shown on its own.
  const single = uniqueSummary(column({ distinctness: 'constant', unique: 1, populated: 1, blank: 0, top_values: [{ value: 'Kenya', count: 1 }] }))
  assert.equal(single.text, '')
  assert.equal(single.value, 'Kenya')
  assert.equal(uniqueSummary(column()).text, '2 values repeat')
  assert.equal(uniqueSummary(column({ duplicated_values: 1 })).text, '1 value repeats')
})

test('warns about duplicated values in key and identifier columns', () => {
  const key = uniqueSummary(
    column({ name: 'occurrenceID', unique: 728, populated: 731, duplicated_values: 3, duplicated_rows: 6 }),
    { name: 'occurrenceID', primary: true },
  )
  assert.equal(key.tone, 'warning')
  assert.equal(key.text, '3 duplicated values')
  assert.deepEqual(key.filter, { column: 'occurrenceID', op: 'duplicated' })
  const fine = uniqueSummary(column({ distinctness: 'all_distinct', unique: 300 }), { weakPrimary: true })
  assert.equal(fine.tone, 'neutral')
  const constantKey = uniqueSummary(column({ distinctness: 'constant', unique: 1, duplicated_values: 1 }), { weakPrimary: true })
  assert.equal(constantKey.text, '1 duplicated value')
})

test('shows small columns inline only when every value is known', () => {
  assert.equal(inlineValues(column()).length, 2)
  assert.deepEqual(inlineValues(column({ unique: 1 })), [])
  assert.deepEqual(inlineValues(column({ unique: 7 })), [])
  assert.deepEqual(inlineValues(column({ unique: 3 })), [])
})

test('summarises numbers and dates with text alternatives', () => {
  const numeric = {
    min: 1, max: 30, median: 12, mean: 12.345,
    histogram: [{ start: 1, end: 10, count: 4 }, { start: 10, end: 20, count: 9 }, { start: 20, end: 30, count: 2 }],
  }
  assert.deepEqual(numericSummary(numeric), { min: '1', median: '12', max: '30', mean: '12.35' })
  assert.equal(
    numericHistogramText(numeric),
    'Values run from 1 to 30, median 12. The most common range is 10 to 20 (9 rows).',
  )
  assert.equal(numericHistogramText({ min: 5, max: 5, median: 5, histogram: [] }), 'Every value is 5.')
  // A narrow range above 1,000 and tiny values are not rounded into a false constant.
  const narrow = numericSummary({ min: 1500.1, max: 1500.2, median: 1500.15, mean: 1500.15 })
  assert.notEqual(narrow.min, narrow.max)
  assert.ok(numericHistogramText({ min: 1500.1, max: 1500.2, median: 1500.15, histogram: [] }).startsWith('Values run from'))
  const tiny = numericSummary({ min: 0.00001, max: 0.00003, median: 0.00002, mean: 0.00002 })
  assert.notEqual(tiny.min, '0')
  assert.notEqual(tiny.min, tiny.max)
  assert.ok(!numericHistogramText({ min: 0.00001, max: 0.00003, median: 0.00002, histogram: [] }).includes('Every value'))
  assert.equal(numericBinLabel({ start: 1000.4, end: 1000.6 }).includes(' to '), true)
  const date = { min: '2004-04-01', max: '2011-09-30', histogram: [{ label: '2004', count: 120 }, { label: '2005', count: 3 }] }
  assert.equal(dateRangeText(date), '2004-04-01 to 2011-09-30')
  assert.equal(dateRangeText({ min: '2004', max: '2004' }), '2004')
  assert.equal(
    dateHistogramText(date),
    'Dates run from 2004-04-01 to 2011-09-30. The busiest period is 2004 (120 rows).',
  )
})

test('flags spelling variants and drafts a merge request with the real values', () => {
  const group = { values: [{ value: 'male', count: 88 }, { value: 'Male ', count: 2 }] }
  assert.equal(variantFlagText(column({ variant_group_count: 0 })), '')
  assert.equal(variantFlagText(column({ variant_groups: [group], variant_group_count: 1 })), '2 values look like spelling variants')
  // The API lists at most 10 values per group; value_count and count describe the whole group.
  const big = {
    values: Array.from({ length: 10 }, (_, index) => ({ value: `Male${'.'.repeat(index)}`, count: 10 - index })),
    count: 70,
    value_count: 14,
  }
  assert.equal(variantGroupValueCount(big), 14)
  assert.equal(variantGroupRowCount(big), 70)
  assert.equal(variantGroupHiddenCount(big), 4)
  assert.equal(variantFlagText(column({ variant_groups: [big], variant_group_count: 1 })), '14 values look like spelling variants')
  assert.equal(variantGroupValueCount(group), 2)
  assert.equal(variantGroupRowCount(group), 90)
  assert.equal(variantGroupHiddenCount(group), 0)
  const mergeBig = askMergeText('occurrence', 'sex', big)
  assert.match(mergeBig, /The group has 14 values in total across 70 rows, including "Male" \(10 rows\), /)
  assert.match(mergeBig, /"Male\.{9}" \(1 row\) and 4 more values\. Please check whether all of them mean the same thing/)
  assert.equal(variantFlagText(column({ variant_groups: [group, group], variant_group_count: 4 })), '4 groups of values look like spelling variants')
  assert.equal(
    askMergeText('occurrence', 'sex', group),
    'In the occurrence table, the sex column has values that look like spelling variants of each other: "male" (88 rows) and "Male " (2 rows). Please check whether they mean the same thing and, if so, merge them into one consistent value.',
  )
})

test('drafts questions about a value or blanks', () => {
  assert.equal(
    askValueText('occurrence', 'scientificName', 'Pteromalidae', 64),
    'In the occurrence table, the scientificName column has the value "Pteromalidae" in 64 rows. ',
  )
  assert.equal(askValueText('', 'sex', 'f', 1), 'In this table, the sex column has the value "f" in 1 row. ')
  assert.equal(askBlankText('event', 'eventDate', 12), 'In the event table, the eventDate column is blank in 12 rows. ')
})

test('makes edge whitespace visible', () => {
  assert.equal(displayValue('Male '), 'Male␣')
  assert.equal(displayValue('  x'), '␣␣x')
  assert.equal(displayValue('plain'), 'plain')
  assert.equal(displayValue(''), '(empty text)')
})

test('describes link health and lineage', () => {
  assert.deepEqual(
    linkSummary({ targetTitle: 'Event', populatedRows: 731, linkedRows: 731, unmatchedRows: 0, blankRows: 0 }),
    { target: 'Event', predicate: '', tone: 'good', text: 'all 731 link to Event', blankText: '' },
  )
  const broken = linkSummary({ targetTitle: 'Event', populatedRows: 700, linkedRows: 688, unmatchedRows: 12, blankRows: 31 })
  assert.equal(broken.tone, 'warning')
  assert.equal(broken.text, '12 do not match any Event')
  assert.equal(broken.blankText, '31 blank')
  assert.equal(linkSummary(null), null)
  assert.equal(
    lineageText([{ column: 'Species', table: 'wasps.xlsx - Sheet1', basis: 'recorded' }]),
    'from Species in wasps.xlsx - Sheet1',
  )
  assert.equal(
    lineageText([{ column: 'Sp', table: 'raw', basis: 'matched' }]),
    'from Sp in raw (likely, matched by values)',
  )
  assert.equal(
    lineageText([{ column: 'basisOfRecord', table: 'occurrence.txt', basis: 'recorded', note: 'derived from this column' }]),
    'from basisOfRecord in occurrence.txt (derived from this column)',
  )
  assert.equal(
    lineageText([{ column: 'Sp', table: 'raw', basis: 'matched', note: '39 of 40 distinct values found' }]),
    'from Sp in raw (likely, matched by values; 39 of 40 distinct values found)',
  )
  assert.equal(lineageText([{ column: 'Species', table: '', table_id: null, basis: 'recorded' }]), 'from Species in an uploaded file')
  assert.equal(lineageText([{ column: '', table: 'wasps.xlsx', basis: 'recorded' }]), 'from wasps.xlsx')
  assert.equal(lineageText([{ column: null, table: null, basis: 'matched' }]), 'from an uploaded file (likely, matched by values)')
})

test('builds column-value URLs and pages values', () => {
  assert.equal(
    columnValuesUrl('https://x.org', 12, { column: 'sex', search: ' fem ', offset: 10, limit: 500, sort: 'value' }),
    'https://x.org/api/tables/12/column-values/?column=sex&offset=10&limit=200&sort=value&search=fem',
  )
  assert.equal(
    columnValuesUrl('https://x.org', 12, { column: 'a b' }),
    'https://x.org/api/tables/12/column-values/?column=a+b&offset=0&limit=50&sort=count',
  )
  assert.deepEqual(
    mergeValuePages([{ value: 'a', count: 2 }], [{ value: 'a', count: 2 }, { value: 'b', count: 1 }]).map((v) => v.value),
    ['a', 'b'],
  )
  assert.equal(canPageValues(column({ unique: 2 })), false)
  assert.equal(canPageValues(column({ unique: 40 })), true)
  assert.deepEqual(
    filterAndSortValues([{ value: 'b', count: 1 }, { value: 'a', count: 1 }, { value: 'c', count: 5 }]).map((v) => v.value),
    ['c', 'a', 'b'],
  )
  assert.deepEqual(
    filterAndSortValues([{ value: 'Bee', count: 1 }, { value: 'wasp', count: 3 }], { search: 'be', sort: 'value' }).map((v) => v.value),
    ['Bee'],
  )
})

test('labels schema badges', () => {
  assert.deepEqual(fieldBadges({ primary: true, type: 'string' }).map((b) => b.label), ['primary key', 'string'])
  assert.deepEqual(fieldBadges({ weakPrimary: true }).map((b) => b.label), ['public identifier'])
  assert.deepEqual(fieldBadges(null), [])
})

test('cut-short values keep their place in paging and are never filtered on', () => {
  const long = 'x'.repeat(300)
  const page1 = [{ value: long, count: 2, truncated: true }]
  const page2 = [{ value: long, count: 1, truncated: true }, { value: 'b', count: 1, truncated: false }]
  const merged = mergeValuePages(page1, page2)
  assert.equal(merged.length, 3)
  assert.equal(nextValueOffset(0, page1), 1)
  assert.equal(nextValueOffset(1, page2), 3)
  assert.equal(variantGroupFilterValue({ values: [{ value: long, truncated: true }, { value: 'Short', truncated: false }] }), 'Short')
  assert.equal(variantGroupFilterValue({ values: [{ value: long, truncated: true }] }), null)
  const constant = uniqueSummary({
    name: 'remarks', kind: 'text', populated: 3, blank: 0, unique: 1, distinctness: 'constant',
    top_values: [{ value: long, count: 3, truncated: true }],
  })
  assert.equal(constant.valueTruncated, true)
})

test('links the package was too large to check read as not checked, not as zero', () => {
  const summary = linkSummary({ targetTitle: 'Event', coverage: 'skipped', linkedRows: null, unmatchedRows: null, blankRows: null })
  assert.equal(summary.tone, 'muted')
  assert.match(summary.text, /not checked/)
  assert.equal(summary.blankText, '')
})
