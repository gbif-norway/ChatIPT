'use client'

import { createContext, useContext, useEffect, useState } from 'react'
import config from '../config.js'

const POLL_MS = 60 * 1000
const NOT_PAUSED = { paused: false }

const GbifStatusContext = createContext(NOT_PAUSED)

export const useGbifStatus = () => useContext(GbifStatusContext)

// Whether ChatIPT is paused because part of GBIF's API is down (api/gbif-status/).
export const GbifStatusProvider = ({ children }) => {
  const [status, setStatus] = useState(NOT_PAUSED)

  useEffect(() => {
    let cancelled = false
    const check = async () => {
      try {
        const response = await fetch(`${config.baseUrl}/api/gbif-status/`, { credentials: 'include' })
        if (!response.ok) return
        const data = await response.json()
        if (!cancelled) setStatus(data?.paused ? data : NOT_PAUSED)
      } catch (_) {
        // Keep the last known status; a failed check says nothing about GBIF.
      }
    }
    check()
    const timer = setInterval(check, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [])

  return <GbifStatusContext.Provider value={status}>{children}</GbifStatusContext.Provider>
}
