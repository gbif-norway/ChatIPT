'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import config from '../config.js'
import { getCsrfToken } from '../utils/csrf.js'
import ScientificNameReview from './ScientificNameReview'

export default function TaxonReviewModal({ datasetId, scope, show, onClose, onDone, onSkip, open, editable }) {
  const [review, setReview] = useState(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [updated, setUpdated] = useState(0)
  const dialogRef = useRef(null)
  const onCloseRef = useRef(onClose)
  useEffect(() => { onCloseRef.current = onClose }, [onClose])
  const sourceTable = scope?.source_table || ''
  const contextColumn = scope?.context_column || ''
  const params = new URLSearchParams({ dataset: datasetId, source_table: sourceTable, context_column: contextColumn })
  const url = `${config.baseUrl}/api/taxon-matches/review/?${params}`

  const load = useCallback(async signal => {
    setLoading(true)
    setError('')
    try {
      const response = await fetch(url, { credentials: 'include', signal })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'The names could not be loaded.')
      setReview(data.name_review)
    } catch (err) {
      if (err.name !== 'AbortError') setError(err.message)
    } finally { setLoading(false) }
  }, [url])
  useEffect(() => {
    if (!show) return undefined
    const controller = new AbortController()
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads the names when the modal opens; the loading flag is meant to show straight away
    load(controller.signal)
    return () => controller.abort()
  }, [show, load])

  const send = useCallback(async body => {
    setBusy(true)
    setError('')
    try {
      const csrf = await getCsrfToken()
      const response = await fetch(url, {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json', ...(csrf ? { 'X-CSRFToken': csrf } : {}) },
        body: JSON.stringify({ ...body, dataset: datasetId, source_table: sourceTable, context_column: contextColumn }),
      })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || 'The decision could not be saved.')
      setReview(data.name_review)
      setUpdated(value => value + 1)
    } catch (err) {
      setError(err.message)
      throw err
    } finally { setBusy(false) }
  }, [url, datasetId, sourceTable, contextColumn])

  useEffect(() => {
    if (!show) return undefined
    const opener = document.activeElement
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    dialogRef.current?.focus()
    const onKeyDown = event => {
      if (event.target.closest('.name-review-modal')) return
      if (event.key === 'Escape') { onCloseRef.current(); return }
      if (event.key !== 'Tab' || !dialogRef.current) return
      const focusable = [...dialogRef.current.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), [tabindex="0"]')]
        .filter(element => element.offsetParent !== null)
      if (!focusable.length) { event.preventDefault(); return }
      const first = focusable[0], last = focusable[focusable.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault(); last.focus()
      } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus() }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.body.style.overflow = overflow
      opener?.focus()
    }
  }, [show])

  if (!show) return null
  const summary = review?.summary
  return <>
    <div className="modal fade show d-block" role="dialog" aria-modal="true" aria-labelledby={`taxon-review-title-${datasetId}`}>
      <div className="modal-dialog modal-xl modal-dialog-scrollable taxon-review-modal">
        <div className="modal-content" ref={dialogRef} tabIndex={-1}>
          <div className="modal-header">
            <div>
              <h2 className="modal-title fs-5" id={`taxon-review-title-${datasetId}`}>Review taxon names</h2>
              <p className="small text-muted mb-0 mt-1">Names checked with Catalogue of Life{review?.checklist ? ` (${review.checklist})` : ''}. A match suggests a name; your original text stays in verbatimIdentification.</p>
            </div>
            <button type="button" className="btn-close" aria-label="Close" onClick={onClose} />
          </div>
          <div className="modal-body">
            {!editable && <div className="alert alert-secondary small py-2">{open
              ? 'ChatIPT is replying to your message. You can change decisions and finish the review once it has.'
              : 'This review is finished. Your decisions are saved.'}</div>}
            {error && <div className="alert alert-danger small" role="alert">{error}</div>}
            {loading && <div className="small text-muted">Loading taxon names…</div>}
            {review && <ScientificNameReview nameReview={review} updated={updated} datasetId={datasetId} url={url}
              editable={editable} busy={busy || loading} send={send} />}
            {!loading && summary?.labels === 0 && <p className="small text-muted">There are no matched taxon names yet.</p>}
          </div>
          <div className="modal-footer justify-content-between">
            <span className="small text-muted">{summary?.decided || 0} of {summary?.labels || 0} names decided</span>
            <div className="d-flex gap-2">
              <button type="button" className="btn btn-outline-secondary" onClick={onClose}>Close</button>
              {editable && <button type="button" className="btn btn-outline-secondary" disabled={busy} onClick={onSkip}>Skip review</button>}
              {editable && <button type="button" className="btn btn-primary" disabled={busy || loading || !summary?.labels} onClick={onDone}>Done, apply my decisions</button>}
            </div>
          </div>
        </div>
      </div>
    </div>
    <div className="modal-backdrop fade show" />
  </>
}
