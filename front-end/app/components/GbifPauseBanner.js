'use client'

import { useGbifStatus } from '../contexts/GbifStatusContext'

const GbifPauseBanner = () => {
  const { paused, message } = useGbifStatus()
  if (!paused) return null
  return (
    <div className="alert alert-warning rounded-0 border-start-0 border-end-0 mb-0" role="status">
      <div className="container d-flex align-items-start gap-2">
        <i className="bi bi-pause-circle-fill mt-1" aria-hidden="true"></i>
        <span>{message}</span>
      </div>
    </div>
  )
}

export default GbifPauseBanner
