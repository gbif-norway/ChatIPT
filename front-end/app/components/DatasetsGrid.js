'use client'
import { useEffect, useState } from 'react'
import config from '../config'
import { useDataset } from '../contexts/DatasetContext'

export default function DatasetsGrid({ onOpenDataset, onNewDataset }) {
  const [items, setItems] = useState(null)
  const [error, setError] = useState(null)
  const [refreshing, setRefreshing] = useState(false)
  const [deleting, setDeleting] = useState(null) // Track which dataset is being deleted
  const { deleteDataset } = useDataset()

  const fetchDatasets = async () => {
    try {
      setRefreshing(true)
      const response = await fetch(`${config.baseUrl}/api/my-datasets/`, { credentials: 'include' })
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

  const handleDeleteDataset = async (dataset) => {
    if (!window.confirm(`Are you sure you want to delete "${dataset.title || dataset.filename}"? This action cannot be undone.`)) {
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

  if (error) return <div className="alert alert-danger">{error}</div>
  if (!items) return <div className="spinner-border" role="status"><span className="visually-hidden">Loading...</span></div>

  if (items.length === 0) {
    return (
      <div className="text-center my-5">
        <p>You don't have any datasets yet.</p>
        <button className="btn btn-primary btn-lg" onClick={onNewDataset}>
          <i className="bi bi-plus-circle me-2"></i>Add new dataset
        </button>
      </div>
    )
  }

  return (
    <>
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2><i className="bi bi-grid-3x3-gap me-2"></i>My datasets</h2>
        <div className="d-flex gap-2">
          <button 
            className="btn btn-outline-secondary" 
            onClick={fetchDatasets}
            disabled={refreshing}
          >
            <i className={`bi bi-arrow-clockwise me-1 ${refreshing ? 'spinner-border spinner-border-sm' : ''}`}></i>
            Refresh
          </button>
          <button className="btn btn-primary" onClick={onNewDataset}>
            <i className="bi bi-plus-circle me-2"></i>Add new dataset
          </button>
        </div>
      </div>

      <div className="row g-3">
        {items.map(d => (
          <div key={d.id} className="col-12 col-md-6 col-lg-4">
            <div className="card h-100">
              <div className="card-body d-flex flex-column">
                <div className="d-flex justify-content-between align-items-start">
                  <h5 className="card-title mb-0">{d.title || d.filename}</h5>
                  <span className={`badge text-bg-${d.status === 'published' ? 'success' : d.status === 'rejected' ? 'warning' : d.status === 'processing' ? 'primary' : 'secondary'}`}>
                    {d.status}
                  </span>
                </div>
                {d.description && <p className="card-text mt-2 text-truncate" style={{maxHeight: 48}}>{d.description}</p>}
                <div className="mt-auto small text-muted">
                  <div>{d.record_count} records • {d.dwc_core || 'unknown'}</div>
                  <div>Updated {new Date(d.last_updated).toLocaleString()}</div>
                  <div>Progress {d.progress.done}/{d.progress.total - 1}</div>
                  {d.last_message_preview && <div className="text-truncate">"{d.last_message_preview}"</div>}
                </div>
                <div className="d-flex gap-2 mt-3">
                  <button className="btn btn-outline-primary flex-grow-1" onClick={() => onOpenDataset(d.id)}>
                    Open
                  </button>
                  <button 
                    className="btn btn-outline-danger" 
                    onClick={() => handleDeleteDataset(d)}
                    disabled={deleting === d.id}
                    title="Delete dataset"
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
        ))}
      </div>
    </>
  )
}