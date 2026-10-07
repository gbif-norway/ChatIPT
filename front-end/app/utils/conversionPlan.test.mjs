import test from 'node:test'
import assert from 'node:assert/strict'
import {
  automaticSummary, columnDetails, conversionStep, conversionTitle, dedupeNotices, makeSelector, planDiagram, statusLine, stepStates,
  summariseColumns, summaryLines, targetTable, unmappedReason,
} from './conversionPlan.mjs'

const term = name => `http://rs.tdwg.org/dwc/terms/${name}`
const mapped = (id, table, name, target, extra = {}) => ({ id, table, term: term(name), default: target, review: false,
  options: [{ value: target, label: target }, { value: 'preserve', label: 'Keep' }], nonempty: 3, ...extra })
const plan = {
  tables: [{ name: 'occurrence.txt', core: true, rows: 120, columns: [] }, { name: 'measurement.txt', core: false, rows: 40, columns: [] }, { name: 'media.txt', core: false, rows: 9, columns: [] }],
  columns: [
    mapped('column:0:0', 0, 'scientificName', 'occurrence.scientificName'),
    mapped('column:0:1', 0, 'eventDate', 'event.eventDate'),
    mapped('column:0:2', 0, 'country', 'event.country'),
    { id: 'column:0:3', table: 0, term: term('basisOfRecord'), default: 'preserve', review: false, options: [{ value: 'preserve', label: 'Keep' }], unmapped: 'no-target' },
    { id: 'column:0:4', table: 0, term: term('dynamicProperties'), default: 'preserve', review: false, options: [{ value: 'preserve', label: 'Keep' }], unmapped: 'unsupported' },
    { id: 'column:0:5', table: 0, term: term('remarks'), default: 'preserve', review: false, options: [{ value: 'preserve', label: 'Keep' }] },
    mapped('column:0:6', 0, 'locality', 'event.locality', { review: true }),
    { id: 'column:1:0', table: 1, term: term('measurementType'), default: 'occurrence-assertion.type', review: false, options: [{ value: 'occurrence-assertion.type', label: 'Type' }, { value: 'preserve', label: 'Keep' }] },
    { id: 'column:2:0', table: 2, term: term('identifier'), default: 'preserve', review: false, options: [{ value: 'preserve', label: 'Keep' }] },
  ],
  automatic_choices: [
    { id: 'table:2', table: 2, default: 'preserve', title: 'media.txt: choose its meaning', options: [{ value: 'preserve' }] },
    { id: 'column:0:3', default: 'preserve' },
  ],
  issues: [{ id: 'table:1', table: 1, options: [{ value: 'occurrence-assertion' }, { value: 'preserve' }] }],
}
const state = { plan, status: 'review', decisions: {} }

test('the selector prefers saved choices, then automatic defaults, then the fallback', () => {
  const selected = makeSelector(state, { 'table:1': 'occurrence-assertion' })
  assert.equal(selected('table:1'), 'occurrence-assertion')
  assert.equal(selected('table:2'), 'preserve')
  assert.equal(selected('unknown', 'x'), 'x')
})

test('the selector prefers saved decisions, conditional defaults, automatic defaults, then fallback', () => {
  const conditional = { ...state, conditional_defaults: { conditional: { value: 'conditional-value' } },
    plan: { ...plan, automatic_choices: [...plan.automatic_choices, { id: 'conditional', default: 'automatic-value' }, { id: 'automatic', default: 'automatic-value' }] } }
  const selected = makeSelector(conditional, { conditional: 'saved-value' })
  assert.equal(selected('conditional'), 'saved-value')
  assert.equal(makeSelector(conditional, {})('conditional'), 'conditional-value')
  assert.equal(makeSelector(conditional, {})('automatic'), 'automatic-value')
  assert.equal(makeSelector(conditional, {})('unknown', 'fallback'), 'fallback')
})

const summaryState = decisions => {
  const options = (values) => values.map(([value, label]) => ({ value, label }))
  const recordedBy = { id: 'column:0:0', table: 0, term: term('recordedBy'), kind: 'column-mapping', family: 'agent-role',
    title: 'Who are the people in recordedBy?', default: 'occurrence.recordedBy', nonempty: 2725, glance: true,
    reason: 'Rows are museum specimens.', options: options([['material.collectedBy', 'Who collected the specimen'], ['occurrence.recordedBy', 'Who saw or recorded the organism']]) }
  const typeStatus = { id: 'column:0:1', table: 0, term: term('typeStatus'), family: 'type-status', title: 'Type status', default: 'identification.typeStatus',
    nonempty: 12, glance: true, reason: 'Type status describes an identification.', options: options([['identification.typeStatus', 'Identification']]) }
  const material = { id: 'material:0', title: 'Create specimen records?', options: options([['per_row', 'Yes'], ['preserve', 'No']]) }
  const keptColumn = { id: 'column:0:2', table: 0, term: term('catalogNumber'), follows: 'material:0', default: 'material.catalogNumber', nonempty: 5 }
  const institution = { id: 'column:0:3', table: 0, term: term('institutionCode'), follows: 'material:0', default: 'material.institutionCode', nonempty: 5 }
  const state = { decisions, conditional_defaults: { 'column:0:0': { value: 'material.collectedBy', reason: 'Each row has its own specimen.' } }, plan: {
    tables: [{ core: true }, { core: false }, { core: false }], automatic_choices: [
      recordedBy, typeStatus,
      { id: 'agent-names', title: 'Link people', family: 'agent-role', default: 'shared', kind: 'agent-identity', options: options([['shared', 'Link'], ['text', 'Keep as text']]), reason: 'Names can be linked.' },
      { id: 'table:2', title: 'Keep media table?', table: 2, default: 'preserve', kind: 'extension-role', options: options([['preserve', 'Keep'], ['media-event', 'Media']]), reason: 'This table is unrecognised.' },
      { id: 'column:2:0', table: 2, term: term('identifier'), default: 'media.identifier', nonempty: 4, options: options([['media.identifier', 'Identifier']]) },
      { id: 'dependent:0', title: 'Dependent choice', table: 0, source_column: 5, default: 'yes', options: options([['yes', 'Yes']]) },
      { id: 'agent-share:1', title: 'Share agent?', default: 'shared', options: options([['shared', 'Shared']]) },
      { id: 'column:0:4', table: 0, term: term('empty'), default: 'preserve', nonempty: 0, options: options([['preserve', 'Keep']]) },
    ], issues: [material], columns: [keptColumn, institution], warnings: [{ id: 'agent-names', title: 'People can be linked', reason: 'Names found.' }],
    glossary: { families: { 'agent-role': 'People' }, targets: {
      'material.collectedBy': { decided: 'saved as who collected the specimen, on each specimen record', field_label: 'collector' },
      'material.catalogNumber': { field_label: 'catalogue number' }, 'material.institutionCode': { field_label: 'institution' },
    }, specimen_details: { stored: '{count} specimen details stored', stored_why: 'You chose to create specimen records.',
      kept: '{count} specimen details kept', kept_why: 'You chose not to create specimen records.',
      pending: '{count} specimen details pending', pending_why: 'Your choice is still open.' } },
  } }
  return state
}

test('automatic summary groups glance choices, hides retained or empty entries, and describes specimen details', () => {
  const state = summaryState({ 'agent-names': 'text', 'column:0:5': 'preserve', 'material:0': 'per_row' })
  const selected = makeSelector(state, state.decisions)
  const summary = automaticSummary(state, selected)
  assert.deepEqual(summary.glance.map(line => line.id), ['column:0:0', 'agent-names', 'column:0:1'])
  assert.equal(summary.glance[0].title, 'recordedBy → saved as who collected the specimen, on each specimen record')
  assert.equal(summary.glance[0].text, 'Each row has its own specimen.')
  assert.equal(summary.glance[1].changed, true)
  assert.equal(summary.glance[1].text, 'You chose: Keep as text.')
  assert.equal(summary.specimen[0].state, 'stored')
  assert.equal(summary.specimen[0].title, '2 specimen details stored')
  assert.deepEqual(summary.specimen[0].names, ['catalogue number', 'institution'])
  assert.ok(!summary.ids.has('column:2:0'))
  assert.ok(!summary.ids.has('agent-share:1'))
  assert.ok(!summary.ids.has('dependent:0'))
  assert.ok(!summary.ids.has('column:0:4'))
  assert.ok(summary.ids.has('agent-names'))
  assert.ok(summary.silent.some(line => line.id === 'table:2'))
  assert.ok(!summary.silent.some(line => line.id === 'column:2:0'))

  const kept = automaticSummary(summaryState({ 'material:0': 'preserve' }), makeSelector(summaryState({ 'material:0': 'preserve' }), { 'material:0': 'preserve' }))
  assert.equal(kept.specimen[0].state, 'kept')
  const pendingState = summaryState({})
  pendingState.plan.automatic_choices = pendingState.plan.automatic_choices.filter(item => item.id !== 'column:0:0')
  const pending = automaticSummary(pendingState, makeSelector(pendingState, {}))
  assert.equal(pending.specimen[0].state, 'pending')

  // While the specimen question is open, a default that follows it says what each answer does.
  const waitingState = summaryState({})
  waitingState.unresolved = ['material:0']
  waitingState.conditional_defaults = {}
  waitingState.plan.automatic_choices[0].default_when = [
    { value: 'material.collectedBy', when: [{ type: 'decision_in', id: 'material:0', values: ['per_row'] }] }]
  waitingState.plan.glossary.targets['occurrence.recordedBy'] = { decided: 'saved as who saw or recorded the organism, on each observation record' }
  const waiting = automaticSummary(waitingState, makeSelector(waitingState, {})).glance.find(line => line.id === 'column:0:0')
  assert.equal(waiting.title, 'recordedBy → waiting for your answer')
  assert.equal(waiting.text, 'Depends on your answer to “Create specimen records?”: saved as who collected the specimen, on each specimen record if yes; otherwise saved as who saw or recorded the organism, on each observation record.')
})

test('columns are summarised by how they are handled, using column.unmapped when present', () => {
  const summary = summariseColumns(state, makeSelector(state, { 'table:1': 'occurrence-assertion' }))
  assert.equal(summary.mapped, 4)
  assert.equal(summary.review, 1)
  assert.deepEqual(summary.groups['no-target'].names, ['basisOfRecord'])
  assert.deepEqual(summary.groups.unsupported.names, ['dynamicProperties'])
  assert.deepEqual(summary.groups['no-mapping'].names, ['remarks'])
  assert.deepEqual(summary.retainedTables.map(item => item.name), ['media.txt'])
  assert.deepEqual(summaryLines(summary), [
    '4 columns mapped automatically. 1 more column is covered by the choices below.',
    '1 column has no Darwin Core Data Package field and stays in your original files: basisOfRecord.',
    "1 column can't be mapped by ChatIPT yet and stays in your original files: dynamicProperties.",
    '1 column has no mapping and stays in your original files: remarks.',
    '1 additional table kept in your original files: media.txt.',
  ])
})

test('long name lists are shortened and plurals agree', () => {
  const columns = Array.from({ length: 8 }, (_, index) => ({ id: `c${index}`, table: 0, term: term(`field${index}`), default: 'preserve', unmapped: 'no-target', options: [] }))
  const big = { plan: { tables: [{ name: 't', core: true, rows: 1 }], columns, automatic_choices: [] } }
  const [line] = summaryLines(summariseColumns(big, makeSelector(big, {})))
  assert.equal(line, '8 columns have no Darwin Core Data Package field and stay in your original files: field0, field1, field2, field3, field4, field5, and 2 more.')
})

test('summaries are empty without a plan, and unmapped falls back gracefully', () => {
  assert.deepEqual(summaryLines(summariseColumns({}, makeSelector({}, {}))), [])
  assert.equal(unmappedReason({ options: [{ value: 'preserve' }] }), 'no-mapping')
  assert.equal(unmappedReason({ options: [{ value: 'a.b' }, { value: 'preserve' }] }), 'chosen')
  assert.equal(unmappedReason({ unmapped: 'no-target', options: [] }), 'no-target')
})

test('column details say where a column goes or why it stays', () => {
  const details = columnDetails(state, makeSelector(state, {}))
  assert.equal(details.find(item => item.name === 'scientificName').outcome, 'occurrence.scientificName')
  assert.match(details.find(item => item.name === 'basisOfRecord').outcome, /No Darwin Core Data Package field/)
  assert.match(details.find(item => item.name === 'identifier').outcome, /with its table/)
})

test('notices that repeat automatic keep-in-originals choices are dropped, as are exact duplicates', () => {
  const selected = makeSelector(state, {})
  const notices = [
    { id: 'table:2', title: 'media.txt', reason: 'r' },
    { id: 'column:0:3', title: 'basisOfRecord', reason: 'r' },
    { id: 'column:0:2', title: 'country', reason: 'Some values fail' },
    { id: 'column:0:2', title: 'country', reason: 'Some values fail' },
    { title: 'Free text', reason: 'once' }, { title: 'Free text', reason: 'once' },
  ]
  assert.deepEqual(dedupeNotices(state, notices, selected).map(notice => notice.title), ['country', 'Free text'])
  assert.deepEqual(dedupeNotices(state, notices, selected, new Set(['column:0:2'])).map(notice => notice.title), ['Free text'])
  // Once the user keeps the table's content mapped, its notice is not an automatic keep-in-originals any more.
  assert.equal(dedupeNotices(state, notices.slice(0, 1), makeSelector(state, { 'table:2': 'media' })).length, 1)
  assert.deepEqual(dedupeNotices(state, undefined, selected), [])
})

test('the agent-names notice shows while names are linked and goes once all are kept as text', () => {
  const plan = { ...state.plan, automatic_choices: [...(state.plan.automatic_choices || []), { id: 'agent-names', default: 'shared', options: [] }] }
  const agents = { ...state, plan }
  const notices = [{ id: 'agent-names', title: 'People and organizations named without identifiers', reason: 'Linked 6 mentions of 2 names' }]
  assert.equal(dedupeNotices(agents, notices, makeSelector(agents, {})).length, 1)
  assert.deepEqual(dedupeNotices(agents, notices, makeSelector(agents, { 'agent-names': 'text' })), [])
  // After conversion the notice is read from the report; the saved decision still decides.
  assert.deepEqual(dedupeNotices({ ...agents, status: 'complete' }, notices, makeSelector(agents, { 'agent-names': 'text' })), [])
})

test('steps follow the conversion status', () => {
  assert.equal(conversionStep(null).index, 1)
  assert.equal(conversionStep({ status: 'queued' }).index, 1)
  assert.equal(conversionStep({ status: 'review' }).index, 2)
  assert.equal(conversionStep({ status: 'reviewing' }).index, 2)
  assert.equal(conversionStep({ status: 'converting' }).index, 3)
  assert.deepEqual(conversionStep({ status: 'failed', plan: { id: 'p' } }), { index: 3, error: true })
  assert.deepEqual(conversionStep({ status: 'failed' }), { index: 1, error: true })
  assert.deepEqual(conversionStep({ status: 'blocked' }), { index: 1, error: true })
  assert.deepEqual(stepStates({ status: 'review' }).map(step => `${step.label}:${step.status}`),
    ['Upload:done', 'Inspect:done', 'Review:current', 'Convert:upcoming', 'Download:upcoming'])
  assert.deepEqual(stepStates({ status: 'complete' }).map(step => step.status), ['done', 'done', 'done', 'done', 'current'])
  assert.equal(stepStates({ status: 'blocked' })[1].status, 'error')
})

test('the status line says what is left to do', () => {
  assert.equal(statusLine(state, 2), '2 choices need your input. Your answers are saved as you go.')
  assert.equal(statusLine(state, 1), '1 choice needs your input. Your answers are saved as you go.')
  assert.equal(statusLine(state, 0, 2), '2 AI choices to check before you can convert.')
  assert.equal(statusLine(state, 0), 'Everything is set; you can convert. You can still adjust any choice.')
  assert.match(statusLine({ status: 'reviewing' }, 3), /AI reviewer is checking/)
})

test('the title falls back to the uploaded file name', () => {
  assert.equal(conversionTitle({ title: ' Birds of Fynbos ' }, null), 'Birds of Fynbos')
  assert.equal(conversionTitle({ title: '', user_files: [{ filename: 'archive.zip' }] }, null), 'archive.zip')
  assert.equal(conversionTitle({ title: '' }, { plan: { uploads: [{ name: 'dwca.zip' }] } }), 'dwca.zip')
  assert.equal(conversionTitle(null, null), 'Darwin Core Archive conversion')
})

test('target tables come from "table.field" values only', () => {
  assert.equal(targetTable('occurrence.scientificName'), 'occurrence')
  assert.equal(targetTable('occurrence-assertion.type'), 'occurrence-assertion')
  for (const value of ['preserve', 'join', 'parent-link', 'declared assertion subject → type', '', undefined]) assert.equal(targetTable(value), null)
})

test('the plan diagram links source tables to target tables from column targets and table roles', () => {
  const diagram = planDiagram(state, makeSelector(state, { 'table:1': 'occurrence-assertion' }))
  assert.deepEqual(diagram.sources.map(source => [source.name, source.rows, source.retained]), [['occurrence.txt', 120, false], ['measurement.txt', 40, false], ['media.txt', 9, true]])
  assert.deepEqual(diagram.targets.map(target => target.name), ['event', 'occurrence', 'occurrence-assertion'])
  assert.deepEqual(diagram.edges.map(edge => `${edge.source}>${edge.target}:${edge.columns}`).sort(),
    ['0>event:3', '0>occurrence:1', '1>occurrence-assertion:1'])
  assert.equal(diagram.targets[0].columns, 3)
})

test('a table role adds a link even when no column maps there, and a kept table has none', () => {
  const roles = { plan: { ...plan, columns: [], automatic_choices: [] } }
  assert.deepEqual(planDiagram(roles, makeSelector(roles, { 'table:1': 'media-event', 'table:2': 'humboldt-survey' })).edges.map(edge => `${edge.source}>${edge.target}`).sort(),
    ['1>event-media', '1>media', '2>survey'])
  const kept = planDiagram(roles, makeSelector(roles, { 'table:1': 'preserve' }))
  assert.equal(kept.edges.length, 0)
  assert.equal(kept.sources[1].retained, true)
  assert.deepEqual(planDiagram({}, makeSelector({}, {})).sources, [])
})

test('occurrence-event details patch existing events or add child events', () => {
  const base = { plan: { ...plan, columns: [], automatic_choices: [{ id: 'occurrence-events:1', default: 'patch' }] } }
  const patch = planDiagram(base, makeSelector(base, { 'table:1': 'occurrence' }))
  assert.deepEqual(patch.edges.filter(edge => edge.target === 'event').map(edge => [edge.source, Boolean(edge.childEvents)]), [[1, false]])
  const perRow = planDiagram(base, makeSelector(base, { 'table:1': 'occurrence', 'occurrence-events:1': 'per-row' }))
  assert.deepEqual(perRow.edges.filter(edge => edge.target === 'event').map(edge => [edge.source, Boolean(edge.childEvents)]), [[1, true]])
  const kept = planDiagram(base, makeSelector(base, { 'table:1': 'occurrence', 'occurrence-events:1': 'preserve' }))
  assert.equal(kept.edges.some(edge => edge.target === 'event'), false)
})

test('a scientific name left empty says its text goes to verbatimIdentification', () => {
  const named = { plan: { tables: [{ name: 't', core: true }], columns: [{ id: 'c', table: 0, term: term('scientificName'), default: 'preserve', verbatim_copy: 'occurrence.verbatimIdentification', options: [{ value: 'preserve' }] }], automatic_choices: [] } }
  assert.match(columnDetails(named, makeSelector(named, {}))[0].outcome, /goes to verbatimIdentification/)
})

test('an identificationQualifier is described as added after the name, and counts as written', () => {
  const qualified = { plan: { tables: [{ name: 't', core: true }], columns: [{ id: 'q', table: 0, term: term('identificationQualifier'), default: 'preserve',
    verbatim_copy: 'occurrence.verbatimIdentification', verbatim_role: 'qualifier', options: [{ value: 'preserve' }] }], automatic_choices: [] } }
  const selected = makeSelector(qualified, {})
  assert.match(columnDetails(qualified, selected)[0].outcome, /^Added after the name text in verbatimIdentification$/)
  assert.equal(summariseColumns(qualified, selected).mapped, 1)
})

test('event details on an occurrence table follow the occurrence-events choice in the diagram and summary', () => {
  const eventPlan = {
    tables: [{ name: 'event.txt', core: true, rows: 10 }, { name: 'occurrence.txt', core: false, rows: 40 }],
    columns: [
      mapped('column:0:0', 0, 'eventDate', 'event.eventDate'),
      mapped('column:1:0', 1, 'eventDate', 'event.eventDate'),
      mapped('column:1:1', 1, 'locality', 'event.locality'),
      mapped('column:1:2', 1, 'scientificName', 'occurrence.scientificName'),
    ],
    issues: [],
    automatic_choices: [{ id: 'occurrence-events:1', default: 'patch', options: [] }],
  }
  const run = decisions => {
    const eventState = { plan: eventPlan, decisions }
    const selected = makeSelector(eventState, decisions)
    return {
      diagram: planDiagram(eventState, selected), summary: summariseColumns(eventState, selected), details: columnDetails(eventState, selected),
    }
  }
  const edgesOf = diagram => diagram.edges.map(edge => `${edge.source}>${edge.target}:${edge.columns}`).sort()
  // The automatic default (patch) maps the columns into the existing events.
  const patch = run({})
  assert.deepEqual(edgesOf(patch.diagram), ['0>event:1', '1>event:2', '1>occurrence:1'])
  assert.equal(patch.summary.mapped, 4)
  // per-row creates child events from the same columns.
  const perRow = run({ 'occurrence-events:1': 'per-row' })
  assert.equal(perRow.diagram.edges.find(edge => edge.source === 1 && edge.target === 'event').childEvents, true)
  assert.equal(perRow.summary.mapped, 4)
  // preserve: no Event edge from the occurrence table, and the columns count as kept in originals.
  const kept = run({ 'occurrence-events:1': 'preserve' })
  assert.deepEqual(edgesOf(kept.diagram), ['0>event:1', '1>occurrence:1'])
  assert.equal(kept.summary.mapped, 2)
  assert.deepEqual(kept.summary.groups.chosen.names, ['eventDate', 'locality'])
  assert.deepEqual(summaryLines(kept.summary).at(-1), '2 columns are kept in your original files by choice: eventDate, locality.')
  assert.match(kept.details.find(item => item.id === 'column:1:1').outcome, /not copied/)
  assert.equal(kept.details.find(item => item.id === 'column:1:1').mapped, false)
  assert.equal(kept.details.find(item => item.id === 'column:1:2').mapped, true)
})

test('an unanswered occurrence-events question is not drawn or counted as mapped', () => {
  const asked = {
    plan: {
      tables: [{ name: 'event.txt', core: true, rows: 10 }, { name: 'occurrence.txt', core: false, rows: 40 }],
      columns: [mapped('column:1:0', 1, 'locality', 'event.locality')],
      issues: [{ id: 'occurrence-events:1', options: [] }], automatic_choices: [],
    }, decisions: {},
  }
  const selected = makeSelector(asked, {})
  assert.equal(planDiagram(asked, selected).edges.length, 0)
  const summary = summariseColumns(asked, selected)
  assert.deepEqual([summary.mapped, summary.review], [0, 1])
  assert.match(columnDetails(asked, selected)[0].outcome, /Waiting for your choice/)
})

test('a scientificName left empty still counts as written to verbatimIdentification', () => {
  const state = { plan: { tables: [{ name: 'occurrence.txt', core: true, rows: 1 }], automatic_choices: [], issues: [],
    columns: [{ id: 'column:0:0', table: 0, term: 'http://rs.tdwg.org/dwc/terms/scientificName', default: 'occurrence.scientificName',
      verbatim_copy: 'occurrence.verbatimIdentification', options: [{ value: 'occurrence.scientificName' }, { value: 'preserve' }] }] } }
  const selected = (id, fallback) => (id === 'column:0:0' ? 'preserve' : fallback)
  const summary = summariseColumns(state, selected)
  assert.equal(summary.mapped, 1)
  assert.deepEqual(summary.groups, {})
  assert.deepEqual(planDiagram(state, selected).targets.map(target => target.name), ['occurrence'])
})

test('a partial verbatim copy follows whether the file\'s own verbatimIdentification is converted', () => {
  const state = { plan: { tables: [{ name: 'occurrence.txt', core: true, rows: 1 }], automatic_choices: [], issues: [],
    columns: [{ id: 'column:0:0', table: 0, term: 'http://rs.tdwg.org/dwc/terms/scientificName', default: 'preserve', verbatim_source: 'column:0:1',
      verbatim_copy: 'occurrence.verbatimIdentification', options: [{ value: 'occurrence.scientificName' }, { value: 'preserve' }] },
    { id: 'column:0:1', table: 0, term: 'http://rs.tdwg.org/dwc/terms/verbatimIdentification', default: 'occurrence.verbatimIdentification',
      options: [{ value: 'occurrence.verbatimIdentification' }, { value: 'preserve' }] }] } }
  const converted = (id, fallback) => fallback
  assert.equal(summariseColumns(state, converted).mapped, 1)
  assert.match(columnDetails(state, converted)[0].outcome, /where your file leaves it empty/)
  const retained = (id, fallback) => (id === 'column:0:1' ? 'preserve' : fallback)
  assert.equal(summariseColumns(state, retained).mapped, 1)
  assert.doesNotMatch(columnDetails(state, retained)[0].outcome, /where your file leaves it empty/)
  const elsewhere = (id, fallback) => (id === 'column:0:1' ? 'identification.verbatimIdentification' : fallback)
  assert.equal(summariseColumns(state, elsewhere).mapped, 2)
  assert.doesNotMatch(columnDetails(state, elsewhere)[0].outcome, /where your file leaves it empty/)
})
