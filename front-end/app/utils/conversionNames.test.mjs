import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DECISION_LABELS, bulkActions, bulkBody, carriedMessage, checkMessage, choiceGroups, choiceLabel, classificationContext, decisionBody, decisionResult,
  isChecking, isEditable, pageCount, pageQuery, parseNote, replacementWarning, skippedMessage, unconfirmedMessage,
} from './conversionNames.mjs'

// "Calanus" (conversion 566, 1,092 rows) as the server lists it: COL's pick is the phylum, the genus is an alternative.
const coarser = { decision: 'col', same_name: false, matchType: 'HIGHERRANK',
  usage: { id: 'RT', scientificName: 'Arthropoda', taxonRank: 'phylum', classification: { kingdom: 'Animalia', phylum: 'Arthropoda' } },
  replaces: { kind: 'coarser', text: 'replaces your genus with a phylum' } }
const leach = { decision: 'alternative', same_name: true, replaces: null, matchType: 'EXACT',
  usage: { id: '7NRJ6', scientificName: 'Calanus', scientificNameAuthorship: 'Leach, 1816', taxonRank: 'genus', status: 'accepted',
    classification: { kingdom: 'Animalia', phylum: 'Arthropoda', class: 'Copepoda', family: 'Calanidae' } } }
const saussure = { ...leach, usage: { ...leach.usage, id: '8NMRP', scientificNameAuthorship: 'Saussure, 1862', status: 'synonym',
  classification: { kingdom: 'Animalia', phylum: 'Arthropoda', class: 'Insecta' } } }
const cajanus = { decision: 'alternative', same_name: false, matchType: 'VARIANT',
  usage: { id: '9CK8F', scientificName: 'Cajanus', scientificNameAuthorship: 'Adans.', taxonRank: 'genus', classification: { kingdom: 'Plantae' } },
  replaces: { kind: 'genus', text: 'replaces your name with Cajanus' } }
const calanus = { label: 'Calanus', rows: 1092, suggested: 'keep', col_choices: [coarser, leach, saussure, cajanus] }

test('exact same-name COL names are listed inline with their classification; coarser and other names are not', () => {
  const groups = choiceGroups(calanus)
  assert.equal(groups.main, coarser)
  assert.deepEqual(groups.sameName.map(choice => choice.usage.id), ['7NRJ6', '8NMRP'])
  assert.deepEqual(groups.others.map(choice => choice.usage.id), ['9CK8F'])
  assert.equal(choiceLabel(leach), 'Calanus Leach, 1816 · genus · Animalia › Arthropoda › Copepoda')
  assert.equal(classificationContext(saussure.usage), 'Animalia › Arthropoda › Insecta')
  assert.equal(classificationContext({}), '')
  assert.deepEqual(choiceGroups({}), { main: null, sameName: [], others: [] })
  // An alternative that repeats COL's own pick is not offered twice.
  assert.deepEqual(choiceGroups({ col_choices: [{ ...leach, decision: 'col' }, leach] }).sameName, [])
})

test('a coarser COL name is spelled out before it can replace the user name', () => {
  assert.equal(replacementWarning(coarser, 1092),
    '“Arthropoda” replaces your genus with a phylum on 1,092 rows. Your text stays in verbatimIdentification.')
  assert.equal(replacementWarning(leach, 1092), '')
  assert.deepEqual(decisionBody('p1', 'Calanus', 'col', undefined, { confirmCoarser: true }).name_decisions,
    { Calanus: { decision: 'col', confirm_coarser: true } })
  assert.deepEqual(decisionBody('p1', 'Calanus', 'alternative', '7NRJ6').name_decisions,
    { Calanus: { decision: 'alternative', usage_id: '7NRJ6' } })
})

test('earlier unconfirmed choices are announced and spelling corrections list what changes', () => {
  assert.equal(unconfirmedMessage({ unconfirmed: 0 }), null)
  assert.equal(unconfirmedMessage(undefined), null)
  assert.match(unconfirmedMessage({ unconfirmed: 1 }), /^1 earlier choice needs confirming: it replaces a name .* your own name is kept/)
  assert.match(unconfirmedMessage({ unconfirmed: 3 }), /^3 earlier choices need confirming: they replace names/)
  const spelling = bulkActions({ summary: { bulk_spelling: 2, spelling_corrections: [
    { label: 'Circium heterophyllum', to: 'Cirsium heterophyllum' }, { label: 'Trema orientalis', to: 'Trema orientale' }] } })
  assert.deepEqual(spelling.map(action => [action.bulk, action.count]), [['spelling', 2]])
  assert.deepEqual(spelling[0].items, ['Circium heterophyllum → Cirsium heterophyllum', 'Trema orientalis → Trema orientale'])
})

test('decisions kept from an earlier check are mentioned', () => {
  assert.equal(carriedMessage(undefined), null)
  assert.equal(carriedMessage({ decisions: 0, bulk_not_carried: 0 }), null)
  assert.equal(carriedMessage({ decisions: 1, bulk_not_carried: 0 }), '1 earlier name decision was kept from your previous check.')
  assert.equal(carriedMessage({ decisions: 989, bulk_not_carried: 12 }),
    '989 earlier name decisions were kept from your previous check. 12 accepted in bulk are offered again in bulk under the current checks.')
})

test('"Leave empty" is now "No name published"', () => {
  assert.equal(DECISION_LABELS.empty, 'No name published')
})

const review = (patch = {}) => ({ status: 'complete', error: '', summary: { labels: 10, checked: 10, bulk_col: 3, bulk_parsed: 0 }, ...patch })

test('decision results show what is written to scientificName', () => {
  assert.equal(decisionResult({ decision: 'col', scientificName: 'Aus bus', scientificNameAuthorship: 'L.' }), 'Aus bus L.')
  assert.equal(decisionResult({ decision: 'keep' }), 'The supplied text')
  assert.equal(decisionResult({ decision: 'empty' }), '')
  assert.equal(decisionResult(null), '')
})

test('messages follow the check status', () => {
  assert.equal(checkMessage(review()), null)
  assert.equal(checkMessage(null), null)
  assert.match(checkMessage(review({ status: 'running', summary: { labels: 1200, checked: 100 } })).text, /100 of 1,200 names checked/)
  assert.equal(checkMessage(review({ checking: true, status: 'incomplete' })).spinner, true)
  const failed = checkMessage(review({ status: 'error', error: 'GBIF unreachable', summary: { labels: 4, checked: 1 } }))
  assert.equal(failed.variant, 'warning')
  assert.match(failed.text, /GBIF unreachable.*Conversion does not depend on them/)
  assert.match(checkMessage(review({ status: 'incomplete', summary: { labels: 4, checked: 1 } })).text, /check again/)
  assert.equal(isChecking(review({ checking: true })), true)
  assert.equal(isChecking(review()), false)
})

test('bulk actions list only those that would decide something', () => {
  assert.deepEqual(bulkActions(review()).map(action => [action.bulk, action.count]), [['exact_col', 3]])
  assert.deepEqual(bulkActions({ summary: { bulk_col: 0, bulk_parsed: 0 } }), [])
  assert.deepEqual(bulkActions(undefined), [])
})

test('request bodies carry the plan id and withdraw decisions with null', () => {
  assert.deepEqual(decisionBody('p1', 'Aus bus', 'alternative', 'X1'), { action: 'names', plan_id: 'p1', name_decisions: { 'Aus bus': { decision: 'alternative', usage_id: 'X1' } } })
  assert.deepEqual(decisionBody('p1', 'Aus bus', 'keep').name_decisions, { 'Aus bus': { decision: 'keep' } })
  assert.deepEqual(decisionBody('p1', 'Aus bus', null).name_decisions, { 'Aus bus': null })
  assert.deepEqual(bulkBody('p1', 'parsed'), { action: 'names', plan_id: 'p1', bulk: 'parsed' })
})

test('paging and editing rules', () => {
  assert.equal(pageQuery({ offset: 200, view: 'all' }), 'names_offset=200&names_limit=100&names_view=all')
  assert.equal(pageQuery(), 'names_offset=0&names_limit=100&names_view=pending')
  assert.equal(pageCount(0), 1)
  assert.equal(pageCount(250), 3)
  assert.equal(isEditable({ status: 'reviewing' }), true)
  assert.equal(isEditable({ status: 'complete' }), false)
})

test('parse notes explain why a split is or is not offered', () => {
  assert.match(parseNote({ qualifier: 'sp.', parsed: { usable: false } }), /qualifier/)
  assert.match(parseNote({ parsed: { usable: false } }), /could not read/)
  assert.match(parseNote({ parsed: { usable: true, lossless: false } }), /reformat/)
  assert.match(parseNote({ parsed: { usable: true, lossless: true, authorship: 'L.' } }), /Splits exactly/)
  assert.equal(parseNote({}), 'Not parsed yet')
})

test('overlong names are reported, not hidden', () => {
  assert.equal(skippedMessage({ skipped_long: { labels: 0, rows: 0 } }), null)
  assert.equal(skippedMessage({}), null)
  assert.match(skippedMessage({ skipped_long: { labels: 1, rows: 1 }, max_label_chars: 500 }), /^1 name is longer than 500 characters \(1 row\)/)
  assert.match(skippedMessage({ skipped_long: { labels: 3, rows: 1200 }, max_label_chars: 500 }), /3 names are longer than 500 characters \(1,200 rows\)/)
})
