'use client'

import { useAuth } from '../contexts/AuthContext'

const Login = () => {
  const { login, loading } = useAuth()

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
    <div className="container mt-5">
      <div className="row justify-content-center">
        <div className="col-md-8 col-lg-6">
          <div className="card shadow">
            <div className="card-body p-5">
              <div className="text-center mb-4">
                <h2 className="card-title">Welcome to ChatIPT</h2>
                <p className="text-muted">
                  A chatbot for students and researchers who are new to data publication
                </p>
              </div>

              <div className="alert alert-info" role="alert">
                <h5 className="alert-heading">Why ORCID Login?</h5>
                <p className="mb-0">
                  ChatIPT requires ORCID authentication to ensure data quality and provide 
                  personalized assistance. Your ORCID account helps us understand your 
                  research background and institution.
                </p>
              </div>

              <div className="d-grid gap-3">
                <button 
                  onClick={login}
                  className="btn btn-primary btn-lg"
                  style={{ backgroundColor: '#A6CE39', borderColor: '#A6CE39' }}
                >
                  <i className="bi bi-person-circle me-2"></i>
                  Sign in with ORCID
                </button>
              </div>

              <div className="mt-4">
                <small className="text-muted">
                  <strong>What is ORCID?</strong> ORCID provides a persistent digital identifier 
                  that distinguishes you from every other researcher. It's free to register at{' '}
                  <a href="https://orcid.org" target="_blank" rel="noopener noreferrer">
                    orcid.org
                  </a>
                </small>
              </div>

              <hr className="my-4" />

              <div className="row text-center">
                <div className="col-md-4">
                  <i className="bi bi-shield-check text-primary fs-1"></i>
                  <h6 className="mt-2">Secure</h6>
                  <small className="text-muted">Your data is protected</small>
                </div>
                <div className="col-md-4">
                  <i className="bi bi-person-check text-primary fs-1"></i>
                  <h6 className="mt-2">Verified</h6>
                  <small className="text-muted">Academic identity verified</small>
                </div>
                <div className="col-md-4">
                  <i className="bi bi-globe text-primary fs-1"></i>
                  <h6 className="mt-2">Global</h6>
                  <small className="text-muted">Used by researchers worldwide</small>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

export default Login 