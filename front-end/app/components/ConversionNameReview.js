'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import config from '../config'
import {
  PAGE_SIZE, applyLabel, bulkBody, carriedMessage, checkMessage, choiceGroups, choiceLabel, decisionBody,
  decisionResult, groupOptions, groupQuery, groupSubtitle, groupTitle, isChecking, isEditable, needsRowDecision, pageCount,
  previewResult, progress, reasonText, replacementWarning, rowStatus, skippedMessage, stemLabel, unconfirmedMessage,
  undoAutoBody, undoBatchBody,
} from '../utils/conversionNames.mjs'

function NameProgress({ summary }) {
  const value = progress(summary)
  return <div className="name-review-progress mb-3" aria-label={value.text}>
    <div className="progress" role="progressbar" aria-label="Name review progress" aria-valuenow={value.percent} aria-valuemin="0" aria-valuemax="100">
      <div className="progress-bar" style={{ width: `${value.percent}%` }} />
    </div>
    <div className="small text-muted mt-1">{value.text}</div>
  </div>
}

function RowChoices({ entry, busy, onDecide }) {
  const [more, setMore] = useState(false)
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
  const submit = async (kind, usageId, options) => {
    const failure = await onDecide(entry.label, kind, usageId, options)
    setRowError(failure || '')
    return !failure
  }
  const usageIdOf = choice => choice.decision === 'alternative' ? choice.usage.id : undefined
  const choose = choice => {
    if (!choice.replaces) return submit(choice.decision, usageIdOf(choice))
    openerRef.current = typeof document !== 'undefined' ? document.activeElement : null
    setConfirming(choice)
  }
  const cancel = () => { restoreFocus.current = true; setConfirming(null) }
  const error = rowError && <div className="text-danger small mt-1" role="alert">{rowError}</div>
  const { main, sameName, others } = choiceGroups(entry)
  const options = <>
    <div className="d-flex flex-wrap gap-1" role="group" aria-label={`Decision for ${entry.label}`}>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={busy} onClick={() => submit('mine')}>Keep my name</button>
      {entry.offers?.parsed && <button type="button" className="btn btn-sm btn-outline-secondary" disabled={busy} onClick={() => submit('parsed')}>Use split</button>}
      {main && <button type="button" className="btn btn-sm btn-outline-primary" disabled={busy} onClick={() => choose(main)}>
        {main.replaces ? `Use ${main.usage.scientificName}…` : 'Use COL name'}
      </button>}
      {(entry.kind === 'uncertain' || entry.stem) && entry.stem?.scientificName && <button type="button" className="btn btn-sm btn-outline-primary" disabled={busy} onClick={() => submit('stem')}>
        {stemLabel(entry)}
      </button>}
      <button type="button" className="btn btn-sm btn-link" aria-expanded={more} onClick={() => setMore(!more)}>{more ? 'Less' : 'More…'}</button>
    </div>
    {sameName.length > 0 && <div className="mt-2">
      <div className="small text-muted">Same-name options in Catalogue of Life:</div>
      <div className="d-flex flex-wrap gap-1 mt-1">{sameName.map(choice => <button key={choice.usage.id} type="button" className="btn btn-sm btn-outline-secondary text-start" disabled={busy} onClick={() => choose(choice)}>
        Use {choiceLabel(choice)}{choice.rank_note ? ` — ${choice.rank_note}` : ''}</button>)}</div>
    </div>}
    {main?.corrects && <div className="small text-muted mt-1">COL {main.corrects}.</div>}
    {confirming && <div className="alert alert-warning small py-2 mt-2 mb-0" role="alert">
      {replacementWarning(confirming, entry.rows)}
      <div className="d-flex flex-wrap gap-1 mt-1">
        <button ref={confirmRef} type="button" className="btn btn-sm btn-warning" disabled={busy}
          onClick={async () => { if (await submit(confirming.decision, usageIdOf(confirming), { confirmCoarser: true })) setConfirming(null) }}>Replace with {confirming.usage.scientificName}</button>
        <button type="button" className="btn btn-sm btn-link" onClick={cancel}>Cancel</button>
      </div>
    </div>}
    {error}
    {more && <div className="d-flex flex-wrap align-items-center gap-2 mt-2">
      {others.length > 0 && <select className="form-select form-select-sm name-review-other" aria-label={`Other COL names for ${entry.label}`} value="" disabled={busy}
        onChange={event => { const choice = others.find(item => String(item.usage.id) === event.target.value); if (choice) choose(choice) }}>
        <option value="">Other COL name…</option>{others.map(choice => <option key={choice.usage.id} value={choice.usage.id}>{choiceLabel(choice)}{choice.replaces ? ` — ${choice.replaces.text}` : ''}</option>)}
      </select>}
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={busy} onClick={() => submit('empty')}>Don&apos;t publish a name</button>
    </div>}
  </>
  return options
}

function NameRow({ entry, editable, busy, selectedOption, undecidedChip = false, onDecide }) {
  // Options start open for a name the group decision does not cover; the user's own toggle wins after that.
  const [toggled, setOpen] = useState(null)
  const status = rowStatus(entry)
  const held = Boolean(entry.decision_unconfirmed || entry.decision_held)
  const result = held ? status.text : decisionResult(entry.decision)
  const needsDecision = (undecidedChip && !entry.decision) || needsRowDecision(entry, selectedOption)
  const open = toggled ?? needsDecision
  const preview = previewResult(entry, selectedOption)
  const undo = () => onDecide(entry.label, null)
  return <article className="name-review-row py-3">
    <div className="d-flex flex-wrap justify-content-between align-items-start gap-2">
      <div className="min-w-0 flex-grow-1">
        <div className="fw-semibold text-break">{entry.label}</div>
        <div className="small text-muted">{entry.rows.toLocaleString()} {entry.rows === 1 ? 'row' : 'rows'}</div>
        {reasonText(entry) && <div className="small text-muted mt-1">{reasonText(entry)}</div>}
        <div className="small mt-1">→ {result || (preview
          ? <span className="text-muted" title="What the group decision above would write">{preview} <span className="fst-italic">(when you apply)</span></span>
          : <span className="text-muted">No decision yet</span>)}</div>
      </div>
      <div className="d-flex align-items-center flex-wrap gap-2">
        {needsDecision && <span className="badge rounded-pill text-bg-light">Decide below</span>}
        {status.text && !held && <span className="small text-muted">{status.text}</span>}
        {editable && entry.decision && !held && <button type="button" className="btn btn-sm btn-link p-0" disabled={busy} onClick={undo}>Undo</button>}
        {editable && <button type="button" className="btn btn-sm btn-outline-secondary" aria-expanded={open} onClick={() => setOpen(!open)}>Change <span aria-hidden="true">▾</span></button>}
      </div>
    </div>
    {held ? <div className="mt-2">
      <div className="small text-danger-emphasis">{entry.decision_held?.kind === 'authorship'
        ? 'This earlier choice would replace your authorship with Catalogue of Life’s, so your name is kept until you confirm it.'
        : 'This earlier choice replaces your name with a coarser or different taxon, so your name is kept until you confirm it.'}</div>
      {editable && <div className="d-flex flex-wrap gap-2 mt-1">
        <button type="button" className="btn btn-sm btn-outline-warning" disabled={busy}
          onClick={() => onDecide(entry.label, entry.decision.decision, entry.decision.decision === 'alternative' ? entry.decision.usageId : undefined, { confirmCoarser: true })}>Confirm {entry.decision.scientificName}</button>
        <button type="button" className="btn btn-sm btn-success" disabled={busy} onClick={() => onDecide(entry.label, 'mine')}>Keep my name</button>
      </div>}
    </div> : null}
    {editable && open && <div className="name-review-row-options mt-2">{optionsContent(entry, busy, onDecide, setOpen)}</div>}
  </article>
}

function optionsContent(entry, busy, onDecide, setOpen) {
  return <RowChoices entry={entry} busy={busy} onDecide={async (...args) => {
    const error = await onDecide(...args)
    if (!error && args[1]) setOpen(false)
    return error
  }} />
}

function NameRows({ group, expanded, updated, datasetId, url, editable, busy, selectedOption, onDecide, searchable = false, undecidedChip = false }) {
  const [rows, setRows] = useState([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(0)
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  useEffect(() => {
    if (!expanded || !datasetId) return undefined
    const controller = new AbortController()
    ;(async () => {
      try {
        const queryString = groupQuery({ group: group.id, offset: page * PAGE_SIZE, q: query })
        const response = await fetch(`${url}?${queryString}`, { credentials: 'include', signal: controller.signal })
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail || 'The names could not be loaded.')
        setRows(data.name_review.labels || [])
        setTotal(data.name_review.page?.total || 0)
        setError('')
      } catch (err) { if (err.name !== 'AbortError') setError(err.message) }
    })()
    return () => controller.abort()
  }, [datasetId, expanded, group.id, page, query, updated, url])
  const pages = pageCount(total)
  return <>
    {searchable && <label className="visually-hidden" htmlFor={`name-search-${group.id}`}>Search names</label>}
    {searchable && <input id={`name-search-${group.id}`} type="search" maxLength={100} className="form-control form-control-sm mb-2" placeholder="Search names" value={query} onChange={event => { setPage(0); setQuery(event.target.value) }} />}
    {error && <div className="alert alert-danger small py-2" role="alert">{error}</div>}
    {!rows.length ? <p className="small text-muted mb-0">{query ? 'No names match your search.' : 'No names in this group.'}</p>
      : <div className="name-review-rows">{rows.map(entry => <NameRow key={entry.label} entry={entry} editable={editable} busy={busy} selectedOption={selectedOption} undecidedChip={undecidedChip} onDecide={onDecide} />)}</div>}
    {pages > 1 && <nav className="d-flex align-items-center gap-2 small mt-2" aria-label={`${groupTitle(group)} pages`}>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
      <span>Page {page + 1} of {pages}</span>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page >= pages - 1} onClick={() => setPage(page + 1)}>Next</button>
    </nav>}
  </>
}

function GroupDecisionBar({ group, planId, busy, editable, send, summary, selected, onSelect }) {
  const options = groupOptions(group)
  const selectedOption = options.find(option => option.decision === selected)
  const lastBatch = summary.last_batch?.group === group.id ? summary.last_batch : null
  const apply = async () => {
    try { await send(bulkBody(planId, group.id, selected)) } catch (err) { return err }
  }
  const undo = async () => {
    try { await send(undoBatchBody(planId, lastBatch.id)) } catch (err) { return err }
  }
  return <>
    {options.length > 0 && <fieldset className="name-review-decision-bar mb-2">
      <div className="d-flex flex-wrap align-items-center gap-2">
        <legend className="small fw-semibold mb-0 me-1">Your decision</legend>
        {options.map(option => <label key={option.decision} className="form-check form-check-inline mb-0">
          <input className="form-check-input" type="radio" name={`name-group-${group.id}`} value={option.decision} checked={selected === option.decision} disabled={!editable || busy || !option.eligible} onChange={() => onSelect(option.decision)} />
          <span className={`form-check-label small${option.eligible ? '' : ' text-muted'}`}>{option.label}{option.eligible ? '' : ' (decide each name below)'}</span>
        </label>)}
        <button type="button" className="btn btn-sm btn-primary ms-auto" disabled={!editable || busy || !selectedOption?.eligible} onClick={apply}>{selectedOption ? applyLabel(selectedOption.eligible) : 'Choose a decision'}</button>
      </div>
    </fieldset>}
    {lastBatch && <div className="small text-success mb-2" role="status" aria-live="polite">Applied to {lastBatch.count.toLocaleString()} names · <button type="button" className="btn btn-link btn-sm p-0" disabled={busy} onClick={undo}>Undo</button></div>}
  </>
}

function NameGroup({ group, summary, planId, url, datasetId, updated, editable, busy, send, onDecide, defaultOpen }) {
  const [expanded, setExpanded] = useState(defaultOpen)
  const [selected, setSelected] = useState(group.default || '')
  const selectedOption = selected
  return <section className="name-review-group">
    <div className="d-flex flex-wrap justify-content-between align-items-center gap-2 mb-2">
      <div><h3 className="h6 mb-0">{groupTitle(group)} <span className="text-muted fw-normal">· {group.labels.toLocaleString()} {group.labels === 1 ? 'name' : 'names'}</span></h3>
        {groupSubtitle(group) && <div className="small text-muted mt-1">{groupSubtitle(group)}</div>}</div>
      <button type="button" className="btn btn-sm btn-outline-secondary" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? 'Hide names' : 'Review names'}</button>
    </div>
    <GroupDecisionBar group={group} planId={planId} busy={busy} editable={editable} send={send} summary={summary} selected={selected} onSelect={setSelected} />
    {expanded && <NameRows group={group} expanded={expanded} updated={updated} datasetId={datasetId} url={url} editable={editable} busy={busy} selectedOption={selectedOption} onDecide={onDecide} />}
  </section>
}

function PreAcceptedGroup({ group, summary, planId, url, datasetId, updated, editable, busy, send, onDecide }) {
  const [expanded, setExpanded] = useState(false)
  const [selected, setSelected] = useState(group.default || groupOptions(group)[0]?.decision || '')
  const verb = group.kind === 'auto' ? 'exact Catalogue of Life matches' : 'published as the genus or family'
  const undoAll = async () => { try { await send(undoAutoBody(planId, group.kind)) } catch (err) { return err } }
  return <section className="name-review-preaccepted">
    <div className="d-flex flex-wrap align-items-center gap-2">
      <button type="button" className="btn btn-link p-0 text-start" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
        <span aria-hidden="true">{expanded ? '▾' : '▸'}</span> {groupTitle(group)} · {group.labels.toLocaleString()} {group.labels === 1 ? 'name' : 'names'} · {verb}
      </button>
      <button type="button" className="btn btn-sm btn-outline-secondary" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>{expanded ? 'Hide' : 'Review'}</button>
      {group.auto > 0 && <button type="button" className="btn btn-sm btn-link" disabled={!editable || busy} onClick={undoAll}>Undo all</button>}
    </div>
    {group.kind === 'auto' && group.authorship_kept > 0 && <div className="small text-muted ms-3">{group.authorship_kept.toLocaleString()} kept your authorship because Catalogue of Life&apos;s differs.</div>}
    {group.undecided > 0 && <GroupDecisionBar group={group} planId={planId} busy={busy} editable={editable} send={send} summary={summary} selected={selected} onSelect={setSelected} />}
    {expanded && <div className="mt-2">
      <NameRows group={group} expanded={expanded} updated={updated} datasetId={datasetId} url={url} editable={editable} busy={busy} selectedOption={selected} onDecide={onDecide} searchable undecidedChip />
    </div>}
  </section>
}

const fallbackLabel = (option, partial) => option.value === 'preserve'
  ? (partial ? 'Leave scientificName empty (the text fills verbatimIdentification wherever that would otherwise be empty)'
    : 'Leave scientificName empty (the text stays in verbatimIdentification)')
  : `Copy the supplied text into scientificName${option.value.startsWith('identification.') ? ' (identification)' : ''}`

export default function ConversionNameReview({ state, send, disabled, datasetId, onRefresh, questions = [], decisions = {}, onChoose }) {
  const nameReview = state?.name_review
  const editable = isEditable(state)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
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
  const actRow = useCallback(async (label, kind, usageId, options) => {
    try { await act(decisionBody(planId, label, kind, usageId, options)); return null } catch (err) { return err.message }
  }, [act, planId])
  if (!nameReview?.summary?.labels && !skippedMessage(nameReview?.summary)) return null
  const { summary } = nameReview
  const groups = summary.groups || []
  const message = checkMessage(nameReview)
  const skipped = skippedMessage(summary)
  const held = unconfirmedMessage(summary)
  const needsReview = summary.decided < summary.labels || Boolean(held) || checking || summary.checked < summary.labels || nameReview.status === 'error' || Boolean(skipped) || questions.some(question => state.unresolved?.includes(question.id))
  return <details className="review-disclosure name-review" open={needsReview}>
    <summary><i className={`bi ${needsReview ? 'bi-flower1' : 'bi-check-circle'} me-2`} aria-hidden="true" /><span id="scientific-names-heading">Scientific names</span><small>{checking ? 'Checking…' : skipped ? 'Some names weren’t checked' : nameReview.status === 'error' ? 'Check interrupted' : `${summary.decided.toLocaleString()} of ${summary.labels.toLocaleString()} decided`}</small></summary>
    <div className="mt-3">
      {editable && (summary.checked < summary.labels || nameReview.status === 'error') && !checking && <button type="button" className="btn btn-sm btn-outline-secondary mb-2" disabled={disabled || busy} onClick={() => act({ action: 'check_names', plan_id: planId })}>Check names again</button>}
      <p className="small text-muted mb-2">Names are checked with the GBIF name parser and Catalogue of Life (COL). Only names are published; Catalogue of Life IDs stay in the report. Your original text always stays in verbatimIdentification and in your original files.</p>
      <NameProgress summary={summary} />
      {held && <div className="alert alert-warning small py-2" role="status">{held}</div>}
      {carriedMessage(nameReview.carried) && <p className="small text-muted">{carriedMessage(nameReview.carried)}</p>}
      {skipped && <p className="small text-warning-emphasis">{skipped}</p>}
      {summary.truncated > 0 && <p className="small text-muted">Only the {summary.labels.toLocaleString()} most frequent names are listed; {summary.truncated.toLocaleString()} others are converted as they are.</p>}
      {message && <div className={`alert alert-${message.variant} small py-2`} role="status">{message.spinner && <span className="spinner-border spinner-border-sm me-2" />}{message.text}</div>}
      {error && <div className="alert alert-danger small py-2" role="alert">{error}</div>}
      {groups.filter(group => !['auto', 'uncertain'].includes(group.kind)).map(group => <NameGroup key={group.id} group={group} summary={summary} planId={planId} url={url} datasetId={datasetId} updated={updated} editable={editable} busy={disabled || busy} send={act} onDecide={actRow} defaultOpen={group.labels <= 20} />)}
      {groups.filter(group => ['uncertain', 'auto'].includes(group.kind)).map(group => <PreAcceptedGroup key={group.id} group={group} summary={summary} planId={planId} url={url} datasetId={datasetId} updated={updated} editable={editable} busy={disabled || busy} send={act} onDecide={actRow} />)}
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
