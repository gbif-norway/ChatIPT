'use client'

import { useCallback, useEffect, useState } from 'react'
import config from '../config'
import { getCsrfToken } from '../utils/csrf'
import { useDataset } from '../contexts/DatasetContext'
import { pendingAdvice, unresolvedIssues } from '../utils/conversionReview.mjs'

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
    <h1>Convert your Darwin Core Archive to a Data Package</h1>
    <p>Upload a ZIP archive, or its files together. ChatIPT maps supported terms, asks you to review ambiguous choices, and validates the converted package.</p>
    <p className="text-muted">Include meta.xml and eml.xml when available. For loose tables, name the core occurrence.csv, event.csv or taxon.csv. Original files and unsupported fields are retained in the download.</p>
    <form onSubmit={submit} className="card card-body gap-3">
      <label className="form-label">Conversion title (optional)<input className="form-control mt-1" value={title} onChange={event => setTitle(event.target.value)} disabled={busy} /></label>
      <label className="form-label">Archive or loose files<input type="file" multiple accept=".zip,.dwca,.csv,.tsv,.txt,.xml" className="form-control mt-1" disabled={busy} onChange={event => setFiles(Array.from(event.target.files || []))} /></label>
      {files.length > 0 && <ul>{files.map(file => <li key={file.name}>{file.name} · {(file.size / 1024).toFixed(0)} KB</li>)}</ul>}
      <small className="text-muted">Maximum 200 MB uploaded or expanded. Event, Occurrence and Taxon cores are accepted. A checklist alone produces a taxonomy data package; actual occurrence records can also be converted to DwC-DP after review.</small>
      {error && <div className="alert alert-danger" role="alert">{error}</div>}
      <button className="btn btn-primary align-self-start" disabled={busy || !files.length}>{busy ? 'Uploading…' : 'Inspect files and prepare mappings'}</button>
    </form>
  </div>
}

export default function DwcConversion() {
  const { currentDataset } = useDataset()
  const [state, setState] = useState(null)
  const [decisions, setDecisions] = useState({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const datasetId = currentDataset?.id
  const url = `${config.baseUrl}/api/datasets/${datasetId}/conversion/`
  const load = useCallback(async () => {
    try { const data = await request(url); setState(data); setError('') } catch (err) { setError(err.message) }
  }, [url])
  useEffect(() => { if (datasetId) load() }, [datasetId, load])
  useEffect(() => { setDecisions(state?.decisions || {}) }, [state?.plan?.id, state?.decisions])
  const working = ['queued', 'inspecting', 'converting', 'reviewing'].includes(state?.status)
  useEffect(() => {
    if (!working) return
    const interval = setInterval(load, 3000)
    return () => clearInterval(interval)
  }, [working, load])
  const perform = async (action) => {
    setBusy(true); setError('')
    try { setState(await request(url, { action, plan_id: state?.plan?.id, decisions })) } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  const automaticChoices = state?.plan?.automatic_choices || []
  const defaults = Object.fromEntries(automaticChoices.map(choice => [choice.id, choice.default]))
  const selected = (id, fallback) => decisions[id] ?? defaults[id] ?? fallback
  const rowChoice = issue => issue.id.startsWith('taxon-occurrence:')
    ? `taxon-occurrence:${issue.table}:row:0:${issue.row - 1}` : `row:${issue.table}:${issue.row - 1}`
  const retainedIssue = (issue) => selected(`table:${issue.table}`) === 'preserve' ||
    (issue.row !== undefined && selected(rowChoice(issue)) === 'preserve')
  const unresolved = unresolvedIssues(state?.plan, decisions)
  const pending = pendingAdvice(state, decisions)
  const batchSize = state?.advice_progress?.batch_size || 40
  const reviewedCount = state?.advice_progress?.reviewed || 0
  const disabled = busy || working
  const suggestionFor = (id) => state?.suggestions?.find(item => item.id === id)
  const scientific = state?.status === 'complete'
    ? state.report?.event_hierarchy?.scientific_consistency : state?.plan?.scientific_hierarchy
  const nestedScientific = state?.status === 'complete'
    ? Object.entries(state.report?.taxonomy?.event_hierarchies || {}).map(([index, hierarchy]) => [index, hierarchy.scientific_consistency])
    : Object.entries(state?.plan?.taxonomy?.scientific_hierarchies || {})
  const scientificAudits = [...(scientific ? [['core', scientific]] : []), ...nestedScientific].filter(([, audit]) => audit)
  const notices = state?.status === 'complete' ? state.report?.warnings || [] : state?.plan?.warnings || []
  return <div className="container p-4">
    <h1>{currentDataset?.title || 'Darwin Core Archive conversion'}</h1>
    <p>Inspect files → resolve any ambiguous choices → convert and validate → download</p>
    {error && <div className="alert alert-danger" role="alert">{error}<button className="btn btn-sm btn-outline-danger ms-2" onClick={load}>Reload</button></div>}
    {state?.error && <div className="alert alert-warning" role="alert">{state.error}</div>}
    {!state && <p>Loading conversion…</p>}
    {working && <p role="status"><span className="spinner-border spinner-border-sm me-2" />{state.status === 'reviewing' ? 'Preparing AI suggestions…' : state.status === 'converting' ? 'Converting and validating…' : 'Inspecting source files…'} You can leave and return while this runs.</p>}
    {state?.status === 'failed' && <button className="btn btn-primary" disabled={disabled} onClick={() => perform('inspect')}>Inspect again</button>}
    {state?.plan?.tables && <>
      {state.plan.taxonomy && <div className="alert alert-info">
        Your Taxon core and its extensions will be preserved as additional taxonomy tables with explicit source links.
        DwC-DP has no standalone Taxon table. A checklist alone produces a taxonomy data package; converting actual occurrence records also adds standard DwC-DP tables.
      </div>}
      <div className="table-responsive"><table className="table"><thead><tr><th>Source table</th><th>Role</th><th>Link to core</th><th>Rows</th><th>Columns</th></tr></thead><tbody>
        {state.plan.tables.map((table, index) => <tr key={index}><td>{table.name}</td><td>{table.core ? 'Core' : 'Extension'} · {table.row_type.split('/').pop() || 'Unrecognised'}</td><td>{table.join_basis?.split('/').pop()}</td><td>{table.rows.toLocaleString()}</td><td>{table.columns.length}</td></tr>)}
      </tbody></table></div>
      <p className="small text-muted">{state.plan.taxonomy ? 'Taxonomy data package; optional occurrence conversion uses' : 'Target:'} DwC-DP {state.plan.schema.version}. {state.plan.files.length} original files retained.</p>
    </>}
    {scientificAudits.map(([index, scientific]) => {
      const scientificFindings = scientific.finding_sample || (scientific.checks || []).filter(check =>
        ['contradiction', 'conflict-signal', 'reporting-gap'].includes(check.status) ||
        ['survey-ambiguous', 'event-source-ambiguity'].includes(check.kind)).slice(0, 10)
      return <div key={index} className={`alert ${scientific.has_findings ? 'alert-warning' : 'alert-info'}`}>
      <p className="fw-semibold mb-1">Parent and child event consistency</p>
      {index !== 'core' && <p className="small mb-1">Source occurrence table: {state.plan.tables[index]?.name}</p>}
      <p className="small mb-1">The audit compares original source assertions, including fields retained without mapping. It checks supported dates, spatial bounds and survey claims. Missing or incomparable information leaves containment unverified.</p>
      <ul className="small mb-2">{Object.entries(scientific.counts || {}).map(([status, count]) => <li key={status}>{status.replaceAll('-', ' ')}: {count.toLocaleString()}</li>)}</ul>
      {scientific.has_findings && <p className="small mb-1">The supplied parent and child claims have unresolved findings. They remain in the report and do not block conversion. Retaining the publisher’s links does not confirm scientific consistency.</p>}
      {scientificFindings.length > 0 && <details className="small mb-2"><summary>Source findings (up to 10 shown)</summary><ul>{scientificFindings.map((finding, index) => <li key={index}>{finding.child_eventID} → {finding.ancestor_eventID || finding.parent_eventID}: {finding.reason}</li>)}</ul></details>}
      <p className="small mb-0">No scopes, completeness flags or other survey values are inherited or repaired. Package validation checks structure and values. <a href="https://eco.tdwg.org/hierarchy/" target="_blank" rel="noreferrer">TDWG hierarchy guidance</a></p>
    </div>})}
    {notices.length > 0 && <div className="alert alert-info">
      <p className="mb-1">{notices.length} conversion notices. Original files retain every source value; the report explains unsupported mappings and values omitted from mapped tables.</p>
      <details className="small"><summary>View notices</summary><ul>{notices.map((notice, index) => <li key={`${notice.id}:${index}`}><strong>{notice.title}:</strong> {notice.reason}</li>)}</ul></details>
    </div>}
    {state?.status === 'review' && <>
      {state.plan.event_grouping_evidence && !state.plan.event_grouping_evidence.checks_passed && <div className="alert alert-info">
        Some event identifiers are missing or their rows disagree on {Object.keys(state.plan.event_grouping_evidence.conflicting_fields).map(field => field.split('.').pop()).join(', ') || 'event details'}. Combining those rows requires resolving these differences. Separate context events retain them.
      </div>}
      <div className="d-flex flex-wrap gap-2 align-items-center mb-3"><h2 className="me-auto mb-0">{unresolved.length ? 'Choices needing your input' : 'Ready to convert'}</h2>
        {unresolved.length > 0 && <button className="btn btn-outline-secondary" disabled={disabled || !pending.length} onClick={() => perform('suggest')}>
          {reviewedCount ? `Suggest next ${Math.min(batchSize, pending.length)} choices with AI` : 'Suggest choices with AI'}
        </button>}
      </div>
      <p>{unresolved.length ? `Resolve ${unresolved.length} remaining choices. AI suggestions need your approval.` : 'Supported mappings are selected automatically. You can adjust them below.'} Preserving a column keeps its values in the original files without asserting a new meaning.</p>
      {unresolved.length > 0 && <p className="small text-muted">AI reviews up to {batchSize} unresolved choices per request. {reviewedCount} choices reviewed; {pending.length} still available for AI review.
        {!!state.advice_progress?.without_suggestion && ` AI could not recommend a choice for ${state.advice_progress.without_suggestion} reviewed items; review those manually.`}
      </p>}
      {(state.plan.issues || []).filter(issue => (!retainedIssue(issue) || issue.id === rowChoice(issue)) && selected(`table:${issue.table}`) !== 'preserve' || issue.id === `table:${issue.table}`).map(issue => {
        const suggestion = suggestionFor(issue.id)
        return <div className="card card-body mb-3" key={issue.id}>
          {issue.table !== undefined && <small className="text-muted mb-1">{state.plan.tables[issue.table]?.name}</small>}
          <label htmlFor={issue.id} className="fw-semibold">{issue.title}</label><p className="small mb-2">{issue.reason}</p>
          {issue.samples?.length > 0 && <div className="small text-muted mb-2" style={{ overflowWrap: 'anywhere' }}>Examples: {issue.samples.join(' · ')}</div>}
          <select id={issue.id} className="form-select" value={decisions[issue.id] || ''} disabled={disabled} onChange={event => setDecisions(previous => ({ ...previous, [issue.id]: event.target.value }))}>
            <option value="">Choose…</option>{issue.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
          {suggestion && <div className="small mt-2">AI suggests {issue.options.find(option => option.value === suggestion.option)?.label}: {suggestion.reason}
            <button className="btn btn-sm btn-outline-primary ms-2" disabled={disabled} onClick={() => setDecisions(previous => ({ ...previous, [issue.id]: suggestion.option }))}>Use suggestion</button>
          </div>}
        </div>
      })}
      <details className="mb-3"><summary>Automatic mappings and choices</summary>
        {automaticChoices.filter(choice => !state.plan.columns.some(column => column.id === choice.id) && (!retainedIssue(choice) || choice.id === `table:${choice.table}`)).map(choice => <div className="my-3" key={choice.id}><label htmlFor={choice.id} className="small fw-semibold">{choice.title}</label><p className="small mb-1">{choice.reason}</p><select id={choice.id} className="form-select form-select-sm" disabled={disabled} value={selected(choice.id)} onChange={event => setDecisions(previous => ({ ...previous, [choice.id]: event.target.value }))}>{choice.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div>)}
        {state.plan.columns.filter(column => !column.review && selected(`table:${column.table}`) !== 'preserve').map(column => <div className="row align-items-center my-2" key={column.id}><label htmlFor={column.id} className="col-md-6 small">{state.plan.tables[column.table].name} · {column.term.split('/').pop()}</label><div className="col-md-6"><select id={column.id} className="form-select form-select-sm" disabled={disabled} value={selected(column.id, column.default)} onChange={event => setDecisions(previous => ({ ...previous, [column.id]: event.target.value }))}>{column.options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select></div></div>)}
      </details>
      <button className="btn btn-primary" disabled={disabled || unresolved.length > 0} onClick={() => perform('convert')}>Convert and validate package</button>
    </>}
    {state?.status === 'complete' && <div className="card card-body">
      <h2>Converted package ready</h2>
      <p>Validation passed. The download includes connected tables, a mapping report with row crosswalks, and every original file.</p>
      {state.report.output_format === 'taxonomy-data-package' && <p>This is a Frictionless taxonomy data package, containing your checklist and its extensions. It contains no standard DwC-DP tables.</p>}
      {state.report.output_format === 'dwc-dp' && state.report.taxonomy && <p>This DwC-DP includes standard occurrence tables and additional taxonomy tables preserving the original checklist and its extensions.</p>}
      <ul>{Object.entries(state.report.resources || {}).map(([name, count]) => <li key={name}>{name}: {count.toLocaleString()} {count === 1 ? 'row' : 'rows'}</li>)}</ul>
      <p className="small">{state.report.columns?.filter(column => column.disposition === 'retained-unmapped' && column.nonempty).length || 0} populated columns are retained in originals without a DwC-DP mapping.</p>
      {!!state.report.withheld_values?.length && <p className="small">{state.report.withheld_values.length.toLocaleString()} values were withheld from mapped tables because they did not meet conversion criteria. The report explains each value; originals retain them.</p>}
      {!!state.report.preserved_extension_rows?.length && <p className="small">{state.report.preserved_extension_rows.length.toLocaleString()} extension rows were retained in originals. The report records the reason and any user decision.</p>}
      {state.report.archive_validation?.warnings?.map(warning => <p key={warning} className="small text-muted">{warning}</p>)}
      <p className="small text-muted">{state.report.metadata?.eml}. Validation checks structure and values; it does not establish semantic equivalence.</p>
      <a className="btn btn-primary align-self-start" href={`${config.baseUrl}/api/datasets/${datasetId}/conversion-download/`}>{state.report.output_format === 'taxonomy-data-package' ? 'Download taxonomy data package' : 'Download Darwin Core Data Package'}</a>
    </div>}
  </div>
}
