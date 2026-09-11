import type { EdgeConnectivityState, EdgeDiagnosticStatus } from '@cv-platform/schemas';

export function edgeStatusLabel(state: EdgeConnectivityState): string {
  return {
    online: 'Online',
    degraded: 'Eingeschränkt',
    offline: 'Offline',
    stale: 'Veraltet',
    unknown: 'Unbekannt'
  }[state];
}

export function edgeStatusTone(
  state: EdgeConnectivityState
): 'ok' | 'warn' | 'error' | 'neutral' {
  if (state === 'online') return 'ok';
  if (state === 'offline') return 'error';
  if (state === 'degraded' || state === 'stale') return 'warn';
  return 'neutral';
}

export function edgeDiagnosticTone(
  status: EdgeDiagnosticStatus
): 'success' | 'warn' | 'danger' {
  if (status === 'healthy') return 'success';
  if (status === 'critical') return 'danger';
  return 'warn';
}

export function edgeTargetLabel(
  target: 'server' | 'edge_device',
  deviceName?: string | null
): string {
  return target === 'edge_device' ? `Edge · ${deviceName || 'Gerät'}` : 'Server';
}
