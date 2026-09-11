import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { RuntimeOperationalSummary } from '@cv-platform/schemas';
import { RuntimePlanePanel } from './health-page';

describe('RuntimePlanePanel', () => {
  it('renders compact recovery indicators and remediation hints', () => {
    const runtime: RuntimeOperationalSummary = {
      state: 'degraded',
      total_channels: 2,
      running_channels: 1,
      degraded_channels: 1,
      failed_channels: 0,
      stopped_channels: 1,
      realtime_active_channels: 1,
      trigger_active_channels: 0,
      active_runtime_instances: 1,
      stale_runtime_instances: 1,
      failed_runtime_instances: 0,
      recovering_channels: 0,
      performance: {
        realtime_fps: 12.4,
        realtime_latency_p95_ms: 310,
        realtime_drop_rate_percent: 4.2,
        trigger_latency_p95_ms: null,
        trigger_timeout_count: 0,
        frame_count: 120,
        drop_count: 5,
        trigger_count: 0
      },
      attention: [
        {
          channel_id: 'chn_press',
          name: 'Press line camera',
          technical_place: 'press-line-1',
          mode: 'realtime',
          observed_state: 'degraded',
          health_state: 'degraded',
          health_text: 'Executor heartbeat is stale',
          runtime_instance_id: 'rex_press',
          executor_service: 'realtime-executor',
          assigned_runtime_worker: 'runtime-orchestrator-a',
          last_heartbeat_at: '2026-05-02T08:00:00Z',
          heartbeat_age_seconds: 600,
          recovery_attempts: 1,
          recovery_hint: 'Restart the channel after confirming the source is healthy.',
          fps: 12.4,
          latency_p95_ms: 310,
          drop_rate_percent: 4.2,
          trigger_latency_ms: null,
          frame_count: 120,
          drop_count: 5,
          trigger_count: 0,
          last_error: null
        }
      ],
      executors: [],
      recovery_hint: 'Restart or reconcile stale channels, then confirm executor heartbeats refresh.',
      observed_at: '2026-05-02T08:10:00Z'
    };

    const html = renderToStaticMarkup(<RuntimePlanePanel runtime={runtime} />);

    expect(html).toContain('Runtime Plane');
    expect(html).toContain('Realtime FPS');
    expect(html).toContain('12.4');
    expect(html).toContain('310 ms');
    expect(html).toContain('Press line camera');
    expect(html).toContain('Executor heartbeat is stale');
    expect(html).toContain('Restart the channel after confirming the source is healthy.');
    expect(html).toContain('Restart or reconcile stale channels');
  });
});
