'use client'

import DatasetsGrid from './components/DatasetsGrid'
import NewDatasetComposer from './components/NewDatasetComposer'
import ProtectedRoute from './components/ProtectedRoute'
import { useAuth } from './contexts/AuthContext'
import { DatasetProvider } from './contexts/DatasetContext'
import { useNavigation } from './components/HeaderWrapper'
import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import Accordion from 'react-bootstrap/Accordion'

const WELCOME_RELEASE_ID = 'dwc-dp-2026-07'

const HomeContent = () => {
  const router = useRouter()
  const { authenticated, user } = useAuth()
  const { updateNavigation } = useNavigation()
  const [mode, setMode] = useState('dashboard') // 'dashboard', 'upload'

  const handleDatasetSelect = useCallback((datasetId) => {
    router.push(`/dataset/${datasetId}`)
  }, [router])

  const handleNewDataset = useCallback(() => {
    setMode('upload')
    router.push('/?mode=upload')
  }, [router])

  const handleBackToDashboard = useCallback(() => {
    setMode('dashboard')
    router.push('/')
  }, [router])

  useEffect(() => {
    const requestedMode = new URLSearchParams(window.location.search).get('mode')
    if (requestedMode === 'upload') {
      setMode('upload')
      return
    }
    setMode('dashboard')
  }, [])

  const welcomeStorageKey = `chatipt-welcome-${WELCOME_RELEASE_ID}-${user?.id || user?.email || 'user'}`

  const showWelcomeModal = useCallback(async (ignoreSeen = false) => {
    if (!authenticated) return
    if (!ignoreSeen) {
      try {
        if (window.localStorage.getItem(welcomeStorageKey) === 'seen') return
      } catch (_) {
        // If persistent browser storage is unavailable, show once for this visit.
      }
    }

    const bootstrap = await import('bootstrap/dist/js/bootstrap.bundle.min.js')
    const modalElement = document.getElementById('myModal')
    if (!modalElement) return
    bootstrap.Modal.getOrCreateInstance(modalElement).show()
  }, [authenticated, welcomeStorageKey])

  useEffect(() => {
    showWelcomeModal()
  }, [showWelcomeModal])

  useEffect(() => {
    const modalElement = document.getElementById('myModal')
    if (!modalElement) return undefined

    const markReleaseSeen = () => {
      try {
        window.localStorage.setItem(welcomeStorageKey, 'seen')
      } catch (_) {
        // The modal can still be dismissed normally when storage is unavailable.
      }
    }
    modalElement.addEventListener('hidden.bs.modal', markReleaseSeen)
    return () => modalElement.removeEventListener('hidden.bs.modal', markReleaseSeen)
  }, [welcomeStorageKey])

  // Update navigation header based on current mode
  useEffect(() => {
    if (authenticated) {
      switch (mode) {
        case 'dashboard':
          updateNavigation({
            showNavigation: false,
            onNewDataset: null,
            onBackToDashboard: null
          });
          break;
        case 'upload':
          updateNavigation({
            showNavigation: true,
            onNewDataset: null,
            onBackToDashboard: handleBackToDashboard
          });
          break;
        default:
          updateNavigation({
            showNavigation: false,
            onNewDataset: null,
            onBackToDashboard: null
          });
      }
    } else {
      updateNavigation({
        showNavigation: false,
        onNewDataset: null,
        onBackToDashboard: null
      });
    }
  }, [authenticated, mode, updateNavigation, handleBackToDashboard]);

  const handleDatasetCreated = useCallback((datasetId) => {
    router.push(`/dataset/${datasetId}`)
  }, [router])

  return (
    <ProtectedRoute>
      <main>
        {mode === 'dashboard' && (
          <div className="container p-4">
            <DatasetsGrid
              onOpenDataset={handleDatasetSelect}
              onNewDataset={handleNewDataset}
              onShowWelcome={() => showWelcomeModal(true)}
            />
          </div>
        )}

        {mode === 'upload' && (
          <NewDatasetComposer onDatasetCreated={handleDatasetCreated} />
        )}

        <div className="modal modal-lg fade" id="myModal" tabIndex="-1" aria-labelledby="welcomeModalLabel" aria-hidden="true">
          <div className="modal-dialog modal-dialog-centered modal-dialog-scrollable">
            <div className="modal-content">
              <div className="modal-header">
                <div>
                  <span className="badge text-bg-success mb-2">Major update</span>
                  <h5 className="modal-title" id="welcomeModalLabel">
                    ChatIPT now creates Darwin Core Data Packages
                  </h5>
                </div>
                <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div className="modal-body">
                <div className="welcome-update-hero">
                  <p className="lead">
                    ChatIPT helps turn spreadsheets and manuscripts into clean, standardised,
                    publication-ready biodiversity data.
                  </p>
                  <p>
                    With this major update, ChatIPT now organises that data as connected entities
                    and relationships using the new Darwin Core Data Package standard, while
                    continuing to produce a simpler Darwin Core Archive for current GBIF
                    publication workflows.
                  </p>

                  <div className="welcome-package-flow" aria-label="ChatIPT creates two publication packages">
                    <div className="welcome-flow-node">
                      <i className="bi bi-file-earmark-spreadsheet" aria-hidden="true"></i>
                      <span>Your files</span>
                    </div>
                    <i className="bi bi-arrow-right welcome-flow-arrow" aria-hidden="true"></i>
                    <div className="welcome-flow-node welcome-flow-chatipt">
                      <i className="bi bi-stars" aria-hidden="true"></i>
                      <span>ChatIPT organises and links</span>
                    </div>
                    <i className="bi bi-arrow-right welcome-flow-arrow" aria-hidden="true"></i>
                    <div className="welcome-flow-outputs">
                      <div className="welcome-flow-output">
                        <strong>DwC-DP</strong>
                        <small>Complete connected package</small>
                      </div>
                      <div className="welcome-flow-output">
                        <strong>DwC-A</strong>
                        <small>GBIF-compatible projection</small>
                      </div>
                    </div>
                  </div>

                  <p className="small no-bottom-margin">
                    Your DwC-DP is the complete, authoritative output. The DwC-A is derived from
                    it for compatibility; some richer relationships remain only in the DwC-DP.
                  </p>
                </div>

                <div className="alert alert-info" role="alert">
                  <p><strong>Also new: PDF manuscript parsing</strong></p>
                  <p className="no-bottom-margin">
                    Upload a manuscript to extract useful dataset metadata and, where available,
                    tabular darwin core data.
                  </p>
                </div>

                <Accordion className="mb-3">
                  <Accordion.Item eventKey="audience">
                    <Accordion.Header>Who is ChatIPT for?</Accordion.Header>
                    <Accordion.Body>
                      <p>
                        ChatIPT helps students and researchers publish biodiversity datasets to
                        GBIF through a guided browser workflow. It&apos;s best suited for:
                      </p>
                      <ul>
                        <li>Students and researchers new to biodiversity data publication.</li>
                        <li>People who publish spreadsheet datasets only occasionally.</li>
                        <li>Users who want help cleaning data, applying standards, and creating metadata.</li>
                      </ul>
                      <p><strong>Current scope</strong></p>
                      <ul className="mb-0">
                        <li>Best for ad hoc spreadsheet publication workflows.</li>
                        <li>Phylogenetic tree files can be uploaded, but tree handling is currently limited.</li>
                      </ul>
                    </Accordion.Body>
                  </Accordion.Item>
                </Accordion>

                <div className="alert alert-light" role="alert">
                  <p className="no-bottom-margin"><strong>Support:</strong> rukayasj@uio.no</p>
                </div>
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-outline-secondary" data-bs-dismiss="modal">
                  Continue to my datasets
                </button>
                <button
                  type="button"
                  className="btn btn-primary"
                  data-bs-dismiss="modal"
                  onClick={handleNewDataset}
                >
                  <i className="bi bi-plus-circle me-1" aria-hidden="true"></i>
                  Start a new dataset
                </button>
              </div>
            </div>
          </div>
        </div>
      </main>
    </ProtectedRoute>
  )
}

const Home = () => {
  return (
    <DatasetProvider>
      <HomeContent />
    </DatasetProvider>
  )
}

export default Home
