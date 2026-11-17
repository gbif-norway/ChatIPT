'use client'

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { pluralize } from '../utils/datasetPresentation'
import { groupTables, sameId } from '../utils/explorerState.mjs'

/**
 * Compact table picker: a button naming the open table that unfolds a list grouped into
 * "Published tables" (DwC-DP) and "Your source files". Works inside a Bootstrap modal:
 * Escape closes only the list.
 *
 * tables: [{id, title, row_count, is_dwc_dp}]
 */
export default function TableList({ tables, selectedId, onSelect, label = 'Table' }) {
  const rootRef = useRef(null)
  const buttonRef = useRef(null)
  const panelRef = useRef(null)
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const groups = useMemo(() => groupTables(tables), [tables])
  const ordered = useMemo(() => [...groups.published, ...groups.sources], [groups])
  const selected = ordered.find((table) => sameId(table.id, selectedId)) || null
  const position = selected ? ordered.indexOf(selected) + 1 : 0

  const close = useCallback((refocus = true) => {
    setOpen(false)
    if (refocus) window.requestAnimationFrame(() => buttonRef.current?.focus())
  }, [])

  const items = () => [...(panelRef.current?.querySelectorAll('.te-picker-item') || [])]

  useEffect(() => {
    if (!open) return undefined
    const current = items().find((item) => item.getAttribute('aria-current') === 'true') || items()[0]
    current?.focus()
    current?.scrollIntoView?.({ block: 'nearest' })
    const onPointerDown = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [open])

  // Native listener so Escape is stopped before it reaches an enclosing Bootstrap modal.
  useEffect(() => {
    const root = rootRef.current
    if (!root) return undefined
    const onKeyDown = (event) => {
      if (event.key !== 'Escape' || !panelRef.current) return
      event.preventDefault()
      event.stopPropagation()
      close()
    }
    root.addEventListener('keydown', onKeyDown)
    return () => root.removeEventListener('keydown', onKeyDown)
  }, [close])

  const onPanelKeyDown = (event) => {
    const list = items()
    const index = list.indexOf(document.activeElement)
    let next = null
    if (event.key === 'ArrowDown') next = list[Math.min(index + 1, list.length - 1)]
    else if (event.key === 'ArrowUp') next = list[Math.max(index - 1, 0)]
    else if (event.key === 'Home') next = list[0]
    else if (event.key === 'End') next = list[list.length - 1]
    else if (event.key === 'Tab') setOpen(false)
    if (next) {
      event.preventDefault()
      next.focus()
    }
  }

  const choose = (table) => {
    onSelect(table.id)
    close()
  }

  const renderGroup = (heading, list) => list.length > 0 && (
    <div className="te-picker-group" role="group" aria-label={heading}>
      <div className="te-picker-heading" aria-hidden="true">{heading}</div>
      <ul>
        {list.map((table) => {
          const active = sameId(table.id, selectedId)
          return (
            <li key={table.id}>
              <button
                type="button"
                className={`te-picker-item${active ? ' is-active' : ''}`}
                aria-current={active ? 'true' : undefined}
                onClick={() => choose(table)}
              >
                <span className="te-picker-title">{table.title}</span>
                <span className="te-picker-count">{pluralize(Number(table.row_count) || 0, 'row')}</span>
                {active && <i className="bi bi-check2 te-picker-check" aria-hidden="true"></i>}
              </button>
            </li>
          )
        })}
      </ul>
    </div>
  )

  return (
    <div className="te-picker" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="te-picker-toggle"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' && !open) {
            event.preventDefault()
            setOpen(true)
          }
        }}
      >
        <span className="te-picker-label">
          {label} {position} of {ordered.length}
          {selected && <span className="visually-hidden">: </span>}
        </span>
        <span className="te-picker-current">
          {selected ? selected.title : 'Choose a table'}
        </span>
        <i className={`bi ${open ? 'bi-chevron-up' : 'bi-chevron-down'} te-picker-chevron`} aria-hidden="true"></i>
        <span className="visually-hidden">, choose another table</span>
      </button>
      {open && (
        <div
          ref={panelRef}
          id={panelId}
          className="te-picker-panel"
          onKeyDown={onPanelKeyDown}
        >
          {renderGroup('Published tables', groups.published)}
          {renderGroup('Your source files', groups.sources)}
        </div>
      )}
    </div>
  )
}
