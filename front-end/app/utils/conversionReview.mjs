// The server resolves choices (automatic defaults, grouped rows, option requirements)
// and returns `unresolved`; the interface only filters and labels.

export function unresolvedIssues(state) {
  const unresolved = new Set(state?.unresolved || [])
  return (state?.plan?.issues || []).filter(issue => unresolved.has(issue.id))
}

// Automatic choices and column mappings whose current value fails a requirement.
export function attentionItems(state) {
  const unresolved = new Set(state?.unresolved || [])
  const issues = new Set((state?.plan?.issues || []).map(issue => issue.id))
  return [...(state?.plan?.automatic_choices || []), ...(state?.plan?.columns || [])]
    .filter(item => unresolved.has(item.id) && !issues.has(item.id))
}

// AI review and conversation (docs/dwca-conversion/ai-review-and-chat.md §10–11).

const issueById = (state, id) => (state?.plan?.issues || []).find(issue => issue.id === id)
const optionLabel = (item, value) => item?.options?.find(option => option.value === value)?.label || value

export function recommendationFor(state, id) {
  return state?.review?.recommendations?.[id] || null
}

// A recommendation shown to the user: escalated with an option, still current, not yet what is chosen.
export function shownRecommendation(state, id) {
  const record = recommendationFor(state, id)
  if (!record?.option || record.outcome !== 'escalated' || record.current === false) return null
  return state?.decisions?.[id] === record.option ? null : record
}

// Choices the AI reviewer applied, with the reason it gave; each stays overridable.
export function aiDecidedItems(state) {
  return (state?.review?.applied || []).map(id => {
    const item = issueById(state, id)
    const value = state?.decisions?.[id]
    return {
      id, item, value, label: optionLabel(item, value), source: state?.decision_sources?.[id] || null,
      stale: Boolean(state?.review?.recommendations?.[id]?.stale_basis),
      conflict: (state?.review?.conflicted || []).includes(id),
    }
  }).filter(entry => entry.item)
}

// Proposals on the latest assistant message that still need the user's click.
export function openProposals(state) {
  const messages = state?.chat?.messages || []
  const latest = [...messages].reverse().find(message => message.role === 'assistant' && message.proposals?.length)
  if (!latest || !latest.current_plan) return []
  const open = new Set(state?.review?.escalated || [])
  return latest.proposals.filter(proposal => open.has(proposal.id)).map(proposal => {
    const item = issueById(state, proposal.id)
    return { ...proposal, message_id: latest.id, title: item?.title || proposal.id, label: optionLabel(item, proposal.value) }
  })
}

export function chatVisible(state) {
  return Boolean(state?.chat?.available && ((state?.review?.escalated || []).length || (state?.conflicts || []).length ||
    (state?.chat?.messages || []).length || state?.status === 'blocked'))
}

export function optionState(state, id, value) {
  return state?.option_status?.[id]?.[value] || { available: true, reasons: [] }
}

export function conflictsFor(state, id) {
  return (state?.conflicts || []).filter(conflict => (conflict.decision_ids || []).includes(id))
}
