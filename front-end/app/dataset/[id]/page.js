'use client'

import Dataset from '../../components/Dataset'
import ProtectedRoute from '../../components/ProtectedRoute'
import { DatasetProvider, useDataset } from '../../contexts/DatasetContext'
import { useNavigation } from '../../components/HeaderWrapper'
import { useCallback, useEffect } from 'react'
import { useParams, useRouter } from 'next/navigation'

const DatasetRouteContent = () => {
  const router = useRouter()
  const params = useParams()
  const { loadDataset, setCurrentDatasetId } = useDataset()
  const { updateNavigation } = useNavigation()

  const routeDatasetId = Number(params?.id)
  const isValidDatasetId = Number.isInteger(routeDatasetId) && routeDatasetId > 0

  const handleBackToDashboard = useCallback(() => {
    setCurrentDatasetId(null)
    router.push('/')
  }, [router, setCurrentDatasetId])

  const handleNewDataset = useCallback(() => {
    router.push('/?mode=upload')
  }, [router])

  useEffect(() => {
    if (!isValidDatasetId) {
      return
    }
    loadDataset(routeDatasetId)
  }, [isValidDatasetId, routeDatasetId, loadDataset])

  useEffect(() => {
    updateNavigation({
      showNavigation: true,
      onNewDataset: null,
      onBackToDashboard: handleBackToDashboard,
    })

    return () => {
      updateNavigation({
        showNavigation: false,
        onNewDataset: null,
        onBackToDashboard: null,
      })
    }
  }, [updateNavigation, handleBackToDashboard])

  if (!isValidDatasetId) {
    return (
      <ProtectedRoute>
        <main>
          <div className="container p-4">
            <div className="alert alert-danger">Invalid dataset URL.</div>
          </div>
        </main>
      </ProtectedRoute>
    )
  }

  return (
    <ProtectedRoute>
      <main>
        <Dataset
          onNewDataset={handleNewDataset}
          onBackToDashboard={handleBackToDashboard}
        />
      </main>
    </ProtectedRoute>
  )
}

const DatasetRoutePage = () => {
  return (
    <DatasetProvider>
      <DatasetRouteContent />
    </DatasetProvider>
  )
}

export default DatasetRoutePage
