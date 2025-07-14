import config from '../config'

let csrfToken = null

export const getCsrfToken = async () => {
  if (csrfToken) {
    return csrfToken
  }
  
  try {
    const response = await fetch(`${config.baseUrl}/api/auth/csrf-token/`, {
      credentials: 'include'
    })
    
    if (response.ok) {
      const data = await response.json()
      csrfToken = data.csrfToken
      return csrfToken
    }
  } catch (error) {
    console.error('Error fetching CSRF token:', error)
  }
  
  return null
}

export const clearCsrfToken = () => {
  csrfToken = null
} 