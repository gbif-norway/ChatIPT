'use client'

import { useEffect, useState } from 'react'

import config from '../config'
import { getCsrfToken } from '../utils/csrf'

/**
 * "Notify me by email" while ChatIPT works: one email the next time it needs input, hits a problem or
 * finishes the package. Renders the button (only while isWorking) and its dialog.
 */
export default function EmailNotification({ datasetId, isWorking }) {
  const [showEmailNotificationModal, setShowEmailNotificationModal] = useState(false)
  const [emailNotification, setEmailNotification] = useState({ status: 'idle' })
  const [notificationEmail, setNotificationEmail] = useState('')
  const [emailNotificationError, setEmailNotificationError] = useState('')
  const [isSavingEmailNotification, setIsSavingEmailNotification] = useState(false)
  const emailNotificationArmed = Boolean(emailNotification.enabled)

  useEffect(() => {
    if (!datasetId) return

    let cancelled = false
    const loadEmailNotification = async () => {
      try {
        const response = await fetch(
          `${config.baseUrl}/api/datasets/${datasetId}/attention-notification/`,
          { credentials: 'include' },
        )
        if (!response.ok) throw new Error('Unable to load email notification settings.')
        const data = await response.json()
        if (cancelled) return
        setEmailNotification(data)
        setNotificationEmail(data.email || data.suggested_email || '')
      } catch (error) {
        if (!cancelled) setEmailNotificationError(error.message)
      }
    }
    loadEmailNotification()
    return () => {
      cancelled = true
    }
  }, [datasetId])

  useEffect(() => {
    if (!showEmailNotificationModal) return

    const closeOnEscape = (event) => {
      if (event.key === 'Escape' && !isSavingEmailNotification) {
        setShowEmailNotificationModal(false)
      }
    }
    document.addEventListener('keydown', closeOnEscape)
    document.body.classList.add('modal-open')
    return () => {
      document.removeEventListener('keydown', closeOnEscape)
      document.body.classList.remove('modal-open')
    }
  }, [isSavingEmailNotification, showEmailNotificationModal])

  const openEmailNotificationModal = () => {
    setEmailNotificationError('')
    setShowEmailNotificationModal(true)
  }

  const closeEmailNotificationModal = () => {
    if (!isSavingEmailNotification) setShowEmailNotificationModal(false)
  }

  const saveEmailNotification = async (event) => {
    event.preventDefault()
    if (!datasetId || !notificationEmail.trim()) return

    setIsSavingEmailNotification(true)
    setEmailNotificationError('')
    try {
      const csrfToken = await getCsrfToken()
      const headers = { 'Content-Type': 'application/json' }
      if (csrfToken) headers['X-CSRFToken'] = csrfToken
      const response = await fetch(
        `${config.baseUrl}/api/datasets/${datasetId}/attention-notification/`,
        {
          method: 'POST',
          credentials: 'include',
          headers,
          body: JSON.stringify({ email: notificationEmail.trim() }),
        },
      )
      const data = await response.json()
      if (!response.ok) {
        const message = data.email?.[0] || data.detail || 'Unable to enable email notifications.'
        throw new Error(message)
      }
      setEmailNotification(data)
      setNotificationEmail(data.email)
    } catch (error) {
      setEmailNotificationError(error.message)
    } finally {
      setIsSavingEmailNotification(false)
    }
  }

  const cancelEmailNotification = async () => {
    if (!datasetId) return

    setIsSavingEmailNotification(true)
    setEmailNotificationError('')
    try {
      const csrfToken = await getCsrfToken()
      const headers = {}
      if (csrfToken) headers['X-CSRFToken'] = csrfToken
      const response = await fetch(
        `${config.baseUrl}/api/datasets/${datasetId}/attention-notification/`,
        { method: 'DELETE', credentials: 'include', headers },
      )
      if (!response.ok) throw new Error('Unable to cancel the email notification.')
      setEmailNotification(await response.json())
    } catch (error) {
      setEmailNotificationError(error.message)
    } finally {
      setIsSavingEmailNotification(false)
    }
  }

  const unavailable = emailNotification.delivery_available === false
  const notificationPrompt = isWorking ? (
    <button
      type="button"
      className={`btn btn-sm ${emailNotificationArmed ? 'btn-outline-success' : 'btn-outline-secondary'}`}
      onClick={openEmailNotificationModal}
      disabled={unavailable}
      title={unavailable
        ? 'Email notifications are unavailable on this server.'
        : 'Get one email when ChatIPT needs your input, hits a problem, or your package is ready.'}
    >
      <i className={`bi ${emailNotificationArmed ? 'bi-envelope-check-fill' : 'bi-envelope'} me-1`} aria-hidden="true"></i>
      {emailNotificationArmed ? 'Email notification set' : 'Notify me by email'}
    </button>
  ) : null

  const emailNotificationModal = showEmailNotificationModal ? (
    <>
      <div
        className="modal fade show d-block"
        role="dialog"
        tabIndex="-1"
        aria-modal="true"
        aria-labelledby={`email-notification-title-${datasetId}`}
        onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeEmailNotificationModal()
        }}
      >
        <div className="modal-dialog modal-dialog-centered modal-sm">
          <div className="modal-content border-0 shadow-lg">
            <div className="modal-header border-0 pb-0 align-items-start">
              <div className="d-flex gap-3">
                <span className="bg-warning-subtle text-warning-emphasis rounded-circle d-inline-flex align-items-center justify-content-center flex-shrink-0" style={{ width: 40, height: 40 }}>
                  <i className="bi bi-envelope-paper" aria-hidden="true"></i>
                </span>
                <div>
                  <h2 className="modal-title fs-5" id={`email-notification-title-${datasetId}`}>
                    Notify me by email
                  </h2>
                  <p className="small text-muted mb-0 mt-1">
                    Step away while ChatIPT keeps working.
                  </p>
                </div>
              </div>
              <button
                type="button"
                className="btn-close"
                aria-label="Close"
                onClick={closeEmailNotificationModal}
                disabled={isSavingEmailNotification}
              ></button>
            </div>

            <div className="modal-body pt-3">
              <p className="small mb-3">
                We’ll send one email the next time ChatIPT needs your input, hits a problem, or your package is ready. Once your upload or reply has been saved, you can close this tab.
              </p>

              {emailNotificationArmed ? (
                <div className="text-center py-2">
                  <span className="d-inline-flex align-items-center justify-content-center rounded-circle bg-success-subtle text-success mb-3" style={{ width: 48, height: 48 }}>
                    <i className="bi bi-check-lg fs-4" aria-hidden="true"></i>
                  </span>
                  <h3 className="h6 mb-1">Email notification set</h3>
                  <p className="small text-muted mb-3 text-break">{emailNotification.email}</p>
                  <button
                    type="button"
                    className="btn btn-sm btn-outline-danger"
                    onClick={cancelEmailNotification}
                    disabled={isSavingEmailNotification}
                  >
                    {isSavingEmailNotification ? 'Cancelling…' : 'Cancel notification'}
                  </button>
                </div>
              ) : (
                <form onSubmit={saveEmailNotification}>
                  <label className="form-label small fw-semibold" htmlFor={`notification-email-${datasetId}`}>
                    Email address
                  </label>
                  <input
                    id={`notification-email-${datasetId}`}
                    className="form-control"
                    type="email"
                    value={notificationEmail}
                    onChange={(event) => setNotificationEmail(event.target.value)}
                    placeholder="you@example.org"
                    autoComplete="email"
                    autoFocus
                    required
                    disabled={isSavingEmailNotification}
                  />
                  <button
                    type="submit"
                    className="btn btn-warning w-100 mt-3"
                    disabled={isSavingEmailNotification || !notificationEmail.trim()}
                  >
                    <i className="bi bi-envelope-check me-2" aria-hidden="true"></i>
                    {isSavingEmailNotification ? 'Saving…' : 'Notify me'}
                  </button>
                </form>
              )}

              {emailNotificationError && (
                <p className="small text-danger mb-0 mt-2" role="alert">{emailNotificationError}</p>
              )}

              <div className="bg-body-tertiary rounded p-2 mt-3 d-flex gap-2">
                <i className="bi bi-info-circle text-muted flex-shrink-0" aria-hidden="true"></i>
                <p className="small text-muted mb-0">
                  First notification? Check your Spam folder and mark ChatIPT as “Not spam” so future messages reach your inbox.
                </p>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div className="modal-backdrop fade show"></div>
    </>
  ) : null
  if (!datasetId) return null
  return (
    <>
      {notificationPrompt}
      {emailNotificationModal}
    </>
  )
}
