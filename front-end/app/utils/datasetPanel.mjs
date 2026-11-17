// Pure helpers for the dataset page's right-hand panel: what is happening now, the state of each
// package and the uploaded files. Wording is short and plain; longer explanations go in tooltips.

// The stage the chat is in: the task of the latest visible agent.
export const currentStageName = (dataset) => {
  const agents = Array.isArray(dataset?.visible_agent_set) ? dataset.visible_agent_set : []
  return agents.at(-1)?.task?.name || ''
}

// status: getDatasetStatus() ('preparing', 'paused', 'needs_input', 'failed', 'ready', 'published').
export const panelStatus = (status, stageName = '') => {
  const stage = stageName || 'Getting started'
  switch (status) {
    case 'published':
      return { title: 'Published to the GBIF sandbox', detail: '', tone: 'success' }
    case 'ready':
      return { title: 'Ready to publish', detail: 'Check the packages, then approve publication in the chat.', tone: 'success' }
    case 'needs_input':
      return { title: stage, detail: 'Waiting for your reply in the chat.', tone: 'warning' }
    case 'paused':
      return { title: stage, detail: 'Paused until GBIF is working again.', tone: 'secondary' }
    case 'failed':
      return { title: stage, detail: 'Something went wrong. See the last message in the chat.', tone: 'danger' }
    default:
      return { title: stage, detail: 'ChatIPT is working. This can take some time.', tone: 'primary' }
  }
}

const countLabel = (count, singular, plural = `${singular}s`) => (
  `${Number(count || 0).toLocaleString('en')} ${Number(count) === 1 ? singular : plural}`
)

// The Data Package: still being written, written but not checked, or checked (valid or not).
export const dwcDpState = (dataset) => {
  if (!dataset?.dwc_dp_url) {
    return {
      label: 'Being built',
      tone: 'secondary',
      hint: 'ChatIPT is still writing the linked tables, so there is nothing to download yet.',
    }
  }
  const validation = dataset.dwc_dp_validation
  if (!validation || typeof validation.valid !== 'boolean') {
    return { label: 'Not checked yet', tone: 'secondary', hint: 'The Data Package checks have not run on this version yet.' }
  }
  if (validation.valid) {
    const warnings = Array.isArray(validation.warnings) ? validation.warnings.length : 0
    return {
      label: 'Valid',
      tone: 'success',
      hint: warnings
        ? `Passed the Data Package checks with ${countLabel(warnings, 'advisory warning')}.`
        : 'Passed the Data Package checks.',
    }
  }
  const errors = Array.isArray(validation.errors) ? validation.errors.length : 0
  return {
    label: 'Has errors',
    tone: 'danger',
    hint: `${errors ? `The Data Package checks found ${countLabel(errors, 'problem')}.` : 'The Data Package checks failed.'} The chat explains what needs fixing.`,
  }
}

// The GBIF validator's verdict on the current archive, from dataset.dwca_summary.validation.
export const dwcaCheck = (validation) => {
  switch (validation?.state) {
    case 'passed':
      return { label: 'GBIF can index it', tone: 'success', hint: 'The GBIF validator checked this archive and can index it.' }
    case 'checking':
      return { label: 'GBIF is checking it', tone: 'info', hint: 'The GBIF validator is checking this archive. This usually takes a few minutes.' }
    case 'failed':
      return { label: 'GBIF found problems', tone: 'danger', hint: 'The GBIF validator could not index this archive. The chat explains what needs fixing.' }
    case 'not_applicable':
      return {
        label: 'Nothing for GBIF to check',
        tone: 'secondary',
        hint: validation.message || 'The GBIF validator only checks archives with occurrence records.',
      }
    default:
      return { label: 'Not checked by GBIF yet', tone: 'secondary', hint: 'ChatIPT sends the archive to the GBIF validator before publishing.' }
  }
}

// 'pdf', 'tree' or 'table', from the upload's type label or, failing that, its name.
export const fileKind = (file) => {
  const type = String(file?.file_type || '').toLowerCase()
  const name = String(file?.filename || '').toLowerCase()
  if (type.includes('pdf') || name.endsWith('.pdf')) return 'pdf'
  if (type.includes('tree') || /\.(newick|nwk|nex|nexus|tre|tree)$/.test(name)) return 'tree'
  return 'table'
}

export const FILE_ICONS = {
  pdf: 'bi-file-earmark-pdf',
  tree: 'bi-diagram-2',
  table: 'bi-file-earmark-spreadsheet',
}

// One archive table as a line: "Event · core · 12 rows". Rows are left out when GBIF has not counted them.
export const archiveTableLine = (table) => [
  table?.label || 'Table',
  table?.kind === 'core' ? 'core' : null,
  Number.isInteger(table?.rows) ? countLabel(table.rows, 'row') : null,
].filter(Boolean).join(' · ')
