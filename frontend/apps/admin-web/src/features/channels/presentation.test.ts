import { describe, expect, it } from 'vitest';
import type { RuntimeChannelSummary, RuntimeManualCaptureTarget } from '@cv-platform/schemas';
import {
  channelStatusLabel,
  channelStatusTone,
  canCaptureManualSnapshot,
  canStartManualIntervalCapture,
  canStopManualIntervalCapture,
  filterChannels,
  formatPerformance,
  manualCaptureResultLabel,
  manualCaptureSessionLabel,
  manualCaptureSessionTone,
  manualCaptureTargetLabel,
  normalizeManualCaptureInterval,
  resolveManualCaptureTargetSelection,
  runtimeActionAvailability
} from './presentation';

const baseChannel: RuntimeChannelSummary = {
  channel_id: 'chn_press',
  slug: 'press-camera',
  name: 'Press camera',
  technical_place: 'Press line 1',
  mode: 'realtime',
  source_type: 'rtsp',
  source_summary: 'rtsp://camera.local/stream',
  assigned_pipeline: 'Defect inspection',
  output_summary: {
    summary: 'Overlay',
    connector_count: 1,
    primary_outputs: ['overlay']
  },
  capture_summary: {
    summary: 'Capture defects',
    rule_count: 1,
    target_project_count: 1,
    modes: ['roi']
  },
  health: {
    state: 'ok',
    status_text: 'Running'
  },
  performance: {
    fps: 24.4,
    latency_ms: 42,
    drop_rate_percent: 0.5,
    frame_count: 1200,
    drop_count: 6,
    trigger_count: 0,
    reconnect_count: 0,
    source_error_count: 0,
    last_result_status: 'completed'
  },
  desired_state: 'running',
  observed_state: 'running',
  status: 'running',
  health_state: 'ok',
  authoring_label_project_ids: [],
  result_history_policy: {
    enabled: false,
    retention_hours: 24,
    sample_interval_seconds: 5,
    max_records: 10000
  },
  stem_rotation_priority_control: {
    schema_version: 'runtime.stem-rotation-priority-control.v1',
    priority_source: 'FRONTEND',
    priority_mode: 'NONE',
    optimization_enable: true,
    revision: 0
  },
  placement: {
    target: 'server'
  },
  last_activity_at: '2026-05-01T10:00:00Z'
};

const manualCaptureTargets: RuntimeManualCaptureTarget[] = [
  {
    capture_target_id: 'lpr_obb_detection',
    display_name: 'OBB defects',
    detail: 'oriented detection - Press dataset - active',
    task_type: 'oriented_detection',
    status: 'active',
    dataset_name: 'Press dataset',
    label_studio_project_id: 42,
    selectable: true
  },
  {
    capture_target_id: 'lpr_segmentation',
    display_name: 'Segmentation captures',
    detail: 'segmentation - Segmentation dataset - active',
    task_type: 'segmentation',
    status: 'active',
    dataset_name: 'Segmentation dataset',
    label_studio_project_id: 77,
    selectable: true
  }
];

describe('channel presentation helpers', () => {
  it('filters channels by live search and dropdown values', () => {
    const snapshotChannel: RuntimeChannelSummary = {
      ...baseChannel,
      channel_id: 'chn_snapshot',
      name: 'Reject snapshot camera',
      technical_place: 'Packing station',
      mode: 'realtime',
      source_type: 'snapshot',
      source_summary: 'https://camera.local/reject.jpg',
      status: 'stopped',
      observed_state: 'stopped',
      health_state: 'down',
      health: { state: 'down', status_text: 'Stopped' }
    };

    const result = filterChannels([baseChannel, snapshotChannel], {
      search: 'reject',
      sourceType: 'snapshot',
      status: 'down',
      technicalPlace: 'Packing station'
    });

    expect(result).toHaveLength(1);
    expect(result[0].channel_id).toBe('chn_snapshot');
  });

  it('renders traffic-light status tones from health and observed state', () => {
    expect(channelStatusTone(baseChannel)).toBe('success');
    expect(channelStatusLabel(baseChannel)).toBe('Running');
    expect(channelStatusTone({ ...baseChannel, health_state: 'degraded', status: 'starting' })).toBe('warn');
    expect(channelStatusTone({ ...baseChannel, health_state: 'down', status: 'failed' })).toBe('danger');
  });

  it('keeps performance compact for channel cards', () => {
    expect(formatPerformance(baseChannel)).toBe('24.4 FPS / 42 ms / 0.5% drop');
    expect(formatPerformance({ ...baseChannel, performance: { ...baseChannel.performance, fps: null, latency_ms: null, drop_rate_percent: null } })).toBe('No live metrics');
  });

  it('keeps manual capture project selection operator-facing', () => {
    const label = manualCaptureTargetLabel({
      capture_target_id: 'lpr_internal_42',
      display_name: 'Bearing defects',
      detail: 'detection - Press dataset - active',
      task_type: 'detection',
      status: 'active',
      dataset_name: 'Press dataset',
      label_studio_project_id: 42,
      selectable: true
    });

    expect(label).toBe('Bearing defects - detection - Press dataset - active');
    expect(label).not.toContain('lpr_internal_42');
    expect(label).not.toContain('42');
  });

  it('labels manual capture state and validates simple interval input', () => {
    expect(manualCaptureSessionLabel('idle')).toBe('Idle');
    expect(manualCaptureSessionTone('running')).toBe('success');
    expect(manualCaptureSessionTone('failed')).toBe('danger');
    expect(manualCaptureResultLabel('queued')).toBe('Queued');
    expect(manualCaptureResultLabel('success')).toBe('Captured');
    expect(manualCaptureResultLabel('failed')).toBe('Failed');
    expect(normalizeManualCaptureInterval(0)).toBe(1);
    expect(normalizeManualCaptureInterval(12.6)).toBe(13);
    expect(normalizeManualCaptureInterval(4000)).toBe(3600);
  });

  it('enables manual capture actions without exposing internal IDs as typed input', () => {
    expect(canCaptureManualSnapshot('lpr_internal_42', false)).toBe(true);
    expect(canCaptureManualSnapshot('', false)).toBe(false);
    expect(canStartManualIntervalCapture('lpr_internal_42', 'idle', false)).toBe(true);
    expect(canStartManualIntervalCapture('lpr_internal_42', 'running', false)).toBe(false);
    expect(canStopManualIntervalCapture('running', false)).toBe(true);
    expect(canStopManualIntervalCapture('stopped', false)).toBe(false);
  });

  it('keeps stopped manual capture sessions from overriding a newly selected project', () => {
    const nextSelection = resolveManualCaptureTargetSelection(
      'lpr_segmentation',
      manualCaptureTargets,
      {
        state: 'stopped',
        capture_target_id: 'lpr_obb_detection'
      }
    );

    expect(nextSelection).toBe('lpr_segmentation');
  });

  it('only syncs manual capture selection from an actively running interval session', () => {
    expect(resolveManualCaptureTargetSelection('', manualCaptureTargets, null)).toBe('lpr_obb_detection');
    expect(
      resolveManualCaptureTargetSelection('lpr_segmentation', manualCaptureTargets, {
        state: 'running',
        capture_target_id: 'lpr_obb_detection'
      })
    ).toBe('lpr_obb_detection');
  });

  it('keeps runtime actions aligned with the observed and requested states', () => {
    const assignedChannel = { ...baseChannel, pipeline_version_id: 'rpv_press_v1' };
    const running = runtimeActionAvailability(assignedChannel);
    expect(running.start.enabled).toBe(false);
    expect(running.stop.enabled).toBe(true);
    expect(running.restart.enabled).toBe(true);

    const stopped = runtimeActionAvailability({
      ...assignedChannel,
      desired_state: 'stopped',
      observed_state: 'stopped',
      status: 'stopped'
    });
    expect(stopped.start.enabled).toBe(true);
    expect(stopped.stop.enabled).toBe(false);
    expect(stopped.restart.enabled).toBe(false);

    const degraded = runtimeActionAvailability({
      ...assignedChannel,
      observed_state: 'degraded',
      status: 'degraded'
    });
    expect(degraded.start.enabled).toBe(false);
    expect(degraded.stop.enabled).toBe(true);
    expect(degraded.restart.enabled).toBe(true);

    const failedButRequested = runtimeActionAvailability({
      ...assignedChannel,
      desired_state: 'running',
      observed_state: 'failed',
      status: 'failed'
    });
    expect(failedButRequested.start.enabled).toBe(false);
    expect(failedButRequested.stop.enabled).toBe(true);
    expect(failedButRequested.restart.enabled).toBe(true);
  });

  it('offers force recovery actions while a stop transition is stuck', () => {
    const stopping = runtimeActionAvailability({
      ...baseChannel,
      pipeline_version_id: 'rpv_press_v1',
      desired_state: 'stop_requested',
      observed_state: 'stopping',
      status: 'stopping'
    });

    expect(stopping.start.enabled).toBe(false);
    expect(stopping.stop.enabled).toBe(true);
    expect(stopping.restart.enabled).toBe(true);
    expect(stopping.stop.reason).toContain('Force-stop');
    expect(stopping.restart.reason).toContain('fresh executor');
  });

  it('offers force recovery actions while a start transition is stuck', () => {
    const starting = runtimeActionAvailability({
      ...baseChannel,
      pipeline_version_id: 'rpv_press_v1',
      desired_state: 'start_requested',
      observed_state: 'starting',
      status: 'starting'
    });

    expect(starting.start.enabled).toBe(false);
    expect(starting.stop.enabled).toBe(true);
    expect(starting.restart.enabled).toBe(true);
    expect(starting.stop.reason).toContain('normal start');
    expect(starting.restart.reason).toContain('fresh executor');
  });
});
