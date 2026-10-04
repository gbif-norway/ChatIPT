import test from 'node:test'
import assert from 'node:assert/strict'
import { aiDecidedItems, attentionItems, chatVisible, conflictsFor, decisionTitle, openProposals, openQuestions, optionState, shownRecommendation, shownRecommendations, unresolvedIssues } from './conversionReview.mjs'

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

const reviewPlan = {
  issues: [
    { id: 'layout', title: 'Confirm layout', options: [{ value: 'confirm', label: 'Confirm' }] },
    { id: 'column:0:2', title: 'scientificName', options: [{ value: 'occurrence.scientificName', label: 'Occurrence name' }, { value: 'preserve', label: 'Keep in originals' }] },
    { id: 'status:0', title: 'Status', options: [{ value: 'present', label: 'Present' }, { value: 'absent', label: 'Absent' }] },
  ],
}

test('AI-applied choices carry their reason and stale flag', () => {
  const state = {
    plan: reviewPlan, decisions: { 'column:0:2': 'occurrence.scientificName' },
    review: { applied: ['column:0:2', 'missing'], conflicted: ['column:0:2'], recommendations: { 'column:0:2': { stale_basis: true } } },
    decision_sources: { 'column:0:2': { source: 'ai-reviewer', rationale: 'Names have no authorship.' } },
  }
  const [decided] = aiDecidedItems(state)
  assert.equal(aiDecidedItems(state).length, 1)
  assert.equal(decided.label, 'Occurrence name')
  assert.equal(decided.source.rationale, 'Names have no authorship.')
  assert.equal(decided.stale, true)
  assert.equal(decided.conflict, true)
})

test('recommendations are shown only while current and not already chosen', () => {
  const record = { outcome: 'escalated', option: 'present', current: true }
  assert.equal(shownRecommendation({ review: { recommendations: { 'status:0': record } }, decisions: {} }, 'status:0'), record)
  assert.equal(shownRecommendation({ review: { recommendations: { 'status:0': { ...record, current: false } } }, decisions: {} }, 'status:0'), null)
  assert.equal(shownRecommendation({ review: { recommendations: { 'status:0': record } }, decisions: { 'status:0': 'present' } }, 'status:0'), null)
  assert.equal(shownRecommendation({ review: { recommendations: { 'status:0': { ...record, outcome: 'applied' } } } }, 'status:0'), null)
})

test('only the latest proposals for open choices can be confirmed', () => {
  const state = {
    plan: reviewPlan, review: { escalated: ['status:0'] },
    chat: { available: true, messages: [
      { id: 1, role: 'assistant', current_plan: true, proposals: [{ id: 'layout', value: 'confirm' }] },
      { id: 2, role: 'user', current_plan: true, proposals: [] },
      { id: 3, role: 'assistant', current_plan: true, proposals: [{ id: 'status:0', value: 'present' }, { id: 'layout', value: 'confirm' }] },
    ] },
  }
  assert.deepEqual(openProposals(state), [{ id: 'status:0', value: 'present', message_id: 3, title: 'Status', label: 'Present' }])
  assert.deepEqual(openProposals({ ...state, chat: { messages: [{ ...state.chat.messages[2], current_plan: false }] } }), [])
  assert.equal(chatVisible(state), true)
  assert.equal(chatVisible({ ...state, chat: { available: false, messages: [] } }), false)
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

test('open questions name their decision and carry current recommendations', () => {
  const state = {
    plan: reviewPlan, decisions: {},
    review: { escalated: ['status:0', 'layout', 'gone'], recommendations: {
      'status:0': { outcome: 'escalated', option: 'present', option_label: 'Present', current: true },
      layout: { outcome: 'escalated', option: 'confirm', current: false },
    } },
  }
  assert.deepEqual(openQuestions(state), [{ id: 'status:0', title: 'Status' }, { id: 'layout', title: 'Confirm layout' }, { id: 'gone', title: 'gone' }])
  assert.deepEqual(shownRecommendations(state).map(entry => [entry.id, entry.recommendation.option]), [['status:0', 'present']])
  assert.equal(decisionTitle(state, 'layout'), 'Confirm layout')
  assert.equal(decisionTitle(state, 'other'), 'other')
  assert.deepEqual(openQuestions({}), [])
})
