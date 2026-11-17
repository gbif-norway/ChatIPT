'use client'

import { useEffect, useRef } from 'react'

import EmailNotification from './EmailNotification'
import { openPackageExplorer } from './PackageExplorer'
import { naturalList, pluralize, resourceCountLabel } from '../utils/datasetPresentation'
import {
  FILE_ICONS,
  archiveTableLine,
  currentStageName,
  dwcaCheck,
  dwcDpState,
  fileKind,
  panelStatus,
} from '../utils/datasetPanel.mjs'
import { datasetIsActivelyProcessing } from '../utils/workflowState.mjs'
import '../dataset-panel.css'

// The order people read a package in: what happened, what was found, then the supporting tables.
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

const STATUS_ICONS = {
  primary: 'bi-arrow-repeat',
  warning: 'bi-chat-left-text',
  secondary: 'bi-pause-circle',
  danger: 'bi-exclamation-triangle',
  success: 'bi-check-circle',
}

// "12 events, 340 occurrences and 18 assertions": the main tables first, assertion tables added up.
const packageStory = (packageTables) => {
  const counts = new Map(packageTables.map((table) => [table.title, table.row_count || 0]))
  const items = MAIN_RESOURCE_NAMES
    .filter((name) => counts.has(name))
    .map((name) => resourceCountLabel(name, counts.get(name)))
  const assertions = packageTables
    .filter((table) => table.title.endsWith('-assertion'))
    .reduce((sum, table) => sum + (table.row_count || 0), 0)
  if (assertions > 0) items.push(pluralize(assertions, 'assertion'))
  return items
}

const profileVersion = (profileUrl, schemaVersion) => {
  const match = String(profileUrl || '').match(/dwc-dp\/([^/]+)\/dwc-dp-profile/)
  return match?.[1] || schemaVersion || 'unknown'
}

const formatDate = (value) => {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

// A small (i) that explains a term on hover or focus. Bootstrap draws the tooltip in <body>, so the
// panel's own scrolling does not clip it.
function Tip({ text, label = 'More information' }) {
  const ref = useRef(null)
  useEffect(() => {
    let tooltip = null
    let cancelled = false
    import('bootstrap/dist/js/bootstrap.bundle.min.js').then((bootstrap) => {
      const Tooltip = bootstrap.Tooltip || bootstrap.default?.Tooltip
      if (cancelled || !ref.current || !Tooltip) return
      tooltip = new Tooltip(ref.current, { title: text, placement: 'top', trigger: 'hover focus' })
    })
    return () => {
      cancelled = true
      tooltip?.dispose()
    }
  }, [text])
  return (
    <button type="button" ref={ref} className="dataset-panel-tip" aria-label={`${label}: ${text}`}>
      <i className="bi bi-info-circle" aria-hidden="true"></i>
    </button>
  )
}

function StateBadge({ state }) {
  if (!state) return null
  return (
    <span className={`dataset-panel-state is-${state.tone}`} title={state.hint}>
      {state.tone === 'info' && <span className="spinner-border spinner-border-sm" aria-hidden="true"></span>}
      {state.label}
    </span>
  )
}

/**
 * The right-hand side of the dataset page: what is happening now, the packages once they exist, and the
 * files that were uploaded. Tables are explored in the package explorer modal, not here.
 *
 * status: getDatasetStatus(dataset). tables: the dataset's tables (row counts and the source tables).
 */
export default function DatasetPanel({
  dataset,
  status,
  tables,
  tablesLoading = false,
  tablesError = '',
  onRetryTables,
  productionPublishMailto,
}) {
  const now = panelStatus(status, currentStageName(dataset))
  const packageTables = tables.filter((table) => table.is_dwc_dp)
  const sourceTables = tables.filter((table) => !table.is_dwc_dp)
  const files = Array.isArray(dataset?.user_files) ? dataset.user_files : []
  const hasPackage = packageTables.length > 0 || Boolean(dataset?.dwc_dp_url)
  const showPackages = hasPackage || Boolean(dataset?.dwca_url)
  const final = Boolean(dataset?.package_ready || dataset?.published_at)
  const story = packageStory(packageTables)
  const dpState = dwcDpState(dataset)
  const archive = dataset?.dwca_summary || null
  const archiveCheck = archive ? dwcaCheck(archive.validation) : null
  const standard = dataset?.dwc_dp_standard || {}
  const schema = standard.schema || dataset?.dwc_dp_validation?.schema || {}
  const published = status === 'published'

  return (
    <aside className="dataset-panel" aria-label="Dataset summary">
      <section className={`dataset-panel-section dataset-panel-now is-${now.tone}`} aria-labelledby="dataset-panel-now">
        <div className="dataset-panel-now-title">
          <i className={`bi ${STATUS_ICONS[now.tone] || STATUS_ICONS.primary}`} aria-hidden="true"></i>
          <h2 id="dataset-panel-now">{now.title}</h2>
        </div>
        {published ? (
          <>
            {formatDate(dataset.published_at) && (
              <p className="small text-muted mb-2">On {formatDate(dataset.published_at)}</p>
            )}
            <div className="dataset-panel-actions">
              {dataset.gbif_url && (
                <a className="btn btn-sm btn-outline-primary" href={dataset.gbif_url} target="_blank" rel="noopener noreferrer">
                  <i className="bi bi-globe me-1" aria-hidden="true"></i>
                  View on GBIF (sandbox)
                </a>
              )}
              {productionPublishMailto && (
                <a className="btn btn-sm btn-success" href={productionPublishMailto}>
                  <i className="bi bi-envelope me-1" aria-hidden="true"></i>
                  Request publication to GBIF
                </a>
              )}
              <Tip
                label="About publishing"
                text="The sandbox is GBIF's test site. To publish on gbif.org, the GBIF Norway helpdesk needs the sandbox link; the button drafts that email."
              />
            </div>
          </>
        ) : (
          <>
            <p className="small mb-0">{now.detail}</p>
            <div className="dataset-panel-notify">
              <EmailNotification datasetId={dataset?.id} isWorking={datasetIsActivelyProcessing(dataset)} />
            </div>
          </>
        )}
      </section>

      {tablesError && (
        <div className="alert alert-warning d-flex align-items-center gap-2 py-2 px-3 small mb-3" role="alert">
          <span>{tablesError}</span>
          {onRetryTables && (
            <button type="button" className="btn btn-sm btn-outline-secondary ms-auto" onClick={onRetryTables}>Retry</button>
          )}
        </div>
      )}

      {showPackages && (
        <section className="dataset-panel-section" aria-labelledby="dataset-panel-packages">
          <div className="dataset-panel-heading">
            <h2 id="dataset-panel-packages">Packages</h2>
            {!final && (
              <>
                <span className="dataset-panel-state is-secondary">Draft</span>
                <Tip label="About drafts" text="Review is still in progress, so these can change. Download again after later edits." />
              </>
            )}
          </div>

          <article className="dataset-panel-card" aria-labelledby="dataset-panel-dwc-dp">
            <div className="dataset-panel-card-head">
              <h3 id="dataset-panel-dwc-dp">Darwin Core Data Package</h3>
              <Tip
                label="About the Data Package"
                text="The complete dataset, with every linked table. It is the authoritative version; the archive below is made from it."
              />
              <StateBadge state={dpState} />
            </div>
            {tablesLoading && packageTables.length === 0 ? (
              <p className="small text-muted mb-2" role="status">
                <span className="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>
                Loading row counts…
              </p>
            ) : packageTables.length > 0 && (
              <p className="small mb-2">
                {story.length > 0 ? `${naturalList(story)} in ` : ''}
                {pluralize(packageTables.length, 'linked table')}.
              </p>
            )}
            <div className="dataset-panel-actions">
              {packageTables.length > 0 && (
                <button type="button" className="btn btn-sm btn-success" onClick={() => openPackageExplorer({ focus: 'package' })}>
                  <i className="bi bi-diagram-3 me-1" aria-hidden="true"></i>
                  Explore
                </button>
              )}
              {dataset.dwc_dp_url && (
                <a className="btn btn-sm btn-outline-secondary" href={dataset.dwc_dp_url}>
                  <i className="bi bi-download me-1" aria-hidden="true"></i>
                  Download
                </a>
              )}
            </div>
            {dataset.dwc_dp_url && (schema.version || standard.profile) && (
              <p className="dataset-panel-meta">
                DwC-DP {profileVersion(standard.profile, schema.version)}
                {schema.source ? (
                  <>
                    {' · '}
                    <a href={schema.source} target="_blank" rel="noopener noreferrer">schema {schema.version || 'snapshot'}</a>
                  </>
                ) : ''}
              </p>
            )}
          </article>

          <article className="dataset-panel-card" aria-labelledby="dataset-panel-dwca">
            <div className="dataset-panel-card-head">
              <h3 id="dataset-panel-dwca">Darwin Core Archive</h3>
              <Tip
                label="About the archive"
                text="The format GBIF publishes today. It keeps as much of the Data Package as a core table with extensions can hold."
              />
              <StateBadge state={archiveCheck} />
            </div>
            {archive ? (
              <>
                {archive.tables.length > 0 && (
                  <ul className="dataset-panel-list">
                    {archive.tables.map((table) => (
                      <li key={table.row_type}>{archiveTableLine(table)}</li>
                    ))}
                  </ul>
                )}
                <div className="dataset-panel-actions">
                  <a className="btn btn-sm btn-outline-secondary" href={archive.url}>
                    <i className="bi bi-download me-1" aria-hidden="true"></i>
                    Download
                  </a>
                </div>
              </>
            ) : (
              <p className="small text-muted mb-0">Made from the Data Package once it is complete.</p>
            )}
          </article>
        </section>
      )}

      <section className="dataset-panel-section" aria-labelledby="dataset-panel-files">
        {/* Open while the files are the whole story; folded away once there is a package. */}
        <details className="dataset-panel-files" open={!hasPackage}>
          <summary>
            <h2 id="dataset-panel-files">Your files</h2>
            <span className="text-muted small">{files.length}</span>
          </summary>
          {files.length > 0 ? (
            <ul className="dataset-panel-file-list">
              {files.map((file, index) => {
                const kind = fileKind(file)
                const name = file.filename || 'Unnamed file'
                return (
                  <li key={file.id ?? `${name}-${index}`}>
                    <i className={`bi ${FILE_ICONS[kind]}`} aria-hidden="true"></i>
                    {file.file_url ? (
                      <a href={file.file_url} target="_blank" rel="noopener noreferrer" title={`Download ${name} as you uploaded it`}>{name}</a>
                    ) : <span>{name}</span>}
                  </li>
                )
              })}
            </ul>
          ) : (
            <p className="small text-muted mb-2">No files uploaded yet.</p>
          )}
          <div className="dataset-panel-actions">
            {sourceTables.length > 0 && (
              <button type="button" className="btn btn-sm btn-outline-secondary" onClick={() => openPackageExplorer({ focus: 'sources' })}>
                <i className="bi bi-table me-1" aria-hidden="true"></i>
                View tables
              </button>
            )}
            {(dataset?.structure_notes || files.length > 0) && (
              <button type="button" className="btn btn-sm btn-outline-secondary" data-bs-toggle="modal" data-bs-target="#structureNotesModal">
                <i className="bi bi-journal-text me-1" aria-hidden="true"></i>
                Notes
              </button>
            )}
            <Tip
              label="About your files"
              text="Your files stay exactly as you uploaded them. ChatIPT works on copies: View tables shows those copies as they are now, and Notes says what was changed."
            />
          </div>
        </details>
      </section>
    </aside>
  )
}
