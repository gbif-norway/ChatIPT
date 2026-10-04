'use client'

import { stepStates } from '../utils/conversionPlan.mjs'

const ICONS = { done: 'bi-check-lg', error: 'bi-exclamation-lg' }
const STATUS_TEXT = { done: 'done', current: 'current step', error: 'stopped', upcoming: 'not started' }

// Upload → Inspect → Review → Convert → Download, with the current step taken from the conversion status.
export default function ConversionSteps({ state }) {
  return <ol className="conversion-steps" aria-label="Conversion progress">
    {stepStates(state).map((step, index) => <li key={step.label} className={`conversion-step conversion-step-${step.status}`}
      aria-current={step.status === 'current' || step.status === 'error' ? 'step' : undefined}>
      <span className="conversion-step-mark" aria-hidden="true">{ICONS[step.status] ? <i className={`bi ${ICONS[step.status]}`} /> : index + 1}</span>
      <span className="conversion-step-label">{step.label}<span className="visually-hidden"> ({STATUS_TEXT[step.status]})</span></span>
    </li>)}
  </ol>
}
