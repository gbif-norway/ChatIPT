// Which kind of dataset a card or page shows. The label is never conveyed by colour alone.
export function datasetKind(dataset) {
  return dataset?.workflow_type === 'dwca_conversion'
    ? { key: 'converted', label: 'Converted archive', icon: 'bi-arrow-repeat' }
    : { key: 'new', label: 'New dataset', icon: 'bi-file-earmark-spreadsheet' }
}

export function datasetDisplayName(dataset, fallback = 'Untitled dataset') {
  return dataset?.title?.trim() || dataset?.user_files?.[0]?.filename || fallback
}
