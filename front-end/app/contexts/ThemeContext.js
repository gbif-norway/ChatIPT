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
    console.log('ThemeContext: Saved theme from localStorage:', savedTheme)
    
    if (savedTheme) {
      setTheme(savedTheme)
    }
    
    // Force a fresh media query detection
    const getSystemTheme = () => {
      try {
        // Create a fresh media query instance
        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
        console.log('ThemeContext: Fresh media query - prefers dark mode:', mediaQuery.matches)
        
        // Also check for light mode explicitly
        const lightMediaQuery = window.matchMedia('(prefers-color-scheme: light)')
        console.log('ThemeContext: Fresh media query - prefers light mode:', lightMediaQuery.matches)
        
        // If both are false, default to light mode
        if (!mediaQuery.matches && !lightMediaQuery.matches) {
          console.log('ThemeContext: Neither dark nor light detected, defaulting to light')
          return 'light'
        }
        
        return mediaQuery.matches ? 'dark' : 'light'
      } catch (error) {
        console.error('ThemeContext: Error detecting system theme:', error)
        return 'light' // Default to light mode on error
      }
    }
    
    let initialResolvedTheme = 'light' // default to light mode
    
    if (savedTheme === 'auto' || !savedTheme) {
      initialResolvedTheme = getSystemTheme()
      console.log('ThemeContext: Auto mode, resolved to:', initialResolvedTheme)
    } else if (savedTheme) {
      initialResolvedTheme = savedTheme
      console.log('ThemeContext: Manual mode, resolved to:', initialResolvedTheme)
    }
    
    setResolvedTheme(initialResolvedTheme)
    setIsInitialized(true)
  }, [])

  // Handle system theme changes
  useEffect(() => {
    if (!isInitialized) return
    
    console.log('ThemeContext: Setting up system theme listener, current theme:', theme)
    
    const getSystemTheme = () => {
      try {
        const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
        const lightMediaQuery = window.matchMedia('(prefers-color-scheme: light)')
        
        if (!mediaQuery.matches && !lightMediaQuery.matches) {
          return 'light'
        }
        
        return mediaQuery.matches ? 'dark' : 'light'
      } catch (error) {
        console.error('ThemeContext: Error detecting system theme:', error)
        return 'light'
      }
    }
    
    const handleSystemThemeChange = (e) => {
      console.log('ThemeContext: System theme changed, prefers dark:', e.matches)
      if (theme === 'auto') {
        const newResolvedTheme = getSystemTheme()
        console.log('ThemeContext: Auto mode, updating resolved theme to:', newResolvedTheme)
        setResolvedTheme(newResolvedTheme)
      }
    }

    // Set initial resolved theme for current theme setting
    if (theme === 'auto') {
      const newResolvedTheme = getSystemTheme()
      console.log('ThemeContext: Auto mode, setting resolved theme to:', newResolvedTheme)
      setResolvedTheme(newResolvedTheme)
    } else {
      console.log('ThemeContext: Manual mode, setting resolved theme to:', theme)
      setResolvedTheme(theme)
    }

    // Listen for system theme changes
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    mediaQuery.addEventListener('change', handleSystemThemeChange)

    return () => {
      mediaQuery.removeEventListener('change', handleSystemThemeChange)
    }
  }, [theme, isInitialized])

  // Update document theme when resolved theme changes
  useEffect(() => {
    console.log('ThemeContext: Updating document theme to:', resolvedTheme)
    const html = document.documentElement
    const body = document.body

    if (resolvedTheme === 'dark') {
      html.setAttribute('data-bs-theme', 'dark')
      html.classList.add('dark')
      html.classList.remove('light')
      console.log('ThemeContext: Applied dark theme to document')
    } else {
      html.setAttribute('data-bs-theme', 'light')
      html.classList.add('light')
      html.classList.remove('dark')
      console.log('ThemeContext: Applied light theme to document')
    }
  }, [resolvedTheme])

  const changeTheme = (newTheme) => {
    console.log('ThemeContext: Changing theme to:', newTheme)
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