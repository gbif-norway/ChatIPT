// Centralized loading text helper to keep messages consistent
export const getLoadingText = ({ phase = 'working', context = null, long = false } = {}) => {
  // phase: 'working' | 'still' | 'loading'
  const ellipsis = '...';
  if (phase === 'loading') {
    return `Loading${ellipsis}`;
  }
  if (phase === 'still') {
    const base = `Still working${ellipsis}`;
    const parts = [base];
    if (context) parts.push(context);
    if (long) parts.push('(can take a while)');
    return parts.join(' ');
  }
  // default 'working'
  const parts = ['Working' + ellipsis];
  if (context) parts.push(context);
  return parts.join(' ');
};

