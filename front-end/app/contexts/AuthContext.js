'use client'

import { createContext, useContext, useState, useEffect } from 'react'
import config from '../config'
import { clearCsrfToken } from '../utils/csrf'

const AuthContext = createContext()

export const useAuth = () => {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return context
}

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null)
  const [authenticated, setAuthenticated] = useState(false)
  const [loading, setLoading] = useState(true)

  const checkAuthStatus = async () => {
    try {
      const response = await fetch(`${config.baseUrl}/api/auth/status/`, {
        credentials: 'include'
      })
      
      if (response.ok) {
        const data = await response.json()
        if (data.authenticated) {
          setUser(data.user)
          setAuthenticated(true)
        } else {
          setUser(null)
          setAuthenticated(false)
        }
      } else {
        setUser(null)
        setAuthenticated(false)
      }
    } catch (error) {
      console.error('Error checking auth status:', error)
      setUser(null)
      setAuthenticated(false)
    } finally {
      setLoading(false)
    }
  }

  const login = () => {
    // Redirect to our custom OAuth2 endpoint which will redirect to ORCID
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- the API host, not a Next.js page
    window.location.href = `${config.baseUrl}/api/auth/orcid/login/`
  }

  const logout = async () => {
    try {
      await fetch(`${config.baseUrl}/accounts/logout/`, {
        method: 'GET',
        credentials: 'include'
      })
    } catch (error) {
      console.error('Error during logout:', error)
    } finally {
      setUser(null)
      setAuthenticated(false)
      clearCsrfToken() // Clear CSRF token on logout
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- checkAuthStatus() sets state only after its request answers
    checkAuthStatus()
  }, [])

  const value = {
    user,
    authenticated,
    loading,
    login,
    logout,
    checkAuthStatus
  }

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  )
} 