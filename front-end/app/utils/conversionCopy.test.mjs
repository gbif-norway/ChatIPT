import test from 'node:test'
import assert from 'node:assert/strict'
import { choiceExamples, choicePresentation, helpTermParts } from './conversionCopy.mjs'
import { automaticSummary, makeSelector } from './conversionPlan.mjs'

const names = { id: 'agent-names', title: 'Old saved title', reason: 'Old saved explanation', count: 901, names: 107,
  default: 'shared', options: [{ value: 'shared', label: 'Old link label', assertion: false }, { value: 'text', label: 'Old text label', assertion: false }] }

test('existing plans get concise explanations without changing choices or the saved plan', () => {
  const state = { plan: { id: 'saved-plan', automatic_choices: [names], warnings: [{ id: names.id }] }, decisions: {} }
  const before = JSON.stringify(state)
  const display = choicePresentation(names)
  assert.deepEqual(display.options.map(({ value, assertion }) => ({ value, assertion })), names.options.map(({ value, assertion }) => ({ value, assertion })))
  const line = automaticSummary(state, makeSelector(state, {})).glance[0]
  assert.equal(line.title, 'Names without identifiers')
  assert.match(line.text, /901 mentions of 107 names/)
  assert.ok(!line.text.includes('Old link label'))
  assert.equal(JSON.stringify(state), before)
  assert.match(choicePresentation({ ...names, count: 1, names: 1 }).reason, /1 mention of 1 name has/)
})

test('a name example uses a real repeated name from the same archive, without changing source text', () => {
  const repeated = (id, name, count) => ({ id, source_value: name, count })
  const state = { plan: { automatic_choices: [
    repeated('agent-share:a', 'Ola <Nordmann>', 3), repeated('agent-share:b', 'Kari Nordmann', 1),
    repeated('taxon-occurrence:1:agent-share:c', 'Other archive name', 100),
  ] } }
  assert.deepEqual(choiceExamples(state, names), ['“Ola <Nordmann>” appears 3 times. Those mentions can link to one person or organization, or stay as unlinked text.'])
  assert.match(choiceExamples(state, { ...names, id: 'taxon-occurrence:1:agent-names' })[0], /Other archive name/)
  assert.deepEqual(choiceExamples({ plan: {} }, names), [])
})

test('column examples use only the uploaded samples, without inventing value/unit pairs', () => {
  const item = { id: 'column:0:7', term: 'http://rs.tdwg.org/dwc/terms/sampleSizeValue', default: 'derive' }
  const state = { plan: { columns: [
    { ...item, samples: ['58', '40', '58', '', '45', '90'] },
    { id: 'column:0:8', samples: ['minutes'] },
  ] } }
  assert.deepEqual(choiceExamples(state, item), ['“58” · “40” · “45”'])
  assert.ok(!choiceExamples(state, item)[0].includes('minutes'))
  assert.deepEqual(choiceExamples(state, { id: 'missing' }), [])
  assert.match(choicePresentation(item).reason, /If either is missing/)
})

test('paired sample size units stay editable without repeating the explanation, and overrides remain visible', () => {
  const column = (id, field) => ({ id, table: 0, term: `http://rs.tdwg.org/dwc/terms/${field}`, default: 'derive', nonempty: 3,
    glance: true, options: [{ value: 'derive', label: 'Keep' }, { value: 'preserve', label: 'Leave out' }] })
  const value = column('column:0:1', 'sampleSizeValue'), unit = column('column:0:2', 'sampleSizeUnit')
  const state = { plan: { columns: [value, unit], automatic_choices: [value, unit] }, decisions: {} }
  const summary = automaticSummary(state, makeSelector(state, {}))
  assert.deepEqual(summary.glance.map(line => line.id), [value.id])
  assert.ok(summary.silent.some(line => line.id === unit.id))
  const changed = { ...state, decisions: { [unit.id]: 'preserve' } }
  assert.ok(automaticSummary(changed, makeSelector(changed, changed.decisions)).glance.some(line => line.id === unit.id))
})

test('event wording retains the missing-category scope and survey-description consequence', () => {
  const reason = 'Some categories are missing. The survey descriptions (Humboldt) can only be converted for survey events; otherwise they are left out of the Data Package.'
  const item = { id: 'event-category', reason }
  const display = choicePresentation(item)
  assert.match(display.reason, /Only events without a category/)
  assert.match(display.reason, /otherwise they stay only in your original files/)
  assert.equal(display.details, reason)
  assert.ok(!choicePresentation({ ...item, reason: 'Some categories are missing.' }).reason.includes('Survey descriptions'))
})

test('help matches whole words and their plurals, beyond any filename prefix', () => {
  assert.deepEqual(helpTermParts('specimen.csv: do these identify physical specimens?', 'specimen'),
    ['specimen.csv: do these identify physical ', 'specimens', '?'])
  assert.deepEqual(helpTermParts('What is an event?', 'events'), ['What is an ', 'event', '?'])
  assert.equal(helpTermParts('specimen.csv: is this a sample?', 'specimen'), null)
  assert.equal(helpTermParts('eventual findings', 'events'), null)
})

test('event examples stay within a nested plan and follow current field choices', () => {
  const column = (id, value) => ({ id, table: 1, nonempty: 2, default: 'event.minimumDepthInMeters',
    term: 'http://rs.tdwg.org/dwc/terms/minimumDepthInMeters', samples: [value] })
  const nestedId = 'taxon-occurrence:1:column:0:1'
  const state = { plan: { columns: [column('column:1:1', '999'), column(nestedId, '28')] } }
  const item = { id: 'taxon-occurrence:1:occurrence-events:0', table: 1 }
  assert.deepEqual(choiceExamples(state, item), ['minimum depth in meters: “28”'])
  assert.deepEqual(choiceExamples({ ...state, decisions: { [nestedId]: 'preserve' } }, item), [])
})

test('sample-size summaries never borrow another nested plan’s unit or value', () => {
  const column = (id, field) => ({ id, table: 1, term: `http://rs.tdwg.org/dwc/terms/${field}`, default: 'derive', nonempty: 3,
    glance: true, options: [{ value: 'derive', label: 'Keep' }] })
  const outerValue = column('column:1:1', 'sampleSizeValue')
  const nestedUnit = column('taxon-occurrence:1:column:0:2', 'sampleSizeUnit')
  const state = { plan: { columns: [outerValue, nestedUnit], automatic_choices: [outerValue, nestedUnit] }, decisions: {} }
  const summary = automaticSummary(state, makeSelector(state, {}))
  assert.equal(summary.glance.find(line => line.id === outerValue.id).title, 'Sample sizes')
  assert.ok(summary.glance.some(line => line.id === nestedUnit.id))
})
