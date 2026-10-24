'use client'

import { useCallback, useEffect, useState } from 'react'
import config from '../config'
import {
  carriedMessage, checkMessage, isChecking, isEditable,
  skippedMessage, unconfirmedMessage,
} from '../utils/conversionNames.mjs'
import ScientificNameReview from './ScientificNameReview'

const fallbackLabel = (option, partial) => option.value === 'preserve'
  ? (partial ? 'Leave scientificName empty (the text fills verbatimIdentification wherever that would otherwise be empty)'
    : 'Leave scientificName empty (the text stays in verbatimIdentification)')
  : `Copy the supplied text into scientificName${option.value.startsWith('identification.') ? ' (identification)' : ''}`

export default function ConversionNameReview({ state, send, disabled, paused = false, datasetId, onRefresh, questions = [], decisions = {}, onChoose }) {
  const nameReview = state?.name_review
  const editable = isEditable(state)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [expanded, setExpanded] = useState(null)
  const checking = isChecking(nameReview)
  const updated = state?.updated_at
  const planId = state?.plan?.id
  const url = `${config.baseUrl}/api/datasets/${datasetId}/conversion/`
  useEffect(() => {
    if (!checking || !onRefresh) return undefined
    const interval = setInterval(onRefresh, 3000)
    return () => clearInterval(interval)
  }, [checking, onRefresh])
  const act = useCallback(async body => {
    setBusy(true); setError('')
    try { await send(body) } catch (err) { setError(err.message); throw err } finally { setBusy(false) }
  }, [send])
  const saveNames = useCallback(body => act({ ...body, action: 'names', plan_id: planId }), [act, planId])
  if (!nameReview?.summary?.labels && !skippedMessage(nameReview?.summary)) return null
  const { summary } = nameReview
  const checkingMessage = checkMessage(nameReview)
  const message = paused && checkingMessage?.spinner
    ? { variant: 'secondary', text: 'Name checks are paused until GBIF is working again.' } : checkingMessage
  const skipped = skippedMessage(summary)
  const held = unconfirmedMessage(summary)
  const needsReview = summary.decided < summary.labels || Boolean(held) || checking || summary.checked < summary.labels || nameReview.status === 'error' || Boolean(skipped) || questions.some(question => state.unresolved?.includes(question.id))
  return <details className="review-disclosure name-review" open={expanded ?? needsReview}
    onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><i className={`bi ${needsReview ? 'bi-flower1' : 'bi-check-circle'} me-2`} aria-hidden="true" /><span id="scientific-names-heading">Scientific names</span><small>{checking ? 'Checking…' : skipped ? 'Some names weren’t checked' : nameReview.status === 'error' ? 'Check interrupted' : `${summary.decided.toLocaleString()} of ${summary.labels.toLocaleString()} decided`}</small></summary>
    <div className="mt-3">
      <p className="small text-muted mb-2">Names checked with Catalogue of Life. Only names are published; your text stays in verbatimIdentification.</p>
      {editable && (summary.checked < summary.labels || nameReview.status === 'error') && !checking && <button type="button" className="btn btn-sm btn-outline-secondary mb-2" disabled={disabled || busy || paused} onClick={() => act({ action: 'check_names', plan_id: planId })}>Check names again</button>}
      {carriedMessage(nameReview.carried) && <p className="small text-muted">{carriedMessage(nameReview.carried)}</p>}
      {skipped && <p className="small text-warning-emphasis">{skipped}</p>}
      {summary.truncated > 0 && <p className="small text-muted">Only the {summary.labels.toLocaleString()} most frequent names are listed; {summary.truncated.toLocaleString()} others are converted as they are.</p>}
      {message && <div className={`alert alert-${message.variant} small py-2`} role="status">{message.spinner && <span className="spinner-border spinner-border-sm me-2" />}{message.text}</div>}
      {error && <div className="alert alert-danger small py-2" role="alert">{error}</div>}
      <ScientificNameReview nameReview={nameReview} updated={updated} datasetId={datasetId} url={url}
        editable={editable} busy={disabled || busy} send={saveNames} />
      {questions.map(question => <div key={question.id} className="border-top pt-2 mt-3" data-decision-id={question.id}>
        <label htmlFor={question.id} className="small fw-semibold">For names you don&apos;t decide above{questions.length > 1 ? ` (${state?.plan?.tables?.[question.table]?.name || 'table'})` : ''}</label>
        <p className="small text-muted mb-1">{summary.decided >= summary.labels && !summary.truncated && !summary.skipped_long?.labels
          ? 'Every name has a decision above, so this applies to no row.'
          : 'A Data Package scientificName holds only the name, without its author. Decide names above where you can.'}</p>
        <select id={question.id} className="form-select form-select-sm" value={decisions[question.id] || ''} disabled={disabled || !onChoose} onChange={event => onChoose(question.id, event.target.value)}>
          {!decisions[question.id] && <option value="">Choose…</option>}
          {question.options.map(option => <option key={option.value} value={option.value}>{fallbackLabel(option, Boolean(state?.plan?.columns?.find(column => column.id === question.id)?.verbatim_source))}</option>)}
        </select>
      </div>)}
    </div>
  </details>
}
