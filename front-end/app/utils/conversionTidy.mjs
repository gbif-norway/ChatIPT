const hasValues = group => Array.isArray(group?.values) && group.values.length > 0

export function tidyGroups(state) {
  if (!state?.tidy?.enabled) return { tidied: [], suggestions: [] }
  const groups = (state.tidy.groups || []).filter(hasValues)
  return {
    tidied: groups.filter(group => group.tier === 'auto').sort((a, b) => (b.changed_rows || 0) - (a.changed_rows || 0)),
    suggestions: groups.filter(group => group.tier === 'suggest'),
  }
}

const visibleText = value => String(value ?? '').replace(/[\u0000-\u001f\u007f-\u009f]/g, char =>
  `\\x${char.charCodeAt(0).toString(16).toUpperCase().padStart(2, '0')}`)

export function valueChange(value, field) {
  const original = visibleText(value?.value)
  const fields = value?.fields || {}
  const target = visibleText(fields[field] ?? '')
  const change = target ? `${target}` : 'left empty'
  const extras = Object.entries(fields)
    .filter(([name, text]) => name !== field && text !== '')
    .map(([name, text]) => `${name}: ${visibleText(text)}`)
  const source = !target || extras.length ? `‘${original}’` : original
  return `${source} → ${change}${extras.length ? ` (${extras.join(', ')})` : ''}`
}

export function groupChange(group, id) {
  if (id != null) {
    const value = typeof id === 'object' ? id : (group?.values || []).find(item => item.id === id)
    if (!value) return {}
    // An explicit undo, so a suggestion applied with "Apply all" can still be undone one by one.
    return value.applied ? { [value.id]: 'undo' } : { [value.id]: 'apply' }
  }
  return group?.applied ? { [group.id]: 'undo' } : { [group.id]: null }
}

// "Apply all" for a suggestion group, including values not listed on the page; applied again, it returns to the default.
export function suggestionGroupChange(group, overrides = {}) {
  return overrides?.[group.id] === 'on' ? { [group.id]: null } : { [group.id]: 'apply' }
}

const count = value => Number.isFinite(Number(value)) ? Number(value) : 0
const plural = (number, singular, many = `${singular}s`) => `${number.toLocaleString('en-US')} ${number === 1 ? singular : many}`

export function tidySummaryLine(state) {
  if (!state?.tidy?.enabled) return ''
  const counts = state.tidy.counts || {}
  const groups = count(counts.tidied_groups)
  const rows = count(counts.tidied_rows)
  const suggestions = count(counts.suggestions)
  if (!groups && !rows && !suggestions) return ''
  const summary = groups
    ? `We tidied ${plural(groups, 'thing')} in your data${rows ? ` (${plural(rows, 'value')})` : ''}.`
    : ''
  const suggestionSummary = suggestions ? `${plural(suggestions, 'suggestion')} to check.` : ''
  return [summary, suggestionSummary].filter(Boolean).join(' ')
}
