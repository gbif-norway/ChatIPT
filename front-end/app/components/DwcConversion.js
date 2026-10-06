'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import config from '../config'
import { getCsrfToken } from '../utils/csrf'
import { useDataset } from '../contexts/DatasetContext'
import { attentionItems, chatVisible, conflictsFor, optionState, shownRecommendation, unresolvedIssues } from '../utils/conversionReview.mjs'
import { conversionTitle, dedupeNotices, makeSelector } from '../utils/conversionPlan.mjs'
import { focusDecision } from '../utils/focusDecision'
import ConversionAiDecisions from './ConversionAiDecisions'
import ConversionChat from './ConversionChat'
import ConversionColumnSummary from './ConversionColumnSummary'
import ConversionNameReview from './ConversionNameReview'
import ConversionPlanDiagram from './ConversionPlanDiagram'
import ConversionSteps from './ConversionSteps'
import PackageExplorer, { openPackageExplorer } from './PackageExplorer'

async function request(url, body) {
  const headers = body ? { 'Content-Type': 'application/json', 'X-CSRFToken': await getCsrfToken() } : {}
  const response = await fetch(url, { credentials: 'include', headers, ...(body ? { method: 'POST', body: JSON.stringify(body) } : {}) })
  const data = await response.json()
  if (!response.ok) throw new Error(data.detail || (Array.isArray(data) ? data.join(' ') : JSON.stringify(data)))
  return data
}

export function ConversionUpload({ onDatasetCreated }) {
  const [files, setFiles] = useState([])
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const submit = async (event) => {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const body = new FormData()
      body.append('workflow_type', 'dwca_conversion'); body.append('title', title)
      files.forEach(file => body.append('files', file))
      const response = await fetch(`${config.baseUrl}/api/datasets/`, { method: 'POST', body, credentials: 'include', headers: { 'X-CSRFToken': await getCsrfToken() } })
      const data = await response.json()
      if (!response.ok) throw new Error(Array.isArray(data) ? data.join(' ') : JSON.stringify(data))
      onDatasetCreated(data.id)
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return <div className="container p-4 upload-page">
    <header className="upload-heading"><span className="eyebrow">Archive conversion</span><h1>Give your archive a new format</h1>
      <p>Upload a Darwin Core Archive. We’ll map its fields, help you review any ambiguous choices, and create a checked Data Package.</p></header>
    <form onSubmit={submit} className="card card-body gap-3">
      <label className="form-label">Title (optional)<input className="form-control mt-1" value={title} onChange={event => setTitle(event.target.value)} disabled={busy} aria-describedby="conversion-title-help" />
        <span id="conversion-title-help" className="form-text">Leave this blank to use the title in the archive&apos;s metadata, or the file name.</span></label>
      <label className="form-label">Archive or loose files<input type="file" multiple accept=".zip,.dwca,.csv,.tsv,.txt,.xml" className="form-control mt-1" disabled={busy} onChange={event => setFiles(Array.from(event.target.files || []))} /></label>
      <p className="small text-body-secondary mb-0">Add a ZIP, or select all the archive&apos;s files together. Include meta.xml and eml.xml if available. For loose tables, name the main table occurrence.csv, event.csv or taxon.csv.</p>
      {files.length > 0 && <ul>{files.map(file => <li key={file.name}>{file.name} · {(file.size / 1024).toFixed(0)} KB</li>)}</ul>}
      <small className="text-muted">Maximum 200 MB uploaded or expanded. Archives built around events, occurrences or taxa (a species checklist) are accepted. A checklist on its own produces a taxonomy data package; actual occurrence records can also be converted to the Data Package standard after you review them.</small>
      {error && <div className="alert alert-danger" role="alert">{error}</div>}
      <button className="btn btn-primary align-self-start" disabled={busy || !files.length}>{busy ? 'Uploading…' : 'Inspect my archive'}<i className="bi bi-arrow-right ms-2" aria-hidden="true" /></button>
    </form>
  </div>
}

const PAGE = 50

function GroupExceptions({ item, decisions, disabled, onChoose }) {
  const [page, setPage] = useState(0)
  const pages = Math.ceil(item.members.length / PAGE)
  const exceptions = item.members.filter(member => decisions[member]).length
  return <details className="small mt-2"><summary>Exceptions for individual rows{exceptions ? ` (${exceptions})` : ''}</summary>
    <p className="mb-1">Choosing something here for one row replaces the group choice for that row only.</p>
    {item.members.slice(page * PAGE, (page + 1) * PAGE).map((member, index) => <div className="row align-items-center my-1" key={member}>
      <label htmlFor={member} className="col-4">Row {item.rows[page * PAGE + index]}</label>
      <div className="col-8"><select id={member} className="form-select form-select-sm" value={decisions[member] || ''} disabled={disabled} onChange={event => onChoose(member, event.target.value)}>
        <option value="">Same as the group</option>{item.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
      </select></div></div>)}
    {pages > 1 && <div className="d-flex align-items-center gap-2 mt-2">
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
      <span>Rows {item.rows[page * PAGE]}–{item.rows[Math.min((page + 1) * PAGE, item.rows.length) - 1]} ({page + 1} of {pages})</span>
      <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page === pages - 1} onClick={() => setPage(page + 1)}>Next</button>
    </div>}
  </details>
}

function ChoiceCard({ item, state, decisions, disabled, onChoose, selected, number }) {
  const recommendation = shownRecommendation(state, item.id)
  const deferred = state.review?.deferred?.[item.id]
  const value = selected(item.id, item.default)
  const current = optionState(state, item.id, value)
  const conflicts = conflictsFor(state, item.id)
  const guided = number !== undefined
  const eventLinks = guided && item.id.startsWith('occurrence-events:')
  return <div data-decision-id={item.id} className={`card card-body choice-card mb-3 ${conflicts.length || !current.available ? 'border-warning' : ''}`}>
    {guided && <div className="d-flex align-items-center justify-content-between mb-2"><span className="choice-number mb-0">Question {number}</span>{!state.unresolved?.includes(item.id) && decisions[item.id] && <span className="answer-saved"><i className="bi bi-check-circle me-1" aria-hidden="true" />Answer saved</span>}</div>}
    {item.table !== undefined && <small className="text-muted mb-1">{state.plan.tables[item.table]?.name}</small>}
    {guided ? <h3 id={`title-${item.id}`} className="choice-title fw-semibold">{eventLinks ? 'How should these records link to events?' : item.title || item.term?.split('/').pop()}</h3>
      : <label id={`title-${item.id}`} htmlFor={item.id} className="choice-title fw-semibold">{item.title || item.term?.split('/').pop()}</label>}
    {eventLinks ? <><p className="small mb-2">In a Data Package, dates and locations belong to events. Choose how to keep the details recorded on these occurrence rows.</p><details className="small mb-2"><summary>See what we found in your files</summary><p className="mt-2 mb-0">{item.reason}</p></details></> : item.reason && <p className="small mb-2">{item.reason}</p>}
    {item.members && <p className="small mb-2">Applies to {item.count.toLocaleString()} rows that raise the same question (rows {item.sample_rows.join(', ')}{item.count > item.sample_rows.length ? ', …' : ''}).</p>}
    {item.samples?.length > 0 && <div className="small text-muted mb-2" style={{ overflowWrap: 'anywhere' }}>Examples: {item.samples.join(' · ')}</div>}
    {guided ? <fieldset id={item.id} className="choice-options" aria-labelledby={`title-${item.id}`}>
      <legend className="visually-hidden">Choose an answer</legend>
      {item.options.map((option, index) => {
        const availability = optionState(state, item.id, option.value)
        const suggested = recommendation?.option === option.value
        return <div key={option.value}><label className={`choice-option${value === option.value ? ' is-selected' : ''}${suggested ? ' is-suggested' : ''}${!availability.available ? ' is-unavailable' : ''}`}>
          <input type="radio" name={item.id} value={option.value} checked={value === option.value} disabled={disabled || !availability.available}
            onChange={() => onChoose(item.id, option.value, suggested ? { accepted_recommendations: [item.id] } : {})} aria-describedby={!availability.available ? `reason-${item.id}-${index}` : undefined} />
          <span>{option.label}{suggested && <small className="suggested-label"><i className="bi bi-stars me-1" aria-hidden="true" />Suggested by ChatIPT</small>}{!availability.available && <small id={`reason-${item.id}-${index}`} className="d-block text-body-secondary mt-1">Not available with your current data and choices.</small>}</span>
          {value === option.value && <i className="bi bi-check2 ms-auto" aria-hidden="true" />}
        </label>{!availability.available && availability.reasons?.length > 0 && <details className="small unavailable-reason"><summary>Why this option isn&apos;t available</summary><p className="mt-2 mb-0">{availability.reasons.join(' ')}</p></details>}</div>
      })}
    </fieldset> : <select id={item.id} className="form-select" value={decisions[item.id] ?? (item.default || '')} disabled={disabled} onChange={event => onChoose(item.id, event.target.value)}>
      {!item.default && <option value="">Choose…</option>}
      {item.options.map(option => <option key={option.value} value={option.value}>
        {option.label}{optionState(state, item.id, option.value).available ? '' : ' (not available with your other choices)'}</option>)}
    </select>}
    {guided && recommendation?.rationale && <details className="small mt-2"><summary>Why ChatIPT suggests this answer</summary><p className="mt-2 mb-0">{recommendation.rationale}</p></details>}
    {guided && (item.authority === 'user-assertion' || item.options.some(option => option.assertion)) && <p className="choice-confirmation small text-body-secondary mb-0 mt-2"><i className="bi bi-info-circle me-1" aria-hidden="true" />Some answers add information the files don&apos;t provide. Choose them only if they describe your data.</p>}
    {!current.available && <div className="small text-warning-emphasis mt-2">{current.reasons.join(' ')}</div>}
    {item.unavailable_options?.length > 0 && <details className="small mt-2"><summary>{item.unavailable_options.length} {item.unavailable_options.length === 1 ? 'option is' : 'options are'} not available for this data</summary>
      <ul className="mb-0">{item.unavailable_options.map(option => <li key={option.value}><strong>{option.label}:</strong> {option.reason}</li>)}</ul></details>}
    {item.members && <GroupExceptions item={item} decisions={decisions} disabled={disabled} onChoose={onChoose} />}
    {conflicts.map(conflict => <div key={conflict.id} className="small text-warning-emphasis mt-2">The last conversion stopped because of this choice: {conflict.reason}</div>)}
    {!guided && recommendation && <div className="small mt-2">Recommended: <strong>{recommendation.option_label}</strong>{recommendation.rationale ? ` — ${recommendation.rationale}` : ''}
      <button className="btn btn-sm btn-outline-primary ms-2" disabled={disabled || !optionState(state, item.id, recommendation.option).available}
        onClick={() => onChoose(item.id, recommendation.option, { accepted_recommendations: [item.id] })}>Use this recommendation</button>
    </div>}
    {deferred && <div className="small text-muted mt-2">AI review waits until “{state.plan.issues.find(issue => issue.id === deferred)?.title || deferred}” is answered.</div>}
  </div>
}

export default function DwcConversion() {
  const { currentDataset, refreshDataset } = useDataset()
  const [state, setState] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [pendingSaves, setPendingSaves] = useState(0)
  // Keep a card in place after it is answered, so keyboard focus and the user's place are preserved.
  const [answeredHere, setAnsweredHere] = useState([])
  const saves = useRef(0)
  const saving = useRef(Promise.resolve())
  const saveFailed = useRef(false)
  const refreshedTitleFor = useRef(null)
  const datasetId = currentDataset?.id
  const url = `${config.baseUrl}/api/datasets/${datasetId}/conversion/`
  const load = useCallback(async () => {
    try { const data = await request(url); saveFailed.current = false; setState(data); setError('') } catch (err) { setError(err.message) }
  }, [url])
  useEffect(() => { if (datasetId) load() }, [datasetId, load])
  useEffect(() => { setDecisions(state?.decisions || {}) }, [state?.plan?.id, state?.decisions])
  // The server fills an empty title from the archive's metadata once the files are inspected. Fetch it once per plan.
  useEffect(() => {
    const planId = state?.plan?.id
    if (!planId || currentDataset?.title || refreshedTitleFor.current === planId) return
    refreshedTitleFor.current = planId
    refreshDataset?.(datasetId)
  }, [state?.plan?.id, currentDataset?.title, datasetId, refreshDataset])
  // Choices stay editable while the AI reviews (status `reviewing`); only inspection and conversion lock them.
  const working = ['queued', 'inspecting', 'converting'].includes(state?.status)
  const polling = working || state?.status === 'reviewing' || Boolean(state?.chat?.pending)
  useEffect(() => {
    if (!polling) return
    const interval = setInterval(load, 3000)
    return () => clearInterval(interval)
  }, [polling, load])
  const perform = async (action) => {
    setBusy(true); setError('')
    try {
      await saving.current  // Convert only after every pending change is saved.
      if (saveFailed.current) throw new Error('Your last change was not saved. Check the choices below before converting.')
      setState(await request(url, { action, plan_id: state?.plan?.id }))
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  const send = async (body) => {
    await saving.current
    if (saveFailed.current) throw new Error('Your last change could not be saved. Reload before sending another answer.')
    setState(await request(url, body))
  }
  // Every change is saved, so the server's unresolved list and option availability stay current.
  // Saves send only the changed choice, so choices the AI reviewer applied meanwhile are kept.
  const save = (changes, extra = {}) => {
    const sequence = ++saves.current
    setPendingSaves(count => count + 1)
    saving.current = saving.current.then(async () => {
      try {
        const saved = await request(url, { action: 'save', plan_id: state?.plan?.id, changes, ...extra })
        saveFailed.current = false
        if (sequence === saves.current) { setState(saved); setError('') }
      } catch (err) {
        // Show the server's choices again, so the form never displays an unsaved change as current.
        // Converting stays blocked until the form shows what the server holds.
        saveFailed.current = true
        setError(err.message)
        try { setState(await request(url)); saveFailed.current = false } catch { /* Stay blocked until a reload succeeds. */ }
      } finally { setPendingSaves(count => count - 1) }
    })
  }
  const choose = (id, value, extra = {}) => {
    const next = { ...decisions, [id]: value }
    if (!value) delete next[id]
    setDecisions(next)
    save({ [id]: value || null }, extra)
  }
  const keep = id => save({}, { confirm: [id] })
  const automaticChoices = state?.plan?.automatic_choices || []
  const selected = makeSelector(state, decisions)
  // The server's unresolved list accounts for retained rows; hide choices under retained tables or columns.
  const retainedIssue = (issue) => selected(`table:${issue.table}`) === 'preserve' ||
    (issue.source_column != null && selected(`column:${issue.table}:${issue.source_column}`) === 'preserve')
  const unresolved = unresolvedIssues(state)
  const attention = attentionItems(state)
  const outstanding = unresolved.length + attention.length
  const blockers = state?.review?.blockers || []
  const unlinkedExtensionConflict = state?.conflicts?.find(conflict =>
    conflict.evidence?.kind === 'unlinked-extension-records')
  const unlinkedExtensionTables = (unlinkedExtensionConflict?.evidence?.tables || []).map(table => ({
    ...table,
    columns: [...new Set((table.records || []).flatMap(record =>
      (record.values || []).map(value => value.field)))].slice(0, 8),
  }))
  const reviewable = state?.review?.reviewable || 0
  const disabled = busy || working
  const inReview = ['review', 'reviewing'].includes(state?.status)
  const needsInput = new Set([...unresolved.map(issue => issue.id), ...(state?.review?.escalated || []), ...(state?.conflicts || []).flatMap(conflict => conflict.decision_ids || [])])
  const scientific = state?.status === 'complete'
    ? state.report?.event_hierarchy?.scientific_consistency : state?.plan?.scientific_hierarchy
  const nestedScientific = state?.status === 'complete'
    ? Object.entries(state.report?.taxonomy?.event_hierarchies || {}).map(([index, hierarchy]) => [index, hierarchy.scientific_consistency])
    : Object.entries(state?.plan?.taxonomy?.scientific_hierarchies || {})
  const scientificAudits = [...(scientific ? [['core', scientific]] : []), ...nestedScientific].filter(([, audit]) => audit)
  const notices = dedupeNotices(state, state?.status === 'complete' ? state.report?.warnings || [] : state?.plan?.warnings || [], selected)
  const valueLedger = state?.report?.value_disposition?.source_terms || []
  const semanticFindings = state?.report?.semantic_value_audit?.findings || []
  const reviewedValueRoutes = state?.report?.reviewed_value_routes || []
  const agentRoles = state?.report?.agent_roles
  const agentRoleLinks = Object.values(agentRoles?.roles_created || {}).reduce((sum, count) => sum + count, 0)
  const cardProps = { state, decisions, disabled, onChoose: choose, selected }
  // scientificName questions are answered inside the name check when it has names to check.
  const nameQuestionIds = new Set(state?.name_review?.question_ids || [])
  const nameQuestions = (state?.plan?.issues || []).filter(issue => nameQuestionIds.has(issue.id) && !retainedIssue(issue))
  const needsInputIssues = (state?.plan?.issues || []).filter(issue => needsInput.has(issue.id) && !nameQuestionIds.has(issue.id) &&
    !retainedIssue(issue) && !(state?.review?.applied || []).includes(issue.id))
  const title = conversionTitle(currentDataset, state)
  const openItems = [...new Map([...attention, ...needsInputIssues].map(item => [item.id, item])).values()]
  const openIds = new Set(openItems.map(item => item.id))
  const choiceOrder = [...(state?.plan?.issues || []), ...automaticChoices, ...(state?.plan?.columns || [])]
  const answeredItems = [...new Map(choiceOrder.filter(item => answeredHere.includes(item.id) && !openIds.has(item.id) && !retainedIssue(item)).map(item => [item.id, item])).values()]
  const guidedItems = [...openItems, ...answeredItems].sort((a, b) => choiceOrder.findIndex(item => item.id === a.id) - choiceOrder.findIndex(item => item.id === b.id))
  return <div className="container conversion-workspace">
    <header className="workspace-heading">
      <span className="eyebrow"><i className="bi bi-arrow-left-right me-2" aria-hidden="true" />Archive conversion</span>
      <h1>{title}</h1>
      <p className="text-body-secondary mb-0">Darwin Core Archive <i className="bi bi-arrow-right mx-2" aria-hidden="true" /><span className="visually-hidden">to </span>{state?.status === 'complete' && state.report?.output_format === 'taxonomy-data-package' ? 'Taxonomy data package' : 'Darwin Core Data Package'}</p>
    </header>
    <ConversionSteps state={state} />
    {error && <div className="alert alert-danger" role="alert">{error}<button className="btn btn-sm btn-outline-danger ms-2" onClick={load}>Reload</button></div>}
    {state?.status === 'blocked' && <div className="alert alert-danger" role="alert">
      <h2 className="h5">Unable to process this archive</h2>
      <p className="mb-2">{(state.error || '').replace(/^Unable to process this archive(?: because|:)?\s*/i, '')}</p>
      {unlinkedExtensionConflict ? <>
        <p className="small">First three unlinked records from each extension table:</p>
        {unlinkedExtensionTables.map((table, tableIndex) => <section className="mb-3" key={`${table.name}:${tableIndex}`}>
          <h3 className="h6 mb-1">{table.name}</h3>
          <p className="small mb-1">{table.count.toLocaleString()} unlinked {table.count === 1 ? 'record' : 'records'}</p>
          <div className="table-responsive">
            <table className="table table-sm table-bordered small mb-0">
              <thead><tr>
                <th scope="col">Record</th>
                <th scope="col">Core link</th>
                {table.columns.map(field => <th scope="col" key={field}>{field}</th>)}
              </tr></thead>
              <tbody>{(table.records || []).map((record, recordIndex) => <tr key={`${record.file}:${record.data_record}:${recordIndex}`}>
                <th scope="row">{record.data_record}</th>
                <td><code style={{ overflowWrap: 'anywhere' }}>{record.core_link || '(blank)'}</code></td>
                {table.columns.map(field => <td key={field} style={{ minWidth: '10rem', overflowWrap: 'anywhere' }}>
                  {record.values?.find(value => value.field === field)?.value || '—'}
                </td>)}
              </tr>)}</tbody>
            </table>
          </div>
        </section>)}
        <p className="small">You can remove these unlinked records from the conversion. Your uploaded files, including these records, will still be kept in the download.</p>
        <button type="button" className="btn btn-primary" disabled={disabled} onClick={() => perform('drop_unlinked_extension_rows')}>
          Drop these records and continue
        </button>
      </> : <p className="small mb-0">Correct the source files and start a new conversion.</p>}
    </div>}
    {state?.status === 'failed' && <div className="alert alert-danger" role="alert">
      {state.retryable && state.conflicts?.[0]?.category !== 'internal' ? 'A temporary problem interrupted this conversion.' : 'The converter hit an internal problem. It has been logged.'} {state.error}
      {state.retryable && <button className="btn btn-sm btn-primary ms-2" disabled={disabled} onClick={() => perform(state.plan?.id ? 'convert' : 'inspect')}>Try again</button>}
    </div>}
    {state?.status === 'review' && state.error && !state.conflicts?.length && <div className="alert alert-warning" role="alert">{state.error}</div>}
    {state?.status === 'review' && state.conflicts?.map(conflict => <div key={conflict.id} className="alert alert-warning" role="alert">
      {conflict.category === 'stale-plan' ? <>{conflict.reason} <button className="btn btn-sm btn-outline-secondary ms-2" disabled={disabled} onClick={() => perform('inspect')}>Inspect again</button></>
        : conflict.category === 'transient' ? <>{conflict.reason} You can convert again.</>
        : conflict.category === 'internal' ? <>The converter hit an internal problem, which has been logged: {conflict.reason} Your choices are kept, so you can convert again, for example after a fix is released.</>
        : <>The conversion stopped: {conflict.reason} The highlighted choices below can fix this.</>}
    </div>)}
    {!state && <p>Loading conversion…</p>}
    {working && <p role="status"><span className="spinner-border spinner-border-sm me-2" />{state.status === 'converting' ? 'Converting and checking your data…' : 'Inspecting your files…'} You can leave and return while this runs.</p>}
    {state?.status === 'reviewing' && <p role="status"><span className="spinner-border spinner-border-sm me-2" />The AI reviewer is checking the remaining choices against your files. You can keep answering meanwhile.</p>}
    {inReview && state.review?.error && <div className="alert alert-info small" role="status">{state.review.error}</div>}
    {state?.plan?.tables && <div className="conversion-layout">
    <div className="conversion-main">
    {inReview && <section className="review-section" aria-labelledby="conversion-options-heading">
      <div className="review-heading">
        <div><span className="eyebrow">Step 3 · Review</span>
          <h2 id="conversion-options-heading">{outstanding > 0 ? `${outstanding} ${outstanding === 1 ? 'choice' : 'choices'} to finish your conversion` : blockers.length ? 'Check the highlighted choices' : 'You’re ready to convert'}</h2>
          <p className="text-body-secondary">{outstanding > 0 ? 'Help us understand your data. Choose an answer below; each answer is saved automatically.' : blockers.length ? 'Review the AI choices below before continuing.' : 'Your choices are saved. Review them below or create your package.'}</p>
        </div>
        {reviewable > 0 && state.status === 'review' && <button className="btn btn-sm btn-outline-secondary" disabled={disabled} onClick={() => perform('review')}>
          <i className="bi bi-stars me-1" aria-hidden="true" />Review {reviewable} {reviewable === 1 ? 'choice' : 'choices'} with AI
        </button>}
      </div>
      {guidedItems.map((item, index) => <ChoiceCard key={item.id} item={item} number={index + 1} {...cardProps} onChoose={(...args) => {
        setAnsweredHere(ids => ids.includes(item.id) ? ids : [...ids, item.id])
        choose(...args)
      }} />)}
      <ConversionNameReview state={state} send={send} disabled={disabled} datasetId={datasetId} onRefresh={load}
        questions={nameQuestions} decisions={decisions} onChoose={choose} />
      <ConversionAiDecisions state={state} disabled={disabled} onChoose={choose} onKeep={keep} />
      {chatVisible(state) && <details className="review-disclosure chat-disclosure">
        <summary><i className="bi bi-chat-dots me-2" aria-hidden="true" /><span>Need help deciding?</span><small>Talk it through with ChatIPT</small></summary>
        <ConversionChat state={state} send={send} disabled={busy || working} onFocusDecision={focusDecision} />
      </details>}
      <details className="review-disclosure conversion-choices">
        <summary><i className="bi bi-sliders me-2" aria-hidden="true" /><span>All mapping choices</span><small>Advanced</small></summary>
        <p className="small text-body-secondary mt-3">Review or change the choices already made for your data. Values kept in the original files remain in your download.</p>
        {(state.plan.issues || []).filter(issue => !needsInput.has(issue.id) && !guidedItems.some(item => item.id === issue.id) && !nameQuestionIds.has(issue.id) && (!retainedIssue(issue) || issue.id === `table:${issue.table}`) && !(state.review?.applied || []).includes(issue.id))
          .map(issue => <ChoiceCard key={issue.id} item={issue} {...cardProps} />)}
        {automaticChoices.filter(choice => !state.plan.columns.some(column => column.id === choice.id) && !guidedItems.some(item => item.id === choice.id) && (!retainedIssue(choice) || choice.id === `table:${choice.table}`)).map(choice => <div className="my-3" key={choice.id} data-decision-id={choice.id}><label htmlFor={choice.id} className="small fw-semibold">{choice.title}</label><p className="small mb-1">{choice.reason}</p><select id={choice.id} className="form-select form-select-sm" disabled={disabled} value={selected(choice.id)} onChange={event => choose(choice.id, event.target.value)}>{choice.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>)}
        {state.plan.columns.filter(column => !column.review && !guidedItems.some(item => item.id === column.id) && selected(`table:${column.table}`) !== 'preserve').map(column => <div className="row align-items-center my-3" key={column.id} data-decision-id={column.id}><label htmlFor={column.id} className="col-md-6 small">{state.plan.tables[column.table].name} · {column.term.split('/').pop()}</label><div className="col-md-6"><select id={column.id} className="form-select form-select-sm" disabled={disabled} value={selected(column.id, column.default)} onChange={event => choose(column.id, event.target.value)}>{column.options.map(option => <option key={option.value} value={option.value}>{option.label}{optionState(state, column.id, option.value).available ? '' : ' (not available with your other choices)'}</option>)}</select></div></div>)}
      </details>
      <div className="conversion-action-bar">
        <div role="status" aria-live="polite"><strong>{pendingSaves ? 'Saving your answer…' : outstanding > 0 ? `${outstanding} ${outstanding === 1 ? 'choice' : 'choices'} remaining` : blockers.length ? `${blockers.length} AI ${blockers.length === 1 ? 'choice' : 'choices'} to check` : 'Ready for the next step'}</strong>
          <small>{saveFailed.current ? 'Could not save. Reload to try again.' : outstanding > 0 || blockers.length ? 'Answer the questions above to continue.' : 'Create and validate your converted package.'}</small></div>
        <button className="btn btn-primary" disabled={disabled || pendingSaves > 0 || saveFailed.current || outstanding > 0 || blockers.length > 0} onClick={() => perform('convert')}>Convert my data<i className="bi bi-arrow-right ms-2" aria-hidden="true" /></button>
      </div>
    </section>}
    {state?.status === 'complete' && <div className="card card-body">
      <h2>Your converted package is ready</h2>
      <p>All checks passed. The download contains the connected tables, a mapping report showing where each source row went, and every original file.</p>
      {state.report.output_format === 'taxonomy-data-package' && <p>This is a taxonomy data package containing your checklist and its attached tables. It has none of the standard Darwin Core Data Package tables.</p>}
      {state.report.output_format === 'dwc-dp' && state.report.taxonomy && <p>This Data Package has the standard occurrence tables, plus additional taxonomy tables that preserve your original checklist and its attached tables.</p>}
      <ul>{Object.entries(state.report.resources || {}).map(([name, count]) => <li key={name}>{name}: {count.toLocaleString()} {count === 1 ? 'row' : 'rows'}</li>)}</ul>
      <p className="small">{state.report.columns?.filter(column => column.disposition === 'retained-unmapped' && column.nonempty).length || 0} columns that contain data stay in your original files because they have no Darwin Core Data Package field.</p>
      {!!state.report.withheld_values?.length && <p className="small">{state.report.withheld_values.length.toLocaleString()} values were left out of the mapped tables because they did not meet the conversion rules. The report explains each one, and your original files still hold them.</p>}
      {semanticFindings.length > 0 && <details className="small mb-3"><summary>Source values that need a closer look ({semanticFindings.length})</summary>
        <ul className="mt-2">{semanticFindings.map(finding => <li key={finding.source_term}>
          <strong>{finding.source_term.split('/').pop()}</strong>: {finding.count.toLocaleString()} values. {finding.guidance}
          {finding.examples?.length > 0 && <span className="text-muted"> Examples: {finding.examples.slice(0, 3).map(example => `${example.source_table} row ${example.source_row}: ${example.value}`).join(' · ')}</span>}
        </li>)}</ul>
      </details>}
      {reviewedValueRoutes.length > 0 && <details className="small mb-3"><summary>Reviewed country labels and organism remarks ({reviewedValueRoutes.length})</summary>
        <ul className="mt-2">{reviewedValueRoutes.map(route => <li key={route.decision_id}>
          {route.source_table}: <strong>{route.source_value}</strong> ({route.count.toLocaleString()} source rows) → {route.target}
        </li>)}</ul>
      </details>}
      {agentRoles && (agentRoleLinks || agentRoles.unlinked_name_only) && <details className="small mb-3"><summary>People and organizations in Agent roles ({agentRoleLinks.toLocaleString()} links)</summary>
        <p className="mt-2 mb-1">Names without identifiers remain in their mapped text fields unless a repeated exact name was confirmed as one Agent. Composite names stay in the mapped text field and are listed in the report.</p>
        <p className="mb-0">New Agents: {(agentRoles.agents_created?.shared_name || 0).toLocaleString()} confirmed shared names, {(agentRoles.agents_created?.explicit_id || 0).toLocaleString()} explicit IDs. Unlinked name mentions: {(agentRoles.unlinked_name_only || 0).toLocaleString()}. Ambiguous mentions: {Object.values(agentRoles.skipped || {}).reduce((sum, count) => sum + count, 0).toLocaleString()}.</p>
      </details>}
      {valueLedger.length > 0 && <details className="small mb-3"><summary>Where each source column's values went ({valueLedger.length})</summary>
        <p className="text-muted mt-2">Counts refer to source values. Mapped and derived values can share a target row. Your download has the complete report and original files.</p>
        <div className="table-responsive"><table className="table table-sm"><thead><tr><th>Source</th><th>Mapped</th><th>Derived</th><th>Originals only</th><th>Withheld</th><th>Unverified</th></tr></thead><tbody>
          {valueLedger.map(row => <tr key={`${row.source_table_index}:${row.source_column}`}><td>{row.source_table} · {row.source_term.split('/').pop()}</td><td>{row.mapped_values.toLocaleString()}</td><td>{row.derived_values.toLocaleString()}</td><td>{row.originals_only_values.toLocaleString()}</td><td>{row.withheld_invalid_values.toLocaleString()}</td><td>{row.unverified_values.toLocaleString()}</td></tr>)}
        </tbody></table></div>
      </details>}
      {!!state.report.preserved_extension_rows?.length && <p className="small">{state.report.preserved_extension_rows.length.toLocaleString()} rows from additional tables stay in your original files. The report records the reason and any choice you made.</p>}
      {state.report.archive_validation?.warnings?.map(warning => <p key={warning} className="small text-muted">{warning}</p>)}
      <p className="small text-muted">{state.report.metadata?.eml}. The check covers structure and values; it cannot confirm that the meaning is unchanged.</p>
      <div className="d-flex flex-wrap gap-2">
        <a className="btn btn-primary" href={`${config.baseUrl}/api/datasets/${datasetId}/conversion-download/`}>{state.report.output_format === 'taxonomy-data-package' ? 'Download taxonomy data package' : 'Download Darwin Core Data Package'}</a>
        <button type="button" className="btn btn-outline-primary" onClick={openPackageExplorer}><i className="bi bi-diagram-3 me-2" aria-hidden="true" />Explore how your data connects</button>
      </div>
    </div>}
    <details className="review-disclosure conversion-details" >
      <summary><i className="bi bi-diagram-3 me-2" aria-hidden="true" /><span>Conversion details</span><small>{scientificAudits.some(([, audit]) => audit.has_findings) ? 'Includes hierarchy findings' : 'Files, columns & checks'}</small></summary>
      <div className="mt-3">
      {state.plan.taxonomy && <div className="alert alert-info">
        The Darwin Core Data Package standard has no table of its own for taxon (checklist) data. Your checklist and any tables attached to it are stored as additional taxonomy tables that link back to your original files.
        A checklist on its own produces a taxonomy data package. If your archive also holds actual occurrence records, those can be converted to the standard tables too.
      </div>}
      {state.status !== 'complete' && <ConversionPlanDiagram state={state} selected={selected} />}
      <details className="mb-4">
        <summary>Files and tables found ({state.plan.tables.length})</summary>
        <p className="small text-body-secondary mt-2 mb-2">An archive has one main table, plus optional additional tables linked to it (for example measurements or media).</p>
        <div className="table-responsive"><table className="table table-sm"><thead><tr><th>Source table</th><th>What it is</th><th>Linked to the main table by</th><th>Rows</th><th>Columns</th></tr></thead><tbody>
          {state.plan.tables.map((table, index) => <tr key={index}><td>{table.name}</td><td>{table.core ? 'Main table' : 'Additional table'} · {table.row_type.split('/').pop() || 'Unrecognised type'}</td><td>{table.join_basis?.split('/').pop()}</td><td>{table.rows.toLocaleString()}</td><td>{table.columns.length}</td></tr>)}
        </tbody></table></div>
        <p className="small text-muted">{state.plan.taxonomy ? 'Taxonomy data package; converting occurrence records uses' : 'Target:'} Darwin Core Data Package (DwC-DP) version {state.plan.schema.version}. {state.plan.files.length} original files are kept.</p>
      </details>
      {state.status !== 'complete' && <ConversionColumnSummary state={state} selected={selected} />}
    {scientificAudits.map(([index, scientific]) => {
      const scientificFindings = scientific.finding_sample || (scientific.checks || []).filter(check =>
        ['contradiction', 'conflict-signal', 'reporting-gap'].includes(check.status) ||
        ['survey-ambiguous', 'event-source-ambiguity'].includes(check.kind)).slice(0, 10)
      return <div key={index} className={`alert ${scientific.has_findings ? 'alert-warning' : 'alert-info'}`}>
      <p className="fw-semibold mb-1">Do parent events and their child events agree?</p>
      {index !== 'core' && <p className="small mb-1">Source occurrence table: {state.plan.tables[index]?.name}</p>}
      <p className="small mb-1">ChatIPT compared what your files say about dates, locations and survey details for each child event with its parent event, including fields kept without mapping. If information is missing or cannot be compared, the link between parent and child is left unchecked.</p>
      <ul className="small mb-2">{Object.entries(scientific.counts || {}).map(([status, count]) => <li key={status}>{status.replaceAll('-', ' ')}: {count.toLocaleString()}</li>)}</ul>
      {scientific.has_findings && <p className="small mb-1">Some parent and child events disagree or cannot be compared. These findings are listed in the report and do not stop the conversion. Keeping your parent-child links does not confirm that they are correct.</p>}
      {scientificFindings.length > 0 && <details className="small mb-2"><summary>Findings in your files (up to 10 shown)</summary><ul>{scientificFindings.map((finding, index) => <li key={index}>{finding.child_eventID} → {finding.ancestor_eventID || finding.parent_eventID}: {finding.reason}</li>)}</ul></details>}
      <p className="small mb-0">No survey scopes, completeness flags or other values are copied from parent to child or repaired. The package check covers structure and values only. <a href="https://eco.tdwg.org/hierarchy/" target="_blank" rel="noreferrer">TDWG guidance on event hierarchies</a></p>
    </div>})}
    {notices.length > 0 && <div className="alert alert-info">
      <p className="mb-1">{notices.length} {notices.length === 1 ? 'notice' : 'notices'} about this conversion. Your original files keep every source value; the report explains what could not be mapped and which values were left out of the mapped tables.</p>
      <details className="small"><summary>View notices</summary><ul>{notices.map((notice, index) => <li key={`${notice.id}:${index}`}><strong>{notice.title}:</strong> {notice.reason}</li>)}</ul></details>
    </div>}
      </div>
    </details>
    </div>
    <aside className="conversion-sidebar" aria-label="Conversion summary">
      <section className="card card-body conversion-summary">
        <span className="eyebrow">Your archive</span>
        <h2 className="h5">A new home for your data</h2>
        <p className="small text-body-secondary">{state.plan.taxonomy ? 'Your checklist is kept in additional taxonomy tables. Occurrence records, if included, can also fill standard Data Package tables.' : 'We’ll turn your archive into connected tables in a Darwin Core Data Package.'}</p>
        <ul className="source-file-list">{state.plan.tables.map((table, index) => <li key={index}><i className="bi bi-file-earmark-spreadsheet" aria-hidden="true" /><span><strong>{table.name}</strong><small>{table.rows.toLocaleString()} rows · {table.columns.length} columns</small></span></li>)}</ul>
        <div className="originals-note"><i className="bi bi-shield-check" aria-hidden="true" /><div><strong>Your originals stay safe</strong><p className="small mb-0">Every original file is included in your download, even when a column can’t be mapped.</p></div></div>
      </section>
      <p className="small text-body-secondary sidebar-help">You can leave and come back at any time. Your saved choices will be here.</p>
    </aside>
    </div>}
    {state?.status === 'complete' && <PackageExplorer datasetId={datasetId} />}
  </div>
}
