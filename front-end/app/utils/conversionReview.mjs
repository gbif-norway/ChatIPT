export function unresolvedIssues(plan, decisions = {}) {
  const effective = { ...Object.fromEntries((plan?.automatic_choices || []).map(choice => [choice.id, choice.default])), ...decisions }
  const rowChoice = issue => issue.id.startsWith('taxon-occurrence:')
    ? `taxon-occurrence:${issue.table}:row:0:${issue.row - 1}` : `row:${issue.table}:${issue.row - 1}`
  return (plan?.issues || []).filter(issue => !effective[issue.id] &&
    !(issue.table !== undefined && (effective[`table:${issue.table}`] === 'preserve' ||
      (issue.row !== undefined && effective[rowChoice(issue)] === 'preserve'))))
}

export function pendingAdvice(state, decisions = {}) {
  const reviewed = new Set([...(state?.advice_reviewed || []), ...(state?.suggestions || []).map(item => item.id)])
  return unresolvedIssues(state?.plan, decisions).filter(issue => !reviewed.has(issue.id))
}
