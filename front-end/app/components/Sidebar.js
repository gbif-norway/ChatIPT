'use client'

import { useState, useEffect } from 'react'
import { useAuth } from '../contexts/AuthContext'
import { useTheme } from '../contexts/ThemeContext'
import config from '../config'

const Sidebar = ({ isOpen, onToggle, onDatasetSelect, currentDatasetId }) => {
  const { user } = useAuth()
  const { isDark } = useTheme()
  const [datasets, setDatasets] = useState([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (user) {
      fetchDatasets()
    }
  }, [user])

  const fetchDatasets = async () => {
    try {
      setLoading(true)
      const response = await fetch(`${config.baseUrl}/api/datasets/`, {
        credentials: 'include'
      })
      
      if (response.ok) {
        const data = await response.json()
        setDatasets(data)
      } else {
        setError('Failed to load datasets')
      }
    } catch (error) {
      console.error('Error fetching datasets:', error)
      setError('Failed to load datasets')
    } finally {
      setLoading(false)
    }
  }

  const formatDate = (dateString) => {
    const date = new Date(dateString)
    const now = new Date()
    const diffTime = Math.abs(now - date)
    const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24))
    
    if (diffDays === 1) {
      return 'Today'
    } else if (diffDays === 2) {
      return 'Yesterday'
    } else if (diffDays <= 7) {
      return `${diffDays - 1} days ago`
    } else {
      return date.toLocaleDateString()
    }
  }

  const getDatasetStatus = (dataset) => {
    if (dataset.published_at) {
      return { text: 'Published', class: 'text-success', icon: 'bi-check-circle' }
    } else if (dataset.rejected_at) {
      return { text: 'Rejected', class: 'text-danger', icon: 'bi-x-circle' }
    } else if (dataset.visible_agent_set && dataset.visible_agent_set.length > 0) {
      const lastAgent = dataset.visible_agent_set[dataset.visible_agent_set.length - 1]
      if (lastAgent.completed_at) {
        return { text: 'In Progress', class: 'text-warning', icon: 'bi-clock' }
      } else {
        return { text: 'Processing', class: 'text-info', icon: 'bi-arrow-clockwise' }
      }
    } else {
      return { text: 'New', class: 'text-secondary', icon: 'bi-plus-circle' }
    }
  }

  const getDatasetTitle = (dataset) => {
    if (dataset.title) {
      return dataset.title
    } else if (dataset.file) {
      return dataset.file.split('/').pop() || 'Untitled Dataset'
    } else {
      return 'Untitled Dataset'
    }
  }

  // Theme-specific styles
  const sidebarStyles = {
    position: 'fixed',
    top: 0,
    left: 0,
    height: '100vh',
    width: '320px',
    backgroundColor: isDark ? '#212529' : '#f8f9fa',
    borderRight: `1px solid ${isDark ? '#495057' : '#dee2e6'}`,
    zIndex: 1050,
    transform: isOpen ? 'translateX(0)' : 'translateX(-100%)',
    transition: 'transform 0.3s ease-in-out',
    overflowY: 'auto'
  }

  const headerStyles = {
    backgroundColor: isDark ? '#343a40' : '#fff',
    borderBottomColor: isDark ? '#495057 !important' : '#dee2e6 !important'
  }

  const getDatasetItemStyles = (isActive) => ({
    cursor: 'pointer',
    transition: 'all 0.2s ease',
    backgroundColor: isActive 
      ? (isDark ? 'rgba(13, 110, 253, 0.25)' : 'rgba(13, 110, 253, 0.1)')
      : (isDark ? '#343a40' : '#fff'),
    borderColor: isActive ? '#0d6efd' : (isDark ? '#6c757d' : '#dee2e6')
  })

  return (
    <>
      {/* Overlay for mobile */}
      {isOpen && (
        <div 
          className="sidebar-overlay d-lg-none" 
          onClick={onToggle}
          style={{
            position: 'fixed',
            top: 0,
            left: 0,
            right: 0,
            bottom: 0,
            backgroundColor: 'rgba(0, 0, 0, 0.5)',
            zIndex: 1040
          }}
        />
      )}

      {/* Sidebar */}
      <div className={`sidebar ${isOpen ? 'open' : ''}`} style={sidebarStyles}>
        {/* Header */}
        <div className="sidebar-header p-3 border-bottom" style={headerStyles}>
          <div className="d-flex justify-content-between align-items-center">
            <h6 className={`mb-0 fw-bold ${isDark ? 'text-light' : 'text-dark'}`}>
              Dataset History
            </h6>
            <button 
              className={`btn btn-sm ${isDark ? 'btn-outline-light' : 'btn-outline-secondary'} d-lg-none`}
              onClick={onToggle}
            >
              <i className="bi bi-x-lg"></i>
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="sidebar-content p-3">
          {loading ? (
            <div className="text-center py-4">
              <div className={`spinner-border spinner-border-sm ${isDark ? 'text-light' : 'text-dark'}`} role="status">
                <span className="visually-hidden">Loading...</span>
              </div>
              <p className={`mt-2 small ${isDark ? 'text-light-50' : 'text-muted'}`}>
                Loading datasets...
              </p>
            </div>
          ) : error ? (
            <div className="text-center py-4">
              <i className="bi bi-exclamation-triangle text-warning fs-4"></i>
              <p className={`mt-2 small ${isDark ? 'text-light-50' : 'text-muted'}`}>{error}</p>
              <button 
                className={`btn btn-sm ${isDark ? 'btn-outline-light' : 'btn-outline-primary'}`}
                onClick={fetchDatasets}
              >
                Try Again
              </button>
            </div>
          ) : datasets.length === 0 ? (
            <div className="text-center py-4">
              <i className={`bi bi-folder fs-4 ${isDark ? 'text-light-50' : 'text-muted'}`}></i>
              <p className={`mt-2 small ${isDark ? 'text-light-50' : 'text-muted'}`}>No datasets yet</p>
              <p className={`small ${isDark ? 'text-light-50' : 'text-muted'}`}>
                Upload your first dataset to get started
              </p>
            </div>
          ) : (
            <div className="dataset-list">
              {datasets.map((dataset) => {
                const status = getDatasetStatus(dataset)
                const title = getDatasetTitle(dataset)
                const isActive = currentDatasetId === dataset.id
                
                return (
                  <div 
                    key={dataset.id}
                    className={`dataset-item p-3 border rounded mb-2 cursor-pointer ${
                      isActive ? 'border-primary' : ''
                    }`}
                    onClick={() => onDatasetSelect(dataset.id)}
                    style={getDatasetItemStyles(isActive)}
                  >
                    <div className="d-flex justify-content-between align-items-start mb-2">
                      <h6 className={`mb-1 text-truncate ${isDark ? 'text-light' : 'text-dark'}`} style={{ maxWidth: '200px' }}>
                        {title}
                      </h6>
                      <small className={`${status.class}`}>
                        <i className={`bi ${status.icon} me-1`}></i>
                        {status.text}
                      </small>
                    </div>
                    
                    <div className="d-flex justify-content-between align-items-center">
                      <small className={isDark ? 'text-light-50' : 'text-muted'}>
                        {formatDate(dataset.created_at)}
                      </small>
                      
                      {dataset.visible_agent_set && dataset.visible_agent_set.length > 0 && (
                        <small className={isDark ? 'text-light-50' : 'text-muted'}>
                          {dataset.visible_agent_set.length} step{dataset.visible_agent_set.length !== 1 ? 's' : ''}
                        </small>
                      )}
                    </div>
                    
                    {dataset.description && (
                      <p className={`small mb-0 mt-2 ${isDark ? 'text-light-50' : 'text-muted'}`} style={{
                        display: '-webkit-box',
                        WebkitLineClamp: 2,
                        WebkitBoxOrient: 'vertical',
                        overflow: 'hidden'
                      }}>
                        {dataset.description}
                      </p>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>
    </>
  )
}

export default Sidebar 