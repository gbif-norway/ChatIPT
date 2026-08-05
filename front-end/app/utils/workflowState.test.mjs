import test from 'node:test'
import assert from 'node:assert/strict'

import {
  datasetIsActivelyProcessing,
  datasetNeedsWorkflowAdvance,
} from './workflowState.mjs'

const datasetWithAgent = (agent, overrides = {}) => ({
  package_ready: false,
  published_at: null,
  visible_agent_set: [agent],
  ...overrides,
})

test('an incomplete dataset with no agents needs initialization', () => {
  assert.equal(datasetNeedsWorkflowAdvance(datasetWithAgent(null, { visible_agent_set: [] })), true)
})

test('a reopened dataset advances when its latest agent is complete', () => {
  const dataset = datasetWithAgent({ completed_at: '2026-06-11T06:16:05Z' })
  assert.equal(datasetNeedsWorkflowAdvance(dataset), true)
})

test('an active, ready, or published dataset does not advance on load', () => {
  assert.equal(datasetNeedsWorkflowAdvance(datasetWithAgent({ completed_at: null })), false)
  assert.equal(datasetNeedsWorkflowAdvance(datasetWithAgent(null, {
    visible_agent_set: [],
    package_ready: true,
  })), false)
  assert.equal(datasetNeedsWorkflowAdvance(datasetWithAgent(null, {
    visible_agent_set: [],
    published_at: '2026-08-05T10:00:00Z',
  })), false)
})

test('only an incomplete agent with pending work is actively processing', () => {
  assert.equal(datasetIsActivelyProcessing(datasetWithAgent({
    completed_at: null,
    busy_thinking: true,
    message_set: [],
  })), true)

  assert.equal(datasetIsActivelyProcessing(datasetWithAgent({
    completed_at: null,
    busy_thinking: false,
    message_set: [{ role: 'tool', openai_obj: {} }],
  })), true)

  assert.equal(datasetIsActivelyProcessing(datasetWithAgent({
    completed_at: null,
    busy_thinking: false,
    message_set: [{ role: 'assistant', openai_obj: { tool_calls: [] } }],
  })), false)

  assert.equal(datasetIsActivelyProcessing(datasetWithAgent({
    completed_at: '2026-06-11T06:16:05Z',
    busy_thinking: false,
    message_set: [{ role: 'tool', openai_obj: {} }],
  })), false)
})
