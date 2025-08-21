'use client'

import React, { createContext, useContext, useState, useCallback } from 'react'
import Header from './Header'

// Create a context for navigation state
const NavigationContext = createContext()

export const useNavigation = () => {
  const context = useContext(NavigationContext)
  if (!context) {
    throw new Error('useNavigation must be used within a NavigationProvider')
  }
  return context
}

export const NavigationProvider = ({ children }) => {
  const [navigationState, setNavigationState] = useState({
    showNavigation: false,
    onNewDataset: null,
    onBackToDashboard: null
  })

  const updateNavigation = useCallback((state) => {
    setNavigationState(state)
  }, [])

  return (
    <NavigationContext.Provider value={{ navigationState, updateNavigation }}>
      {children}
    </NavigationContext.Provider>
  )
}

const HeaderWrapper = () => {
  const { navigationState } = useNavigation()

  return (
    <Header 
      showNavigation={navigationState.showNavigation}
      onNewDataset={navigationState.onNewDataset}
      onBackToDashboard={navigationState.onBackToDashboard}
    />
  )
}

export default HeaderWrapper


