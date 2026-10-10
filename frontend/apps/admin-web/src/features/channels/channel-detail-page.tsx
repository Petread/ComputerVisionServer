import { useUnsavedWorkspace } from '../../components/use-unsaved-workspace';
import { Icon, Modal, ToolButton, Tabs } from '../../components/workspace-ui';
import { OverlayControls, LiveViewport } from '../../components/live-workspace-controls';
import { FacilitySelect } from './places';
import { BaslerCameraIpField, baslerCameraAddressConfig, validBaslerCameraIp } from './basler-camera-address';
import { ChannelResultsWorkspace } from './channel-results-workspace';
import { LabelStudioProjectLink } from '../pipelines/label-studio-link';
import {
  type CSSProperties,
  type SetStateAction,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  LabelProjectSummary,
  RuntimeChannelStreamListResponse,
  RuntimeLiveFrame,
  RuntimeLiveViewState,
  RuntimeLiveViewResponse,
  RuntimeStreamEndpoint,
  RuntimeStreamVariantStatus,
  RuntimeOverlayPrimitiveState,
  RuntimeManualCaptureResult,
  RuntimeManualCaptureSessionStatus,
  RuntimeManualCaptureTarget,
  RuntimeChannelRequestEndpoint,
  RuntimeChannelPlacement,
  RuntimeChannelSummary,
  RuntimeChannelUpdateRequest,
  RuntimeStemRotationPriorityControl,
  RuntimePipelineDefinitionDetail,
  RuntimePipelineDefinitionSummary,
  RuntimePipelineVersionSummary,
  RuntimeResultRecordSummary,
  RuntimeSourceType,
  StemRotationPriorityMode
} from '@cv-platform/schemas';
import { api, apiBaseUrls } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import { loadMediaMtxReader, type MediaMtxReader } from '../../lib/mediamtx-reader';
import { useEdgeViewerLease } from './use-edge-viewer-lease';
import { ChannelEdgePerformance } from '../edge-devices/performance';
import { RuntimePipelineDesignerPage } from '../runtime-pipelines/runtime-pipeline-designer-page';
import {
  diagnosticGraphFromOverlay,
  StemCurvaturePhaseRuntimePanel
} from '../stem-orientation/curvature-phase-diagnostic-graph';
import {
  StemRotationOptimizationView,
  stemRotationViewModelFromResult
} from '../stem-rotation/stem-rotation-optimization-view';
import {
  channelStatusLabel,
  channelStatusTone,
  canCaptureManualSnapshot,
  canStartManualIntervalCapture,
  canStopManualIntervalCapture,
  channelRuntimeActualState,
  effectiveRuntimeActualState,
  formatActivity,
  manualCaptureResultLabel,
  manualCaptureSessionLabel,
  manualCaptureSessionTone,
  manualCaptureTargetLabel,
  normalizeManualCaptureInterval,
  resolveManualCaptureTargetSelection,
  runtimeActionAvailability,
  runtimeDesiredStateLabel,
  runtimeObservedStateLabel,
  shouldUseLiveFrameStream,
  sourceTypeLabel
} from './presentation';

const sourceTypes: RuntimeSourceType[] = [
  'rtsp',
  'usb_opencv',
  'aravis_gige',
  'flir_blackfly',
  'basler_pylon',
  'snapshot',
  'trigger_capture',
  'file_test'
];

const baslerAcquisitionModes = ['Continuous', 'SingleFrame', 'MultiFrame'];
const baslerGrabStrategies = ['LatestImageOnly', 'OneByOne', 'LatestImages'];
const baslerPixelFormats = ['BayerRG8', 'BayerBG8', 'BayerGB8', 'BayerGR8', 'Mono8', 'RGB8', 'BGR8', 'YUV422Packed'];
const baslerTriggerModes = ['Off', 'On'];
const baslerTriggerSources = ['Line1', 'Line2', 'Software'];
const baslerTriggerActivations = ['RisingEdge', 'FallingEdge', 'AnyEdge'];
const baslerTransportModes = [
  { value: 'auto', label: 'Auto' },
  { value: 'pylon_direct', label: 'Pylon direct' },
  { value: 'aravis_rtsp_bridge', label: 'Aravis RTSP bridge' }
];
const WEBRTC_MAX_RECONNECT_ATTEMPTS = 5;
const WEBRTC_FIRST_FRAME_TIMEOUT_MS = 3000;

const emptyForm = {
  name: '',
  technicalPlace: '',
  facilityId: '',
  description: '',
  sourceType: 'rtsp' as RuntimeSourceType,
  sourceAddress: '',
  testFileName: '',
  testFileSourceConfig: {} as Record<string, unknown>,
  rtspUsername: '',
  rtspPassword: '',
  baslerAcquisitionMode: 'Continuous',
  baslerGrabStrategy: 'LatestImageOnly',
  baslerPixelFormat: 'BayerRG8',
  baslerCameraFps: '30',
  baslerLinkSpeedMbps: '1000',
  baslerExposureTimeUs: '',
  baslerGainDb: '',
  baslerTriggerMode: 'Off',
  baslerTriggerSource: 'Line1',
  baslerTriggerActivation: 'RisingEdge',
  baslerPacketSize: '',
  baslerInterPacketDelay: '',
  baslerBufferCount: '16',
  baslerOutputQueueSize: '',
  baslerFrameRetentionMs: '',
  baslerGrabFailureReconnectThreshold: '10',
  baslerWidth: '',
  baslerHeight: '',
  baslerTransportMode: 'auto',
  baslerCameraIp: '',
  maxFps: '',
  sampleEveryN: '1',
  pipelineMaxInflight: '2',
  assignedPipeline: '',
  pipelineVersionId: '',
  authoringLabelProjectIds: [] as string[],
  captureSummary: '',
  historyEnabled: false,
  historyRetentionHours: '24',
  historySampleIntervalSeconds: '5',
  historyMaxRecords: '10000',
  executionTarget: 'server' as RuntimeChannelPlacement['target'],
  edgeDeviceId: ''
};

export function ChannelDetailPage() {
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [configSection, setConfigSection] = useState('Allgemein & Platz');
  const [workspaceTab, setWorkspaceTab] = useState('Betrieb');
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [pipelineOpened, setPipelineOpened] = useState(false);
  const liveVisible = workspaceTab === 'Betrieb';
  const { channelId } = useParams({ from: '/authenticated/channels/$channelId' });
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { notify } = useNotifications();
  const [form, setForm] = useState(emptyForm);
  const [channelFormDirty, setChannelFormDirty] = useState(false);
  useUnsavedWorkspace(channelFormDirty);
  const [selectedCaptureTargetId, setSelectedCaptureTargetId] = useState('');
  const [manualIntervalSeconds, setManualIntervalSeconds] = useState(10);
  const [manualCapturePredictions, setManualCapturePredictions] = useState(true);
  const [manualCaptureSource, setManualCaptureSource] = useState<
    'LIVE_VIEW_FRAME' | 'PIPELINE_IMAGE_OUTPUT'
  >('LIVE_VIEW_FRAME');
  const autoSelectedCanonicalCaptureProfileRef = useRef<string | null>(null);
  const [manualCaptureFeedback, setManualCaptureFeedback] = useState<{
    tone: 'success' | 'error';
    message: string;
  } | null>(null);
  const [visibleOverlayLayers, setVisibleOverlayLayers] = useState<Record<string, boolean>>(
    () => readLiveOverlayLayerVisibility(channelId)
  );
  const [overlayVisibilityChannelId, setOverlayVisibilityChannelId] = useState(channelId);
  const [knownLiveLayers, setKnownLiveLayers] = useState<string[]>([]);
  const [runtimePreviewView, setRuntimePreviewView] = useState<RuntimePreviewView>('full');
  const [overlayEventState, setOverlayEventState] = useState<RuntimeLiveViewState | null>(null);
  const overlayExpiryTimerRef = useRef<number | null>(null);
  const activeBoardGeometryWarningRef = useRef<string | null>(null);
  const boardGeometryWarningShownAtRef = useRef(new Map<string, number>());

  const channelQuery = useQuery({
    queryKey: ['runtime-channel', channelId],
    queryFn: () => api.channels.detail(channelId),
    refetchInterval: channelFormDirty ? false : 8000
  });
  const edgeDevicesQuery = useQuery({
    queryKey: ['edge-devices', 'channel-placement'],
    queryFn: () => api.edgeDevices.list({ enabled: true }),
    staleTime: 15_000
  });
  const updateChannelForm = (next: SetStateAction<typeof emptyForm>) => {
    setChannelFormDirty(true);
    setForm(next);
  };
  const persistedLiveActualState = channelQuery.data
    ? channelRuntimeActualState(channelQuery.data)
    : undefined;
  const hasTriggerLiveState = (channelQuery.data?.triggerable_pipelines?.length ?? 0) > 0;
  const streamQuery = useQuery({
    queryKey: ['runtime-channel-streams', channelId],
    queryFn: () => api.channels.streams(channelId),
    enabled: Boolean(channelQuery.data),
    refetchInterval: 3000
  });
  const streamStatusError = streamQuery.error
    ? formatErrorDescription(describeError(streamQuery.error, 'Stream status could not be loaded'))
    : null;
  const browserStreamEndpoint = useMemo(
    () => selectBrowserStreamEndpoint(streamQuery.data),
    [streamQuery.data]
  );
  const canonicalBrowserStreamEndpoint = useMemo(
    () => selectBrowserStreamEndpoint(streamQuery.data, 'canonical_model'),
    [streamQuery.data]
  );
  const useLiveFrameStream = liveVisible && shouldUseLiveFrameStream(
    persistedLiveActualState,
    streamQuery.data?.actual_state
  );
  useEdgeViewerLease(
    channelId,
    useLiveFrameStream && channelQuery.data?.placement.target === 'edge_device',
    { maxFps: 5, maxWidth: 1280 }
  );
  const selectedPreviewStreamEndpoint =
    runtimePreviewView === 'model'
      ? canonicalBrowserStreamEndpoint
      : browserStreamEndpoint;
  const useSelectedBrowserStream =
    useLiveFrameStream && selectedPreviewStreamEndpoint !== null;
  const liveMetadataRefetchIntervalMs = useSelectedBrowserStream
    ? 5000
    : useLiveFrameStream
      ? 250
      : hasTriggerLiveState
        ? 3000
        : false;
  const liveViewQuery = useQuery({
    queryKey: [
      'runtime-channel-live-view',
      channelId,
      runtimePreviewView,
      useSelectedBrowserStream ? 'metadata' : 'frame'
    ],
    queryFn: () =>
      api.channels.liveViewLatest(
        channelId,
        useSelectedBrowserStream ? { includeFrame: false } : { includeFrame: true }
      ),
    enabled: liveVisible && (useLiveFrameStream || hasTriggerLiveState),
    refetchInterval: liveVisible ? liveMetadataRefetchIntervalMs : false
  });
  useEffect(() => {
    setOverlayEventState(null);
    const clearExpiryTimer = () => {
      if (overlayExpiryTimerRef.current !== null) {
        window.clearTimeout(overlayExpiryTimerRef.current);
        overlayExpiryTimerRef.current = null;
      }
    };
    clearExpiryTimer();
    if (!useSelectedBrowserStream || typeof EventSource === 'undefined') {
      return undefined;
    }
    const source = new EventSource(api.channels.overlayEventsUrl(channelId), {
      withCredentials: true
    });
    const handleOverlay = (event: MessageEvent<string>) => {
      try {
        const response = JSON.parse(event.data) as RuntimeLiveViewResponse;
        clearExpiryTimer();
        setOverlayEventState(response.state);
        if (response.state.overlay.length) {
          const updatedAt = response.state.updated_at
            ? Date.parse(response.state.updated_at)
            : Number.NaN;
          const ageMs = Number.isFinite(updatedAt) ? Math.max(Date.now() - updatedAt, 0) : 0;
          const remainingMs = overlayStaleAfterMs(response.state) - ageMs;
          if (remainingMs <= 0) {
            setOverlayEventState(null);
          } else {
            overlayExpiryTimerRef.current = window.setTimeout(() => {
              overlayExpiryTimerRef.current = null;
              setOverlayEventState(null);
            }, remainingMs);
          }
        }
      } catch {
        // Keep the last valid overlay until its normal expiry. EventSource
        // reconnects automatically and a malformed event must not cause flicker.
        return;
      }
    };
    const handleClear = () => {
      clearExpiryTimer();
      setOverlayEventState(null);
    };
    source.addEventListener('overlay', handleOverlay as EventListener);
    source.addEventListener('clear', handleClear);
    return () => {
      clearExpiryTimer();
      source.removeEventListener('overlay', handleOverlay as EventListener);
      source.removeEventListener('clear', handleClear);
      source.close();
    };
  }, [channelId, useSelectedBrowserStream]);
  const polledLiveState = liveViewQuery.data?.state ?? null;
  const currentLiveState = mergeRuntimeLiveViewState(
    polledLiveState,
    overlayEventState,
    useLiveFrameStream,
    useSelectedBrowserStream
  );
  const boardGeometryWarning = currentLiveState?.overlay.find(
    (primitive) => primitive.layer === 'board_geometry_warning'
  );
  const boardGeometryWarningReason = String(
    boardGeometryWarning?.metadata.reason_code ?? ''
  ).trim();
  useEffect(() => {
    if (!boardGeometryWarning || !boardGeometryWarningReason) {
      activeBoardGeometryWarningRef.current = null;
      return;
    }
    const warningNodeKey = String(boardGeometryWarning.metadata.node_key ?? 'unknown-node');
    const signature = `${channelId}:${warningNodeKey}:${boardGeometryWarningReason}`;
    if (activeBoardGeometryWarningRef.current === signature) return;
    activeBoardGeometryWarningRef.current = signature;
    const now = Date.now();
    const configuredCooldown = Number(
      boardGeometryWarning.metadata.notification_cooldown_ms ?? 3000
    );
    const cooldown = Number.isFinite(configuredCooldown)
      ? Math.max(configuredCooldown, 3000)
      : 3000;
    const lastShownAt = boardGeometryWarningShownAtRef.current.get(signature) ?? 0;
    if (now - lastShownAt < cooldown) return;
    boardGeometryWarningShownAtRef.current.set(signature, now);
    notify({
      title: 'Board geometry temporarily invalid',
      description: boardGeometryWarning.label ?? boardGeometryWarningReason,
      tone: 'error'
    });
  }, [boardGeometryWarning, boardGeometryWarningReason, channelId, notify]);
  const liveLayerKey = currentLiveState?.layers_available.join('|') ?? '';
  const canonicalManualViewProfileId =
    typeof currentLiveState?.frame?.metadata.view_profile_id === 'string'
      ? currentLiveState.frame.metadata.view_profile_id
      : null;
  const canonicalManualEligibility = currentLiveState?.overlay.find(
    (primitive) => primitive.layer === 'stem_capture_eligibility'
  );
  const canonicalManualEligibilityStatus =
    typeof canonicalManualEligibility?.metadata.status === 'string'
      ? canonicalManualEligibility.metadata.status
      : null;
  useEffect(() => {
    if (
      canonicalManualViewProfileId &&
      autoSelectedCanonicalCaptureProfileRef.current !== canonicalManualViewProfileId
    ) {
      autoSelectedCanonicalCaptureProfileRef.current = canonicalManualViewProfileId;
      setManualCaptureSource('PIPELINE_IMAGE_OUTPUT');
    }
  }, [canonicalManualViewProfileId]);

  const manualCaptureTargetsQuery = useQuery({
    queryKey: ['runtime-channel-manual-capture-targets', channelId],
    queryFn: () => api.channels.manualCaptureTargets(channelId),
    refetchInterval: 30000
  });
  const labelProjectsQuery = useQuery({
    queryKey: ['label-projects', 'channel-authoring'],
    queryFn: () => api.datasets.projects(),
    staleTime: 30000
  });

  const manualCaptureSessionQuery = useQuery({
    queryKey: ['runtime-channel-manual-capture-session', channelId],
    queryFn: () => api.channels.manualCaptureSession(channelId),
    refetchInterval: 3000
  });

  const manualCaptureHistoryQuery = useQuery({
    queryKey: ['runtime-channel-manual-capture-history', channelId],
    queryFn: () => api.channels.manualCaptureHistory(channelId, 5),
    refetchInterval: 5000
  });

  const triggerEndpointsQuery = useQuery({
    queryKey: ['runtime-trigger-endpoints', channelId],
    queryFn: () => api.triggerEndpoints.list({ channelId }),
    enabled: Boolean(channelQuery.data),
    refetchInterval: 5000
  });
  const triggerEndpointItems = triggerEndpointsQuery.data?.items ?? [];
  const triggerablePipelineEndpoints = useMemo(() => {
    const currentPipelineVersionId = channelQuery.data?.pipeline_version_id;
    if (!currentPipelineVersionId) {
      return [];
    }
    return triggerEndpointItems.filter(
      (endpoint) => endpoint.pipeline_version_id === currentPipelineVersionId
    );
  }, [channelQuery.data?.pipeline_version_id, triggerEndpointItems]);

  const runtimePipelinesQuery = useQuery({
    queryKey: ['runtime-pipeline-definitions', 'channels', channelId],
    queryFn: () =>
      api.runtimePipelines.list({
        includeTemplates: false,
        scope: 'channel_instance',
        ownerChannelId: channelId
      }),
    staleTime: 30000
  });
  const runtimePipelineDefinitions = runtimePipelinesQuery.data?.items ?? [];
  const currentChannelPipelineDefinition = useMemo(() => {
    const currentVersionId = channelQuery.data?.pipeline_version_id;
    if (currentVersionId) {
      const matchingDefinition = runtimePipelineDefinitions.find((pipeline) =>
        [pipeline.published_version_id, pipeline.latest_version_id].includes(currentVersionId)
      );
      if (matchingDefinition) {
        return matchingDefinition;
      }
    }
    return runtimePipelineDefinitions[0] ?? null;
  }, [channelQuery.data?.pipeline_version_id, runtimePipelineDefinitions]);
  const currentChannelPipelineDetailQuery = useQuery({
    queryKey: ['runtime-pipeline-definition', currentChannelPipelineDefinition?.pipeline_definition_id, 'channel-settings'],
    queryFn: () => api.runtimePipelines.detail(currentChannelPipelineDefinition!.pipeline_definition_id),
    enabled: Boolean(currentChannelPipelineDefinition?.pipeline_definition_id)
  });
  const stemRotationOptimizationEnabled = Boolean(
    currentChannelPipelineDetailQuery.data?.latest_version?.graph.nodes.some(
      (node) => node.node_type === 'analysis.stem_rotation_optimizer'
    )
  );
  const stemRotationHistoryQuery = useQuery({
    queryKey: ['runtime-results', 'stem-rotation-optimization', channelId],
    queryFn: () =>
      api.runtimeResults.list({
        channelId,
        resultType: 'stem_rotation_optimization',
        status: 'completed',
        limit: 1
      }),
    enabled: stemRotationOptimizationEnabled,
    refetchInterval: 5000
  });
  const pipelineVersionOptions = useMemo(
    () =>
      channelPipelineVersionOptions(
        runtimePipelineDefinitions,
        currentChannelPipelineDetailQuery.data ?? null
      ),
    [currentChannelPipelineDetailQuery.data, runtimePipelineDefinitions]
  );
  const selectedPipelineVersionOption = pipelineVersionOptions.find(
    (option) => option.value === form.pipelineVersionId
  );
  const publishedPipelineVersionIds = useMemo(
    () => new Set(pipelineVersionOptions.map((option) => option.value)),
    [pipelineVersionOptions]
  );
  const publishedTriggerPipelineVersionIds = useMemo(
    () =>
      new Set(
        pipelineVersionOptions
          .filter((option) => option.mode === 'trigger')
          .map((option) => option.value)
      ),
    [pipelineVersionOptions]
  );
  const labelProjects = labelProjectsQuery.data?.items ?? [];

  useEffect(() => {
    const channel = channelQuery.data;
    if (!channel || channelFormDirty) {
      return;
    }
    setForm({
      name: channel.name,
      technicalPlace: channel.technical_place,
      facilityId: channel.facility_id ?? '',
      description: channel.description ?? '',
      sourceType: channel.source_type,
      sourceAddress: sourceAddressFromConfig(channel.source_type, channel.source_config),
      testFileName: testFileNameFromConfig(channel.source_config),
      testFileSourceConfig: ['file_test', 'usb_opencv', 'aravis_gige', 'flir_blackfly', 'basler_pylon'].includes(channel.source_type)
        ? channel.source_config
        : {},
      rtspUsername: '',
      rtspPassword: '',
      baslerAcquisitionMode: String(channel.source_config.acquisition_mode ?? 'Continuous'),
      baslerGrabStrategy: String(channel.source_config.grab_strategy ?? 'LatestImageOnly'),
      baslerPixelFormat: String(channel.source_config.pixel_format ?? 'BayerRG8'),
      baslerCameraFps: numericConfigValue(channel.source_config, 'fps', '30'),
      baslerLinkSpeedMbps: numericConfigValue(
        channel.source_config,
        'link_speed_mbps',
        '1000'
      ),
      baslerExposureTimeUs: numericConfigValue(channel.source_config, 'exposure_time_us', ''),
      baslerGainDb: numericConfigValue(channel.source_config, 'gain_db', ''),
      baslerTriggerMode: String(channel.source_config.trigger_mode ?? 'Off'),
      baslerTriggerSource: String(channel.source_config.trigger_source ?? 'Line1'),
      baslerTriggerActivation: String(channel.source_config.trigger_activation ?? 'RisingEdge'),
      baslerPacketSize: numericConfigValue(channel.source_config, 'packet_size', ''),
      baslerInterPacketDelay: numericConfigValue(channel.source_config, 'inter_packet_delay', ''),
      baslerBufferCount: numericConfigValue(channel.source_config, 'buffer_count', '16'),
      baslerOutputQueueSize: numericConfigValue(channel.source_config, 'output_queue_size', ''),
      baslerFrameRetentionMs: numericConfigValue(channel.source_config, 'frame_retention_ms', ''),
      baslerGrabFailureReconnectThreshold: numericConfigValue(
        channel.source_config,
        'grab_failure_reconnect_threshold',
        '10'
      ),
      baslerWidth: numericConfigValue(channel.source_config, 'width', ''),
      baslerHeight: numericConfigValue(channel.source_config, 'height', ''),
      baslerTransportMode: String(channel.source_config.transport_mode ?? 'auto'),
      baslerCameraIp: String(channel.source_config.ip_address ?? ''),
      maxFps: numericConfigValue(channel.source_config, 'max_fps', ''),
      sampleEveryN: numericConfigValue(channel.source_config, 'sample_every_n', '1'),
      pipelineMaxInflight: numericConfigValue(
        channel.source_config,
        'pipeline_max_inflight',
        '2'
      ),
      assignedPipeline: channel.assigned_pipeline ?? '',
      pipelineVersionId:
        channel.pipeline_version_id && publishedPipelineVersionIds.has(channel.pipeline_version_id)
          ? channel.pipeline_version_id
          : '',
      authoringLabelProjectIds: channel.authoring_label_project_ids ?? [],
      captureSummary: channel.capture_summary.summary,
      historyEnabled: channel.result_history_policy.enabled,
      historyRetentionHours: String(channel.result_history_policy.retention_hours),
      historySampleIntervalSeconds: String(
        channel.result_history_policy.sample_interval_seconds
      ),
      historyMaxRecords: String(channel.result_history_policy.max_records),
      executionTarget: channel.placement.target,
      edgeDeviceId: channel.placement.edge_device_id ?? ''
    });
  }, [channelFormDirty, channelQuery.data, publishedPipelineVersionIds]);

  useEffect(() => {
    const targets = manualCaptureTargetsQuery.data?.items ?? [];
    const session = manualCaptureSessionQuery.data?.session;
    const nextSelectedCaptureTargetId = resolveManualCaptureTargetSelection(
      selectedCaptureTargetId,
      targets,
      session
    );
    if (nextSelectedCaptureTargetId !== selectedCaptureTargetId) {
      setSelectedCaptureTargetId(nextSelectedCaptureTargetId);
    }
  }, [
    manualCaptureSessionQuery.data?.session,
    manualCaptureTargetsQuery.data?.items,
    selectedCaptureTargetId
  ]);

  useEffect(() => {
    setKnownLiveLayers([]);
    setOverlayVisibilityChannelId(channelId);
    setVisibleOverlayLayers(readLiveOverlayLayerVisibility(channelId));
  }, [channelId]);

  useEffect(() => {
    if (overlayVisibilityChannelId !== channelId) {
      return;
    }
    writeLiveOverlayLayerVisibility(channelId, visibleOverlayLayers);
  }, [channelId, overlayVisibilityChannelId, visibleOverlayLayers]);

  useEffect(() => {
    const layers = liveLayerKey ? liveLayerKey.split('|') : [];
    if (!layers.length) {
      return;
    }
    setKnownLiveLayers((current) => {
      const next = [...current];
      for (const layer of layers) {
        if (!next.includes(layer)) {
          next.push(layer);
        }
      }
      return next.length === current.length ? current : next;
    });
    setVisibleOverlayLayers((current) => {
      let changed = false;
      const next = { ...current };
      for (const layer of layers) {
        if (!(layer in next)) {
          next[layer] = true;
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, [liveLayerKey]);

  const updateMutation = useMutation({
    mutationFn: (payload: RuntimeChannelUpdateRequest) => api.channels.update(channelId, payload),
    onSuccess: (channel, payload) => {
      setChannelFormDirty(false);
      queryClient.invalidateQueries({ queryKey: ['runtime-channel', channelId] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definitions'] });
      if (payload.placement) {
        queryClient.invalidateQueries({ queryKey: ['edge-devices'] });
      }
      notify({
        title: 'Channel updated',
        description:
          payload.placement?.target === 'edge_device'
            ? `${channel.name}: Edge-Ziel gespeichert. Als Nächstes Preflight und Deployment ausführen.`
            : channel.name,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Channel update failed'), tone: 'error' });
    }
  });
  const stemRotationPriorityMutation = useMutation({
    mutationFn: (payload: RuntimeChannelUpdateRequest) => api.channels.update(channelId, payload),
    onSuccess: (updatedChannel) => {
      queryClient.invalidateQueries({ queryKey: ['runtime-channel', channelId] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      notify({
        title: 'Stamm-Priorität aktualisiert',
        description: `Revision ${updatedChannel.stem_rotation_priority_control.revision}`,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Stamm-Priorität konnte nicht gesetzt werden'), tone: 'error' });
    }
  });
  const testFileUploadMutation = useMutation({
    mutationFn: (file: File) => api.channels.uploadTestFile(file),
    onSuccess: (upload) => {
      updateChannelForm((current) => ({
        ...current,
        sourceType: 'file_test',
        sourceAddress: String(upload.source_config.path ?? upload.path),
        testFileName: upload.file_name,
        testFileSourceConfig: upload.source_config,
        maxFps: current.maxFps,
        sampleEveryN: current.sampleEveryN || '1'
      }));
      notify({ title: 'Test file uploaded', description: upload.file_name, tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Test file upload failed'), tone: 'error' });
    }
  });

  const commandMutation = useMutation({
    mutationFn: (command: 'start' | 'stop' | 'restart') => {
      const currentChannel = channelQuery.data;
      const currentActualState = currentChannel
        ? channelRuntimeActualState(currentChannel)
        : undefined;
      const currentDesiredState =
        currentChannel?.runtime_state?.desired_state ?? currentChannel?.desired_state;
      const force =
        command !== 'start' &&
        (currentActualState === 'stopping' ||
          currentActualState === 'starting' ||
          currentActualState === 'validating' ||
          currentActualState === 'loading_pipeline' ||
          currentDesiredState === 'stop_requested' ||
          currentDesiredState === 'start_requested');
      if (command === 'start') {
        return api.channels.start(channelId);
      }
      if (command === 'stop') {
        return api.channels.stop(channelId, {
          force,
          reason: force ? 'Operator force-stopped a stuck runtime transition.' : undefined
        });
      }
      return api.channels.restart(channelId, {
        force,
        reason: force ? 'Operator force-restarted a stuck runtime transition.' : undefined
      });
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['runtime-channel', channelId] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channel-live-view', channelId] });
      notify({
        title: 'Runtime command accepted',
        description: response.channel.name,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Runtime command failed'), tone: 'error' });
    }
  });

  const streamCommandMutation = useMutation({
    mutationFn: (command: 'start' | 'stop' | 'restart') => {
      if (command === 'start') {
        return api.channels.startStream(channelId);
      }
      if (command === 'stop') {
        return api.channels.stopStream(channelId);
      }
      return api.channels.restartStream(channelId);
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['runtime-channel-streams', channelId] });
      notify({
        title: 'Live-Stream-Befehl angenommen',
        description: `Sollzustand: ${response.desired_state}`,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Live-Stream-Befehl fehlgeschlagen'), tone: 'error' });
    }
  });

  const invalidateManualCapture = () => {
    queryClient.invalidateQueries({ queryKey: ['runtime-channel-manual-capture-session', channelId] });
    queryClient.invalidateQueries({ queryKey: ['runtime-channel-manual-capture-history', channelId] });
    queryClient.invalidateQueries({ queryKey: ['runtime-channel-capture-delivery-history', channelId] });
  };

  const singleCaptureMutation = useMutation({
    mutationFn: () =>
      api.channels.manualCaptureOnce(channelId, {
        capture_target_id: selectedCaptureTargetId,
        capture_source: manualCaptureSource,
        view_profile_id:
          manualCaptureSource === 'PIPELINE_IMAGE_OUTPUT'
            ? canonicalManualViewProfileId
            : null,
        require_eligibility:
          manualCaptureSource === 'PIPELINE_IMAGE_OUTPUT' &&
          canonicalManualEligibilityStatus === 'ACCEPTED',
        request_predictions: manualCapturePredictions
      }),
    onSuccess: (response) => {
      invalidateManualCapture();
      const success = response.result.status === 'success';
      const queued = response.result.status === 'queued';
      setManualCaptureFeedback({
        tone: success || queued ? 'success' : 'error',
        message: success
          ? 'Snapshot captured.'
          : queued
            ? 'Snapshot queued.'
            : response.result.error_summary ?? 'Capture failed.'
      });
      notify({
        title: success ? 'Snapshot captured' : queued ? 'Snapshot queued' : 'Capture failed',
        description: response.result.target_display_name,
        tone: success || queued ? 'success' : 'error'
      });
    },
    onError: (error) => {
      const described = describeError(error, 'Capture failed');
      setManualCaptureFeedback({ tone: 'error', message: described.description ?? described.title });
      notify({ ...described, tone: 'error' });
    }
  });

  const startIntervalMutation = useMutation({
    mutationFn: () =>
      api.channels.startManualCaptureInterval(channelId, {
        capture_target_id: selectedCaptureTargetId,
        capture_source: manualCaptureSource,
        view_profile_id:
          manualCaptureSource === 'PIPELINE_IMAGE_OUTPUT'
            ? canonicalManualViewProfileId
            : null,
        require_eligibility:
          manualCaptureSource === 'PIPELINE_IMAGE_OUTPUT' &&
          canonicalManualEligibilityStatus === 'ACCEPTED',
        interval_seconds: normalizeManualCaptureInterval(manualIntervalSeconds),
        request_predictions: manualCapturePredictions
      }),
    onSuccess: (response) => {
      invalidateManualCapture();
      setManualCaptureFeedback({
        tone: 'success',
        message: `Interval capture running every ${response.session.interval_seconds ?? manualIntervalSeconds} seconds.`
      });
      notify({
        title: 'Interval capture started',
        description: response.session.target_display_name ?? 'Manual live capture',
        tone: 'success'
      });
    },
    onError: (error) => {
      const described = describeError(error, 'Interval capture failed');
      setManualCaptureFeedback({ tone: 'error', message: described.description ?? described.title });
      notify({ ...described, tone: 'error' });
    }
  });

  const stopIntervalMutation = useMutation({
    mutationFn: () => api.channels.stopManualCaptureInterval(channelId, { reason: 'Stopped from live view' }),
    onSuccess: (response) => {
      invalidateManualCapture();
      setManualCaptureFeedback({
        tone: 'success',
        message: `Interval capture stopped after ${response.session.capture_count} capture${response.session.capture_count === 1 ? '' : 's'}.`
      });
      notify({
        title: 'Interval capture stopped',
        description: response.session.target_display_name ?? 'Manual live capture',
        tone: 'success'
      });
    },
    onError: (error) => {
      const described = describeError(error, 'Stop capture failed');
      setManualCaptureFeedback({ tone: 'error', message: described.description ?? described.title });
      notify({ ...described, tone: 'error' });
    }
  });

  const deleteMutation = useMutation({
    mutationFn: () => api.channels.delete(channelId, 'Removed from operator UI'),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      notify({
        title: 'Channel deleted',
        description: 'The channel was removed from the active list.',
        tone: 'success'
      });
      navigate({ to: '/channels' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Channel delete failed'), tone: 'error' });
    }
  });

  const channel = channelQuery.data;
  if (channelQuery.isError) {
    const channelError = describeError(channelQuery.error, 'Channel detail could not be loaded');
    return (
      <div className="panel-stack panel-stack--wide">
        <Panel title="Channel Detail">
          <div className="inline-alert inline-alert--danger">
            <strong>{channelError.title}</strong>
            {channelError.description ? <p>{channelError.description}</p> : null}
            <button type="button" onClick={() => channelQuery.refetch()}>
              Retry
            </button>
          </div>
          <Link to="/channels" className="button-link">
            Back to channels
          </Link>
        </Panel>
      </div>
    );
  }
  if (!channel) {
    return (
      <div className="panel-stack panel-stack--wide">
        <Panel title="Channel Detail">
          <p className="subtle-text">Loading channel...</p>
        </Panel>
      </div>
    );
  }

  const tone = channelStatusTone(channel);
  const persistedActualState = channelRuntimeActualState(channel);
  const actualState = effectiveRuntimeActualState(persistedActualState, currentLiveState);
  const liveReady = actualState === 'running' || actualState === 'degraded';
  const triggerablePipelineCount = Math.max(triggerablePipelineEndpoints.length, channel.triggerable_pipelines.length);
  const liveAvailable =
    liveReady ||
    (currentLiveState?.available === true && !currentLiveState.stale) ||
    triggerablePipelineCount > 0;
  const liveState = liveAvailable ? currentLiveState : null;
  const runtimeLiveLayers = knownLiveLayers.length
    ? knownLiveLayers
    : liveState?.layers_available ?? [];
  const liveLayers = cropOverlayPrimitive(liveState) && !runtimeLiveLayers.includes('pipeline_crop')
    ? [...runtimeLiveLayers, 'pipeline_crop']
    : runtimeLiveLayers;
  const channelVisibleOverlayLayers =
    overlayVisibilityChannelId === channelId ? visibleOverlayLayers : {};
  const assignedPipelineIsTriggerOnly = Boolean(
    channel.pipeline_version_id &&
      (publishedTriggerPipelineVersionIds.has(channel.pipeline_version_id) ||
        [...channel.triggerable_pipelines, ...triggerablePipelineEndpoints].some(
          (endpoint) => endpoint.pipeline_version_id === channel.pipeline_version_id
        ))
  );
  const showingTriggerFrame = isTriggerLiveViewState(liveState);
  const statusPillValue = assignedPipelineIsTriggerOnly && liveReady
    ? showingTriggerFrame
      ? 'Latest trigger frame'
      : 'Trigger endpoint ready'
    : channelStatusLabel(channel);
  const statusPillTone = assignedPipelineIsTriggerOnly && liveReady ? 'success' : tone;
  const runtimeActions = runtimeActionAvailability(channel, {
    busy: commandMutation.isPending,
    triggerOnly: assignedPipelineIsTriggerOnly
  });
  const desiredState = channel.runtime_state?.desired_state ?? channel.desired_state;
  const placementEditable = channelPlacementEditable(channel);
  const placementUpdate = buildChannelPlacementUpdate(
    form.executionTarget,
    form.edgeDeviceId,
    channel.placement
  );
  const placementChanged = placementUpdate !== undefined;
  const placementSelectionValid =
    form.executionTarget === 'server' || Boolean(form.edgeDeviceId.trim());
  const enabledEdgeDevices = edgeDevicesQuery.data?.items ?? [];
  const currentEdgeDeviceMissing = Boolean(
    channel.placement.edge_device_id &&
      !enabledEdgeDevices.some(
        (device) => device.edge_device_id === channel.placement.edge_device_id
      )
  );
  const runtimeRecoveryRequired =
    ['validating', 'loading_pipeline', 'starting', 'stopping'].includes(persistedActualState) ||
    desiredState === 'start_requested' ||
    desiredState === 'stop_requested';
  const showSourceTuning =
    form.sourceType === 'rtsp' ||
    form.sourceType === 'usb_opencv' ||
    form.sourceType === 'aravis_gige' ||
    form.sourceType === 'flir_blackfly' ||
    form.sourceType === 'basler_pylon' ||
    form.sourceType === 'file_test';
  const canSaveChannel =
    sourceAddressValid(form.sourceType, form.sourceAddress) &&
    (form.sourceType !== 'basler_pylon' || validBaslerCameraIp(form.baslerCameraIp)) &&
    placementSelectionValid &&
    (!placementChanged || placementEditable) &&
    !testFileUploadMutation.isPending;
  const manualCapturePanel = (
    <ManualLiveCapturePanel
      compact
      targets={manualCaptureTargetsQuery.data?.items ?? []}
      targetsLoading={manualCaptureTargetsQuery.isLoading}
      selectedTargetId={selectedCaptureTargetId}
      onSelectTarget={setSelectedCaptureTargetId}
      intervalSeconds={manualIntervalSeconds}
      onIntervalSecondsChange={setManualIntervalSeconds}
      requestPredictions={manualCapturePredictions}
      onRequestPredictionsChange={setManualCapturePredictions}
      captureSource={manualCaptureSource}
      onCaptureSourceChange={setManualCaptureSource}
      viewProfileId={canonicalManualViewProfileId}
      sourceFrame={currentLiveState?.undistorted_frame ?? currentLiveState?.source_frame ?? null}
      modelFrame={currentLiveState?.frame ?? null}
      eligibilityStatus={canonicalManualEligibilityStatus}
      eligibilityReason={canonicalManualEligibility?.label ?? null}
      session={manualCaptureSessionQuery.data?.session}
      lastResult={
        manualCaptureSessionQuery.data?.session.last_result ??
        manualCaptureHistoryQuery.data?.items[0] ??
        null
      }
      feedback={manualCaptureFeedback}
      onCaptureOnce={() => singleCaptureMutation.mutate()}
      onStartInterval={() => startIntervalMutation.mutate()}
      onStopInterval={() => stopIntervalMutation.mutate()}
      busy={
        singleCaptureMutation.isPending ||
        startIntervalMutation.isPending ||
        stopIntervalMutation.isPending
      }
    />
  );
  const stemRuntimeResultEnabled = Boolean(
    currentChannelPipelineDetailQuery.data?.latest_version?.graph.nodes.some(
      (node) => node.node_type === 'analysis.stem_curvature_phase_solver'
    )
  );
  const liveStemRotationView = liveState?.stem_rotation_view ?? null;
  const historicalStemRotationResult = stemRotationHistoryQuery.data?.items[0] ?? null;
  const showHistoricalStemRotationResult = !liveReady && !liveStemRotationView;
  const stemRotationView =
    liveStemRotationView ??
    (showHistoricalStemRotationResult
      ? stemRotationViewModelFromResult(historicalStemRotationResult)
      : null);

  return (
    <div className="panel-stack panel-stack--wide channel-detail-page">
      <div className="ws-object-bar"><Link to="/channels" className="ws-back-button" aria-label="Zurück zu Kanälen"><Icon name="back" size={16}/></Link><div><strong>{channel.name}</strong><small>{channel.technical_place || 'Nicht zugeordnet'}</small></div><span className={`ws-badge ws-badge--${statusPillTone === 'success' ? 'ok' : statusPillTone === 'danger' ? 'bad' : 'muted'}`}><i/>{statusPillValue}</span><span className="ws-spacer"/><span className="ws-muted">{channel.placement.target === 'edge_device' ? `Edge · ${channel.placement.edge_device_name ?? channel.placement.edge_management_ip ?? 'Gerät'}` : 'Zentraler Server'}</span></div>
      <div className="workspace-channel-tabs">      <Tabs tabs={['Betrieb', 'Pipeline', 'Ergebnisverlauf', 'Konfiguration']} value={workspaceTab} onChange={tab => {setWorkspaceTab(tab); if(tab === 'Pipeline') setPipelineOpened(true);}}/><span hidden={workspaceTab !== 'Betrieb'}><ToolButton icon="panel" label="Inspektor ein-/ausblenden" active={inspectorOpen} onClick={() => setInspectorOpen(v => !v)}/></span></div>
      <div className="workspace-tab-body workspace-results-tab" hidden={workspaceTab !== 'Ergebnisverlauf'}>{workspaceTab === 'Ergebnisverlauf' && <ChannelResultsWorkspace key={channelId} channelId={channelId} historyEnabled={channel.result_history_policy.enabled} onConfigure={() => {setConfigSection('Datenerfassung & Historie'); setWorkspaceTab('Konfiguration');}}/>}</div>
      <div className="channel-operation-tab" hidden={workspaceTab !== 'Betrieb'}>
      {workspaceTab === 'Betrieb' && channel.placement.target === 'edge_device' && channel.placement.edge_device_id && <details className="edge-performance-details"><summary>Edge-Leistung und TensorRT-Cache</summary><ChannelEdgePerformance edgeDeviceId={channel.placement.edge_device_id} channelId={channelId} /></details>}

      <div className={inspectorOpen ? "channel-live-workspace" : "channel-live-workspace channel-live-workspace--expanded"}>
        <div className="channel-live-workspace__main">
          {liveVisible && <LiveViewPanel
            liveState={liveState}
            actualState={actualState}
            previewView={runtimePreviewView}
            onPreviewViewChange={setRuntimePreviewView}
            liveLayers={liveLayers}
            visibleOverlayLayers={channelVisibleOverlayLayers}
            onVisibleOverlayLayersChange={setVisibleOverlayLayers}
            channel={channel}
            browserStreamEndpoint={browserStreamEndpoint}
            canonicalBrowserStreamEndpoint={canonicalBrowserStreamEndpoint}
            streamStatus={streamQuery.data ?? null}
            streamStatusError={streamStatusError}
            streamStatusPending={streamQuery.isPending}
            stemRuntimeResultEnabled={stemRuntimeResultEnabled}
          />}
        </div>
        <aside hidden={!inspectorOpen} className="channel-live-workspace__tools ws-live-inspector ws-scroll"><div className="ws-inspector-title">Betrieb & Ergebnisse</div>
          <section className="workspace-live-results" aria-label="Aktuelle Messwerte">
            <h3>Aktuelles Ergebnis</h3>
            <small>{liveState?.stale ? 'Veraltet' : liveState?.available ? 'Live' : 'Wartet auf Daten'}</small>
            {allValueOverlayPrimitives(liveState).length ? (
              <ul>{allValueOverlayPrimitives(liveState).map((primitive, index) => (
                <li key={`${primitive.layer}-${index}`}>{primitive.label ?? overlayLayerLabel(primitive.layer)}</li>
              ))}</ul>
            ) : <p className="subtle-text">Noch keine Messwerte verfügbar.</p>}
            <button onClick={() => setWorkspaceTab('Ergebnisverlauf')}><Icon name="results" size={14}/>Gespeicherte Ergebnisse ansehen</button>
          </section>
      {stemRotationOptimizationEnabled ? (
        <StemRotationOptimizationView
          control={channel.stem_rotation_priority_control}
          viewModel={stemRotationView}
          historical={showHistoricalStemRotationResult && stemRotationView !== null}
          historicalCompletedAt={historicalStemRotationResult?.completed_at}
          busy={stemRotationPriorityMutation.isPending}
          onModeChange={(priorityMode) =>
            stemRotationPriorityMutation.mutate({
              stem_rotation_priority_mode: priorityMode
            })
          }
          onOptimizationEnableChange={(optimizationEnable) =>
            stemRotationPriorityMutation.mutate({
              stem_rotation_optimization_enable: optimizationEnable
            })
          }
        />
      ) : null}

          <section className="channel-runtime-controls" aria-live="polite">
            <div className="channel-runtime-controls__heading">
              <div>
                <span>Runtime</span>
                <strong>{runtimeObservedStateLabel(actualState)}</strong>
              </div>
              <span className={`channel-status-dot channel-status-dot--${tone}`} />
            </div>
            <div className="channel-runtime-controls__actions">
              <button
                type="button"
                className="button--primary"
                onClick={() => commandMutation.mutate('start')}
                disabled={!runtimeActions.start.enabled}
                title={runtimeActions.start.reason}
              >
                Starten
              </button>
              <button
                type="button"
                onClick={() => commandMutation.mutate('stop')}
                disabled={!runtimeActions.stop.enabled}
                title={runtimeActions.stop.reason}
              >
                {runtimeRecoveryRequired ? 'Hart stoppen' : 'Stoppen'}
              </button>
              <button
                type="button"
                onClick={() => commandMutation.mutate('restart')}
                disabled={!runtimeActions.restart.enabled}
                title={runtimeActions.restart.reason}
              >
                {runtimeRecoveryRequired ? 'Hart neu starten' : 'Neustarten'}
              </button>
            </div>
            <small className="subtle-text">
              Soll: {runtimeDesiredStateLabel(desiredState)} · Ist: {runtimeObservedStateLabel(actualState)}
            </small>
          </section>

          <section className="channel-runtime-controls" aria-live="polite">
            <div className="channel-runtime-controls__heading">
              <div>
                <span>Live-Stream</span>
                <strong>{streamQuery.data?.actual_state ?? 'stopped'}</strong>
              </div>
              <span
                className={`channel-status-dot channel-status-dot--${
                  streamQuery.data?.actual_state === 'ready'
                    ? 'success'
                    : streamQuery.data?.actual_state === 'failed'
                      ? 'danger'
                      : 'neutral'
                }`}
              />
            </div>
            <div className="channel-runtime-controls__actions">
              <button
                type="button"
                className="button--primary"
                onClick={() => streamCommandMutation.mutate('start')}
                disabled={
                  streamCommandMutation.isPending ||
                  streamQuery.data?.desired_state === 'running'
                }
              >
                Starten
              </button>
              <button
                type="button"
                onClick={() => streamCommandMutation.mutate('stop')}
                disabled={
                  streamCommandMutation.isPending ||
                  streamQuery.data?.desired_state !== 'running'
                }
              >
                Stoppen
              </button>
              <button
                type="button"
                onClick={() => streamCommandMutation.mutate('restart')}
                disabled={
                  streamCommandMutation.isPending ||
                  streamQuery.data?.desired_state !== 'running'
                }
              >
                Neustarten
              </button>
            </div>
            <small className="subtle-text">
              Kamera und Stream laufen unabhängig von der Inferenz-Runtime.
            </small>
          </section>


          <details className="workspace-capture-section">
            <summary><strong>Aufnahmen</strong><span className={`ws-badge ws-badge--${manualCaptureSessionQuery.data?.session.state === 'running' ? 'ok' : manualCaptureSessionQuery.data?.session.state === 'failed' ? 'bad' : 'muted'}`}><i/>{({idle:'Bereit',running:'Serie läuft',stopped:'Gestoppt',failed:'Fehler'})[manualCaptureSessionQuery.data?.session.state ?? 'idle']}</span></summary>
            {manualCapturePanel}
          </details>
        </aside>
      </div>

      </div><div className="workspace-tab-body workspace-config-tab" hidden={workspaceTab !== 'Konfiguration'}><div className="ws-config-workspace"><nav className="ws-subnav" aria-label="Kanalkonfiguration">{['Allgemein & Platz', 'Quelle', 'Ausführung', 'Datenerfassung & Historie', 'Schnittstellen'].map(section => <button key={section} className={configSection === section ? 'is-selected' : ''} onClick={() => setConfigSection(section)}>{section}</button>)}</nav><div className="ws-config-form ws-scroll"><header><h2>{configSection}</h2></header>
          <form
            className="workspace-channel-form"
            onSubmit={(event) => {
              event.preventDefault();
              if (!canSaveChannel) {
                return;
              }
              updateMutation.mutate(buildUpdatePayload(form, channel.placement));
            }}
          >
            <div className="workspace-config-fields" hidden={configSection !== 'Allgemein & Platz'}>
            <label>
              <span>Name</span>
              <input value={form.name} onChange={(event) => updateChannelForm({ ...form, name: event.target.value })} />
            </label>
            <FacilitySelect value={form.facilityId} onChange={(id, name) => updateChannelForm({...form, facilityId: id, technicalPlace: name || form.technicalPlace})}/>
            <label>
              <span>Ortsbezeichnung</span>
              <input
                disabled={Boolean(form.facilityId)} value={form.technicalPlace}
                onChange={(event) => updateChannelForm({ ...form, technicalPlace: event.target.value })}
              />
            </label>
            <label className="channel-form-grid__wide">
              <span>Beschreibung</span>
              <textarea
                value={form.description}
                onChange={(event) => updateChannelForm({ ...form, description: event.target.value })}
              />
            </label>
            </div><div className="workspace-config-fields" hidden={configSection !== 'Quelle'}>
            <label>
              <span>Quellentyp</span>
              <select
                value={form.sourceType}
                onChange={(event) =>
                  updateChannelForm(resetSourceSelection(form, event.target.value as RuntimeSourceType))
                }
              >
                {sourceTypes.map((sourceType) => (
                  <option key={sourceType} value={sourceType}>
                    {sourceTypeLabel(sourceType)}
                  </option>
                ))}
              </select>
            </label>
            {form.sourceType === 'file_test' ? (
              <label className="channel-form-grid__wide test-file-picker">
                <span>Test file</span>
                <input
                  type="file"
                  accept="image/*,video/*"
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) {
                      testFileUploadMutation.mutate(file);
                    }
                    event.target.value = '';
                  }}
                />
                <small className="subtle-text">
                  {testFileUploadMutation.isPending
                    ? 'Uploading test file...'
                    : form.testFileName
                      ? form.testFileName
                      : 'Choose an image or short video for pipeline tests.'}
                </small>
              </label>
            ) : (
              <label>
                <span>
                  {form.sourceType === 'rtsp'
                    ? 'Stream URL'
                    : form.sourceType === 'usb_opencv'
                      ? 'USB device index or path'
                      : 'Source address'}
                </span>
                <input
                  value={form.sourceAddress}
                  onChange={(event) => updateChannelForm({ ...form, sourceAddress: event.target.value })}
                  placeholder={sourcePlaceholder(form.sourceType)}
                />
                {form.sourceType === 'usb_opencv' ? (
                  <small className="subtle-text">
                    Use an index such as 0 on Windows, or /dev/video0 bzw. a stable
                    /dev/v4l/by-id path on Linux/Jetson.
                  </small>
                ) : null}
              </label>
            )}
            {form.sourceType === 'rtsp' ? (
              <>
                <label>
                  <span>RTSP username</span>
                  <input
                    value={form.rtspUsername}
                    onChange={(event) => updateChannelForm({ ...form, rtspUsername: event.target.value })}
                    autoComplete="off"
                  />
                </label>
                <label>
                  <span>RTSP password</span>
                  <input
                    type="password"
                    value={form.rtspPassword}
                    onChange={(event) => updateChannelForm({ ...form, rtspPassword: event.target.value })}
                    autoComplete="new-password"
                    placeholder="Leave blank to keep current secret"
                  />
                </label>
              </>
            ) : null}
            {form.sourceType === 'basler_pylon' ? (
              <>
                <BaslerCameraIpField
                  value={form.baslerCameraIp}
                  onChange={(baslerCameraIp) => updateChannelForm({ ...form, baslerCameraIp })}
                />
                <label>
                  <span>Transport mode</span>
                  <select
                    value={form.baslerTransportMode}
                    onChange={(event) => updateChannelForm({ ...form, baslerTransportMode: event.target.value })}
                  >
                    {baslerTransportModes.map((mode) => (
                      <option key={mode.value} value={mode.value}>
                        {mode.label}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Acquisition mode</span>
                  <select
                    value={form.baslerAcquisitionMode}
                    onChange={(event) => updateChannelForm({ ...form, baslerAcquisitionMode: event.target.value })}
                  >
                    {baslerAcquisitionModes.map((mode) => (
                      <option key={mode} value={mode}>
                        {mode}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Grab strategy</span>
                  <select
                    value={form.baslerGrabStrategy}
                    onChange={(event) => updateChannelForm({ ...form, baslerGrabStrategy: event.target.value })}
                  >
                    {baslerGrabStrategies.map((strategy) => (
                      <option key={strategy} value={strategy}>
                        {strategy}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Pixel format</span>
                  <select
                    value={form.baslerPixelFormat}
                    onChange={(event) => updateChannelForm({ ...form, baslerPixelFormat: event.target.value })}
                  >
                    {baslerPixelFormats.map((format) => (
                      <option key={format} value={format}>
                        {format}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Camera FPS</span>
                  <input
                    type="number"
                    min="0.1"
                    max="240"
                    step="0.1"
                    value={form.baslerCameraFps}
                    onChange={(event) => updateChannelForm({ ...form, baslerCameraFps: event.target.value })}
                  />
                </label>
                <label>
                  <span>Camera link speed (Mbit/s)</span>
                  <input
                    type="number"
                    min="100"
                    max="100000"
                    step="100"
                    value={form.baslerLinkSpeedMbps}
                    onChange={(event) =>
                      updateChannelForm({ ...form, baslerLinkSpeedMbps: event.target.value })
                    }
                  />
                </label>
                <label>
                  <span>Exposure time (us)</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerExposureTimeUs}
                    onChange={(event) => updateChannelForm({ ...form, baslerExposureTimeUs: event.target.value })}
                  />
                </label>
                <label>
                  <span>Gain (dB)</span>
                  <input
                    type="number"
                    min="0"
                    step="0.1"
                    value={form.baslerGainDb}
                    onChange={(event) => updateChannelForm({ ...form, baslerGainDb: event.target.value })}
                  />
                </label>
                <label>
                  <span>Trigger mode</span>
                  <select
                    value={form.baslerTriggerMode}
                    onChange={(event) => updateChannelForm({ ...form, baslerTriggerMode: event.target.value })}
                  >
                    {baslerTriggerModes.map((mode) => (
                      <option key={mode} value={mode}>
                        {mode}
                      </option>
                    ))}
                  </select>
                </label>
                {form.baslerTriggerMode === 'On' ? (
                  <>
                    <label>
                      <span>Trigger source</span>
                      <select
                        value={form.baslerTriggerSource}
                        onChange={(event) => updateChannelForm({ ...form, baslerTriggerSource: event.target.value })}
                      >
                        {baslerTriggerSources.map((source) => (
                          <option key={source} value={source}>
                            {source}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>Trigger activation</span>
                      <select
                        value={form.baslerTriggerActivation}
                        onChange={(event) =>
                          updateChannelForm({ ...form, baslerTriggerActivation: event.target.value })
                        }
                      >
                        {baslerTriggerActivations.map((activation) => (
                          <option key={activation} value={activation}>
                            {activation}
                          </option>
                        ))}
                      </select>
                    </label>
                  </>
                ) : null}
                <label>
                  <span>Packet size</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerPacketSize}
                    onChange={(event) => updateChannelForm({ ...form, baslerPacketSize: event.target.value })}
                  />
                </label>
                <label>
                  <span>Inter-packet delay</span>
                  <input
                    type="number"
                    min="0"
                    step="1"
                    value={form.baslerInterPacketDelay}
                    onChange={(event) => updateChannelForm({ ...form, baslerInterPacketDelay: event.target.value })}
                  />
                </label>
                <label>
                  <span>Grab buffers</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerBufferCount}
                    onChange={(event) => updateChannelForm({ ...form, baslerBufferCount: event.target.value })}
                  />
                </label>
                <label>
                  <span>Output queue size</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerOutputQueueSize}
                    onChange={(event) => updateChannelForm({ ...form, baslerOutputQueueSize: event.target.value })}
                  />
                </label>
                <label>
                  <span>Frame retention (ms)</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerFrameRetentionMs}
                    onChange={(event) => updateChannelForm({ ...form, baslerFrameRetentionMs: event.target.value })}
                  />
                </label>
                <label>
                  <span>Reconnect after failed grabs</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerGrabFailureReconnectThreshold}
                    onChange={(event) =>
                      updateChannelForm({ ...form, baslerGrabFailureReconnectThreshold: event.target.value })
                    }
                  />
                </label>
                <label>
                  <span>Width</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerWidth}
                    onChange={(event) => updateChannelForm({ ...form, baslerWidth: event.target.value })}
                  />
                </label>
                <label>
                  <span>Height</span>
                  <input
                    type="number"
                    min="1"
                    step="1"
                    value={form.baslerHeight}
                    onChange={(event) => updateChannelForm({ ...form, baslerHeight: event.target.value })}
                  />
                </label>
                {baslerConfigurationWarning(form) ? (
                  <div className="channel-form-grid__wide inline-alert inline-alert--warn">
                    {baslerConfigurationWarning(form)}
                  </div>
                ) : null}
              </>
            ) : null}
            </div><div className="workspace-config-fields" hidden={configSection !== 'Ausführung'}>
              <label>
                <span>Ausführungsort</span>
                <select
                  value={form.executionTarget}
                  disabled={!placementEditable || updateMutation.isPending}
                  onChange={(event) => {
                    const executionTarget = event.target.value as RuntimeChannelPlacement['target'];
                    updateChannelForm({
                      ...form,
                      executionTarget,
                      edgeDeviceId: executionTarget === 'server' ? '' : form.edgeDeviceId
                    });
                  }}
                >
                  <option value="server">Zentraler Server</option>
                  <option value="edge_device">Edge Device</option>
                </select>
                <small className="subtle-text">
                  {placementEditable
                    ? 'Kanal, Quelle und Pipeline bleiben beim Wechsel erhalten.'
                    : 'Stoppe den Kanal zuerst im Tab Betrieb, um den Ausführungsort zu ändern.'}
                </small>
              </label>
              {form.executionTarget === 'edge_device' ? (
                <label>
                  <span>Edge Device</span>
                  <select
                    required
                    value={form.edgeDeviceId}
                    disabled={!placementEditable || updateMutation.isPending}
                    onChange={(event) =>
                      updateChannelForm({ ...form, edgeDeviceId: event.target.value })
                    }
                  >
                    <option value="">Gerät auswählen</option>
                    {currentEdgeDeviceMissing && channel.placement.edge_device_id ? (
                      <option value={channel.placement.edge_device_id}>
                        {channel.placement.edge_device_name ?? 'Aktuelles Edge Device'} /{' '}
                        {channel.placement.edge_management_ip ?? 'IP unbekannt'} / nicht aktiviert
                      </option>
                    ) : null}
                    {enabledEdgeDevices.map((device) => (
                      <option key={device.edge_device_id} value={device.edge_device_id}>
                        {device.name} / {device.address.management_ip} / {device.connectivity_state}
                      </option>
                    ))}
                  </select>
                  <small className="subtle-text">
                    {edgeDevicesQuery.isLoading
                      ? 'Registrierte Edge Devices werden geladen …'
                      : edgeDevicesQuery.isError
                        ? 'Die Liste der Edge Devices konnte nicht geladen werden.'
                        : 'Es werden nur aktivierte, registrierte Edge Devices angeboten.'}
                  </small>
                </label>
              ) : null}
              {!placementEditable ? (
                <div className="channel-form-grid__wide inline-alert inline-alert--warn">
                  Ein laufender Kanal kann nicht verschoben werden. Stoppe ihn vollständig und warte auf
                  den Ist-Zustand „Stopped“.
                </div>
              ) : null}
              {placementChanged && form.executionTarget === 'edge_device' ? (
                <div className="channel-form-grid__wide inline-alert">
                  Nach dem Speichern unter Edge Devices das gewählte Gerät öffnen, im Bereich Deployments
                  den Preflight ausführen und anschließend das geprüfte Release ausrollen.
                </div>
              ) : null}
              {placementChanged && form.executionTarget === 'server' ? (
                <div className="channel-form-grid__wide inline-alert">
                  Nach dem Speichern wird der Kanal wieder zentral ausgeführt. Starte ihn anschließend im
                  Tab Betrieb.
                </div>
              ) : null}
            {showSourceTuning ? (
              <>
                <label>
                  <span>Max processing FPS</span>
                  <input
                    type="number"
                    min="0.1"
                    max="120"
                    step="0.1"
                    value={form.maxFps}
                    onChange={(event) => updateChannelForm({ ...form, maxFps: event.target.value })}
                  />
                </label>
                <label>
                  <span>Sample every N</span>
                  <input
                    type="number"
                    min="1"
                    max="1000"
                    step="1"
                    value={form.sampleEveryN}
                    onChange={(event) => updateChannelForm({ ...form, sampleEveryN: event.target.value })}
                  />
                </label>
                <label>
                  <span>Pipeline max. parallel</span>
                  <input
                    type="number"
                    min="1"
                    max="16"
                    step="1"
                    value={form.pipelineMaxInflight}
                    onChange={(event) =>
                      updateChannelForm({ ...form, pipelineMaxInflight: event.target.value })
                    }
                  />
                </label>
              </>
            ) : null}
            <label>
              <span>Pipeline</span>
              <select
                value={form.pipelineVersionId}
                onChange={(event) => updateChannelForm({ ...form, pipelineVersionId: event.target.value })}
              >
                <option value="">
                  {pipelineVersionOptions.length ? 'No pipeline' : 'Create a channel pipeline from a template first'}
                </option>
                {pipelineVersionOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <small className="subtle-text">
                {selectedPipelineVersionOption?.description ??
                  currentChannelPipelineDefinition?.description ??
                  'Channel-specific pipeline versions appear here after publishing.'}
              </small>
            </label>
            </div><div className="workspace-config-fields" hidden={configSection !== 'Datenerfassung & Historie'}>
            <AuthoringLabelProjectDropdown
              projects={labelProjects}
              selectedIds={form.authoringLabelProjectIds}
              loading={labelProjectsQuery.isLoading}
              onChange={(authoringLabelProjectIds) =>
                updateChannelForm({ ...form, authoringLabelProjectIds })
              }
            />
            <label>
              <span>Capture</span>
              <input
                value={form.captureSummary}
                onChange={(event) => updateChannelForm({ ...form, captureSummary: event.target.value })}
              />
            </label>
            <label>
              <span>Result history</span>
              <input
                type="checkbox"
                checked={form.historyEnabled}
                onChange={(event) =>
                  updateChannelForm({ ...form, historyEnabled: event.target.checked })
                }
              />
              <small>
                Nur bei Bedarf aktivieren. Im Result-Output-Node bestimmen die Felder mit Ziel
                „History“, was übertragen und gespeichert wird. Ohne Historie bleiben
                Edge-Vorschau und HTTP-Antwort aktiv, ohne jedes Ergebnis lokal zu speichern.
                Änderungen für Edge-Kanäle mit einem Kanalneustart ausrollen.
              </small>
            </label>
            {form.historyEnabled ? (
              <>
                <label>
                  <span>Delete after (hours)</span>
                  <input
                    type="number"
                    min="1"
                    max="8760"
                    value={form.historyRetentionHours}
                    onChange={(event) =>
                      updateChannelForm({
                        ...form,
                        historyRetentionHours: event.target.value
                      })
                    }
                  />
                </label>
                <label>
                  <span>Store at most every (seconds)</span>
                  <input
                    type="number"
                    min="1"
                    max="3600"
                    value={form.historySampleIntervalSeconds}
                    onChange={(event) =>
                      updateChannelForm({
                        ...form,
                        historySampleIntervalSeconds: event.target.value
                      })
                    }
                  />
                </label>
                <label>
                  <span>Maximum records</span>
                  <input
                    type="number"
                    min="1"
                    max="1000000"
                    value={form.historyMaxRecords}
                    onChange={(event) =>
                      updateChannelForm({ ...form, historyMaxRecords: event.target.value })
                    }
                  />
                </label>
              </>
            ) : null}
            </div><div hidden={configSection !== 'Schnittstellen'}><ChannelHttpRequestEndpointsPanel endpoints={channel.request_endpoints ?? []}/></div>
            <div className="ws-form-actions"><span className="ws-muted">{channelFormDirty ? 'Ungespeicherte Änderungen' : 'Gespeichert'}</span><span className="ws-spacer"/><button type="button" disabled={!channelFormDirty || updateMutation.isPending} onClick={() => setChannelFormDirty(false)}>Verwerfen</button>
              <button
                type="submit"
                className="button--primary"
                disabled={updateMutation.isPending || !canSaveChannel}
              >
                {updateMutation.isPending ? 'Speichert …' : 'Speichern'}
              </button>
              <button
                type="button"
                className="button--danger"
                onClick={() => setDeleteOpen(true)}
                disabled={deleteMutation.isPending}
              >
                Kanal löschen
              </button>
            </div>
          </form>
        </div></div>
      {deleteOpen && <Modal title="Kanal löschen?" onClose={() => !deleteMutation.isPending && setDeleteOpen(false)}><p>„{channel.name}“ aus der aktiven Kanalliste entfernen?</p><div className="ws-dialog-actions"><button disabled={deleteMutation.isPending} onClick={() => setDeleteOpen(false)}>Abbrechen</button><button className="ws-danger" disabled={deleteMutation.isPending} onClick={() => deleteMutation.mutate()}>Kanal entfernen</button></div></Modal>}
      </div><div className="workspace-tab-body workspace-pipeline-tab" hidden={workspaceTab !== 'Pipeline'}>          {pipelineOpened && <RuntimePipelineDesignerPage active={workspaceTab === 'Pipeline'}
            embeddedChannel={{
              channelId,
              channelName: channel.name,
              pipelineVersionId: channel.pipeline_version_id
            }}
          />}


      </div>
    </div>
  );
}

export function StemRotationPriorityControlPanel(props: {
  control: RuntimeStemRotationPriorityControl;
  busy: boolean;
  onModeChange: (mode: StemRotationPriorityMode) => void;
  onOptimizationEnableChange: (enabled: boolean) => void;
}) {
  const frontendActive = props.control.priority_source === 'FRONTEND';
  const controlsDisabled = props.busy || !frontendActive || !props.control.optimization_enable;
  return (
    <section className="channel-runtime-controls" aria-live="polite">
      <div className="channel-runtime-controls__heading">
        <div>
          <span>Priorisierte Richtung</span>
          <strong>{stemRotationPriorityModeLabel(props.control.priority_mode)}</strong>
        </div>
        <span className="runtime-chip runtime-chip--neutral">
          Rev. {props.control.revision}
        </span>
      </div>
      <small className="subtle-text">
        Quelle: {frontendActive ? 'Frontend (Test/Inbetriebnahme)' : 'SPS (nur Anzeige)'}
      </small>
      <label>
        <input
          type="checkbox"
          checked={props.control.optimization_enable}
          disabled={props.busy || !frontendActive}
          onChange={(event) => props.onOptimizationEnableChange(event.target.checked)}
        />{' '}
        Geometrische Eindrehoptimierung aktiv
      </label>
      <div className="channel-runtime-controls__actions" role="group" aria-label="Prioritätsmodus">
        {(
          [
            ['VORSCHNITT_KRITISCH', 'Vorschnitt / oben-unten'],
            ['NACHSCHNITT_KRITISCH', 'Nachschnitt / links-rechts'],
            ['NONE', 'Keine Priorität']
          ] as const
        ).map(([mode, label]) => (
          <button
            key={mode}
            type="button"
            className={props.control.priority_mode === mode ? 'button--primary' : undefined}
            aria-pressed={props.control.priority_mode === mode}
            disabled={controlsDisabled}
            onClick={() => props.onModeChange(mode)}
          >
            {label}
          </button>
        ))}
      </div>
      {!frontendActive ? (
        <small className="subtle-text">
          Frontend-Eingaben sind bei aktiver SPS-Quelle gesperrt. Ein SPS-Leseadapter ist derzeit
          nicht verbunden.
        </small>
      ) : null}
    </section>
  );
}

function stemRotationPriorityModeLabel(mode: StemRotationPriorityMode) {
  if (mode === 'VORSCHNITT_KRITISCH') {
    return 'Vorschnitt / oben-unten (z)';
  }
  if (mode === 'NACHSCHNITT_KRITISCH') {
    return 'Nachschnitt / links-rechts (y)';
  }
  return 'Keine Priorität';
}

function AuthoringLabelProjectDropdown(props: {
  projects: LabelProjectSummary[];
  selectedIds: string[];
  loading: boolean;
  onChange: (selectedIds: string[]) => void;
}) {
  const selectedProjects = props.projects.filter((project) =>
    props.selectedIds.includes(project.label_project_id)
  );
  const selectedSummary = props.loading
    ? 'Loading projects...'
    : selectedProjects.length
      ? selectedProjects.map((project) => project.title).join(', ')
      : 'No projects selected';
  const toggleProject = (projectId: string, checked: boolean) => {
    props.onChange(
      checked
        ? [...new Set([...props.selectedIds, projectId])]
        : props.selectedIds.filter((item) => item !== projectId)
    );
  };

  return (
    <div className="channel-form-grid__wide channel-checkbox-dropdown">
      <span className="channel-checkbox-dropdown__label">Authoring label projects</span>
      <details className="checkbox-dropdown">
        <summary>
          <span>{selectedSummary}</span>
          <span className={`runtime-chip runtime-chip--${props.selectedIds.length ? 'info' : 'neutral'}`}>
            {props.selectedIds.length ? `${props.selectedIds.length} selected` : 'optional'}
          </span>
        </summary>
        <div className="checkbox-dropdown__body" role="listbox" aria-multiselectable="true">
          {props.projects.length ? (
            props.projects.map((project) => (
              <label key={project.label_project_id} className="checkbox-dropdown__item" role="option">
                <input
                  type="checkbox"
                  checked={props.selectedIds.includes(project.label_project_id)}
                  onChange={(event) => toggleProject(project.label_project_id, event.target.checked)}
                />
                <span>
                  {project.title}
                  <small className="subtle-text">{authoringProjectMeta(project)}</small>
                </span>
              </label>
            ))
          ) : (
            <p className="subtle-text">
              {props.loading ? 'Loading label projects...' : 'No label projects are currently available.'}
            </p>
          )}
        </div>
      </details>
    </div>
  );
}

type LiveFrameViewProps = {
  liveState: RuntimeLiveViewState | null;
  actualState: string;
  previewView?: RuntimePreviewView;
  visibleLayers: Record<string, boolean>;
  browserStreamEndpoint?: RuntimeStreamEndpoint | null;
  canonicalBrowserStreamEndpoint?: RuntimeStreamEndpoint | null;
};

type RuntimePreviewView = 'full' | 'model';

function hydratedLiveFrame(
  eventFrame: RuntimeLiveFrame | null | undefined,
  polledFrame: RuntimeLiveFrame | null | undefined
): RuntimeLiveFrame | null {
  if (eventFrame?.image_base64) return eventFrame;
  if (polledFrame?.image_base64) return polledFrame;
  return eventFrame ?? polledFrame ?? null;
}

export function mergeRuntimeLiveViewState(
  polledState: RuntimeLiveViewState | null,
  overlayEventState: RuntimeLiveViewState | null,
  useLiveFrameStream: boolean,
  useBrowserStream: boolean = useLiveFrameStream
): RuntimeLiveViewState | null {
  // With REST frame fallback the pixels and overlays must come from the same
  // response. Merging a newer SSE overlay into an older hydrated image makes
  // classifications appear on the wrong test photo.
  if (!useBrowserStream) return polledState;
  if (overlayEventState) {
    return {
      ...polledState,
      ...overlayEventState,
      // Overlay SSE events intentionally contain frame metadata without image bytes.
      // Keep the independently hydrated REST frames instead of replacing their pixels.
      frame: hydratedLiveFrame(overlayEventState.frame, polledState?.frame),
      source_frame: hydratedLiveFrame(
        overlayEventState.source_frame,
        polledState?.source_frame
      ),
      undistorted_frame: hydratedLiveFrame(
        overlayEventState.undistorted_frame,
        polledState?.undistorted_frame
      ),
      source_overlay: overlayEventState.source_overlay ?? polledState?.source_overlay ?? [],
      raw_overlay: overlayEventState.raw_overlay ?? polledState?.raw_overlay ?? []
    };
  }
  if (useLiveFrameStream && polledState) {
    return {
      ...polledState,
      overlay: [],
      source_overlay: [],
      raw_overlay: [],
      layers_available: []
    };
  }
  return polledState;
}

export function canonicalCaptureEligibilityPolicy(status: string | null | undefined) {
  return {
    singleCaptureAllowed: status !== 'REJECTED',
    intervalCaptureAllowed: status === 'ACCEPTED',
    requireEligibility: status === 'ACCEPTED'
  };
}

function LiveViewPanel(props: {
  liveState: RuntimeLiveViewState | null;
  actualState: string;
  previewView: RuntimePreviewView;
  onPreviewViewChange: (value: RuntimePreviewView) => void;
  liveLayers: string[];
  visibleOverlayLayers: Record<string, boolean>;
  onVisibleOverlayLayersChange: (
    updater: (current: Record<string, boolean>) => Record<string, boolean>
  ) => void;
  channel: RuntimeChannelSummary;
  browserStreamEndpoint?: RuntimeStreamEndpoint | null;
  canonicalBrowserStreamEndpoint?: RuntimeStreamEndpoint | null;
  streamStatus?: RuntimeChannelStreamListResponse | null;
  streamStatusError?: string | null;
  streamStatusPending?: boolean;
  stemRuntimeResultEnabled?: boolean;
}) {
  const [overlayOpacity, setOverlayOpacity] = useState(1);
  const curvatureDiagnosticGraph = diagnosticGraphFromOverlay(props.liveState?.overlay);
  const modelFrame = props.liveState?.frame ?? null;
  const viewProfileId =
    typeof modelFrame?.metadata.view_profile_id === 'string'
      ? modelFrame.metadata.view_profile_id
      : null;
  const eligibilityPrimitive = props.liveState?.overlay.find(
    (primitive) => primitive.layer === 'stem_capture_eligibility'
  );
  const eligibilityStatus =
    typeof eligibilityPrimitive?.metadata.status === 'string'
      ? eligibilityPrimitive.metadata.status
      : null;
  const rawStream = props.streamStatus?.items.find((item) => item.variant === 'raw') ?? null;
  const canonicalStream =
    props.streamStatus?.items.find((item) => item.variant === 'canonical_model') ?? null;
  const productionStreamReady = Boolean(
    rawStream?.ready && (rawStream.status === 'ready' || rawStream.status === 'degraded')
  );
  const streamAvailabilityLabel = productionStreamReady
    ? 'Production stream ready'
    : props.streamStatusPending
        ? 'Stream status loading'
        : 'Stream unavailable';
  const canonicalStreamReady = Boolean(
    canonicalStream?.ready &&
      (canonicalStream.status === 'ready' || canonicalStream.status === 'degraded')
  );
  const canonicalStreamLabel = canonicalStreamReady
    ? 'Canonical live stream ready'
    : props.streamStatusPending
      ? 'Canonical stream status loading'
      : canonicalStream?.status === 'unavailable'
        ? 'Canonical stream unavailable'
        : 'Canonical stream warming up';
  return (
    <div id="live" className="channel-live-block">
      <Panel title="LiveView">
        <div className="live-view-shell">
          <div className="live-view-toolbar">
            <div className="actions-row" aria-label="Runtime preview image space">
              <button
                type="button"
                className={props.previewView === 'full' ? 'button--primary' : undefined}
                onClick={() => props.onPreviewViewChange('full')}
              >
                <span title="Full camera view + overlays">Kamerabild</span>
              </button>
              <button
                type="button"
                className={props.previewView === 'model' ? 'button--primary' : undefined}
                onClick={() => props.onPreviewViewChange('model')}
              >
                <span title="Model / Label Studio view + overlays">Modell / Label Studio</span>
              </button>
            </div>
            <OverlayControls layers={props.liveLayers} visibility={props.visibleOverlayLayers} onChange={value => props.onVisibleOverlayLayersChange(() => value)} label={overlayLayerLabel} opacity={overlayOpacity} onOpacity={setOverlayOpacity}/>
          </div>

          <LiveViewport opacity={overlayOpacity}><LiveFrameView
            liveState={props.liveState}
            actualState={props.actualState}
            previewView={props.previewView}
            visibleLayers={props.visibleOverlayLayers}
            browserStreamEndpoint={props.browserStreamEndpoint}
            canonicalBrowserStreamEndpoint={props.canonicalBrowserStreamEndpoint}
          /></LiveViewport>

          {props.stemRuntimeResultEnabled || curvatureDiagnosticGraph ? (
            <details className="channel-live-diagnostics"><summary>Krümmung über Rollphase</summary><StemCurvaturePhaseRuntimePanel
              overlay={props.liveState?.overlay}
              title="Live-Diagnose: Krümmung über Rollphase"
              compact
            /></details>
          ) : null}

          <details className="stream-status-disclosure channel-live-diagnostics">
            <summary>
              <span>Stream & Diagnose</span>
              <span className={`runtime-chip runtime-chip--${productionStreamReady ? 'success' : 'warn'}`}>
                {productionStreamReady ? 'bereit' : 'prüfen'}
              </span>
            </summary>
            <div className="stream-status-disclosure__body"><div className="actions-row">            <span className={`runtime-chip runtime-chip--${props.liveState?.source_health.connected ? 'success' : 'warn'}`}>
              {props.liveState?.source_health.status_text ?? 'Live source waiting'}
            </span>
            <span
              className={`runtime-chip runtime-chip--${productionStreamReady ? 'success' : 'danger'}`}
              title={rawStream?.warning ?? undefined}
            >
              {streamAvailabilityLabel}
            </span>
            <span
              className={`runtime-chip runtime-chip--${canonicalStreamReady ? 'success' : 'warn'}`}
              title={canonicalStream?.warning ?? undefined}
            >
              {canonicalStreamLabel}
            </span>
            {viewProfileId ? (
              <span className="runtime-chip runtime-chip--success">
                {viewProfileId} · {modelFrame?.width ?? '?'}×{modelFrame?.height ?? '?'}
              </span>
            ) : null}
            {eligibilityStatus ? (
              <span
                className={`runtime-chip runtime-chip--${eligibilityStatus === 'ACCEPTED' ? 'success' : 'warn'}`}
                title={eligibilityPrimitive?.label ?? undefined}
              >
                Capture {eligibilityStatus.toLowerCase()}
              </span>
            ) : null}
</div>
              <StreamVariantStatusPanel
                streamStatus={props.streamStatus ?? null}
                streamStatusError={props.streamStatusError ?? null}
                streamStatusPending={Boolean(props.streamStatusPending)}
              />

              <div className="detail-grid channel-live-metrics">
                <MetricPill
                  label="View"
                  value={
                    isTriggerLiveViewState(props.liveState)
                      ? 'latest trigger frame'
                      : props.actualState.replaceAll('_', ' ')
                  }
                />
                <MetricPill
                  label="FPS"
                  value={formatMetric(
                    props.liveState?.metrics.processed_fps ??
                      props.channel.performance.processed_fps ??
                      props.channel.performance.fps,
                    'fps'
                  )}
                />
                <MetricPill
                  label="Source FPS"
                  value={formatMetric(props.liveState?.metrics.source_fps, 'fps')}
                />
                <MetricPill
                  label="Frame cap"
                  value={formatFrameCap(props.liveState)}
                />
                <MetricPill
                  label="In-flight"
                  value={formatPipelineInflight(props.liveState)}
                />
                <MetricPill
                  label="Drop rate"
                  value={formatMetric(
                    props.liveState?.metrics.drop_rate_percent ??
                      props.channel.performance.drop_rate_percent,
                    '%'
                  )}
                />
                <MetricPill
                  label="Latency p95"
                  value={formatMetric(
                    props.liveState?.metrics.latency_p95_ms ??
                      props.channel.performance.latency_p95_ms,
                    'ms'
                  )}
                />
                <MetricPill
                  label="Reconnects"
                  value={String(
                    props.liveState?.metrics.reconnect_count ??
                      props.channel.performance.reconnect_count ??
                      0
                  )}
                />
                <MetricPill
                  label={isTriggerLiveViewState(props.liveState) ? 'Last trigger frame' : 'Last frame'}
                  value={formatActivity(
                    props.liveState?.metrics.last_frame_at ?? props.channel.performance.last_frame_at
                  )}
                />
              </div>
              {props.liveState?.recovery_hint ? (
                <div className="inline-alert inline-alert--warn">{props.liveState.recovery_hint}</div>
              ) : null}
              <LiveDiagnosticsPanel liveState={props.liveState} mode="stats" />
              <LiveDiagnosticsPanel liveState={props.liveState} mode="plc" />
            </div>
          </details>
        </div>
      </Panel>
    </div>
  );
}

function LiveDiagnosticsPanel(props: {
  liveState: RuntimeLiveViewState | null;
  mode?: 'all' | 'stats' | 'plc';
}) {
  const mode = props.mode ?? 'all';
  const warnings = props.liveState?.diagnostics?.warnings ?? [];
  const timings = orderedStageTimings(props.liveState?.metrics.stage_timings_ms ?? {});
  const nodeTimings = orderedPipelineNodeTimings(
    props.liveState?.diagnostics?.pipeline_node_timings ?? []
  );
  const inferenceTimings = orderedPipelineInferenceTimings(
    props.liveState?.diagnostics?.pipeline_inference_timings ?? []
  );
  const plcWrites = plcWriteStatuses(props.liveState?.diagnostics?.plc_write_outputs ?? []);
  const showStats = mode === 'all' || mode === 'stats';
  const showPlc = mode === 'all' || mode === 'plc';
  if (
    (!showStats || (!warnings.length && !timings.length && !nodeTimings.length && !inferenceTimings.length)) &&
    (!showPlc || !plcWrites.length)
  ) {
    return null;
  }
  return (
    <div className="live-diagnostics">
      {showStats && warnings.length ? (
        <div className="live-diagnostics__warnings">
          {warnings.slice(0, 4).map((warning) => (
            <span key={warning}>{warning}</span>
          ))}
        </div>
      ) : null}
      {showStats && timings.length ? (
        <dl className="live-diagnostics__timings">
          {timings.map(([key, value]) => (
            <div key={key}>
              <dt>{stageTimingLabel(key)}</dt>
              <dd>{value.toFixed(value >= 100 ? 0 : 1)} ms</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {showStats && nodeTimings.length ? (
        <dl className="live-diagnostics__timings">
          {nodeTimings.map((item) => (
            <div key={`node-${String(item.node_key ?? item.node_type ?? 'unknown')}`}>
              <dt>{pipelineNodeTimingLabel(item)}</dt>
              <dd>{pipelineNodeTimingValue(item)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {showStats && inferenceTimings.length ? (
        <dl className="live-diagnostics__timings">
          {inferenceTimings.map((item) => (
            <div key={`inference-${String(item.node_key ?? item.node_type ?? 'unknown')}`}>
              <dt>{pipelineInferenceTimingLabel(item)}</dt>
              <dd>{pipelineInferenceTimingValue(item)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {showPlc && plcWrites.length ? (
        <div className="live-diagnostics__plc">
          {plcWrites.slice(0, 6).map((item, index) => (
            <div key={`${item.nodeKey}-${item.mappingKey}-${index}`}>
              <span className={`runtime-chip runtime-chip--${plcWriteTone(item.status)}`}>
                {item.status}
              </span>
              <strong>{item.mappingLabel}</strong>
              <span>{item.address} / {item.dataType}</span>
              <small className="subtle-text">
                {item.error || item.reason || `value ${item.mappedValue}`}
              </small>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function StreamVariantStatusPanel(props: {
  streamStatus: RuntimeChannelStreamListResponse | null;
  streamStatusError?: string | null;
  streamStatusPending?: boolean;
}) {
  const [messagesOpen, setMessagesOpen] = useState(false);
  const [variantsOpen, setVariantsOpen] = useState(false);
  const variants = props.streamStatus?.items ?? [];
  const warnings = props.streamStatus?.warnings ?? [];
  const messages = [
    props.streamStatusError ? { tone: 'danger' as const, text: `Stream status unavailable: ${props.streamStatusError}` } : null,
    !props.streamStatusError && props.streamStatusPending
      ? { tone: 'info' as const, text: 'Stream status loading.' }
      : null,
    ...warnings.slice(0, 3).map((warning) => ({ tone: 'warn' as const, text: warning }))
  ].filter((message): message is { tone: 'danger' | 'info' | 'warn'; text: string } => Boolean(message));
  const showEmptyState = !variants.length;
  return (
    <div className="stream-variant-list">
      <details
        className="stream-status-disclosure"
        open={messagesOpen}
        onToggle={(event) => setMessagesOpen(event.currentTarget.open)}
      >
        <summary>
          <span>Stream messages</span>
          <span className={`runtime-chip runtime-chip--${messages.length ? 'warn' : 'success'}`}>
            {messages.length ? `${messages.length} message${messages.length === 1 ? '' : 's'}` : 'clear'}
          </span>
        </summary>
        <div className="stream-status-disclosure__body">
          {messages.length ? (
            messages.map((message) => (
              <div key={`${message.tone}-${message.text}`} className={`inline-alert inline-alert--${message.tone}`}>
                {message.text}
              </div>
            ))
          ) : (
            <div className="inline-alert inline-alert--success">No stream warnings reported.</div>
          )}
        </div>
      </details>

      <details
        className="stream-status-disclosure"
        open={variantsOpen}
        onToggle={(event) => setVariantsOpen(event.currentTarget.open)}
      >
        <summary>
          <span>Stream variants</span>
          <span className={`runtime-chip runtime-chip--${variants.length ? 'success' : 'warn'}`}>
            {variants.length ? `${variants.length} available` : 'none'}
          </span>
        </summary>
        <div className="stream-status-disclosure__body">
          {showEmptyState && !props.streamStatusError && !props.streamStatusPending ? (
            <div className="inline-alert inline-alert--warn">
              No runtime stream variants are currently published for this channel.
            </div>
          ) : null}
          {variants.map((variant) => (
            <article key={variant.variant} className="stream-variant-row">
              <div>
                <strong>{streamVariantLabel(variant)}</strong>
                <span className="subtle-text">{variant.intended_use}</span>
              </div>
              <span className={`runtime-chip runtime-chip--${streamStatusTone(variant)}`}>
                {variant.status.replaceAll('_', ' ')}
              </span>
              <dl>
            <div>
              <dt>Protocol</dt>
              <dd>{streamProtocols(variant)}</dd>
            </div>
            <div>
              <dt>Codec</dt>
              <dd>{variant.codec.toUpperCase()}</dd>
            </div>
            <div>
              <dt>Encoder FPS</dt>
              <dd>{formatMetric(variant.metrics.output_fps ?? variant.fps, 'fps')}</dd>
            </div>
            {variant.metrics.input_fps !== undefined ? (
              <div>
                <dt>Submitted FPS</dt>
                <dd>{formatMetric(variant.metrics.input_fps, 'fps')}</dd>
              </div>
            ) : null}
            <div>
              <dt>Repeated</dt>
              <dd>{variant.metrics.repeated_frames}</dd>
            </div>
            <div>
              <dt>Dropped</dt>
              <dd>{variant.metrics.dropped_frames}</dd>
            </div>
            {variant.metrics.encoder_dropped_frames !== undefined ? (
              <div>
                <dt>Encoder drops</dt>
                <dd>{variant.metrics.encoder_dropped_frames}</dd>
              </div>
            ) : null}
            {variant.metrics.transform_fps !== undefined ? (
              <div>
                <dt>Transform FPS</dt>
                <dd>{formatMetric(variant.metrics.transform_fps, 'fps')}</dd>
              </div>
            ) : null}
            {variant.metrics.transform_dropped_frames !== undefined ? (
              <div>
                <dt>Transform drops</dt>
                <dd>{variant.metrics.transform_dropped_frames}</dd>
              </div>
            ) : null}
            {variant.metrics.processing_latency_ms !== undefined ? (
              <div>
                <dt>Transform latency</dt>
                <dd>{formatMetric(variant.metrics.processing_latency_ms, 'ms')}</dd>
              </div>
            ) : null}
            {variant.metrics.transform_source_frame_age_ms !== undefined ? (
              <div>
                <dt>Transform frame age</dt>
                <dd>{formatMetric(variant.metrics.transform_source_frame_age_ms, 'ms')}</dd>
              </div>
            ) : null}
            <div>
              <dt>Frame age</dt>
              <dd>{formatMetric(variant.metrics.source_frame_age_ms, 'ms')}</dd>
            </div>
            <div>
              <dt>Encoder</dt>
              <dd>{streamEncoderLabel(variant)}</dd>
            </div>
            <div>
              <dt>Clients</dt>
              <dd>{variant.metrics.connected_clients}</dd>
            </div>
            <div>
              <dt>Access</dt>
              <dd>{variant.access_policy.replaceAll('_', ' ')}</dd>
            </div>
              </dl>
              {variant.endpoints.length ? (
                <div className="stream-url-list">
                  {variant.endpoints.slice(0, 3).map((endpoint) => (
                    <StreamUrlRow key={`${variant.variant}-${endpoint.protocol}-${endpoint.url}`} endpoint={endpoint} />
                  ))}
                </div>
              ) : null}
            </article>
          ))}
        </div>
      </details>
    </div>
  );
}

function StreamUrlRow(props: { endpoint: RuntimeStreamEndpoint }) {
  const copyUrl = () => {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      void navigator.clipboard.writeText(props.endpoint.display_url ?? props.endpoint.url);
    }
  };
  return (
    <div className="stream-url-row">
      <span>{props.endpoint.protocol.replaceAll('_', ' ')}</span>
      <code>{props.endpoint.display_url ?? props.endpoint.url}</code>
      <button type="button" onClick={copyUrl}>
        Copy
      </button>
      {props.endpoint.external_client_example ? (
        <code className="stream-url-row__example">{props.endpoint.external_client_example}</code>
      ) : null}
    </div>
  );
}

function CompactRuntimeResultCard(props: { result: RuntimeResultRecordSummary }) {
  const fields = compactResultFields(props.result);
  return (
    <article className="runtime-result-card runtime-result-card--readonly">
      <span className={`runtime-chip runtime-chip--${compactResultTone(props.result.status)}`}>
        {props.result.status.replaceAll('_', ' ')}
      </span>
      <strong>{formatActivity(props.result.completed_at)}</strong>
      <span className="subtle-text">
        {props.result.mode} / {props.result.result_type.replaceAll('_', ' ')}
      </span>
      {fields.length ? (
        <dl className="runtime-result-fields">
          {fields.slice(0, 3).map(([key, value]) => (
            <div key={key}>
              <dt>{key.replaceAll('_', ' ')}</dt>
              <dd>{compactValue(value)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <span className="subtle-text">No final fields recorded</span>
      )}
    </article>
  );
}

export function PipelineResultPanel(props: { results: RuntimeResultRecordSummary[] }) {
  const [latest, ...previous] = props.results;
  return (
    <Panel title="Pipeline Result">
      {latest ? (
        <div className="runtime-result-list runtime-result-list--compact">
          <CompactRuntimeResultCard result={latest} />
          {previous.slice(0, 2).map((result) => (
            <CompactRuntimeResultCard key={result.result_record_id} result={result} />
          ))}
          <a href="/results" className="button-link">
            Open result history
          </a>
        </div>
      ) : (
        <EmptyState title="No result yet" body="The configured Result output has not reported a runtime result." />
      )}
    </Panel>
  );
}

function formatErrorDescription(error: { title: string; description?: string }) {
  return [error.title, error.description].filter(Boolean).join(' ');
}

export function overlayStaleAfterMs(state: RuntimeLiveViewState) {
  const effectiveFps =
    state.diagnostics?.effective_source_limit_fps ?? state.metrics.processed_fps ?? null;
  const cadenceBudgetMs = effectiveFps && effectiveFps > 0 ? 2500 / effectiveFps : 3000;
  return Math.min(Math.max(cadenceBudgetMs, 2000), 15000);
}

export function cropOverlayPrimitive(
  state: RuntimeLiveViewState | null
): RuntimeOverlayPrimitiveState | null {
  const frame = state?.frame ?? null;
  const contexts = [frame?.image_context, state?.display_context].map(objectRecord);
  const cropContext = contexts.find(
    (context) => objectRecord(context.transform).transform_type === 'crop'
  );
  if (!cropContext) {
    return null;
  }
  const transform = objectRecord(cropContext.transform);
  const parameters = objectRecord(transform.parameters);
  const x = Math.max(0, numericValue(parameters.x) ?? 0);
  const y = Math.max(0, numericValue(parameters.y) ?? 0);
  const width = numericValue(parameters.width) ?? frame?.width ?? null;
  const height = numericValue(parameters.height) ?? frame?.height ?? null;
  const sourceWidth =
    numericValue(parameters.source_width) ?? state?.source_frame?.width ?? null;
  const sourceHeight =
    numericValue(parameters.source_height) ?? state?.source_frame?.height ?? null;
  if (
    width === null ||
    height === null ||
    sourceWidth === null ||
    sourceHeight === null ||
    width <= 0 ||
    height <= 0 ||
    sourceWidth <= 0 ||
    sourceHeight <= 0 ||
    x >= sourceWidth ||
    y >= sourceHeight
  ) {
    return null;
  }
  const cropWidth = Math.min(width, sourceWidth - x);
  const cropHeight = Math.min(height, sourceHeight - y);
  const sourceContext = objectRecord(state?.source_frame?.image_context);
  return {
    primitive_type: 'box',
    layer: 'pipeline_crop',
    label: 'Processed crop',
    geometry: {
      x,
      y,
      width: cropWidth,
      height: cropHeight,
      coordinate_system: 'image_pixels'
    },
    image_context_id:
      typeof sourceContext.context_id === 'string' ? sourceContext.context_id : null,
    display_context_id: state?.display_context_id ?? null,
    frame_id: frame ? String(frame.sequence) : null,
    source_timestamp: frame?.source_timestamp ?? null,
    coordinate_system: 'image_pixels',
    resolution: { width: sourceWidth, height: sourceHeight },
    style: {
      stroke: '#2dd4bf',
      fill: 'rgba(45, 212, 191, 0.08)',
      stroke_width: 3
    },
    metadata: {
      transform_type: 'crop',
      crop_x: x,
      crop_y: y,
      crop_width: cropWidth,
      crop_height: cropHeight,
      source_width: sourceWidth,
      source_height: sourceHeight
    }
  };
}

function allValueOverlayPrimitives(
  state: RuntimeLiveViewState | null
): RuntimeOverlayPrimitiveState[] {
  const unique = new Map<string, RuntimeOverlayPrimitiveState>();
  for (const primitive of [
    ...(state?.overlay ?? []),
    ...(state?.source_overlay ?? []),
    ...(state?.raw_overlay ?? [])
  ]) {
    if (hasSpatialGeometry(primitive)) continue;
    const key = JSON.stringify([
      primitive.layer,
      primitive.primitive_type,
      primitive.label ?? null,
      primitive.metadata.value ?? null,
      primitive.metadata.node_key ?? null
    ]);
    if (!unique.has(key)) unique.set(key, primitive);
  }
  return [...unique.values()];
}

export function LiveFrameView({
  liveState,
  actualState,
  previewView = 'full',
  visibleLayers,
  browserStreamEndpoint,
  canonicalBrowserStreamEndpoint
}: LiveFrameViewProps) {
  const frame = liveState?.frame ?? null;
  const sourceFrame = liveState?.source_frame ?? frame;
  const [loadedFrameSize, setLoadedFrameSize] = useState<{ width: number; height: number } | null>(null);
  const [browserPlaybackProtocol, setBrowserPlaybackProtocol] =
    useState<RuntimeStreamEndpoint['protocol'] | null>(null);
  const [browserDecodedFps, setBrowserDecodedFps] = useState<number | null>(null);
  const isTriggerFrame = isTriggerLiveViewState(liveState);
  const selectedFrame = previewView === 'model' ? frame : sourceFrame;
  const resultFrameSrc = selectedFrame?.image_base64
    ? liveImageSource(selectedFrame.image_base64, selectedFrame.mime_type)
    : null;
  const selectedBrowserStreamEndpoint =
    previewView === 'model' ? canonicalBrowserStreamEndpoint : browserStreamEndpoint;
  const browserStream =
    selectedBrowserStreamEndpoint && isBrowserLiveStreamEndpoint(selectedBrowserStreamEndpoint)
      ? selectedBrowserStreamEndpoint
      : null;
  useEffect(() => {
    setLoadedFrameSize(null);
    setBrowserDecodedFps(null);
  }, [browserStream?.url, previewView]);
  const imageSrc = browserStream ? null : resultFrameSrc;
  // WebRTC displays each encoder's native frame. Prefer the dimensions actually
  // decoded by the video element so projected overlays share its exact pixel space.
  const frameWidth = browserStream
    ? loadedFrameSize?.width ?? selectedFrame?.width ?? null
    : selectedFrame?.width ?? loadedFrameSize?.width ?? null;
  const frameHeight = browserStream
    ? loadedFrameSize?.height ?? selectedFrame?.height ?? null
    : selectedFrame?.height ?? loadedFrameSize?.height ?? null;
  const mediaStyle = liveMediaStyle(frameWidth, frameHeight);
  const cropPrimitive = previewView === 'full' ? cropOverlayPrimitive(liveState) : null;
  const selectedOverlay =
    previewView === 'model' ? liveState?.overlay ?? [] : liveState?.raw_overlay ?? [];
  const spatialPrimitives = [
    ...selectedOverlay,
    ...(cropPrimitive ? [cropPrimitive] : [])
  ].filter(
    (primitive) => hasSpatialGeometry(primitive) && (visibleLayers[primitive.layer] ?? true)
  );
  const texturePrimitives = spatialPrimitives.filter(isTextureOverlay);
  const vectorPrimitives = spatialPrimitives.filter((primitive) => !isTextureOverlay(primitive));
  const valuePrimitives = allValueOverlayPrimitives(liveState).filter(
    (primitive) => visibleLayers[primitive.layer] ?? true
  );
  const renderLiveOverlays = true;
  const emptyLabel =
    liveState?.available && !imageSrc
      ? isTriggerFrame
        ? 'Waiting for latest trigger frame'
        : previewView === 'model'
          ? 'Waiting for canonical pipeline model frame'
          : 'Waiting for first live frame'
      : liveState?.runtime_instance_id?.startsWith('trigger-latest-')
        ? 'Invoke the trigger pipeline to publish a control frame'
      : actualState === 'running' || actualState === 'degraded'
        ? 'Live runtime warming up'
        : 'Runtime is not live';

  return (
    <div className={`live-view-frame ${imageSrc || browserStream ? '' : 'live-view-frame--empty'}`}>
      {imageSrc || browserStream ? (
        <div className="live-view-media" style={mediaStyle}>
          {browserStream ? (
            <BrowserLiveStream
              endpoint={browserStream}
              onPlaybackProtocolChange={(protocol) => {
                setBrowserPlaybackProtocol(protocol);
                if (protocol !== 'webrtc') {
                  setBrowserDecodedFps(null);
                }
              }}
              onDecodedFpsChange={setBrowserDecodedFps}
              onVideoSize={(size) => {
                setLoadedFrameSize((current) =>
                  current?.width === size.width && current.height === size.height ? current : size
                );
              }}
            />
          ) : (
            <img
              className="live-view-image"
              src={imageSrc ?? undefined}
              alt={
                previewView === 'model'
                  ? 'Canonical runtime model frame'
                  : isTriggerFrame
                    ? 'Latest trigger channel frame'
                    : 'Latest live channel frame'
              }
              onLoad={(event) => {
                const image = event.currentTarget;
                if (image.naturalWidth > 0 && image.naturalHeight > 0) {
                  setLoadedFrameSize((current) =>
                    current?.width === image.naturalWidth && current.height === image.naturalHeight
                      ? current
                      : { width: image.naturalWidth, height: image.naturalHeight }
                  );
                }
              }}
            />
          )}
          {browserStream && browserPlaybackProtocol ? (
            <span className="live-view-playback-stats">
              {browserPlaybackProtocol.replaceAll('_', ' ').toUpperCase()}
              {browserDecodedFps !== null
                ? ` · ${browserDecodedFps.toFixed(1)} decoded fps`
                : ''}
            </span>
          ) : null}
          {renderLiveOverlays ? (
            <div className="live-view-overlay" aria-hidden="true">
              <CanvasOverlayLayer
                primitives={vectorPrimitives}
                frameWidth={frameWidth}
                frameHeight={frameHeight}
              />
              {texturePrimitives.map((primitive, index) => (
                <OverlayPrimitive
                  key={overlayPrimitiveKey(primitive, index)}
                  primitive={primitive}
                  frameWidth={frameWidth}
                  frameHeight={frameHeight}
                />
              ))}
            </div>
          ) : null}
          {renderLiveOverlays && valuePrimitives.length ? (
            <div className="live-view-values">
              {valuePrimitives.slice(0, 6).map((primitive, index) => (
                <span key={`${primitive.layer}-value-${index}`}>
                  {primitive.label ?? overlayLayerLabel(primitive.layer)}
                  {primitive.metadata.value !== undefined ? `: ${String(primitive.metadata.value)}` : ''}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      ) : (
        <span>{emptyLabel}</span>
      )}
    </div>
  );
}

function BrowserLiveStream(props: {
  endpoint: RuntimeStreamEndpoint;
  onVideoSize?: (size: { width: number; height: number }) => void;
  onPlaybackProtocolChange?: (protocol: RuntimeStreamEndpoint['protocol']) => void;
  onDecodedFpsChange?: (fps: number | null) => void;
}) {
  const { endpoint, onDecodedFpsChange, onPlaybackProtocolChange, onVideoSize } = props;

  useEffect(() => {
    onPlaybackProtocolChange?.(endpoint.protocol);
  }, [endpoint.protocol, onPlaybackProtocolChange]);

  if (endpoint.protocol !== 'webrtc') {
    return (
      <div className="inline-alert inline-alert--danger">
        Unsupported stream protocol: {endpoint.protocol}. Only WebRTC/WHEP over TCP is allowed.
      </div>
    );
  }

  return (
    <WebRtcLiveVideo
      key={endpoint.url}
      streamUrl={endpoint.url}
      onVideoSize={onVideoSize}
      onDecodedFpsChange={onDecodedFpsChange}
    />
  );
}

function decodedVideoFrameCount(video: HTMLVideoElement) {
  const quality = video.getVideoPlaybackQuality?.();
  if (quality && Number.isFinite(quality.totalVideoFrames)) {
    return quality.totalVideoFrames;
  }
  const browserVideo = video as HTMLVideoElement & {
    webkitDecodedFrameCount?: number;
  };
  return Number.isFinite(browserVideo.webkitDecodedFrameCount)
    ? browserVideo.webkitDecodedFrameCount ?? null
    : null;
}

function WebRtcLiveVideo(props: {
  streamUrl: string;
  onVideoSize?: (size: { width: number; height: number }) => void;
  onFatalConnectionError?: (message: string) => void;
  onDecodedFpsChange?: (fps: number | null) => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const onFatalConnectionErrorRef = useRef(props.onFatalConnectionError);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    onFatalConnectionErrorRef.current = props.onFatalConnectionError;
  }, [props.onFatalConnectionError]);

  useEffect(() => {
    let closed = false;
    let reader: MediaMtxReader | null = null;
    let firstFrameTimer: number | null = null;
    let decodedFpsTimer: number | null = null;
    let lastDecodedFrameCount: number | null = null;
    let lastDecodedFramePerf: number | null = null;
    let firstVideoFrameObserved = false;
    let firstVideoFrameCallbackId: number | null = null;
    let errorCount = 0;
    const whepUrl = toAbsoluteEndpointUrl(toWhepEndpointUrl(props.streamUrl));

    const clearFirstFrameTimer = () => {
      if (firstFrameTimer !== null) {
        window.clearTimeout(firstFrameTimer);
        firstFrameTimer = null;
      }
    };
    const clearDecodedFpsTimer = () => {
      if (decodedFpsTimer !== null) {
        window.clearInterval(decodedFpsTimer);
        decodedFpsTimer = null;
      }
      lastDecodedFrameCount = null;
      lastDecodedFramePerf = null;
      props.onDecodedFpsChange?.(null);
    };
    const clearFirstVideoFrameCallback = () => {
      const video = videoRef.current;
      if (
        firstVideoFrameCallbackId !== null &&
        video &&
        typeof video.cancelVideoFrameCallback === 'function'
      ) {
        video.cancelVideoFrameCallback(firstVideoFrameCallbackId);
      }
      firstVideoFrameCallbackId = null;
    };
    const observeFirstVideoFrame = (video: HTMLVideoElement) => {
      firstVideoFrameObserved = false;
      clearFirstVideoFrameCallback();
      if (typeof video.requestVideoFrameCallback === 'function') {
        firstVideoFrameCallbackId = video.requestVideoFrameCallback(() => {
          firstVideoFrameCallbackId = null;
          firstVideoFrameObserved = true;
          clearFirstFrameTimer();
          setError(null);
          if (video.videoWidth > 0 && video.videoHeight > 0) {
            props.onVideoSize?.({ width: video.videoWidth, height: video.videoHeight });
          }
        });
      }
    };
    const startDecodedFpsTimer = () => {
      if (decodedFpsTimer !== null) {
        return;
      }
      decodedFpsTimer = window.setInterval(() => {
        const video = videoRef.current;
        if (!video) {
          return;
        }
        const frameCount = decodedVideoFrameCount(video);
        const now = performance.now();
        if (
          frameCount !== null &&
          lastDecodedFrameCount !== null &&
          lastDecodedFramePerf !== null
        ) {
          const elapsedSeconds = Math.max((now - lastDecodedFramePerf) / 1000, 0.001);
          props.onDecodedFpsChange?.(
            Math.max(frameCount - lastDecodedFrameCount, 0) / elapsedSeconds
          );
        }
        lastDecodedFrameCount = frameCount;
        lastDecodedFramePerf = now;
      }, 1000);
    };
    const failPermanently = (message: string) => {
      if (closed) {
        return;
      }
      reader?.close();
      reader = null;
      clearFirstFrameTimer();
      clearFirstVideoFrameCallback();
      clearDecodedFpsTimer();
      setError(message);
      onFatalConnectionErrorRef.current?.(message);
    };

    void loadMediaMtxReader()
      .then((Reader) => {
        if (closed) {
          return;
        }
        reader = new Reader({
          url: whepUrl,
          onError: (message) => {
            if (closed) {
              return;
            }
            errorCount += 1;
            const detail = `${message}. WebRTC is configured for the TURN/TCP 3478 transport.`;
            if (errorCount > WEBRTC_MAX_RECONNECT_ATTEMPTS) {
              failPermanently(detail);
              return;
            }
            setError(
              `${detail} Reconnecting WHEP (${errorCount}/${WEBRTC_MAX_RECONNECT_ATTEMPTS}).`
            );
          },
          onTrack: (event) => {
            const video = videoRef.current;
            if (closed || !video || event.track.kind !== 'video') {
              return;
            }
            errorCount = 0;
            setError(null);
            const [remoteStream] = event.streams;
            video.srcObject = remoteStream ?? new MediaStream([event.track]);
            clearFirstFrameTimer();
            observeFirstVideoFrame(video);
            startDecodedFpsTimer();
            void video.play().catch((reason: unknown) => {
              if (closed) {
                return;
              }
              const detail =
                reason instanceof Error ? reason.message : 'The browser rejected video playback.';
              setError(`WebRTC video playback could not start: ${detail}`);
            });
            firstFrameTimer = window.setTimeout(() => {
              const decodedFrames = decodedVideoFrameCount(video);
              const hasDecodedFrame =
                firstVideoFrameObserved ||
                (decodedFrames !== null && decodedFrames > 0) ||
                video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA;
              if (!closed && !hasDecodedFrame) {
                failPermanently(
                  `WebRTC track arrived but no H.264 frame was decoded within ${WEBRTC_FIRST_FRAME_TIMEOUT_MS / 1000}s. Check NVENC constrained-baseline output and TURN/TCP 3478.`
                );
              }
            }, WEBRTC_FIRST_FRAME_TIMEOUT_MS);
          }
        });
      })
      .catch((reason: unknown) => {
        const message =
          reason instanceof Error
            ? reason.message
            : 'The local MediaMTX WebRTC reader could not be initialized.';
        failPermanently(message);
      });

    return () => {
      closed = true;
      clearFirstFrameTimer();
      clearFirstVideoFrameCallback();
      clearDecodedFpsTimer();
      reader?.close();
      if (videoRef.current) {
        videoRef.current.srcObject = null;
      }
    };
  }, [props.streamUrl, props.onDecodedFpsChange]);

  return (
    <div className="live-view-webrtc">
      <video
        ref={videoRef}
        className="live-view-image live-view-image--webrtc"
        autoPlay
        muted
        playsInline
        onPlaying={() => {
          setError(null);
        }}
        onLoadedMetadata={(event) => {
          const video = event.currentTarget;
          if (video.videoWidth > 0 && video.videoHeight > 0) {
            props.onVideoSize?.({ width: video.videoWidth, height: video.videoHeight });
          }
        }}
      />
      {error ? <span className="live-view-webrtc__error">{error}</span> : null}
    </div>
  );
}


function toWhepEndpointUrl(value: string) {
  const trimmed = value.trim().replace(/\/+$/, '');
  return trimmed.endsWith('/whep') ? trimmed : `${trimmed}/whep`;
}

export function toAbsoluteEndpointUrl(value: string, origin?: string) {
  if (/^https?:\/\//i.test(value)) {
    return value;
  }
  if (origin) {
    return new URL(value, origin).toString();
  }
  if (typeof window !== 'undefined' && window.location?.origin) {
    return new URL(value, window.location.origin).toString();
  }
  return value;
}

export function requestEndpointDisplayUrl(
  endpoint: RuntimeChannelRequestEndpoint,
  origin?: string
) {
  const baseUrl =
    endpoint.kind === 'trigger_execution'
      ? apiBaseUrls.triggerExecutorUrl
      : apiBaseUrls.resultServiceUrl;
  return buildServiceEndpointUrl(baseUrl, endpoint.path, origin);
}

export function buildServiceEndpointUrl(baseUrl: string, path: string, origin?: string) {
  const trimmedPath = path.trim();
  if (/^https?:\/\//i.test(trimmedPath)) {
    return trimmedPath;
  }
  const normalizedPath = trimmedPath.startsWith('/') ? trimmedPath : `/${trimmedPath}`;
  const trimmedBase = baseUrl.trim().replace(/\/+$/, '');
  if (!trimmedBase) {
    return toAbsoluteEndpointUrl(normalizedPath, origin);
  }
  if (/^https?:\/\//i.test(trimmedBase)) {
    return `${trimmedBase}${normalizedPath}`;
  }
  const normalizedBase = trimmedBase.startsWith('/') ? trimmedBase : `/${trimmedBase}`;
  return toAbsoluteEndpointUrl(`${normalizedBase}${normalizedPath}`, origin);
}


function CanvasOverlayLayer({
  primitives,
  frameWidth,
  frameHeight
}: {
  primitives: RuntimeOverlayPrimitiveState[];
  frameWidth: number | null;
  frameHeight: number | null;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return undefined;
    }

    const draw = () => {
      const bounds = canvas.getBoundingClientRect();
      const width = Math.max(bounds.width, 1);
      const height = Math.max(bounds.height, 1);
      const ratio = Math.max(window.devicePixelRatio || 1, 1);
      const targetWidth = Math.round(width * ratio);
      const targetHeight = Math.round(height * ratio);
      if (canvas.width !== targetWidth) {
        canvas.width = targetWidth;
      }
      if (canvas.height !== targetHeight) {
        canvas.height = targetHeight;
      }

      const context = canvas.getContext('2d');
      if (!context) {
        return;
      }

      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      context.clearRect(0, 0, width, height);
      context.lineJoin = 'round';
      context.lineCap = 'round';
      context.font =
        '700 12px Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif';

      for (const primitive of primitives) {
        drawCanvasPrimitive(context, primitive, width, height, frameWidth, frameHeight);
      }
    };

    draw();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(draw);
    observer?.observe(canvas);
    return () => observer?.disconnect();
  }, [frameHeight, frameWidth, primitives]);

  return <canvas ref={canvasRef} className="live-view-overlay__canvas" />;
}

function OverlayPrimitive({
  primitive,
  frameWidth,
  frameHeight
}: {
  primitive: RuntimeOverlayPrimitiveState;
  frameWidth: number | null;
  frameHeight: number | null;
}) {
  if (primitive.primitive_type === 'heatmap' || primitive.primitive_type === 'mask') {
    const data = typeof primitive.geometry.data === 'string' ? primitive.geometry.data : null;
    if (!data) {
      return null;
    }
    const opacity = numericValue(primitive.style.opacity) ?? (primitive.primitive_type === 'mask' ? 0.35 : 0.55);
    return (
      <img
        className={
          primitive.primitive_type === 'mask'
            ? 'live-view-overlay__mask'
            : 'live-view-overlay__heatmap'
        }
        src={`data:image/png;base64,${data}`}
        alt=""
        aria-hidden="true"
        style={{ opacity: Math.max(0, Math.min(opacity, 1)) }}
      />
    );
  }

  if (primitive.primitive_type === 'polygon') {
    const points = polygonPoints(overlayGeometry(primitive), frameWidth, frameHeight);
    if (!points) {
      return null;
    }
    const stroke = typeof primitive.style.stroke === 'string' ? primitive.style.stroke : undefined;
    const fill = typeof primitive.style.fill === 'string' ? primitive.style.fill : undefined;
    const polygonStyle: CSSProperties = {};
    if (stroke) {
      polygonStyle.stroke = stroke;
    }
    if (fill) {
      polygonStyle.fill = fill;
    }
    return (
      <svg className="live-view-overlay__svg" viewBox="0 0 100 100" preserveAspectRatio="none">
        <polygon className="live-view-overlay__polygon" points={points} style={polygonStyle} />
      </svg>
    );
  }

  if (
    primitive.primitive_type === 'line' ||
    primitive.primitive_type === 'axis' ||
    primitive.primitive_type === 'arrow'
  ) {
    const line = lineGeometry(overlayGeometry(primitive), frameWidth, frameHeight);
    if (!line) {
      return null;
    }
    const stroke = typeof primitive.style.stroke === 'string' ? primitive.style.stroke : undefined;
    const strokeWidth = numericValue(primitive.style.stroke_width) ?? 0.55;
    const lineStyle: CSSProperties = { strokeWidth };
    if (stroke) {
      lineStyle.stroke = stroke;
    }
    const markerId = `overlay-arrow-${primitive.layer.replace(/[^a-z0-9_-]/gi, '-')}`;
    return (
      <svg className="live-view-overlay__svg" viewBox="0 0 100 100" preserveAspectRatio="none">
        {primitive.primitive_type === 'arrow' ? (
          <defs>
            <marker id={markerId} markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
              <path d="M0,0 L6,3 L0,6 Z" className="live-view-overlay__arrow-head" />
            </marker>
          </defs>
        ) : null}
        <line
          className={`live-view-overlay__line live-view-overlay__line--${primitive.primitive_type}`}
          x1={line.x1}
          y1={line.y1}
          x2={line.x2}
          y2={line.y2}
          style={lineStyle}
          markerEnd={primitive.primitive_type === 'arrow' ? `url(#${markerId})` : undefined}
        />
        {primitive.label ? (
          <text className="live-view-overlay__line-label" x={line.labelX} y={line.labelY}>
            {primitive.label}
          </text>
        ) : null}
      </svg>
    );
  }

  if (primitive.primitive_type === 'point' || primitive.primitive_type === 'motion') {
    const point = pointStyle(overlayGeometry(primitive), frameWidth, frameHeight);
    if (!point) {
      return null;
    }
    return (
      <span className="live-view-overlay__point" style={point}>
        {primitive.label ? <span>{primitive.label}</span> : null}
      </span>
    );
  }

  const boxStyle = boxGeometryStyle(overlayGeometry(primitive), frameWidth, frameHeight);
  if (!boxStyle) {
    return null;
  }
  const styledBox: CSSProperties = { ...boxStyle };
  if (typeof primitive.style.stroke === 'string') {
    styledBox.borderColor = primitive.style.stroke;
  }
  if (typeof primitive.style.fill === 'string') {
    styledBox.background = primitive.style.fill;
  }
  return (
    <span className={`live-view-overlay__box live-view-overlay__box--${primitive.layer}`} style={styledBox}>
      {primitive.label ? (
        <span>
          {primitive.label}
          {primitive.confidence !== null && primitive.confidence !== undefined
            ? ` ${Math.round(primitive.confidence * 100)}%`
            : ''}
        </span>
      ) : null}
    </span>
  );
}

type ManualLiveCapturePanelProps = {
  compact?: boolean;
  targets: RuntimeManualCaptureTarget[];
  targetsLoading: boolean;
  selectedTargetId: string;
  onSelectTarget: (value: string) => void;
  intervalSeconds: number;
  onIntervalSecondsChange: (value: number) => void;
  requestPredictions: boolean;
  onRequestPredictionsChange: (value: boolean) => void;
  captureSource: 'LIVE_VIEW_FRAME' | 'PIPELINE_IMAGE_OUTPUT';
  onCaptureSourceChange: (value: 'LIVE_VIEW_FRAME' | 'PIPELINE_IMAGE_OUTPUT') => void;
  viewProfileId?: string | null;
  sourceFrame?: RuntimeLiveFrame | null;
  modelFrame?: RuntimeLiveFrame | null;
  eligibilityStatus?: string | null;
  eligibilityReason?: string | null;
  session?: RuntimeManualCaptureSessionStatus;
  lastResult?: RuntimeManualCaptureResult | null;
  feedback: { tone: 'success' | 'error'; message: string } | null;
  busy: boolean;
  onCaptureOnce: () => void;
  onStartInterval: () => void;
  onStopInterval: () => void;
};

export function ManualLiveCapturePanel(props: ManualLiveCapturePanelProps) {
  const selectedTarget = props.targets.find((target) => target.capture_target_id === props.selectedTargetId);
  const state = props.session?.state ?? 'idle';
  const running = state === 'running';
  const canonicalSourceUnavailable =
    props.captureSource === 'PIPELINE_IMAGE_OUTPUT' && !props.viewProfileId;
  const canonicalEligibilityPolicy = canonicalCaptureEligibilityPolicy(
    props.eligibilityStatus
  );
  const canonicalFrameRejected =
    props.captureSource === 'PIPELINE_IMAGE_OUTPUT' &&
    !canonicalEligibilityPolicy.singleCaptureAllowed;
  const canonicalIntervalUnavailable =
    props.captureSource === 'PIPELINE_IMAGE_OUTPUT' &&
    !canonicalEligibilityPolicy.intervalCaptureAllowed;
  const canCapture =
    canCaptureManualSnapshot(props.selectedTargetId, props.busy) &&
    !canonicalSourceUnavailable &&
    !canonicalFrameRejected;
  const canStart =
    canStartManualIntervalCapture(props.selectedTargetId, state, props.busy) &&
    !canonicalSourceUnavailable &&
    !canonicalIntervalUnavailable;
  const canStop = canStopManualIntervalCapture(state, props.busy);
  const lastResult = props.lastResult ?? null;
  const feedbackTone = props.feedback?.tone === 'error' ? 'danger' : 'success';

  const content = (
      <div className={`manual-capture ${props.compact ? 'manual-capture--compact' : ''}`}>
        <label className="filter-field">
          <span>{props.compact ? 'Label-Studio-Projekt' : 'Label Studio project'}</span>
          <select
            value={props.selectedTargetId}
            onChange={(event) => props.onSelectTarget(event.target.value)}
            disabled={props.targetsLoading || running || props.busy}
          >
            {props.targets.length ? (
              props.targets.map((target) => (
                <option key={target.capture_target_id} value={target.capture_target_id}>
                  {manualCaptureTargetLabel(target)}
                </option>
              ))
            ) : (
              <option value="">Keine Projekte verfügbar</option>
            )}
          </select>
          {props.compact ? <small className="subtle-text">Zielprojekt für Einzelbilder und Serienaufnahmen.</small> : <small className="subtle-text">
            Use a dedicated segmentation project for the canonical corridor.{' '}
            <a href="/datasets/projects">Create or map project</a>
          </small>}
        </label>
        {props.compact && <LabelStudioProjectLink projectId={selectedTarget?.label_studio_project_id}/>}

        <label className="filter-field">
          <span>{props.compact ? 'Bildquelle' : 'Capture image'}</span>
          <select
            value={props.captureSource}
            onChange={(event) =>
              props.onCaptureSourceChange(
                event.target.value as 'LIVE_VIEW_FRAME' | 'PIPELINE_IMAGE_OUTPUT'
              )
            }
            disabled={running || props.busy}
          >
            <option value="LIVE_VIEW_FRAME">{props.compact ? 'Vollständiges Kamerabild' : 'Full/live view (legacy)'}</option>
            <option value="PIPELINE_IMAGE_OUTPUT">{props.compact ? 'Modellbild / Label Studio' : 'Canonical model/Label Studio view'}</option>
          </select>
        </label>

        {props.captureSource === 'PIPELINE_IMAGE_OUTPUT' ? (
          props.viewProfileId ? (
            props.compact ? <p className="subtle-text">Profil: {props.viewProfileId}. Das geprüfte Modellbild wird unverändert übernommen; kein Rückfall auf das Vollbild.</p> : <p className="subtle-text">
              Label image: {props.viewProfileId}. The capture service verifies this profile,
              enforces explicit eligibility rejections, sends these exact pixels to Label Studio,
              and never falls back to the full frame. This source is selected automatically when a
              canonical pipeline output becomes available.
            </p>
          ) : (
            <div className="inline-alert inline-alert--danger">
              No current canonical pipeline output is available. Start a compatible pipeline or
              choose the legacy full/live view.
            </div>
          )
        ) : null}

        {props.captureSource === 'PIPELINE_IMAGE_OUTPUT' && props.viewProfileId ? (
          <div
            className={`inline-alert inline-alert--${
              props.eligibilityStatus === 'ACCEPTED'
                ? 'success'
                : props.eligibilityStatus === 'REJECTED'
                  ? 'danger'
                  : 'warn'
            }`}
          >
            Eligibility: {props.eligibilityStatus ?? 'UNAVAILABLE'}
            {!props.eligibilityStatus || props.eligibilityStatus === 'UNAVAILABLE'
              ? ' - A single manual bootstrap snapshot is allowed and marked as unverified. Automatic interval capture waits for an ACCEPTED eligibility result.'
              : ''}
            {props.eligibilityReason ? ` · ${props.eligibilityReason}` : ''}
          </div>
        ) : null}

        {props.captureSource === 'PIPELINE_IMAGE_OUTPUT' &&
        props.sourceFrame?.image_base64 &&
        props.modelFrame?.image_base64 ? (
          <details className="manual-capture__image-details" open={props.compact ? undefined : true}><summary>Bildvergleich</summary><div className="manual-capture__comparison" aria-label="Capture source comparison">
            <figure>
              <img
                src={liveImageSource(
                  props.sourceFrame.image_base64,
                  props.sourceFrame.mime_type
                )}
                alt="Undistorted full camera frame"
              />
              <figcaption>Operator view</figcaption>
            </figure>
            <figure>
              <img
                src={liveImageSource(props.modelFrame.image_base64, props.modelFrame.mime_type)}
                alt="Exact canonical Label Studio image"
              />
              <figcaption>Actual Label Studio / model input</figcaption>
            </figure>
          </div></details>
        ) : null}

        {props.compact ? (
          <div className="manual-capture__compact-state">
            <span>Letzte Aufnahme</span>
            <strong>{lastResult ? formatActivity(lastResult.captured_at) : 'Noch keine'}</strong>
            {lastResult && <span>{({success:'Aufgenommen',queued:'In Warteschlange',failed:'Fehlgeschlagen'})[lastResult.status]}</span>}
            <small>{props.session?.capture_count ?? 0} Aufnahmen · {props.session?.failure_count ?? 0} Fehler in dieser Sitzung</small>
          </div>
        ) : (
          <div className="manual-capture__state">
            <MetricPill
              label="Session"
              value={manualCaptureSessionLabel(state)}
              tone={manualCaptureSessionTone(state) === 'neutral' ? undefined : manualCaptureSessionTone(state)}
            />
            <MetricPill
              label="Last capture"
              value={lastResult ? formatActivity(lastResult.captured_at) : 'No capture yet'}
            />
            <MetricPill label="Last result" value={manualCaptureResultLabel(lastResult?.status)} />
          </div>
        )}

        <div className="manual-capture__actions">
          <button
            type="button"
            className="button--primary"
            onClick={props.onCaptureOnce}
            disabled={!canCapture}
          >
            {props.compact ? 'Einzelbild aufnehmen' : 'Capture once'}
          </button>
          <label className="manual-capture__interval">
            <span>{props.compact ? 'Intervall' : 'Every'}</span>
            <input
              type="number"
              min={1}
              max={3600}
              step={1}
              value={props.intervalSeconds}
              onChange={(event) =>
                props.onIntervalSecondsChange(normalizeManualCaptureInterval(Number(event.target.value)))
              }
              disabled={running || props.busy}
            />
            <span>sec</span>
          </label>
          <button
            type="button"
            onClick={props.onStartInterval}
            disabled={!canStart}
          >
            {props.compact ? 'Serie starten' : 'Start'}
          </button>
          <button
            type="button"
            className="button--danger"
            onClick={props.onStopInterval}
            disabled={!canStop}
          >
            {props.compact ? 'Serie stoppen' : 'Stop'}
          </button>
        </div>

        <label className="manual-capture__prediction-toggle">
          <input
            type="checkbox"
            checked={props.requestPredictions}
            onChange={(event) => props.onRequestPredictionsChange(event.target.checked)}
            disabled={running || props.busy}
          />
          <span>{props.compact ? 'Vorhersagen anfordern' : 'Predictions'}</span>
        </label>

        {!props.compact ? <dl className="definition-list definition-list--inline">
          <div>
            <dt>Project</dt>
            <dd>{selectedTarget?.display_name ?? 'Choose a project'}</dd>
          </div>
          <div>
            <dt>Mode</dt>
            <dd>{running ? `Interval / ${props.session?.interval_seconds ?? props.intervalSeconds}s` : 'Single or interval'}</dd>
          </div>
          <div>
            <dt>Captures</dt>
            <dd>{String(props.session?.capture_count ?? 0)}</dd>
          </div>
          <div>
            <dt>Failures</dt>
            <dd>{String(props.session?.failure_count ?? 0)}</dd>
          </div>
        </dl> : null}

        {props.feedback ? (
          <div className={`inline-alert inline-alert--${feedbackTone}`}>{props.feedback.message}</div>
        ) : null}
        {lastResult?.error_summary ? (
          <div className="inline-alert inline-alert--danger">{lastResult.error_summary}</div>
        ) : null}
      </div>
  );
  return props.compact ? content : <Panel title="Manual Live Capture">{content}</Panel>;
}

export function ChannelHttpRequestEndpointsPanel(props: { endpoints: RuntimeChannelRequestEndpoint[] }) {
  const orderedEndpoints = [...props.endpoints].sort((left, right) => {
    const order = { trigger_execution: 0, latest_result: 1 } as const;
    return order[left.kind] - order[right.kind];
  });
  return (
    <details className="channel-compact-section channel-endpoints-section">
      <summary>
        <span>
          <strong>HTTP Request Endpunkte</strong>
          <small>Trigger und Live-Ergebnisse für externe Systeme</small>
        </span>
        <span className="runtime-chip runtime-chip--neutral">{orderedEndpoints.length}</span>
      </summary>
      <div className="channel-compact-section__body channel-endpoint-list">
        {orderedEndpoints.length ? (
          orderedEndpoints.map((endpoint) => (
            <article key={`${endpoint.kind}-${endpoint.path}`} className="channel-endpoint-row">
              <div className="channel-endpoint-row__heading">
                <div>
                  <strong>{endpoint.label}</strong>
                  <small className="subtle-text">
                    {endpoint.pipeline_label ?? endpoint.description ?? endpoint.path}
                  </small>
                </div>
                <span className={`badge badge--${endpoint.available ? 'success' : 'warn'}`}>
                  {endpoint.available ? 'Bereit' : 'Setup fehlt'}
                </span>
              </div>
              <RequestEndpointUrlRow endpoint={endpoint} />
              <div className="channel-endpoint-row__meta">
                <span>{endpoint.kind === 'trigger_execution' ? 'Trigger' : 'Live-Ergebnis'}</span>
                <span>{endpoint.auth_required ? endpoint.auth_label : 'Not required'}</span>
                <span>{formatActivity(endpoint.last_activity_at)}</span>
              </div>
              {!endpoint.available && endpoint.unavailable_reason ? (
                <small className="subtle-text">{endpoint.unavailable_reason}</small>
              ) : null}
            </article>
          ))
        ) : (
          <p className="subtle-text">
            Nach dem Veröffentlichen einer Channel-Pipeline erscheinen hier ihre HTTP-Endpunkte.
          </p>
        )}
      </div>
    </details>
  );
}

function RequestEndpointUrlRow(props: { endpoint: RuntimeChannelRequestEndpoint }) {
  const displayUrl = requestEndpointDisplayUrl(props.endpoint);
  const copyUrl = () => {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      void navigator.clipboard.writeText(displayUrl);
    }
  };
  return (
    <div className="stream-url-row">
      <span>{props.endpoint.method}</span>
      <code>{displayUrl}</code>
      <button type="button" onClick={copyUrl}>
        Copy
      </button>
    </div>
  );
}

function buildUpdatePayload(
  form: typeof emptyForm,
  currentPlacement: Pick<RuntimeChannelPlacement, 'target' | 'edge_device_id'>
): RuntimeChannelUpdateRequest {
  const { config, credentials } = sourceConfigAndCredentials(
    form.sourceType,
    form.sourceAddress,
    form.testFileSourceConfig,
    form.rtspUsername,
    form.rtspPassword,
    form.maxFps,
    form.sampleEveryN,
    form.pipelineMaxInflight,
    baslerPylonConfig(form.sourceAddress, form)
  );
  return {
    name: form.name.trim(),
    technical_place: form.technicalPlace.trim(),
    facility_id: form.facilityId || null,
    description: form.description.trim() || null,
    source_type: form.sourceType,
    source_config: config,
    source_credentials: credentials,
    mode: 'realtime',
    pipeline_version_id: form.pipelineVersionId || null,
    placement: buildChannelPlacementUpdate(
      form.executionTarget,
      form.edgeDeviceId,
      currentPlacement
    ),
    authoring_label_project_ids: form.authoringLabelProjectIds,
    result_history_policy: {
      enabled: form.historyEnabled,
      retention_hours: positiveInteger(form.historyRetentionHours, 24),
      sample_interval_seconds: positiveInteger(form.historySampleIntervalSeconds, 5),
      max_records: positiveInteger(form.historyMaxRecords, 10000)
    },
    capture_summary: {
      summary: form.captureSummary.trim() || 'No capture rules',
      rule_count: form.captureSummary.trim() ? 1 : 0,
      target_project_count: 0,
      modes: []
    }
  };
}

export function buildChannelPlacementUpdate(
  target: RuntimeChannelPlacement['target'],
  edgeDeviceId: string,
  currentPlacement: Pick<RuntimeChannelPlacement, 'target' | 'edge_device_id'>
): RuntimeChannelUpdateRequest['placement'] | undefined {
  const normalizedEdgeDeviceId = target === 'edge_device' ? edgeDeviceId.trim() : null;
  const currentEdgeDeviceId =
    currentPlacement.target === 'edge_device' ? currentPlacement.edge_device_id ?? null : null;
  if (target === currentPlacement.target && normalizedEdgeDeviceId === currentEdgeDeviceId) {
    return undefined;
  }
  return {
    target,
    edge_device_id: normalizedEdgeDeviceId
  };
}

export function channelPlacementEditable(
  channel: Pick<
    RuntimeChannelSummary,
    'desired_state' | 'observed_state' | 'placement' | 'runtime_state' | 'status'
  >
) {
  const desiredState = channel.runtime_state?.desired_state ?? channel.desired_state;
  const observedState = channelRuntimeActualState(channel);
  return (
    !['start_requested', 'running', 'stop_requested'].includes(desiredState) &&
    !['starting', 'running', 'stopping'].includes(observedState)
  );
}

function positiveInteger(value: string, fallback: number) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

type ChannelPipelineVersionOption = {
  value: string;
  label: string;
  description: string;
  mode: RuntimePipelineVersionSummary['mode'];
};

function channelPipelineVersionOptions(
  pipelines: RuntimePipelineDefinitionSummary[],
  detail: RuntimePipelineDefinitionDetail | null
): ChannelPipelineVersionOption[] {
  const options = new Map<string, ChannelPipelineVersionOption>();
  for (const pipeline of pipelines) {
    if (!pipeline.published_version_id) {
      continue;
    }
    const description = pipeline.description || 'Published channel pipeline version';
    options.set(pipeline.published_version_id, {
      value: pipeline.published_version_id,
      label: `${pipeline.name} / ${pipelineModeLabel(pipeline.published_version_mode)} / ${description}`,
      description,
      mode: pipeline.published_version_mode ?? 'realtime'
    });
  }
  const pipeline = pipelines.find((item) => item.pipeline_definition_id === detail?.pipeline_definition_id) ?? null;
  const publishedVersions = (detail?.versions ?? [])
    .filter((version) => version.status === 'published')
    .sort((left, right) => right.version_no - left.version_no);
  if (pipeline && publishedVersions.length) {
    for (const version of publishedVersions) {
      const description = version.change_note || pipeline.description || 'Published channel pipeline version';
      options.set(version.pipeline_version_id, {
        value: version.pipeline_version_id,
        label: `${pipeline.name} v${version.version_no} / ${pipelineModeLabel(version.mode)} / ${description}`,
        description,
        mode: version.mode
      });
    }
  }
  return Array.from(options.values());
}

function authoringProjectMeta(project: LabelProjectSummary) {
  const classCount = new Set([
    ...project.required_classes,
    ...Object.keys(project.class_distribution ?? {})
  ]).size;
  return [
    project.dataset_name || project.task_type,
    classCount ? `${classCount} classes` : 'labels from project config'
  ].join(' / ');
}

function pipelineModeLabel(
  mode: RuntimePipelineDefinitionSummary['published_version_mode'] | RuntimePipelineVersionSummary['mode']
) {
  if (mode === 'trigger') {
    return 'trigger';
  }
  if (mode === 'interval') {
    return 'interval';
  }
  return 'live';
}

function sourceConfigAndCredentials(
  sourceType: RuntimeSourceType,
  sourceAddress: string,
  testFileSourceConfig: Record<string, unknown>,
  rtspUsername: string,
  rtspPassword: string,
  maxFps: string,
  sampleEveryN: string,
  pipelineMaxInflight: string,
  baslerConfig: Record<string, unknown>
): { config: Record<string, unknown>; credentials?: { username?: string; password?: string } } {
  const value = sourceAddress.trim();
  if (sourceType === 'rtsp') {
    const parsed = parseCredentialedRtspUrl(value);
    const username = rtspUsername.trim() || parsed.username;
    const password = rtspPassword || parsed.password;
    return {
      config: withRealtimeSourceTuning(
        parsed.config,
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      ),
      credentials:
        username || password
          ? {
              ...(username ? { username } : {}),
              ...(password ? { password } : {})
            }
          : undefined
    };
  }
  if (sourceType === 'snapshot') {
    return { config: { url: value } };
  }
  if (sourceType === 'usb_opencv') {
    return {
      config: withRealtimeSourceTuning(
        usbOpenCvConfig(value, testFileSourceConfig),
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'flir_blackfly') {
    return {
      config: withRealtimeSourceTuning(
        {
          ...testFileSourceConfig,
          ...flirBlackflyConfig(value)
        },
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'aravis_gige') {
    return {
      config: withRealtimeSourceTuning(
        {
          ...testFileSourceConfig,
          ...aravisGigeConfig(value)
        },
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'basler_pylon') {
    return {
      config: withRealtimeSourceTuning(
        {
          ...testFileSourceConfig,
          ...baslerConfig
        },
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  if (sourceType === 'file_test') {
    return {
      config: withRealtimeSourceTuning(
        {
          ...testFileSourceConfig,
          path: value
        },
        maxFps,
        sampleEveryN,
        pipelineMaxInflight
      )
    };
  }
  return { config: value ? { camera: value } : {} };
}

function withRealtimeSourceTuning(
  config: Record<string, unknown>,
  maxFps: string,
  sampleEveryN: string,
  pipelineMaxInflight: string
) {
  const tuned = { ...config };
  delete tuned.max_fps;
  delete tuned.sample_every_n;
  delete tuned.pipeline_max_inflight;
  const fps = Number(maxFps);
  if (Number.isFinite(fps) && fps > 0) {
    tuned.max_fps = Math.min(Math.max(fps, 0.1), 120);
  }
  const sample = Math.round(Number(sampleEveryN));
  if (Number.isFinite(sample) && sample > 1) {
    tuned.sample_every_n = Math.min(Math.max(sample, 1), 1000);
  }
  const maxInflight = Math.round(Number(pipelineMaxInflight));
  if (Number.isFinite(maxInflight) && maxInflight > 0) {
    tuned.pipeline_max_inflight = Math.min(Math.max(maxInflight, 1), 16);
  }
  return tuned;
}

function numericConfigValue(config: Record<string, unknown>, key: string, fallback: string) {
  const value = config[key];
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  return String(value);
}

function sourceAddressFromConfig(sourceType: RuntimeSourceType, config: Record<string, unknown>) {
  if (sourceType === 'rtsp') {
    return String(config.url ?? rtspAddressFromParts(config) ?? '');
  }
  if (sourceType === 'snapshot') {
    return String(config.url ?? config.snapshot_url ?? config.http_url ?? '');
  }
  if (sourceType === 'usb_opencv') {
    return String(config.device_path ?? config.device_index ?? '');
  }
  if (sourceType === 'flir_blackfly') {
    return String(
      config.serial_number ??
        config.ip_address ??
        config.device_id ??
        config.id_ ??
        config.user_defined_name ??
        ''
    );
  }
  if (sourceType === 'basler_pylon') {
    if (config.user_defined_name) {
      return `name:${config.user_defined_name}`;
    }
    if (config.camera_name) {
      return `camera:${config.camera_name}`;
    }
    if (config.device_id ?? config.id_) {
      return `id:${config.device_id ?? config.id_}`;
    }
    if (config.model) {
      return `model:${config.model}`;
    }
    return String(config.serial_number ?? config.ip_address ?? '');
  }
  if (sourceType === 'aravis_gige') {
    return String(
      config.camera_name ??
        config.serial_number ??
        config.ip_address ??
        config.device_id ??
        config.id_ ??
        config.user_defined_name ??
        ''
    );
  }
  if (sourceType === 'file_test') {
    return String(config.path ?? '');
  }
  return String(config.camera ?? config.device ?? '');
}

function sourceAddressValid(sourceType: RuntimeSourceType, sourceAddress: string) {
  if (sourceType === 'snapshot') {
    return /^https?:\/\//i.test(sourceAddress.trim());
  }
  return sourceType === 'trigger_capture' || Boolean(sourceAddress.trim());
}

function sourcePlaceholder(sourceType: RuntimeSourceType) {
  if (sourceType === 'rtsp') {
    return 'rtsp://camera.local/stream';
  }
  if (sourceType === 'usb_opencv') {
    return '0 or /dev/v4l/by-id/usb-camera-video-index0';
  }
  if (sourceType === 'flir_blackfly') {
    return 'serial number, device id, or 192.168.10.42';
  }
  if (sourceType === 'basler_pylon') {
    return 'serial, name:PressCam, model:a2A1920-51gcBAS, or 192.168.10.42';
  }
  if (sourceType === 'aravis_gige') {
    return 'Aravis camera name, serial number, or 192.168.10.42';
  }
  if (sourceType === 'snapshot') {
    return 'https://camera.local/latest.jpg';
  }
  if (sourceType === 'file_test') {
    return '/data/runtime/test-image.jpg';
  }
  return 'Press camera 1';
}

function flirBlackflyConfig(value: string) {
  const trimmed = value.trim();
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(trimmed)) {
    return { ip_address: trimmed };
  }
  if (trimmed.startsWith('id:')) {
    return { device_id: trimmed.slice(3).trim() };
  }
  if (trimmed.startsWith('name:')) {
    return { user_defined_name: trimmed.slice(5).trim() };
  }
  return { serial_number: trimmed };
}

function usbOpenCvConfig(value: string, existing: Record<string, unknown> = {}) {
  const trimmed = value.trim();
  const config = { ...existing };
  delete config.device_index;
  delete config.device_path;
  delete config.device;
  delete config.uri;
  if (/^\d+$/.test(trimmed)) {
    return { ...config, device_index: Number.parseInt(trimmed, 10) };
  }
  return { ...config, device_path: trimmed };
}

function aravisGigeConfig(value: string) {
  return {
    ...flirBlackflyConfig(value),
    camera_name: value,
    vendor: 'flir_blackfly',
    pixel_format: 'BayerRG8',
    conversion_mode: 'bayer'
  };
}

function baslerPylonConfig(value: string, form: typeof emptyForm) {
  const config: Record<string, unknown> = {
    ...baslerCameraAddressConfig(baslerIdentifierConfig(value), form.baslerCameraIp),
    transport_mode: form.baslerTransportMode || 'auto',
    acquisition_mode: form.baslerAcquisitionMode || 'Continuous',
    grab_strategy: form.baslerGrabStrategy || 'LatestImageOnly',
    pixel_format: form.baslerPixelFormat || 'BayerRG8',
    trigger_mode: form.baslerTriggerMode || 'Off'
  };
  addPositiveNumber(config, 'fps', form.baslerCameraFps);
  addPositiveNumber(config, 'link_speed_mbps', form.baslerLinkSpeedMbps);
  if (config.fps !== undefined) {
    config.acquisition_frame_rate_enable = true;
  }
  addPositiveNumber(config, 'exposure_time_us', form.baslerExposureTimeUs);
  addNonNegativeNumber(config, 'gain_db', form.baslerGainDb);
  addPositiveInteger(config, 'packet_size', form.baslerPacketSize);
  addNonNegativeInteger(config, 'inter_packet_delay', form.baslerInterPacketDelay);
  addPositiveInteger(config, 'buffer_count', form.baslerBufferCount);
  addPositiveInteger(config, 'output_queue_size', form.baslerOutputQueueSize);
  addPositiveInteger(config, 'frame_retention_ms', form.baslerFrameRetentionMs);
  addPositiveInteger(
    config,
    'grab_failure_reconnect_threshold',
    form.baslerGrabFailureReconnectThreshold
  );
  addPositiveInteger(config, 'width', form.baslerWidth);
  addPositiveInteger(config, 'height', form.baslerHeight);
  if (form.baslerTriggerMode === 'On') {
    config.trigger_source = form.baslerTriggerSource || 'Line1';
    config.trigger_activation = form.baslerTriggerActivation || 'RisingEdge';
  }
  return config;
}

function baslerIdentifierConfig(value: string) {
  const trimmed = value.trim();
  if (trimmed.startsWith('model:')) {
    return { model: trimmed.slice(6).trim() };
  }
  if (trimmed.startsWith('camera:')) {
    return { camera_name: trimmed.slice(7).trim() };
  }
  return flirBlackflyConfig(trimmed);
}

function baslerConfigurationWarning(form: typeof emptyForm) {
  const width = Number(form.baslerWidth);
  const height = Number(form.baslerHeight);
  const fps = Number(form.baslerCameraFps);
  const linkSpeedMbps = Number(form.baslerLinkSpeedMbps);
  const bitsPerPixel: Record<string, number> = {
    Mono8: 8,
    BayerRG8: 8,
    BayerBG8: 8,
    BayerGB8: 8,
    BayerGR8: 8,
    YUV422Packed: 16,
    RGB8: 24,
    BGR8: 24
  };
  const bpp = bitsPerPixel[form.baslerPixelFormat];
  if (width > 0 && height > 0 && fps > 0 && linkSpeedMbps > 0 && bpp) {
    const payloadMbps = (width * height * fps * bpp) / 1_000_000;
    if (payloadMbps > linkSpeedMbps * 0.8) {
      return `Estimated raw payload is ${payloadMbps.toFixed(0)} Mbit/s before transport overhead. Keep it below roughly 80% of the configured ${linkSpeedMbps.toFixed(0)}-Mbit/s camera link by using Bayer/Mono8, a smaller ROI, or fewer FPS.`;
    }
  }
  if (
    form.baslerAcquisitionMode === 'Continuous' &&
    form.baslerTriggerMode === 'On' &&
    form.baslerTriggerSource === 'Software'
  ) {
    return 'Continuous live acquisition is configured with a synchronous software trigger. Use Trigger mode Off for a free-running stream unless every frame must be software-triggered.';
  }
  return null;
}

function addPositiveNumber(config: Record<string, unknown>, key: string, value: string) {
  const number = Number(value);
  if (Number.isFinite(number) && number > 0) {
    config[key] = number;
  }
}

function addNonNegativeNumber(config: Record<string, unknown>, key: string, value: string) {
  const number = Number(value);
  if (Number.isFinite(number) && number >= 0) {
    config[key] = number;
  }
}

function addPositiveInteger(config: Record<string, unknown>, key: string, value: string) {
  const number = Math.round(Number(value));
  if (Number.isFinite(number) && number > 0) {
    config[key] = number;
  }
}

function addNonNegativeInteger(config: Record<string, unknown>, key: string, value: string) {
  const number = Math.round(Number(value));
  if (Number.isFinite(number) && number >= 0) {
    config[key] = number;
  }
}

function testFileNameFromConfig(config: Record<string, unknown>) {
  const configured = config.file_name ?? config.original_filename;
  if (configured) {
    return String(configured);
  }
  const path = String(config.path ?? '');
  return path.split(/[\\/]/).filter(Boolean).pop() ?? '';
}

function resetSourceSelection(form: typeof emptyForm, sourceType: RuntimeSourceType) {
  return {
    ...form,
    sourceType,
    sourceAddress: '',
    testFileName: '',
    testFileSourceConfig: {}
  };
}

function rtspAddressFromParts(config: Record<string, unknown>) {
  const host = String(config.host ?? '').trim();
  if (!host) {
    return '';
  }
  const scheme = String(config.scheme ?? 'rtsp').trim() || 'rtsp';
  const port = config.port ? `:${config.port}` : '';
  const path = String(config.path ?? config.stream_path ?? '').replace(/^\/+/, '');
  return `${scheme}://${host}${port}/${path}`.replace(/\/$/, '');
}

function parseCredentialedRtspUrl(value: string) {
  if (!value) {
    return { config: { url: value }, username: '', password: '' };
  }
  try {
    const parsed = new URL(value);
    if (!['rtsp:', 'rtsps:'].includes(parsed.protocol) || (!parsed.username && !parsed.password)) {
      return { config: { url: value }, username: '', password: '' };
    }
    const username = decodeURIComponent(parsed.username);
    const password = decodeURIComponent(parsed.password);
    parsed.username = '';
    parsed.password = '';
    return { config: { url: parsed.toString() }, username, password };
  } catch {
    return { config: { url: value }, username: '', password: '' };
  }
}

function compactResultFields(result: RuntimeResultRecordSummary) {
  const responseEntries = Object.entries(result.trigger_response);
  return responseEntries.length ? responseEntries : Object.entries(result.derived_preview);
}

function compactValue(value: unknown) {
  if (value === null || value === undefined || value === '') {
    return 'n/a';
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  if (typeof value === 'boolean') {
    return value ? 'yes' : 'no';
  }
  if (typeof value === 'string') {
    return value;
  }
  return JSON.stringify(value);
}

function compactResultTone(status: string) {
  if (status === 'completed') {
    return 'success';
  }
  if (status === 'timeout') {
    return 'warn';
  }
  if (status === 'failed') {
    return 'danger';
  }
  return 'neutral';
}

function liveImageSource(imageBase64: string, mimeType = 'image/jpeg') {
  return imageBase64.startsWith('data:') ? imageBase64 : `data:${mimeType};base64,${imageBase64}`;
}

function isTriggerLiveViewState(liveState: RuntimeLiveViewState | null) {
  return Boolean(
    liveState?.runtime_instance_id?.startsWith('trigger-latest-') ||
    liveState?.frame?.metadata?.trigger_preview === true
  );
}

function readLiveOverlayLayerVisibility(channelId: string): Record<string, boolean> {
  if (typeof window === 'undefined') {
    return {};
  }
  try {
    const raw = window.localStorage.getItem(liveOverlayStorageKey(channelId));
    const parsed = raw ? JSON.parse(raw) : null;
    if (!parsed || typeof parsed !== 'object') {
      return {};
    }
    return Object.fromEntries(
      Object.entries(parsed).filter(([, value]) => typeof value === 'boolean')
    ) as Record<string, boolean>;
  } catch {
    return {};
  }
}

function writeLiveOverlayLayerVisibility(channelId: string, visibility: Record<string, boolean>) {
  if (typeof window === 'undefined') {
    return;
  }
  try {
    window.localStorage.setItem(liveOverlayStorageKey(channelId), JSON.stringify(visibility));
  } catch {
    // Ignore storage quota or private-mode failures; the in-memory state still applies.
  }
}

function liveOverlayStorageKey(channelId: string) {
  return `runtime-live-overlay-layers:${channelId}`;
}

export function selectBrowserStreamEndpoint(
  response: RuntimeChannelStreamListResponse | undefined,
  variant: RuntimeStreamVariantStatus['variant'] = 'raw'
): RuntimeStreamEndpoint | null {
  if (!response) {
    return null;
  }
  const production = selectReadyBrowserStreamVariant(response, variant);
  return selectEndpointByProtocol(production, 'webrtc') ?? null;
}

function isBrowserLiveStreamEndpoint(endpoint: RuntimeStreamEndpoint) {
  return endpoint.protocol === 'webrtc';
}

function selectEndpointByProtocol(
  variant: RuntimeStreamVariantStatus | null | undefined,
  protocol: RuntimeStreamEndpoint['protocol']
) {
  return variant?.endpoints.find(
    (endpoint) => endpoint.protocol === protocol && endpoint.browser_compatible
  );
}

function selectReadyBrowserStreamVariant(
  response: RuntimeChannelStreamListResponse | undefined,
  requestedVariant: RuntimeStreamVariantStatus['variant'] = 'raw'
) {
  if (!response) {
    return null;
  }
  const readyVariants = response.items.filter(
    (variant) =>
      variant.variant === requestedVariant &&
      variant.fanout === 'server' &&
      variant.ready &&
      (variant.status === 'ready' || variant.status === 'degraded')
  );
  return readyVariants[0] ?? null;
}

function streamVariantLabel(variant: RuntimeStreamVariantStatus) {
  if (variant.variant === 'raw') {
    return 'Raw stream';
  }
  if (variant.variant === 'canonical_model') {
    return 'Canonical model stream';
  }
  if (variant.variant === 'undistorted_full') {
    return 'Undistorted full stream';
  }
  return 'WebRTC live stream';
}

function streamStatusTone(variant: RuntimeStreamVariantStatus): 'success' | 'warn' {
  if (variant.status === 'ready') {
    return 'success';
  }
  return 'warn';
}

function streamProtocols(variant: RuntimeStreamVariantStatus) {
  const protocols = Array.from(new Set(variant.endpoints.map((endpoint) => endpoint.protocol)));
  return protocols.length ? protocols.join(' / ').replaceAll('_', ' ') : variant.protocol.replaceAll('_', ' ');
}

function streamEncoderLabel(variant: RuntimeStreamVariantStatus) {
  if (variant.encoder.mode === 'gpu') {
    return variant.encoder.encoder ? `GPU ${variant.encoder.encoder}` : 'GPU';
  }
  return 'Unavailable';
}

function liveMediaStyle(width: number | null, height: number | null): CSSProperties & {'--live-frame-ratio': number} {
  if (!width || !height || width <= 0 || height <= 0) {
    return {
      aspectRatio: '16 / 9',
      '--live-frame-ratio': 16 / 9,
      maxWidth: `${(16 / 9) * 520}px`
    };
  }
  const aspect = width / height;
  return {
    aspectRatio: `${width} / ${height}`,
    '--live-frame-ratio': aspect,
    maxWidth: `${Math.max(aspect * 520, 1)}px`
  };
}

function overlayPrimitiveKey(primitive: RuntimeOverlayPrimitiveState, index: number) {
  return [
    primitive.layer,
    primitive.primitive_type,
    primitive.object_id ?? primitive.source_object_id ?? primitive.label ?? 'overlay',
    primitive.image_context_id ?? primitive.display_context_id ?? index
  ].join(':');
}

function formatMetric(value: number | null | undefined, unit: 'fps' | '%' | 'ms') {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return 'n/a';
  }
  if (unit === '%') {
    return `${value.toFixed(1)}%`;
  }
  if (unit === 'ms') {
    return `${value.toFixed(0)} ms`;
  }
  return `${value.toFixed(1)} fps`;
}

function formatFrameCap(liveState: RuntimeLiveViewState | null) {
  const diagnostics = liveState?.diagnostics;
  if (!diagnostics) {
    return 'n/a';
  }
  const parts: string[] = [];
  if (diagnostics.effective_source_limit_fps) {
    parts.push(`${diagnostics.effective_source_limit_fps.toFixed(1)} fps`);
  }
  if (diagnostics.source_sample_every_n > 1) {
    parts.push(`1/${diagnostics.source_sample_every_n}`);
  }
  if (diagnostics.pipeline_frame_samplers.length) {
    parts.push(`${diagnostics.pipeline_frame_samplers.length} sampler`);
  }
  return parts.length ? parts.join(' / ') : 'none';
}

function formatPipelineInflight(liveState: RuntimeLiveViewState | null) {
  const diagnostics = liveState?.diagnostics;
  if (!diagnostics) {
    return 'n/a';
  }
  const reasonLabels: Record<string, string> = {
    stateful_pipeline: 'stateful',
    per_release_concurrency: 'release limit',
    global_inference_concurrency: 'gateway limit',
    executor_safety_cap: 'safety cap',
    channel_configuration: 'channel config',
  };
  const reasonKey = diagnostics.pipeline_inflight_limit_reason
    ?? (diagnostics.pipeline_stateful_nodes?.length ? 'stateful_pipeline' : 'channel_configuration');
  const reason = reasonLabels[reasonKey] ?? 'effective';
  const configured = diagnostics.pipeline_configured_max_inflight
    ?? diagnostics.pipeline_max_inflight;
  const configuredSuffix = configured !== diagnostics.pipeline_max_inflight
    ? `; configured ${configured}`
    : '';
  return `${diagnostics.pipeline_max_inflight} (${reason}${configuredSuffix})`;
}

type PlcWriteStatusRow = {
  nodeKey: string;
  mappingKey: string;
  mappingLabel: string;
  status: string;
  address: string;
  dataType: string;
  mappedValue: string;
  reason: string;
  error: string;
};

function plcWriteStatuses(outputs: Array<Record<string, unknown>>): PlcWriteStatusRow[] {
  const rows: PlcWriteStatusRow[] = [];
  for (const output of outputs) {
    const payload = objectRecord(output.payload);
    const writes = Array.isArray(payload.writes) ? payload.writes : [];
    for (const rawWrite of writes) {
      const write = objectRecord(rawWrite);
      rows.push({
        nodeKey: String(output.node_key ?? output.node_type ?? 'plc_write'),
        mappingKey: String(write.mapping_key ?? write.mapping_label ?? rows.length),
        mappingLabel: String(write.mapping_label ?? write.mapping_key ?? 'PLC write'),
        status: String(write.status ?? 'unknown').replaceAll('_', ' '),
        address: String(write.address ?? 'DB'),
        dataType: String(write.data_type ?? ''),
        mappedValue: formatPlcMappedValue(write.mapped_value),
        reason: String(write.reason ?? ''),
        error: String(write.error ?? '')
      });
    }
  }
  return rows;
}

function objectRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
}

function formatPlcMappedValue(value: unknown) {
  if (value === null || value === undefined) {
    return 'n/a';
  }
  if (typeof value === 'number') {
    return Number.isFinite(value)
      ? value.toFixed(Math.abs(value) >= 100 ? 0 : 3).replace(/\.?0+$/, '')
      : 'n/a';
  }
  return String(value);
}

function plcWriteTone(status: string): 'success' | 'warn' | 'danger' {
  if (status === 'failed') {
    return 'danger';
  }
  if (status === 'success' || status === 'dry run') {
    return 'success';
  }
  return 'warn';
}

const stageTimingOrder = [
  'source_read_ms',
  'source_frame_interval_ms',
  'buffer_wait_ms',
  'throttle_wait_ms',
  'pipeline_queue_wait_ms',
  'frame_encode_ms',
  'frame_payload_prepare_ms',
  'frame_payload_ms',
  'context_build_ms',
  'pipeline_execute_ms',
  'pipeline_task_ms',
  'stale_result_drop_ms',
  'live_state_build_ms',
  'stream_publish_enqueue_ms',
  'live_state_write_ms',
  'result_persist_ms',
  'heartbeat_ms',
  'total_ms'
];

function orderedStageTimings(timings: Record<string, number>) {
  return Object.entries(timings)
    .filter(([, value]) => Number.isFinite(value))
    .sort(([left], [right]) => {
      const leftIndex = stageTimingOrder.indexOf(left);
      const rightIndex = stageTimingOrder.indexOf(right);
      return (leftIndex === -1 ? 999 : leftIndex) - (rightIndex === -1 ? 999 : rightIndex);
    });
}

function stageTimingLabel(key: string) {
  const labels: Record<string, string> = {
    source_read_ms: 'Source read',
    source_frame_interval_ms: 'Source interval',
    buffer_wait_ms: 'Buffer wait',
    throttle_wait_ms: 'Throttle wait',
    pipeline_queue_wait_ms: 'Pipeline queue',
    frame_encode_ms: 'Frame encode',
    frame_payload_prepare_ms: 'Frame prepare',
    frame_payload_ms: 'Frame payload',
    context_build_ms: 'Context',
    pipeline_execute_ms: 'Pipeline',
    pipeline_task_ms: 'Pipeline task',
    stale_result_drop_ms: 'Stale drop',
    live_state_build_ms: 'Live state',
    stream_publish_enqueue_ms: 'Stream publish',
    live_state_write_ms: 'State write',
    result_persist_ms: 'Result persist',
    heartbeat_ms: 'Heartbeat',
    total_ms: 'Total'
  };
  return labels[key] ?? key.replace(/_ms$/, '').replaceAll('_', ' ');
}

function orderedPipelineNodeTimings(items: Array<Record<string, unknown>>) {
  return [...items]
    .filter((item) => timingNumber(item.total_ms) !== null)
    .sort((left, right) => (timingNumber(right.total_ms) ?? 0) - (timingNumber(left.total_ms) ?? 0))
    .slice(0, 6);
}

function orderedPipelineInferenceTimings(items: Array<Record<string, unknown>>) {
  return [...items]
    .filter(
      (item) =>
        timingNumber(item.client_roundtrip_ms) !== null ||
        timingNumber(item.gateway_total_ms) !== null
    )
    .sort(
      (left, right) =>
        (timingNumber(right.client_roundtrip_ms) ??
          timingNumber(right.gateway_total_ms) ??
          0) -
        (timingNumber(left.client_roundtrip_ms) ?? timingNumber(left.gateway_total_ms) ?? 0)
    )
    .slice(0, 4);
}

function pipelineNodeTimingLabel(item: Record<string, unknown>) {
  const nodeKey = String(item.node_key ?? 'node');
  const nodeType = String(item.node_type ?? '');
  return nodeType ? `Node ${nodeKey} (${nodeType})` : `Node ${nodeKey}`;
}

function pipelineNodeTimingValue(item: Record<string, unknown>) {
  const total = timingNumber(item.total_ms);
  const execute = timingNumber(item.execute_ms);
  const status = typeof item.status === 'string' && item.status !== 'completed' ? ` ${item.status}` : '';
  if (total === null) {
    return `n/a${status}`;
  }
  if (execute !== null && Math.abs(total - execute) > 1) {
    return `${formatMs(total)} total / ${formatMs(execute)} exec${status}`;
  }
  return `${formatMs(total)}${status}`;
}

function pipelineInferenceTimingLabel(item: Record<string, unknown>) {
  const nodeKey = String(item.node_key ?? 'node');
  return `Inference ${nodeKey}`;
}

function pipelineInferenceTimingValue(item: Record<string, unknown>) {
  const parts: string[] = [];
  const client = timingNumber(item.client_roundtrip_ms);
  const gateway = timingNumber(item.gateway_total_ms);
  const inference = timingNumber(item.gateway_inference_ms);
  const preprocessing = timingNumber(item.gateway_preprocessing_ms);
  const postprocessing = timingNumber(item.gateway_postprocessing_ms);
  const sharedMemory = timingNumber(item.gateway_shared_memory_transport);
  const imagePrepare = timingNumber(item.request_image_prepare_ms);
  const binaryTransport = timingNumber(item.binary_transport);
  if (imagePrepare !== null) {
    parts.push(`${formatMs(imagePrepare)} image`);
  }
  if (client !== null) {
    parts.push(`${formatMs(client)} ${binaryTransport === 1 ? 'client bytes' : 'client'}`);
  }
  if (gateway !== null) {
    parts.push(`${formatMs(gateway)} gateway${sharedMemory === 1 ? ' shm' : ''}`);
  }
  if (inference !== null) {
    parts.push(`${formatMs(inference)} infer`);
  }
  if (preprocessing !== null) {
    parts.push(`${formatMs(preprocessing)} prep`);
  }
  if (postprocessing !== null) {
    parts.push(`${formatMs(postprocessing)} post`);
  }
  return parts.length ? parts.join(' / ') : 'n/a';
}

function timingNumber(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function formatMs(value: number) {
  return `${value.toFixed(value >= 100 ? 0 : 1)} ms`;
}

function overlayLayerLabel(layer: string) {
  if (layer === 'pipeline_crop') {
    return 'Crop area';
  }
  return layer
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
}

function isTextureOverlay(primitive: RuntimeOverlayPrimitiveState) {
  return primitive.primitive_type === 'heatmap' || primitive.primitive_type === 'mask';
}

function overlayGeometry(primitive: RuntimeOverlayPrimitiveState): Record<string, unknown> {
  const coordinateSystem = primitive.geometry.coordinate_system ?? primitive.coordinate_system;
  if (!coordinateSystem) {
    return primitive.geometry;
  }
  return { ...primitive.geometry, coordinate_system: coordinateSystem };
}

function hasSpatialGeometry(primitive: RuntimeOverlayPrimitiveState) {
  const geometry = overlayGeometry(primitive);
  if (primitive.primitive_type === 'heatmap' || primitive.primitive_type === 'mask') {
    return typeof geometry.data === 'string' && geometry.data.length > 0;
  }
  if (primitive.primitive_type === 'polygon') {
    return Array.isArray(geometry.points);
  }
  if (
    primitive.primitive_type === 'line' ||
    primitive.primitive_type === 'axis' ||
    primitive.primitive_type === 'arrow'
  ) {
    return lineGeometry(geometry, null, null) !== null;
  }
  if (primitive.primitive_type === 'point' || primitive.primitive_type === 'motion') {
    return numericValue(geometry.x) !== null && numericValue(geometry.y) !== null;
  }
  return boxGeometryStyle(geometry, null, null) !== null;
}

function drawCanvasPrimitive(
  context: CanvasRenderingContext2D,
  primitive: RuntimeOverlayPrimitiveState,
  canvasWidth: number,
  canvasHeight: number,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const geometry = overlayGeometry(primitive);
  if (primitive.primitive_type === 'polygon') {
    drawCanvasPolygon(context, primitive, geometry, canvasWidth, canvasHeight, frameWidth, frameHeight);
    return;
  }
  if (
    primitive.primitive_type === 'line' ||
    primitive.primitive_type === 'axis' ||
    primitive.primitive_type === 'arrow'
  ) {
    drawCanvasLine(context, primitive, geometry, canvasWidth, canvasHeight, frameWidth, frameHeight);
    return;
  }
  if (primitive.primitive_type === 'point' || primitive.primitive_type === 'motion') {
    drawCanvasPoint(context, primitive, geometry, canvasWidth, canvasHeight, frameWidth, frameHeight);
    return;
  }
  drawCanvasBox(context, primitive, geometry, canvasWidth, canvasHeight, frameWidth, frameHeight);
}

function drawCanvasPolygon(
  context: CanvasRenderingContext2D,
  primitive: RuntimeOverlayPrimitiveState,
  geometry: Record<string, unknown>,
  canvasWidth: number,
  canvasHeight: number,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const points = polygonPointPairs(geometry, frameWidth, frameHeight);
  if (points.length < 3) {
    return;
  }
  const stroke = canvasColor(primitive.style.stroke, defaultOverlayStroke(primitive));
  const fill = canvasColor(primitive.style.fill, defaultOverlayFill(primitive));
  context.save();
  context.beginPath();
  points.forEach((point, index) => {
    const x = percentToCanvas(point.x, canvasWidth);
    const y = percentToCanvas(point.y, canvasHeight);
    if (index === 0) {
      context.moveTo(x, y);
    } else {
      context.lineTo(x, y);
    }
  });
  context.closePath();
  context.fillStyle = fill;
  context.strokeStyle = stroke;
  context.lineWidth = canvasStrokeWidth(primitive, 2);
  context.fill();
  context.stroke();
  context.restore();

  const label = overlayPrimitiveLabel(primitive);
  if (label) {
    const anchorX = Math.min(...points.map((point) => percentToCanvas(point.x, canvasWidth)));
    const anchorY = Math.min(...points.map((point) => percentToCanvas(point.y, canvasHeight)));
    drawCanvasLabel(
      context,
      label,
      anchorX,
      anchorY + 16,
      canvasWidth,
      canvasHeight,
      stroke
    );
  }
}

function drawCanvasLine(
  context: CanvasRenderingContext2D,
  primitive: RuntimeOverlayPrimitiveState,
  geometry: Record<string, unknown>,
  canvasWidth: number,
  canvasHeight: number,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const line = lineGeometry(geometry, frameWidth, frameHeight);
  if (!line) {
    return;
  }
  const x1 = percentToCanvas(line.x1, canvasWidth);
  const y1 = percentToCanvas(line.y1, canvasHeight);
  const x2 = percentToCanvas(line.x2, canvasWidth);
  const y2 = percentToCanvas(line.y2, canvasHeight);
  const stroke = canvasColor(primitive.style.stroke, defaultOverlayStroke(primitive));
  context.save();
  context.strokeStyle = stroke;
  context.fillStyle = stroke;
  context.lineWidth = canvasStrokeWidth(primitive, 2);
  context.beginPath();
  context.moveTo(x1, y1);
  context.lineTo(x2, y2);
  context.stroke();
  if (primitive.primitive_type === 'arrow') {
    drawCanvasArrowHead(context, x1, y1, x2, y2);
  }
  const label = overlayPrimitiveLabel(primitive);
  if (label) {
    drawCanvasLabel(
      context,
      label,
      percentToCanvas(line.labelX, canvasWidth),
      percentToCanvas(line.labelY, canvasHeight),
      canvasWidth,
      canvasHeight,
      stroke
    );
  }
  context.restore();
}

function drawCanvasPoint(
  context: CanvasRenderingContext2D,
  primitive: RuntimeOverlayPrimitiveState,
  geometry: Record<string, unknown>,
  canvasWidth: number,
  canvasHeight: number,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const point = pointGeometryPercent(geometry, frameWidth, frameHeight);
  if (!point) {
    return;
  }
  const x = percentToCanvas(point.x, canvasWidth);
  const y = percentToCanvas(point.y, canvasHeight);
  const stroke = canvasColor(primitive.style.stroke, '#ffffff');
  const fill = canvasColor(primitive.style.fill, defaultOverlayStroke(primitive));
  context.save();
  context.fillStyle = fill;
  context.strokeStyle = stroke;
  context.lineWidth = 2;
  context.beginPath();
  context.arc(x, y, 5, 0, Math.PI * 2);
  context.fill();
  context.stroke();
  const label = overlayPrimitiveLabel(primitive);
  if (label) {
    drawCanvasLabel(context, label, x + 8, y - 8, canvasWidth, canvasHeight, fill);
  }
  context.restore();
}

function drawCanvasBox(
  context: CanvasRenderingContext2D,
  primitive: RuntimeOverlayPrimitiveState,
  geometry: Record<string, unknown>,
  canvasWidth: number,
  canvasHeight: number,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const box = boxGeometryPercent(geometry, frameWidth, frameHeight);
  if (!box) {
    return;
  }
  const x = percentToCanvas(box.left, canvasWidth);
  const y = percentToCanvas(box.top, canvasHeight);
  const width = percentToCanvas(box.width, canvasWidth);
  const height = percentToCanvas(box.height, canvasHeight);
  const stroke = canvasColor(primitive.style.stroke, defaultOverlayStroke(primitive));
  const fill = canvasColor(primitive.style.fill, defaultOverlayFill(primitive));
  context.save();
  context.translate(x + width / 2, y + height / 2);
  context.rotate((box.angle * Math.PI) / 180);
  context.fillStyle = fill;
  context.strokeStyle = stroke;
  context.lineWidth = canvasStrokeWidth(primitive, 2);
  context.fillRect(-width / 2, -height / 2, width, height);
  context.strokeRect(-width / 2, -height / 2, width, height);
  context.restore();

  const label = overlayPrimitiveLabel(primitive);
  if (label) {
    drawCanvasLabel(
      context,
      label,
      x,
      Math.max(y - 6, 14),
      canvasWidth,
      canvasHeight,
      stroke
    );
  }
}

function drawCanvasArrowHead(
  context: CanvasRenderingContext2D,
  x1: number,
  y1: number,
  x2: number,
  y2: number
) {
  const angle = Math.atan2(y2 - y1, x2 - x1);
  const size = 9;
  context.save();
  context.translate(x2, y2);
  context.rotate(angle);
  context.beginPath();
  context.moveTo(0, 0);
  context.lineTo(-size, size * 0.45);
  context.lineTo(-size, -size * 0.45);
  context.closePath();
  context.fill();
  context.restore();
}

function drawCanvasLabel(
  context: CanvasRenderingContext2D,
  label: string,
  x: number,
  y: number,
  canvasWidth: number,
  canvasHeight: number,
  backgroundColor = '#ffffff'
) {
  const text = label.trim();
  if (!text) {
    return;
  }
  const paddingX = 6;
  const paddingY = 3;
  const metrics = context.measureText(text);
  const width = Math.min(metrics.width + paddingX * 2, Math.max(canvasWidth - 8, 24));
  const left = Math.min(Math.max(x, 4), Math.max(canvasWidth - width - 4, 4));
  const top = Math.min(Math.max(y - 15, 4), Math.max(canvasHeight - 23, 4));
  context.save();
  context.fillStyle = backgroundColor;
  context.fillRect(left, top, width, 19);
  context.fillStyle = overlayLabelTextColor(backgroundColor);
  context.fillText(text, left + paddingX, top + 14 + paddingY / 2);
  context.restore();
}

function canvasColor(value: unknown, fallback: string) {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function canvasStrokeWidth(primitive: RuntimeOverlayPrimitiveState, fallback: number) {
  const strokeWidth = numericValue(primitive.style.stroke_width);
  return Math.max(strokeWidth ?? fallback, 1);
}

function defaultOverlayStroke(primitive: RuntimeOverlayPrimitiveState) {
  if (primitive.primitive_type === 'arrow') {
    return '#ffffff';
  }
  if (primitive.primitive_type === 'axis') {
    return '#49c47a';
  }
  if (primitive.layer === 'zones') {
    return '#f4b740';
  }
  if (isClassColoredPrimitive(primitive)) {
    return overlayClassColors(primitive.label ?? '').stroke;
  }
  if (primitive.layer === 'rois') {
    return '#5db7ff';
  }
  return '#49c47a';
}

function defaultOverlayFill(primitive: RuntimeOverlayPrimitiveState) {
  if (primitive.layer === 'zones') {
    return 'rgba(244, 183, 64, 0.12)';
  }
  if (isClassColoredPrimitive(primitive)) {
    return overlayClassColors(primitive.label ?? '').fill;
  }
  if (primitive.layer === 'rois') {
    return 'rgba(93, 183, 255, 0.12)';
  }
  return primitive.primitive_type === 'polygon' ? 'rgba(93, 183, 255, 0.18)' : 'rgba(73, 196, 122, 0.12)';
}

export function overlayPrimitiveLabel(primitive: RuntimeOverlayPrimitiveState) {
  if (!primitive.label) {
    return null;
  }
  if (primitive.confidence === null || primitive.confidence === undefined) {
    return primitive.label;
  }
  const confidencePercent = Math.min(Math.max(primitive.confidence * 100, 0), 100);
  return `${primitive.label} · ${confidencePercent.toFixed(1)}%`;
}

const OVERLAY_CLASS_COLORS = [
  { stroke: '#00d9ff', fill: 'rgba(0, 217, 255, 0.18)' },
  { stroke: '#ff9f1c', fill: 'rgba(255, 159, 28, 0.18)' },
  { stroke: '#ff4d9d', fill: 'rgba(255, 77, 157, 0.18)' },
  { stroke: '#4ade80', fill: 'rgba(74, 222, 128, 0.18)' },
  { stroke: '#a78bfa', fill: 'rgba(167, 139, 250, 0.18)' },
  { stroke: '#facc15', fill: 'rgba(250, 204, 21, 0.18)' },
  { stroke: '#fb5d5d', fill: 'rgba(251, 93, 93, 0.18)' },
  { stroke: '#60a5fa', fill: 'rgba(96, 165, 250, 0.18)' },
  { stroke: '#2dd4bf', fill: 'rgba(45, 212, 191, 0.18)' },
  { stroke: '#f472b6', fill: 'rgba(244, 114, 182, 0.18)' },
  { stroke: '#bef264', fill: 'rgba(190, 242, 100, 0.18)' },
  { stroke: '#c084fc', fill: 'rgba(192, 132, 252, 0.18)' }
] as const;

export function overlayClassColors(label: string) {
  const normalized = label.trim().toLowerCase();
  let hash = 2166136261;
  for (let index = 0; index < normalized.length; index += 1) {
    hash ^= normalized.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return OVERLAY_CLASS_COLORS[(hash >>> 0) % OVERLAY_CLASS_COLORS.length];
}

function isClassColoredPrimitive(primitive: RuntimeOverlayPrimitiveState) {
  if (!primitive.label) {
    return false;
  }
  const metadataLayer =
    typeof primitive.metadata.layer === 'string' ? primitive.metadata.layer : '';
  return (
    primitive.layer === 'detections' ||
    primitive.layer === 'segmentation' ||
    metadataLayer === 'detections' ||
    metadataLayer === 'segmentation'
  );
}

function overlayLabelTextColor(backgroundColor: string) {
  const hexMatch = backgroundColor.trim().match(/^#([0-9a-f]{6})$/i);
  if (!hexMatch) {
    return '#ffffff';
  }
  const value = Number.parseInt(hexMatch[1], 16);
  const red = (value >> 16) & 0xff;
  const green = (value >> 8) & 0xff;
  const blue = value & 0xff;
  const luminance = (red * 299 + green * 587 + blue * 114) / 1000;
  return luminance >= 155 ? '#102018' : '#ffffff';
}

function percentToCanvas(value: number, canvasSize: number) {
  return (value / 100) * canvasSize;
}

function boxGeometryStyle(
  geometry: Record<string, unknown>,
  frameWidth: number | null,
  frameHeight: number | null
): CSSProperties | null {
  const box = boxGeometryPercent(geometry, frameWidth, frameHeight);
  if (!box) {
    return null;
  }
  return {
    left: `${box.left}%`,
    top: `${box.top}%`,
    width: `${box.width}%`,
    height: `${box.height}%`,
    transform: box.angle ? `rotate(${box.angle}deg)` : undefined,
    transformOrigin: '50% 50%'
  };
}

function boxGeometryPercent(
  geometry: Record<string, unknown>,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const pixelGeometry = isPixelCoordinateSystem(geometry.coordinate_system);
  const xMin = numericValue(geometry.x_min ?? geometry.left ?? geometry.x);
  const yMin = numericValue(geometry.y_min ?? geometry.top ?? geometry.y);
  let xMax = numericValue(geometry.x_max ?? geometry.right);
  let yMax = numericValue(geometry.y_max ?? geometry.bottom);
  const width = numericValue(geometry.width ?? geometry.w);
  const height = numericValue(geometry.height ?? geometry.h);
  if (xMin === null || yMin === null) {
    return null;
  }
  xMax = xMax ?? (width !== null ? xMin + width : null);
  yMax = yMax ?? (height !== null ? yMin + height : null);
  if (xMax === null || yMax === null || xMax <= xMin || yMax <= yMin) {
    return null;
  }
  const left = toPercent(xMin, frameWidth, pixelGeometry);
  const top = toPercent(yMin, frameHeight, pixelGeometry);
  const right = toPercent(xMax, frameWidth, pixelGeometry);
  const bottom = toPercent(yMax, frameHeight, pixelGeometry);
  const angle = geometryAngleDegrees(geometry);
  return {
    left,
    top,
    width: Math.max(right - left, 0.1),
    height: Math.max(bottom - top, 0.1),
    angle
  };
}

function pointStyle(
  geometry: Record<string, unknown>,
  frameWidth: number | null,
  frameHeight: number | null
): CSSProperties | null {
  const point = pointGeometryPercent(geometry, frameWidth, frameHeight);
  if (!point) {
    return null;
  }
  return {
    left: `${point.x}%`,
    top: `${point.y}%`
  };
}

function pointGeometryPercent(
  geometry: Record<string, unknown>,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const pixelGeometry = isPixelCoordinateSystem(geometry.coordinate_system);
  const x = numericValue(geometry.x ?? geometry.cx);
  const y = numericValue(geometry.y ?? geometry.cy);
  if (x === null || y === null) {
    return null;
  }
  return {
    x: toPercent(x, frameWidth, pixelGeometry),
    y: toPercent(y, frameHeight, pixelGeometry)
  };
}

function polygonPoints(
  geometry: Record<string, unknown>,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const points = polygonPointPairs(geometry, frameWidth, frameHeight);
  return points.length >= 3 ? points.map((point) => `${point.x},${point.y}`).join(' ') : null;
}

function polygonPointPairs(
  geometry: Record<string, unknown>,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const pixelGeometry = isPixelCoordinateSystem(geometry.coordinate_system);
  const rawPoints = Array.isArray(geometry.points) ? geometry.points : [];
  return rawPoints
    .map((point) => {
      if (Array.isArray(point)) {
        const x = numericValue(point[0]);
        const y = numericValue(point[1]);
        return x === null || y === null
          ? null
          : {
              x: toPercent(x, frameWidth, pixelGeometry),
              y: toPercent(y, frameHeight, pixelGeometry)
            };
      }
      if (point && typeof point === 'object') {
        const item = point as Record<string, unknown>;
        const x = numericValue(item.x);
        const y = numericValue(item.y);
        return x === null || y === null
          ? null
          : {
              x: toPercent(x, frameWidth, pixelGeometry),
              y: toPercent(y, frameHeight, pixelGeometry)
            };
      }
      return null;
    })
    .filter((point): point is { x: number; y: number } => Boolean(point));
}

function lineGeometry(
  geometry: Record<string, unknown>,
  frameWidth: number | null,
  frameHeight: number | null
) {
  const pixelGeometry = isPixelCoordinateSystem(geometry.coordinate_system);
  const start = geometry.start && typeof geometry.start === 'object'
    ? geometry.start as Record<string, unknown>
    : null;
  const end = geometry.end && typeof geometry.end === 'object'
    ? geometry.end as Record<string, unknown>
    : null;
  const x1 = numericValue(start?.x ?? geometry.x1);
  const y1 = numericValue(start?.y ?? geometry.y1);
  const x2 = numericValue(end?.x ?? geometry.x2);
  const y2 = numericValue(end?.y ?? geometry.y2);
  if (x1 === null || y1 === null || x2 === null || y2 === null) {
    return null;
  }
  const startX = toPercent(x1, frameWidth, pixelGeometry);
  const startY = toPercent(y1, frameHeight, pixelGeometry);
  const endX = toPercent(x2, frameWidth, pixelGeometry);
  const endY = toPercent(y2, frameHeight, pixelGeometry);
  return {
    x1: startX,
    y1: startY,
    x2: endX,
    y2: endY,
    labelX: (startX + endX) / 2,
    labelY: Math.max((startY + endY) / 2 - 1.5, 2)
  };
}

function numericValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function geometryAngleDegrees(geometry: Record<string, unknown>) {
  const parsed = numericValue(geometry.angle_degrees ?? geometry.rotation_degrees ?? geometry.angle) ?? 0;
  const normalized = ((((parsed + 180) % 360) + 360) % 360) - 180;
  return Object.is(normalized, -0) ? 0 : normalized;
}

function isPixelCoordinateSystem(value: unknown) {
  const coordinateSystem = typeof value === 'string' ? value.toLowerCase() : '';
  return (
    coordinateSystem === 'pixel_xy' ||
    coordinateSystem === 'image_pixels' ||
    coordinateSystem === 'pixels' ||
    coordinateSystem === 'pixel'
  );
}

function toPercent(value: number, frameSize: number | null, pixelGeometry = false) {
  if (pixelGeometry && frameSize && frameSize > 0) {
    return Math.min(Math.max((value / frameSize) * 100, 0), 100);
  }
  if (value >= 0 && value <= 1) {
    return value * 100;
  }
  if (frameSize && value > 100) {
    return Math.min(Math.max((value / frameSize) * 100, 0), 100);
  }
  return Math.min(Math.max(value, 0), 100);
}
