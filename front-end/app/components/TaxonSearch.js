'use client'

import { useEffect, useState } from 'react'
import config from '../config.js'

// A Catalogue of Life name search for both name reviews; onPick receives the COL usage id.
export default function TaxonSearch({ onPick, disabled }) {
  const [text, setText] = useState('')
  const [results, setResults] = useState([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState(null)

  // Under three letters there is no search, and no results are left over from the last one.
  if (text.trim().length < 3 && results.length > 0) setResults([])

  useEffect(() => {
    const query = text.trim()
    if (query.length < 3) return undefined
    const controller = new AbortController()
    const timer = setTimeout(async () => {
      setSearching(true)
      setError(null)
      try {
        const response = await fetch(
          `${config.baseUrl}/api/taxon-matches/search/?q=${encodeURIComponent(query)}`,
          { credentials: 'include', signal: controller.signal },
        )
        const data = await response.json()
        if (!response.ok) throw new Error(data?.detail || 'Search failed.')
        setResults(data.results || [])
      } catch (err) {
        if (err.name !== 'AbortError') setError(err.message)
      } finally {
        setSearching(false)
      }
    }, 300)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [text])

  return (
    <div>
      <input
        type="search"
        className="form-control form-control-sm"
        placeholder="Search Catalogue of Life (at least 3 letters)"
        value={text}
        onChange={(event) => setText(event.target.value)}
        disabled={disabled}
        aria-label="Search Catalogue of Life"
        autoFocus
      />
      {searching && <div className="small text-muted mt-1">Searching…</div>}
      {error && <div className="small text-danger mt-1">{error}</div>}
      {results.length > 0 && (
        <div className="list-group list-group-flush taxon-search-results mt-1">
          {results.map((result) => (
            <button
              key={result.id}
              type="button"
              className="list-group-item list-group-item-action small py-1"
              onClick={() => onPick(result.id)}
              disabled={disabled}
            >
              {result.suggestion || result.label}
              {result.status && result.status !== 'accepted' && (
                <span className="text-muted"> · {result.status}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
