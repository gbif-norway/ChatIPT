'use client'

import Brand from './Brand'
import { useAuth } from '../contexts/AuthContext'
import { useSearchParams } from 'next/navigation'

const ERROR_MESSAGES = {
  callback_failed: 'We had trouble completing your ORCID sign-in. Please try again.',
  no_code: 'ORCID did not return the authorization code we expected. Please try signing in again.',
  no_orcid_id: 'We could not read your ORCID identifier from the login. Please try again.',
  public_profile_required: 'ChatIPT currently supports only ORCID records with some public information. Please make parts of your ORCID profile public or contact us for help.',
}

const DEFAULT_ERROR_MESSAGE = 'Sign in with ORCID failed. Please try again.'

const Login = () => {
  const { login, loading } = useAuth()
  const searchParams = useSearchParams()
  const errorKey = searchParams.get('error')
  const errorMessage = errorKey ? (ERROR_MESSAGES[errorKey] || DEFAULT_ERROR_MESSAGE) : null

  if (loading) {
    return (
      <div className="container mt-5">
        <div className="row justify-content-center">
          <div className="col-md-6">
            <div className="card">
              <div className="card-body text-center">
                <div className="spinner-border" role="status">
                  <span className="visually-hidden">Loading...</span>
                </div>
                <p className="mt-3">Checking authentication...</p>
              </div>
            </div>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="container login-page">
      <div className="login-layout">
        <section className="login-intro">
          <Brand />
          <h1>Good data.<br />Greater discoveries.</h1>
          <p>Turn your biodiversity data into something the world can build on. We’ll help you clean it, connect it, and prepare it for GBIF.</p>
          <svg className="login-botanical" viewBox="0 0 380 170" fill="none" aria-hidden="true">
            <path d="M15 150h350M95 150V65m95 85V30m95 120V80" stroke="currentColor" strokeWidth="1.5" opacity=".4" />
            <path d="M95 110C49 112 46 81 47 63c30-3 51 15 48 47Zm0-25c-2-36 22-57 51-55 2 30-17 55-51 55Zm95 22c-39 1-62-26-62-56 40 0 64 19 62 56Zm0-40c0-37 21-56 48-57 1 33-16 58-48 57Zm95 64c-37 2-49-21-51-45 31-1 52 16 51 45Zm0-28c-1-31 16-53 45-55 2 34-14 53-45 55Z" fill="currentColor" opacity=".2" />
            <circle cx="348" cy="27" r="10" fill="currentColor" opacity=".2" />
          </svg>
        </section>
        <section className="login-form" aria-labelledby="signin-heading">
          <span className="eyebrow">Let’s get started</span>
          <h2 id="signin-heading">Welcome to ChatIPT</h2>
          <p>A guided workspace for students and researchers. Sign in to start a dataset or pick up where you left off.</p>
          {errorMessage && <div className="alert alert-warning" role="alert"><strong>Sign-in issue:</strong> {errorMessage}</div>}
          <button onClick={login} className="btn btn-primary"><i className="bi bi-person-circle me-2" aria-hidden="true" />Sign in with ORCID<i className="bi bi-arrow-right ms-2" aria-hidden="true" /></button>
          <div className="login-orcid-note">
            <h3 className="h6">One research identity, fewer passwords</h3>
            <p>ORCID is a free identifier for researchers. ChatIPT uses your public ORCID profile to connect your name and institution to your account.</p>
            <a href="https://orcid.org" target="_blank" rel="noopener noreferrer" className="small">Learn about ORCID<i className="bi bi-arrow-up-right ms-1" aria-hidden="true" /></a>
          </div>
        </section>
      </div>
      <p className="login-footnote">From spreadsheets to connected biodiversity data.</p>
    </div>
  )
}

export default Login
