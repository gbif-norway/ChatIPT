import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import config from '../config.js'
import { getCsrfToken } from '../utils/csrf.js'
import {
  DECISION_LABELS,
  RANKS,
  countByDecision,
  filterRows,
  formatName,
  isPending,
  splitScientificName,
  statusInfo,
} from '../utils/taxonReview.mjs'

const PAGE_SIZE = 50
const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'

const postJson = async (path, body) => {
  const csrfToken = await getCsrfToken()
  const headers = { 'Content-Type': 'application/json' }
  if (csrfToken) headers['X-CSRFToken'] = csrfToken
  const response = await fetch(`${config.baseUrl}${path}`, {
    method: 'POST',
    headers,
    credentials: 'include',
    body: JSON.stringify(body),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = data?.detail || Object.values(data || {}).flat().join(' ')
    throw new Error(detail || 'The decision could not be saved.')
  }
  return data
}

function TaxonSearch({ onPick, disabled }) {
  const [text, setText] = useState('')
  const [results, setResults] = useState([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    const query = text.trim()
    if (query.length < 3) {
      setResults([])
      return undefined
    }
    const controller = new AbortController()
    const timer = setTimeout(async () => {
      setSearching(true)
      setError(null)
      try {
        const response = await fetch(
          `${config.baseUrl}/api/taxon-matches/search/?q=${encodeURIComponent(query)}`,
          { credentials: 'include', signal: controller.signal },
        )
        const data = await response.json()
        if (!response.ok) throw new Error(data?.detail || 'Search failed.')
        setResults(data.results || [])
      } catch (err) {
        if (err.name !== 'AbortError') setError(err.message)
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [text])

  return (
    <div>
      <input
        type="search"
        className="form-control form-control-sm"
        placeholder="Search Catalogue of Life (at least 3 letters)"
        value={text}
        onChange={(event) => setText(event.target.value)}
        disabled={disabled}
        aria-label="Search Catalogue of Life"
      />
      {searching && <div className="small text-muted mt-1">Searching…</div>}
      {error && <div className="small text-danger mt-1">{error}</div>}
      {results.length > 0 && (
        <div className="list-group list-group-flush taxon-search-results mt-1">
          {results.map((result) => (
            <button
              key={result.id}
              type="button"
              className="list-group-item list-group-item-action small py-1"
              onClick={() => onPick(result.id)}
              disabled={disabled}
            >
              {result.suggestion || result.label}
              {result.status && result.status !== 'accepted' && (
                <span className="text-muted"> · {result.status}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function NotInColForm({ row, onSave, onCancel, disabled }) {
  const initial = splitScientificName(row.query?.scientificName || row.verbatim_label)
  const [name, setName] = useState(initial.scientificName)
  const [authorship, setAuthorship] = useState(initial.scientificNameAuthorship)
  const [rank, setRank] = useState(initial.taxonRank)

  return (
    <form
      className="taxon-not-in-col-form d-grid gap-1"
      onSubmit={(event) => {
        event.preventDefault()
        onSave({ scientificName: name, scientificNameAuthorship: authorship, taxonRank: rank })
      }}
    >
      <label className="small" htmlFor={`not-in-col-name-${row.id}`}>Scientific name</label>
      <input id={`not-in-col-name-${row.id}`} className="form-control form-control-sm" value={name}
        onChange={(event) => setName(event.target.value)} required disabled={disabled} />
      <label className="small" htmlFor={`not-in-col-author-${row.id}`}>Authorship (optional)</label>
      <input id={`not-in-col-author-${row.id}`} className="form-control form-control-sm" value={authorship}
        onChange={(event) => setAuthorship(event.target.value)} disabled={disabled} />
      <label className="small" htmlFor={`not-in-col-rank-${row.id}`}>Rank</label>
      <select id={`not-in-col-rank-${row.id}`} className="form-select form-select-sm" value={rank}
        onChange={(event) => setRank(event.target.value)} disabled={disabled}>
        <option value="">unknown</option>
        {RANKS.map((value) => <option key={value} value={value}>{value}</option>)}
      </select>
      <div className="d-flex gap-1 mt-1">
        <button type="submit" className="btn btn-sm btn-primary" disabled={disabled}>Save</button>
        <button type="button" className="btn btn-sm btn-link" onClick={onCancel} disabled={disabled}>Cancel</button>
      </div>
    </form>
  )
}

function Suggestion({ row }) {
  const status = statusInfo(row)
  const usage = row.match?.usage
  const aids = row.review_aids || {}
  const aidLines = [
    ['GBIF Backbone', aids.gbifBackbone],
    ['Newer COL release', aids.checklistBankXR],
  ].filter(([, aid]) => aid && ['exact', 'variant'].includes(aid.status))

  return (
    <div className="small">
      <div className="d-flex flex-wrap align-items-center gap-2">
        {usage ? <span className="fw-semibold">{formatName(usage)}</span> : <span className="text-muted">No suggestion</span>}
        {usage?.taxonRank && <span className="text-muted">{usage.taxonRank}</span>}
        <span className={`badge text-bg-${status.variant}`}>{status.label}</span>
      </div>
      {row.match?.acceptedUsage && (
        <div className="text-muted">Synonym; COL accepts {formatName(row.match.acceptedUsage)}</div>
      )}
      {row.preprocessed && (
        <div className="text-muted">
          <i className="bi bi-lightbulb me-1" aria-hidden="true"></i>
          Interpreted as <em>{row.query?.scientificName}</em>
          {row.preprocessing_note && <> — {row.preprocessing_note}</>}
        </div>
      )}
      {aidLines.map(([source, aid]) => (
        <div key={source} className="text-muted">
          {source}: {formatName(aid)}{aid.taxonRank ? ` (${aid.taxonRank})` : ''}
          {' '}<span className="fst-italic">may not yet be reflected on GBIF.org</span>
        </div>
      ))}
    </div>
  )
}

function DecisionCell({ row, busy, onDecide }) {
  const [mode, setMode] = useState(null)
  const alternatives = row.match?.alternatives || []
  const disabled = busy

  if (!isPending(row)) {
    const decided = row.decided_usage
    return (
      <div className="small">
        <span className="badge text-bg-primary">{DECISION_LABELS[row.decision]}</span>
        {decided?.scientificName && <div>{formatName(decided)}{decided.taxonRank ? ` (${decided.taxonRank})` : ''}</div>}
        <button type="button" className="btn btn-link btn-sm p-0" disabled={disabled}
          onClick={() => onDecide(row, { decision: 'pending' })}>
          Undo
        </button>
      </div>
    )
  }

  return (
    <div>
      <div className="d-flex flex-wrap gap-1" role="group" aria-label={`Decision for ${row.verbatim_label}`}>
        <button type="button" className="btn btn-sm btn-outline-success" disabled={disabled || !row.match?.usage}
          onClick={() => onDecide(row, { decision: 'accepted' })}
          title="Use the suggested Catalogue of Life name">
          Accept
        </button>
        <button type="button" className={`btn btn-sm btn-outline-secondary${mode === 'other' ? ' active' : ''}`}
          disabled={disabled} onClick={() => setMode(mode === 'other' ? null : 'other')}
          title="Choose a different Catalogue of Life name">
          Other…
        </button>
        <button type="button" className={`btn btn-sm btn-outline-secondary${mode === 'not_in_col' ? ' active' : ''}`}
          disabled={disabled} onClick={() => setMode(mode === 'not_in_col' ? null : 'not_in_col')}
          title="The name is correct but Catalogue of Life does not have it">
          Not in COL
        </button>
        <button type="button" className="btn btn-sm btn-outline-secondary" disabled={disabled}
          onClick={() => onDecide(row, { decision: 'keep_original' })}
          title="Leave the data for this name unchanged">
          Keep original
        </button>
      </div>
      {mode === 'other' && (
        <div className="mt-2">
          {alternatives.length > 0 && (
            <div className="d-flex flex-wrap gap-1 mb-2">
              {alternatives.map((alternative) => (
                <button key={alternative.id} type="button" className="btn btn-sm btn-outline-primary"
                  disabled={disabled}
                  onClick={() => onDecide(row, { decision: 'accepted', usage_id: alternative.id })}>
                  {formatName(alternative)} <span className="opacity-75">({alternative.taxonRank})</span>
                </button>
              ))}
            </div>
          )}
          <TaxonSearch disabled={disabled}
            onPick={(usageId) => onDecide(row, { decision: 'accepted', usage_id: usageId })} />
        </div>
      )}
      {mode === 'not_in_col' && (
        <div className="mt-2">
          <NotInColForm row={row} disabled={disabled} onCancel={() => setMode(null)}
            onSave={(name) => onDecide(row, { decision: 'not_in_col', ...name })} />
        </div>
      )}
    </div>
  )
}

export default function TaxonReviewModal({ datasetId, scope, show, onClose, onDone, canSendSummary }) {
  const [rows, setRows] = useState([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [view, setView] = useState('pending')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(0)
  const [busyIds, setBusyIds] = useState(new Set())
  const [confirmBulk, setConfirmBulk] = useState(false)
  const [bulkBusy, setBulkBusy] = useState(false)
  const bodyRef = useRef(null)
  const dialogRef = useRef(null)
  // The parent re-renders while it polls; a ref keeps the keyboard effect from re-running.
  const onCloseRef = useRef(onClose)
  useEffect(() => { onCloseRef.current = onClose }, [onClose])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({ dataset: datasetId })
      if (scope?.source_table) {
        params.set('source_table', scope.source_table)
        params.set('context_column', scope.context_column || '')
      }
      const response = await fetch(`${config.baseUrl}/api/taxon-matches/?${params}`, {
        credentials: 'include',
      })
      if (!response.ok) throw new Error('The taxon names could not be loaded.')
      const data = await response.json()
      setRows(Array.isArray(data) ? data : (data.results || []))
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }, [datasetId, scope?.source_table, scope?.context_column])

  useEffect(() => {
    if (show) load()
  }, [show, load])

  // Keyboard: move focus into the dialog, keep Tab inside it, close on Escape, and return focus
  // to whatever opened it.
  useEffect(() => {
    if (!show) return undefined
    const opener = document.activeElement
    dialogRef.current?.focus()
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab' || !dialogRef.current) return
      const focusable = [...dialogRef.current.querySelectorAll(FOCUSABLE)]
        .filter((element) => !element.disabled && element.offsetParent !== null)
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      const inside = dialogRef.current.contains(active)
      if (event.shiftKey && (active === first || active === dialogRef.current || !inside)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !inside)) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      if (opener && typeof opener.focus === 'function') opener.focus()
    }
  }, [show])

  useEffect(() => { setPage(0) }, [view, query])

  const replaceRows = (updated) => {
    const byId = new Map(updated.map((row) => [row.id, row]))
    setRows((current) => current.map((row) => byId.get(row.id) || row))
  }

  const decide = async (row, payload) => {
    setBusyIds((current) => new Set(current).add(row.id))
    setError(null)
    try {
      replaceRows([await postJson(`/api/taxon-matches/${row.id}/decide/`, payload)])
    } catch (err) {
      setError(`${row.verbatim_label}: ${err.message}`)
    } finally {
      setBusyIds((current) => {
        const next = new Set(current)
        next.delete(row.id)
        return next
      })
    }
  }

  const bulkRows = useMemo(() => rows.filter((row) => row.bulk_acceptable), [rows])

  const acceptBulk = async () => {
    setBulkBusy(true)
    setError(null)
    try {
      const data = await postJson('/api/taxon-matches/accept-exact/', {
        dataset: datasetId,
        ...(scope?.source_table
          ? { source_table: scope.source_table, context_column: scope.context_column || '' }
          : {}),
      })
      replaceRows(data.results || [])
      setConfirmBulk(false)
    } catch (err) {
      setError(err.message)
    } finally {
      setBulkBusy(false)
    }
  }

  const visible = useMemo(() => filterRows(rows, { view, query }), [rows, view, query])
  const pageCount = Math.max(1, Math.ceil(visible.length / PAGE_SIZE))
  const pageRows = visible.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)
  const counts = countByDecision(rows)
  const pendingCount = counts.pending || 0
  const release = rows.find((row) => row.col_release?.alias)?.col_release?.alias

  if (!show) return null

  return (
    <>
      <div
        className="modal fade show d-block"
        role="dialog"
        tabIndex="-1"
        aria-modal="true"
        aria-labelledby={`taxon-review-title-${datasetId}`}
        onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}
      >
        <div className="modal-dialog modal-xl modal-dialog-scrollable taxon-review-modal">
          <div className="modal-content" ref={dialogRef} tabIndex="-1">
            <div className="modal-header align-items-start">
              <div>
                <h2 className="modal-title fs-5" id={`taxon-review-title-${datasetId}`}>Review taxon names</h2>
                <p className="small text-muted mb-0 mt-1">
                  Suggestions from Catalogue of Life{release ? ` (${release})` : ''}, the taxonomy GBIF.org uses.
                  A match means the name was found, not that the identification is right. Your original
                  names are always kept.
                </p>
              </div>
              <button type="button" className="btn-close" aria-label="Close" onClick={onClose}></button>
            </div>

            <div className="modal-body" ref={bodyRef}>
              <div className="d-flex flex-wrap align-items-center gap-2 mb-3">
                <div className="btn-group btn-group-sm" role="group" aria-label="Filter names">
                  {[
                    ['pending', `Needs review (${pendingCount})`],
                    ['reviewed', `Reviewed (${rows.length - pendingCount})`],
                    ['all', `All (${rows.length})`],
                  ].map(([value, label]) => (
                    <button key={value} type="button"
                      className={`btn btn-outline-secondary${view === value ? ' active' : ''}`}
                      onClick={() => setView(value)} aria-pressed={view === value}>
                      {label}
                    </button>
                  ))}
                </div>
                <input type="search" className="form-control form-control-sm taxon-review-filter"
                  placeholder="Filter names" value={query} onChange={(event) => setQuery(event.target.value)}
                  aria-label="Filter names" />
                <button type="button" className="btn btn-sm btn-success ms-auto"
                  disabled={bulkRows.length === 0 || bulkBusy} onClick={() => setConfirmBulk(true)}>
                  Accept {bulkRows.length} exact {bulkRows.length === 1 ? 'match' : 'matches'}…
                </button>
              </div>

              {confirmBulk && (
                <div className="alert alert-success small">
                  <p className="mb-2">
                    These names are written exactly as in Catalogue of Life and have no qualifier such as
                    “sp.” or “cf.”. Accepting them sets the scientific name, authorship and rank
                    for {bulkRows.reduce((sum, row) => sum + row.record_count, 0)} records:
                  </p>
                  <div className="taxon-bulk-list mb-2">
                    {bulkRows.map((row) => (
                      <div key={row.id}>
                        {row.verbatim_label} → <strong>{formatName(row.match.usage)}</strong>
                        <span className="text-muted"> ({row.match.usage.taxonRank}, {row.record_count} records)</span>
                      </div>
                    ))}
                  </div>
                  <button type="button" className="btn btn-sm btn-success me-2" onClick={acceptBulk} disabled={bulkBusy}>
                    {bulkBusy ? 'Accepting…' : `Accept ${bulkRows.length}`}
                  </button>
                  <button type="button" className="btn btn-sm btn-link" onClick={() => setConfirmBulk(false)} disabled={bulkBusy}>
                    Cancel
                  </button>
                </div>
              )}

              {error && <div className="alert alert-danger small" role="alert">{error}</div>}
              {loading && <div className="text-muted small">Loading taxon names…</div>}

              {!loading && visible.length === 0 && (
                <div className="text-muted small py-4 text-center">
                  {rows.length === 0 ? 'There are no matched taxon names yet.' : 'No names in this view.'}
                </div>
              )}

              {pageRows.length > 0 && (
                <div className="table-responsive">
                  <table className="table table-sm align-top taxon-review-table">
                    <thead>
                      <tr>
                        <th scope="col">Original name</th>
                        <th scope="col">Catalogue of Life suggestion</th>
                        <th scope="col">Your decision</th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageRows.map((row) => (
                        <tr key={row.id}>
                          <td className="small">
                            <div className="fw-semibold text-break">{row.verbatim_label}</div>
                            <div className="text-muted">
                              {row.record_count} {row.record_count === 1 ? 'record' : 'records'}
                              {row.context_key && <> · {row.context_key}</>}
                            </div>
                            {row.identification_qualifier && (
                              <span className="badge text-bg-light border">qualifier: {row.identification_qualifier}</span>
                            )}
                          </td>
                          <td><Suggestion row={row} /></td>
                          <td><DecisionCell row={row} busy={busyIds.has(row.id)} onDecide={decide} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {pageCount > 1 && (
                <nav className="d-flex align-items-center gap-2 small" aria-label="Taxon name pages">
                  <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page === 0}
                    onClick={() => { setPage(page - 1); bodyRef.current?.scrollTo(0, 0) }}>
                    Previous
                  </button>
                  <span>Page {page + 1} of {pageCount}</span>
                  <button type="button" className="btn btn-sm btn-outline-secondary" disabled={page >= pageCount - 1}
                    onClick={() => { setPage(page + 1); bodyRef.current?.scrollTo(0, 0) }}>
                    Next
                  </button>
                </nav>
              )}
            </div>

            <div className="modal-footer justify-content-between">
              <span className="small text-muted">
                {rows.length - pendingCount} of {rows.length} names reviewed
              </span>
              <div className="d-flex gap-2">
                <button type="button" className="btn btn-outline-secondary" onClick={onClose}>Close</button>
                {canSendSummary && (
                  <button type="button" className="btn btn-primary" disabled={loading || rows.length === 0}
                    onClick={() => onDone(rows)}>
                    Done, tell ChatIPT
                  </button>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
      <div className="modal-backdrop fade show"></div>
    </>
  )
}
