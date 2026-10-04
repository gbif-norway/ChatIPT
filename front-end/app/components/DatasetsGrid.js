'use client'
import { useEffect, useState } from 'react'
import config from '../config'
import { useDataset } from '../contexts/DatasetContext'
import { useAuth } from '../contexts/AuthContext'
import { getStatusMeta } from '../utils/datasetPresentation'
import { datasetDisplayName, datasetKind } from '../utils/datasetKind.mjs'
import NewDatasetChooser from './NewDatasetChooser'

export default function DatasetsGrid({ onOpenDataset, onNewDataset, onConvertArchive, onShowWelcome }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [deleting, setDeleting] = useState(null) // Track which dataset is being deleted
  const [choosing, setChoosing] = useState(false)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const { deleteDataset } = useDataset()
  const { user } = useAuth()

  const fetchDatasets = async () => {
    try {
      setRefreshing(true)
      const response = await fetch(`${config.baseUrl}/api/my-datasets/`, { credentials: 'include' })
      if (!response.ok) throw new Error('Your datasets could not be loaded. Please try again.')
      const data = await response.json()
      setItems(data)
      setError(null)
    } catch (e) {
      setError(String(e))
    } finally {
      setRefreshing(false)
    }
  }

  useEffect(() => {
    fetchDatasets()
  }, [])

  const getDisplayName = (dataset) => datasetDisplayName(dataset, 'Untitled Dataset')

  const pluralize = (count, singular) => `${count.toLocaleString()} ${singular}${count === 1 ? '' : 's'}`

  const getCountSummary = (dataset) => {
    const counts = dataset.counts || {}
    const resources = counts.resources || {}
    const primaryCounts = [
      ['occurrence', 'occurrence'],
      ['event', 'event'],
      ['material', 'material'],
    ]
      .filter(([resource]) => resources[resource] !== undefined)
      .map(([resource, label]) => pluralize(resources[resource], label))

    if (Object.keys(resources).length > 0) {
      return {
        primary: primaryCounts,
        package: `${pluralize(Object.keys(resources).length, 'linked table')}`,
      }
    }

    return {
      primary: [pluralize(counts.source_rows || 0, 'source row')],
      package: null,
    }
  }

  const handleDeleteDataset = async (dataset) => {
    if (!window.confirm(`Are you sure you want to delete "${getDisplayName(dataset)}"? This action cannot be undone.`)) {
      return
    }

    try {
      setDeleting(dataset.id)
      await deleteDataset(dataset.id)
      // Refresh the list after successful deletion
      await fetchDatasets()
    } catch (e) {
      alert(`Failed to delete dataset: ${e.message}`)
    } finally {
      setDeleting(null)
    }
  }

  if (error) return <div className="alert alert-danger" role="alert">{error}<button className="btn btn-sm btn-outline-danger ms-2" onClick={fetchDatasets}>Try again</button></div>
  if (!items) return <div className="spinner-border" role="status"><span className="visually-hidden">Loading...</span></div>

  const chooser = (
    <NewDatasetChooser
      show={choosing}
      onHide={() => setChoosing(false)}
      onStartOwnData={onNewDataset}
      onConvertArchive={onConvertArchive}
    />
  )

  if (items.length === 0) {
    return (
      <div className="empty-datasets">
        <i className="bi bi-flower1" aria-hidden="true" />
        <h1 className="h3 mt-3">Your data has a story to tell</h1>
        <p className="text-body-secondary">Start with your own data files, or convert an existing Darwin Core Archive.</p>
        <button className="btn btn-primary" onClick={() => setChoosing(true)}>
          <i className="bi bi-plus-lg me-2" aria-hidden="true"></i>New dataset
        </button>
        {chooser}
      </div>
    )
  }

  const filteredItems = items.filter(dataset =>
    `${getDisplayName(dataset)} ${dataset.description || ''}`.toLowerCase().includes(query.trim().toLowerCase()) &&
    (filter === 'all' || (filter === 'conversion' ? dataset.workflow_type === 'dwca_conversion' : dataset.workflow_type !== 'dwca_conversion')))

  return (
    <div className="dashboard">
      {chooser}
      <div className="dashboard-heading">
        <div><span className="eyebrow">Your workspace</span><h1>My datasets</h1>
          <p>A little help, from raw data to ready to share.</p></div>
        <div className="d-flex flex-wrap gap-2">
          <button
            className="btn btn-link"
            onClick={onShowWelcome}
            title="See what is new in ChatIPT"
          >
            <i className="bi bi-stars me-1" aria-hidden="true"></i>
            What&apos;s new
          </button>
          <button className="btn btn-primary" onClick={() => setChoosing(true)}>
            <i className="bi bi-plus-lg me-2" aria-hidden="true"></i>New dataset
          </button>
        </div>
      </div>
      <div className="dashboard-toolbar">
        <div className="dataset-search"><i className="bi bi-search" aria-hidden="true" /><input type="search" className="form-control" aria-label="Search datasets" placeholder="Find a dataset…" value={query} onChange={event => setQuery(event.target.value)} /></div>
        <select className="form-select w-auto" aria-label="Filter datasets by workflow" value={filter} onChange={event => setFilter(event.target.value)}>
          <option value="all">All datasets</option><option value="publication">Data publication</option><option value="conversion">Archive conversions</option>
        </select>
        <span className="dataset-total" role="status">{filteredItems.length} {filteredItems.length === 1 ? 'dataset' : 'datasets'}</span>
        <button className="btn btn-outline-secondary btn-sm" aria-label="Refresh datasets" title="Refresh datasets" disabled={refreshing} onClick={fetchDatasets}><i className={`bi bi-arrow-clockwise ${refreshing ? 'spinner-border spinner-border-sm' : ''}`} aria-hidden="true" /></button>
      </div>
      {filteredItems.length === 0 && <div className="empty-datasets"><p>No datasets match your search.</p><button className="btn btn-outline-primary" onClick={() => { setQuery(''); setFilter('all') }}>Clear filters</button></div>}

      <div className="row g-3">
        {filteredItems.map(d => {
          const countSummary = getCountSummary(d)
          const statusMeta = getStatusMeta(d.status)
          const kind = datasetKind(d)
          return (
          <div key={d.id} className="col-12 col-md-6 col-lg-4">
            <div className="card h-100 dataset-card">
              <div className="card-body d-flex flex-column">
                <div className="d-flex justify-content-between align-items-center flex-wrap gap-2"><span className={`kind-badge kind-badge-${kind.key}`}>
                  <i className={`bi ${kind.icon}`} aria-hidden="true"></i>{kind.label}
                </span>
                  <span className={`badge ${statusMeta.badgeClass}`}>
                    {statusMeta.label}
                  </span>
                </div>
                <h2 className="card-title">{getDisplayName(d)}</h2>
                {d.description && <p className="card-text mt-2 text-truncate" style={{maxHeight: 48}}>{d.description}</p>}
                <div className="mt-auto text-body-secondary dataset-card-meta">
                  {countSummary.primary.map(label => <div key={label}>{label}</div>)}
                  {countSummary.package && <div>{countSummary.package}</div>}
                  <div>Updated {new Date(d.last_updated).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}</div>
                  <div>{d.package_ready ? (d.workflow_type === 'dwca_conversion' ? 'Converted package ready' : 'Publication packages ready') : `${d.progress.done} of ${d.progress.total} steps complete`}</div>
                  {/* Show dataset user ORCID for superusers */}
                  {user && user.is_superuser && d.user_info && d.user_info.orcid_id && (
                    <div className="mt-1">
                      <i className="bi bi-person-circle me-1"></i>
                      Owner:
                      <a
                        href={`https://orcid.org/${d.user_info.orcid_id}`}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-decoration-none ms-1"
                      >
                        {d.user_info.orcid_id}
                      </a>
                      {d.user_info.first_name && d.user_info.last_name && (
                        <span className="ms-1">
                          ({d.user_info.first_name} {d.user_info.last_name})
                        </span>
                      )}
                    </div>
                  )}
                </div>
                <div className="d-flex gap-2 mt-3">
                  <button className="btn btn-outline-primary flex-grow-1" onClick={() => onOpenDataset(d.id)}>
                    Open dataset<i className="bi bi-arrow-right ms-2" aria-hidden="true" />
                  </button>
                  <button
                    className="btn btn-outline-danger delete-dataset"
                    onClick={() => handleDeleteDataset(d)}
                    disabled={deleting === d.id}
                    title="Delete dataset"
                    aria-label={`Delete ${getDisplayName(d)}`}
                  >
                    {deleting === d.id ? (
                      <i className="bi bi-spinner bi-spin"></i>
                    ) : (
                      <i className="bi bi-trash"></i>
                    )}
                  </button>
                </div>
              </div>
            </div>
          </div>
          )
        })}
      </div>
    </div>
  )
}
