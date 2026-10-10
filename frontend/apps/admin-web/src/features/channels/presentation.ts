import type {
  HealthState,
  RuntimeChannelDesiredState,
  RuntimeChannelStreamListResponse,
  RuntimeManualCaptureResultStatus,
  RuntimeManualCaptureSessionStatus,
  RuntimeManualCaptureSessionState,
  RuntimeManualCaptureTarget,
  RuntimeChannelSummary,
  RuntimeMode,
  RuntimeLiveViewState,
  RuntimeObservedState,
  RuntimeSourceType
} from '@cv-platform/schemas';

export type RuntimeAction = 'start' | 'stop' | 'restart';

export type RuntimeActionAvailability = Record<
  RuntimeAction,
  { enabled: boolean; reason: string }
>;

const runtimeStartingStates: RuntimeObservedState[] = [
  'validating',
  'loading_pipeline',
  'starting'
];
const runtimeOperationalStates: RuntimeObservedState[] = ['running', 'degraded', 'recovering'];
const runtimeStoppingStates: RuntimeObservedState[] = ['stopping'];

export function channelRuntimeActualState(
  channel: Pick<
    RuntimeChannelSummary,
    'placement' | 'runtime_state' | 'observed_state' | 'status'
  >
): RuntimeObservedState {
  if (channel.placement.target === 'edge_device') {
    return channel.observed_state ?? channel.status;
  }
  return channel.runtime_state?.actual_state ?? channel.observed_state ?? channel.status;
}

export function runtimeActionAvailability(
  channel: Pick<
    RuntimeChannelSummary,
    | 'desired_state'
    | 'observed_state'
    | 'pipeline_version_id'
    | 'placement'
    | 'runtime_state'
    | 'status'
  >,
  options: { busy?: boolean; triggerOnly?: boolean } = {}
): RuntimeActionAvailability {
  const actualState = channelRuntimeActualState(channel);
  const desiredState = channel.runtime_state?.desired_state ?? channel.desired_state;
  const busy = Boolean(options.busy);
  const hasPipeline = Boolean(channel.pipeline_version_id);
  const runtimeStopping =
    desiredState === 'stop_requested' || runtimeStoppingStates.includes(actualState);
  const runtimeStarting =
    desiredState === 'start_requested' || runtimeStartingStates.includes(actualState);
  const commandPending =
    runtimeStarting || runtimeStopping;
  const runtimeOperational = runtimeOperationalStates.includes(actualState);
  const runtimeFailed = actualState === 'failed';

  const commonStartReason = busy
    ? 'A runtime command is currently being submitted.'
    : !hasPipeline
        ? 'Assign and publish a pipeline before starting the runtime.'
        : desiredState === 'disabled'
          ? 'The channel is disabled and must be enabled first.'
          : commandPending
            ? 'A runtime state transition is already in progress.'
            : runtimeOperational
              ? `The runtime is already ${actualState.replaceAll('_', ' ')}.`
              : desiredState === 'running'
                ? 'The runtime is already requested to run; use Restart if recovery is required.'
                : 'Start the stopped runtime.';
  const startEnabled =
    !busy &&
    hasPipeline &&
    desiredState !== 'disabled' &&
    !commandPending &&
    !runtimeOperational &&
    desiredState !== 'running';

  const stopReason = busy
    ? 'A runtime command is currently being submitted.'
    : runtimeStopping
      ? 'Force-stop the runtime when the normal stop no longer completes.'
      : runtimeStarting
        ? 'Force-stop the runtime when the normal start no longer completes.'
      : runtimeOperational || runtimeStartingStates.includes(actualState) || desiredState === 'running'
        ? 'Stop the active runtime.'
        : 'The runtime is already stopped.';
  const stopEnabled =
    !busy &&
    (runtimeStopping ||
      runtimeOperational ||
      runtimeStartingStates.includes(actualState) ||
      desiredState === 'running');

  const restartReason = busy
    ? 'A runtime command is currently being submitted.'
    : !hasPipeline
        ? 'Assign and publish a pipeline before restarting the runtime.'
        : runtimeStopping || runtimeStarting
          ? 'Force-stop the stuck runtime and start a fresh executor.'
          : commandPending
          ? 'A runtime state transition is already in progress.'
          : runtimeOperational || (runtimeFailed && desiredState === 'running')
            ? 'Stop and start this runtime with its current configuration.'
            : 'Start is the correct action while the runtime is stopped.';
  const restartEnabled =
    !busy &&
    hasPipeline &&
    desiredState !== 'disabled' &&
    (!commandPending || runtimeStopping || runtimeStarting) &&
    (runtimeStopping ||
      runtimeStarting ||
      runtimeOperational ||
      (runtimeFailed && desiredState === 'running'));

  return {
    start: { enabled: startEnabled, reason: commonStartReason },
    stop: { enabled: stopEnabled, reason: stopReason },
    restart: { enabled: restartEnabled, reason: restartReason }
  };
}

export function runtimeDesiredStateLabel(state: RuntimeChannelDesiredState) {
  const labels: Record<RuntimeChannelDesiredState, string> = {
    draft: 'Draft',
    stopped: 'Stopped',
    start_requested: 'Start requested',
    running: 'Running requested',
    stop_requested: 'Stop requested',
    disabled: 'Disabled'
  };
  return labels[state];
}

export function runtimeObservedStateLabel(state: RuntimeObservedState) {
  return state.replaceAll('_', ' ').replace(/^./, (character) => character.toUpperCase());
}

export function shouldUseLiveFrameStream(
  persistedActualState: RuntimeObservedState | undefined,
  streamActualState: RuntimeChannelStreamListResponse['actual_state'] | undefined
) {
  return (
    persistedActualState === 'running' ||
    persistedActualState === 'degraded' ||
    streamActualState === 'ready'
  );
}

export function effectiveRuntimeActualState(
  persistedActualState: RuntimeObservedState,
  liveState:
    | Pick<RuntimeLiveViewState, 'actual_state' | 'available' | 'stale'>
    | null
    | undefined
) {
  if (
    liveState?.available &&
    !liveState.stale &&
    (liveState.actual_state === 'running' || liveState.actual_state === 'degraded')
  ) {
    return liveState.actual_state;
  }
  return persistedActualState;
}

export type ChannelFilters = {
  search: string;
  sourceType: RuntimeSourceType | 'all';
  status: RuntimeObservedState | HealthState | 'all';
  technicalPlace: string;
};

export function filterChannels(channels: RuntimeChannelSummary[], filters: ChannelFilters) {
  const search = filters.search.trim().toLowerCase();
  return channels.filter((channel) => {
    const matchesSearch =
      !search ||
      [
        channel.name,
        channel.technical_place,
        channel.source_summary,
        channel.assigned_pipeline ?? '',
        channel.health.status_text
      ]
        .join(' ')
        .toLowerCase()
        .includes(search);
    const matchesSource =
      filters.sourceType === 'all' || channel.source_type === filters.sourceType;
    const matchesStatus =
      filters.status === 'all' ||
      channel.status === filters.status ||
      channel.health_state === filters.status;
    const matchesPlace =
      !filters.technicalPlace || channel.technical_place === filters.technicalPlace;
    return matchesSearch && matchesSource && matchesStatus && matchesPlace;
  });
}

export function channelStatusTone(
  channel: Pick<RuntimeChannelSummary, 'health_state' | 'status'>
): 'success' | 'warn' | 'danger' | 'neutral' {
  if (channel.health_state === 'ok' && channel.status === 'running') {
    return 'success';
  }
  if (
    channel.health_state === 'degraded' ||
    ['starting', 'validating', 'loading_pipeline', 'stopping'].includes(channel.status)
  ) {
    return 'warn';
  }
  if (channel.health_state === 'down' && ['failed', 'unassigned'].includes(channel.status)) {
    return 'danger';
  }
  return 'neutral';
}

export function channelStatusLabel(channel: RuntimeChannelSummary) {
  if (channel.health.status_text) {
    return channel.health.status_text;
  }
  return channel.status.replaceAll('_', ' ');
}

export function sourceTypeLabel(sourceType: RuntimeSourceType) {
  const labels: Record<RuntimeSourceType, string> = {
    rtsp: 'RTSP stream',
    usb_opencv: 'USB camera (OpenCV/UVC)',
    aravis_gige: 'GigE Vision (Aravis)',
    flir_blackfly: 'FLIR Blackfly',
    basler_pylon: 'Basler Pylon',
    snapshot: 'Snapshot',
    trigger_capture: 'Discrete camera input',
    file_test: 'Test file'
  };
  return labels[sourceType];
}

export function runtimeModeLabel(mode: RuntimeMode) {
  const labels: Record<RuntimeMode, string> = {
    realtime: 'Realtime',
    trigger: 'Trigger',
    interval: 'Interval'
  };
  return labels[mode];
}

export function formatActivity(value?: string | null) {
  if (!value) {
    return 'No activity yet';
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(new Date(value));
}

export function formatPerformance(channel: RuntimeChannelSummary) {
  const parts = [];
  if (channel.performance.fps !== null && channel.performance.fps !== undefined) {
    parts.push(`${channel.performance.fps.toFixed(1)} FPS`);
  }
  if (channel.performance.latency_ms !== null && channel.performance.latency_ms !== undefined) {
    parts.push(`${channel.performance.latency_ms.toFixed(0)} ms`);
  }
  if (
    channel.performance.drop_rate_percent !== null &&
    channel.performance.drop_rate_percent !== undefined
  ) {
    parts.push(`${channel.performance.drop_rate_percent.toFixed(1)}% drop`);
  }
  return parts.length ? parts.join(' / ') : 'No live metrics';
}

export function manualCaptureSessionLabel(state: RuntimeManualCaptureSessionState) {
  const labels: Record<RuntimeManualCaptureSessionState, string> = {
    idle: 'Idle',
    running: 'Running',
    stopped: 'Stopped',
    failed: 'Failed'
  };
  return labels[state];
}

export function manualCaptureSessionTone(
  state: RuntimeManualCaptureSessionState
): 'success' | 'warn' | 'danger' | 'neutral' {
  if (state === 'running') {
    return 'success';
  }
  if (state === 'failed') {
    return 'danger';
  }
  if (state === 'stopped') {
    return 'warn';
  }
  return 'neutral';
}

export function manualCaptureResultLabel(status?: RuntimeManualCaptureResultStatus | null) {
  if (status === 'success') {
    return 'Captured';
  }
  if (status === 'queued') {
    return 'Queued';
  }
  if (status === 'failed') {
    return 'Failed';
  }
  return 'No capture yet';
}

export function manualCaptureTargetLabel(target: RuntimeManualCaptureTarget) {
  return [target.display_name, target.detail].filter(Boolean).join(' - ');
}

export function resolveManualCaptureTargetSelection(
  selectedTargetId: string,
  targets: RuntimeManualCaptureTarget[],
  session?: Pick<RuntimeManualCaptureSessionStatus, 'state' | 'capture_target_id'> | null
) {
  const runningTargetId = session?.state === 'running' ? session.capture_target_id : undefined;
  if (runningTargetId && runningTargetId !== selectedTargetId) {
    return runningTargetId;
  }
  if (!selectedTargetId && targets.length) {
    return targets[0].capture_target_id;
  }
  return selectedTargetId;
}

export function normalizeManualCaptureInterval(value: number) {
  if (!Number.isFinite(value)) {
    return 10;
  }
  return Math.min(Math.max(Math.round(value), 1), 3600);
}

export function canCaptureManualSnapshot(targetId: string, busy: boolean) {
  return Boolean(targetId) && !busy;
}

export function canStartManualIntervalCapture(
  targetId: string,
  state: RuntimeManualCaptureSessionState,
  busy: boolean
) {
  return Boolean(targetId) && state !== 'running' && !busy;
}

export function canStopManualIntervalCapture(
  state: RuntimeManualCaptureSessionState,
  busy: boolean
) {
  return state === 'running' && !busy;
}
