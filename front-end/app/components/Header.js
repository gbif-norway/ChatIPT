'use client'

import { useAuth } from '../contexts/AuthContext'
import { useTheme } from '../contexts/ThemeContext'
import ThemeToggle from './ThemeToggle'

const Header = () => {
  const { user, logout } = useAuth()
  const { isDark } = useTheme()

  if (!user) {
    return null
  }

  return (
    <nav className={`navbar navbar-expand-lg border-bottom ${isDark ? 'navbar-dark bg-dark' : 'navbar-light bg-light'}`}>
      <div className="container">
        <span className="navbar-brand">ChatIPT</span>
        
        <div className="navbar-nav ms-auto">
          <div className="nav-item me-3 d-flex align-items-center">
            <ThemeToggle />
          </div>
          
          <div className="nav-item dropdown">
            <a 
              className="nav-link dropdown-toggle d-flex align-items-center" 
              href="#" 
              role="button" 
              data-bs-toggle="dropdown" 
              aria-expanded="false"
            >
              <i className="bi bi-person-circle me-2"></i>
              <span className="d-none d-sm-inline">
                {user.first_name && user.last_name 
                  ? `${user.first_name} ${user.last_name}`
                  : user.email
                }
              </span>
            </a>
            <ul className="dropdown-menu dropdown-menu-end">
              <li>
                <div className="dropdown-item-text">
                  <small className="text-muted d-block">Signed in as</small>
                  <strong>{user.email}</strong>
                </div>
              </li>
              {user.orcid_id && (
                <li>
                  <div className="dropdown-item-text">
                    <small className="text-muted d-block">ORCID</small>
                    <a 
                      href={`https://orcid.org/${user.orcid_id}`} 
                      target="_blank" 
                      rel="noopener noreferrer"
                      className="text-decoration-none"
                    >
                      {user.orcid_id}
                    </a>
                  </div>
                </li>
              )}
              {user.institution && (
                <li>
                  <div className="dropdown-item-text">
                    <small className="text-muted d-block">Institution</small>
                    <span>{user.institution}</span>
                  </div>
                </li>
              )}
              <li><hr className="dropdown-divider" /></li>
              <li>
                <button 
                  className="dropdown-item" 
                  onClick={logout}
                >
                  <i className="bi bi-box-arrow-right me-2"></i>
                  Sign out
                </button>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </nav>
  )
}

export default Header 