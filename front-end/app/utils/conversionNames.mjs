// Pure helpers for the conversion's scientific-name review.
import { formatName } from './taxonReview.mjs'

export const DECISION_LABELS = {
  parsed: 'Parsed name and authorship',
  col: 'Catalogue of Life name',
  alternative: 'Other Catalogue of Life name',
  keep: 'Kept as supplied',
  empty: 'No name published',
  stem: 'Published without “sp.”',
}

export const GROUPS = {
  check: { title: 'Check against your data', options: { col: 'Use COL names', mine: 'Keep my names' } },
  unconfirmed: { title: 'Not confirmed by COL', options: { mine: 'Keep my names' } },
  spelling: { title: 'Spelling differs from COL', options: { col: 'Use COL spelling', mine: 'Keep my spelling' } },
  uncertain: { title: 'Uncertain to genus or family', options: { stem: 'Publish the name without “sp.”', keep: 'Keep as written' } },
  auto: { title: 'Accepted automatically', options: { col: 'Use COL name', parsed: 'Use split', keep: 'Keep as written' } },
}

const CONTEXT_RANKS = ['kingdom', 'phylum', 'class']
export const PAGE_SIZE = 100

export const classificationContext = (usage) =>
  CONTEXT_RANKS.map(rank => usage?.classification?.[rank]).filter(Boolean).join(' › ')

export const choiceGroups = (entry) => {
  const choices = entry?.col_choices || []
  const main = choices.find(choice => choice.decision === 'col') || null
  const alternatives = choices.filter(choice => choice.decision === 'alternative')
  const fresh = alternatives.filter(choice => !main || String(choice.usage.id) !== String(main.usage.id))
  return {
    main,
    sameName: fresh.filter(choice => choice.same_name && !choice.replaces),
    others: fresh.filter(choice => !(choice.same_name && !choice.replaces)),
  }
}

export const choiceLabel = (choice) => {
  const usage = choice?.usage
  if (!usage) return ''
  return [formatName(usage), usage.taxonRank, classificationContext(usage)].filter(Boolean).join(' · ')
}

export const replacementWarning = (choice, rows) => {
  if (!choice?.replaces && !choice?.authorship_differs) return ''
  const count = rows ? ` on ${rows.toLocaleString()} ${rows === 1 ? 'row' : 'rows'}` : ''
  if (!choice.replaces) {
    return `Catalogue of Life writes the authorship of “${choice.usage.scientificName}” as “${choice.usage.scientificNameAuthorship}”, `
      + `which is not the authorship in your data; it would replace yours${count}. It may be a different taxon with the same name.`
  }
  return `“${choice.usage.scientificName}” ${choice.replaces.text}${count}. Your text stays in verbatimIdentification.`
}

// A COL choice that replaces the user's name, or their authorship, waits for a second click.
export const needsConfirmation = (choice) => Boolean(choice?.replaces || choice?.authorship_differs)

export const decisionResult = (decision) => {
  if (!decision) return ''
  if (decision.decision === 'empty') return 'No name published'
  if (decision.decision === 'keep') return 'Your text as written'
  return `${formatName(decision)}${decision.taxonRank ? ` (${decision.taxonRank})` : ''}`
}

export const groupTitle = (group) => {
  if (!group) return ''
  if (group.kind === 'check') return GROUPS.check.title
  return GROUPS[group.kind]?.title || ''
}

export const groupSubtitle = (group) => {
  const signature = group?.signature
  if (group?.kind !== 'check' || !signature) return ''
  if (['kingdom', 'phylum', 'class'].includes(signature.code)) {
    const count = group.labels || 0
    const subject = count === 1 ? 'this name' : `these ${count.toLocaleString()} names`
    return `Your ${signature.code} says ${signature.yours}; COL places ${subject} in ${signature.col}.`
  }
  if (signature.code === 'name') return 'COL returned a different name for these. Keep yours unless you are sure.'
  if (signature.code === 'id') return 'Your scientificNameID or taxonID points to another name in Catalogue of Life.'
  if (signature.code === 'rank') return `Your rank says ${signature.yours}; COL has ${group.labels === 1 ? 'this name' : 'these names'} as ${signature.col}. It may be a different taxon with the same name.`
  if (signature.code === 'mixed') return `Rows with the same name give different ${signature.yours || 'classifications'}, so they may be different taxa. Keep yours unless you are sure.`
  return ''
}

export const groupOptions = (group) => (group?.options || []).map(option => ({
  decision: option.decision,
  label: GROUPS[group.kind]?.options?.[option.decision] || option.decision,
  eligible: option.eligible || 0,
}))

export const applyLabel = (count) => `Apply to ${count.toLocaleString()} ${count === 1 ? 'name' : 'names'}`

export const progress = (summary = {}) => {
  const groups = summary.groups || []
  const auto = groups.reduce((sum, group) => sum + (group.auto || 0), 0)
  const need = groups.reduce((sum, group) => sum + (group.undecided || 0), 0)
  const unchecked = summary.unchecked || 0
  const decided = summary.decided || 0
  const labels = summary.labels || 0
  return {
    auto,
    need,
    unchecked,
    decided,
    labels,
    percent: labels ? Math.min(100, Math.round((decided / labels) * 100)) : 0,
    text: `${auto.toLocaleString()} accepted automatically · ${need.toLocaleString()} need you${unchecked ? ` · ${unchecked.toLocaleString()} still being checked` : ''}`,
  }
}

export const rowStatus = (entry) => {
  const decision = entry?.decision
  if (!decision) return { kind: null, text: '' }
  // A held choice is converted as the user's own text until it is confirmed, so that is what the row shows.
  if (entry.decision_unconfirmed || entry.decision_held) return { kind: 'held', text: 'Your text as written (until you confirm)' }
  if (decision.by === 'user') return { kind: 'user', text: 'Changed by you' }
  if (decision.by?.startsWith('bulk:')) return { kind: 'bulk', text: 'Applied to the group' }
  if (decision.by?.startsWith('auto:')) return { kind: 'auto', text: 'Accepted automatically' }
  return { kind: null, text: '' }
}

export const reasonText = (entry) => entry?.reasons?.[0]?.text || ''

export const stemLabel = (entry) => {
  const stem = entry?.stem
  if (!stem?.scientificName) return ''
  return `Publish ${formatName(stem)}${stem.taxonRank ? ` (${stem.taxonRank})` : ''}`
}

// An undecided name the chosen group decision does not cover; nothing is flagged until a decision is chosen.
export const needsRowDecision = (entry, option) => Boolean(option) && !entry?.decision && !(entry?.eligible || []).includes(option)

// What applying the group decision would write for this name, shown before it is applied; '' when it would not apply.
export const previewResult = (entry, option) => {
  if (!entry || entry.decision || !(entry.eligible || []).includes(option)) return ''
  const parsed = entry.parsed || {}
  const usage = entry.match?.usage
  // A "sp." name in a check group is published as its stem when COL's name is chosen.
  if (option === 'stem' || (option === 'col' && entry.stem)) return formatName(entry.stem)
  if (option === 'col') return usage ? formatName(usage) : ''
  if (option === 'parsed' || (option === 'mine' && parsed.usable && parsed.lossless && !entry.qualifier)) return parsed.canonical || entry.label
  return entry.label
}

export const decisionBody = (planId, label, kind, usageId, { confirmCoarser = false } = {}) => ({
  action: 'names',
  plan_id: planId,
  name_decisions: {
    [label]: kind ? { decision: kind, ...(usageId ? { usage_id: usageId } : {}), ...(confirmCoarser ? { confirm_coarser: true } : {}) } : null,
  },
})

// The latest bulk decision of a group that can still be undone; earlier groups keep their own Undo.
export const groupBatch = (summary, groupId) => [...(summary?.batches || (summary?.last_batch ? [summary.last_batch] : []))]
  .reverse().find(batch => batch.group === groupId) || null

export const bulkBody = (planId, group, decision) => ({ action: 'names', plan_id: planId, bulk: { group, decision } })
export const undoBatchBody = (planId, id) => ({ action: 'names', plan_id: planId, undo_batch: id })
export const undoAutoBody = (planId, kind) => ({ action: 'names', plan_id: planId, undo_auto: kind })

export const groupQuery = ({ group, offset = 0, limit = PAGE_SIZE, q = '' } = {}) => {
  const params = new URLSearchParams({ names_view: 'group', names_group: group || '', names_offset: String(offset), names_limit: String(limit) })
  if (q) params.set('names_q', q)
  return params.toString()
}

export const pageCount = (total, limit = PAGE_SIZE) => Math.max(1, Math.ceil((total || 0) / limit))
export const isEditable = (state) => ['review', 'reviewing'].includes(state?.status)
export const isChecking = (nameReview) => Boolean(nameReview?.checking) || nameReview?.status === 'running'

export const checkMessage = (nameReview) => {
  if (!nameReview) return null
  const { status, error, summary = {} } = nameReview
  const checked = `${(summary.checked || 0).toLocaleString()} of ${(summary.labels || 0).toLocaleString()} names checked`
  if (isChecking(nameReview)) return { variant: 'info', text: `Checking names against Catalogue of Life… ${checked}.`, spinner: true }
  if (status === 'error') return { variant: 'warning', text: `${error || 'Name checks stopped.'} ${checked}. Conversion does not depend on them.` }
  if (status === 'incomplete' || status === 'pending') return { variant: 'warning', text: `${checked}. You can convert now, or check again to continue.` }
  return null
}

export const carriedMessage = (carried) => {
  const kept = carried?.decisions || 0
  const dropped = carried?.dropped || 0
  if (!kept && !dropped) return null
  const keptText = kept ? `${kept.toLocaleString()} earlier name ${kept === 1 ? 'decision was' : 'decisions were'} kept from your previous check.` : ''
  const droppedText = dropped ? `${dropped.toLocaleString()} earlier ${dropped === 1 ? 'name decision could' : 'name decisions could'} not be kept because the names in your data changed.` : ''
  return [keptText, droppedText].filter(Boolean).join(' ')
}

export const unconfirmedMessage = (summary) => {
  const count = summary?.unconfirmed || 0
  if (!count) return null
  return `${count.toLocaleString()} held ${count === 1 ? 'decision is' : 'decisions are'} kept as your own name until confirmed.`
}

export const parseNote = (entry) => {
  const parsed = entry?.parsed
  if (!parsed) return 'Not parsed yet'
  if (entry.qualifier) return `Not split: the qualifier “${entry.qualifier}” stays in the supplied text`
  if (!parsed.usable) return 'The parser could not read this as a scientific name'
  if (!parsed.lossless) return 'The parser would reformat the supplied text'
  return parsed.authorship ? 'Splits exactly into name and authorship' : 'No authorship found'
}

export const skippedMessage = (summary) => {
  const skipped = summary?.skipped_long
  if (!skipped?.labels) return null
  const names = skipped.labels === 1 ? '1 name is' : `${skipped.labels.toLocaleString()} names are`
  const rows = skipped.rows === 1 ? '1 row' : `${skipped.rows.toLocaleString()} rows`
  return `${names} longer than ${(summary.max_label_chars || 500).toLocaleString()} characters (${rows}). They are not checked and are converted as they are.`
}
