const toolCallsFor = (message) => (
  Array.isArray(message?.openai_obj?.tool_calls)
    ? message.openai_obj.tool_calls
    : []
)

export const groupAgentMessages = (messages) => {
  const safeMessages = Array.isArray(messages) ? messages : []
  const toolResultsByCallId = new Map(
    safeMessages
      .filter((message) => message?.role === 'tool' && message.openai_obj?.tool_call_id)
      .map((message) => [message.openai_obj.tool_call_id, message])
  )
  const pythonToolCallIds = new Set()
  const processingEntries = []

  safeMessages.forEach((message) => {
    if (message?.role !== 'assistant') {
      return
    }

    toolCallsFor(message).forEach((toolCall) => {
      if (toolCall?.function?.name !== 'Python') {
        return
      }

      pythonToolCallIds.add(toolCall.id)
      processingEntries.push({
        kind: 'python',
        message,
        toolCall,
        toolResult: toolResultsByCallId.get(toolCall.id) || null,
      })
    })
  })

  safeMessages.forEach((message) => {
    if (
      message?.role === 'tool'
      && !pythonToolCallIds.has(message.openai_obj?.tool_call_id)
    ) {
      processingEntries.push({
        kind: 'result',
        message,
        toolCall: null,
        toolResult: null,
      })
    }
  })

  processingEntries.sort((left, right) => {
    const leftIndex = safeMessages.indexOf(left.message)
    const rightIndex = safeMessages.indexOf(right.message)
    return leftIndex - rightIndex
  })

  const conversationMessages = safeMessages.filter((message) => {
    if (message?.role === 'system' || message?.role === 'tool') {
      return false
    }
    return !(message?.role === 'assistant' && toolCallsFor(message).length > 0)
  })

  return {
    conversationMessages,
    processingEntries,
  }
}
