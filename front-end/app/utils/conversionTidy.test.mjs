import test from 'node:test'
import assert from 'node:assert/strict'
import { groupChange, suggestionGroupChange, tidyGroups, tidySummaryLine, valueChange } from './conversionTidy.mjs'

const country = {
  id: 'tidy:570:countryCode:country-name', table_name: 'Occurrence', field: 'countryCode', tier: 'auto',
  applied: true, changed_rows: 12, conflict_rows: 0, values: [
    { id: 'norway', value: 'Norway', rows: 12, fields: { countryCode: 'NO', country: 'Norway' }, applied: true },
  ],
}
const sex = {
  id: 'tidy:570:sex:unknown', table_name: 'Occurrence', field: 'sex', tier: 'auto',
  applied: true, changed_rows: 63677, conflict_rows: 0, values: [
    { id: 'unknown-sex', value: 'Unknown', rows: 63677, fields: { sex: '' }, applied: true },
  ],
}
const lifeStage = {
  id: 'tidy:572:eventRemarks:life-stage', table_name: 'Occurrence', field: 'eventRemarks', tier: 'auto',
  applied: false, changed_rows: 1, conflict_rows: 0,
  values: [{ id: 'juv', value: 'juv.', rows: 1, fields: { eventRemarks: '', lifeStage: 'juvenile' }, applied: false }],
}
const pullus = {
  id: 'tidy:570:lifeStage:vocabulary', table_name: 'Occurrence', field: 'lifeStage', tier: 'auto',
  applied: true, changed_rows: 2, conflict_rows: 0,
  values: [{ id: 'pullus', value: 'Pullus', rows: 2, fields: { lifeStage: 'nestling' }, applied: true }],
}
const numeric = {
  id: 'tidy:568:measurement:decimal', table_name: 'Measurements', field: 'measurementValue', tier: 'suggest',
  applied: false, changed_rows: 0, values: [
    { id: 'decimal', value: '1,000', rows: 5, fields: { measurementValue: '1.000' }, applied: false, note: 'The column appears to use decimal commas.' },
  ],
}
const encoding = {
  id: 'tidy:570:locality:encoding', table_name: 'Occurrence', field: 'locality', tier: 'suggest',
  applied: true, changed_rows: 1, values: [
    { id: 'busingen', value: 'B\x99SINGEN', rows: 1, fields: { locality: 'BÖSINGEN' }, applied: true },
  ],
}

test('tidyGroups splits tiers, drops empty groups, and sorts automatic changes by rows', () => {
  const state = { tidy: { enabled: true, groups: [lifeStage, numeric, country, sex, pullus, { tier: 'auto', values: [] }, encoding] } }
  const result = tidyGroups(state)
  assert.deepEqual(result.tidied.map(group => group.id), [sex.id, country.id, pullus.id, lifeStage.id])
  assert.deepEqual(result.suggestions.map(group => group.id), [numeric.id, encoding.id])
  assert.deepEqual(tidyGroups({ tidy: { enabled: false, groups: [country] } }), { tidied: [], suggestions: [] })
  assert.deepEqual(tidyGroups({}), { tidied: [], suggestions: [] })
})

test('valueChange describes direct, empty, additional-field, moved, and damaged values', () => {
  assert.equal(valueChange({ value: 'Norway', fields: { countryCode: 'NO', country: 'Norway' } }, 'countryCode'), '‘Norway’ → NO (country: Norway)')
  assert.equal(valueChange({ value: 'Unknown', fields: { sex: '' } }, 'sex'), '‘Unknown’ → left empty')
  assert.equal(valueChange({ value: 'Pullus', fields: { lifeStage: 'nestling' } }, 'lifeStage'), 'Pullus → nestling')
  assert.equal(valueChange({ value: 'juv.', fields: { eventRemarks: '', lifeStage: 'juvenile' } }, 'eventRemarks'), '‘juv.’ → left empty (lifeStage: juvenile)')
  assert.equal(valueChange({ value: 'B\x99SINGEN', fields: { locality: 'BÖSINGEN' } }, 'locality'), 'B\\x99SINGEN → BÖSINGEN')
})

test('groupChange toggles whole-group defaults and individual suggestions', () => {
  assert.deepEqual(groupChange(country), { [country.id]: 'undo' })
  assert.deepEqual(groupChange(sex), { [sex.id]: 'undo' })
  assert.deepEqual(groupChange(lifeStage), { [lifeStage.id]: null })
  assert.deepEqual(groupChange(pullus), { [pullus.id]: 'undo' })
  assert.deepEqual(groupChange(numeric, 'decimal'), { decimal: 'apply' })
  assert.deepEqual(groupChange(encoding, encoding.values[0]), { busingen: 'undo' })
})

test('tidySummaryLine formats counts and omits zero sections', () => {
  assert.equal(tidySummaryLine({ tidy: { enabled: true, counts: { tidied_groups: 7, tidied_rows: 73412, suggestions: 3 } } }), 'We tidied 7 things in your data (73,412 values). 3 suggestions to check.')
  assert.equal(tidySummaryLine({ tidy: { enabled: true, counts: { tidied_groups: 1, tidied_rows: 1, suggestions: 1 } } }), 'We tidied 1 thing in your data (1 value). 1 suggestion to check.')
  assert.equal(tidySummaryLine({ tidy: { enabled: true, counts: { tidied_groups: 0, tidied_rows: 0, suggestions: 0 } } }), '')
  assert.equal(tidySummaryLine({ tidy: { enabled: false, counts: { tidied_groups: 2 } } }), '')
})

test('suggestionGroupChange applies every suggestion of a group and returns to the default', () => {
  const group = { id: 'tidy:0:7:thousands-or-decimal', values: [], more_values: 31 }
  assert.deepEqual(suggestionGroupChange(group, {}), { [group.id]: 'apply' })
  assert.deepEqual(suggestionGroupChange(group, { [group.id]: 'on' }), { [group.id]: null })
})
