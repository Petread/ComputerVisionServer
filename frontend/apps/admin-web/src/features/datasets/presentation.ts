export function formatDate(value?: string | null) {
  if (!value) {
    return 'Not recorded';
  }
  return new Intl.DateTimeFormat('en-GB', {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(new Date(value));
}

export function statusTone(status?: string | null) {
  const value = (status ?? '').toLowerCase();
  if (
    ['ready', 'passed', 'processed', 'active', 'snapshotted', 'completed', 'succeeded'].includes(
      value
    )
  ) {
    return 'success';
  }
  if (['failed', 'down', 'archived', 'needs_attention'].includes(value)) {
    return 'danger';
  }
  if (['requested', 'exporting', 'materializing', 'validating', 'pending', 'processing'].includes(value)) {
    return 'warn';
  }
  return 'neutral';
}

export function stringifyClassDistribution(distribution: Record<string, number>) {
  const entries = Object.entries(distribution);
  if (entries.length === 0) {
    return 'No class distribution yet';
  }
  return entries.map(([label, count]) => `${label}: ${count}`).join(', ');
}
