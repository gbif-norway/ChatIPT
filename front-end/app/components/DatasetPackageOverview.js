'use client'

import {
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

export default function DatasetPackageOverview({ dataset, tables }) {
  const files = Array.isArray(dataset?.user_files) ? dataset.user_files : []
  const resources = getResourceRows(dataset, tables)
  const accounting = getAccountingSummary(dataset)
  const storyItems = getStoryItems(resources)
  const validation = dataset?.dwc_dp_validation || {}
  const ready = Boolean(dataset?.package_ready)
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
        <p className="small mb-0 mt-1">
          ChatIPT is examining your source data before organising it into linked Darwin Core tables.
        </p>
      ) : (
        <p className="small mb-0 mt-1">
          {ready ? 'ChatIPT organised' : 'ChatIPT is organising'}{' '}
          {accounting ? `${pluralize(accounting.sourceRows, 'source row')} into ` : ''}
          {storyItems.length > 0 ? naturalList(storyItems) : pluralize(resources.length, 'linked table')}
          {storyItems.length > 0 ? ` across ${pluralize(resources.length, 'linked table')}.` : '.'}
        </p>
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
