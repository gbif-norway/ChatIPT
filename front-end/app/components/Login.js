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
    <main className="login-page">
      <Brand />
      <h1>Publish biodiversity data to GBIF</h1>
      <p className="login-lead">ChatIPT is for students and researchers who publish data only now and then. You upload your spreadsheets and a chatbot works through them with you.</p>
      <ol className="login-steps">
        <li>Upload one or more data files.</li>
        <li>Clean and standardise the data by answering questions in a chat.</li>
        <li>Fill in the dataset description.</li>
        <li>Get a Darwin Core package that is ready for GBIF.</li>
      </ol>
      {errorMessage && <div className="alert alert-warning" role="alert"><strong>Sign-in issue:</strong> {errorMessage}</div>}
      <button onClick={login} className="btn btn-primary login-button">
        <svg className="login-orcid-mark" viewBox="0 0 256 256" aria-hidden="true">
          <path fill="#A6CE39" d="M256 128c0 70.7-57.3 128-128 128S0 198.7 0 128 57.3 0 128 0s128 57.3 128 128z" />
          <path fill="#fff" d="M86.3 186.2H70.9V79.1h15.4v107.1zM108.9 79.1h41.6c39.6 0 57 28.3 57 53.6 0 27.5-21.5 53.6-56.8 53.6h-41.8V79.1zm15.4 93.3h24.5c34.9 0 42.9-26.5 42.9-39.7 0-21.5-13.7-39.7-43.7-39.7h-23.7v79.4zM88.7 56.8c0 5.5-4.5 10.1-10.1 10.1s-10.1-4.6-10.1-10.1c0-5.6 4.5-10.1 10.1-10.1s10.1 4.6 10.1 10.1z" />
        </svg>
        Sign in with ORCID
      </button>
      <p className="login-orcid-note">ORCID is a free identifier for researchers. ChatIPT reads your public ORCID record to connect your name and institution to your account. <a href="https://orcid.org" target="_blank" rel="noopener noreferrer">About ORCID</a></p>
    </main>
  )
}

export default Login
