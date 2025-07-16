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
    
    // Initialize resolved theme based on current theme setting
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    console.log('ThemeContext: System prefers dark mode:', mediaQuery.matches)
    
    let initialResolvedTheme = 'dark' // default fallback
    
    if (savedTheme === 'auto' || !savedTheme) {
      initialResolvedTheme = mediaQuery.matches ? 'dark' : 'light'
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
    
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    console.log('ThemeContext: Setting up system theme listener, current theme:', theme)
    
    const handleSystemThemeChange = (e) => {
      console.log('ThemeContext: System theme changed, prefers dark:', e.matches)
      if (theme === 'auto') {
        const newResolvedTheme = e.matches ? 'dark' : 'light'
        console.log('ThemeContext: Auto mode, updating resolved theme to:', newResolvedTheme)
        setResolvedTheme(newResolvedTheme)
      }
    }

    // Set initial resolved theme for current theme setting
    if (theme === 'auto') {
      const newResolvedTheme = mediaQuery.matches ? 'dark' : 'light'
      console.log('ThemeContext: Auto mode, setting resolved theme to:', newResolvedTheme)
      setResolvedTheme(newResolvedTheme)
    } else {
      console.log('ThemeContext: Manual mode, setting resolved theme to:', theme)
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