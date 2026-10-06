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
    { bulk: 'spelling', count: summary.bulk_spelling || 0, label: 'spelling corrections',
      detail: 'Catalogue of Life spellings of your names: a genus a letter or two apart or an epithet with another gender ending, '
        + 'at the same rank and in the kingdom (and class or family) your data gives',
      items: (summary.spelling_corrections || []).map(item => `${item.label} → ${item.to}`) },
  ].filter((action) => action.count > 0)
}

// Name decisions kept from the previous check when the archive was inspected again; null when none.
export const carriedMessage = (carried) => {
  const kept = carried?.decisions || 0
  const bulk = carried?.bulk_not_carried || 0
  if (!kept && !bulk) return null
  const decisions = kept === 1 ? '1 earlier name decision was' : `${kept.toLocaleString()} earlier name decisions were`
  const again = bulk ? ` ${bulk.toLocaleString()} accepted in bulk ${bulk === 1 ? 'is' : 'are'} offered again in bulk under the current checks.` : ''
  return `${kept ? `${decisions} kept from your previous check.` : 'No earlier name decision could be kept.'}${again}`
}

// Earlier COL choices that replace a name with a coarser or different taxon and were never confirmed; null when none.
export const unconfirmedMessage = (summary) => {
  const count = summary?.unconfirmed || 0
  if (!count) return null
  const choices = count === 1 ? '1 earlier choice needs' : `${count.toLocaleString()} earlier choices need`
  return `${choices} confirming: ${count === 1 ? 'it replaces a name' : 'they replace names'} with a coarser or different `
    + 'Catalogue of Life taxon. Until confirmed, your own name is kept. They are listed under “Needs review”.'
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
