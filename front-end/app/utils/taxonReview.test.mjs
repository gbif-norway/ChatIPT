import test from 'node:test'
import assert from 'node:assert/strict'

import { filterRows, formatName, reviewSummaryMessage, splitScientificName, statusInfo } from './taxonReview.mjs'

const rows = [
  { verbatim_label: 'N_silvestris', decision: 'pending', record_count: 55,
    query: { scientificName: 'Nothrus silvestris' }, match: { status: 'exact' } },
  { verbatim_label: 'Pająki', decision: 'accepted', record_count: 87,
    query: { scientificName: 'Araneae' }, match: { status: 'exact', usage: { scientificName: 'Araneae' } } },
  { verbatim_label: 'Zieminek', decision: 'keep_original', record_count: 87, match: { status: 'none' } },
  { verbatim_label: 'Neodiscopoma splendida', decision: 'not_in_col', record_count: 149,
    match: { status: 'higher_rank' } },
]

test('filters by review state and searches labels and interpretations', () => {
  assert.deepEqual(filterRows(rows).map((row) => row.verbatim_label), ['N_silvestris'])
  assert.equal(filterRows(rows, { view: 'reviewed' }).length, 3)
  assert.deepEqual(
    filterRows(rows, { view: 'all', query: 'nothrus' }).map((row) => row.verbatim_label),
    ['N_silvestris'],
  )
  assert.deepEqual(
    filterRows(rows, { view: 'all', query: 'araneae' }).map((row) => row.verbatim_label),
    ['Pająki'],
  )
})

test('formats names and statuses', () => {
  assert.equal(formatName({ scientificName: 'Nothrus silvestris', scientificNameAuthorship: 'Nicolet, 1855' }),
    'Nothrus silvestris Nicolet, 1855')
  assert.equal(formatName(null), '')
  assert.equal(statusInfo({ match: { status: 'higher_rank' } }).label, 'Higher rank only')
  assert.equal(statusInfo({}).label, 'No match')
})

test('summarises decisions for the assistant', () => {
  assert.equal(
    reviewSummaryMessage(rows),
    'I have finished reviewing the taxon names: 1 name accepted, 1 name marked as correct but not in COL, '
      + '1 name kept unchanged. 1 name (55 records) is left unreviewed; leave it unchanged. '
      + 'Please apply the reviewed names.',
  )
  assert.equal(
    reviewSummaryMessage([rows[1]]),
    'I have finished reviewing the taxon names: 1 name accepted. Please apply the reviewed names.',
  )
})

test('splits names from authorship to prefill the not-in-COL form', () => {
  assert.deepEqual(splitScientificName('Neodiscopoma splendida (Kramer, 1882)'), {
    scientificName: 'Neodiscopoma splendida', scientificNameAuthorship: '(Kramer, 1882)', taxonRank: 'species',
  })
  assert.deepEqual(splitScientificName('Zercon peltatus peltatoides Halaskova, 1969'), {
    scientificName: 'Zercon peltatus peltatoides', scientificNameAuthorship: 'Halaskova, 1969', taxonRank: 'subspecies',
  })
  assert.deepEqual(splitScientificName('Nothrus capilatus'), {
    scientificName: 'Nothrus capilatus', scientificNameAuthorship: '', taxonRank: 'species',
  })
  assert.equal(splitScientificName('Ptyctima').taxonRank, '')
})
