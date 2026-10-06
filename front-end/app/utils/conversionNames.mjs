// Pure helpers for the conversion's scientific-name review.
import { formatName } from './taxonReview.mjs'

export const DECISION_LABELS = {
  parsed: 'Parsed name and authorship',
  col: 'Catalogue of Life name',
  alternative: 'Other Catalogue of Life name',
  keep: 'Kept as supplied',
  empty: 'No name published',
}

// The ranks shown to tell same-name COL usages (homonyms) apart.
const CONTEXT_RANKS = ['kingdom', 'phylum', 'class']

// "Animalia › Arthropoda › Copepoda" for a COL usage, or '' when the match carried no classification.
export const classificationContext = (usage) =>
  CONTEXT_RANKS.map(rank => usage?.classification?.[rank]).filter(Boolean).join(' › ')

// The server's COL choices for one name, grouped for display: COL's own pick, alternatives that are the user's own
// name (shown inline), and the rest (behind "More"). Each choice says what it would replace, if anything.
export const choiceGroups = (entry) => {
  const choices = entry?.col_choices || []
  const main = choices.find(choice => choice.decision === 'col') || null
  const alternatives = choices.filter(choice => choice.decision === 'alternative')
  // COL's own pick is shown on its own button, so an alternative with the same id is not repeated.
  const fresh = alternatives.filter(choice => !main || String(choice.usage.id) !== String(main.usage.id))
  return {
    main,
    sameName: fresh.filter(choice => choice.same_name && !choice.replaces),
    others: fresh.filter(choice => !(choice.same_name && !choice.replaces)),
  }
}

// One line naming a COL usage: name, authorship, rank and, when known, where it sits in the classification.
export const choiceLabel = (choice) => {
  const usage = choice?.usage
  if (!usage) return ''
  return [formatName(usage), usage.taxonRank, classificationContext(usage)].filter(Boolean).join(' · ')
}

// The confirmation shown before a COL name that is coarser than, or in another genus from, the user's name.
export const replacementWarning = (choice, rows) => {
  if (!choice?.replaces) return ''
  const count = rows ? ` on ${rows.toLocaleString()} ${rows === 1 ? 'row' : 'rows'}` : ''
  return `“${choice.usage.scientificName}” ${choice.replaces.text}${count}. Your text stays in verbatimIdentification.`
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

// confirmCoarser is the user's second click on a COL name that replaces theirs with a coarser or different taxon;
// the server refuses such a decision without it.
export const decisionBody = (planId, label, kind, usageId, { confirmCoarser = false } = {}) => ({
  action: 'names',
  plan_id: planId,
  name_decisions: {
    [label]: kind ? { decision: kind, ...(usageId ? { usage_id: usageId } : {}), ...(confirmCoarser ? { confirm_coarser: true } : {}) } : null,
  },
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
