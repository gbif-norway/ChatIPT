'use client'

import { useEffect, useState } from 'react'

import config from '../config'
import { getCsrfToken } from '../utils/csrf'

import {
  getDatasetStatus,
  naturalList,
  pluralize,
  resourceCountLabel,
} from '../utils/datasetPresentation'

const MAIN_RESOURCE_NAMES = [
  'event',
  'occurrence',
  'material',
  'identification',
  'organism',
  'organism-interaction',
  'survey',
  'survey-target',
  'agent',
  'media',
  'molecular-protocol',
  'nucleotide-analysis',
  'nucleotide-sequence',
  'bibliographic-resource',
  'protocol',
  'provenance',
  'chronometric-age',
  'geological-context',
  'usage-policy',
]

const getResourceRows = (dataset, tables) => {
  const validationResources = Array.isArray(dataset?.dwc_dp_validation?.resources)
    ? dataset.dwc_dp_validation.resources
    : []
  const resourceNames = [...new Set(validationResources)]

  return resourceNames
    .map((name) => {
      const table = tables.find((candidate) => candidate.title === name)
      return {
        name,
        tableId: table?.id ?? null,
        rowCount: table?.row_count ?? 0,
      }
    })
    .sort((a, b) => {
      const aIndex = MAIN_RESOURCE_NAMES.indexOf(a.name)
      const bIndex = MAIN_RESOURCE_NAMES.indexOf(b.name)
      if (aIndex !== -1 || bIndex !== -1) {
        if (aIndex === -1) return 1
        if (bIndex === -1) return -1
        return aIndex - bIndex
      }
      return a.name.localeCompare(b.name)
    })
}

const getStoryItems = (resources) => {
  const resourceMap = new Map(resources.map((resource) => [resource.name, resource.rowCount]))
  const items = MAIN_RESOURCE_NAMES
    .filter((name) => resourceMap.has(name))
    .map((name) => resourceCountLabel(name, resourceMap.get(name)))

  const assertionRows = resources
    .filter((resource) => resource.name.endsWith('-assertion'))
    .reduce((sum, resource) => sum + resource.rowCount, 0)
  if (assertionRows > 0) {
    items.push(pluralize(assertionRows, 'assertion'))
  }

  return items
}

const profileVersion = (profileUrl, schemaVersion) => {
  const match = String(profileUrl || '').match(/dwc-dp\/([^/]+)\/dwc-dp-profile/)
  return match?.[1] || schemaVersion || 'Unknown'
}

const openPackageExplorer = async () => {
  const bootstrap = await import('bootstrap/dist/js/bootstrap.bundle.min.js')
  const modalElement = document.getElementById('packageExplorerModal')
  if (modalElement) bootstrap.Modal.getOrCreateInstance(modalElement).show()
}

export default function DatasetPackageOverview({ dataset, tables, tablesLoading = false }) {
  const files = Array.isArray(dataset?.user_files) ? dataset.user_files : []
  const resources = getResourceRows(dataset, tables)
  const resourceCountsLoading = tablesLoading
  const storyItems = getStoryItems(resources)
  const validation = dataset?.dwc_dp_validation || {}
  const ready = Boolean(dataset?.package_ready)
  const datasetStatus = getDatasetStatus(dataset)
  const isWorking = datasetStatus === 'preparing'
  const datasetId = dataset?.id
  const [showEmailNotificationModal, setShowEmailNotificationModal] = useState(false)
  const [emailNotification, setEmailNotification] = useState({ status: 'idle' })
  const [notificationEmail, setNotificationEmail] = useState('')
  const [emailNotificationError, setEmailNotificationError] = useState('')
  const [isSavingEmailNotification, setIsSavingEmailNotification] = useState(false)
  const emailNotificationArmed = ['pending', 'ready', 'sending'].includes(emailNotification.status)
  const sourceSummary = files.length > 0
    ? pluralize(files.length, 'uploaded file')
    : 'Waiting for source files'
  const validationTrustText = ready
    ? `Package validation passed${
        Array.isArray(validation.warnings) && validation.warnings.length > 0
          ? ` with ${pluralize(validation.warnings.length, 'advisory warning')}`
          : ''
      }.`
    : ''
  const trustText = validationTrustText

  useEffect(() => {
    if (!datasetId || !isWorking) return

    let cancelled = false
    const loadEmailNotification = async () => {
      try {
        const response = await fetch(
          `${config.baseUrl}/api/datasets/${datasetId}/attention-notification/`,
          { credentials: 'include' },
        )
        if (!response.ok) throw new Error('Unable to load email notification settings.')
        const data = await response.json()
        if (cancelled) return
        setEmailNotification(data)
        setNotificationEmail(data.email || data.suggested_email || '')
      } catch (error) {
        if (!cancelled) setEmailNotificationError(error.message)
      }
    }
    loadEmailNotification()
    return () => {
      cancelled = true
    }
  }, [datasetId, isWorking])

  useEffect(() => {
    if (!showEmailNotificationModal) return

    const closeOnEscape = (event) => {
      if (event.key === 'Escape' && !isSavingEmailNotification) {
        setShowEmailNotificationModal(false)
      }
    }
    document.addEventListener('keydown', closeOnEscape)
    document.body.classList.add('modal-open')
    return () => {
      document.removeEventListener('keydown', closeOnEscape)
      document.body.classList.remove('modal-open')
    }
  }, [isSavingEmailNotification, showEmailNotificationModal])

  const openEmailNotificationModal = () => {
    setEmailNotificationError('')
    setShowEmailNotificationModal(true)
  }

  const closeEmailNotificationModal = () => {
    if (!isSavingEmailNotification) setShowEmailNotificationModal(false)
  }

  const saveEmailNotification = async (event) => {
    event.preventDefault()
    if (!datasetId || !notificationEmail.trim()) return

    setIsSavingEmailNotification(true)
    setEmailNotificationError('')
    try {
      const csrfToken = await getCsrfToken()
      const headers = { 'Content-Type': 'application/json' }
      if (csrfToken) headers['X-CSRFToken'] = csrfToken
      const response = await fetch(
        `${config.baseUrl}/api/datasets/${datasetId}/attention-notification/`,
        {
          method: 'POST',
          credentials: 'include',
          headers,
          body: JSON.stringify({ email: notificationEmail.trim() }),
        },
      )
      const data = await response.json()
      if (!response.ok) {
        const message = data.email?.[0] || data.detail || 'Unable to enable email notifications.'
        throw new Error(message)
      }
      setEmailNotification(data)
      setNotificationEmail(data.email)
    } catch (error) {
      setEmailNotificationError(error.message)
    } finally {
      setIsSavingEmailNotification(false)
    }
  }

  const cancelEmailNotification = async () => {
    if (!datasetId) return

    setIsSavingEmailNotification(true)
    setEmailNotificationError('')
    try {
      const csrfToken = await getCsrfToken()
      const headers = {}
      if (csrfToken) headers['X-CSRFToken'] = csrfToken
      const response = await fetch(
        `${config.baseUrl}/api/datasets/${datasetId}/attention-notification/`,
        { method: 'DELETE', credentials: 'include', headers },
      )
      if (!response.ok) throw new Error('Unable to cancel the email notification.')
      setEmailNotification(await response.json())
    } catch (error) {
      setEmailNotificationError(error.message)
    } finally {
      setIsSavingEmailNotification(false)
    }
  }

  const notificationPrompt = isWorking && datasetId ? (
    <div className="d-inline-flex flex-wrap align-items-center gap-2">
      <span className="small text-muted">
        This process can take some time.
      </span>
      <button
        type="button"
        className={`btn btn-sm rounded-pill px-3 ${emailNotificationArmed ? 'btn-outline-success' : 'btn-warning'}`}
        onClick={openEmailNotificationModal}
      >
        <i className={`bi ${emailNotificationArmed ? 'bi-envelope-check-fill' : 'bi-envelope'} me-1`} aria-hidden="true"></i>
        {emailNotificationArmed ? 'Email notification set' : 'Notify me by email'}
      </button>
    </div>
  ) : null

  const emailNotificationModal = isWorking && datasetId && showEmailNotificationModal ? (
    <>
      <div
        className="modal fade show d-block"
        role="dialog"
        tabIndex="-1"
        aria-modal="true"
        aria-labelledby={`email-notification-title-${datasetId}`}
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeEmailNotificationModal()
        }}
      >
        <div className="modal-dialog modal-dialog-centered modal-sm">
          <div className="modal-content border-0 shadow-lg">
            <div className="modal-header border-0 pb-0 align-items-start">
              <div className="d-flex gap-3">
                <span className="bg-warning-subtle text-warning-emphasis rounded-circle d-inline-flex align-items-center justify-content-center flex-shrink-0" style={{ width: 40, height: 40 }}>
                  <i className="bi bi-envelope-paper" aria-hidden="true"></i>
                </span>
                <div>
                  <h2 className="modal-title fs-5" id={`email-notification-title-${datasetId}`}>
                    Notify me by email
                  </h2>
                  <p className="small text-muted mb-0 mt-1">
                    Step away while ChatIPT keeps working.
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="btn-close"
                aria-label="Close"
                onClick={closeEmailNotificationModal}
                disabled={isSavingEmailNotification}
              ></button>
            </div>

            <div className="modal-body pt-3">
              <p className="small mb-3">
                We’ll send one email when ChatIPT needs your input or your package is ready. <strong>Keep this tab open so processing can continue.</strong>
              </p>

              {emailNotificationArmed ? (
                <div className="text-center py-2">
                  <span className="d-inline-flex align-items-center justify-content-center rounded-circle bg-success-subtle text-success mb-3" style={{ width: 48, height: 48 }}>
                    <i className="bi bi-check-lg fs-4" aria-hidden="true"></i>
                  </span>
                  <h3 className="h6 mb-1">Email notification set</h3>
                  <p className="small text-muted mb-3 text-break">{emailNotification.email}</p>
                  <button
                    type="button"
                    className="btn btn-sm btn-outline-danger"
                    onClick={cancelEmailNotification}
                    disabled={isSavingEmailNotification}
                  >
                    {isSavingEmailNotification ? 'Cancelling…' : 'Cancel notification'}
                  </button>
                </div>
              ) : (
                <form onSubmit={saveEmailNotification}>
                  <label className="form-label small fw-semibold" htmlFor={`notification-email-${datasetId}`}>
                    Email address
                  </label>
                  <input
                    id={`notification-email-${datasetId}`}
                    className="form-control"
                    type="email"
                    value={notificationEmail}
                    onChange={(event) => setNotificationEmail(event.target.value)}
                    placeholder="you@example.org"
                    autoComplete="email"
                    autoFocus
                    required
                    disabled={isSavingEmailNotification}
                  />
                  <button
                    type="submit"
                    className="btn btn-warning w-100 mt-3"
                    disabled={isSavingEmailNotification || !notificationEmail.trim()}
                  >
                    <i className="bi bi-envelope-check me-2" aria-hidden="true"></i>
                    {isSavingEmailNotification ? 'Saving…' : 'Notify me'}
                  </button>
                </form>
              )}

              {emailNotificationError && (
                <p className="small text-danger mb-0 mt-2" role="alert">{emailNotificationError}</p>
              )}

              <div className="bg-body-tertiary rounded p-2 mt-3 d-flex gap-2">
                <i className="bi bi-info-circle text-muted flex-shrink-0" aria-hidden="true"></i>
                <p className="small text-muted mb-0">
                  First notification? Check your Spam folder and mark ChatIPT as “Not spam” so future messages reach your inbox.
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div className="modal-backdrop fade show"></div>
    </>
  ) : null

  return (
    <section className="dataset-overview mb-2" aria-labelledby="dataset-overview-title">
      <div className="dataset-overview-line">
        <div
          className={trustText ? 'dataset-overview-tooltip' : ''}
          tabIndex={trustText ? 0 : undefined}
          aria-describedby={trustText ? 'dataset-overview-trust' : undefined}
        >
          <h3 className="h6 mb-0" id="dataset-overview-title">
            {resources.length > 0 ? 'How your data is organised' : 'Dataset overview'}
          </h3>
          {trustText && (
            <span className="dataset-overview-tooltip-content" id="dataset-overview-trust" role="tooltip">
              {trustText}
            </span>
          )}
        </div>
        <span className="small text-muted">{sourceSummary}</span>
        {resources.length > 0 && (
          <>
            <button
              type="button"
              className="btn btn-sm btn-outline-success dataset-overview-explore"
              onClick={openPackageExplorer}
            >
              <i className="bi bi-diagram-3 me-1" aria-hidden="true"></i>
              Explore package
            </button>
          </>
        )}
      </div>

      {resources.length === 0 ? (
        <div className="d-flex flex-wrap align-items-center gap-2 mt-1">
          <p className="small mb-0">
            ChatIPT is examining your source data before organising it into linked Darwin Core tables.
          </p>
          {notificationPrompt}
        </div>
      ) : (
        <div className="d-flex flex-wrap align-items-center gap-2 mt-1">
          {resourceCountsLoading ? (
            <p className="small text-muted mb-0" role="status">
              <span className="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>
              Loading package row counts…
            </p>
          ) : (
            <p className="small mb-0">
              {ready ? 'ChatIPT organised' : 'ChatIPT is organising'}{' '}
              {storyItems.length > 0 ? naturalList(storyItems) : pluralize(resources.length, 'linked table')}
              {storyItems.length > 0 ? ` across ${pluralize(resources.length, 'linked table')}.` : '.'}
            </p>
          )}
          {notificationPrompt}
        </div>
      )}
      {emailNotificationModal}
    </section>
  )
}

export function PublicationPackageCards({ dataset, tables }) {
  const resources = getResourceRows(dataset, tables)
  const standard = dataset?.dwc_dp_standard || {}
  const schema = standard.schema || dataset?.dwc_dp_validation?.schema || {}
  const hasFinalPackages = Boolean(dataset?.package_ready || dataset?.published_at)

  if (!hasFinalPackages || (!dataset?.dwc_dp_url && !dataset?.dwca_url)) {
    return null
  }

  return (
    <section className="publication-packages mt-3" id="publication-packages" aria-labelledby="publication-packages-title">
      <h3 className="h6 mb-3" id="publication-packages-title">Publication packages</h3>
      <div className="row g-3">
        <div className="col-12 col-xl-6">
          <div className="publication-output-card h-100 border border-success rounded p-3">
            <h4 className="h6 mb-1">Darwin Core Data Package</h4>
            <p className="small mb-2">
              Complete linked dataset · {pluralize(resources.length, 'linked table')} · Validated
            </p>
            <p className="small text-muted mb-3">
              DwC-DP profile {profileVersion(standard.profile, schema.version)} ·{' '}
              {schema.source ? (
                <a href={schema.source} target="_blank" rel="noopener noreferrer">
                  schema snapshot {schema.version || 'Unknown'}{schema.issued ? ` (${schema.issued})` : ''}
                </a>
              ) : `schema snapshot ${schema.version || 'Unknown'}${schema.issued ? ` (${schema.issued})` : ''}`}
            </p>
            {dataset.dwc_dp_url && (
              <div className="d-flex flex-column align-items-start gap-2">
                <a href={dataset.dwc_dp_url} className="btn btn-warning btn-sm">
                  <i className="bi bi-download me-1" aria-hidden="true"></i>
                  Download DwC-DP
                </a>
                <button
                  type="button"
                  className="btn btn-outline-success btn-sm"
                  onClick={openPackageExplorer}
                >
                  <i className="bi bi-diagram-3 me-1" aria-hidden="true"></i>
                  Explore package
                </button>
              </div>
            )}
          </div>
        </div>
        <div className="col-12 col-xl-6">
          <div className="publication-output-card h-100 border rounded p-3">
            <h4 className="h6 mb-1">Darwin Core Archive</h4>
            <p className="small mb-3">Standards-compliant projection for current GBIF publication workflows</p>
            {dataset.dwca_url && (
              <a href={dataset.dwca_url} className="btn btn-outline-secondary btn-sm">
                <i className="bi bi-download me-1" aria-hidden="true"></i>
                Download DwC-A
              </a>
            )}
            <p className="small text-muted mb-0 mt-3">
              The DwC-A preserves as much of the full package as its core-and-extension structure
              can represent. The DwC-DP remains the complete authoritative package.{' '}
              <a
                href="https://gbif.github.io/dwc-dp/qrg/"
                target="_blank"
                rel="noopener noreferrer"
              >
                Learn about the DwC-DP standard
              </a>.
            </p>
          </div>
        </div>
      </div>
    </section>
  )
}
