'use client'

import { useAuth } from '../contexts/AuthContext'
import Login from './Login'

const ProtectedRoute = ({ children }) => {
  const { authenticated, loading } = useAuth()

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

  if (!authenticated) {
    return <Login />
  }

  return children
}

export default ProtectedRoute 