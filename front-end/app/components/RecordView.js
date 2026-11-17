'use client'

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import config from '../config.js'
import { pluralize } from '../utils/datasetPresentation'
import {
  askAboutRecordText,
  cellText,
  incomingLinkLabel,
  isBlankValue,
  orderRecordFields,
  recordKey,
  recordSummary,
} from '../utils/rowFilters.mjs'
import { apiErrorMessage, tableRowsUrl } from '../utils/tableApi.mjs'

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

// Looks up rows of another table where `column` is exactly `value`, with an `equals` filter only (the older
// search param is limited to 200 characters, which long link values can exceed).
function useLinkedRows(tableId, column, value, limit) {
  const [attempt, setAttempt] = useState(0)
  const requestKey = JSON.stringify([tableId, column, value, limit, attempt])
  // Results are tagged with the request they answer, so a stale result reads as still loading.
  const [result, setResult] = useState({ key: null, status: 'loading', payload: null })

  useEffect(() => {
    const controller = new AbortController()
    const url = tableRowsUrl(config.baseUrl, tableId, 1, limit, {
      filters: [{ column, op: 'equals', value }],
    })
    fetch(url, { credentials: 'include', signal: controller.signal })
      .then(async (response) => {
        const payload = await response.json().catch(() => null)
        if (!response.ok || !Array.isArray(payload?.results)) throw new Error(apiErrorMessage(payload, response.status))
        setResult({ key: requestKey, status: 'done', payload })
      })
      .catch((error) => {
        if (error.name === 'AbortError') return
        console.error('Error loading linked rows:', error)
        setResult({ key: requestKey, status: 'error', payload: null })
      })
    return () => controller.abort()
  }, [tableId, column, value, limit, requestKey])

  const retry = useCallback(() => setAttempt((count) => count + 1), [])
  if (result.key !== requestKey) return { status: 'loading', payload: null, retry }
  return { status: result.status, payload: result.payload, retry }
}

function LookupStatus({ status, label, onRetry }) {
  if (status === 'loading') {
    return (
      <p className="record-link-quiet" role="status">
        <span className="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>
        Looking up {label}…
      </p>
    )
  }
  return (
    <p className="record-link-quiet">
      Could not look up {label}.{' '}
      <button type="button" className="btn btn-link btn-sm p-0 align-baseline" onClick={onRetry}>Retry</button>
    </p>
  )
}

function OutgoingLink({ relation, value, onOpenRelated }) {
  const { status, payload, retry } = useLinkedRows(relation.targetTableId, relation.targetColumn, value, 3)
  const title = relation.targetTitle || 'linked table'
  const filters = [{ column: relation.targetColumn, op: 'equals', value }]
  const linked = payload?.results?.[0]
  const count = Number(payload?.count) || 0

  return (
    <li className="record-link">
      <div className="record-link-heading">
        <i className="bi bi-box-arrow-up-right" aria-hidden="true"></i>
        <span><strong>{relation.column}</strong> links to {title}</span>
      </div>
      {status !== 'done' && <LookupStatus status={status} label={`the linked ${title} row`} onRetry={retry} />}
      {status === 'done' && count === 0 && (
        <p className="record-link-missing">
          <i className="bi bi-exclamation-triangle me-1" aria-hidden="true"></i>
          No {title} row has {relation.targetColumn} = “{value}”. This link points to nothing.
        </p>
      )}
      {status === 'done' && linked && (
        <>
          <dl className="record-link-summary">
            {recordSummary(payload.columns, linked, 4).map((entry) => (
              <div key={entry.name}>
                <dt>{entry.name}</dt>
                <dd title={entry.value}>{entry.value}</dd>
              </div>
            ))}
          </dl>
          {count > 1 && (
            <p className="record-link-quiet">
              {pluralize(count, `${title} row`)} have this {relation.targetColumn}; showing the first.
            </p>
          )}
        </>
      )}
      {status === 'done' && count > 0 && onOpenRelated && (
        <button
          type="button"
          className="btn btn-sm btn-outline-secondary"
          onClick={() => onOpenRelated({ tableId: relation.targetTableId, filters })}
        >
          Open in {title}
        </button>
      )}
    </li>
  )
}

function IncomingLink({ relation, value, onOpenRelated }) {
  const { status, payload, retry } = useLinkedRows(relation.sourceTableId, relation.sourceColumn, value, 1)
  const title = relation.sourceTitle || 'linked'
  const count = Number(payload?.count) || 0

  return (
    <li className="record-link">
      <div className="record-link-heading">
        <i className="bi bi-box-arrow-in-down-left" aria-hidden="true"></i>
        <span>{title} rows using <strong>{relation.sourceColumn}</strong></span>
      </div>
      {status !== 'done' && <LookupStatus status={status} label={`${title} rows`} onRetry={retry} />}
      {status === 'done' && (
        <div className="record-link-count">
          <span>{incomingLinkLabel(count, title)}</span>
          {count > 0 && onOpenRelated && (
            <button
              type="button"
              className="btn btn-sm btn-outline-secondary"
              onClick={() => onOpenRelated({
                tableId: relation.sourceTableId,
                filters: [{ column: relation.sourceColumn, op: 'equals', value }],
              })}
            >
              Show in {title}
            </button>
          )}
        </div>
      )}
    </li>
  )
}

function FieldList({ entries }) {
  return (
    <dl className="record-fields">
      {entries.map((entry) => (
        <div key={entry.name} className={entry.blank ? 'record-field is-blank' : 'record-field'}>
          <dt>
            <span className="record-field-name">{entry.label}</span>
            {entry.field?.primary && (
              <i className="bi bi-key-fill ms-1" role="img" aria-label="Primary key" title="Primary key"></i>
            )}
            {!entry.field?.primary && entry.field?.weakPrimary && (
              <i className="bi bi-key ms-1" role="img" aria-label="Public identifier" title="Public identifier"></i>
            )}
            {entry.label !== entry.name && <code className="record-field-uri">{entry.name}</code>}
            {entry.field?.description && <small className="record-field-description">{entry.field.description}</small>}
          </dt>
          <dd>{entry.blank ? <span className="record-blank">blank</span> : cellText(entry.value)}</dd>
        </div>
      ))}
    </dl>
  )
}

/**
 * One record from the current page as a side panel. Rendered into `container` (the enclosing Bootstrap
 * modal when there is one, so the modal's focus trap and Escape handling leave it alone).
 */
export default function RecordView({
  tableTitle = '',
  columns,
  rows,
  index,
  fields,
  relations,
  container,
  onNavigate,
  onClose,
  onOpenRelated,
  onAsk,
}) {
  const panelRef = useRef(null)
  const closeRef = useRef(null)
  const onCloseRef = useRef(onClose)
  const headingId = useId()
  const [showBlank, setShowBlank] = useState(false)
  const row = rows[index]

  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    closeRef.current?.focus()
  }, [])

  // Native listener so Escape is stopped before it bubbles to an enclosing Bootstrap modal and closes it too.
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return undefined
    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        event.stopPropagation()
        onCloseRef.current?.()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = [...panel.querySelectorAll(FOCUSABLE)].filter((element) => element.offsetParent !== null)
      if (focusable.length === 0) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    panel.addEventListener('keydown', onKeyDown)
    return () => panel.removeEventListener('keydown', onKeyDown)
  }, [])

  // Stepping records can disable the button that had focus; keep focus inside the panel and start at the top.
  useEffect(() => {
    const panel = panelRef.current
    if (!panel) return
    panel.querySelector('.record-view-body')?.scrollTo?.({ top: 0 })
    const active = document.activeElement
    if (!panel.contains(active) || active?.disabled) panel.focus()
  }, [index])

  const { filled, blank } = useMemo(() => orderRecordFields(columns, row, fields), [columns, row, fields])
  const key = useMemo(() => recordKey(columns, row, fields), [columns, row, fields])

  if (!row) return null

  const linkValue = (column) => (isBlankValue(row[column]) ? '' : cellText(row[column]).trim())
  const outgoing = (relations?.outgoing || []).filter((relation) => linkValue(relation.column))
  const incoming = (relations?.incoming || []).filter((relation) => linkValue(relation.column))

  const panel = (
    <div className="record-view-layer">
      <div className="record-view-backdrop" onClick={onClose} aria-hidden="true"></div>
      <section
        ref={panelRef}
        className="record-view"
        role="dialog"
        aria-modal="true"
        aria-labelledby={headingId}
        tabIndex={-1}
      >
        <header className="record-view-header">
          <div className="record-view-title">
            <span className="record-view-kicker">{tableTitle ? `${tableTitle} record` : 'Record'}</span>
            <h2 id={headingId}>
              {key ? <>{key.column} <span className="record-view-key">{key.value}</span></> : 'Record with no values'}
            </h2>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="btn-close"
            aria-label="Close record"
            onClick={onClose}
          ></button>
        </header>

        <div className="record-view-nav">
          <button
            type="button"
            className="btn btn-sm btn-outline-secondary"
            onClick={() => onNavigate(index - 1)}
            disabled={index <= 0}
            aria-label="Previous record"
          >
            <i className="bi bi-chevron-left" aria-hidden="true"></i>
          </button>
          <span className="record-view-position">Row {index + 1} of {rows.length} on this page</span>
          <button
            type="button"
            className="btn btn-sm btn-outline-secondary"
            onClick={() => onNavigate(index + 1)}
            disabled={index >= rows.length - 1}
            aria-label="Next record"
          >
            <i className="bi bi-chevron-right" aria-hidden="true"></i>
          </button>
          {onAsk && (
            <button
              type="button"
              className="btn btn-sm btn-outline-success ms-auto"
              onClick={() => onAsk(askAboutRecordText(tableTitle, key))}
            >
              <i className="bi bi-chat-dots me-1" aria-hidden="true"></i>
              Ask ChatIPT about this record
            </button>
          )}
        </div>

        <div className="record-view-body">
          {filled.length === 0 && <p className="record-link-quiet">Every field in this record is blank.</p>}
          <FieldList entries={filled} />

          {blank.length > 0 && (
            <>
              <button
                type="button"
                className="btn btn-link btn-sm px-0"
                aria-expanded={showBlank}
                onClick={() => setShowBlank((value) => !value)}
              >
                <i className={`bi ${showBlank ? 'bi-chevron-up' : 'bi-chevron-down'} me-1`} aria-hidden="true"></i>
                {showBlank ? 'Hide' : 'Show'} {pluralize(blank.length, 'blank field')}
              </button>
              {showBlank && <FieldList entries={blank} />}
            </>
          )}

          {(outgoing.length > 0 || incoming.length > 0) && (
            <section className="record-links" aria-label="Linked records">
              <h3>Linked records</h3>
              <ul>
                {outgoing.map((relation) => (
                  <OutgoingLink
                    key={`out:${relation.column}:${relation.targetTableId}:${relation.targetColumn}:${linkValue(relation.column)}`}
                    relation={relation}
                    value={linkValue(relation.column)}
                    onOpenRelated={onOpenRelated}
                  />
                ))}
                {incoming.map((relation) => (
                  <IncomingLink
                    key={`in:${relation.column}:${relation.sourceTableId}:${relation.sourceColumn}:${linkValue(relation.column)}`}
                    relation={relation}
                    value={linkValue(relation.column)}
                    onOpenRelated={onOpenRelated}
                  />
                ))}
              </ul>
            </section>
          )}
        </div>
      </section>
    </div>
  )

  return createPortal(panel, container || document.body)
}
