// Which table the package explorer modal shows as the package model and source tables change,
// and what openPackageExplorer() requests open (explorerState.mjs).
import assert from 'node:assert/strict'
import test from 'node:test'

import { explorerForRequest, firstPackageTableId, reconcileExplorer } from './explorerState.mjs'

const model = {
  nodes: [
    { id: 'material', tableId: 318, title: 'Material', fields: [{ name: 'materialEntity_pk' }] },
    { id: 'occurrence', tableId: 322, title: 'Occurrence', fields: [{ name: 'occurrence_pk' }, { name: 'sex' }] },
    { id: 'event', tableId: 315, title: 'Event', fields: [{ name: 'event_pk' }] },
  ],
}
const emptyModel = { nodes: [] }
const sources = [
  { id: 400, title: 'wasps.xlsx', columns: ['Species', 'Sex'] },
  { id: 401, title: 'sites.csv', columns: ['Site'] },
]
const state = (tableId, extra = {}) => ({ tableId, view: 'rows', column: null, filters: [], ...extra })

test('the first linked table follows the standard order', () => {
  assert.equal(firstPackageTableId(model), 315)
  assert.equal(firstPackageTableId(emptyModel), null)
  assert.equal(firstPackageTableId(null), null)
})

test('the open table stays while it exists, as the same object when nothing changed', () => {
  const open = state(400, { column: 'Sex', filters: [{ column: 'Sex', op: 'is', value: 'F' }] })
  assert.equal(reconcileExplorer(open, { model, sourceTables: sources }), open)
  const linked = state(322, { filters: [{ column: 'sex', op: 'blank' }] })
  assert.equal(reconcileExplorer(linked, { model, sourceTables: sources }), linked)
})

test('a column or filters on columns the table lost are dropped', () => {
  const open = state(400, { column: 'Sex', filters: [{ column: 'Sex', op: 'blank' }, { column: 'Species', op: 'blank' }] })
  const renamed = [{ ...sources[0], columns: ['Species', 'sex_cleaned'] }, sources[1]]
  assert.deepEqual(reconcileExplorer(open, { model, sourceTables: renamed }), state(400, {
    filters: [{ column: 'Species', op: 'blank' }],
  }))
})

test('a deleted table falls back to the first linked table, or the first source table without a package', () => {
  assert.equal(reconcileExplorer(state(999), { model, sourceTables: sources }).tableId, 315)
  assert.equal(reconcileExplorer(state(400), { model: null, sourceTables: [sources[1]] }).tableId, 401)
  assert.equal(reconcileExplorer(state(400), { model: emptyModel, sourceTables: [] }).tableId, null)
})

test('a deleted source table falls back even while the package model is still loading', () => {
  assert.equal(reconcileExplorer(state(400), { model: null, sourceTables: [sources[1]], waiting: true }).tableId, 401)
})

test('a package table asked for before the model knows it waits for that model', () => {
  const asked = state(330)
  const pending = { tableId: 330 }
  assert.equal(reconcileExplorer(asked, { model: null, sourceTables: sources, pending, waiting: true }), asked)
  const arrived = { nodes: [...model.nodes, { id: 'organism', tableId: 330, title: 'Organism', fields: [] }] }
  assert.equal(reconcileExplorer(asked, { model: arrived, sourceTables: sources, pending, waiting: false }).tableId, 330)
  assert.equal(reconcileExplorer(asked, { model, sourceTables: sources, pending, waiting: false }).tableId, 315)
})

test('Explore asked for before the model has linked tables opens the first one when it arrives', () => {
  const onSource = state(400)
  const pending = { focus: 'package' }
  assert.equal(reconcileExplorer(onSource, { model: null, sourceTables: sources, pending, waiting: true }), onSource)
  assert.equal(reconcileExplorer(onSource, { model, sourceTables: sources, pending, waiting: false }).tableId, 315)
})

test('a request for a table opens it with the given view, column and filters', () => {
  const current = state(400)
  const requested = { view: 'columns', column: 'sex', filters: [] }
  assert.deepEqual(explorerForRequest(current, { tableId: 322, state: requested }, { model, sourceTables: sources }), {
    explorer: { tableId: 322, view: 'columns', column: 'sex', filters: [] },
    pending: null,
  })
})

test('a request for a table the model does not know yet waits, unless no newer model is coming', () => {
  const waiting = explorerForRequest(state(400), { tableId: 330 }, { model: null, sourceTables: sources })
  assert.deepEqual(waiting.pending, { tableId: 330 })
  assert.equal(waiting.explorer.tableId, 330)
  const settled = explorerForRequest(state(400), { tableId: 330 }, { model, sourceTables: sources, modelSettled: true })
  assert.deepEqual(settled, { explorer: state(400), pending: null })
})

test('Explore opens the first linked table, keeping one that is already open', () => {
  assert.equal(explorerForRequest(state(400), { focus: 'package' }, { model, sourceTables: sources }).explorer.tableId, 315)
  const onOccurrence = state(322)
  assert.equal(explorerForRequest(onOccurrence, { focus: 'package' }, { model }).explorer, onOccurrence)
  assert.deepEqual(explorerForRequest(state(400), { focus: 'package' }, { model: null, sourceTables: sources }).pending, {
    focus: 'package',
  })
  // An older model without linked tables, with its refresh not started yet: Explore still waits for it.
  assert.deepEqual(explorerForRequest(state(400), { focus: 'package' }, { model: emptyModel, sourceTables: sources, modelSettled: true }), {
    explorer: state(400),
    pending: { focus: 'package' },
  })
})

test('View tables opens the first source table, keeping one that is already open', () => {
  assert.equal(explorerForRequest(state(322), { focus: 'sources' }, { model, sourceTables: sources }).explorer.tableId, 400)
  const onSites = state(401)
  assert.equal(explorerForRequest(onSites, { focus: 'sources' }, { model, sourceTables: sources }).explorer, onSites)
})
