import test from 'node:test'
import assert from 'node:assert/strict'
import { attentionItems, conflictsFor, optionState, pendingAdvice, unresolvedIssues } from './conversionReview.mjs'

const plan = {
  issues: [{ id: 'layout' }, { id: 'table:1', table: 1 }, { id: 'row-group:1:0', table: 1, members: ['row:1:0', 'row:1:4'] }],
  automatic_choices: [{ id: 'event-grain', default: 'by_id' }],
  columns: [{ id: 'column:0:2', table: 0 }],
}

test('unresolved choices come from the server, including grouped rows', () => {
  const state = { plan, unresolved: ['table:1', 'row-group:1:0'] }
  assert.deepEqual(unresolvedIssues(state).map(item => item.id), ['table:1', 'row-group:1:0'])
  assert.deepEqual(unresolvedIssues({ plan }), [])
})

test('later advice skips reviewed items and earlier suggestions', () => {
  const state = { plan, unresolved: ['layout', 'table:1', 'row-group:1:0'], suggestions: [{ id: 'layout' }], advice_reviewed: ['table:1'] }
  assert.deepEqual(pendingAdvice(state).map(item => item.id), ['row-group:1:0'])
})

test('automatic choices and columns with failing requirements need attention', () => {
  const state = { plan, unresolved: ['event-grain', 'column:0:2', 'layout'] }
  assert.deepEqual(attentionItems(state).map(item => item.id), ['event-grain', 'column:0:2'])
})

test('option availability and conflicts are looked up by decision id', () => {
  const state = {
    option_status: { 'event-grain': { by_id: { available: false, reasons: ['eventDate disagrees'] } } },
    conflicts: [{ id: 'conflict:1', decision_ids: ['table:1', 'row:1:0'], reason: 'Patch conflict' }],
  }
  assert.equal(optionState(state, 'event-grain', 'by_id').available, false)
  assert.equal(optionState(state, 'event-grain', 'per_row').available, true)
  assert.equal(conflictsFor(state, 'table:1').length, 1)
  assert.equal(conflictsFor(state, 'layout').length, 0)
})
