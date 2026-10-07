import test from 'node:test'
import assert from 'node:assert/strict'
import {
  DECISION_LABELS, GROUPS, applyLabel, bulkBody, carriedMessage, checkMessage, choiceGroups, choiceLabel, classificationContext, decisionBody,
  decisionResult, groupOptions, groupQuery, groupSubtitle, groupTitle, isChecking, isEditable, needsRowDecision, pageCount, parseNote, previewResult,
  groupBatch, needsConfirmation, progress, reasonText, replacementWarning, rowStatus, skippedMessage, stemLabel, undoAutoBody, undoBatchBody,
} from './conversionNames.mjs'

test('group order, titles and option labels follow the server groups', () => {
  assert.deepEqual(Object.keys(GROUPS), ['check', 'unconfirmed', 'spelling', 'uncertain', 'auto'])
  assert.equal(groupTitle({ kind: 'check' }), 'Check against your data')
  assert.equal(groupTitle({ kind: 'auto' }), 'Accepted automatically')
  assert.deepEqual(groupOptions({ kind: 'auto', options: [{ decision: 'col', eligible: 5 }, { decision: 'parsed', eligible: 3 }, { decision: 'keep', eligible: 1 }] }), [
    { decision: 'col', label: 'Use COL name', eligible: 5 },
    { decision: 'parsed', label: 'Use split', eligible: 3 },
    { decision: 'keep', label: 'Keep as written', eligible: 1 },
  ])
  assert.deepEqual(groupOptions({ kind: 'check', options: [{ decision: 'mine', eligible: 0 }] }), [{ decision: 'mine', label: 'Keep my names', eligible: 0 }])
  assert.equal(groupOptions({ kind: 'spelling', options: [{ decision: 'col', eligible: 2 }, { decision: 'mine', eligible: 1 }] })[1].label, 'Keep my spelling')
  assert.equal(groupOptions({ kind: 'uncertain', options: [{ decision: 'stem', eligible: 4 }, { decision: 'keep', eligible: 2 }] })[0].label, 'Publish the genus or family name')
  assert.equal(groupOptions({ kind: 'unconfirmed', options: [{ decision: 'mine', eligible: 3 }] })[0].eligible, 3)
  assert.equal(applyLabel(1), 'Apply to 1 name')
  assert.equal(applyLabel(4), 'Apply to 4 names')
})

test('check subtitles describe each conflict signature', () => {
  assert.equal(groupSubtitle({ kind: 'check', labels: 81, signature: { code: 'kingdom', yours: 'Animalia', col: 'Plantae' } }), 'Your kingdom says Animalia; COL places these 81 names in Plantae.')
  assert.equal(groupSubtitle({ kind: 'check', labels: 1, signature: { code: 'phylum', yours: 'Chordata', col: 'Arthropoda' } }), 'Your phylum says Chordata; COL places this name in Arthropoda.')
  assert.match(groupSubtitle({ kind: 'check', signature: { code: 'name' } }), /Keep yours unless you are sure/)
  assert.match(groupSubtitle({ kind: 'check', signature: { code: 'id' } }), /scientificNameID or taxonID/)
  assert.equal(groupSubtitle({ kind: 'auto', signature: { code: 'name' } }), '')
})

test('progress combines automatic, undecided and unchecked counts', () => {
  assert.deepEqual(progress({ labels: 20, decided: 16, unchecked: 2, groups: [{ auto: 8, undecided: 2 }, { auto: 3, undecided: 1 }] }), {
    auto: 11, need: 3, unchecked: 2, decided: 16, labels: 20, percent: 80,
    text: '11 accepted automatically · 3 need you · 2 still being checked',
  })
})

test('check messages, paging and editing helpers keep their expected states', () => {
  assert.equal(checkMessage(null), null)
  assert.equal(checkMessage({ status: 'complete' }), null)
  assert.equal(checkMessage({ status: 'running', summary: { checked: 1, labels: 4 } }).spinner, true)
  assert.match(checkMessage({ status: 'error', error: 'COL unavailable', summary: { checked: 1, labels: 4 } }).text, /COL unavailable/)
  assert.equal(isChecking({ status: 'running' }), true)
  assert.equal(isEditable({ status: 'review' }), true)
  assert.equal(isEditable({ status: 'complete' }), false)
  assert.equal(pageCount(250), 3)
  assert.equal(pageCount(0), 1)
  assert.match(parseNote({ parsed: { usable: true, lossless: true, authorship: 'L.' } }), /Splits exactly/)
  assert.match(parseNote({ parsed: { usable: false } }), /could not read/)
  assert.equal(skippedMessage({ skipped_long: { labels: 1, rows: 1 }, max_label_chars: 500 }), '1 name is longer than 500 characters (1 row). They are not checked and are converted as they are.')
  assert.equal(skippedMessage({ skipped_long: { labels: 0, rows: 0 } }), null)
  assert.equal(replacementWarning({ usage: { scientificName: 'Arthropoda' }, replaces: { text: 'replaces your genus with a phylum' } }, 2), '“Arthropoda” replaces your genus with a phylum on 2 rows. Your text stays in verbatimIdentification.')
})

test('request bodies and group paging use the server contract', () => {
  assert.deepEqual(bulkBody('p1', 'check:name', 'mine'), { action: 'names', plan_id: 'p1', bulk: { group: 'check:name', decision: 'mine' } })
  assert.deepEqual(undoBatchBody('p1', 'b2'), { action: 'names', plan_id: 'p1', undo_batch: 'b2' })
  assert.deepEqual(undoAutoBody('p1', 'uncertain'), { action: 'names', plan_id: 'p1', undo_auto: 'uncertain' })
  assert.deepEqual(decisionBody('p1', 'Aus bus', 'stem').name_decisions['Aus bus'], { decision: 'stem' })
  assert.deepEqual(decisionBody('p1', 'Aus bus', null).name_decisions['Aus bus'], null)
  assert.match(groupQuery({ group: 'check:name', offset: 100, q: 'Aus bus' }), /names_view=group/)
  assert.match(groupQuery({ group: 'check:name', offset: 100, q: 'Aus bus' }), /names_group=check%3Aname/)
  assert.match(groupQuery({ group: 'check:name', offset: 100, q: 'Aus bus' }), /names_q=Aus\+bus/)
})

test('row helpers label result, provenance, reasons and stem choices', () => {
  assert.equal(DECISION_LABELS.stem, 'Published as the genus or family')
  assert.equal(decisionResult({ decision: 'col', scientificName: 'Aus bus', scientificNameAuthorship: 'L.' }), 'Aus bus L.')
  assert.equal(decisionResult({ decision: 'stem', scientificName: 'Larus', scientificNameAuthorship: 'Linnaeus, 1758', taxonRank: 'genus' }),
    'Larus Linnaeus, 1758 (genus)')
  assert.equal(decisionResult({ decision: 'keep' }), 'Your text as written')
  assert.equal(decisionResult({ decision: 'empty' }), 'No name published')
  assert.deepEqual(rowStatus({ decision: { decision: 'keep', by: 'user' } }), { kind: 'user', text: 'Changed by you' })
  assert.deepEqual(rowStatus({ decision: { decision: 'col', by: 'bulk:check' } }), { kind: 'bulk', text: 'Applied to the group' })
  assert.deepEqual(rowStatus({ decision: { decision: 'stem', by: 'auto:uncertain' } }), { kind: 'auto', text: 'Accepted automatically' })
  assert.deepEqual(rowStatus({ decision: { decision: 'col', scientificName: 'Arthropoda' }, decision_held: true }),
    { kind: 'held', text: 'Your text as written (until you confirm)' })
  assert.deepEqual(rowStatus({}), { kind: null, text: '' })
  assert.equal(reasonText({ reasons: [{ text: 'Not found in COL' }] }), 'Not found in COL')
  assert.equal(stemLabel({ stem: { scientificName: 'Galium', taxonRank: 'genus' } }), 'Publish the genus Galium')
  assert.equal(needsRowDecision({ eligible: ['col'] }, 'col'), false)
  assert.equal(needsRowDecision({ eligible: ['keep'] }, 'col'), true)
  // Nothing is flagged before a group decision is chosen (check groups have no default).
  assert.equal(needsRowDecision({ eligible: [] }, ''), false)
  assert.equal(needsRowDecision({ eligible: [], decision: { decision: 'keep' } }, 'col'), false)
})

test('carried decisions mention dropped labels', () => {
  assert.equal(carriedMessage(undefined), null)
  assert.equal(carriedMessage({ decisions: 1, dropped: 0 }), '1 earlier name decision was kept from your previous check.')
  assert.equal(carriedMessage({ decisions: 0, dropped: 3 }), '3 earlier name decisions could not be kept because the names in your data changed.')
})

test('classification and COL choice helpers retain context for row options', () => {
  const choice = { decision: 'alternative', same_name: true, replaces: null, usage: {
    id: '7', scientificName: 'Calanus', scientificNameAuthorship: 'Leach, 1816', taxonRank: 'genus',
    classification: { kingdom: 'Animalia', phylum: 'Arthropoda', class: 'Copepoda' },
  } }
  assert.equal(classificationContext(choice.usage), 'Animalia › Arthropoda › Copepoda')
  assert.equal(choiceLabel(choice), 'Calanus Leach, 1816 · genus · Animalia › Arthropoda › Copepoda')
  assert.deepEqual(choiceGroups({ col_choices: [{ ...choice, decision: 'col' }, choice] }).sameName, [])
})

test('exported copy stays free of broad acceptance wording', () => {
  const exports = { GROUPS, DECISION_LABELS, groupTitle, groupSubtitle, groupOptions, applyLabel, progress, groupQuery, bulkBody,
    undoBatchBody, undoAutoBody, rowStatus, reasonText, stemLabel, needsRowDecision, carriedMessage, decisionBody }
  const text = JSON.stringify(exports)
  assert.doesNotMatch(text, /for all|all possible|accept all/i)
})

test('the row shows what the chosen group decision would write before it is applied', () => {
  const galium = { label: 'Galium boreale', kind: 'unconfirmed', eligible: ['mine'], parsed: { usable: true, lossless: true, canonical: 'Galium boreale' } }
  assert.equal(previewResult(galium, 'mine'), 'Galium boreale')
  assert.equal(previewResult({ ...galium, decision: { decision: 'keep' } }, 'mine'), '')
  assert.equal(previewResult(galium, 'col'), '')
  const trema = { label: 'Trema orientalis', kind: 'spelling', eligible: ['col', 'mine'], match: { usage: { scientificName: 'Trema orientale', scientificNameAuthorship: '(L.) Blume' } } }
  assert.equal(previewResult(trema, 'col'), 'Trema orientale (L.) Blume')
  assert.equal(previewResult(trema, 'mine'), 'Trema orientalis')
  const sapotaceae = { label: 'Sapotaceae sp', kind: 'check', qualifier: 'sp.', eligible: ['col', 'mine'], stem: { scientificName: 'Sapotaceae', taxonRank: 'family' },
    match: { usage: { scientificName: 'Sapotaceae', scientificNameAuthorship: 'Juss.' } } }
  assert.equal(previewResult(sapotaceae, 'col'), 'Sapotaceae')
  assert.equal(previewResult(sapotaceae, 'mine'), 'Sapotaceae sp')
})

test('class and mixed-classification conflicts get their own explanation', () => {
  assert.equal(groupSubtitle({ kind: 'check', labels: 1, signature: { code: 'class', yours: 'Copepoda', col: 'Insecta' } }),
    'Your class says Copepoda; COL places this name in Insecta.')
  assert.match(groupSubtitle({ kind: 'check', labels: 2, signature: { code: 'mixed', yours: 'kingdom, class', col: null } }),
    /^Rows with the same name give different kingdom, class/)
})

test('a rank conflict says which ranks disagree', () => {
  assert.equal(groupSubtitle({ kind: 'check', labels: 1, signature: { code: 'rank', yours: 'genus', col: 'order' } }),
    'Your rank says genus; COL has this name as order. It may be a different taxon with the same name.')
})

test('each group keeps the Undo of its own latest bulk decision', () => {
  const summary = { batches: [{ id: 'a', group: 'spelling', count: 4 }, { id: 'b', group: 'unconfirmed', count: 3 }, { id: 'c', group: 'spelling', count: 1 }] }
  assert.equal(groupBatch(summary, 'spelling').id, 'c')
  assert.equal(groupBatch(summary, 'unconfirmed').id, 'b')
  assert.equal(groupBatch(summary, 'auto'), null)
  assert.equal(groupBatch({ last_batch: { id: 'x', group: 'auto' } }, 'auto').id, 'x')
})

test('a COL name with another authorship than the user\'s asks before replacing it', () => {
  const jones = { decision: 'col', same_name: true, replaces: null, authorship_differs: true,
    usage: { scientificName: 'Aus bus', scientificNameAuthorship: 'Jones, 1900' } }
  assert.equal(needsConfirmation(jones), true)
  assert.equal(needsConfirmation({ ...jones, authorship_differs: false }), false)
  assert.match(replacementWarning(jones, 3), /writes the authorship of “Aus bus” as “Jones, 1900”.*on 3 rows/)
})
