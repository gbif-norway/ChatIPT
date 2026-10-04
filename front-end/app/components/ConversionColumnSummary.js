'use client'

import { columnDetails, summariseColumns, summaryLines } from '../utils/conversionPlan.mjs'

// Automatic mappings in one short summary, with the full per-column list one click away.
export default function ConversionColumnSummary({ state, selected }) {
  const lines = summaryLines(summariseColumns(state, selected))
  if (!lines.length) return null
  const details = columnDetails(state, selected)
  return <section className="mb-4" aria-labelledby="conversion-columns-heading">
    <h2 className="h5" id="conversion-columns-heading">Your columns</h2>
    <ul className="mb-2">{lines.map(line => <li key={line}>{line}</li>)}</ul>
    <details className="small">
      <summary>Show every column</summary>
      <div className="table-responsive mt-2" style={{ maxHeight: 360 }}>
        <table className="table table-sm mb-0">
          <thead><tr><th scope="col">Table</th><th scope="col">Column</th><th scope="col">What happens to it</th></tr></thead>
          <tbody>{details.map(column => <tr key={column.id}><td>{column.table}</td><td>{column.name}</td><td>{column.outcome}</td></tr>)}</tbody>
        </table>
      </div>
    </details>
  </section>
}
