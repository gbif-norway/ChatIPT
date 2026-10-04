'use client'

import { aiDecidedItems, optionState } from '../utils/conversionReview.mjs'

// Choices the AI reviewer settled, with its reason and cited evidence. Any change is the user's own decision.
export default function ConversionAiDecisions({ state, disabled, onChoose, onKeep }) {
  const items = aiDecidedItems(state)
  if (!items.length) return null
  const stale = items.filter(entry => entry.stale).length
  return <details className="card card-body mb-3" open={stale > 0}>
    <summary className="fw-semibold">Decided by the AI reviewer ({items.length}){stale ? ` · ${stale} to check` : ''}</summary>
    <p className="small text-muted mt-2 mb-2">These choices only interpret values already in your files. Change any of them; your choice replaces the AI reviewer’s.</p>
    {items.map(({ id, item, value, source, stale: needsCheck }) => <div key={id} className={`border rounded p-2 mb-2 ${needsCheck ? 'border-warning' : ''}`}>
      <label htmlFor={`ai-${id}`} className="small fw-semibold">{item.title}</label>
      {source?.rationale && <p className="small mb-1">{source.rationale}</p>}
      {source?.evidence?.length > 0 && <details className="small mb-1"><summary>Evidence it relied on</summary>
        <ul className="mb-0">{source.evidence.filter(entry => entry.excerpt).map(entry => <li key={entry.ref} style={{ overflowWrap: 'anywhere' }}>{entry.excerpt}</li>)}</ul></details>}
      {needsCheck && <div className="small text-warning-emphasis mb-1">This was decided before a choice it depends on changed. Keep it or choose another option before converting.
        <button className="btn btn-sm btn-outline-secondary ms-2" disabled={disabled} onClick={() => onKeep(id)}>Keep this choice</button></div>}
      <select id={`ai-${id}`} className="form-select form-select-sm" value={value} disabled={disabled} onChange={event => onChoose(id, event.target.value)}>
        {item.options.map(option => <option key={option.value} value={option.value}>
          {option.label}{optionState(state, id, option.value).available ? '' : ' (not possible with other current choices)'}</option>)}
      </select>
    </div>)}
  </details>
}
