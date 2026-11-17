'use client'

import { useRef, useState } from 'react'
import { formatFileSize } from '../utils/uploadSize.mjs'

const extensionOf = name => {
  const dot = String(name || '').lastIndexOf('.')
  return dot === -1 ? '' : name.slice(dot).toLowerCase()
}

// One place to add files, used by both upload screens: drop files on it or choose them.
// `files` is the selected File list; picking more adds to it, and the same file is never added twice.
export default function FileDropZone({ files, onChange, extensions, hint, details, disabled = false, onRejected }) {
  const inputRef = useRef(null)
  const [dragging, setDragging] = useState(false)

  const add = (incoming) => {
    const list = Array.from(incoming || [])
    if (!list.length) return
    const rejected = list.filter(file => !extensions.includes(extensionOf(file.name))).map(file => file.name)
    const accepted = list.filter(file => extensions.includes(extensionOf(file.name)) &&
      !files.some(existing => existing.name === file.name && existing.size === file.size))
    onChange([...files, ...accepted])
    onRejected?.(rejected.length ? `Not a supported file type: ${rejected.join(', ')}.` : null)
    if (inputRef.current) inputRef.current.value = ''
  }

  const onDragOver = (event) => {
    event.preventDefault()
    if (!disabled) setDragging(true)
  }
  const onDrop = (event) => {
    event.preventDefault()
    setDragging(false)
    if (!disabled) add(event.dataTransfer.files)
  }

  return <div className="file-drop">
    <div className={`file-drop-zone${dragging ? ' is-dragging' : ''}${disabled ? ' is-disabled' : ''}`}
      onDragOver={onDragOver} onDragEnter={onDragOver} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false) }} onDrop={onDrop}>
      <i className="bi bi-upload" aria-hidden="true" />
      <span>Drop files here or <button type="button" className="btn btn-link p-0 align-baseline" onClick={() => inputRef.current?.click()} disabled={disabled}>choose files</button></span>
      <input ref={inputRef} type="file" multiple accept={extensions.join(',')} className="d-none" onChange={event => add(event.target.files)} disabled={disabled} />
    </div>
    {files.length > 0 && <ul className="file-drop-list">
      {files.map(file => <li key={`${file.name}:${file.size}`}>
        <i className="bi bi-file-earmark" aria-hidden="true" />
        <span className="file-drop-name">{file.name}</span>
        <span className="text-body-secondary">{formatFileSize(file.size)}</span>
        <button type="button" className="btn btn-sm btn-link text-body-secondary p-0 ms-auto" onClick={() => onChange(files.filter(item => item !== file))}
          disabled={disabled} aria-label={`Remove ${file.name}`}><i className="bi bi-x-lg" aria-hidden="true" /></button>
      </li>)}
    </ul>}
    <div className="form-text">{hint}</div>
    {details && <details className="small mt-1"><summary>What can I upload?</summary><div className="mt-2 text-body-secondary">{details}</div></details>}
  </div>
}
