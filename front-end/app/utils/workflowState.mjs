const visibleAgents = (dataset) => (
  Array.isArray(dataset?.visible_agent_set) ? dataset.visible_agent_set : []
)

export const getLatestVisibleAgent = (dataset) => {
  const agents = visibleAgents(dataset)
  return agents.length > 0 ? agents[agents.length - 1] : null
}

export const datasetNeedsWorkflowAdvance = (dataset) => {
  if (!dataset || dataset.published_at != null || dataset.package_ready) {
    return false
  }

  const latestAgent = getLatestVisibleAgent(dataset)
  return latestAgent == null || latestAgent.completed_at != null
}

export const datasetIsActivelyProcessing = (dataset) => {
  const latestAgent = getLatestVisibleAgent(dataset)
  if (!latestAgent || latestAgent.completed_at != null) {
    return false
  }

  if (latestAgent.busy_thinking) {
    return true
  }

  const messages = Array.isArray(latestAgent.message_set) ? latestAgent.message_set : []
  if (messages.length === 0) {
    return true
  }

  const lastMessage = messages[messages.length - 1]
  const assistantHasToolCalls = (
    lastMessage?.role === 'assistant'
    && Array.isArray(lastMessage?.openai_obj?.tool_calls)
    && lastMessage.openai_obj.tool_calls.length > 0
  )

  return lastMessage?.role !== 'assistant' || assistantHasToolCalls
}
