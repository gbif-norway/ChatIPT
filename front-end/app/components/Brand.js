export default function Brand({ compact = false }) {
  return <span className={`brand${compact ? ' brand-compact' : ''}`}>
    <svg className="brand-mark" viewBox="0 0 48 48" fill="none" aria-hidden="true">
      <rect width="48" height="48" rx="15" fill="currentColor" />
      <path d="M24 36V23" stroke="#f4f7ec" strokeWidth="2" strokeLinecap="round" />
      <path d="M24 25C12 26 11 17 11 12c9-1 16 4 13 13Z" fill="#cbdc9f" />
      <path d="M24 30c-1-10 6-16 14-16 0 10-5 16-14 16Z" fill="#f4f7ec" />
      <path d="m18 19 6 6m8-4-8 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
    </svg>
    <span className="brand-name">ChatIPT</span>
  </span>
}
