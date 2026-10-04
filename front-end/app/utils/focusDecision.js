// Scroll to a choice card and highlight it briefly. Cards are tagged with data-decision-id.
export function focusDecision(id) {
  if (typeof document === 'undefined') return false
  const card = document.querySelector(`[data-decision-id="${CSS.escape(id)}"]`)
  if (!card) return false
  for (let node = card.parentElement; node; node = node.parentElement) {
    if (node.tagName === 'DETAILS') node.open = true
  }
  const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  card.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'center' })
  card.classList.remove('decision-highlight')
  void card.offsetWidth // Restart the highlight if the card was highlighted a moment ago.
  card.classList.add('decision-highlight')
  window.setTimeout(() => card.classList.remove('decision-highlight'), 2500)
  card.querySelector('select')?.focus({ preventScroll: true })
  return true
}
