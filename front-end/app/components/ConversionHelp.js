'use client'

import { useEffect, useId, useRef, useState } from 'react'
import Overlay from 'react-bootstrap/Overlay'
import Tooltip from 'react-bootstrap/Tooltip'
import { choiceExamples, CONVERSION_TERMS, helpTermParts } from '../utils/conversionCopy.mjs'

// Definitions open on hover, keyboard focus or tap, and close on Escape or an outside click.
export function ConversionHelpTerm({ term, children }) {
  const id = useId()
  const target = useRef(null)
  const hideTimer = useRef(null)
  const pinned = useRef(false)
  const [open, setOpen] = useState(false)
  const definition = CONVERSION_TERMS[term]
  const show = () => { clearTimeout(hideTimer.current); setOpen(true) }
  const hideAfterHover = () => {
    clearTimeout(hideTimer.current)
    hideTimer.current = setTimeout(() => { if (!pinned.current && document.activeElement !== target.current) setOpen(false) }, 150)
  }
  useEffect(() => () => clearTimeout(hideTimer.current), [])
  useEffect(() => {
    if (!open) return
    const outside = event => {
      if (!target.current?.contains(event.target) && !document.getElementById(id)?.contains(event.target)) {
        pinned.current = false
        setOpen(false)
      }
    }
    const escape = event => { if (event.key === 'Escape') { pinned.current = false; setOpen(false); event.stopPropagation() } }
    document.addEventListener('pointerdown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('pointerdown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [open, id])
  if (!definition) return children
  return <>
    <button ref={target} type="button" className="conversion-help-term"
      aria-describedby={open ? id : undefined} onMouseEnter={show} onMouseLeave={hideAfterHover}
      onFocus={show} onBlur={() => { pinned.current = false; hideAfterHover() }}
      onPointerDown={() => { pinned.current = true; show() }} onClick={() => { pinned.current = true; show() }}>
      {children}<i className="bi bi-question-circle ms-1" aria-hidden="true" />
    </button>
    <Overlay target={() => target.current} show={open} placement="auto">
      {props => <Tooltip id={id} {...props} onMouseEnter={show} onMouseLeave={hideAfterHover}>{definition}</Tooltip>}
    </Overlay>
  </>
}

export function ConversionChoiceTitle({ item, text = item.title }) {
  const parts = helpTermParts(text, item.help)
  if (!parts) return text
  return <>{parts[0]}<ConversionHelpTerm term={item.help}>{parts[1]}</ConversionHelpTerm>{parts[2]}</>
}

export function ConversionExamples({ state, item, open = false }) {
  const examples = choiceExamples(state, item)
  if (!examples.length) return null
  return <details className="conversion-examples small text-body-secondary" open={open}>
    <summary>From your files</summary>
    {examples.map((example, index) => <p key={index} className="mt-1 mb-1">{example}</p>)}
  </details>
}
