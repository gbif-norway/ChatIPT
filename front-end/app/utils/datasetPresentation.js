export const DATASET_STATUS = {
  preparing: {
    label: 'Preparing',
    badgeClass: 'text-bg-primary',
    textClass: 'text-primary',
    icon: 'bi-arrow-clockwise',
  },
  needs_input: {
    label: 'Needs input',
    badgeClass: 'text-bg-warning',
    textClass: 'text-warning',
    icon: 'bi-chat-left-text',
  },
  ready: {
    label: 'Ready',
    badgeClass: 'text-bg-success',
    textClass: 'text-success',
    icon: 'bi-check-circle',
  },
  published: {
    label: 'Published',
    badgeClass: 'text-bg-success',
    textClass: 'text-success',
    icon: 'bi-check-circle',
  },
}

const RESOURCE_LABELS = {
  agent: ['agent', 'agents'],
  'bibliographic-resource': ['reference', 'references'],
  'chronometric-age': ['chronometric age', 'chronometric ages'],
  event: ['event', 'events'],
  'geological-context': ['geological context', 'geological contexts'],
  identification: ['identification', 'identifications'],
  material: ['material entity', 'material entities'],
  media: ['media item', 'media items'],
  'molecular-protocol': ['molecular protocol', 'molecular protocols'],
  'nucleotide-analysis': ['nucleotide analysis', 'nucleotide analyses'],
  'nucleotide-sequence': ['nucleotide sequence', 'nucleotide sequences'],
  occurrence: ['occurrence', 'occurrences'],
  organism: ['organism', 'organisms'],
  'organism-interaction': ['organism interaction', 'organism interactions'],
  protocol: ['protocol', 'protocols'],
  provenance: ['provenance record', 'provenance records'],
  survey: ['survey', 'surveys'],
  'survey-target': ['survey target', 'survey targets'],
  'usage-policy': ['usage policy', 'usage policies'],
}

export const getDatasetStatus = (dataset) => {
  if (dataset?.published_at) return 'published'
  if (dataset?.package_ready) return 'ready'

  const agents = Array.isArray(dataset?.visible_agent_set) ? dataset.visible_agent_set : []
  const latestAgent = agents.at(-1)
  if (!latestAgent || latestAgent.completed_at) return 'preparing'

  const messages = Array.isArray(latestAgent.message_set) ? latestAgent.message_set : []
  const latestMessage = messages.at(-1)
  const hasToolCalls = latestMessage?.role === 'assistant'
    && Array.isArray(latestMessage?.openai_obj?.tool_calls)
    && latestMessage.openai_obj.tool_calls.length > 0
  const isWorking = (
    latestAgent.busy_thinking
    || !latestMessage
    || latestMessage.role !== 'assistant'
    || hasToolCalls
  )

  return isWorking ? 'preparing' : 'needs_input'
}

export const getStatusMeta = (status) => DATASET_STATUS[status] || DATASET_STATUS.preparing

export const pluralize = (count, singular, plural = `${singular}s`) => (
  `${Number(count || 0).toLocaleString()} ${Number(count) === 1 ? singular : plural}`
)

export const resourceLabel = (name, count = 2) => {
  if (RESOURCE_LABELS[name]) {
    return RESOURCE_LABELS[name][Number(count) === 1 ? 0 : 1]
  }
  return String(name || '')
    .split('-')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export const resourceCountLabel = (name, count) => (
  `${Number(count || 0).toLocaleString()} ${resourceLabel(name, count)}`
)

export const naturalList = (items) => {
  if (items.length === 0) return ''
  if (items.length === 1) return items[0]
  if (items.length === 2) return `${items[0]} and ${items[1]}`
  return `${items.slice(0, -1).join(', ')}, and ${items.at(-1)}`
}
