'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import config from '../config'
import { STATUS_LABELS, formatName } from '../utils/taxonReview.mjs'
import {
  DECISION_LABELS, PAGE_SIZE, bulkActions, bulkBody, checkMessage, choiceGroups, choiceLabel, decisionBody, decisionResult,
  isChecking, isEditable, pageCount, pageQuery, parseNote, replacementWarning, skippedMessage, unconfirmedMessage,
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
  const [more, setMore] = useState(false)
  // A COL choice that replaces the user's name waits here for its confirming second click.
  const [confirming, setConfirming] = useState(null)
  const [rowError, setRowError] = useState('')
  const confirmRef = useRef(null)
  const openerRef = useRef(null)
  const restoreFocus = useRef(false)
  useEffect(() => {
    if (confirming) confirmRef.current?.focus()
    else if (restoreFocus.current) {
      restoreFocus.current = false
      openerRef.current?.focus?.()
    }
  }, [confirming])

  // Sends one decision; an error stays next to this row and a pending confirmation stays open.
  const submit = async (kind, usageId, options) => {
    const failure = await onDecide(entry.label, kind, usageId, options)
    setRowError(failure || '')
    return !failure
  }
  const usageIdOf = (choice) => choice.decision === 'alternative' ? choice.usage.id : undefined
  const choose = (choice) => {
    if (!choice.replaces) return submit(choice.decision, usageIdOf(choice))
    openerRef.current = typeof document !== 'undefined' ? document.activeElement : null
    setConfirming(choice)
  }
  const cancel = () => { restoreFocus.current = true; setConfirming(null) }
  const error = rowError && <div className="text-danger small mt-1" role="alert">{rowError}</div>

  if (decision) {
    const result = decisionResult(decision)
    return <div className="small">
      <span className="badge text-bg-primary">{DECISION_LABELS[decision.decision]}</span>
      {result && <div>{result}{decision.taxonRank ? ` (${decision.taxonRank})` : ''}</div>}
      {decision.replaces && <div className="text-warning-emphasis">Confirmed: {decision.replaces}</div>}
      {decision.corrects && <div className="text-muted">COL {decision.corrects}</div>}
      {entry.decision_unconfirmed && <div className="text-danger-emphasis">
        This earlier choice replaces your name with a coarser or different taxon and was never confirmed, so your own name is kept.
        Confirm it or choose again.</div>}
      {decision.by?.startsWith('bulk') && <div className="text-muted">Accepted in bulk</div>}
      {editable && <div className="d-flex flex-wrap gap-1">
        {entry.decision_unconfirmed && <button type="button" className="btn btn-sm btn-outline-warning" disabled={busy}
          onClick={() => submit(decision.decision, decision.decision === 'alternative' ? decision.usageId : undefined, { confirmCoarser: true })}>
          Confirm {decision.scientificName}</button>}
        {entry.decision_unconfirmed && <button type="button" className="btn btn-sm btn-success" disabled={busy}
          onClick={() => submit('keep')}>Keep my name</button>}
        <button type="button" className="btn btn-link btn-sm p-0" disabled={busy} onClick={() => submit(null)}>Undo</button>
      </div>}
      {error}
    </div>
  }
  if (!editable) return <span className="badge text-bg-secondary">Not reviewed</span>
  const { main, sameName, others } = choiceGroups(entry)
  const keepFirst = entry.suggested === 'keep'
  const keep = <button type="button" className={`btn btn-sm ${keepFirst ? 'btn-success' : 'btn-outline-secondary'}`} disabled={busy}
    onClick={() => submit('keep')} title="Copy the supplied text to scientificName where it is empty">Keep my name</button>
  return <div>
    <div className="d-flex flex-wrap gap-1" role="group" aria-label={`Decision for ${entry.label}`}>
      {keepFirst && keep}
      <button type="button" className="btn btn-sm btn-outline-success" disabled={busy || !entry.offers.parsed}
        onClick={() => submit('parsed')} title="Write the parsed name to scientificName and its authorship to scientificNameAuthorship">Use split</button>
      {main && (main.replaces
        ? <button type="button" className="btn btn-sm btn-outline-warning" disabled={busy} onClick={() => choose(main)}
          title={`Write ${formatName(main.usage)} instead of your name`}>Use {main.usage.scientificName}… <span className="fw-normal">({main.replaces.text})</span></button>
        : <button type="button" className="btn btn-sm btn-outline-success" disabled={busy} onClick={() => choose(main)}
          title="Write the Catalogue of Life name and authorship">Use COL name</button>)}
      {!keepFirst && keep}
      <button type="button" className="btn btn-sm btn-link" aria-expanded={more} onClick={() => setMore(!more)}>{more ? 'Fewer options' : 'More…'}</button>
    </div>
    {main?.corrects && <div className="small text-muted mt-1">COL {main.corrects}.</div>}
    {confirming && <div className="alert alert-warning small py-2 mt-1 mb-0" role="alert">
      {replacementWarning(confirming, entry.rows)}
      <div className="d-flex flex-wrap gap-1 mt-1">
        <button ref={confirmRef} type="button" className="btn btn-sm btn-warning" disabled={busy}
          onClick={async () => { if (await submit(confirming.decision, usageIdOf(confirming), { confirmCoarser: true })) setConfirming(null) }}>
          Replace with {confirming.usage.scientificName}</button>
        <button type="button" className="btn btn-sm btn-link" onClick={cancel}>Cancel</button>
      </div>
    </div>}
    {sameName.length > 0 && <div className="mt-1">
      <div className="text-muted small">Your name in Catalogue of Life:</div>
      <div className="d-flex flex-column align-items-start gap-1">
        {sameName.map(choice => <button key={choice.usage.id} type="button" className="btn btn-sm btn-outline-success text-start" disabled={busy}
          onClick={() => choose(choice)} title="Write this Catalogue of Life name and authorship">
          Use {choiceLabel(choice)}{choice.usage.status && choice.usage.status !== 'accepted' ? ` (${choice.usage.status.replace(/_/g, ' ')})` : ''}
          {choice.rank_note && <span className="text-warning-emphasis"> — {choice.rank_note}</span>}</button>)}
      </div>
    </div>}
    {error}
    {more && <div className="d-flex flex-wrap align-items-center gap-1 mt-1">
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={busy}
        onClick={() => submit('empty')} title="Publish no scientificName; the supplied text fills verbatimIdentification wherever that would otherwise be empty">Don&apos;t publish a name</button>
      {others.length > 0 && <select className="form-select form-select-sm w-auto" aria-label={`Other COL names for ${entry.label}`} value="" disabled={busy}
        onChange={event => {
          const choice = others.find(item => String(item.usage.id) === event.target.value)
          if (choice) choose(choice)
        }}>
        <option value="">Other COL name…</option>
        {others.map(choice => <option key={choice.usage.id} value={choice.usage.id}>
          {choiceLabel(choice)}{choice.replaces ? ` — ${choice.replaces.text}` : ''}</option>)}
      </select>}
    </div>}
  </div>
}

// The scientificName question asks what happens to names without a decision here; each option is phrased for that.
const fallbackLabel = (option, partial) => option.value === 'preserve'
  ? (partial ? 'Leave scientificName empty (the text fills verbatimIdentification wherever that would otherwise be empty)'
    : 'Leave scientificName empty (the text stays in verbatimIdentification)')
  : `Copy the supplied text into scientificName${option.value.startsWith('identification.') ? ' (identification)' : ''}`

export default function ConversionNameReview({ state, send, disabled, datasetId, onRefresh, questions = [], decisions = {}, onChoose }) {
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

  // One row's decision: its error is returned for that row to show instead of the section-wide alert.
  const actRow = useCallback(async (body) => {
    setBusy(true)
    try { await send(body); return null } catch (err) { return err.message } finally { setBusy(false) }
  }, [send])

  if (!nameReview?.summary?.labels && !skippedMessage(nameReview?.summary)) return null
  const { summary } = nameReview
  const message = checkMessage(nameReview)
  const bulk = bulkActions(nameReview)
  const pages = pageCount(total)
  const locked = disabled || busy
  const unfinished = summary.checked < summary.labels
  const skipped = skippedMessage(summary)
  const held = unconfirmedMessage(summary)
  const needsReview = summary.decided < summary.labels || Boolean(held) || checking || unfinished || nameReview.status === 'error' || Boolean(skipped) || questions.some(question => state.unresolved?.includes(question.id))
  return <details className="review-disclosure name-review" open={needsReview}>
    <summary><i className={`bi ${needsReview ? 'bi-flower1' : 'bi-check-circle'} me-2`} aria-hidden="true" /><span id="scientific-names-heading">Scientific names</span><small>{checking ? 'Checking…' : skipped ? 'Some names weren’t checked' : nameReview.status === 'error' ? 'Check interrupted' : `${summary.decided.toLocaleString()} of ${summary.labels.toLocaleString()} reviewed`}</small></summary>
    <div className="mt-3">
    <div className="d-flex flex-wrap align-items-center gap-2">
      {editable && (unfinished || nameReview.status === 'error') && !checking && <button type="button" className="btn btn-sm btn-outline-secondary" disabled={locked}
        onClick={() => act({ action: 'check_names', plan_id: planId })}>Check names again</button>}
    </div>
    <p className="small text-muted mt-2 mb-2">
      Your names are checked with the GBIF name parser and Catalogue of Life, the taxonomy GBIF.org uses{nameReview.checklist ? ` (${nameReview.checklist})` : ''}.
      A match shows that a name was found, not that the identification is right. Nothing changes unless you decide: the supplied text fills
      verbatimIdentification wherever that would otherwise be empty, and your original files keep everything{questions.length ? ', and names you don\'t decide follow the choice at the end of this section' : ', and names you do not review are converted as they are'}.
      {' '}{summary.decided.toLocaleString()} of {summary.labels.toLocaleString()} names decided, covering {summary.rows.toLocaleString()} rows.
    </p>
    {held && <div className="alert alert-warning small py-2" role="status">{held}</div>}
    {skippedMessage(summary) && <p className="small text-warning-emphasis">{skippedMessage(summary)}</p>}
    {summary.truncated > 0 && <p className="small text-muted">Only the {summary.labels.toLocaleString()} most frequent names are listed; {summary.truncated.toLocaleString()} others are converted as they are.</p>}
    {message && <div className={`alert alert-${message.variant} small py-2`} role="status">
      {message.spinner && <span className="spinner-border spinner-border-sm me-2" />}{message.text}</div>}
    {error && <div className="alert alert-danger small py-2" role="alert">{error}</div>}
    {editable && bulk.length > 0 && <div className="d-flex flex-wrap gap-2 mb-2">
      {bulk.map(action => confirming === action.bulk
        ? <div key={action.bulk} className="alert alert-success small py-2 mb-0">
          Accept {action.count.toLocaleString()} {action.label}? These are {action.detail}.
          {action.items?.length > 0 && <ul className="mb-1 mt-1">{action.items.map(item => <li key={item}>{item}</li>)}</ul>}
          {action.items?.length > 0 && action.items.length < action.count && <div className="text-muted">…and {(action.count - action.items.length).toLocaleString()} more.</div>}
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
          <td><DecisionCell entry={entry} editable={editable} busy={locked}
            onDecide={(label, kind, usageId, options) => actRow(decisionBody(planId, label, kind, usageId, options))} /></td>
        </tr>)}</tbody></table></div>}
    {pages > 1 && <nav className="d-flex align-items-center gap-2 small" aria-label="Name pages">
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
      <span>Page {page + 1} of {pages}</span>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next</button>
    </nav>}
    {questions.map(question => <div key={question.id} className="border-top pt-2 mt-2" data-decision-id={question.id}>
      <label htmlFor={question.id} className="small fw-semibold">
        For names you don&apos;t decide above{questions.length > 1 ? ` (${state?.plan?.tables?.[question.table]?.name || 'table'})` : ''}
      </label>
      <p className="small text-muted mb-1">
        {summary.decided >= summary.labels && !summary.truncated && !summary.skipped_long?.labels
          ? 'Every name has a decision above, so this applies to no row.'
          : 'A Data Package scientificName holds only the name, without its author. Decide names above where you can.'}
      </p>
      <select id={question.id} className="form-select form-select-sm" value={decisions[question.id] || ''} disabled={disabled || !onChoose}
        onChange={event => onChoose(question.id, event.target.value)}>
        {!decisions[question.id] && <option value="">Choose…</option>}
        {question.options.map(option => <option key={option.value} value={option.value}>
          {fallbackLabel(option, Boolean(state?.plan?.columns?.find(column => column.id === question.id)?.verbatim_source))}</option>)}
      </select>
    </div>)}
    </div>
  </details>
}
