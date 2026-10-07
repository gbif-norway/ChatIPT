'use client'

import { useState } from 'react'
import { groupChange, suggestionGroupChange, tidyGroups, tidySummaryLine, valueChange } from '../utils/conversionTidy.mjs'

export default function ConversionTidySummary({ state, send, disabled }) {
  const { tidied, suggestions } = tidyGroups(state)
  const [errors, setErrors] = useState({})
  if (!tidied.length && !suggestions.length) return null

  const pending = Boolean(state?.tidy?.pending)
  const change = async (key, changes) => {
    setErrors(current => ({ ...current, [key]: '' }))
    try {
      await send({ action: 'tidy', plan_id: state.plan.id, changes })
    } catch (error) {
      setErrors(current => ({ ...current, [key]: error?.message || 'Your tidy-up choice could not be updated.' }))
    }
  }
  const rows = number => `${Number(number || 0).toLocaleString('en-US')} ${Number(number || 0) === 1 ? 'row' : 'rows'}`

  return <details className="review-disclosure" open={suggestions.length > 0}>
    <summary className="fw-semibold">Here’s what we tidied <small className="text-muted fw-normal">{tidySummaryLine(state)}</small></summary>
    <p className="small text-muted mt-2 mb-2">We cleaned these values automatically so you have fewer questions. Your original files are kept unchanged in the download, and you can undo any change.</p>
    {pending && <p className="small text-muted" role="status"><span className="spinner-border spinner-border-sm me-2" aria-hidden="true" />Updating your choices…</p>}
    {tidied.map(group => <div key={group.id} className={`border rounded p-2 mb-2 ${group.applied ? '' : 'text-body-secondary'}`}>
      <div className="d-flex justify-content-between align-items-start gap-2">
        <div className="small fw-semibold">{group.title}</div>
        <button type="button" className="btn btn-sm btn-outline-secondary flex-shrink-0" disabled={disabled || pending}
          aria-label={`${group.applied ? 'Undo' : 'Redo'} tidy-up for ${group.field}`}
          onClick={() => change(group.id, groupChange(group))}>{group.applied ? 'Undo' : 'Redo'}</button>
      </div>
      {!group.applied && <p className="small mb-1">Undone — your original values are used.</p>}
      <ul className="small mb-1 ps-3">
        {group.values.slice(0, 5).map(value => <li key={value.id}>{valueChange(value, group.field)} <span className="text-muted">· {rows(value.rows)}</span></li>)}
      </ul>
      {group.conflict_rows > 0 && <p className="small text-muted mb-1">{rows(group.conflict_rows)} kept as written because another column already says something different.</p>}
      {errors[group.id] && <p className="small text-danger mb-0" role="alert">{errors[group.id]}</p>}
    </div>)}
    {suggestions.length > 0 && <>
      <h3 className="small fw-semibold mt-3">Suggestions</h3>
      {suggestions.map(group => <div key={group.id} className="border rounded p-2 mb-2">
        <div className="d-flex justify-content-between align-items-start gap-2">
          <div className="small fw-semibold mb-1">{group.title}</div>
          {(group.values.length > 1 || group.more_values > 0) && <button type="button" className="btn btn-sm btn-outline-secondary flex-shrink-0" disabled={disabled || pending}
            aria-label={`${state.tidy.overrides?.[group.id] === 'on' ? 'Undo all suggestions' : 'Apply all suggestions'} for ${group.field}`}
            onClick={() => change(group.id, suggestionGroupChange(group, state.tidy.overrides))}>{state.tidy.overrides?.[group.id] === 'on' ? 'Undo all' : `Apply all${group.more_values ? ` ${(group.values.length + group.more_values).toLocaleString('en-US')}` : ''}`}</button>}
        </div>
        {errors[group.id] && <p className="small text-danger mb-0" role="alert">{errors[group.id]}</p>}
        {group.values.map(value => <div key={value.id} className="border-top pt-2 mt-2">
          <div className="d-flex justify-content-between align-items-start gap-2">
            <div className="small">{valueChange(value, group.field)} <span className="text-muted">· {rows(value.rows)}</span></div>
            <button type="button" className="btn btn-sm btn-outline-secondary flex-shrink-0" disabled={disabled || pending}
              aria-label={`${value.applied ? 'Undo applied suggestion' : 'Apply suggestion'} for ${group.field}`}
              onClick={() => change(value.id, groupChange(group, value.id))}>{value.applied ? 'Applied · Undo' : 'Apply'}</button>
          </div>
          {value.note && <p className="small text-muted mb-1 mt-1">{value.note}</p>}
          {errors[value.id] && <p className="small text-danger mb-0" role="alert">{errors[value.id]}</p>}
        </div>)}
        {group.more_values > 0 && <p className="small text-muted mb-0 mt-2">{group.more_values.toLocaleString('en-US')} more like these are not listed here; “Apply all” includes them.</p>}
      </div>)}
    </>}
  </details>
}
