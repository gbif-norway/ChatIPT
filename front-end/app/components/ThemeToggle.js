'use client'

import { useState, useRef, useEffect } from 'react'
import { useTheme } from '../contexts/ThemeContext'

const ThemeToggle = () => {
  const { theme, changeTheme, isDark, isLight, isAuto } = useTheme()
  const [isOpen, setIsOpen] = useState(false)
  const dropdownRef = useRef(null)

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target)) {
        setIsOpen(false)
      }
    }

    document.addEventListener('mousedown', handleClickOutside)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [])

  const handleThemeChange = (newTheme) => {
    changeTheme(newTheme)
    setIsOpen(false)
  }

  const getThemeIcon = () => {
    if (isAuto) return 'bi-circle-half'
    if (isDark) return 'bi-moon-fill'
    return 'bi-sun-fill'
  }

  const getThemeLabel = () => {
    if (isAuto) return 'Auto'
    if (isDark) return 'Dark'
    return 'Light'
  }

  return (
    <div className="dropdown" ref={dropdownRef}>
      <button
        className={`btn btn-sm dropdown-toggle ${isDark ? 'btn-outline-light' : 'btn-outline-secondary'}`}
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
        aria-label="Toggle theme"
      >
        <i className={`bi ${getThemeIcon()} me-1`}></i>
        {getThemeLabel()}
      </button>
      
      <ul className={`dropdown-menu ${isOpen ? 'show' : ''}`} style={{
        position: 'absolute',
        right: 0,
        top: '100%',
        zIndex: 1000,
        minWidth: '120px'
      }}>
        <li>
          <button
            className={`dropdown-item ${isAuto ? 'active' : ''}`}
            onClick={() => handleThemeChange('auto')}
          >
            <i className="bi bi-circle-half me-2"></i>
            Auto
          </button>
        </li>
        <li>
          <button
            className={`dropdown-item ${isLight ? 'active' : ''}`}
            onClick={() => handleThemeChange('light')}
          >
            <i className="bi bi-sun-fill me-2"></i>
            Light
          </button>
        </li>
        <li>
          <button
            className={`dropdown-item ${isDark ? 'active' : ''}`}
            onClick={() => handleThemeChange('dark')}
          >
            <i className="bi bi-moon-fill me-2"></i>
            Dark
          </button>
        </li>
      </ul>
    </div>
  )
}

export default ThemeToggle 