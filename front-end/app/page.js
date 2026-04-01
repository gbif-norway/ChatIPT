'use client'

import styles from './app.css'
import Dataset from './components/Dataset'
import DatasetsGrid from './components/DatasetsGrid'
import NewDatasetComposer from './components/NewDatasetComposer'
import ProtectedRoute from './components/ProtectedRoute'
import { useAuth } from './contexts/AuthContext'
import { DatasetProvider, useDataset } from './contexts/DatasetContext'
import { useNavigation } from './components/HeaderWrapper'
import { useEffect, useState, useCallback } from 'react'

const HomeContent = () => {
  const { authenticated } = useAuth()
  const { currentDatasetId, loadDataset, setCurrentDatasetId } = useDataset()
  const { updateNavigation } = useNavigation()
  const [mode, setMode] = useState('dashboard') // 'dashboard', 'upload', 'dataset'

  const handleDatasetSelect = useCallback((datasetId) => {
    loadDataset(datasetId)
    setMode('dataset')
  }, [loadDataset])

  const handleNewDataset = useCallback(() => {
    setMode('upload')
  }, [])

  const handleBackToDashboard = useCallback(() => {
    setMode('dashboard')
    setCurrentDatasetId(null)
  }, [setCurrentDatasetId])

  useEffect(() => {
    // Only show welcome modal if user is authenticated
    if (authenticated) {
      // Dynamically import Bootstrap JavaScript to ensure it's available
      import('bootstrap/dist/js/bootstrap.bundle.min.js').then((bootstrap) => {
        const myModal = new bootstrap.Modal(document.getElementById('myModal'));

        // Show the modal on page load
        myModal.show();

        // Clean up modal and backdrop when it is hidden
        const modalElement = document.getElementById('myModal');
        modalElement.addEventListener('hidden.bs.modal', () => {
          myModal.dispose();
          const backdrops = document.querySelectorAll('.modal-backdrop');
          backdrops.forEach((backdrop) => backdrop.remove());
        });
      });
    }
  }, [authenticated]);

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
        case 'dataset':
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
    loadDataset(datasetId)
    setMode('dataset')
  }, [loadDataset])

  return (
    <ProtectedRoute>
      <main>
        {mode === 'dashboard' && (
          <div className="container p-4">
            <DatasetsGrid 
              onOpenDataset={handleDatasetSelect}
              onNewDataset={handleNewDataset}
            />
          </div>
        )}
        
        {mode === 'upload' && (
          <NewDatasetComposer onDatasetCreated={handleDatasetCreated} />
        )}
        
        {mode === 'dataset' && currentDatasetId && (
          <Dataset 
            onNewDataset={handleNewDataset}
            onBackToDashboard={handleBackToDashboard}
          />
        )}

        <div className="modal modal-lg fade" id="myModal" tabIndex="-1" aria-labelledby="exampleModalLabel" aria-hidden="true">
          <div className="modal-dialog">
            <div className="modal-content">
              <div className="modal-header">
                <h5 className="modal-title" id="exampleModalLabel">Welcome to ChatIPT</h5>
                <button type="button" className="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
              </div>
              <div className="modal-body">
                <div className="alert alert-warning" role="alert">
                  <p>ChatIPT helps students and researchers publish biodiversity datasets to GBIF.</p>
                  <p className="no-bottom-margin">Upload data files, then use the chat to clean and standardize data, create metadata, and publish as a Darwin Core Archive.</p>
                </div>
                <hr />
                <p><strong>Who this is for</strong></p>
                <ul>
                  <li>Students and researchers new to biodiversity data publication.</li>
                  <li>People who publish spreadsheet datasets only occasionally.</li>
                  <li>Users who want a guided workflow in a browser.</li>
                </ul>
                <p><strong>Current scope</strong></p>
                <ul>
                  <li>Best for ad hoc spreadsheet publication workflows.</li>
                  <li>Not intended for direct publication from operational databases.</li>
                  <li>Tree files can be uploaded, but tree handling is currently limited.</li>
                </ul>
                <div className="alert alert-light" role="alert">
                  <p className="no-bottom-margin"><strong>Support:</strong> rukayasj@uio.no</p>
                </div>
              </div>
              <div className="modal-footer">
                <button type="button" className="btn btn-secondary" data-bs-dismiss="modal">Close</button>
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
