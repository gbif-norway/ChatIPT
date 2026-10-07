'use client'

import { useState } from 'react'
import { automaticSummary } from '../utils/conversionPlan.mjs'

function SummaryLine({ line, icon, renderChoice, disabled }) {
  const [open, setOpen] = useState(false)
  return <div className="automatic-summary-line" data-decision-id={line.id}>
    <div className="d-flex align-items-start gap-2">
      {icon && <i className={`bi ${icon} text-body-secondary mt-1`} aria-hidden="true" />}
      <div className="flex-grow-1">
        <div className="d-flex flex-wrap align-items-center gap-2"><strong>{line.title}</strong>{line.changed && <span className="badge text-bg-secondary">Changed by you</span>}</div>
        {line.text && <p className="small text-body-secondary mb-1">{line.text}</p>}
        <button type="button" className="btn btn-link btn-sm p-0" disabled={disabled} aria-expanded={open} onClick={() => setOpen(value => !value)}>
          {open ? 'Close' : 'Change'}
        </button>
        {open && <div className="automatic-summary-editor mt-2">{renderChoice(line.item)}</div>}
      </div>
    </div>
  </div>
}

export default function ConversionAutomaticSummary({ state, selected, hidden, disabled, renderChoice }) {
  const [specimenOpen, setSpecimenOpen] = useState({})
  const { glance, specimen, silent } = automaticSummary(state, selected, hidden)
  if (!glance.length && !specimen.length && !silent.length) return null
  const groups = [...new Map(glance.map(line => [line.family, line.heading])).entries()]
  // Routine choices can be many (one per repeated agent name), so each family folds on its own.
  const silentGroups = [...new Map(silent.map(line => [line.heading, []])).keys()]
    .map(heading => [heading, silent.filter(line => line.heading === heading)])
  return <section className="automatic-summary card card-body mb-3" aria-labelledby="automatic-summary-heading">
    <h2 id="automatic-summary-heading" className="h4">What we decided for you</h2>
    <p className="small text-body-secondary">We made these choices from what your files show. Change any of them; your original files are always kept in the download.</p>
    {groups.map(([family, heading]) => <div key={family} className="automatic-summary-group">
      <h3 className="h6 text-body-secondary">{heading}</h3>
      {glance.filter(line => line.family === family).map(line => <SummaryLine key={line.id} line={line} icon="bi-lightbulb" renderChoice={renderChoice} disabled={disabled} />)}
    </div>)}
    {specimen.map(line => <div className="automatic-summary-line" data-decision-id={line.id} key={line.id}>
      <div className="d-flex align-items-start gap-2"><i className="bi bi-archive text-body-secondary mt-1" aria-hidden="true" /><div className="flex-grow-1">
        <strong>{line.title}</strong>
        <details className="small"><summary>See the list</summary><p className="mt-1 mb-1">{line.names.join(', ')}</p>
          {line.overridden.length > 0 && <p className="mb-1">Changed by you: {line.overridden.join(', ')}</p>}</details>
        <p className="small text-body-secondary mb-1">{line.why}</p>
        <button type="button" className="btn btn-link btn-sm p-0" disabled={disabled} aria-expanded={Boolean(specimenOpen[line.id])}
          onClick={() => setSpecimenOpen(current => ({ ...current, [line.id]: !current[line.id] }))}>{specimenOpen[line.id] ? 'Close' : 'Change'}</button>
        {specimenOpen[line.id] && <div className="automatic-summary-editor mt-2">{renderChoice(line.item)}</div>}
      </div></div>
    </div>)}
    {silent.length > 0 && <details className="automatic-summary-silent">
      <summary><i className="bi bi-check2-circle me-1" aria-hidden="true" />{silent.length} routine {silent.length === 1 ? 'choice' : 'choices'} made automatically</summary>
      <div className="mt-2">{silentGroups.map(([heading, lines]) => <details key={heading} className="small mb-2">
        <summary>{heading} ({lines.length})</summary>
        {lines.map(line => <SummaryLine key={line.id} line={line} renderChoice={renderChoice} disabled={disabled} />)}
      </details>)}</div>
    </details>}
  </section>
}
