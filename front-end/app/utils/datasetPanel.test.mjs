import test from 'node:test'
import assert from 'node:assert/strict'

import {
  archiveTableLine,
  currentStageName,
  dwcaCheck,
  dwcDpState,
  fileKind,
  panelStatus,
} from './datasetPanel.mjs'

test('the stage is the task of the latest visible agent', () => {
  assert.equal(currentStageName({
    visible_agent_set: [{ task: { name: 'Data structure exploration' } }, { task: { name: 'Data transformation' } }],
  }), 'Data transformation')
  assert.equal(currentStageName({ visible_agent_set: [] }), '')
  assert.equal(currentStageName(null), '')
})

test('the status names the stage while ChatIPT works or waits', () => {
  assert.deepEqual(panelStatus('preparing', 'Data transformation'), {
    title: 'Data transformation', detail: 'ChatIPT is working. This can take some time.', tone: 'primary',
  })
  assert.equal(panelStatus('needs_input', 'Data validation and refinement').detail, 'Waiting for your reply in the chat.')
  assert.equal(panelStatus('paused', '').title, 'Getting started')
  assert.equal(panelStatus('failed', 'X').tone, 'danger')
})

test('ready and published replace the stage with the outcome', () => {
  assert.equal(panelStatus('ready', 'Final Review & Publication').title, 'Ready to publish')
  assert.equal(panelStatus('published', 'Data maintenance').title, 'Published to the GBIF sandbox')
})

test('the Data Package is being built until it has a download', () => {
  assert.equal(dwcDpState({ dwc_dp_url: '' }).label, 'Being built')
  assert.equal(dwcDpState({ dwc_dp_url: 'https://x/dp.zip', dwc_dp_validation: null }).label, 'Not checked yet')
})

test('a checked Data Package is valid, with its warnings counted, or has errors', () => {
  const valid = dwcDpState({ dwc_dp_url: 'u', dwc_dp_validation: { valid: true, warnings: ['a', 'b'] } })
  assert.equal(valid.label, 'Valid')
  assert.equal(valid.hint, 'Passed the Data Package checks with 2 advisory warnings.')
  const invalid = dwcDpState({ dwc_dp_url: 'u', dwc_dp_validation: { valid: false, errors: ['a'] } })
  assert.equal(invalid.label, 'Has errors')
  assert.match(invalid.hint, /found 1 problem\./)
})

test('the archive check follows the GBIF validator state', () => {
  assert.equal(dwcaCheck({ state: 'passed' }).label, 'GBIF can index it')
  assert.equal(dwcaCheck({ state: 'checking' }).tone, 'info')
  assert.equal(dwcaCheck({ state: 'failed' }).tone, 'danger')
  assert.equal(dwcaCheck({ state: 'not_applicable', message: 'No occurrences.' }).hint, 'No occurrences.')
  assert.equal(dwcaCheck(undefined).label, 'Not checked by GBIF yet')
})

test('uploads are PDFs, trees or tables', () => {
  assert.equal(fileKind({ filename: 'paper.PDF' }), 'pdf')
  assert.equal(fileKind({ file_type: 'Phylogenetic tree', filename: 'x' }), 'tree')
  assert.equal(fileKind({ filename: 'tree.nwk' }), 'tree')
  assert.equal(fileKind({ filename: 'plots.xlsx', file_type: 'Tabular data' }), 'table')
})

test('archive tables read as one line, with rows only once GBIF counted them', () => {
  assert.equal(archiveTableLine({ label: 'Event', kind: 'core', rows: 1200 }), 'Event · core · 1,200 rows')
  assert.equal(archiveTableLine({ label: 'Occurrence', kind: 'extension', rows: 1 }), 'Occurrence · 1 row')
  assert.equal(archiveTableLine({ label: 'Measurement or fact', kind: 'extension', rows: null }), 'Measurement or fact')
})
