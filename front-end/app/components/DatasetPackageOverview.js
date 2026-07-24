'use client'

import { useCallback, useEffect, useState, useSyncExternalStore } from 'react'

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

const getAccountingSummary = (dataset) => {
  const sources = dataset?.dwc_dp_accounting?.declaration?.sources
  if (!Array.isArray(sources) || sources.length === 0) {
    return null
  }

  return sources.reduce((summary, source) => ({
    sourceTables: summary.sourceTables + 1,
    sourceRows: summary.sourceRows + Number(source.source_rows || 0),
    accountedRows: summary.accountedRows + Number(source.rows_accounted || 0),
    omittedRows: summary.omittedRows + Number(source.omitted_rows || 0),
  }), {
    sourceTables: 0,
    sourceRows: 0,
    accountedRows: 0,
    omittedRows: 0,
  })
}

const getResourceRows = (dataset, tables) => {
  const validationResources = Array.isArray(dataset?.dwc_dp_validation?.resources)
    ? dataset.dwc_dp_validation.resources
    : []
  const receiptResourceRows = Array.isArray(dataset?.dwc_dp_accounting?.resources)
    ? dataset.dwc_dp_accounting.resources
    : []
  const receiptResources = receiptResourceRows.map((resource) => resource.title)
  const resourceNames = [...new Set([...receiptResources, ...validationResources])]

  return resourceNames
    .map((name) => {
      const table = tables.find((candidate) => candidate.title === name)
      const receipt = receiptResourceRows.find((candidate) => candidate.title === name)
      return {
        name,
        tableId: table?.id ?? receipt?.table_id ?? null,
        rowCount: table?.df?.length ?? receipt?.row_count ?? 0,
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

const profileVersion = (profileUrl) => {
  const match = String(profileUrl || '').match(/dwc-dp\/([^/]+)\/dwc-dp-profile/)
  return match?.[1] || 'Unknown'
}

const openPackageExplorer = async () => {
  const bootstrap = await import('bootstrap/dist/js/bootstrap.bundle.min.js')
  const modalElement = document.getElementById('packageExplorerModal')
  if (modalElement) bootstrap.Modal.getOrCreateInstance(modalElement).show()
}

const notificationStorageKey = (datasetId) => `chatipt:attention-notification:${datasetId}`
const notificationPreferenceEvent = 'chatipt:attention-notification-change'

const getNotificationStatus = (datasetId) => {
  if (!datasetId || typeof window === 'undefined') return 'idle'
  if (!('Notification' in window)) return 'unsupported'
  if (window.Notification.permission === 'denied') return 'blocked'

  const storedPreference = window.localStorage.getItem(notificationStorageKey(datasetId))
  if (storedPreference === 'notified') return 'notified'
  if (storedPreference === 'armed' && window.Notification.permission === 'granted') return 'armed'
  return 'idle'
}

const subscribeToNotificationPreference = (onStoreChange) => {
  window.addEventListener('storage', onStoreChange)
  window.addEventListener(notificationPreferenceEvent, onStoreChange)
  return () => {
    window.removeEventListener('storage', onStoreChange)
    window.removeEventListener(notificationPreferenceEvent, onStoreChange)
  }
}

const announceNotificationPreferenceChange = () => {
  window.dispatchEvent(new Event(notificationPreferenceEvent))
}

export default function DatasetPackageOverview({ dataset, tables }) {
  const files = Array.isArray(dataset?.user_files) ? dataset.user_files : []
  const resources = getResourceRows(dataset, tables)
  const accounting = getAccountingSummary(dataset)
  const storyItems = getStoryItems(resources)
  const validation = dataset?.dwc_dp_validation || {}
  const ready = Boolean(dataset?.package_ready)
  const datasetStatus = getDatasetStatus(dataset)
  const isWorking = datasetStatus === 'preparing'
  const attentionRequired = ['needs_input', 'ready', 'published'].includes(datasetStatus)
  const workComplete = ['ready', 'published'].includes(datasetStatus)
  const datasetId = dataset?.id
  const [isRequestingNotification, setIsRequestingNotification] = useState(false)
  const getNotificationSnapshot = useCallback(
    () => getNotificationStatus(datasetId),
    [datasetId],
  )
  const notificationStatus = useSyncExternalStore(
    subscribeToNotificationPreference,
    getNotificationSnapshot,
    () => 'idle',
  )
  const inputRows = accounting?.sourceRows ?? (
    resources.length === 0
      ? tables.reduce((sum, table) => sum + Number(table.df?.length || 0), 0)
      : null
  )

  const sourceSummary = files.length > 0
    ? `${pluralize(files.length, 'uploaded file')}${inputRows !== null ? ` · ${pluralize(inputRows, 'source row')}` : ''}`
    : 'Waiting for source files'
  const accountingTrustText = ready && accounting
    ? (
        accounting.omittedRows === 0
          ? `All ${accounting.sourceRows.toLocaleString()} source rows were accounted for; none were omitted.`
          : `${accounting.accountedRows.toLocaleString()} source rows were represented and ${accounting.omittedRows.toLocaleString()} were explicitly omitted.`
      )
    : ''
  const validationTrustText = ready
    ? `Package validation passed${
        Array.isArray(validation.warnings) && validation.warnings.length > 0
          ? ` with ${pluralize(validation.warnings.length, 'advisory warning')}`
          : ''
      }.`
    : ''
  const trustText = [accountingTrustText, validationTrustText].filter(Boolean).join(' ')
  useEffect(() => {
    if (
      !attentionRequired
      || !datasetId
      || notificationStatus !== 'armed'
      || typeof window === 'undefined'
    ) {
      return
    }

    const storageKey = notificationStorageKey(datasetId)
    if (window.localStorage.getItem(storageKey) !== 'armed') return

    window.localStorage.setItem(storageKey, 'notified')
    announceNotificationPreferenceChange()

    if ('Notification' in window && window.Notification.permission === 'granted') {
      try {
        const notification = new window.Notification(
          workComplete ? 'Your ChatIPT package is ready' : 'ChatIPT needs your attention',
          {
            body: workComplete
              ? (
                  dataset?.title
                    ? `${dataset.title} is ready to review and download.`
                    : 'Your dataset is ready to review and download.'
                )
              : (
                  dataset?.title
                    ? `ChatIPT is waiting for your input on ${dataset.title}.`
                    : 'ChatIPT is waiting for your input.'
                ),
            tag: `chatipt-attention-${datasetId}`,
          },
        )
        notification.onclick = () => {
          window.focus()
          notification.close()
          if (workComplete) {
            document.getElementById('publication-packages')?.scrollIntoView({ behavior: 'smooth' })
          }
        }
      } catch (error) {
        console.error('Unable to display ChatIPT attention notification:', error)
      }
    }
  }, [attentionRequired, dataset?.title, datasetId, notificationStatus, workComplete])

  const handleNotificationClick = async () => {
    if (!datasetId || typeof window === 'undefined') return

    const storageKey = notificationStorageKey(datasetId)
    if (notificationStatus === 'armed') {
      window.localStorage.removeItem(storageKey)
      announceNotificationPreferenceChange()
      return
    }

    if (!('Notification' in window)) return

    setIsRequestingNotification(true)
    try {
      const permission = window.Notification.permission === 'default'
        ? await window.Notification.requestPermission()
        : window.Notification.permission

      if (permission === 'granted') {
        window.localStorage.setItem(storageKey, 'armed')
      } else {
        window.localStorage.removeItem(storageKey)
      }
      announceNotificationPreferenceChange()
    } catch (error) {
      console.error('Unable to enable ChatIPT attention notifications:', error)
    } finally {
      setIsRequestingNotification(false)
    }
  }

  const displayedNotificationStatus = isRequestingNotification ? 'requesting' : notificationStatus
  const notificationButton = isWorking && datasetId ? (
    <button
      type="button"
      className={`btn btn-sm ${
        displayedNotificationStatus === 'armed' ? 'btn-outline-success' : 'btn-outline-secondary'
      }`}
      onClick={handleNotificationClick}
      disabled={['requesting', 'blocked', 'unsupported'].includes(displayedNotificationStatus)}
      title={
        displayedNotificationStatus === 'armed'
          ? 'Click to cancel this notification'
          : displayedNotificationStatus === 'blocked'
            ? 'Notifications are blocked in your browser settings'
            : displayedNotificationStatus === 'unsupported'
              ? 'This browser does not support desktop notifications'
              : 'Notify me when ChatIPT next needs my attention; keep this tab open'
      }
    >
      <i
        className={`bi ${displayedNotificationStatus === 'armed' ? 'bi-bell-fill' : 'bi-bell'} me-1`}
        aria-hidden="true"
      ></i>
      {displayedNotificationStatus === 'requesting'
        ? 'Enabling…'
        : displayedNotificationStatus === 'armed'
          ? 'Notification on'
          : displayedNotificationStatus === 'blocked'
            ? 'Notifications blocked'
            : displayedNotificationStatus === 'unsupported'
              ? 'Notifications unavailable'
              : 'Notify me'}
    </button>
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
          {notificationButton}
        </div>
      ) : (
        <div className="d-flex flex-wrap align-items-center gap-2 mt-1">
          <p className="small mb-0">
            {ready ? 'ChatIPT organised' : 'ChatIPT is organising'}{' '}
            {accounting ? `${pluralize(accounting.sourceRows, 'source row')} into ` : ''}
            {storyItems.length > 0 ? naturalList(storyItems) : pluralize(resources.length, 'linked table')}
            {storyItems.length > 0 ? ` across ${pluralize(resources.length, 'linked table')}.` : '.'}
          </p>
          {notificationButton}
        </div>
      )}
      {notificationStatus === 'armed' && (
        <p className="small text-muted mb-0 mt-1" role="status">
          You’ll get a browser notification when ChatIPT next needs your attention. Keep this tab open.
        </p>
      )}
      {notificationStatus === 'notified' && attentionRequired && (
        <div className="alert alert-success py-2 px-3 mb-0 mt-2" role="status">
          <i className="bi bi-check-circle-fill me-2" aria-hidden="true"></i>
          {workComplete
            ? 'Your package is ready to review and download.'
            : 'ChatIPT is ready for your input.'}
        </div>
      )}
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
              DwC-DP profile {profileVersion(standard.profile)} ·{' '}
              {schema.source ? (
                <a href={schema.source} target="_blank" rel="noopener noreferrer">
                  schema {schema.version || 'Unknown'}
                </a>
              ) : `schema ${schema.version || 'Unknown'}`}
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
            <p className="small mb-3">Simplified projection for current GBIF publication workflows</p>
            {dataset.dwca_url && (
              <a href={dataset.dwca_url} className="btn btn-outline-secondary btn-sm">
                <i className="bi bi-download me-1" aria-hidden="true"></i>
                Download DwC-A
              </a>
            )}
            <p className="small text-muted mb-0 mt-3">
              The DwC-A is a simplified projection derived from the full package. Some relationships
              and richer structures are preserved only in the DwC-DP.{' '}
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
