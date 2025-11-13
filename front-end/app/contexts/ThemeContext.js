'use client'

import { createContext, useContext, useEffect } from 'react'
import { useTheme as useNextTheme, ThemeProvider as NextThemeProvider } from 'next-themes'

const ThemeContext = createContext()

export const useTheme = () => {
  const context = useContext(ThemeContext)
  if (!context) {
    throw new Error('useTheme must be used within a ThemeProvider')
  }
  return context
}

const ThemeContextProvider = ({ children }) => {
  const { theme, setTheme, resolvedTheme } = useNextTheme()

  // Update document attributes when theme changes
  useEffect(() => {
    const html = document.documentElement
    
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
  }

  const value = {
    theme: theme || 'system',
    resolvedTheme: resolvedTheme || 'dark',
    changeTheme,
    isDark: resolvedTheme === 'dark',
    isLight: resolvedTheme === 'light',
    isAuto: theme === 'system' || !theme
  }

  return (
    <ThemeContext.Provider value={value}>
      {children}
    </ThemeContext.Provider>
  )
}

export const ThemeProvider = ({ children }) => {
  return (
    <NextThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
      storageKey="chatipt-theme"
      enableColorScheme={false}
    >
      <ThemeContextProvider>
        {children}
      </ThemeContextProvider>
    </NextThemeProvider>
  )
}
