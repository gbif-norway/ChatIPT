'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import config from '../config'
import { getCsrfToken } from '../utils/csrf'
import { useDataset } from '../contexts/DatasetContext'
import { attentionItems, chatVisible, conflictsFor, openQuestions, optionState, shownRecommendation, unresolvedIssues } from '../utils/conversionReview.mjs'
import { conversionTitle, dedupeNotices, makeSelector, statusLine } from '../utils/conversionPlan.mjs'
import { focusDecision } from '../utils/focusDecision'
import ConversionAiDecisions from './ConversionAiDecisions'
import ConversionChat from './ConversionChat'
import ConversionColumnSummary from './ConversionColumnSummary'
import ConversionNameReview from './ConversionNameReview'
import ConversionOpenQuestions from './ConversionOpenQuestions'
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
  return <div className="container p-4" style={{ maxWidth: 900 }}>
    <h1>Convert a Darwin Core Archive to a Data Package</h1>
    <p>Upload your archive as a ZIP file, or upload its files together. ChatIPT maps the fields it supports automatically, asks you only about what is ambiguous, and checks the converted package.</p>
    <p className="text-muted">Include meta.xml and eml.xml if you have them, because they describe how the files fit together. If you upload loose tables instead, name the main table occurrence.csv, event.csv or taxon.csv. Your original files, and any fields that cannot be mapped, are kept in the download.</p>
    <form onSubmit={submit} className="card card-body gap-3">
      <label className="form-label">Title (optional)<input className="form-control mt-1" value={title} onChange={event => setTitle(event.target.value)} disabled={busy} aria-describedby="conversion-title-help" />
        <span id="conversion-title-help" className="form-text">Leave this blank to use the title in the archive&apos;s metadata, or the file name.</span></label>
      <label className="form-label">Archive or loose files<input type="file" multiple accept=".zip,.dwca,.csv,.tsv,.txt,.xml" className="form-control mt-1" disabled={busy} onChange={event => setFiles(Array.from(event.target.files || []))} /></label>
      {files.length > 0 && <ul>{files.map(file => <li key={file.name}>{file.name} · {(file.size / 1024).toFixed(0)} KB</li>)}</ul>}
      <small className="text-muted">Maximum 200 MB uploaded or expanded. Archives built around events, occurrences or taxa (a species checklist) are accepted. A checklist on its own produces a taxonomy data package; actual occurrence records can also be converted to the Data Package standard after you review them.</small>
      {error && <div className="alert alert-danger" role="alert">{error}</div>}
      <button className="btn btn-primary align-self-start" disabled={busy || !files.length}>{busy ? 'Uploading…' : 'Inspect my files and prepare the mappings'}</button>
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

function ChoiceCard({ item, state, decisions, disabled, onChoose, selected }) {
  const recommendation = shownRecommendation(state, item.id)
  const deferred = state.review?.deferred?.[item.id]
  const value = selected(item.id, item.default)
  const current = optionState(state, item.id, value)
  const conflicts = conflictsFor(state, item.id)
  return <div data-decision-id={item.id} className={`card card-body mb-3 ${conflicts.length || !current.available ? 'border-warning' : ''}`}>
    {item.table !== undefined && <small className="text-muted mb-1">{state.plan.tables[item.table]?.name}</small>}
    <label htmlFor={item.id} className="fw-semibold">{item.title || item.term?.split('/').pop()}
      {item.authority === 'user-assertion' && <span className="badge text-bg-secondary ms-2">Needs your confirmation</span>}</label>
    {item.reason && <p className="small mb-2">{item.reason}</p>}
    {item.members && <p className="small mb-2">Applies to {item.count.toLocaleString()} rows that raise the same question (rows {item.sample_rows.join(', ')}{item.count > item.sample_rows.length ? ', …' : ''}).</p>}
    {item.samples?.length > 0 && <div className="small text-muted mb-2" style={{ overflowWrap: 'anywhere' }}>Examples: {item.samples.join(' · ')}</div>}
    <select id={item.id} className="form-select" value={decisions[item.id] ?? (item.default || '')} disabled={disabled} onChange={event => onChoose(item.id, event.target.value)}>
      {!item.default && <option value="">Choose…</option>}
      {item.options.map(option => <option key={option.value} value={option.value}>
        {option.label}{optionState(state, item.id, option.value).available ? '' : ' (not available with your other choices)'}</option>)}
    </select>
    {!current.available && <div className="small text-warning-emphasis mt-2">{current.reasons.join(' ')}</div>}
    {item.unavailable_options?.length > 0 && <details className="small mt-2"><summary>{item.unavailable_options.length} {item.unavailable_options.length === 1 ? 'option is' : 'options are'} not available for this data</summary>
      <ul className="mb-0">{item.unavailable_options.map(option => <li key={option.value}><strong>{option.label}:</strong> {option.reason}</li>)}</ul></details>}
    {item.members && <GroupExceptions item={item} decisions={decisions} disabled={disabled} onChoose={onChoose} />}
    {conflicts.map(conflict => <div key={conflict.id} className="small text-warning-emphasis mt-2">The last conversion stopped because of this choice: {conflict.reason}</div>)}
    {recommendation && <div className="small mt-2">Recommended: <strong>{recommendation.option_label}</strong>{recommendation.rationale ? ` — ${recommendation.rationale}` : ''}
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
  const saves = useRef(0)
  const saving = useRef(Promise.resolve())
  const saveFailed = useRef(false)
  const refreshedTitleFor = useRef(null)
  const datasetId = currentDataset?.id
  const url = `${config.baseUrl}/api/datasets/${datasetId}/conversion/`
  const load = useCallback(async () => {
    try { const data = await request(url); setState(data); setError('') } catch (err) { setError(err.message) }
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
  const send = async (body) => setState(await request(url, body))
  // Every change is saved, so the server's unresolved list and option availability stay current.
  // Saves send only the changed choice, so choices the AI reviewer applied meanwhile are kept.
  const save = (changes, extra = {}) => {
    const sequence = ++saves.current
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
      }
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
  // The server's unresolved list already accounts for retained rows; here only retained tables hide cards.
  const retainedIssue = (issue) => selected(`table:${issue.table}`) === 'preserve'
  const unresolved = unresolvedIssues(state)
  const attention = attentionItems(state)
  const outstanding = unresolved.length + attention.length
  const blockers = state?.review?.blockers || []
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
  const cardProps = { state, decisions, disabled, onChoose: choose, selected }
  const needsInputIssues = (state?.plan?.issues || []).filter(issue => needsInput.has(issue.id) && !retainedIssue(issue) && !(state?.review?.applied || []).includes(issue.id))
  // The questions panel (conversation and recommendations) and the choices list are two views of the same saved decisions.
  const showQuestions = chatVisible(state) || openQuestions(state).length > 0
  const title = conversionTitle(currentDataset, state)
  const packageExplorable = state?.status === 'complete' && state.report?.output_format === 'dwc-dp'
  return <div className="container p-4">
    <h1 className="mb-1">{title}</h1>
    <p className="text-body-secondary mb-3">Converting a Darwin Core Archive to a Darwin Core Data Package</p>
    <ConversionSteps state={state} />
    {error && <div className="alert alert-danger" role="alert">{error}<button className="btn btn-sm btn-outline-danger ms-2" onClick={load}>Reload</button></div>}
    {state?.status === 'blocked' && <div className="alert alert-danger" role="alert">
      <p className="fw-semibold mb-1">The files need correcting before they can be converted.</p>
      <p className="mb-1">{state.error}</p>
      <p className="small mb-0">No choice here can fix this. Correct the files and start a new conversion.</p>
    </div>}
    {state?.status === 'failed' && <div className="alert alert-danger" role="alert">
      {state.retryable ? 'A temporary problem interrupted this conversion.' : 'The converter hit an internal problem. It has been logged.'} {state.error}
      {state.retryable && <button className="btn btn-sm btn-primary ms-2" disabled={disabled} onClick={() => perform(state.plan?.id ? 'convert' : 'inspect')}>Try again</button>}
    </div>}
    {state?.status === 'review' && state.error && !state.conflicts?.length && <div className="alert alert-warning" role="alert">{state.error}</div>}
    {state?.status === 'review' && state.conflicts?.map(conflict => <div key={conflict.id} className="alert alert-warning" role="alert">
      {conflict.category === 'stale-plan' ? <>{conflict.reason} <button className="btn btn-sm btn-outline-secondary ms-2" disabled={disabled} onClick={() => perform('inspect')}>Inspect again</button></>
        : conflict.category === 'transient' ? <>{conflict.reason} You can convert again.</>
        : <>The conversion stopped: {conflict.reason} The highlighted choices below can fix this.</>}
    </div>)}
    {!state && <p>Loading conversion…</p>}
    {working && <p role="status"><span className="spinner-border spinner-border-sm me-2" />{state.status === 'converting' ? 'Converting and checking your data…' : 'Inspecting your files…'} You can leave and return while this runs.</p>}
    {state?.status === 'reviewing' && <p role="status"><span className="spinner-border spinner-border-sm me-2" />The AI reviewer is checking the remaining choices against your files. You can keep answering meanwhile.</p>}
    {inReview && state.review?.error && <div className="alert alert-info small" role="status">{state.review.error}</div>}
    {state?.plan?.tables && <>
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
    </>}
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
    {inReview && <section aria-labelledby="conversion-options-heading">
      <div className="d-flex flex-wrap gap-2 align-items-center mb-1"><h2 className="me-auto mb-0" id="conversion-options-heading">Conversion options</h2>
        {reviewable > 0 && state.status === 'review' && <button className="btn btn-outline-secondary" disabled={disabled} onClick={() => perform('review')}>
          Review {reviewable} {reviewable === 1 ? 'choice' : 'choices'} with AI
        </button>}
      </div>
      <p className="mb-1" role="status">{statusLine(state, outstanding, blockers.length)}</p>
      <p className="small text-body-secondary">Keeping something in your original files means its values are not lost. They just aren&apos;t mapped to a Darwin Core Data Package field.</p>
      <ConversionAiDecisions state={state} disabled={disabled} onChoose={choose} onKeep={keep} />
      {(attention.length > 0 || needsInputIssues.length > 0) && <h3 className="h5 mt-3">Needs your input</h3>}
      {attention.map(item => <ChoiceCard key={item.id} item={item} {...cardProps} />)}
      {needsInputIssues.map(issue => <ChoiceCard key={issue.id} item={issue} {...cardProps} />)}
      <ConversionNameReview state={state} send={send} disabled={disabled} datasetId={datasetId} onRefresh={load} />
      <div className="row g-3 align-items-start mb-3">
        {showQuestions && <div className="col-12 col-lg-6">
          <h3 className="h5">Questions about your data</h3>
          <ConversionOpenQuestions state={state} disabled={disabled} onChoose={choose} onFocusDecision={focusDecision} />
          {chatVisible(state) && <ConversionChat state={state} send={send} disabled={busy || working} onFocusDecision={focusDecision} />}
        </div>}
        <div className={showQuestions ? 'col-12 col-lg-6' : 'col-12'}>
          <details open={showQuestions} className="conversion-choices">
            <summary className="h5">All choices (advanced)</summary>
            <p className="small text-body-secondary mt-2">The same saved choices as the questions {showQuestions ? 'beside' : 'above'}. Change any of them here.</p>
            <div className={showQuestions ? 'conversion-choices-scroll' : ''}>
        {(state.plan.issues || []).filter(issue => !needsInput.has(issue.id) && (!retainedIssue(issue) || issue.id === `table:${issue.table}`))
          .map(issue => <ChoiceCard key={issue.id} item={issue} {...cardProps} />)}
        {automaticChoices.filter(choice => !state.plan.columns.some(column => column.id === choice.id) && (!retainedIssue(choice) || choice.id === `table:${choice.table}`)).map(choice => <div className="my-3" key={choice.id} data-decision-id={choice.id}><label htmlFor={choice.id} className="small fw-semibold">{choice.title}</label><p className="small mb-1">{choice.reason}</p><select id={choice.id} className="form-select form-select-sm" disabled={disabled} value={selected(choice.id)} onChange={event => choose(choice.id, event.target.value)}>{choice.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>)}
        {state.plan.columns.filter(column => !column.review && selected(`table:${column.table}`) !== 'preserve').map(column => <div className="row align-items-center my-2" key={column.id} data-decision-id={column.id}><label htmlFor={column.id} className="col-md-6 small">{state.plan.tables[column.table].name} · {column.term.split('/').pop()}</label><div className="col-md-6"><select id={column.id} className="form-select form-select-sm" disabled={disabled} value={selected(column.id, column.default)} onChange={event => choose(column.id, event.target.value)}>{column.options.map(option => <option key={option.value} value={option.value}>{option.label}{optionState(state, column.id, option.value).available ? '' : ' (not available with your other choices)'}</option>)}</select></div></div>)}
            </div>
          </details>
        </div>
      </div>
      <button className="btn btn-primary" disabled={disabled || outstanding > 0 || blockers.length > 0} onClick={() => perform('convert')}>Convert and check my data</button>
    </section>}
    {state?.status === 'complete' && <div className="card card-body">
      <h2>Your converted package is ready</h2>
      <p>All checks passed. The download contains the connected tables, a mapping report showing where each source row went, and every original file.</p>
      {state.report.output_format === 'taxonomy-data-package' && <p>This is a taxonomy data package containing your checklist and its attached tables. It has none of the standard Darwin Core Data Package tables.</p>}
      {state.report.output_format === 'dwc-dp' && state.report.taxonomy && <p>This Data Package has the standard occurrence tables, plus additional taxonomy tables that preserve your original checklist and its attached tables.</p>}
      <ul>{Object.entries(state.report.resources || {}).map(([name, count]) => <li key={name}>{name}: {count.toLocaleString()} {count === 1 ? 'row' : 'rows'}</li>)}</ul>
      <p className="small">{state.report.columns?.filter(column => column.disposition === 'retained-unmapped' && column.nonempty).length || 0} columns that contain data stay in your original files because they have no Darwin Core Data Package field.</p>
      {!!state.report.withheld_values?.length && <p className="small">{state.report.withheld_values.length.toLocaleString()} values were left out of the mapped tables because they did not meet the conversion rules. The report explains each one, and your original files still hold them.</p>}
      {!!state.report.preserved_extension_rows?.length && <p className="small">{state.report.preserved_extension_rows.length.toLocaleString()} rows from additional tables stay in your original files. The report records the reason and any choice you made.</p>}
      {state.report.archive_validation?.warnings?.map(warning => <p key={warning} className="small text-muted">{warning}</p>)}
      <p className="small text-muted">{state.report.metadata?.eml}. The check covers structure and values; it cannot confirm that the meaning is unchanged.</p>
      <div className="d-flex flex-wrap gap-2">
        <a className="btn btn-primary" href={`${config.baseUrl}/api/datasets/${datasetId}/conversion-download/`}>{state.report.output_format === 'taxonomy-data-package' ? 'Download taxonomy data package' : 'Download Darwin Core Data Package'}</a>
        {packageExplorable && <button type="button" className="btn btn-outline-primary" onClick={openPackageExplorer}><i className="bi bi-diagram-3 me-2" aria-hidden="true" />Explore how your data connects</button>}
      </div>
    </div>}
    {packageExplorable && <PackageExplorer datasetId={datasetId} />}
  </div>
}
