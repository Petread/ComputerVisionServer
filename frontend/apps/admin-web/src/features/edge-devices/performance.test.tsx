import { renderToStaticMarkup } from 'react-dom/server';
import type { EdgeDeviceDetail } from '@cv-platform/schemas';
import { describe, expect, it, vi } from 'vitest';
import { EdgePerformancePanel, edgePerformanceIsFresh } from './performance';

vi.mock('@tanstack/react-router', () => ({ Link: ({ children }: { children: React.ReactNode }) => <a>{children}</a> }));
const now = Date.parse('2026-09-21T08:00:00Z');
const timing = { key: 'pipeline', sample_count: 100, mean_ms: 20, p95_ms: 25, max_ms: 30, last_ms: 21 };
const device = {
  edge_device_id: 'edge-1', name: 'Jetson', enabled: true, connectivity_state: 'online',
  last_seen_at: '2026-09-21T08:00:00Z', current_boot_id: 'boot-1',
  health: { boot_id: 'boot-1', runtime: { boot_id: 'boot-1', processes: [{
    deployment_id: 'dep-1', channel_id: 'channel-1', state: 'running', restart_count: 0,
    metrics: { observed_at: '2026-09-21T08:00:00Z', window_seconds: 10,
      source_fps: 60, processed_fps: 30, acquired_frames: 600, processed_frames: 300,
      sampled_out_frames: 300, execution_errors: 0, stages: [timing],
      nodes: [{ ...timing, key: 'classification', node_type: 'inference.classification' }] },
    engines: [{ node_key: 'classification', cache_hit: true, duration_ms: 4, engine_size_bytes: 1048576 }]
  }] } }
} as EdgeDeviceDetail;

describe('Edge performance', () => {
  it('shows measured source/processing rates, node timing and persistent cache reuse', () => {
    const html = renderToStaticMarkup(<EdgePerformancePanel device={device} channelNames={{ 'channel-1': 'Messkanal' }} now={now} />);
    expect(html).toContain('Messkanal');
    expect(html).toContain('30.0 FPS');
    expect(html).toContain('60.0 FPS');
    expect(html).toContain('25.0 ms');
    expect(html).toContain('classification');
    expect(html).toContain('Aus persistentem Cache geladen');
    expect(html).toContain('durch Sampling ausgelassen');
  });

  it('never presents stale, offline or previous-boot rates as live values', () => {
    expect(edgePerformanceIsFresh(device, now + 46_000)).toBe(false);
    expect(edgePerformanceIsFresh({ ...device, connectivity_state: 'offline' }, now)).toBe(false);
    expect(edgePerformanceIsFresh({ ...device, current_boot_id: 'boot-2' }, now)).toBe(false);
    const html = renderToStaticMarkup(<EdgePerformancePanel device={device} now={now + 46_000} />);
    expect(html).not.toContain('30.0 FPS');
    expect(html).not.toContain('25.0 ms');
    expect(html).toContain('Kein aktueller Gerätestatus');
    expect(html).toContain('Letzter bekannter Stand');
  });

  it('supports older agents and filters statistics to the selected channel', () => {
    const older = { ...device, health: { ...device.health!, runtime: { ...device.health!.runtime!, processes: [{ deployment_id: 'dep-old', state: 'running' as const, restart_count: 0 }] } } };
    const html = renderToStaticMarkup(<EdgePerformancePanel device={older} now={now} />);
    expect(html).toContain('Noch keine Leistungsmessung');
    const filtered = renderToStaticMarkup(<EdgePerformancePanel device={device} channelId="other" now={now} />);
    expect(filtered).toContain('Kein aktiver Edge-Kanal gemeldet');
    expect(filtered).not.toContain('30.0 FPS');
  });
});
