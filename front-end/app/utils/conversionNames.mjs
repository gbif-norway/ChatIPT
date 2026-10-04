// Pure helpers for the conversion's scientific-name review.
import { formatName } from './taxonReview.mjs'

export const DECISION_LABELS = {
  parsed: 'Parsed name and authorship',
  col: 'Catalogue of Life name',
  alternative: 'Other Catalogue of Life name',
  keep: 'Kept as supplied',
  empty: 'scientificName left empty',
}

export const PAGE_SIZE = 100

// The name a decision writes to scientificName, with its authorship, for display.
export const decisionResult = (decision) => {
  if (!decision) return ''
  if (decision.decision === 'empty') return ''
  if (decision.decision === 'keep') return 'The supplied text'
  return formatName(decision)
}

export const isEditable = (state) => ['review', 'reviewing'].includes(state?.status)

export const isChecking = (nameReview) => Boolean(nameReview?.checking) || nameReview?.status === 'running'

// What to tell the user about the state of the checks; null when nothing needs saying.
export const checkMessage = (nameReview) => {
  if (!nameReview) return null
  const { status, error, summary = {} } = nameReview
  const progress = `${(summary.checked || 0).toLocaleString()} of ${(summary.labels || 0).toLocaleString()} names checked`
  if (isChecking(nameReview)) return { variant: 'info', text: `Checking names against Catalogue of Life… ${progress}.`, spinner: true }
  if (status === 'error') return { variant: 'warning', text: `${error || 'Name checks stopped.'} ${progress}. Conversion does not depend on them.` }
  if (status === 'incomplete' || status === 'pending') {
    return { variant: 'warning', text: `${progress}. You can convert now, or check again to continue.` }
  }
  return null
}

export const bulkActions = (nameReview) => {
  const summary = nameReview?.summary || {}
  return [
    { bulk: 'exact_col', count: summary.bulk_col || 0, label: 'exact COL matches',
      detail: 'names found exactly in Catalogue of Life, written as in your data and without a qualifier such as “sp.” or “cf.”' },
    { bulk: 'parsed', count: summary.bulk_parsed || 0, label: 'parsed splits',
      detail: 'names whose parts (name and authorship) rebuild your text exactly, so nothing is changed or lost' },
  ].filter((action) => action.count > 0)
}

export const decisionBody = (planId, label, kind, usageId) => ({
  action: 'names',
  plan_id: planId,
  name_decisions: { [label]: kind ? { decision: kind, ...(usageId ? { usage_id: usageId } : {}) } : null },
})

export const bulkBody = (planId, bulk) => ({ action: 'names', plan_id: planId, bulk })

export const pageQuery = ({ offset = 0, view = 'pending', limit = PAGE_SIZE } = {}) =>
  `names_offset=${offset}&names_limit=${limit}&names_view=${view === 'all' ? 'all' : 'pending'}`

export const pageCount = (total, limit = PAGE_SIZE) => Math.max(1, Math.ceil((total || 0) / limit))

// One line describing how the parser read a label, and whether using it would rewrite the supplied text.
export const parseNote = (entry) => {
  const parsed = entry?.parsed
  if (!parsed) return 'Not parsed yet'
  if (entry.qualifier) return `Not split: the qualifier “${entry.qualifier}” stays in the supplied text`
  if (!parsed.usable) return 'The parser could not read this as a scientific name'
  if (!parsed.lossless) return 'The parser would reformat the supplied text'
  return parsed.authorship ? 'Splits exactly into name and authorship' : 'No authorship found'
}

// Cells too long to be a name are never checked or listed; say how many so nothing is hidden silently.
export const skippedMessage = (summary) => {
  const skipped = summary?.skipped_long
  if (!skipped?.labels) return null
  const names = skipped.labels === 1 ? '1 name is' : `${skipped.labels.toLocaleString()} names are`
  const rows = skipped.rows === 1 ? '1 row' : `${skipped.rows.toLocaleString()} rows`
  return `${names} longer than ${(summary.max_label_chars || 500).toLocaleString()} characters (${rows}). They are not checked and are converted as they are.`
}
