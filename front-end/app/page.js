'use client'

import DatasetsGrid from './components/DatasetsGrid'
import NewDatasetComposer from './components/NewDatasetComposer'
import { ConversionUpload } from './components/DwcConversion'
import ProtectedRoute from './components/ProtectedRoute'
import { useAuth } from './contexts/AuthContext'
import { DatasetProvider } from './contexts/DatasetContext'
import { useNavigation } from './components/HeaderWrapper'
import { useEffect, useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import WhatsNew from './components/WhatsNew'

const HomeContent = () => {
  const router = useRouter()
  const { authenticated } = useAuth()
  const { updateNavigation } = useNavigation()
  const [mode, setMode] = useState('dashboard') // 'dashboard', 'upload', 'convert'
  const [showWhatsNew, setShowWhatsNew] = useState(false)

  const handleDatasetSelect = useCallback((datasetId) => {
    router.push(`/dataset/${datasetId}`)
  }, [router])

  const handleNewDataset = useCallback(() => {
    setMode('upload')
    router.push('/?mode=upload')
  }, [router])

  const handleConversion = useCallback(() => {
    setMode('convert')
    router.push('/?mode=convert')
  }, [router])

  const handleBackToDashboard = useCallback(() => {
    setMode('dashboard')
    router.push('/')
  }, [router])

  // The URL is read after mount: the server render cannot see it, and useSearchParams would stop prerendering.
  useEffect(() => {
    const requestedMode = new URLSearchParams(window.location.search).get('mode')
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setMode(['upload', 'convert'].includes(requestedMode) ? requestedMode : 'dashboard')
  }, [])

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
        case 'convert':
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
              onConvertArchive={handleConversion}
              onShowWhatsNew={() => setShowWhatsNew(true)}
            />
          </div>
        )}

        {mode === 'upload' && (
          <NewDatasetComposer onDatasetCreated={handleDatasetCreated} />
        )}

        {mode === 'convert' && <ConversionUpload onDatasetCreated={handleDatasetCreated} />}

        <WhatsNew show={showWhatsNew} onHide={() => setShowWhatsNew(false)} />
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
