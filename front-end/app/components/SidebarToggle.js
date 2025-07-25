'use client'

const SidebarToggle = ({ isOpen, onToggle }) => {
  return (
    <button
      className="btn btn-outline-secondary btn-sm d-flex align-items-center"
      onClick={onToggle}
      style={{
        position: 'fixed',
        top: '20px',
        left: isOpen ? '340px' : '20px',
        zIndex: 1060,
        transition: 'left 0.3s ease-in-out',
        backgroundColor: '#fff',
        border: '1px solid #dee2e6',
        borderRadius: '8px',
        padding: '8px 12px',
        boxShadow: '0 2px 4px rgba(0,0,0,0.1)'
      }}
    >
      <i className={`bi ${isOpen ? 'bi-chevron-left' : 'bi-list'} me-1`}></i>
      <span className="d-none d-sm-inline">
        {isOpen ? 'Hide' : 'History'}
      </span>
    </button>
  )
}

export default SidebarToggle 