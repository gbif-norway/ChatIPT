'use client'

import { useCallback, useEffect, useState } from 'react'
import config from '../config'
import { STATUS_LABELS, formatName } from '../utils/taxonReview.mjs'
import {
  DECISION_LABELS, PAGE_SIZE, bulkActions, bulkBody, checkMessage, decisionBody, decisionResult,
  isChecking, isEditable, pageCount, pageQuery, parseNote,
} from '../utils/conversionNames.mjs'

function Parsed({ entry }) {
  const parsed = entry.parsed
  return <div className="small">
    {parsed?.usable ? <div className="fw-semibold">{formatName({ scientificName: parsed.canonical, scientificNameAuthorship: parsed.authorship })}</div> : <span className="text-muted">No split</span>}
    {parsed?.rank && <span className="text-muted">{parsed.rank}</span>}
    <div className="text-muted">{parseNote(entry)}</div>
  </div>
}

function Match({ entry }) {
  const match = entry.match
  const status = STATUS_LABELS[match?.status] || STATUS_LABELS.none
  if (!match) return <span className="small text-muted">Not matched yet</span>
  return <div className="small">
    <div className="d-flex flex-wrap align-items-center gap-2">
      {match.usage ? <span className="fw-semibold">{formatName(match.usage)}</span> : <span className="text-muted">No suggestion</span>}
      {match.usage?.taxonRank && <span className="text-muted">{match.usage.taxonRank}</span>}
      <span className={`badge text-bg-${status.variant}`}>{status.label}</span>
    </div>
    {match.acceptedUsage && <div className="text-muted">Synonym; COL accepts {formatName(match.acceptedUsage)}</div>}
    {entry.rank_mismatch && <div className="text-warning-emphasis">Your rank is “{entry.source_rank}”, COL’s is “{match.usage.taxonRank}”.</div>}
  </div>
}

function DecisionCell({ entry, editable, busy, onDecide }) {
  const decision = entry.decision
  const alternatives = entry.match?.alternatives || []
  if (decision) {
    const result = decisionResult(decision)
    return <div className="small">
      <span className="badge text-bg-primary">{DECISION_LABELS[decision.decision]}</span>
      {result && <div>{result}{decision.taxonRank ? ` (${decision.taxonRank})` : ''}</div>}
      {decision.by?.startsWith('bulk') && <div className="text-muted">Accepted in bulk</div>}
      {editable && <button type="button" className="btn btn-link btn-sm p-0" disabled={busy} onClick={() => onDecide(entry.label, null)}>Undo</button>}
    </div>
  }
  if (!editable) return <span className="badge text-bg-secondary">Not reviewed</span>
  return <div>
    <div className="d-flex flex-wrap gap-1" role="group" aria-label={`Decision for ${entry.label}`}>
      <button type="button" className="btn btn-sm btn-outline-success" disabled={busy || !entry.offers.parsed}
        onClick={() => onDecide(entry.label, 'parsed')} title="Write the parsed name to scientificName and its authorship to scientificNameAuthorship">Use split</button>
      <button type="button" className="btn btn-sm btn-outline-success" disabled={busy || !entry.offers.col}
        onClick={() => onDecide(entry.label, 'col')} title="Write the Catalogue of Life name and authorship">Use COL name</button>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={busy}
        onClick={() => onDecide(entry.label, 'keep')} title="Copy the supplied text to scientificName where it is empty">Keep as supplied</button>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={busy}
        onClick={() => onDecide(entry.label, 'empty')} title="Leave scientificName empty; the supplied text stays in verbatimIdentification">Leave empty</button>
    </div>
    {alternatives.length > 0 && <select className="form-select form-select-sm mt-1" aria-label={`Other COL names for ${entry.label}`} value="" disabled={busy}
      onChange={event => event.target.value && onDecide(entry.label, 'alternative', event.target.value)}>
      <option value="">Other COL name…</option>
      {alternatives.map(alternative => <option key={alternative.id} value={alternative.id}>{formatName(alternative)}{alternative.taxonRank ? ` (${alternative.taxonRank})` : ''}</option>)}
    </select>}
  </div>
}

export default function ConversionNameReview({ state, send, disabled, datasetId, onRefresh }) {
  const nameReview = state?.name_review
  const editable = isEditable(state)
  const [view, setView] = useState('pending')
  const [page, setPage] = useState(0)
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(null)
  const checking = isChecking(nameReview)
  const updated = state?.updated_at
  const planId = state?.plan?.id
  const url = `${config.baseUrl}/api/datasets/${datasetId}/conversion/`

  // One page of labels at a time, so a long list never travels with every poll.
  useEffect(() => {
    if (!datasetId || !nameReview?.summary?.labels) return undefined
    const controller = new AbortController()
    ;(async () => {
      try {
        const response = await fetch(`${url}?${pageQuery({ offset: page * PAGE_SIZE, view })}`, { credentials: 'include', signal: controller.signal })
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail || 'The names could not be loaded.')
        setRows(data.name_review.labels); setTotal(data.name_review.page.total)
      } catch (err) { if (err.name !== 'AbortError') setError(err.message) }
    })()
    return () => controller.abort()
  }, [datasetId, url, page, view, updated, nameReview?.summary?.labels])

  useEffect(() => { setPage(0) }, [view])

  // Progress arrives by polling while a check runs.
  useEffect(() => {
    if (!checking || !onRefresh) return undefined
    const interval = setInterval(onRefresh, 3000)
    return () => clearInterval(interval)
  }, [checking, onRefresh])

  const act = useCallback(async (body) => {
    setBusy(true); setError('')
    try { await send(body) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }, [send])

  if (!nameReview?.summary?.labels) return null
  const { summary } = nameReview
  const message = checkMessage(nameReview)
  const bulk = bulkActions(nameReview)
  const pages = pageCount(total)
  const locked = disabled || busy
  const unfinished = summary.checked < summary.labels
  return <section className="card card-body mb-3" aria-labelledby="scientific-names-heading">
    <div className="d-flex flex-wrap align-items-center gap-2">
      <h3 className="h5 mb-0 me-auto" id="scientific-names-heading">Scientific names</h3>
      {editable && (unfinished || nameReview.status === 'error') && !checking && <button type="button" className="btn btn-sm btn-outline-secondary" disabled={locked}
        onClick={() => act({ action: 'check_names', plan_id: planId })}>Check names again</button>}
    </div>
    <p className="small text-muted mt-2 mb-2">
      Your names are checked with the GBIF name parser and Catalogue of Life, the taxonomy GBIF.org uses{nameReview.checklist ? ` (${nameReview.checklist})` : ''}.
      A match shows that a name was found, not that the identification is right. Nothing changes unless you decide: the supplied text always stays in
      verbatimIdentification, and names you do not review are converted as they are.
      {' '}{summary.decided.toLocaleString()} of {summary.labels.toLocaleString()} names decided, covering {summary.rows.toLocaleString()} rows.
    </p>
    {summary.truncated > 0 && <p className="small text-muted">Only the {summary.labels.toLocaleString()} most frequent names are listed; {summary.truncated.toLocaleString()} others are converted as they are.</p>}
    {message && <div className={`alert alert-${message.variant} small py-2`} role="status">
      {message.spinner && <span className="spinner-border spinner-border-sm me-2" />}{message.text}</div>}
    {error && <div className="alert alert-danger small py-2" role="alert">{error}</div>}
    {editable && bulk.length > 0 && <div className="d-flex flex-wrap gap-2 mb-2">
      {bulk.map(action => confirming === action.bulk
        ? <div key={action.bulk} className="alert alert-success small py-2 mb-0">
          Accept {action.count.toLocaleString()} {action.label}? These are {action.detail}.
          <button type="button" className="btn btn-sm btn-success ms-2" disabled={locked}
            onClick={async () => { await act(bulkBody(planId, action.bulk)); setConfirming(null) }}>Accept {action.count.toLocaleString()}</button>
          <button type="button" className="btn btn-sm btn-link" onClick={() => setConfirming(null)}>Cancel</button>
        </div>
        : <button key={action.bulk} type="button" className="btn btn-sm btn-success" disabled={locked} onClick={() => setConfirming(action.bulk)}>
          Accept all {action.count.toLocaleString()} {action.label}…</button>)}
    </div>}
    <div className="btn-group btn-group-sm mb-2 align-self-start" role="group" aria-label="Filter names">
      {[['pending', `Needs review (${(summary.labels - summary.decided).toLocaleString()})`], ['all', `All (${summary.labels.toLocaleString()})`]].map(([value, label]) =>
        <button key={value} type="button" className={`btn btn-outline-secondary${view === value ? ' active' : ''}`} aria-pressed={view === value} onClick={() => setView(value)}>{label}</button>)}
    </div>
    {rows.length === 0 ? <p className="small text-muted">{view === 'pending' ? 'No names are waiting for a decision.' : 'No names.'}</p>
      : <div className="table-responsive"><table className="table table-sm align-top">
        <thead><tr><th scope="col">Name in your data</th><th scope="col">Parsed</th><th scope="col">Catalogue of Life</th><th scope="col">Your decision</th></tr></thead>
        <tbody>{rows.map(entry => <tr key={entry.label}>
          <td className="small"><div className="fw-semibold text-break">{entry.label}</div>
            <div className="text-muted">{entry.rows.toLocaleString()} {entry.rows === 1 ? 'row' : 'rows'}{entry.hints?.kingdom ? ` · ${entry.hints.kingdom}` : ''}</div></td>
          <td><Parsed entry={entry} /></td>
          <td><Match entry={entry} /></td>
          <td><DecisionCell entry={entry} editable={editable} busy={locked} onDecide={(label, kind, usageId) => act(decisionBody(planId, label, kind, usageId))} /></td>
        </tr>)}</tbody></table></div>}
    {pages > 1 && <nav className="d-flex align-items-center gap-2 small" aria-label="Name pages">
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
      <span>Page {page + 1} of {pages}</span>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next</button>
    </nav>}
  </section>
}
