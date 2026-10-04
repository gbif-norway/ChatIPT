import test from 'node:test'
import assert from 'node:assert/strict'
import { datasetDisplayName, datasetKind } from './datasetKind.mjs'

test('conversions and datasets started from files have different labels and icons', () => {
  assert.deepEqual(datasetKind({ workflow_type: 'dwca_conversion' }), { key: 'converted', label: 'Archive conversion', icon: 'bi-arrow-repeat' })
  assert.equal(datasetKind({ workflow_type: 'dataset_publication' }).label, 'Data publication')
  assert.equal(datasetKind({}).key, 'new')
})

test('display name falls back to the first uploaded file, then the given fallback', () => {
  assert.equal(datasetDisplayName({ title: 'Bats' }), 'Bats')
  assert.equal(datasetDisplayName({ title: '  ', user_files: [{ filename: 'bats.csv' }] }), 'bats.csv')
  assert.equal(datasetDisplayName({ title: '', user_files: [] }, 'Untitled Dataset'), 'Untitled Dataset')
})
