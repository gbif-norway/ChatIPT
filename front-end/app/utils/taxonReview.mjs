// Pure helpers for the taxon name review modal.

export const STATUS_LABELS = {
  exact: { label: 'Exact match', variant: 'success' },
  variant: { label: 'Close spelling', variant: 'warning' },
  higher_rank: { label: 'Higher rank only', variant: 'warning' },
  ambiguous: { label: 'Ambiguous', variant: 'warning' },
  none: { label: 'No match', variant: 'secondary' },
}

export const DECISION_LABELS = {
  accepted: 'Accepted',
  not_in_col: 'Correct name, not in COL',
  keep_original: 'Kept unchanged',
  pending: 'Needs review',
}

export const RANKS = [
  'kingdom', 'phylum', 'class', 'order', 'family', 'subfamily', 'tribe',
  'genus', 'subgenus', 'species', 'subspecies', 'variety', 'form',
]

export const statusInfo = (row) => STATUS_LABELS[row?.match?.status] || STATUS_LABELS.none

export const formatName = (usage) => {
  if (!usage?.scientificName) return ''
  return [usage.scientificName, usage.scientificNameAuthorship].filter(Boolean).join(' ')
}

export const isPending = (row) => row.decision === 'pending'

export const filterRows = (rows, { view = 'pending', query = '' } = {}) => {
  const needle = query.trim().toLowerCase()
  return rows.filter((row) => {
    if (view === 'pending' && !isPending(row)) return false
    if (view === 'reviewed' && isPending(row)) return false
    if (!needle) return true
    const haystack = [
      row.verbatim_label,
      row.context_key,
      row.query?.scientificName,
      row.match?.usage?.scientificName,
    ].filter(Boolean).join(' ').toLowerCase()
    return haystack.includes(needle)
  })
}

export const countByDecision = (rows) => rows.reduce((counts, row) => {
  counts[row.decision] = (counts[row.decision] || 0) + 1
  counts.records[row.decision] = (counts.records[row.decision] || 0) + (row.record_count || 0)
  return counts
}, { records: {} })

const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`

// The message sent to the assistant when the reviewer is done.
export const reviewSummaryMessage = (rows) => {
  const counts = countByDecision(rows)
  const parts = [
    counts.accepted && `${plural(counts.accepted, 'name')} accepted`,
    counts.not_in_col && `${plural(counts.not_in_col, 'name')} marked as correct but not in COL`,
    counts.keep_original && `${plural(counts.keep_original, 'name')} kept unchanged`,
  ].filter(Boolean)
  let message = 'I have finished reviewing the taxon names'
  message += parts.length ? `: ${parts.join(', ')}.` : '.'
  if (counts.pending) {
    message += ` ${plural(counts.pending, 'name')} (${plural(counts.records.pending, 'record')}) ` +
      `${counts.pending === 1 ? 'is' : 'are'} left unreviewed; leave ${counts.pending === 1 ? 'it' : 'them'} unchanged.`
  }
  return `${message} Please apply the reviewed names.`
}

// Best-effort split of "Genus epithet (Author, 1882)" into name, authorship and rank, used only to
// prefill the reviewer's form; the reviewer can correct every field.
export const splitScientificName = (text) => {
  const tokens = String(text || '').trim().split(/\s+/).filter(Boolean)
  if (!tokens.length) return { scientificName: '', scientificNameAuthorship: '', taxonRank: '' }
  const nameTokens = [tokens[0]]
  let index = 1
  while (index < tokens.length && /^[a-z×-][a-z-]*\.?$/.test(tokens[index]) && !/^(et|ex|in)$/.test(tokens[index])) {
    nameTokens.push(tokens[index])
    index += 1
  }
  const ranks = { 1: '', 2: 'species', 3: 'subspecies' }
  return {
    scientificName: nameTokens.join(' '),
    scientificNameAuthorship: tokens.slice(index).join(' '),
    taxonRank: ranks[nameTokens.length] ?? '',
  }
}
