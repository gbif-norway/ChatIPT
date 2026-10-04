import test from 'node:test'
import assert from 'node:assert/strict'
import { pendingAdvice, unresolvedIssues } from './conversionReview.mjs'

const plan = { issues: [
  { id: 'layout' }, { id: 'table:1', table: 1 }, { id: 'column:1:0', table: 1 },
  { id: 'row:1:0', table: 1, row: 1 }, { id: 'scope:1:0', table: 1, row: 1 },
] }

test('later advice remains available alongside unapproved earlier suggestions and abstentions', () => {
  const state = { plan, suggestions: [{ id: 'layout' }], advice_reviewed: ['table:1'] }
  assert.deepEqual(pendingAdvice(state).map(item => item.id), ['column:1:0', 'row:1:0', 'scope:1:0'])
  assert.equal(unresolvedIssues(plan).length, 5)
})

test('draft approvals and preservation skip affected choices before they are saved', () => {
  assert.deepEqual(pendingAdvice({ plan }, { layout: 'confirm', 'table:1': 'preserve' }), [])
  assert.deepEqual(unresolvedIssues(plan, { 'row:1:0': 'preserve' }).map(item => item.id),
    ['layout', 'table:1', 'column:1:0'])
})

test('automatic preservation skips advice while a user override reopens the required choices', () => {
  const automaticPlan = { ...plan, automatic_choices: [{ id: 'table:1', default: 'preserve' }] }
  assert.deepEqual(pendingAdvice({ plan: automaticPlan }).map(item => item.id), ['layout'])
  assert.deepEqual(pendingAdvice({ plan: automaticPlan }, { 'table:1': 'convert' }).map(item => item.id),
    ['layout', 'column:1:0', 'row:1:0', 'scope:1:0'])
})

test('nested taxonomy row choices suppress their own dependent review items', () => {
  const nestedPlan = { issues: [
    { id: 'table:2', table: 2 },
    { id: 'taxon-occurrence:2:row:0:0', table: 2, row: 1 },
    { id: 'taxon-occurrence:2:scope:0:0', table: 2, row: 1 },
  ] }
  assert.deepEqual(unresolvedIssues(nestedPlan, { 'taxon-occurrence:2:row:0:0': 'preserve' }).map(item => item.id), ['table:2'])
})
