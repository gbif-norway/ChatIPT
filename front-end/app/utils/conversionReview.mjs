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

export function pendingAdvice(state) {
  const reviewed = new Set([...(state?.advice_reviewed || []), ...(state?.suggestions || []).map(item => item.id)])
  return unresolvedIssues(state).filter(issue => !reviewed.has(issue.id))
}

export function optionState(state, id, value) {
  return state?.option_status?.[id]?.[value] || { available: true, reasons: [] }
}

export function conflictsFor(state, id) {
  return (state?.conflicts || []).filter(conflict => (conflict.decision_ids || []).includes(id))
}
