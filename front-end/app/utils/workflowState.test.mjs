import test from 'node:test'
import assert from 'node:assert/strict'

import { datasetIsActivelyProcessing } from './workflowState.mjs'

const datasetWithAgent = (agent, overrides = {}) => ({
  package_ready: false,
  published_at: null,
  visible_agent_set: [agent],
  ...overrides,
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
