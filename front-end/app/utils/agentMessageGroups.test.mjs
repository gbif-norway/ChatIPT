import test from 'node:test'
import assert from 'node:assert/strict'

import { groupAgentMessages } from './agentMessageGroups.mjs'

const message = (id, role, openaiObj = {}) => ({
  id,
  role,
  openai_obj: openaiObj,
})

test('keeps user-facing conversation separate from processing messages', () => {
  const messages = [
    message(1, 'system', { content: 'instructions' }),
    message(2, 'user', { content: 'Please continue' }),
    message(3, 'assistant', {
      tool_calls: [{ id: 'call-1', function: { name: 'ValidateDwCA', arguments: '{}' } }],
    }),
    message(4, 'tool', { tool_call_id: 'call-1', content: 'valid' }),
    message(5, 'assistant', { content: 'The package is ready.' }),
  ]

  const grouped = groupAgentMessages(messages)

  assert.deepEqual(grouped.conversationMessages.map(({ id }) => id), [2, 5])
  assert.equal(grouped.processingEntries.length, 1)
  assert.equal(grouped.processingEntries[0].kind, 'result')
  assert.equal(grouped.processingEntries[0].message.id, 4)
})

test('pairs Python calls with results by tool call id', () => {
  const pythonMessage = message(10, 'assistant', {
    tool_calls: [
      { id: 'python-1', function: { name: 'Python', arguments: '{"code":"one"}' } },
      { id: 'metadata-1', function: { name: 'SetBasicMetadata', arguments: '{}' } },
      { id: 'python-2', function: { name: 'Python', arguments: '{"code":"two"}' } },
    ],
  })
  const messages = [
    pythonMessage,
    message(11, 'tool', { tool_call_id: 'metadata-1', content: 'metadata saved' }),
    message(12, 'tool', { tool_call_id: 'python-2', content: 'two result' }),
    message(13, 'tool', { tool_call_id: 'python-1', content: 'one result' }),
  ]

  const { processingEntries } = groupAgentMessages(messages)

  assert.equal(processingEntries.length, 3)
  assert.deepEqual(
    processingEntries.map(({ kind }) => kind),
    ['python', 'python', 'result']
  )
  assert.equal(processingEntries[0].toolCall.id, 'python-1')
  assert.equal(processingEntries[0].toolResult.id, 13)
  assert.equal(processingEntries[1].toolCall.id, 'python-2')
  assert.equal(processingEntries[1].toolResult.id, 12)
  assert.equal(processingEntries[2].message.id, 11)
})

test('handles missing and empty message collections', () => {
  assert.deepEqual(groupAgentMessages(), {
    conversationMessages: [],
    processingEntries: [],
  })
  assert.deepEqual(groupAgentMessages([]), {
    conversationMessages: [],
    processingEntries: [],
  })
})
