'use client'

import { useRef, useState } from 'react'
import { formatFileSize } from '../utils/uploadSize.mjs'

const TREE_EXTENSIONS = ['.newick', '.nwk', '.nex', '.nexus', '.tre', '.tree']

const extensionOf = name => {
  const dot = String(name || '').lastIndexOf('.')
  return dot === -1 ? '' : name.slice(dot).toLowerCase()
}

const iconFor = (name) => {
  const extension = extensionOf(name)
  if (extension === '.pdf') return 'bi-file-earmark-pdf'
  if (extension === '.zip' || extension === '.dwca') return 'bi-file-earmark-zip'
  if (extension === '.xml') return 'bi-file-earmark-code'
  if (TREE_EXTENSIONS.includes(extension)) return 'bi-diagram-3'
  return 'bi-file-earmark-spreadsheet'
}

const fileKey = file => `${file.name}:${file.size}`

// One place to add files, used by both upload screens. Empty, the whole box is a target to drop on or click.
// Once files are added, the box becomes the list of those files, so it is obvious they are there.
// `files` is the selected File list; adding more keeps the ones already chosen, and the same file is never added twice.
export default function FileDropZone({ files, onChange, extensions, hint, details, limitMB, disabled = false, onRejected }) {
  const inputRef = useRef(null)
  const [dragging, setDragging] = useState(false)
  const [lastAdded, setLastAdded] = useState(() => new Set())
  const totalBytes = files.reduce((sum, file) => sum + file.size, 0)
  const hasFiles = files.length > 0

  const add = (incoming) => {
    const list = Array.from(incoming || [])
    if (!list.length) return
    const rejected = list.filter(file => !extensions.includes(extensionOf(file.name))).map(file => file.name)
    const accepted = list.filter(file => extensions.includes(extensionOf(file.name)) &&
      !files.some(existing => fileKey(existing) === fileKey(file)))
    setLastAdded(new Set(accepted.map(fileKey)))
    onChange([...files, ...accepted])
    onRejected?.(rejected.length ? `Not a supported file type: ${rejected.join(', ')}.` : null)
    if (inputRef.current) inputRef.current.value = ''
  }

  const choose = () => inputRef.current?.click()
  const onDragOver = (event) => {
    event.preventDefault()
    if (!disabled) setDragging(true)
  }
  const dropProps = {
    onDragOver,
    onDragEnter: onDragOver,
    onDragLeave: (event) => { if (!event.currentTarget.contains(event.relatedTarget)) setDragging(false) },
    onDrop: (event) => {
      event.preventDefault()
      setDragging(false)
      if (!disabled) add(event.dataTransfer.files)
    },
  }
  const stateClass = `${dragging ? ' is-dragging' : ''}${disabled ? ' is-disabled' : ''}`

  return <div className="file-drop">
    <input ref={inputRef} type="file" multiple accept={extensions.join(',')} className="d-none" onChange={event => add(event.target.files)} disabled={disabled} />
    {hasFiles ? (
      <div className={`file-drop-zone has-files${stateClass}`} {...dropProps}>
        <ul className="file-drop-list">
          {files.map(file => <li key={fileKey(file)} className={lastAdded.has(fileKey(file)) ? 'is-new' : undefined}>
            <i className={`bi ${iconFor(file.name)} file-drop-icon`} aria-hidden="true" />
            <span className="file-drop-name" title={file.name}>{file.name}</span>
            <span className="file-drop-size">{formatFileSize(file.size)}</span>
            <button type="button" className="btn btn-sm file-drop-remove" onClick={() => onChange(files.filter(item => item !== file))}
              disabled={disabled} aria-label={`Remove ${file.name}`} title="Remove"><i className="bi bi-x-lg" aria-hidden="true" /></button>
          </li>)}
        </ul>
        <div className="file-drop-footer">
          <button type="button" className="btn btn-link btn-sm p-0" onClick={choose} disabled={disabled}>
            <i className="bi bi-plus-lg me-1" aria-hidden="true" />Add more files
          </button>
          <span>
            {files.length} {files.length === 1 ? 'file' : 'files'} · {formatFileSize(totalBytes)}{limitMB ? ` of ${limitMB} MB` : ''}
          </span>
        </div>
      </div>
    ) : (
      <button type="button" className={`file-drop-zone is-empty${stateClass}`} onClick={choose} disabled={disabled} {...dropProps}>
        <i className="bi bi-upload file-drop-empty-icon" aria-hidden="true" />
        <span className="file-drop-empty-title">Drop files here, or click to choose</span>
        {hint && <span className="file-drop-empty-hint">{hint}</span>}
      </button>
    )}
    <span className="visually-hidden" role="status" aria-live="polite">
      {hasFiles ? `${files.length} ${files.length === 1 ? 'file' : 'files'} ready to upload.` : ''}
    </span>
    {details && !hasFiles && <details className="small mt-2"><summary>What can I upload?</summary><div className="mt-2 text-body-secondary">{details}</div></details>}
  </div>
}
