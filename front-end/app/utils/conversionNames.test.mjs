import test from 'node:test'
import assert from 'node:assert/strict'
import { bulkActions, bulkBody, checkMessage, decisionBody, decisionResult, isChecking, isEditable, pageCount, pageQuery, parseNote } from './conversionNames.mjs'

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
