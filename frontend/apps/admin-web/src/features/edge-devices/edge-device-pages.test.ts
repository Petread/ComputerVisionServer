import { describe, expect, it } from 'vitest';
import {
  edgeDiagnosticTone,
  edgeStatusLabel,
  edgeStatusTone,
  edgeTargetLabel
} from './presentation';

describe('edge device presentation', () => {
  it('keeps stale connectivity separate from reported runtime health', () => {
    expect(edgeStatusLabel('stale')).toBe('Veraltet');
    expect(edgeStatusTone('stale')).toBe('warn');
  });

  it('uses an error tone only for a confirmed offline device', () => {
    expect(edgeStatusTone('offline')).toBe('error');
    expect(edgeStatusTone('unknown')).toBe('neutral');
    expect(edgeStatusTone('online')).toBe('ok');
  });

  it('maps M5 diagnostics and execution targets for operator badges', () => {
    expect(edgeDiagnosticTone('healthy')).toBe('success');
    expect(edgeDiagnosticTone('degraded')).toBe('warn');
    expect(edgeDiagnosticTone('critical')).toBe('danger');
    expect(edgeTargetLabel('server')).toBe('Server');
    expect(edgeTargetLabel('edge_device', 'Orin Nano 1')).toBe('Edge · Orin Nano 1');
  });
});
