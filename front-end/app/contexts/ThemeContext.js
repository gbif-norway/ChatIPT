'use client'

import { createContext, useContext, useEffect, useState } from 'react'

const ThemeContext = createContext()

export const useTheme = () => {
  const context = useContext(ThemeContext)
  if (!context) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return context
}

export const ThemeProvider = ({ children }) => {
  const [theme, setTheme] = useState('auto')
  const [resolvedTheme, setResolvedTheme] = useState('dark')
  const [isInitialized, setIsInitialized] = useState(false)

  // Get initial theme from localStorage and set up system theme detection
  useEffect(() => {
    const savedTheme = localStorage.getItem('chatipt-theme')
    if (savedTheme) {
      setTheme(savedTheme)
    }
    
    // Initialize resolved theme based on current theme setting
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    if (savedTheme === 'auto') {
      setResolvedTheme(mediaQuery.matches ? 'dark' : 'light')
    } else if (savedTheme) {
      setResolvedTheme(savedTheme)
    } else {
      // Default to auto mode
      setResolvedTheme(mediaQuery.matches ? 'dark' : 'light')
    }
    
    setIsInitialized(true)
  }, [])

  // Handle system theme changes
  useEffect(() => {
    if (!isInitialized) return
    
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    
    const handleSystemThemeChange = (e) => {
      if (theme === 'auto') {
        setResolvedTheme(e.matches ? 'dark' : 'light')
      }
    }

    // Set initial resolved theme
    if (theme === 'auto') {
      setResolvedTheme(mediaQuery.matches ? 'dark' : 'light')
    } else {
      setResolvedTheme(theme)
    }

    // Listen for system theme changes
    mediaQuery.addEventListener('change', handleSystemThemeChange)

    return () => {
      mediaQuery.removeEventListener('change', handleSystemThemeChange)
    }
  }, [theme, isInitialized])

  // Update document theme when resolved theme changes
  useEffect(() => {
    const html = document.documentElement
    const body = document.body

    if (resolvedTheme === 'dark') {
      html.setAttribute('data-bs-theme', 'dark')
      html.classList.add('dark')
      html.classList.remove('light')
    } else {
      html.setAttribute('data-bs-theme', 'light')
      html.classList.add('light')
      html.classList.remove('dark')
    }
  }, [resolvedTheme])

  const changeTheme = (newTheme) => {
    setTheme(newTheme)
    localStorage.setItem('chatipt-theme', newTheme)
  }

  const value = {
    theme,
    resolvedTheme,
    changeTheme,
    isDark: resolvedTheme === 'dark',
    isLight: resolvedTheme === 'light',
    isAuto: theme === 'auto'
  }

  return (
    <ThemeContext.Provider value={value}>
      {children}
    </ThemeContext.Provider>
  )
} 