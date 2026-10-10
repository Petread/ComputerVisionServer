import { Icon, Modal, Search, ToolButton } from '../../components/workspace-ui';
import { useUnsavedWorkspace } from '../../components/use-unsaved-workspace';
import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type CSSProperties
} from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  LabelProjectSummary,
  RuntimeCameraCalibrationBoardDefinition,
  RuntimeCameraCalibrationCalculateResponse,
  RuntimeCameraCalibrationObservation,
  RuntimeCameraIntrinsicCalibrationProfile,
  RuntimeImageViewProfile,
  RuntimeImageViewProfileCalculateResponse,
  RuntimeNormalizedImagePoint,
  RuntimeStemCurvatureImageGate,
  RuntimeStemCurvatureImagePoint,
  RuntimeStemCurvatureProcessCalibrationCalculateResponse,
  RuntimeStemCurvatureProcessCalibrationProfile,
  RuntimeStemCurvatureCoordinateFrameDefinition,
  RuntimeStemCurvatureProcessReferencePoint,
  RuntimeStemCurvatureReferencePlane,
  RuntimeStemSurfacePhaseCalibrationCalculateResponse,
  RuntimeStemSurfacePhaseCalibrationObservation,
  RuntimeStemSurfacePhaseCalibrationProfile,
  RuntimeStemMinimumPrincipleValidationArtifact,
  RuntimeStemMinimumPrincipleValidationCalculateResponse,
  RuntimeStemMinimumPrincipleValidationObservation,
  RuntimeStemMinimumPrincipleValidationThresholds,
  RuntimeChannelAuthoringContext,
  RuntimeChannelSummary,
  RuntimeGraphEdge,
  RuntimeLiveFrame,
  RuntimeLiveViewState,
  RuntimeLogicalModelRef,
  RuntimeMode,
  RuntimeModelCapabilityOption,
  RuntimeNodeCategory,
  RuntimeNodePort,
  RuntimeNodePropertySpec,
  RuntimeNodeSpec,
  RuntimeNodeTypeSummary,
  RuntimeOverlayPrimitiveState,
  RuntimePipelineAuthoringGraph,
  RuntimePipelineDefinitionSummary,
  RuntimePipelineGraph,
  RuntimePipelineOutputCatalog,
  RuntimePipelineOutputCatalogEntry,
  RuntimePipelineOutputCatalogField,
  RuntimePipelineValidationReport,
  RuntimeProjectionDestination,
  RuntimeResultProjection,
  TaskType
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { BarkInspectionWorkspace } from './bark-inspection-workspace';
import { PipelineTrainingCapture } from './pipeline-training-capture';
import { describeError } from '../../app/errors';
import { AppIcon, type AppIconName } from '../../components/app-icon';
import { useNotifications } from '../../components/notification-provider';
import {
  StemCurvaturePhaseRuntimePanel
} from '../stem-orientation/curvature-phase-diagnostic-graph';

type RuntimePipelineStatusFilter = RuntimePipelineDefinitionSummary['status'] | 'all';

const categoryOrder: RuntimeNodeCategory[] = [
  'trigger',
  'source',
  'preprocessing',
  'computer_vision',
  'inference',
  'analysis',
  'capture',
  'output'
];
const guidedBuilderCategorySections: RuntimeNodeCategory[] = [
  'preprocessing',
  'computer_vision',
  'inference',
  'analysis',
  'capture',
  'output'
];
const runtimePipelineStatusOptions: RuntimePipelineStatusFilter[] = [
  'all',
  'draft',
  'validated',
  'published',
  'superseded',
  'archived'
];
const nodeWidth = 190;
const nodeHeight = 76;
const MAX_TEST_PHOTO_BYTES = 5 * 1024 * 1024;
const MAX_TEST_PHOTO_SEQUENCE_TOTAL_BYTES = 100 * 1024 * 1024;

type UploadedPhoto = {
  data_url: string;
  name: string;
  mime_type: string;
  size: number;
};

const emptyGraph: RuntimePipelineGraph = { nodes: [], edges: [] };

function normalizeUploadedPhoto(value: unknown): UploadedPhoto | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = value as Record<string, unknown>;
  const dataUrl = typeof candidate.data_url === 'string' ? candidate.data_url : '';
  if (!dataUrl.startsWith('data:image/')) return null;
  return {
    data_url: dataUrl,
    name: typeof candidate.name === 'string' && candidate.name ? candidate.name : 'test-photo',
    mime_type: typeof candidate.mime_type === 'string' ? candidate.mime_type : '',
    size: typeof candidate.size === 'number' && candidate.size >= 0 ? candidate.size : 0
  };
}

function normalizeUploadedPhotos(value: unknown): UploadedPhoto[] {
  if (!Array.isArray(value)) return [];
  return value.map(normalizeUploadedPhoto).filter((photo): photo is UploadedPhoto => Boolean(photo)).slice(0, 100);
}

function readUploadedPhoto(file: File): Promise<UploadedPhoto> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Could not read ${file.name}.`));
    reader.onload = () => {
      if (typeof reader.result !== 'string') {
        reject(new Error(`Could not read ${file.name}.`));
        return;
      }
      resolve({ data_url: reader.result, name: file.name, mime_type: file.type, size: file.size });
    };
    reader.readAsDataURL(file);
  });
}

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

export function graphToAuthoringGraph(graph: RuntimePipelineGraph): RuntimePipelineAuthoringGraph {
  return {
    nodes: graph.nodes.map((node) => ({
      node_key: node.node_key,
      node_type: node.node_type,
      config: node.config,
      logical_model_ref: node.logical_model_ref ?? null,
      position: node.position
    })),
    edges: graph.edges
  };
}

export function applyImageViewProfileToGraph(
  graph: RuntimePipelineGraph,
  nodeKey: string,
  fieldKey: string,
  value: unknown
): RuntimePipelineGraph {
  const target = graph.nodes.find((node) => node.node_key === nodeKey);
  if (!target) return graph;

  const profile = normalizeImageViewProfile(value);
  if (!profile) {
    return {
      ...graph,
      nodes: graph.nodes.map((node) =>
        node.node_key === nodeKey
          ? { ...node, config: { ...node.config, [fieldKey]: value } }
          : node
      )
    };
  }

  const previousProfile = normalizeImageViewProfile(target.config[fieldKey]);
  const previousIds = new Set(
    [previousProfile?.view_profile_id, String(target.config.view_profile_id ?? '').trim()].filter(
      (item): item is string => Boolean(item)
    )
  );
  const downstream = new Set<string>();
  const pending = [nodeKey];
  while (pending.length) {
    const current = pending.shift();
    if (!current) continue;
    for (const edge of graph.edges) {
      if (edge.from_node_key !== current || downstream.has(edge.to_node_key)) continue;
      downstream.add(edge.to_node_key);
      pending.push(edge.to_node_key);
    }
  }

  return {
    ...graph,
    nodes: graph.nodes.map((node) => {
      if (node.node_key === nodeKey) {
        return {
          ...node,
          config: {
            ...node.config,
            [fieldKey]: value,
            view_profile_id: profile.view_profile_id
          }
        };
      }
      const configuredId = String(node.config.view_profile_id ?? '').trim();
      if (
        downstream.has(node.node_key) &&
        typeof node.config.view_profile_id === 'string' &&
        (!configuredId || previousIds.has(configuredId))
      ) {
        return {
          ...node,
          config: { ...node.config, view_profile_id: profile.view_profile_id }
        };
      }
      return node;
    })
  };
}

type DragPayload =
  | { kind: 'new-node'; nodeType: string }
  | { kind: 'move-node'; nodeKey: string; offsetX: number; offsetY: number };

export type RuntimePipelineDesignerPageProps = {
  active?: boolean;
  embeddedChannel?: {
    channelId: string;
    channelName?: string | null;
    pipelineVersionId?: string | null;
  };
};

export function RuntimePipelineDesignerPage(props: RuntimePipelineDesignerPageProps = {}) {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [workspaceDialog, setWorkspaceDialog] = useState<'settings' | 'guided' | null>(null);
  const [paletteOpen, setPaletteOpen] = useState(true);
  const [barkInspectionOpen, setBarkInspectionOpen] = useState(false);
  const [selectedDefinitionId, setSelectedDefinitionId] = useState<string>('');
  const [selectedNodeKey, setSelectedNodeKey] = useState<string | null>(null);
  const [selectedEdgeKey, setSelectedEdgeKey] = useState<string | null>(null);
  const [mode, setMode] = useState<RuntimeMode>('realtime');
  const [graph, setGraph] = useState<RuntimePipelineGraph>(emptyGraph);
  const [trainingCapture, setTrainingCapture] = useState<{ graph: RuntimePipelineGraph; execution: Record<string, unknown> } | null>(null);
  const captureGraphRef = useRef(graph);
  captureGraphRef.current = graph;
  const captureChannelRef = useRef(props.embeddedChannel?.channelId);
  captureChannelRef.current = props.embeddedChannel?.channelId;
  useEffect(() => { setTrainingCapture(null); }, [graph, props.embeddedChannel?.channelId]);
  const [outputCatalog, setOutputCatalog] = useState<RuntimePipelineOutputCatalog | null>(null);
  const [validationReport, setValidationReport] = useState<RuntimePipelineValidationReport | null>(null);
  const [lastSavedVersionId, setLastSavedVersionId] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  useUnsavedWorkspace(dirty);
  const [newPipelineName, setNewPipelineName] = useState('');
  const [newPipelineDescription, setNewPipelineDescription] = useState('');
  const [selectedTemplateId, setSelectedTemplateId] = useState('');
  const [changeNote, setChangeNote] = useState('Operator visual update');
  const [pipelineSearch, setPipelineSearch] = useState('');
  const [pipelineStatusFilter, setPipelineStatusFilter] = useState<RuntimePipelineStatusFilter>('all');
  const [pipelineUsageFilter, setPipelineUsageFilter] = useState<'all' | 'used' | 'unused'>('all');
  const [previewLiveState, setPreviewLiveState] = useState<RuntimeLiveViewState | null>(null);
  const [commissioningPreviewLiveState, setCommissioningPreviewLiveState] =
    useState<RuntimeLiveViewState | null>(null);
  const [frozenBoardEndPreviewFrame, setFrozenBoardEndPreviewFrame] =
    useState<RuntimeLiveFrame | null>(null);
  const [capturedCalibrationLiveState, setCapturedCalibrationLiveState] =
    useState<RuntimeLiveViewState | null>(null);
  const lastAutomaticPreviewSignature = useRef('');
  const latestDesiredPreviewSignature = useRef('');
  const lastCommissioningPreviewSignature = useRef('');
  const latestDesiredCommissioningPreviewSignature = useRef('');

  const nodeTypesQuery = useQuery({
    queryKey: ['runtime-node-types'],
    queryFn: () => api.runtimePipelines.nodeTypes()
  });
  const definitionsQuery = useQuery({
    queryKey: props.embeddedChannel
      ? ['runtime-pipeline-definitions', 'designer', props.embeddedChannel.channelId]
      : ['runtime-pipeline-definitions', 'designer'],
    queryFn: () =>
      props.embeddedChannel
        ? api.runtimePipelines.list({ includeTemplates: true })
        : api.runtimePipelines.list({ includeTemplates: true, scope: 'template' })
  });
  const modelCapabilitiesQuery = useQuery({
    queryKey: ['runtime-model-capabilities'],
    queryFn: () => api.runtimePipelines.modelCapabilities()
  });
  const labelProjectsQuery = useQuery({
    queryKey: ['label-projects'],
    queryFn: () => api.datasets.projects()
  });
  const channelsQuery = useQuery({
    queryKey: ['runtime-channels', 'pipeline-designer'],
    queryFn: () => api.channels.list(),
    staleTime: 30000
  });
  const detailQuery = useQuery({
    queryKey: ['runtime-pipeline-definition', selectedDefinitionId],
    queryFn: () => api.runtimePipelines.detail(selectedDefinitionId),
    enabled: Boolean(selectedDefinitionId)
  });
  const authoringLiveViewQuery = useQuery({
    queryKey: ['runtime-channel-authoring-live-view', props.embeddedChannel?.channelId],
    queryFn: () =>
      api.channels.liveViewLatest(props.embeddedChannel!.channelId, {
        maxFrameWidth: 1024,
        includeFrame: true
      }),
    enabled: props.active !== false && Boolean(props.embeddedChannel?.channelId),
    refetchInterval: props.active === false ? false : graph.nodes.some(
      (node) =>
        node.node_key === selectedNodeKey &&
        node.node_type === 'analysis.board_end_coordinate_frame'
    )
      ? 1000
      : 5000
  });
  const boardCalibrationChannelId =
    selectedBoardCalibrationChannelId(graph, selectedNodeKey) ??
    props.embeddedChannel?.channelId ??
    null;
  const intrinsicCalibrationSelected = graph.nodes.some(
    (node) =>
      node.node_key === selectedNodeKey &&
      node.node_type === 'preprocessing.camera_undistort'
  );
  const canonicalViewCalibrationSelected = graph.nodes.some(
    (node) =>
      node.node_key === selectedNodeKey &&
      (node.node_type === 'preprocessing.process_coordinate_rectify' ||
        node.node_type === 'analysis.stem_measurement_eligibility')
  );
  const fullResolutionCalibrationSourceSelected =
    intrinsicCalibrationSelected || canonicalViewCalibrationSelected;
  const boardCalibrationLiveViewQuery = useQuery({
    queryKey: [
      'runtime-board-calibration-live-view',
      boardCalibrationChannelId,
      fullResolutionCalibrationSourceSelected ? 'full-resolution-source' : 'preview'
    ],
    queryFn: () =>
      api.channels.liveViewLatest(boardCalibrationChannelId!, {
        // Intrinsic observations must use the exact source resolution. A
        // resized preview would produce a profile that fails closed at runtime.
        maxFrameWidth: fullResolutionCalibrationSourceSelected ? undefined : 1024,
        includeFrame: true,
        // camera-media owns the camera before the inference pipeline is valid.
        // Its raw shared-memory frame breaks the calibration/startup cycle.
        preferCameraStreamFrame: fullResolutionCalibrationSourceSelected
      }),
    enabled: props.active !== false && Boolean(boardCalibrationChannelId),
    refetchInterval: props.active === false ? false : fullResolutionCalibrationSourceSelected ? 1000 : 5000
  });
  useEffect(() => {
    setCapturedCalibrationLiveState(null);
  }, [boardCalibrationChannelId]);
  useEffect(() => {
    setFrozenBoardEndPreviewFrame(null);
    setCommissioningPreviewLiveState(null);
    lastAutomaticPreviewSignature.current = '';
    latestDesiredPreviewSignature.current = '';
    lastCommissioningPreviewSignature.current = '';
    latestDesiredCommissioningPreviewSignature.current = '';
  }, [props.embeddedChannel?.channelId, selectedNodeKey]);
  const calibrationCaptureMutation = useMutation({
    mutationFn: (channelId: string) => api.channels.captureCalibrationFrame(channelId),
    onSuccess: (response) => {
      setCapturedCalibrationLiveState(response.state);
      const capturedFromRunningStream =
        response.state.frame?.metadata.calibration_capture_mode === 'camera_media_stream';
      notify({
        title: 'Calibration frame captured',
        description: capturedFromRunningStream
          ? 'The frame was copied from the running camera stream; the stream remained active.'
          : 'The camera was released immediately after the single frame.',
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Calibration capture failed'), tone: 'error' });
    }
  });
  const authoringContextQuery = useQuery({
    queryKey: ['runtime-channel-authoring-context', props.embeddedChannel?.channelId],
    queryFn: () => api.channels.authoringContext(props.embeddedChannel!.channelId),
    enabled: Boolean(props.embeddedChannel?.channelId),
    staleTime: 30000
  });

  const nodeTypes = nodeTypesQuery.data?.items ?? [];
  const modelCapabilities = modelCapabilitiesQuery.data?.items ?? [];
  const labelProjects = labelProjectsQuery.data?.items ?? [];
  const channels = channelsQuery.data?.items ?? [];
  const authoringContext = authoringContextQuery.data?.context ?? null;
  const nodeTypeMap = useMemo(
    () => new Map(nodeTypes.map((item) => [item.node_type, item])),
    [nodeTypes]
  );
  const definitions = definitionsQuery.data?.items ?? [];
  const templateDefinitions = definitions.filter((item) => item.scope === 'template');
  const channelInstanceDefinitions = props.embeddedChannel
    ? definitions.filter(
        (item) =>
          item.scope === 'channel_instance' &&
          item.owner_channel_id === props.embeddedChannel?.channelId
      )
    : [];
  const templates = templateDefinitions.filter((item) => item.template);
  const channelTemplateDefinitions = templateDefinitions.filter(
    (item) => Boolean(item.published_version_id || item.latest_version_id)
  );
  const reusableDefinitions = templateDefinitions.filter((item) => !item.template);
  const selectedDefinition = definitions.find(
    (item) => item.pipeline_definition_id === selectedDefinitionId
  );
  const selectedDefinitionIsEditable = Boolean(
    selectedDefinition &&
      (!props.embeddedChannel ||
        (selectedDefinition.scope === 'channel_instance' &&
          selectedDefinition.owner_channel_id === props.embeddedChannel.channelId))
  );
  const embeddedPipelineDefinition = props.embeddedChannel
    ? props.embeddedChannel.pipelineVersionId
      ? channelInstanceDefinitions.find((definition) =>
          [
            definition.published_version_id,
            definition.latest_version_id
          ].includes(props.embeddedChannel?.pipelineVersionId ?? null)
        ) ?? channelInstanceDefinitions[0] ?? null
      : channelInstanceDefinitions[0] ?? null
    : null;
  const selectedPipelineChannels = selectedDefinition
    ? channelsUsingDefinition(selectedDefinition, channels)
    : [];
  const filteredReusableDefinitions = useMemo(
    () =>
      reusableDefinitions.filter((definition) => {
        const usedChannels = channelsUsingDefinition(definition, channels);
        const query = pipelineSearch.trim().toLocaleLowerCase();
        const matchesSearch =
          !query ||
          [
            definition.name,
            definition.description ?? '',
            definition.slug,
            definition.status,
            ...usedChannels.map((channel) => channel.name)
          ]
            .join(' ')
            .toLocaleLowerCase()
            .includes(query);
        const matchesStatus =
          pipelineStatusFilter === 'all' || definition.status === pipelineStatusFilter;
        const matchesUsage =
          pipelineUsageFilter === 'all' ||
          (pipelineUsageFilter === 'used' ? usedChannels.length > 0 : usedChannels.length === 0);
        return matchesSearch && matchesStatus && matchesUsage;
      }),
    [channels, pipelineSearch, pipelineStatusFilter, pipelineUsageFilter, reusableDefinitions]
  );
  const selectedNode = graph.nodes.find((node) => node.node_key === selectedNodeKey) ?? null;
  const selectedNodeType = selectedNode ? nodeTypeMap.get(selectedNode.node_type) ?? null : null;
  const effectiveMode = inferGraphMode(graph, mode);
  const graphWithReconciledClassRules = useMemo(
    () => reconcileBoardCutClassRulesInGraph(graph, modelCapabilities),
    [graph, modelCapabilities]
  );
  const authoringGraph = useMemo(
    () => graphToAuthoringGraph(graphWithReconciledClassRules),
    [graphWithReconciledClassRules]
  );

  useEffect(() => {
    if (
      selectedDefinitionId &&
      definitionsQuery.isSuccess &&
      !definitions.some((definition) => definition.pipeline_definition_id === selectedDefinitionId)
    ) {
      setSelectedDefinitionId('');
      setSelectedNodeKey(null);
      setSelectedEdgeKey(null);
      setGraph(emptyGraph);
      setOutputCatalog(null);
      setValidationReport(null);
      setLastSavedVersionId(null);
      setDirty(false);
    }
  }, [definitions, definitionsQuery.isSuccess, selectedDefinitionId]);

  useEffect(() => {
    const detail = detailQuery.data;
    if (!detail) {
      return;
    }
    const latest = detail.latest_version;
    setMode(latest?.mode ?? 'realtime');
    setGraph(latest?.graph ?? emptyGraph);
    setOutputCatalog(null);
    setValidationReport(latest?.validation_report ?? null);
    setLastSavedVersionId(latest?.pipeline_version_id ?? null);
    setSelectedNodeKey(null);
    setSelectedEdgeKey(null);
    setDirty(false);
    setPreviewLiveState(null);
  }, [detailQuery.data]);

  const createMutation = useMutation({
    mutationFn: async () => {
      const name = newPipelineName.trim();
      if (!name) {
        throw new Error('Name is required');
      }
      if (selectedTemplateId) {
        return api.runtimePipelines.instantiateTemplate(selectedTemplateId, {
          name,
          description: newPipelineDescription.trim() || null,
          change_note: 'Created from no-code template'
        });
      }
      return api.runtimePipelines.create({
        name,
        description: newPipelineDescription.trim() || null,
        template: false
      });
    },
    onSuccess: async (definition) => {
      setNewPipelineName('');
      setNewPipelineDescription('');
      setSelectedTemplateId('');
      setSelectedDefinitionId(definition.pipeline_definition_id);
      await queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definitions'] });
      notify({ title: 'Pipeline workspace created', description: definition.name, tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Pipeline creation failed'), tone: 'error' });
    }
  });

  const channelInstanceMutation = useMutation({
    mutationFn: async () => {
      if (!props.embeddedChannel?.channelId) {
        throw new Error('Open a channel before creating a channel pipeline');
      }
      if (!selectedTemplateId) {
        throw new Error('Select a template first');
      }
      return api.channels.pipelineInstance(props.embeddedChannel.channelId, {
        template_definition_id: selectedTemplateId,
        name: `${props.embeddedChannel.channelName || 'Channel'} Pipeline`,
        replace_existing: Boolean(embeddedPipelineDefinition)
      });
    },
    onSuccess: async (definition) => {
      setSelectedDefinitionId(definition.pipeline_definition_id);
      setSelectedTemplateId('');
      setMode(definition.latest_version?.mode ?? 'realtime');
      setGraph(definition.latest_version?.graph ?? emptyGraph);
      setOutputCatalog(null);
      setValidationReport(definition.latest_version?.validation_report ?? null);
      setLastSavedVersionId(definition.latest_version?.pipeline_version_id ?? null);
      setSelectedNodeKey(null);
      setSelectedEdgeKey(null);
      setDirty(false);
      setPreviewLiveState(null);
      const invalidations = [
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definitions'] }),
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definition', definition.pipeline_definition_id] }),
        queryClient.invalidateQueries({ queryKey: ['runtime-channels'] })
      ];
      if (props.embeddedChannel?.channelId) {
        invalidations.push(
          queryClient.invalidateQueries({ queryKey: ['runtime-channel', props.embeddedChannel.channelId] }),
          queryClient.invalidateQueries({ queryKey: ['runtime-trigger-endpoints', props.embeddedChannel.channelId] }),
          queryClient.invalidateQueries({
            queryKey: ['runtime-channel-authoring-context', props.embeddedChannel.channelId]
          })
        );
      }
      await Promise.all(invalidations);
      notify({ title: 'Channel pipeline created', description: definition.name, tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Channel pipeline creation failed'), tone: 'error' });
    }
  });

  const deleteMutation = useMutation({
    mutationFn: (pipelineDefinitionId: string) =>
      api.runtimePipelines.delete(pipelineDefinitionId, 'Removed from runtime pipeline designer'),
    onSuccess: async (definition) => {
      if (selectedDefinitionId === definition.pipeline_definition_id) {
        setSelectedDefinitionId('');
        setSelectedNodeKey(null);
        setSelectedEdgeKey(null);
        setGraph(emptyGraph);
        setOutputCatalog(null);
        setValidationReport(null);
        setLastSavedVersionId(null);
        setDirty(false);
        setPreviewLiveState(null);
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definitions'] }),
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definition', definition.pipeline_definition_id] })
      ]);
      notify({ title: 'Pipeline deleted', description: definition.name, tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Pipeline delete failed'), tone: 'error' });
    }
  });

  const validateMutation = useMutation({
    mutationFn: () => api.runtimePipelines.validate({ mode: effectiveMode, authoring_graph: authoringGraph }),
    onSuccess: (response) => {
      setValidationReport(response.report);
      setOutputCatalog(response.output_catalog ?? null);
      notify({
        title: response.report.status === 'valid' ? 'Pipeline validates' : 'Pipeline needs attention',
        description: `${response.report.error_count} errors / ${response.report.warning_count} warnings`,
        tone: response.report.status === 'valid' ? 'success' : 'info'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Pipeline validation failed'), tone: 'error' });
    }
  });

  const saveMutation = useMutation({
    mutationFn: () => {
      if (!selectedDefinitionId) {
        throw new Error('Select or create a pipeline first');
      }
      if (!selectedDefinitionIsEditable) {
        throw new Error('Create or select this channel pipeline before saving');
      }
      return api.runtimePipelines.createVersion(selectedDefinitionId, {
        mode: effectiveMode,
        authoring_graph: authoringGraph,
        change_note: changeNote
      });
    },
    onSuccess: async (version) => {
      setLastSavedVersionId(version.pipeline_version_id);
      setGraph(version.graph);
      setOutputCatalog(null);
      setValidationReport(version.validation_report);
      setDirty(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definitions'] }),
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definition', selectedDefinitionId] })
      ]);
      notify({
        title: 'Pipeline version saved',
        description: version.error_count
          ? `Version ${version.version_no} saved. ${validationIssueSummary(version.validation_report)}`
          : `Version ${version.version_no} is ready for channel activation.`,
        tone: version.error_count ? 'info' : 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Pipeline save failed'), tone: 'error' });
    }
  });

  const previewMutation = useMutation({
    mutationFn: async (options: {
      silent?: boolean;
      signature?: string;
      previewFrame?: RuntimeLiveFrame | null;
      selectedNodeKey?: string | null;
      purpose?: 'operator' | 'commissioning';
    }) => {
      if (!props.embeddedChannel?.channelId) {
        throw new Error('Open a channel to run the selected pipeline step');
      }
      const captureGraph = graph;
      const captureChannel = props.embeddedChannel.channelId;
      const response = await api.channels.pipelinePreview(props.embeddedChannel.channelId, {
        mode: effectiveMode,
        authoring_graph: authoringGraph,
        preview_frame: options.previewFrame ?? null,
        selected_node_key: options.selectedNodeKey ?? selectedNodeKey,
        include_capture_outputs: false,
        include_execution_result: !options.silent && options.purpose !== 'commissioning'
      });
      return { ...response, captureGraph, captureChannel };
    },
    onSuccess: (response, options) => {
      const desiredSignature =
        options.purpose === 'commissioning'
          ? latestDesiredCommissioningPreviewSignature.current
          : latestDesiredPreviewSignature.current;
      if (options.signature && options.signature !== desiredSignature) {
        return;
      }
      if (options.purpose === 'commissioning') {
        setCommissioningPreviewLiveState(response.state);
      } else {
        setPreviewLiveState(response.state);
        if (response.execution_result && response.captureGraph === captureGraphRef.current
          && response.captureChannel === captureChannelRef.current) {
          setTrainingCapture({ graph: response.captureGraph, execution: response.execution_result });
        }
      }
      if (!options.silent) {
        notify({
          title: response.state.source_health.state === 'ok' ? 'Selected step is running' : 'Selected step completed with warnings',
          description: `${response.state.overlay.length} overlays / ${response.state.diagnostics?.pipeline_node_timings.length ?? 0} node timings`,
          tone: response.state.source_health.state === 'ok' ? 'success' : 'info'
        });
      }
    },
    onError: (error, options) => {
      const desiredSignature =
        options.purpose === 'commissioning'
          ? latestDesiredCommissioningPreviewSignature.current
          : latestDesiredPreviewSignature.current;
      if (options.signature && options.signature !== desiredSignature) {
        return;
      }
      if (!options.silent) {
        notify({ ...describeError(error, 'Selected step could not run'), tone: 'error' });
      }
    }
  });
  const runDraftPreview = previewMutation.mutate;
  const draftPreviewPending = previewMutation.isPending;

  useEffect(() => {
    const targetNodeKey = commissioningPreviewTargetNodeKey(graph, selectedNodeKey);
    const rawSourceFrame = intrinsicCalibrationSourceFrame(
      boardCalibrationLiveViewQuery.data?.state ?? null
    );
    if (
      props.active === false ||
      !props.embeddedChannel?.channelId ||
      !canonicalViewCalibrationSelected ||
      !targetNodeKey ||
      !rawSourceFrame
    ) {
      lastCommissioningPreviewSignature.current = '';
      latestDesiredCommissioningPreviewSignature.current = '';
      if (!canonicalViewCalibrationSelected) {
        setCommissioningPreviewLiveState(null);
      }
      return;
    }
    const signature = JSON.stringify({
      channelId: props.embeddedChannel.channelId,
      frameSequence: rawSourceFrame.sequence,
      frameTimestamp: rawSourceFrame.source_timestamp ?? rawSourceFrame.captured_at,
      mode: effectiveMode,
      targetNodeKey,
      graph: authoringGraph
    });
    latestDesiredCommissioningPreviewSignature.current = signature;
    if (signature === lastCommissioningPreviewSignature.current || draftPreviewPending) {
      return;
    }
    const timer = window.setTimeout(() => {
      lastCommissioningPreviewSignature.current = signature;
      runDraftPreview({
        silent: true,
        signature,
        previewFrame: rawSourceFrame,
        selectedNodeKey: targetNodeKey,
        purpose: 'commissioning'
      });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [
    authoringGraph,
    boardCalibrationLiveViewQuery.data?.state,
    canonicalViewCalibrationSelected,
    draftPreviewPending,
    effectiveMode,
    graph,
    props.embeddedChannel?.channelId,
    props.active,
    runDraftPreview,
    selectedNodeKey
  ]);

  useEffect(() => {
    const automaticPreviewFrame =
      frozenBoardEndPreviewFrame ?? authoringLiveViewQuery.data?.state.frame ?? null;
    if (
      props.active === false ||
      !props.embeddedChannel?.channelId ||
      selectedNode?.node_type !== 'analysis.board_end_coordinate_frame' ||
      !automaticPreviewFrame
    ) {
      lastAutomaticPreviewSignature.current = '';
      latestDesiredPreviewSignature.current = '';
      return;
    }
    const signature = JSON.stringify({
      channelId: props.embeddedChannel.channelId,
      frameSequence: automaticPreviewFrame.sequence,
      frozen: frozenBoardEndPreviewFrame !== null,
      mode: effectiveMode,
      selectedNodeKey: selectedNode.node_key,
      graph: authoringGraph
    });
    latestDesiredPreviewSignature.current = signature;
    if (
      signature === lastAutomaticPreviewSignature.current ||
      draftPreviewPending
    ) {
      return;
    }
    const timer = window.setTimeout(() => {
      lastAutomaticPreviewSignature.current = signature;
      runDraftPreview({
        silent: true,
        signature,
        previewFrame: frozenBoardEndPreviewFrame
      });
    }, 300);
    return () => window.clearTimeout(timer);
  }, [
    authoringGraph,
    authoringLiveViewQuery.data?.state.frame,
    effectiveMode,
    draftPreviewPending,
    props.embeddedChannel?.channelId,
    props.active,
    frozenBoardEndPreviewFrame,
    runDraftPreview,
    selectedNode
  ]);

  const publishMutation = useMutation({
    mutationFn: () => {
      if (!lastSavedVersionId) {
        throw new Error('Save a version before publishing');
      }
      return api.runtimePipelines.publishVersion(lastSavedVersionId);
    },
    onSuccess: async (version) => {
      setValidationReport(version.validation_report);
      const invalidations = [
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definitions'] }),
        queryClient.invalidateQueries({ queryKey: ['runtime-pipeline-definition', selectedDefinitionId] }),
        queryClient.invalidateQueries({ queryKey: ['runtime-channels'] })
      ];
      if (props.embeddedChannel?.channelId) {
        invalidations.push(
          queryClient.invalidateQueries({ queryKey: ['runtime-channel', props.embeddedChannel.channelId] }),
          queryClient.invalidateQueries({ queryKey: ['runtime-trigger-endpoints', props.embeddedChannel.channelId] }),
          queryClient.invalidateQueries({ queryKey: ['runtime-channel-authoring-context', props.embeddedChannel.channelId] }),
          queryClient.invalidateQueries({
            queryKey: ['runtime-channel-live-view', props.embeddedChannel.channelId]
          })
        );
      }
      await Promise.all(invalidations);
      notify({ title: 'Pipeline active on channel', description: `Version ${version.version_no}`, tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Pipeline publish failed'), tone: 'error' });
    }
  });

  const canPublish = Boolean(
    lastSavedVersionId &&
      !dirty &&
      selectedDefinitionIsEditable &&
      validationReport?.status === 'valid' &&
      !publishMutation.isPending
  );
  const canRequestPublish = Boolean(
    lastSavedVersionId && !dirty && selectedDefinitionIsEditable && !publishMutation.isPending
  );
  const triggerSummary = triggerablePipelineSummary(graph, selectedDefinition);
  const cameraStreamCalibrationLiveState =
    intrinsicCalibrationSelected &&
    intrinsicCalibrationSourceFrame(boardCalibrationLiveViewQuery.data?.state ?? null)
      ? boardCalibrationLiveViewQuery.data?.state ?? null
      : null;
  const resolvedAuthoringLiveState =
    cameraStreamCalibrationLiveState ??
    (canonicalViewCalibrationSelected ? commissioningPreviewLiveState : null) ??
    (canonicalViewCalibrationSelected ? boardCalibrationLiveViewQuery.data?.state ?? null : null) ??
    previewLiveState ??
    (capturedCalibrationLiveState?.channel_id === boardCalibrationChannelId
      ? capturedCalibrationLiveState
      : null) ??
    boardCalibrationLiveViewQuery.data?.state ??
    authoringLiveViewQuery.data?.state ??
    null;

  const mutateGraph = (next: RuntimePipelineGraph) => {
    setGraph(next);
    setDirty(true);
    setValidationReport(null);
    if (!frozenBoardEndPreviewFrame) {
      setPreviewLiveState(null);
    }
  };

  const selectDefinition = (pipelineDefinitionId: string) => {
    if (pipelineDefinitionId === selectedDefinitionId) {
      return;
    }
    setSelectedDefinitionId(pipelineDefinitionId);
    setSelectedNodeKey(null);
    setSelectedEdgeKey(null);
    setGraph(emptyGraph);
    setOutputCatalog(null);
    setValidationReport(null);
    setLastSavedVersionId(null);
    setDirty(false);
    setPreviewLiveState(null);
  };

  useEffect(() => {
    if (!props.embeddedChannel) {
      return;
    }
    if (!embeddedPipelineDefinition) {
      if (selectedDefinitionId) {
        setSelectedDefinitionId('');
        setSelectedNodeKey(null);
        setSelectedEdgeKey(null);
        setGraph(emptyGraph);
        setOutputCatalog(null);
        setValidationReport(null);
        setLastSavedVersionId(null);
        setDirty(false);
        setPreviewLiveState(null);
      }
      return;
    }
    if (selectedDefinitionId === embeddedPipelineDefinition.pipeline_definition_id) {
      return;
    }
    setSelectedDefinitionId(embeddedPipelineDefinition.pipeline_definition_id);
    setSelectedNodeKey(null);
    setSelectedEdgeKey(null);
    setGraph(emptyGraph);
    setOutputCatalog(null);
    setValidationReport(null);
    setLastSavedVersionId(null);
    setDirty(false);
    setPreviewLiveState(null);
  }, [embeddedPipelineDefinition, props.embeddedChannel, selectedDefinitionId]);

  const handlePublish = () => {
    if (!canPublish) {
      if (validationReport?.status === 'invalid') {
        notify({
          title: 'Complete pipeline still needs configuration',
          description: validationIssueSummary(validationReport),
          tone: 'info'
        });
      } else if (dirty) {
        notify({
          title: 'Save changes first',
          description: 'Channel activation always uses a saved version.',
          tone: 'info'
        });
      }
      return;
    }
    if (selectedPipelineChannels.length && selectedDefinition?.scope === 'channel_instance') {
      const names = selectedPipelineChannels
        .map((channel) => `- ${channel.name} / ${channel.technical_place}`)
        .join('\n');
      const confirmed = window.confirm(
        `This channel pipeline instance is used by ${selectedPipelineChannels.length} channel${
          selectedPipelineChannels.length === 1 ? '' : 's'
        }:\n${names}\n\nPublishing will move only the owning channel to the new version. Running channels will be restarted.`
      );
      if (!confirmed) {
        return;
      }
    }
    const plcWriteNodes = enabledSiemensSpsWriteNodes(graph);
    if (plcWriteNodes.length) {
      const names = plcWriteNodes.map((node) => `- ${node.node_key}`).join('\n');
      const confirmed = window.confirm(
        `This pipeline contains enabled Siemens SPS write side effects:\n${names}\n\nPublishing allows runtime executions to write to configured PLC DB addresses when dry-run is off and runtime PLC writes are allowed.`
      );
      if (!confirmed) {
        return;
      }
    }
    publishMutation.mutate();
  };

  const handleDeleteDefinition = (definition: RuntimePipelineDefinitionSummary) => {
    const usedChannels = channelsUsingDefinition(definition, channels);
    if (usedChannels.length) {
      notify({
        title: 'Pipeline is in use',
        description: usedChannels.map((channel) => channel.name).join(', '),
        tone: 'error'
      });
      return;
    }
    const confirmed = window.confirm(`Delete runtime pipeline "${definition.name}"?`);
    if (confirmed) {
      deleteMutation.mutate(definition.pipeline_definition_id);
    }
  };

  const handleCreateChannelPipeline = () => {
    if (!selectedTemplateId || channelInstanceMutation.isPending) {
      return;
    }
    if (embeddedPipelineDefinition) {
      const confirmed = window.confirm(
        `Replace channel pipeline "${embeddedPipelineDefinition.name}" with a new instance from the selected template?`
      );
      if (!confirmed) {
        return;
      }
    }
    channelInstanceMutation.mutate();
  };

  const editorActions = (
    <div className="actions-row">
      <button type="button" onClick={() => validateMutation.mutate()} disabled={validateMutation.isPending}>
        {validateMutation.isPending ? 'Prüft …' : 'Prüfen'}
      </button>
      {props.embeddedChannel ? (
        <button
          type="button"
          onClick={() =>
            previewMutation.mutate({
              silent: false,
              previewFrame:
                frozenBoardEndPreviewFrame ??
                intrinsicCalibrationSourceFrame(boardCalibrationLiveViewQuery.data?.state ?? null)
            })
          }
          disabled={!selectedDefinitionIsEditable || !graph.nodes.length || previewMutation.isPending}
        >
          {previewMutation.isPending ? 'Läuft …' : 'Schritt testen'}
        </button>
      ) : null}
      <button
        type="button"
        className="button--primary"
        onClick={() => saveMutation.mutate()}
        disabled={!selectedDefinitionIsEditable || saveMutation.isPending}
      >
        {saveMutation.isPending ? 'Speichert …' : 'Speichern'}
      </button>
      <button
        type="button"
        onClick={handlePublish}
        disabled={!canRequestPublish}
        title="Assign the saved, completely configured pipeline to this channel. Siemens writes remain controlled separately."
      >
        Aktivieren
      </button>
    </div>
  );
  const editorStatusContent = (
    <>
      <div className="runtime-pipeline-toolbar">
        <div className="metric-pill">
          <span>Execution</span>
          <strong>{modeLabel(effectiveMode)}</strong>
          <small className="subtle-text">Derived from trigger and source nodes</small>
        </div>
        <label className="filter-field">
          <span>Change note</span>
          <input value={changeNote} onChange={(event) => setChangeNote(event.target.value)} />
        </label>
        <div className="metric-pill">
          <span>Current version</span>
          <strong>{lastSavedVersionId ? 'Saved' : 'Not saved'}</strong>
          <small className="subtle-text">
            {selectedDefinition
              ? `${selectedDefinition.name}${dirty ? ' has unsaved canvas changes' : ''}`
              : 'Create or select a pipeline'}
          </small>
        </div>
      </div>

      {selectedPipelineChannels.length && selectedDefinition?.scope === 'channel_instance' ? (
        <div className="inline-alert inline-alert--info">
          <strong>Used by channels</strong>
          <p className="subtle-text">
            Publishing will update this channel-scoped pipeline instance: {selectedPipelineChannels.map((channel) => channel.name).join(', ')}.
          </p>
        </div>
      ) : null}
      {selectedDefinition?.scope === 'template' ? (
        <div className="inline-alert inline-alert--info">
          <strong>Template publishing</strong>
          <p className="subtle-text">
            Publishing a template does not automatically update assigned channel instances.
          </p>
        </div>
      ) : null}
    </>
  );

  useEffect(() => {
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (props.active === false) return;
      if (event.key !== 'Delete' || isEditableKeyboardTarget(event.target)) {
        return;
      }
      if (selectedEdgeKey) {
        event.preventDefault();
        setGraph((current) => ({
          ...current,
          edges: current.edges.filter((edge) => runtimeEdgeKey(edge) !== selectedEdgeKey)
        }));
        setSelectedEdgeKey(null);
        setDirty(true);
        setValidationReport(null);
        setOutputCatalog(null);
        return;
      }
      if (selectedNodeKey) {
        event.preventDefault();
        setGraph((current) => removeNode(current, selectedNodeKey));
        setSelectedNodeKey(null);
        setSelectedEdgeKey(null);
        setDirty(true);
        setValidationReport(null);
        setOutputCatalog(null);
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [selectedEdgeKey, selectedNodeKey, props.active]);

  return (
    <div className="panel-stack panel-stack--wide runtime-designer-workspace">
      <div className="ws-toolbar workspace-graph-toolbar"><ToolButton icon="panel" label="Bausteine ein-/ausblenden" active={paletteOpen} onClick={() => setPaletteOpen(value => !value)}/><strong title={selectedDefinition?.name}>{selectedDefinition?.name ?? embeddedPipelineDefinition?.name ?? 'Pipeline auswählen'}</strong><span className="ws-badge"><i/>{dirty ? 'Ungespeichert' : lastSavedVersionId ? 'Gespeichert' : 'Entwurf'}</span><span className="ws-spacer"/><button onClick={() => setWorkspaceDialog('settings')}><Icon name="settings" size={14}/>{props.embeddedChannel ? 'Version & Vorlage' : 'Bibliothek'}</button><button onClick={() => setWorkspaceDialog('guided')}>Geführter Aufbau</button>{editorActions}</div>
      {workspaceDialog === 'settings' && <Modal wide title={props.embeddedChannel ? 'Pipeline · Version & Vorlage' : 'Pipeline-Bibliothek'} onClose={() => setWorkspaceDialog(null)}>
      {props.embeddedChannel ? (
        <Panel title="Pipeline verwalten">
          <div className="runtime-pipeline-toolbar runtime-pipeline-toolbar--embedded">
            <div className="channel-pipeline-current">
              <span>Aktive Pipeline</span>
              <strong>{selectedDefinition?.name ?? embeddedPipelineDefinition?.name ?? 'Not assigned'}</strong>
              <small className="subtle-text">
                {embeddedPipelineDefinition
                  ? 'Kanalspezifische Pipeline bearbeiten'
                  : 'Aus einer Vorlage erstellen'}
              </small>
            </div>
            <MetricPill
              label="Setup"
              value={pipelineCheckStatusLabel(validationReport, dirty)}
              tone={validationReport?.status === 'valid' ? 'success' : validationReport ? 'warn' : 'neutral'}
            />
          </div>
          <details><summary>Vorlage auswählen / ersetzen</summary>          <div className="runtime-pipeline-create-row">
            <label className="filter-field">
              <span>Template basis</span>
              <select value={selectedTemplateId} onChange={(event) => setSelectedTemplateId(event.target.value)}>
                <option value="">Select template</option>
                {channelTemplateDefinitions.map((template) => (
                  <option key={template.pipeline_definition_id} value={template.pipeline_definition_id}>
                    {template.name}{template.description ? ` / ${template.description}` : ''}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={handleCreateChannelPipeline}
              disabled={!selectedTemplateId || channelInstanceMutation.isPending}
            >
              {channelInstanceMutation.isPending
                ? 'Creating...'
                : embeddedPipelineDefinition
                  ? 'Replace channel pipeline'
                  : 'Create channel pipeline'}
            </button>
          </div>
          </details>
          {editorStatusContent}
          <PipelineValidationDetails
            report={validationReport}
            dirty={dirty}
            onSelectNode={(nodeKey) => {
              setSelectedNodeKey(nodeKey);
              setSelectedEdgeKey(null);
            }}
          />
        </Panel>
      ) : (
        <>


          <details className="workspace-definition-picker" open><summary>Pipeline auswählen · {selectedDefinition?.name ?? 'Keine Auswahl'}</summary><PipelineSelectionPanel
            definitions={filteredReusableDefinitions}
            totalDefinitionCount={reusableDefinitions.length}
            selectedDefinitionId={selectedDefinitionId}
            channels={channels}
            search={pipelineSearch}
            statusFilter={pipelineStatusFilter}
            usageFilter={pipelineUsageFilter}
            deletingDefinitionId={
              deleteMutation.isPending ? String(deleteMutation.variables ?? '') : null
            }
            onSearchChange={setPipelineSearch}
            onStatusFilterChange={setPipelineStatusFilter}
            onUsageFilterChange={setPipelineUsageFilter}
            onSelect={selectDefinition}
            onDelete={handleDeleteDefinition}
          /></details>
        </>
      )}

      {!props.embeddedChannel ? (
        <Panel title="Pipeline changes and channel use">
          {editorStatusContent}

          <details><summary>Neue Pipeline anlegen</summary>          <div className="runtime-pipeline-create-row">
            <label className="filter-field">
              <span>New pipeline name</span>
              <input
                value={newPipelineName}
                onChange={(event) => setNewPipelineName(event.target.value)}
                placeholder="Packing reject decision"
              />
            </label>
            <label className="filter-field">
              <span>Description</span>
              <input
                value={newPipelineDescription}
                onChange={(event) => setNewPipelineDescription(event.target.value)}
                placeholder="Board inspection with live overlay"
              />
            </label>
            <label className="filter-field">
              <span>Template</span>
              <select value={selectedTemplateId} onChange={(event) => setSelectedTemplateId(event.target.value)}>
                <option value="">Blank pipeline</option>
                {templates.map((template) => (
                  <option key={template.pipeline_definition_id} value={template.pipeline_definition_id}>
                    {template.name}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" onClick={() => createMutation.mutate()} disabled={createMutation.isPending}>
              {createMutation.isPending ? 'Creating...' : 'Create workspace'}
            </button>
          </div>
          </details>
          {triggerSummary ? (
            <div className="inline-alert inline-alert--info">
              <strong>Externally triggerable pipeline</strong>
              <p className="subtle-text">
                {triggerSummary.method} {triggerSummary.path} / {triggerSummary.authLabel} / {triggerSummary.timeoutMs} ms.
              </p>
            </div>
          ) : null}
        </Panel>
      ) : null}

      </Modal>}
      {workspaceDialog === 'guided' && <Modal wide title="Geführt zusammenstellen" onClose={() => setWorkspaceDialog(null)}><GuidedPipelineBuilder
        compact={Boolean(props.embeddedChannel)}
        graph={graph}
        nodeTypes={nodeTypes}
        nodeTypeMap={nodeTypeMap}
        effectiveMode={effectiveMode}
        onGraphChange={mutateGraph}
        onSelectNode={(nodeKey) => {
          setSelectedNodeKey(nodeKey);
          setSelectedEdgeKey(null);
        }}
      />

      </Modal>}
      {graph.nodes.some(node => node.node_type === 'preprocessing.endface_pair_split') &&
        <button type="button" onClick={() => setBarkInspectionOpen(true)}>Rindenmessung · Foto und CSV hochladen</button>}
      {barkInspectionOpen && <Modal title="Rindenmessung – zwei Stirnflächen" onClose={() => setBarkInspectionOpen(false)}>
        <BarkInspectionWorkspace graph={graph} channelId={props.embeddedChannel?.channelId}
          definitionId={selectedDefinitionId} versionId={lastSavedVersionId} dirty={dirty} onGraphChange={mutateGraph}
          onPreviewFrame={frame => setCapturedCalibrationLiveState({
            ...(resolvedAuthoringLiveState ?? {}),
            channel_id: props.embeddedChannel?.channelId ?? '', frame,
          } as RuntimeLiveViewState)} />
      </Modal>}
      {trainingCapture && <details><summary>Trainingsbilder aus getesteten Pipeline-Stufen</summary>
        <PipelineTrainingCapture channelId={props.embeddedChannel?.channelId}
          graph={trainingCapture.graph} execution={trainingCapture.execution} />
      </details>}
      <section className="workspace-graph"><span className="sr-only">Advanced graph</span>
      <div className={`runtime-pipeline-layout ${paletteOpen ? '' : 'runtime-pipeline-layout--no-palette'}`}>
        {paletteOpen && <NodePalette
          graph={graph}
          nodeTypes={nodeTypes}
          nodeTypeMap={nodeTypeMap}
          effectiveMode={effectiveMode}
          onAdd={nodeType => {
            const node = buildNode(nodeType, graph.nodes, {x: 80 + graph.nodes.length % 3 * 240, y: 80 + Math.floor(graph.nodes.length / 3) * 140});
            mutateGraph({...graph, nodes: [...graph.nodes, node]}); setSelectedNodeKey(node.node_key); setSelectedEdgeKey(null);
          }}
        />}
        <PipelineCanvas key={selectedDefinitionId}
          graph={graph}
          nodeTypes={nodeTypeMap}
          effectiveMode={effectiveMode}
          selectedNodeKey={selectedNodeKey}
          selectedEdgeKey={selectedEdgeKey}
          onSelectNode={(nodeKey) => {
            setSelectedNodeKey(nodeKey);
            if (nodeKey) {
              setSelectedEdgeKey(null);
            }
          }}
          onSelectEdge={(edgeKey) => {
            setSelectedEdgeKey(edgeKey);
            if (edgeKey) {
              setSelectedNodeKey(null);
            }
          }}
          onGraphChange={mutateGraph}
          onNodeDropRejected={(nodeType, reason) => {
            notify({
              title: `${nodeType.display_name} cannot be added here`,
              description: reason,
              tone: 'error'
            });
          }}
        />
      <NodePropertiesModal
        graph={graph}
        node={selectedNode}
        nodeType={selectedNodeType}
        nodeTypes={nodeTypeMap}
        outputCatalog={outputCatalog}
        channels={channels}
        modelCapabilities={modelCapabilities}
        labelProjects={labelProjects}
        authoringContext={authoringContext}
        authoringLiveState={resolvedAuthoringLiveState}
        calibrationChannelId={boardCalibrationChannelId}
        capturedCalibrationFrame={
          capturedCalibrationLiveState?.channel_id === boardCalibrationChannelId
            ? capturedCalibrationLiveState.frame ?? null
            : null
        }
        calibrationCaptureAvailable={Boolean(boardCalibrationChannelId)}
        calibrationCapturePending={calibrationCaptureMutation.isPending}
        previewPending={previewMutation.isPending}
        saveAvailable={selectedDefinitionIsEditable}
        savePending={saveMutation.isPending}
        onSave={() => saveMutation.mutate()}
        hasUncheckedChanges={dirty}
        previewFrozen={frozenBoardEndPreviewFrame !== null}
        onTogglePreviewFreeze={() => {
          if (frozenBoardEndPreviewFrame) {
            setFrozenBoardEndPreviewFrame(null);
            setPreviewLiveState(null);
            return;
          }
          const frameToFreeze = resolvedAuthoringLiveState?.frame ?? null;
          if (frameToFreeze?.image_base64) {
            setFrozenBoardEndPreviewFrame(frameToFreeze);
          }
        }}
        onCaptureCalibrationFrame={() => {
          if (boardCalibrationChannelId) {
            calibrationCaptureMutation.mutate(boardCalibrationChannelId);
          }
        }}
        validationReport={validationReport}
        selectedEdgeKey={selectedEdgeKey}
        onGraphChange={mutateGraph}
        onSelectNode={setSelectedNodeKey}
        onSelectEdge={setSelectedEdgeKey}
        onClose={() => setSelectedNodeKey(null)}
      />
      </div></section>

      <details className="workspace-graph-checks"><summary><span>Prüfergebnisse · {pipelineCheckStatusLabel(validationReport, dirty)}</span><span>{graph.nodes.length} Bausteine · {graph.edges.length} Verbindungen · {modeLabel(effectiveMode)}</span></summary><PipelineValidationDetails report={validationReport} dirty={dirty} onSelectNode={nodeKey => {setSelectedNodeKey(nodeKey); setSelectedEdgeKey(null);}}/></details>
    </div>
  );
}

function PipelineValidationDetails(props: {
  report: RuntimePipelineValidationReport | null;
  dirty: boolean;
  onSelectNode: (nodeKey: string) => void;
}) {
  if (!props.report) {
    return (
      <div className="inline-alert inline-alert--info">
        <strong>{props.dirty ? 'Changes are not checked yet' : 'No complete-pipeline check yet'}</strong>
        <p className="subtle-text">
          You can save and run individual configured steps now. Check the complete pipeline when
          you want to activate it on the channel.
        </p>
      </div>
    );
  }
  return (
    <div className="runtime-validation-list" aria-label="Complete pipeline validation details">
      <div className={`inline-alert inline-alert--${props.report.error_count ? 'warn' : 'info'}`}>
        <strong>
          {props.report.status === 'valid'
            ? 'Complete pipeline is ready for channel activation'
            : 'Saved successfully; complete pipeline still needs configuration'}
        </strong>
        <p className="subtle-text">
          {props.report.error_count} errors and {props.report.warning_count} warnings. Individual
          configured steps can still be run from their node editor.
        </p>
      </div>
      {props.report.issues.map((issue, index) => (
        <article
          key={`${issue.code}-${issue.node_key ?? 'graph'}-${index}`}
          className={`runtime-validation-item runtime-validation-item--${issue.severity}`}
        >
          <strong>{issue.message}</strong>
          <span>
            {issue.node_key ? `Node ${issue.node_key}` : 'Complete graph'}
            {issue.field ? ` / ${issue.field}` : ` / ${issue.code}`}
          </span>
          {issue.node_key ? (
            <button type="button" onClick={() => props.onSelectNode(issue.node_key!)}>
              Open affected node
            </button>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function PipelineSelectionPanel(props: {
  definitions: RuntimePipelineDefinitionSummary[];
  totalDefinitionCount: number;
  selectedDefinitionId: string;
  channels: RuntimeChannelSummary[];
  search: string;
  statusFilter: RuntimePipelineStatusFilter;
  usageFilter: 'all' | 'used' | 'unused';
  deletingDefinitionId: string | null;
  onSearchChange: (value: string) => void;
  onStatusFilterChange: (value: RuntimePipelineStatusFilter) => void;
  onUsageFilterChange: (value: 'all' | 'used' | 'unused') => void;
  onSelect: (pipelineDefinitionId: string) => void;
  onDelete: (definition: RuntimePipelineDefinitionSummary) => void;
}) {
  const handleCardKeyDown = (
    event: KeyboardEvent<HTMLElement>,
    definition: RuntimePipelineDefinitionSummary
  ) => {
    if (event.key !== 'Enter' && event.key !== ' ') {
      return;
    }
    event.preventDefault();
    props.onSelect(definition.pipeline_definition_id);
  };

  return (
    <Panel title="Runtime Pipelines">
      <div className="runtime-pipeline-filter-bar">
        <label className="filter-field">
          <span>Search</span>
          <input
            value={props.search}
            onChange={(event) => props.onSearchChange(event.target.value)}
            placeholder="Name, description, channel"
          />
        </label>
        <label className="filter-field">
          <span>Status</span>
          <select
            value={props.statusFilter}
            onChange={(event) => props.onStatusFilterChange(event.target.value as RuntimePipelineStatusFilter)}
          >
            {runtimePipelineStatusOptions.map((status) => (
              <option key={status} value={status}>
                {status === 'all' ? 'All statuses' : status.replaceAll('_', ' ')}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Usage</span>
          <select
            value={props.usageFilter}
            onChange={(event) => props.onUsageFilterChange(event.target.value as 'all' | 'used' | 'unused')}
          >
            <option value="all">All pipelines</option>
            <option value="used">Used by channels</option>
            <option value="unused">Unused</option>
          </select>
        </label>
        <span className="filter-count">
          {props.definitions.length} of {props.totalDefinitionCount}
        </span>
      </div>

      {props.definitions.length ? (
        <div className="runtime-pipeline-card-grid">
          {props.definitions.map((definition) => {
            const usedChannels = channelsUsingDefinition(definition, props.channels);
            const selected = definition.pipeline_definition_id === props.selectedDefinitionId;
            const deleteDisabled =
              Boolean(usedChannels.length) ||
              props.deletingDefinitionId === definition.pipeline_definition_id;
            return (
              <article
                key={definition.pipeline_definition_id}
                className={`runtime-pipeline-select-card ${
                  selected ? 'runtime-pipeline-select-card--selected' : ''
                }`}
                role="button"
                tabIndex={0}
                onClick={() => props.onSelect(definition.pipeline_definition_id)}
                onKeyDown={(event) => handleCardKeyDown(event, definition)}
                aria-pressed={selected}
              >
                <div className="runtime-pipeline-select-card__header">
                  <div>
                    <strong>{definition.name}</strong>
                    <span>{definition.status}</span>
                  </div>
                  <button
                    type="button"
                    className="button--danger"
                    onClick={(event) => {
                      event.stopPropagation();
                      props.onDelete(definition);
                    }}
                    disabled={deleteDisabled}
                    title={
                      usedChannels.length
                        ? 'Unassign this pipeline from channels before deleting it.'
                        : 'Delete runtime pipeline'
                    }
                  >
                    {props.deletingDefinitionId === definition.pipeline_definition_id ? 'Deleting...' : 'Delete'}
                  </button>
                </div>
                <p>{definition.description || 'No description set.'}</p>
                <div className="runtime-pipeline-select-card__channels">
                  <span>Channels</span>
                  <strong>
                    {usedChannels.length
                      ? usedChannels.map((channel) => channel.name).join(', ')
                      : 'No channels'}
                  </strong>
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <EmptyState
          title="No runtime pipelines match the filters"
          body="Adjust the filter bar or create a new runtime pipeline below."
        />
      )}
    </Panel>
  );
}

function GuidedPipelineBuilder(props: {
  compact?: boolean;
  graph: RuntimePipelineGraph;
  nodeTypes: RuntimeNodeTypeSummary[];
  nodeTypeMap: Map<string, RuntimeNodeTypeSummary>;
  effectiveMode: RuntimeMode;
  onGraphChange: (graph: RuntimePipelineGraph) => void;
  onSelectNode: (nodeKey: string) => void;
}) {
  const addOrSelectNode = (nodeTypeKey: string) => {
    const existing = props.graph.nodes.find((node) => node.node_type === nodeTypeKey);
    if (existing && guidedNodeIsSingleton(nodeTypeKey)) {
      props.onSelectNode(existing.node_key);
      return;
    }
    const nodeType = props.nodeTypeMap.get(nodeTypeKey);
    if (!nodeType) {
      return;
    }
    const eligibility = canAddNodeToGraph(
      props.graph,
      nodeType,
      props.nodeTypeMap,
      props.effectiveMode
    );
    if (!eligibility.eligible) {
      return;
    }
    const node = buildNode(nodeType, props.graph.nodes, guidedNodePosition(props.graph, nodeType));
    const next = autoConnectNewNode(
      { ...props.graph, nodes: [...props.graph.nodes, node] },
      node,
      nodeType,
      props.nodeTypeMap
    );
    props.onGraphChange(next);
    props.onSelectNode(node.node_key);
  };

  const setInputMode = (inputMode: 'realtime' | 'trigger') => {
    const sourceType = props.nodeTypeMap.get('source.channel_frame');
    if (!sourceType) {
      return;
    }
    let next = props.graph;
    if (inputMode === 'realtime') {
      const triggerKeys = new Set(
        next.nodes.filter((node) => node.node_category === 'trigger').map((node) => node.node_key)
      );
      next = {
        nodes: next.nodes.filter((node) => !triggerKeys.has(node.node_key)),
        edges: next.edges.filter(
          (edge) => !triggerKeys.has(edge.from_node_key) && !triggerKeys.has(edge.to_node_key)
        )
      };
    }
    const source = next.nodes.find((node) => node.node_type === 'source.channel_frame') ??
      buildNode(sourceType, next.nodes, { x: 80, y: 160 });
    if (!next.nodes.some((node) => node.node_key === source.node_key)) {
      next = { ...next, nodes: [...next.nodes, source] };
    }
    if (inputMode === 'trigger') {
      const triggerType = props.nodeTypeMap.get('trigger.http_put');
      if (!triggerType) {
        props.onGraphChange(next);
        props.onSelectNode(source.node_key);
        return;
      }
      const trigger = next.nodes.find((node) => node.node_type === 'trigger.http_put') ??
        buildNode(triggerType, next.nodes, { x: 80, y: 64 });
      if (!next.nodes.some((node) => node.node_key === trigger.node_key)) {
        next = { ...next, nodes: [...next.nodes, trigger] };
      }
      const triggerEdge: RuntimeGraphEdge = {
        from_node_key: trigger.node_key,
        from_port: 'trigger',
        to_node_key: source.node_key,
        to_port: 'trigger'
      };
      if (!next.edges.some((edge) => runtimeEdgeKey(edge) === runtimeEdgeKey(triggerEdge))) {
        next = { ...next, edges: [...next.edges, triggerEdge] };
      }
      props.onGraphChange(next);
      props.onSelectNode(trigger.node_key);
      return;
    }
    props.onGraphChange(next);
    props.onSelectNode(source.node_key);
  };

  const builder = (
      <div className="runtime-guided-builder">
        <GuidedBuilderSection
          title="Input"
          actions={[
            {
              key: 'camera',
              label: 'Camera stream',
              icon: 'realtime',
              active: props.graph.nodes.some((node) => node.node_type === 'source.channel_frame') &&
                !props.graph.nodes.some((node) => node.node_category === 'trigger'),
              onClick: () => setInputMode('realtime')
            },
            {
              key: 'trigger',
              label: 'HTTP trigger',
              icon: 'endpoint',
              active: props.graph.nodes.some((node) => node.node_type === 'trigger.http_put'),
              disabled: !props.nodeTypeMap.has('trigger.http_put'),
              onClick: () => setInputMode('trigger')
            }
          ]}
        />
        {guidedBuilderCategorySections.map((category) => (
          <GuidedBuilderSection
            key={category}
            title={guidedBuilderSectionTitle(category)}
            groups={guidedActionGroupsForCategory({
              category,
              nodeTypes: props.nodeTypes,
              nodeTypeMap: props.nodeTypeMap,
              graph: props.graph,
              effectiveMode: props.effectiveMode,
              onAdd: addOrSelectNode
            })}
          />
        ))}
      </div>
  );
  return props.compact ? (
    <section className="runtime-guided-builder-compact">
      <h3>Pipeline zusammenstellen</h3>
      {builder}
    </section>
  ) : (
    <Panel title="Pipeline Builder">{builder}</Panel>
  );
}

type GuidedBuilderAction = {
  key: string;
  label: string;
  icon: AppIconName;
  description?: string;
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
};

type GuidedBuilderActionGroup = {
  label: string;
  actions: GuidedBuilderAction[];
};

function GuidedBuilderSection(props: {
  title: string;
  actions?: GuidedBuilderAction[];
  groups?: GuidedBuilderActionGroup[];
}) {
  const groups = (props.groups ?? [{ label: props.title, actions: props.actions ?? [] }])
    .filter((group) => group.actions.length);
  if (!groups.length) {
    return null;
  }
  return (
    <section className="runtime-guided-builder__section">
      <h3>{props.title}</h3>
      {groups.map((group) => (
        <div key={group.label} className="runtime-guided-builder__group">
          {props.groups ? <strong>{group.label}</strong> : null}
          <div className="runtime-guided-builder__actions">
            {group.actions.map((action) => (
              <button
                key={action.key}
                type="button"
                className={action.active ? 'runtime-guided-action runtime-guided-action--active' : 'runtime-guided-action'}
                disabled={action.disabled}
                title={action.description}
                onClick={action.onClick}
              >
                <AppIcon name={action.icon} />
                <span>{action.label}</span>
              </button>
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}

function guidedBuilderSectionTitle(category: RuntimeNodeCategory) {
  if (category === 'computer_vision') {
    return 'Computer Vision';
  }
  if (category === 'inference') {
    return 'AI evaluation';
  }
  if (category === 'analysis') {
    return 'Rules & measurements';
  }
  return categoryLabel(category);
}

export function guidedNodeGroupsForCategory(
  category: RuntimeNodeCategory,
  nodeTypes: RuntimeNodeTypeSummary[]
): { label: string; items: RuntimeNodeTypeSummary[] }[] {
  return paletteGroupsForCategory(
    category,
    nodeTypes.filter((nodeType) => nodeType.node_category === category && !isHiddenFromPalette(nodeType))
  );
}

function guidedActionGroupsForCategory(props: {
  category: RuntimeNodeCategory;
  nodeTypes: RuntimeNodeTypeSummary[];
  nodeTypeMap: Map<string, RuntimeNodeTypeSummary>;
  graph: RuntimePipelineGraph;
  effectiveMode: RuntimeMode;
  onAdd: (nodeTypeKey: string) => void;
}): GuidedBuilderActionGroup[] {
  return guidedNodeGroupsForCategory(props.category, props.nodeTypes)
    .map((group) => ({
      label: group.label,
      actions: group.items.map((nodeType) =>
        guidedActionForNodeType(
          nodeType,
          props.nodeTypeMap,
          props.graph,
          props.effectiveMode,
          props.onAdd
        )
      )
    }))
    .filter((group) => group.actions.length);
}

function guidedActionForNodeType(
  nodeType: RuntimeNodeTypeSummary,
  nodeTypeMap: Map<string, RuntimeNodeTypeSummary>,
  graph: RuntimePipelineGraph,
  effectiveMode: RuntimeMode,
  onAdd: (nodeTypeKey: string) => void
): GuidedBuilderAction {
  const active = graph.nodes.some((node) => node.node_type === nodeType.node_type);
  const eligibility = canAddNodeToGraph(graph, nodeType, nodeTypeMap, effectiveMode);
  const selectableExistingNode = active && guidedNodeIsSingleton(nodeType.node_type);
  return {
    key: nodeType.node_type,
    label: nodeType.display_name,
    icon: categoryIcon(nodeType.node_category),
    description: eligibility.reason || nodeType.description,
    active,
    disabled: !eligibility.eligible && !selectableExistingNode,
    onClick: () => onAdd(nodeType.node_type)
  };
}

function guidedNodeIsSingleton(nodeTypeKey: string) {
  return [
    'source.channel_frame',
    'trigger.http_put',
    'trigger.interval',
    'output.result',
    'output.overlay',
    'output.siemens_sps_write'
  ].includes(nodeTypeKey);
}

function guidedNodePosition(graph: RuntimePipelineGraph, nodeType: RuntimeNodeTypeSummary) {
  const categoryIndex = Math.max(0, categoryOrder.indexOf(nodeType.node_category));
  const sameCategory = graph.nodes.filter((node) => node.node_category === nodeType.node_category).length;
  return {
    x: 80 + categoryIndex * 220,
    y: 96 + sameCategory * 112
  };
}

function autoConnectNewNode(
  graph: RuntimePipelineGraph,
  newNode: RuntimeNodeSpec,
  newNodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>
): RuntimePipelineGraph {
  const edges = [...graph.edges];
  const occupiedInputs = new Set(edges.map((edge) => `${edge.to_node_key}:${edge.to_port}`));
  const existingNodes = graph.nodes
    .filter((node) => node.node_key !== newNode.node_key)
    .sort((first, second) => (Number(second.position?.x ?? 0) - Number(first.position?.x ?? 0)));
  for (const inputPort of newNodeType.input_ports.filter((port) => port.required)) {
    const inputKey = `${newNode.node_key}:${inputPort.key}`;
    if (occupiedInputs.has(inputKey)) {
      continue;
    }
    const edge = bestUpstreamEdgeForInput(
      existingNodes,
      inputPort,
      newNode,
      newNodeType,
      nodeTypes,
      new Set(edges.map(runtimeEdgeKey))
    );
    if (edge) {
      edges.push(edge);
      occupiedInputs.add(inputKey);
    }
  }
  return { ...graph, edges };
}

function bestUpstreamEdgeForInput(
  sourceNodes: RuntimeNodeSpec[],
  inputPort: RuntimeNodePort,
  newNode: RuntimeNodeSpec,
  newNodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>,
  existingEdgeKeys: Set<string>
): RuntimeGraphEdge | null {
  for (const sourceNode of sourceNodes) {
    const sourceType = nodeTypes.get(sourceNode.node_type);
    const outputPorts = sourceNode.output_ports.length ? sourceNode.output_ports : sourceType?.output_ports ?? [];
    for (const outputPort of outputPorts) {
      if (!portsCompatible(outputPort, inputPort, newNodeType)) {
        continue;
      }
      const edge: RuntimeGraphEdge = {
        from_node_key: sourceNode.node_key,
        from_port: outputPort.key,
        to_node_key: newNode.node_key,
        to_port: inputPort.key
      };
      if (!existingEdgeKeys.has(runtimeEdgeKey(edge))) {
        return edge;
      }
    }
  }
  return null;
}

function NodePalette(props: {
  graph: RuntimePipelineGraph;
  nodeTypes: RuntimeNodeTypeSummary[];
  nodeTypeMap: Map<string, RuntimeNodeTypeSummary>;
  effectiveMode: RuntimeMode;
  onAdd: (nodeType: RuntimeNodeTypeSummary) => void;
}) {
  const [search, setSearch] = useState('');
  const eligible = eligibleNodeTypesForGraph(props).filter(node => `${node.display_name} ${node.description}`.toLowerCase().includes(search.toLowerCase()));
  return <aside className="workspace-palette ws-scroll"><div className="ws-inspector-title">Bausteine</div><Search value={search} onChange={setSearch} placeholder="Baustein suchen …"/>{categoryOrder.map(category => {
    const items = eligible.filter(node => node.node_category === category);
    return items.length ? <section key={category}><h4>{categoryLabel(category)}</h4>{items.map(nodeType => <button className={`workspace-palette-node workspace-palette-node--${category}`} key={nodeType.node_type} draggable title={authoringConstraints(nodeType).palette_reason || nodeType.description} onClick={() => props.onAdd(nodeType)} onDragStart={event => {event.dataTransfer.setData('application/x-runtime-node', JSON.stringify({kind:'new-node', nodeType:nodeType.node_type} satisfies DragPayload));}}><AppIcon name={categoryIcon(category)}/><span>{nodeType.display_name}</span><Icon name="plus" size={12}/></button>)}</section> : null;
  })}{!eligible.length && <p className="ws-help">Keine passenden Bausteine verfügbar.</p>}</aside>;
}

function isHiddenFromPalette(nodeType: RuntimeNodeTypeSummary) {
  return nodeType.compatibility_constraints.hidden_from_palette === true;
}

type NodeAuthoringConstraints = {
  starter: boolean;
  singleton: boolean;
  blocks_with_node_types: string[];
  requires_upstream_data_types: string[];
  terminal: boolean;
  palette_reason: string;
};

type NodeEligibilityResult = {
  eligible: boolean;
  reason: string;
};

export function eligibleNodeTypesForGraph(props: {
  graph: RuntimePipelineGraph;
  nodeTypes: RuntimeNodeTypeSummary[];
  nodeTypeMap: Map<string, RuntimeNodeTypeSummary>;
  effectiveMode: RuntimeMode;
}) {
  return props.nodeTypes.filter(
    (nodeType) =>
      canAddNodeToGraph(
        props.graph,
        nodeType,
        props.nodeTypeMap,
        props.effectiveMode
      ).eligible
  );
}

export function canAddNodeToGraph(
  graph: RuntimePipelineGraph,
  nodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>,
  effectiveMode: RuntimeMode
): NodeEligibilityResult {
  if (isHiddenFromPalette(nodeType)) {
    return blocked('This node is hidden from normal authoring.');
  }
  const authoring = authoringConstraints(nodeType);
  if (!nodeSupportsAuthoringMode(nodeType, graph, effectiveMode)) {
    return blocked(`This node does not support ${modeLabel(effectiveMode)} execution.`);
  }
  if (graph.nodes.length === 0) {
    if (authoring.starter || nodeType.node_category === 'trigger' || nodeType.node_category === 'source') {
      return allowed(authoring.palette_reason || 'Valid starter node.');
    }
    return blocked('Start with a trigger or source node.');
  }
  const blockingReason = nodeTypeBlockedByExistingGraph(graph, nodeType, nodeTypes);
  if (blockingReason) {
    return blocked(blockingReason);
  }
  if (nodeType.node_category === 'trigger') {
    return triggerNodeEligible(graph, nodeType, nodeTypes);
  }
  if (nodeType.node_category === 'source') {
    return sourceNodeEligible(graph, nodeType, nodeTypes);
  }
  if (
    authoring.terminal &&
    !graphOutputPorts(graph, nodeTypes).some((port) => !['frame', 'trigger'].includes(port.data_type))
  ) {
    return blocked('Terminal nodes need an upstream result-producing node.');
  }
  const missingInput = missingRequiredUpstreamInput(graph, nodeType, nodeTypes);
  if (missingInput) {
    return blocked(missingInput);
  }
  return allowed(authoring.palette_reason || 'Compatible with the current graph outputs.');
}

function nodeTypeBlockedByExistingGraph(
  graph: RuntimePipelineGraph,
  nodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>
) {
  const authoring = authoringConstraints(nodeType);
  if (nodeType.node_category === 'source' && !nodeType.compatibility_constraints.auxiliary_source
      && graph.nodes.some((node) => node.node_category === 'source'
        && !nodeTypes.get(node.node_type)?.compatibility_constraints.auxiliary_source)) {
    return 'A pipeline can use only one source node.';
  }
  if (authoring.singleton && graph.nodes.some((node) => node.node_type === nodeType.node_type)) {
    return 'This node type can be added only once.';
  }
  if (
    graph.nodes.some(
      (node) =>
        authoring.blocks_with_node_types.includes(node.node_type) ||
        authoringConstraints(nodeTypes.get(node.node_type)).blocks_with_node_types.includes(nodeType.node_type)
    )
  ) {
    return 'This node conflicts with an existing node in the graph.';
  }
  if (
    nodeType.node_category === 'trigger' &&
    graph.nodes.some((node) => node.node_category === 'trigger')
  ) {
    return 'HTTP and interval triggers are mutually exclusive.';
  }
  return null;
}

function triggerNodeEligible(
  graph: RuntimePipelineGraph,
  nodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>
): NodeEligibilityResult {
  const triggerPorts = nodeType.output_ports.filter((port) => port.data_type === 'trigger');
  const waitingSource = graph.nodes.some((node) => {
    const sourceType = nodeTypes.get(node.node_type);
    if (!sourceType || node.node_category !== 'source') {
      return false;
    }
    return triggerPorts.some((port) => canConnectToSourceNode(port, node, sourceType));
  });
  if (waitingSource) {
    return allowed(authoringConstraints(nodeType).palette_reason || 'Can drive the existing channel source.');
  }
  return blocked('Triggers can only be added before or with a trigger-capable channel source.');
}

function sourceNodeEligible(
  graph: RuntimePipelineGraph,
  nodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>
): NodeEligibilityResult {
  const hasTrigger = graph.nodes.some((node) => node.node_category === 'trigger');
  const missingInput = missingRequiredUpstreamInput(graph, nodeType, nodeTypes);
  if (missingInput) {
    return blocked(missingInput);
  }
  if (hasTrigger && !nodeType.compatibility_constraints.auxiliary_source
      && !nodeType.input_ports.some((port) => port.data_type === 'trigger')) {
    return blocked('Triggered graphs require the channel frame source.');
  }
  return allowed(authoringConstraints(nodeType).palette_reason || 'Compatible source for the current graph.');
}

function missingRequiredUpstreamInput(
  graph: RuntimePipelineGraph,
  nodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>
) {
  const outputs = graphOutputPorts(graph, nodeTypes);
  const requiredPorts = nodeType.input_ports.filter((port) => port.required);
  for (const inputPort of requiredPorts) {
    if (!outputs.some((outputPort) => portsCompatible(outputPort, inputPort, nodeType))) {
      return `Needs upstream ${inputPort.data_type} data.`;
    }
  }
  for (const dataType of authoringConstraints(nodeType).requires_upstream_data_types) {
    if (!outputs.some((outputPort) => dataTypeCompatible(outputPort.data_type, dataType, nodeType))) {
      return `Needs upstream ${dataType} data.`;
    }
  }
  return null;
}

function graphOutputPorts(
  graph: RuntimePipelineGraph,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>
) {
  return graph.nodes.flatMap((node) => {
    const spec = nodeTypes.get(node.node_type);
    return node.output_ports.length ? node.output_ports : spec?.output_ports ?? [];
  });
}

function nodeSupportsAuthoringMode(
  nodeType: RuntimeNodeTypeSummary,
  graph: RuntimePipelineGraph,
  effectiveMode: RuntimeMode
) {
  if (nodeType.supported_modes.includes(effectiveMode)) {
    return true;
  }
  if (
    nodeType.node_category === 'trigger' &&
    graph.nodes.some((node) => node.node_type === 'source.channel_frame')
  ) {
    // Adding an ingress changes the graph mode; evaluate its source compatibility below.
    return true;
  }
  if (graph.nodes.length === 0 && ['trigger', 'source'].includes(nodeType.node_category)) {
    return true;
  }
  return false;
}

function dataTypeCompatible(
  fromDataType: string,
  toDataType: string,
  toNodeType: RuntimeNodeTypeSummary
) {
  return portsCompatible(
    { key: 'out', data_type: fromDataType, required: true },
    { key: 'in', data_type: toDataType, required: true },
    toNodeType
  );
}

function authoringConstraints(
  nodeType: RuntimeNodeTypeSummary | undefined
): NodeAuthoringConstraints {
  const raw =
    nodeType?.compatibility_constraints.authoring &&
    typeof nodeType.compatibility_constraints.authoring === 'object'
      ? (nodeType.compatibility_constraints.authoring as Record<string, unknown>)
      : {};
  return {
    starter: raw.starter === true,
    singleton: raw.singleton === true,
    blocks_with_node_types: arrayOfStrings(raw.blocks_with_node_types),
    requires_upstream_data_types: arrayOfStrings(raw.requires_upstream_data_types),
    terminal: raw.terminal === true,
    palette_reason: typeof raw.palette_reason === 'string' ? raw.palette_reason : ''
  };
}

function arrayOfStrings(value: unknown) {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : [];
}

function allowed(reason: string): NodeEligibilityResult {
  return { eligible: true, reason };
}

function blocked(reason: string): NodeEligibilityResult {
  return { eligible: false, reason };
}

function enabledSiemensSpsWriteNodes(graph: RuntimePipelineGraph) {
  return graph.nodes.filter(
    (node) =>
      node.node_type === 'output.siemens_sps_write' &&
      Boolean(node.config.enabled) &&
      !node.config.dry_run
  );
}

function PipelineCanvas(props: {
  graph: RuntimePipelineGraph;
  nodeTypes: Map<string, RuntimeNodeTypeSummary>;
  effectiveMode: RuntimeMode;
  selectedNodeKey: string | null;
  selectedEdgeKey: string | null;
  onSelectNode: (nodeKey: string | null) => void;
  onSelectEdge: (edgeKey: string | null) => void;
  onGraphChange: (graph: RuntimePipelineGraph) => void;
  onNodeDropRejected?: (nodeType: RuntimeNodeTypeSummary, reason: string) => void;
}) {
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({x: 40, y: 40});
  const viewport = useRef<HTMLDivElement>(null);
  const dragging = useRef<{x: number; y: number; panX: number; panY: number} | null>(null);
  const fit = () => {
    const bounds = viewport.current?.getBoundingClientRect(); if (!bounds) return;
    const minX = Math.min(0, ...props.graph.nodes.map(node => node.position.x ?? 24));
    const minY = Math.min(0, ...props.graph.nodes.map(node => node.position.y ?? 24));
    const width = Math.max(400, ...props.graph.nodes.map(node => (node.position.x ?? 24) + nodeWidth)) - minX;
    const height = Math.max(240, ...props.graph.nodes.map(node => (node.position.y ?? 24) + nodeHeight)) - minY;
    const nextZoom = Math.max(.15, Math.min(1.25, (bounds.width - 80) / width, (bounds.height - 80) / height));
    setZoom(nextZoom); setPan({x: (bounds.width - width * nextZoom) / 2 - minX * nextZoom, y: (bounds.height - height * nextZoom) / 2 - minY * nextZoom});
  };
  const fitted = useRef(false);
  useEffect(() => { if (!fitted.current && props.graph.nodes.length && viewport.current?.clientWidth) { fit(); fitted.current = true; } });
  const nodeByKey = new Map(props.graph.nodes.map((node) => [node.node_key, node]));
  return (
    <section className="workspace-graph-center"><div ref={viewport} className="workspace-graph-viewport" aria-label="Pipeline Canvas" onPointerDown={event => {if (event.button !== 0 || (event.target as Element).closest('button, path')) return; dragging.current = {x:event.clientX,y:event.clientY,panX:pan.x,panY:pan.y}; event.currentTarget.setPointerCapture(event.pointerId);}} onPointerMove={event => {if (dragging.current) setPan({x:dragging.current.panX+event.clientX-dragging.current.x,y:dragging.current.panY+event.clientY-dragging.current.y});}} onPointerUp={() => {dragging.current=null;}} onPointerCancel={() => {dragging.current=null;}}>
      <div
        className="runtime-canvas"
        style={{transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`, width: Math.max(3000, ...props.graph.nodes.map(n => (n.position.x ?? 24) + nodeWidth + 500)), height: Math.max(2000, ...props.graph.nodes.map(n => (n.position.y ?? 24) + nodeHeight + 500))}}
        onDragOver={(event) => event.preventDefault()}
        onDrop={(event) => {
          event.preventDefault();
          const payload = readDragPayload(event);
          if (!payload) {
            return;
          }
          const rect = event.currentTarget.getBoundingClientRect();
          if (payload.kind === 'new-node') {
            const nodeType = props.nodeTypes.get(payload.nodeType);
            if (!nodeType) {
              return;
            }
            const eligibility = canAddNodeToGraph(
              props.graph,
              nodeType,
              props.nodeTypes,
              props.effectiveMode
            );
            if (!eligibility.eligible) {
              props.onNodeDropRejected?.(nodeType, eligibility.reason);
              return;
            }
            const node = buildNode(nodeType, props.graph.nodes, {
              x: (event.clientX - rect.left) / zoom - nodeWidth / 2,
              y: (event.clientY - rect.top) / zoom - nodeHeight / 2
            });
            props.onGraphChange({ ...props.graph, nodes: [...props.graph.nodes, node] });
            props.onSelectNode(node.node_key);
            return;
          }
          props.onGraphChange({
            ...props.graph,
            nodes: props.graph.nodes.map((node) =>
              node.node_key === payload.nodeKey
                ? {
                    ...node,
                    position: {
                      x: Math.max(8, (event.clientX - rect.left) / zoom - payload.offsetX),
                      y: Math.max(8, (event.clientY - rect.top) / zoom - payload.offsetY)
                    }
                  }
                : node
            )
          });
        }}
        onClick={() => {
          props.onSelectNode(null);
          props.onSelectEdge(null);
        }}
      >
        <svg className="runtime-canvas__edges" aria-hidden="true">
          {props.graph.edges.map((edge, index) => {
            const from = nodeByKey.get(edge.from_node_key);
            const to = nodeByKey.get(edge.to_node_key);
            if (!from || !to) {
              return null;
            }
            const fromPoint = nodeCenter(from);
            const toPoint = nodeCenter(to);
            const edgeKey = runtimeEdgeKey(edge);
            return (
              <g key={`${edge.from_node_key}-${edge.to_node_key}-${index}`}>
                <path
                  className={props.selectedEdgeKey === edgeKey ? 'runtime-canvas__edge--selected' : ''}
                  d={`M ${fromPoint.x} ${fromPoint.y} C ${fromPoint.x + 80} ${fromPoint.y}, ${toPoint.x - 80} ${toPoint.y}, ${toPoint.x} ${toPoint.y}`}
                  onClick={(event) => {
                    event.stopPropagation();
                    props.onSelectEdge(edgeKey);
                  }}
                />
              </g>
            );
          })}
        </svg>
        {props.graph.nodes.length ? (
          props.graph.nodes.map((node) => {
            const nodeType = props.nodeTypes.get(node.node_type);
            return (
              <button
                key={node.node_key}
                type="button"
                aria-label={nodeType?.display_name ?? node.node_type}
                aria-pressed={props.selectedNodeKey === node.node_key}
                className={[
                  'runtime-canvas-node',
                  `runtime-canvas-node--${node.node_category}`,
                  props.selectedNodeKey === node.node_key ? 'runtime-canvas-node--selected' : ''
                ].join(' ')}
                style={{
                  left: node.position.x ?? 24,
                  top: node.position.y ?? 24,
                  width: nodeWidth,
                  height: nodeHeight
                }}
                draggable
                onClick={(event) => {
                  event.stopPropagation();
                  props.onSelectNode(node.node_key);
                  props.onSelectEdge(null);
                }}
                onDragStart={(event) => {
                  const rect = event.currentTarget.getBoundingClientRect();
                  event.dataTransfer.setData(
                    'application/x-runtime-node',
                    JSON.stringify({
                      kind: 'move-node',
                      nodeKey: node.node_key,
                      offsetX: (event.clientX - rect.left) / zoom,
                      offsetY: (event.clientY - rect.top) / zoom
                    } satisfies DragPayload)
                  );
                }}
              >
                <span>{nodeType?.display_name ?? node.node_type}</span>
                <small>{categoryLabel(node.node_category)}</small>
              </button>
            );
          })
        ) : (
          <div className="runtime-canvas__empty">
            <AppIcon name="pipeline" />
            <span>Drag a source node here to begin</span>
          </div>
        )}
      </div>
    </div><div className="workspace-graph-zoom"><ToolButton icon="fit" label="Graph einpassen" onClick={fit}/><ToolButton icon="minus" label="Graph verkleinern" disabled={zoom <= .15} onClick={() => setZoom(value => Math.max(.15,value-.15))}/><button onClick={() => setZoom(1)}>{Math.round(zoom*100)}%</button><ToolButton icon="plus" label="Graph vergrößern" disabled={zoom >= 2} onClick={() => setZoom(value => Math.min(2,value+.15))}/></div><span className="workspace-graph-hint">Ziehen: verschieben · Baustein: Eigenschaften · Entf: entfernen</span></section>
  );
}

function NodePropertiesModal(props: {
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec | null;
  nodeType: RuntimeNodeTypeSummary | null;
  nodeTypes: Map<string, RuntimeNodeTypeSummary>;
  outputCatalog: RuntimePipelineOutputCatalog | null;
  channels: RuntimeChannelSummary[];
  modelCapabilities: RuntimeModelCapabilityOption[];
  labelProjects: LabelProjectSummary[];
  authoringContext: RuntimeChannelAuthoringContext | null;
  authoringLiveState: RuntimeLiveViewState | null;
  calibrationChannelId: string | null;
  capturedCalibrationFrame: RuntimeLiveFrame | null;
  calibrationCaptureAvailable: boolean;
  calibrationCapturePending: boolean;
  previewPending: boolean;
  saveAvailable: boolean;
  savePending: boolean;
  onSave: () => void;
  hasUncheckedChanges: boolean;
  previewFrozen: boolean;
  onTogglePreviewFreeze: () => void;
  onCaptureCalibrationFrame: () => void;
  validationReport: RuntimePipelineValidationReport | null;
  selectedEdgeKey: string | null;
  onGraphChange: (graph: RuntimePipelineGraph) => void;
  onSelectNode: (nodeKey: string | null) => void;
  onSelectEdge: (edgeKey: string | null) => void;
  onClose: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [selectedConnection, setSelectedConnection] = useState('');
  const compatibleConnections = useMemo(
    () =>
      props.node && props.nodeType
        ? buildCompatibleConnections(props.graph, props.node, props.nodeType, props.nodeTypes)
        : [],
    [props.graph, props.node, props.nodeType, props.nodeTypes]
  );

  useEffect(() => {
    setSelectedConnection(compatibleConnections[0]?.key ?? '');
  }, [compatibleConnections]);

  if (!props.node || !props.nodeType) {
    return <aside className="workspace-node-inspector"><div className="ws-inspector-title">Eigenschaften</div><div className="ws-empty"><Icon name="graph" size={28}/><strong>Baustein auswählen</strong><p>Parameter und Verbindungen erscheinen hier. Bild und Kalibrierung öffnen sich in einer vergrößerten Arbeitsansicht.</p></div></aside>;
  }

  const nodeIssues =
    props.validationReport?.issues.filter((issue) => issue.node_key === props.node?.node_key) ?? [];
  const groupedFields = groupPropertyFields(visiblePropertyFields(props.nodeType.property_schema, props.node));

  const updateNode = (patch: Partial<RuntimeNodeSpec>) => {
    props.onGraphChange({
      ...props.graph,
      nodes: props.graph.nodes.map((node) =>
        node.node_key === props.node?.node_key ? { ...node, ...patch } : node
      )
    });
  };
  const renderPropertyField = (field: RuntimeNodePropertySpec) => {
    if (!expanded && (field.field_type === 'image_view_profile' || guidedStructuredPropertyField(field))) return <button key={field.key} onClick={() => setExpanded(true)}><Icon name="expand" size={14}/>{field.label} öffnen</button>;
    const editor = (
      <NodeField
        field={field}
        graph={props.graph}
        node={props.node!}
        nodeTypes={props.nodeTypes}
        outputCatalog={props.outputCatalog}
        liveState={props.authoringLiveState}
        calibrationChannelId={props.calibrationChannelId}
        capturedCalibrationFrame={props.capturedCalibrationFrame}
        calibrationCaptureAvailable={props.calibrationCaptureAvailable}
        calibrationCapturePending={props.calibrationCapturePending}
        onCaptureCalibrationFrame={props.onCaptureCalibrationFrame}
        channels={props.channels}
        modelCapabilities={props.modelCapabilities}
        labelProjects={props.labelProjects}
        authoringContext={props.authoringContext}
        onChange={(value) => {
          if (field.field_type === 'model_ref') {
            const bindingGroup = String(props.node!.config.model_binding_group ?? '').trim();
            if (bindingGroup) {
              props.onGraphChange({
                ...props.graph,
                nodes: props.graph.nodes.map((node) =>
                  String(node.config.model_binding_group ?? '').trim() === bindingGroup
                    ? { ...node, logical_model_ref: value as RuntimeLogicalModelRef }
                    : node
                )
              });
            } else {
              updateNode({ logical_model_ref: value as RuntimeLogicalModelRef });
            }
            return;
          }
          if (field.field_type === 'image_view_profile') {
            props.onGraphChange(
              applyImageViewProfileToGraph(props.graph, props.node!.node_key, field.key, value)
            );
            return;
          }
          updateNode({ config: { ...props.node!.config, [field.key]: value } });
        }}
      />
    );
    return guidedStructuredPropertyField(field) ? (
      <details
        key={field.key}
        className="runtime-advanced-graph"
        open={guidedStructuredPropertyInitiallyOpen(props.node!, field)}
      >
        <summary>{field.label}</summary>
        {editor}
      </details>
    ) : (
      <div key={field.key}>{editor}</div>
    );
  };

  const content = (
      <section className="runtime-node-properties-modal" aria-labelledby="runtime-node-properties-title">
        <div className="runtime-node-properties-modal__header">
          <div>
            <p className="ws-overline">Eigenschaften</p>
            <h2 id="runtime-node-properties-title">{props.nodeType.display_name}</h2>

          </div>
          <div className="actions-row">
            {expanded && <button
              type="button"
              className="button--primary"
              disabled={!props.saveAvailable || props.savePending}
              onClick={props.onSave}
            >
              {props.savePending ? 'Speichert …' : 'Pipeline speichern'}
            </button>}
            <button
              type="button"
              className="ws-icon-button" aria-label="Baustein entfernen" title="Baustein entfernen"
              onClick={() => {
                props.onGraphChange(removeNode(props.graph, props.node!.node_key));
                props.onSelectNode(null);
                props.onSelectEdge(null);
              }}
            >
              <Icon name="trash" size={15}/>
            </button>
            <button type="button" className="ws-icon-button" aria-label="Eigenschaften schließen" onClick={() => {setExpanded(false); props.onClose();}}>
              <AppIcon name="close" />
            </button>
          </div>
        </div>

        <button type="button" onClick={() => setExpanded(v => !v)}>{expanded ? 'Im Inspektor anzeigen' : 'Bild & Kalibrierung öffnen'}</button>
        <div
          className={[
            'runtime-property-panel',
            props.node.node_type === 'analysis.board_end_coordinate_frame'
              ? 'runtime-property-panel--board-end-tuning'
              : ''
          ]
            .filter(Boolean)
            .join(' ')}
        >
          <section className="runtime-property-section">
            <details className="workspace-node-overview"><summary>Baustein-Informationen</summary><div className="runtime-property-overview">
              <div className="metric-pill">
                <span>{categoryLabel(props.node.node_category)}</span>
                <strong>{props.node.node_key}</strong>
                <small className="subtle-text">{props.node.node_type}</small>
              </div>
              <div className="metric-pill">
                <span>Visual data</span>
                <strong>{visualContractLabel(props.nodeType)}</strong>
                <small className="subtle-text">{visualContractHelp(props.nodeType)}</small>
              </div>
              {props.authoringContext ? (
                <div className="metric-pill">
                  <span>Channel labels</span>
                  <strong>{props.authoringContext.labels.length || 'None'}</strong>
                  <small className="subtle-text">
                    {props.authoringContext.label_projects.map((project) => project.title).join(', ') || 'No label project selected'}
                  </small>
                </div>
              ) : null}
            </div>
            </details>{expanded && <NodeVisualEditor
              node={props.node}
              nodeType={props.nodeType}
              liveState={props.authoringLiveState}
              previewPending={props.previewPending}
              previewFrozen={props.previewFrozen}
              onTogglePreviewFreeze={props.onTogglePreviewFreeze}
              onConfigPatch={(patch) => updateNode({ config: { ...props.node!.config, ...patch } })}
            />}
            {props.node.node_type === 'preprocessing.process_coordinate_rectify' ? (
              <div className="inline-alert inline-alert--info">
                <strong>This node has two separate jobs</strong>
                <p className="subtle-text">
                  Step 1 defines the exact perspective-preserving model and Label Studio image.
                  Step 2 calibrates plant X/Y and optionally a spatial camera pose using measured Z. Image
                  appearance and metric measurement are deliberately kept separate.
                </p>
              </div>
            ) : null}
            {props.node.node_type === 'analysis.stem_measurement_eligibility' ? (
              <div className="inline-alert inline-alert--info">
                <strong>Configure on the canonical model image</strong>
                <p className="subtle-text">
                  Drag one measurement rectangle and draw the crowd-boundary line toward the
                  waiting stems. The rectangle limits valid active stems; the line rejects contact.
                  Neither geometry changes the pixels already selected in Step 1.
                </p>
              </div>
            ) : null}
            {props.hasUncheckedChanges && !props.validationReport ? (
              <div className="inline-alert inline-alert--info">
                <strong>Settings changed</strong>
                <p className="subtle-text">
                  Save pipeline changes to refresh the concrete list of remaining settings.
                  Changing an unrelated field does not resolve previous setup issues by itself.
                </p>
              </div>
            ) : null}
          </section>

          {nodeIssues.length ? (
            <section className="runtime-property-section">
              <h3>What this node still needs</h3>
              <div className="runtime-validation-list">
                {nodeIssues.map((issue, index) => (
                  <article key={`${issue.code}-${index}`} className={`runtime-validation-item runtime-validation-item--${issue.severity}`}>
                    <strong>{issue.message}</strong>
                    <span>{issue.field ?? issue.code}</span>
                  </article>
                ))}
              </div>
            </section>
          ) : null}

          {groupedFields.map((group, index) => {
            const fields = (
              <div className="runtime-property-section__fields">
                {group.fields.map(renderPropertyField)}
              </div>
            );
            return compactStemPropertySections(props.node!) ? (
              <details
                key={group.section}
                className="runtime-property-section"
                open={
                  props.node!.node_type === 'preprocessing.process_coordinate_rectify' ||
                  index === 0 ||
                  group.section === 'Geometry'
                }
              >
                <summary>{group.section}</summary>
                {fields}
              </details>
            ) : (
              <section key={group.section} className="runtime-property-section">
                <h3>{group.section}</h3>
                {fields}
              </section>
            );
          })}

          <section className="runtime-property-section runtime-connection-panel">
            <h3>Connections</h3>
            {compatibleConnections.length ? (
              <div className="actions-row">
                <select value={selectedConnection} onChange={(event) => setSelectedConnection(event.target.value)}>
                  {compatibleConnections.map((connection) => (
                    <option key={connection.key} value={connection.key}>
                      {connection.label}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  onClick={() => {
                    const connection = compatibleConnections.find((item) => item.key === selectedConnection);
                    if (!connection) {
                      return;
                    }
                    props.onGraphChange({
                      ...props.graph,
                      edges: [...props.graph.edges, connection.edge]
                    });
                  }}
                >
                  Connect
                </button>
              </div>
            ) : (
              <p className="subtle-text">No compatible downstream port is currently available.</p>
            )}
            {props.graph.edges
              .filter((edge) => edge.from_node_key === props.node?.node_key || edge.to_node_key === props.node?.node_key)
              .map((edge, index) => {
                const edgeKey = runtimeEdgeKey(edge);
                return (
                  <div
                    key={`${edge.from_node_key}-${edge.to_node_key}-${index}`}
                    className={`runtime-edge-row ${
                      props.selectedEdgeKey === edgeKey ? 'runtime-edge-row--selected' : ''
                    }`}
                  >
                    <button
                      type="button"
                      className="runtime-edge-row__select"
                      onClick={() => {
                        props.onSelectEdge(edgeKey);
                      }}
                    >
                      {edge.from_node_key}.{edge.from_port} to {edge.to_node_key}.{edge.to_port}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        props.onGraphChange({
                          ...props.graph,
                          edges: props.graph.edges.filter((candidate) => runtimeEdgeKey(candidate) !== edgeKey)
                        });
                        props.onSelectEdge(null);
                      }}
                    >
                      Remove
                    </button>
                  </div>
                );
              })}
          </section>

        </div>
      </section>
  );
  return expanded ? <Modal wide title="Bild & Kalibrierung" onClose={() => setExpanded(false)}>{content}</Modal> : <aside className="workspace-node-inspector">{content}</aside>;
}

function previewMediaStyle(width: number | null, height: number | null): CSSProperties {
  return {
    aspectRatio: width && height ? `${width} / ${height}` : '16 / 9'
  };
}

type CropEditorCoordinateSpace = {
  x: number;
  y: number;
  width: number;
  height: number;
  sourceWidth: number;
  sourceHeight: number;
};

export function cropEditorLiveState(
  liveState: RuntimeLiveViewState | null
): RuntimeLiveViewState | null {
  const sourceFrame = liveState?.source_frame ?? null;
  if (!liveState || !sourceFrame) {
    return liveState;
  }
  const sourceContext = recordValue(sourceFrame.image_context);
  return {
    ...liveState,
    display_context_id:
      typeof sourceContext.context_id === 'string'
        ? sourceContext.context_id
        : liveState.display_context_id,
    display_context: sourceContext,
    frame: sourceFrame
  };
}

export function cropEditorCoordinateSpace(
  liveState: RuntimeLiveViewState | null
): CropEditorCoordinateSpace | null {
  const frame = liveState?.frame ?? null;
  const frameWidth = frame?.width ?? null;
  const frameHeight = frame?.height ?? null;
  if (!frameWidth || !frameHeight) {
    return null;
  }
  const frameContext = recordValue(frame?.image_context);
  const displayContext = recordValue(liveState?.display_context);
  const imageContext = Object.keys(frameContext).length ? frameContext : displayContext;
  const transform = recordValue(imageContext.transform);
  if (transform.transform_type !== 'crop') {
    return {
      x: 0,
      y: 0,
      width: frameWidth,
      height: frameHeight,
      sourceWidth: frameWidth,
      sourceHeight: frameHeight
    };
  }
  const parameters = recordValue(transform.parameters);
  const x = Math.max(0, finiteNumber(parameters.x, 0));
  const y = Math.max(0, finiteNumber(parameters.y, 0));
  const width = Math.max(1, finiteNumber(parameters.width, frameWidth));
  const height = Math.max(1, finiteNumber(parameters.height, frameHeight));
  return {
    x,
    y,
    width,
    height,
    sourceWidth: Math.max(width + x, finiteNumber(parameters.source_width, width + x)),
    sourceHeight: Math.max(height + y, finiteNumber(parameters.source_height, height + y))
  };
}

export function cropEditorConfigFromBox(
  liveState: RuntimeLiveViewState | null,
  box: Pick<StructuredBox, 'x' | 'y' | 'width' | 'height'>
) {
  const space = cropEditorCoordinateSpace(liveState);
  if (!space) {
    return null;
  }
  return {
    x: Math.round(space.x + box.x * space.width),
    y: Math.round(space.y + box.y * space.height),
    width: Math.max(1, Math.round(box.width * space.width)),
    height: Math.max(1, Math.round(box.height * space.height))
  };
}

function NodeVisualEditor(props: {
  node: RuntimeNodeSpec;
  nodeType: RuntimeNodeTypeSummary;
  liveState: RuntimeLiveViewState | null;
  previewPending: boolean;
  previewFrozen: boolean;
  onTogglePreviewFreeze: () => void;
  onConfigPatch: (patch: Record<string, unknown>) => void;
}) {
  if (props.node.node_type === 'analysis.board_end_coordinate_frame') {
    return (
      <BoardEndLiveTuningPreview
        liveState={props.liveState}
        pending={props.previewPending}
        frozen={props.previewFrozen}
        onToggleFreeze={props.onTogglePreviewFreeze}
      />
    );
  }
  if (props.node.node_type === 'analysis.stem_curvature_phase_solver') {
    return (
      <StemCurvaturePhaseRuntimePanel
        overlay={props.liveState?.overlay}
        title="Draft Preview: Krümmung über Rollphase"
        compact
      />
    );
  }
  if (props.node.node_type !== 'preprocessing.crop') {
    return null;
  }
  const editorLiveState = cropEditorLiveState(props.liveState);
  const frame = editorLiveState?.frame ?? null;
  const frameWidth = frame?.width ?? null;
  const frameHeight = frame?.height ?? null;
  if (!frameWidth || !frameHeight) {
    return (
      <div className="runtime-live-editor runtime-live-editor--empty">
        <span>Live editor</span>
        <p className="subtle-text">Start the channel runtime or invoke a trigger pipeline to load a frame.</p>
      </div>
    );
  }
  const coordinateSpace = cropEditorCoordinateSpace(editorLiveState) ?? {
    x: 0,
    y: 0,
    width: frameWidth,
    height: frameHeight,
    sourceWidth: frameWidth,
    sourceHeight: frameHeight
  };
  const x = clampNumber(finiteNumber(props.node.config.x, 0), 0, coordinateSpace.sourceWidth - 1);
  const y = clampNumber(finiteNumber(props.node.config.y, 0), 0, coordinateSpace.sourceHeight - 1);
  const width = clampNumber(
    finiteNumber(props.node.config.width, Math.min(640, coordinateSpace.sourceWidth)),
    1,
    coordinateSpace.sourceWidth - x
  );
  const height = clampNumber(
    finiteNumber(props.node.config.height, Math.min(480, coordinateSpace.sourceHeight)),
    1,
    coordinateSpace.sourceHeight - y
  );
  const visibleLeft = Math.max(x, coordinateSpace.x);
  const visibleTop = Math.max(y, coordinateSpace.y);
  const visibleRight = Math.min(x + width, coordinateSpace.x + coordinateSpace.width);
  const visibleBottom = Math.min(y + height, coordinateSpace.y + coordinateSpace.height);
  const normalized: StructuredBox[] =
    visibleRight > visibleLeft && visibleBottom > visibleTop
      ? [
          {
            label: 'Crop',
            x: (visibleLeft - coordinateSpace.x) / coordinateSpace.width,
            y: (visibleTop - coordinateSpace.y) / coordinateSpace.height,
            width: (visibleRight - visibleLeft) / coordinateSpace.width,
            height: (visibleBottom - visibleTop) / coordinateSpace.height,
            angle_degrees: 0
          }
        ]
      : [];
  return (
    <VisualBoxEditor
      title="Crop window"
      liveState={editorLiveState}
      boxes={normalized}
      mode="crop"
      maxBoxes={1}
      onChange={(boxes) => {
        const box = boxes[0];
        if (!box) {
          return;
        }
        const config = cropEditorConfigFromBox(editorLiveState, box);
        if (config) {
          props.onConfigPatch(config);
        }
      }}
    />
  );
}

const boardEndPreviewLayers = new Set([
  'board_color_region',
  'surface_boundary_candidates',
  'board_end_candidates',
  'board_axis_candidates',
  'board_end',
  'board_axis_reference',
  'board_longitudinal_axis',
  'board_end_error'
]);

function BoardEndLiveTuningPreview(props: {
  liveState: RuntimeLiveViewState | null;
  pending: boolean;
  frozen: boolean;
  onToggleFreeze: () => void;
}) {
  const frame = props.liveState?.frame ?? null;
  const imageSource = liveFrameImageSource(props.liveState);
  const width = frame?.width ?? null;
  const height = frame?.height ?? null;
  if (!imageSource || !width || !height) {
    return (
      <div className="runtime-live-editor runtime-live-editor--empty">
        <span>Live edge diagnosis</span>
        <p className="subtle-text">
          Start the channel or capture one frame. Slider changes are then evaluated here
          without saving or publishing the pipeline.
        </p>
      </div>
    );
  }
  const primitives =
    props.liveState?.overlay.filter((primitive) =>
      boardEndPreviewLayers.has(primitive.layer)
    ) ?? [];
  const selected =
    primitives.find((primitive) => primitive.layer === 'board_end') ??
    primitives.find((primitive) => primitive.layer === 'board_end_error') ??
    null;
  const source = selected?.metadata ?? {};
  const found = source.found === true;
  const reason =
    typeof source.reason_code === 'string' ? source.reason_code : 'waiting_for_preview';
  const candidateCount = primitives.filter(
    (primitive) =>
      primitive.layer === 'surface_boundary_candidates' ||
      primitive.layer === 'board_end_candidates' ||
      primitive.layer === 'board_axis_candidates'
  ).length;
  const endCandidateCount = Math.round(
    finiteNumber(source.qualified_end_line_count, 0)
  );
  const longitudinalCandidateCount = Math.round(
    finiteNumber(source.qualified_longitudinal_line_count, 0)
  );
  const surfaceBoundaryCandidateCount = Math.round(
    finiteNumber(source.surface_boundary_candidate_count, 0)
  );
  const boardColorRegionFound = source.board_color_region_found === true;
  const boardColorRegionConfidence = Math.round(
    finiteNumber(source.board_color_region_confidence, 0) * 100
  );
  const nearestJoinDistance =
    typeof source.nearest_join_distance_px === 'number'
      ? source.nearest_join_distance_px
      : null;
  const allowedJoinDistance = finiteNumber(
    source.maximum_reference_join_gap_px,
    0
  );
  const cornerAssociation =
    typeof source.corner_association === 'string'
      ? source.corner_association
      : null;
  const surfaceConsensusCount = Math.round(
    finiteNumber(source.surface_boundary_consensus_count, 0)
  );
  const diagnosis = boardEndPreviewDiagnosis(
    reason,
    source,
    endCandidateCount,
    longitudinalCandidateCount
  );

  return (
    <div className="runtime-live-editor runtime-board-end-preview">
      <div className="runtime-live-editor__topline">
        <span>Live edge diagnosis / unpublished draft</span>
        <div className="runtime-board-end-preview__actions">
          <button type="button" onClick={props.onToggleFreeze}>
            {props.frozen ? 'Use live frames' : 'Freeze this frame'}
          </button>
          <MetricPill
            label={props.pending ? 'Evaluating' : found ? 'Board corner' : 'No valid corner'}
            value={
              props.pending
                ? '...'
                : found
                  ? `${Math.round(finiteNumber(selected?.confidence, 0) * 100)}%`
                  : reason
            }
            tone={found ? 'success' : 'warn'}
          />
        </div>
      </div>
      <div
        className="runtime-live-editor__canvas runtime-board-end-preview__canvas"
        style={previewMediaStyle(width, height)}
      >
        <img src={imageSource} alt="Current board-end tuning frame" draggable={false} />
        <svg
          className="runtime-board-end-preview__overlay"
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          aria-label="Detected board-end and longitudinal candidates"
        >
          {primitives.map((primitive, index) => (
            <BoardEndPreviewPrimitive
              key={`${primitive.layer}-${index}`}
              primitive={primitive}
            />
          ))}
        </svg>
      </div>
      <div className="runtime-board-end-preview__metrics">
        <span>
          Input frame{' '}
          <strong>
            #{frame?.sequence ?? 0} / {props.frozen ? 'frozen bytes' : 'live'}
          </strong>
        </span>
        <span>
          Board-end candidates <strong>{endCandidateCount}</strong>
        </span>
        <span>
          Long-edge candidates <strong>{longitudinalCandidateCount}</strong>
        </span>
        <span>
          Surface boundaries <strong>{surfaceBoundaryCandidateCount}</strong>
        </span>
        <span>
          Board color region{' '}
          <strong>
            {boardColorRegionFound ? `${boardColorRegionConfidence}%` : 'not found'}
          </strong>
        </span>
        <span>
          Closest corner gap{' '}
          <strong>
            {nearestJoinDistance === null
              ? '-'
              : `${nearestJoinDistance.toFixed(1)} / ${allowedJoinDistance.toFixed(1)} px`}
          </strong>
        </span>
        <span>
          Selected junction{' '}
          <strong>
            {cornerAssociation === 'board_region_projected_junction'
              ? 'silhouette-projected'
              : cornerAssociation === 'direct_segment_junction'
                ? 'direct'
                : '-'}
          </strong>
        </span>
        <span>
          Confirming face boundaries <strong>{surfaceConsensusCount || '-'}</strong>
        </span>
      </div>
      {!found ? (
        <p className="runtime-board-end-preview__diagnosis">{diagnosis}</p>
      ) : null}
      <p className="subtle-text runtime-board-end-preview__hint">
        Purple: board area supported by color, texture, brightness, and shape / Cyan: sustained surface changes inside
        that board area / Grey: generic line candidates / Orange: selected board end and long edge
        / {candidateCount} visible candidates. Move a slider and the current frame is evaluated
        automatically.
      </p>
    </div>
  );
}

function boardEndPreviewDiagnosis(
  reason: string,
  source: Record<string, unknown>,
  endCandidateCount: number,
  longitudinalCandidateCount: number
) {
  if (reason === 'required_reference_lines_missing') {
    if (!endCandidateCount) {
      return 'No sufficiently long board-end edge is supported by the current image.';
    }
    if (!longitudinalCandidateCount) {
      return 'No sufficiently long longitudinal board edge is supported by the current image.';
    }
  }
  if (reason === 'reference_line_pair_missing') {
    const rejections =
      source.pair_rejections && typeof source.pair_rejections === 'object'
        ? (source.pair_rejections as Record<string, unknown>)
        : {};
    if (finiteNumber(rejections.corner_gap, 0) > 0) {
      return 'The fitted end and long edges do not meet within Maximum corner gap.';
    }
    if (finiteNumber(rejections.directional_span, 0) > 0) {
      return 'The long edge does not continue far enough from the detected board end.';
    }
    if (finiteNumber(rejections.angle, 0) > 0) {
      return 'The angle between the fitted end and long edges is outside the allowed geometry.';
    }
    return 'End and long-edge candidates exist, but none forms a valid board corner.';
  }
  return reason;
}

function BoardEndPreviewPrimitive(props: {
  primitive: RuntimeOverlayPrimitiveState;
}) {
  const start = overlayPoint(props.primitive.geometry.start);
  const end = overlayPoint(props.primitive.geometry.end);
  if (!start || !end) {
    return null;
  }
  const candidate =
    props.primitive.layer === 'board_color_region' ||
    props.primitive.layer === 'surface_boundary_candidates' ||
    props.primitive.layer === 'board_end_candidates' ||
    props.primitive.layer === 'board_axis_candidates';
  const stroke =
    typeof props.primitive.style.stroke === 'string'
      ? props.primitive.style.stroke
      : candidate
        ? '#8b98a1'
        : '#ffab40';
  const strokeWidth = finiteNumber(
    props.primitive.style.stroke_width,
    candidate ? 1 : 3
  );
  return (
    <g>
      <line
        x1={start.x}
        y1={start.y}
        x2={end.x}
        y2={end.y}
        stroke={stroke}
        strokeWidth={strokeWidth}
        strokeOpacity={finiteNumber(props.primitive.style.stroke_opacity, 1)}
        vectorEffect="non-scaling-stroke"
      />
      {!candidate && props.primitive.label ? (
        <text
          className="runtime-board-end-preview__label"
          x={(start.x + end.x) / 2}
          y={Math.max((start.y + end.y) / 2 - 5, 12)}
        >
          {props.primitive.label}
        </text>
      ) : null}
    </g>
  );
}

function overlayPoint(value: unknown): { x: number; y: number } | null {
  if (!value || typeof value !== 'object') {
    return null;
  }
  const candidate = value as Record<string, unknown>;
  const x = Number(candidate.x);
  const y = Number(candidate.y);
  return Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null;
}

function FieldLabel(props: { label: string; helpText?: string | null; className?: string }) {
  return (
    <span className={['runtime-field-label', props.className].filter(Boolean).join(' ')}>
      <span>{props.label}</span>
      {props.helpText ? <HelpTooltip text={props.helpText} /> : null}
    </span>
  );
}

function HelpTooltip(props: { text: string }) {
  return (
    <span className="runtime-help-tooltip" tabIndex={0} title={props.text}>
      <AppIcon name="help" />
      <span className="runtime-help-tooltip__bubble" role="tooltip">
        {props.text}
      </span>
    </span>
  );
}

function fieldHelpText(field: RuntimeNodePropertySpec) {
  return field.help_text || `Controls the runtime value stored as config.${field.key}.`;
}

export function visiblePropertyFields(fields: RuntimeNodePropertySpec[], node: RuntimeNodeSpec) {
  if (node.node_type === 'preprocessing.process_coordinate_rectify') {
    // The structured image-view editor owns the profile ID. Keeping a second editable
    // text field allowed config.view_profile_id and view_profile.view_profile_id to drift.
    return fields.filter((field) => field.key !== 'view_profile_id');
  }
  if (node.node_type === 'analysis.board_end_coordinate_frame') {
    if (String(node.config.geometry_source ?? 'image_edges') === 'segmentation') {
      const segmentationKeys = new Set([
        'geometry_source',
        'view_role',
        'board_surface_class_mapping',
        'board_end_side',
        'debug_overlay'
      ]);
      return fields.filter((field) => segmentationKeys.has(field.key));
    }
    const lineDetectorMode = String(node.config.line_detector_mode ?? 'robust_lsd');
    if (lineDetectorMode === 'robust_lsd') {
      const legacyHoughKeys = new Set([
        'lower_threshold',
        'upper_threshold',
        'hough_threshold',
        'minimum_line_length_px',
        'minimum_edge_support_percent'
      ]);
      return fields.filter((field) => !legacyHoughKeys.has(field.key));
    }
    return fields;
  }
  if (
    node.node_type === 'analysis.active_stem_tracker' &&
    String(node.config.handover_reference_mode ?? 'transfer_roi_center') !== 'manual_point'
  ) {
    return fields.filter(
      (field) => !['expected_handover_x', 'expected_handover_y'].includes(field.key)
    );
  }
  if (node.node_type !== 'output.result') {
    return fields;
  }
  const hiddenLegacyKeys = new Set(['result_fields', 'include_decision', 'wrap_key']);
  if (node.config.edge_response_mode !== 'result_with_metadata') {
    hiddenLegacyKeys.add('edge_response_metadata_fields');
  }
  return fields.filter((field) => !hiddenLegacyKeys.has(field.key));
}

function groupPropertyFields(fields: RuntimeNodePropertySpec[]) {
  const groups = new Map<string, RuntimeNodePropertySpec[]>();
  for (const field of fields) {
    const section = field.section?.trim() || 'Configuration';
    groups.set(section, [...(groups.get(section) ?? []), field]);
  }
  return Array.from(groups.entries()).map(([section, groupFields]) => ({
    section,
    fields: groupFields
  }));
}

const guidedStructuredPropertyFieldTypes = new Set([
  'camera_intrinsic_calibration',
  'image_view_profile',
  'stem_curvature_process_calibration',
  'stem_surface_phase_calibration',
  'stem_minimum_principle_validation'
]);

function guidedStructuredPropertyField(field: RuntimeNodePropertySpec) {
  return guidedStructuredPropertyFieldTypes.has(field.field_type);
}

function guidedStructuredPropertyInitiallyOpen(
  node: RuntimeNodeSpec,
  field: RuntimeNodePropertySpec
) {
  if (field.field_type === 'stem_minimum_principle_validation') return false;
  return true;
}

function compactStemPropertySections(node: RuntimeNodeSpec) {
  return (
    node.node_type.startsWith('analysis.stem_') ||
    node.node_type === 'preprocessing.camera_undistort' ||
    node.node_type === 'preprocessing.process_coordinate_rectify'
  );
}

function NodeField(props: {
  field: RuntimeNodePropertySpec;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  nodeTypes: Map<string, RuntimeNodeTypeSummary>;
  outputCatalog: RuntimePipelineOutputCatalog | null;
  liveState: RuntimeLiveViewState | null;
  calibrationChannelId: string | null;
  capturedCalibrationFrame: RuntimeLiveFrame | null;
  calibrationCaptureAvailable: boolean;
  calibrationCapturePending: boolean;
  onCaptureCalibrationFrame: () => void;
  channels: RuntimeChannelSummary[];
  modelCapabilities: RuntimeModelCapabilityOption[];
  labelProjects: LabelProjectSummary[];
  authoringContext: RuntimeChannelAuthoringContext | null;
  onChange: (value: unknown) => void;
}) {
  const value = props.node.config[props.field.key] ?? props.field.default_value ?? '';
  const canonicalEditorViewProfile =
    props.node.node_type === 'analysis.stem_measurement_eligibility' ||
    props.node.node_type === 'analysis.active_stem_tracker'
      ? upstreamImageViewProfile(props.graph, props.node.node_key)
      : null;
  const canonicalEditorFrame = imageViewFrameForProfile(
    props.liveState?.frame ?? null,
    canonicalEditorViewProfile
  );
  const spatialEditorLiveState =
    canonicalEditorViewProfile
      ? canonicalEditorFrame && props.liveState
        ? { ...props.liveState, frame: canonicalEditorFrame }
        : null
      : props.liveState;
  const authoringLabels = authoringLabelOptions(props.graph, props.authoringContext);
  const labelDatalistId = `runtime-label-options-${props.node.node_key}-${props.field.key}`;
  const labelDatalist = authoringLabels.length ? (
    <datalist id={labelDatalistId}>
      {authoringLabels.map((label) => (
        <option key={label} value={label} />
      ))}
    </datalist>
  ) : null;
  if (props.field.field_type === 'model_ref') {
    const taskType = requiredTaskTypeForNode(props.node.node_type);
    const compatibleTaskTypes = compatibleTaskTypesForModelNode(taskType);
    const modelRef = props.node.logical_model_ref ?? defaultModelRef(taskType);
    const compatibleOptions = props.modelCapabilities.filter(
      (item) =>
        compatibleTaskTypes.includes(item.task_type) &&
        item.target_path === modelRef.target_path &&
        item.active
    );
    const options = compatibleOptions.filter((item) =>
      modelCapabilityMatchesSelectionPolicy(item, modelRef.selection_policy)
    );
    const selectedReleaseId = resolveSelectedModelReleaseId(modelRef, options);
    return (
      <div className="runtime-model-ref">
        <label>
          <FieldLabel label="Binding mode" helpText="Selects how the runtime resolves the model release before each execution." />
          <select
            value={modelRef.selection_policy}
            onChange={(event) => {
              const nextPolicy = event.target.value as RuntimeLogicalModelRef['selection_policy'];
              const nextOptions = compatibleOptions.filter((item) =>
                modelCapabilityMatchesSelectionPolicy(item, nextPolicy)
              );
              const projectId = modelRef.label_project_id ?? compatibleOptions.find(
                (item) => item.deployment_release_id === selectedReleaseId
              )?.capability.label_project_id;
              const selected =
                nextOptions.find((item) => item.deployment_release_id === selectedReleaseId) ??
                nextOptions.find(
                  (item) =>
                    item.model_family_slug === modelRef.model_family_slug &&
                    item.contract_major === modelRef.contract_major
                ) ??
                nextOptions.find(
                  (item) => projectId && item.capability.label_project_id === projectId &&
                    item.contract_major === modelRef.contract_major
                ) ??
                nextOptions[0];
              if (!selected) {
                props.onChange({
                  ...modelRef,
                  selection_policy: nextPolicy,
                  deployment_release_id:
                    nextPolicy === 'explicit_release' ? modelRef.deployment_release_id : null
                });
                return;
              }
              props.onChange(buildLogicalModelRefFromCapability(selected, taskType, nextPolicy));
            }}
          >
            <option value="active_production">Follow active production</option>
            <option value="staged_test">Use staged test capability</option>
            <option value="explicit_release">Pin deployment release</option>
          </select>
        </label>
        <label>
          <FieldLabel
            label="Deployed model"
            helpText={
              modelRef.selection_policy === 'active_production'
                ? 'Shows the active production model of each project. Follows compatible production updates within the selected project.'
                : 'Limits the selectable releases to the model task type required by this node.'
            }
          />
          <select
            value={selectedReleaseId}
            onChange={(event) => {
              const selected = options.find(
                (item) => item.deployment_release_id === event.target.value
              );
              if (!selected) {
                props.onChange(defaultModelRef(taskType));
                return;
              }
              props.onChange(
                buildLogicalModelRefFromCapability(selected, taskType, modelRef.selection_policy)
              );
            }}
          >
            <option value="">Select deployed model</option>
            {options.map((option) => (
              <option key={option.deployment_release_id} value={option.deployment_release_id}>
                {formatModelCapabilityOption(option)}
              </option>
            ))}
          </select>
        </label>
        <small className="subtle-text">
          {selectedReleaseId
            ? describeModelBinding(
                options.find((item) => item.deployment_release_id === selectedReleaseId) ?? null
              )
            : `Task: ${compatibleTaskTypes.join(' / ')}. Choose a deployed capability before publishing.`}
        </small>
      </div>
    );
  }

  if (props.field.field_type === 'boolean') {
    return (
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(event) => props.onChange(event.target.checked)}
        />
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      </label>
    );
  }

  if (props.field.field_type === 'channel_ref') {
    const currentValue = value == null ? '' : String(value);
    return (
      <label>
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <select value={currentValue} onChange={(event) => props.onChange(event.target.value)}>
          <option value="">Choose a channel</option>
          {props.channels.map((channel) => (
            <option key={channel.channel_id} value={channel.channel_id}>
              {channel.name} / {channel.technical_place}
            </option>
          ))}
        </select>
      </label>
    );
  }

  if (props.field.field_type === 'image_upload') {
    const photo = normalizeUploadedPhoto(value);
    return (
      <div className="runtime-image-upload">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {photo ? (
          <img
            src={photo.data_url}
            alt={`Selected test input ${photo.name}`}
            style={{ display: 'block', maxWidth: '100%', maxHeight: 240, objectFit: 'contain' }}
          />
        ) : null}
        <input
          type="file"
          accept="image/jpeg,image/png,image/webp,image/bmp"
          aria-label={props.field.label}
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (!file) return;
            if (file.size > MAX_TEST_PHOTO_BYTES) {
              event.target.value = '';
              window.alert('The test photo must not exceed 5 MB.');
              return;
            }
            const reader = new FileReader();
            reader.onload = () => {
              if (typeof reader.result !== 'string') return;
              props.onChange({
                data_url: reader.result,
                name: file.name,
                mime_type: file.type,
                size: file.size
              });
            };
            reader.readAsDataURL(file);
          }}
        />
        {photo ? (
          <div className="actions-row">
            <small className="subtle-text">{photo.name} ({formatFileSize(photo.size)})</small>
            <button type="button" onClick={() => props.onChange(null)}>Remove photo</button>
          </div>
        ) : (
          <small className="subtle-text">No test photo selected.</small>
        )}
      </div>
    );
  }

  if (props.field.field_type === 'image_upload_list') {
    const photos = normalizeUploadedPhotos(value);
    return (
      <div className="runtime-image-upload">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <input
          type="file"
          accept="image/jpeg,image/png,image/webp,image/bmp"
          multiple
          ref={(element) => {
            element?.setAttribute('webkitdirectory', '');
            element?.setAttribute('directory', '');
          }}
          aria-label={props.field.label}
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []).sort((left, right) =>
              left.name.localeCompare(right.name, undefined, { numeric: true, sensitivity: 'base' })
            );
            event.target.value = '';
            if (!files.length) return;
            if (files.length > 100) {
              window.alert('A test photo sequence can contain up to 100 photos.');
              return;
            }
            if (files.some((file) => file.size > MAX_TEST_PHOTO_BYTES)) {
              window.alert('Each test photo must not exceed 5 MB.');
              return;
            }
            if (files.reduce((total, file) => total + file.size, 0) > MAX_TEST_PHOTO_SEQUENCE_TOTAL_BYTES) {
              window.alert('The test photo sequence must not exceed 100 MB in total (up to 100 photos at 1 MB each).');
              return;
            }
            void Promise.all(files.map(readUploadedPhoto))
              .then((uploadedPhotos) => props.onChange(uploadedPhotos))
              .catch((error: unknown) => window.alert(error instanceof Error ? error.message : 'Could not read test photos.'));
          }}
        />
        {photos.length ? (
          <div className="runtime-image-upload__sequence">
            <small className="subtle-text">{photos.length} photos selected (alphabetical order); they repeat in a loop.</small>
            <ol>
              {photos.map((photo, index) => (
                <li key={`${photo.name}-${index}`}>
                  <span>{index + 1}. {photo.name} ({formatFileSize(photo.size)})</span>
                  <button type="button" onClick={() => props.onChange(photos.filter((_, photoIndex) => photoIndex !== index))}>
                    Remove
                  </button>
                </li>
              ))}
            </ol>
            <button type="button" onClick={() => props.onChange([])}>Remove sequence</button>
          </div>
        ) : (
          <small className="subtle-text">Select individual photos or a folder with up to 100 photos.</small>
        )}
      </div>
    );
  }

  if (props.field.field_type === 'board_cut_class_rules') {
    return (
      <BoardCutClassRulesEditor
        field={props.field}
        value={value}
        graph={props.graph}
        node={props.node}
        modelCapabilities={props.modelCapabilities}
        onChange={props.onChange}
      />
    );
  }

  if (props.field.field_type === 'board_surface_class_mapping') {
    return (
      <BoardSurfaceClassMappingEditor
        field={props.field}
        value={value}
        graph={props.graph}
        node={props.node}
        modelCapabilities={props.modelCapabilities}
        onChange={props.onChange}
      />
    );
  }

  if (props.field.field_type === 'board_surface_calibration') {
    return (
      <BoardSurfaceCalibrationEditor
        field={props.field}
        value={value}
        node={props.node}
        liveState={props.liveState}
        captureAvailable={props.calibrationCaptureAvailable}
        capturePending={props.calibrationCapturePending}
        onCapture={props.onCaptureCalibrationFrame}
        onChange={props.onChange}
      />
    );
  }

  if (props.field.field_type === 'camera_intrinsic_calibration') {
    return (
      <CameraIntrinsicCalibrationEditor
        field={props.field}
        value={value}
        channelId={props.calibrationChannelId}
        liveState={props.liveState}
        capturedFrame={props.capturedCalibrationFrame}
        captureAvailable={props.calibrationCaptureAvailable}
        capturePending={props.calibrationCapturePending}
        onCapture={props.onCaptureCalibrationFrame}
        onChange={props.onChange}
      />
    );
  }
  if (props.field.field_type === 'image_view_profile') {
    return (
      <ImageViewProfileEditor
        field={props.field}
        value={value}
        graph={props.graph}
        node={props.node}
        channelId={props.calibrationChannelId}
        liveState={props.liveState}
        onChange={props.onChange}
      />
    );
  }
  if (props.field.field_type === 'stem_curvature_process_calibration') {
    return (
      <StemCurvatureProcessCalibrationEditor
        field={props.field}
        value={value}
        graph={props.graph}
        node={props.node}
        channelId={props.calibrationChannelId}
        liveState={props.liveState}
        onChange={props.onChange}
      />
    );
  }
  if (props.field.field_type === 'stem_surface_phase_calibration') {
    return (
      <StemSurfacePhaseCalibrationEditor
        field={props.field}
        value={value}
        graph={props.graph}
        node={props.node}
        channelId={props.calibrationChannelId}
        onChange={props.onChange}
      />
    );
  }
  if (props.field.field_type === 'stem_minimum_principle_validation') {
    return (
      <StemMinimumPrincipleValidationEditor
        field={props.field}
        value={value}
        graph={props.graph}
        node={props.node}
        channelId={props.calibrationChannelId}
        onChange={props.onChange}
      />
    );
  }

  if (props.field.field_type === 'result_projection_table') {
    return (
      <ResultProjectionEditor
        field={props.field}
        value={value}
        graph={props.graph}
        node={props.node}
        nodeTypes={props.nodeTypes}
        outputCatalog={props.outputCatalog}
        onChange={props.onChange}
      />
    );
  }

  if (props.field.field_type === 'plc_connection') {
    const connection = normalizeSiemensSpsConnection(value);
    return (
      <div className="runtime-structured-table runtime-plc-connection">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <div className="runtime-warning">
          PLC writes are dry-run by default and require runtime write permission before hardware writes occur.
        </div>
        <div className="runtime-structured-table__row runtime-structured-table__row--grid">
          <label>
            <span>Name</span>
            <input
              value={connection.name}
              onChange={(event) => props.onChange({ ...connection, name: event.target.value })}
              placeholder="Siemens SPS"
            />
          </label>
          <label>
            <span>IP / Hostname</span>
            <input
              value={connection.host}
              onChange={(event) => props.onChange({ ...connection, host: event.target.value })}
              placeholder="192.168.0.10"
            />
          </label>
          <label>
            <span>Port</span>
            <input
              type="number"
              min={1}
              max={65535}
              value={connection.port}
              onChange={(event) => props.onChange({ ...connection, port: Number(event.target.value) })}
            />
          </label>
          <label>
            <span>Rack</span>
            <input
              type="number"
              min={0}
              max={7}
              value={connection.rack}
              onChange={(event) => props.onChange({ ...connection, rack: Number(event.target.value) })}
            />
          </label>
          <label>
            <span>Slot</span>
            <input
              type="number"
              min={0}
              max={31}
              value={connection.slot}
              onChange={(event) => props.onChange({ ...connection, slot: Number(event.target.value) })}
            />
          </label>
          <label>
            <span>Connect timeout ms</span>
            <input
              type="number"
              min={50}
              max={30000}
              value={connection.connect_timeout_ms}
              onChange={(event) =>
                props.onChange({ ...connection, connect_timeout_ms: Number(event.target.value) })
              }
            />
          </label>
          <label>
            <span>Write timeout ms</span>
            <input
              type="number"
              min={50}
              max={30000}
              value={connection.write_timeout_ms}
              onChange={(event) => props.onChange({ ...connection, write_timeout_ms: Number(event.target.value) })}
            />
          </label>
          <label>
            <span>Reconnect</span>
            <select
              value={connection.reconnect}
              onChange={(event) =>
                props.onChange({ ...connection, reconnect: event.target.value as SiemensSpsConnectionConfig['reconnect'] })
              }
            >
              <option value="on_failure">On failure</option>
              <option value="disabled">Disabled</option>
            </select>
          </label>
          <label>
            <span>Connection reuse</span>
            <select
              value={connection.reuse_policy}
              onChange={(event) =>
                props.onChange({
                  ...connection,
                  reuse_policy: event.target.value as SiemensSpsConnectionConfig['reuse_policy']
                })
              }
            >
              <option value="per_execution">Per execution</option>
              <option value="per_runtime">Per runtime</option>
            </select>
          </label>
        </div>
      </div>
    );
  }

  if (props.field.field_type === 'plc_write_mapping_table') {
    const mappings = normalizeSiemensSpsMappings(value);
    const fieldOptions = resultFieldOptionsForNode(
      props.graph,
      props.node,
      props.nodeTypes,
      props.authoringContext,
      props.outputCatalog
    );
    const datalistId = `runtime-plc-fields-${props.node.node_key}-${props.field.key}`;
    const optionValues = new Set(fieldOptions.map((option) => option.value));
    return (
      <div className="runtime-structured-table runtime-plc-mappings">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <small className="subtle-text">
          Preview and validation never write to a PLC. Unknown enum values only write a default when configured.
        </small>
        <datalist id={datalistId}>
          {fieldOptions.map((option) => (
            <option key={option.value} value={option.value} label={option.label} />
          ))}
        </datalist>
        {mappings.map((mapping, index) => (
          <SiemensSpsMappingEditor
            key={`${mapping.key || mapping.label || 'mapping'}-${index}`}
            mapping={mapping}
            index={index}
            datalistId={datalistId}
            fieldOptionsAvailable={fieldOptions.length > 0}
            sourceKnown={!mapping.source.field || optionValues.has(mapping.source.field) || mapping.source.allow_advanced_path}
            onChange={(next) =>
              props.onChange(mappings.map((candidate, candidateIndex) => (candidateIndex === index ? next : candidate)))
            }
            onRemove={() => props.onChange(mappings.filter((_, candidateIndex) => candidateIndex !== index))}
          />
        ))}
        <div className="actions-row">
          {optionValues.has('valid') && optionValues.has('longitudinal_shift_mm') ? (
            <button
              type="button"
              onClick={() => props.onChange(boardCutSiemensSpsMappings())}
            >
              Use safe board-cut mapping
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => props.onChange([...mappings, blankSiemensSpsMapping(firstSpsSourceField(fieldOptions))])}
          >
            Add mapping
          </button>
          <button
            type="button"
            onClick={() =>
              props.onChange([
                ...mappings,
                {
                  ...blankSiemensSpsMapping('classification.top1.label'),
                  key: 'class_to_int',
                  label: 'Class gut/schlecht',
                  target: { area: 'DB', db_number: 101, byte_offset: 0, bit_offset: null, data_type: 'INT' },
                  conversion: {
                    ...blankSiemensSpsMapping('').conversion,
                    mode: 'enum',
                    enum_values: [
                      { source: 'gut', value: 1 },
                      { source: 'schlecht', value: 0 }
                    ]
                  }
                },
                {
                  ...blankSiemensSpsMapping('classification.top1.score'),
                  key: 'confidence_percent',
                  label: 'Confidence percent',
                  target: { area: 'DB', db_number: 101, byte_offset: 10, bit_offset: null, data_type: 'INT' },
                  conversion: {
                    ...blankSiemensSpsMapping('').conversion,
                    mode: 'scale',
                    multiplier: 100,
                    rounding: 'nearest',
                    clamp_min: 0,
                    clamp_max: 100
                  }
                }
              ])
            }
          >
            Add classification examples
          </button>
        </div>
      </div>
    );
  }

  if (props.field.field_type === 'response_mapping') {
    const mappings = normalizeResponseMappings(value);
    const fieldOptions = resultFieldOptionsForNode(
      props.graph,
      props.node,
      props.nodeTypes,
      props.authoringContext,
      props.outputCatalog
    );
    const optionValues = new Set(fieldOptions.map((option) => option.value));
    const datalistId = `runtime-result-fields-${props.node.node_key}-${props.field.key}`;
    const defaultSource = firstUnusedResultField(fieldOptions, mappings);
    return (
      <div className="runtime-box-list runtime-result-mapping">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <small className="subtle-text">
          {props.field.help_text ||
            'Choose a source field from the connected upstream node and set the output name shown in channel results.'}
        </small>
        <datalist id={datalistId}>
          {fieldOptions.map((option) => (
            <option key={option.value} value={option.value} label={option.label} />
          ))}
        </datalist>
        {mappings.map((mapping, index) => (
          <div key={`${mapping.source_field}-${index}`} className="runtime-result-mapping__row">
            <label>
              <span>Source field</span>
              <input
                list={datalistId}
                value={mapping.source_field}
                onChange={(event) => {
                  const sourceField = event.target.value;
                  props.onChange(
                    mappings.map((candidate, candidateIndex) =>
                      candidateIndex === index
                        ? {
                            ...candidate,
                            source_field: sourceField,
                            response_key:
                              candidate.response_key || suggestedResponseKey(sourceField)
                          }
                        : candidate
                    )
                  );
                }}
                placeholder={fieldOptions.length ? 'Choose a field' : 'Connect upstream result first'}
              />
              {mapping.source_field && !optionValues.has(mapping.source_field) ? (
                <small className="subtle-text">Custom field path</small>
              ) : null}
            </label>
            <label>
              <span>Output name</span>
              <input
                value={mapping.response_key}
                onChange={(event) =>
                  props.onChange(
                    mappings.map((candidate, candidateIndex) =>
                      candidateIndex === index
                        ? { ...candidate, response_key: event.target.value }
                        : candidate
                    )
                  )
                }
                placeholder={suggestedResponseKey(mapping.source_field) || 'display_name'}
              />
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={mapping.required}
                onChange={(event) =>
                  props.onChange(
                    mappings.map((candidate, candidateIndex) =>
                      candidateIndex === index
                        ? { ...candidate, required: event.target.checked }
                        : candidate
                    )
                  )
                }
              />
              <span>Required</span>
            </label>
            <button
              type="button"
              onClick={() => props.onChange(mappings.filter((_, candidateIndex) => candidateIndex !== index))}
            >
              Remove
            </button>
          </div>
        ))}
        <button
          type="button"
          onClick={() =>
            props.onChange([
              ...mappings,
              {
                source_field: defaultSource,
                response_key: suggestedResponseKey(defaultSource),
                required: false
              }
            ])
          }
        >
          Add field
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'edge_role_mapping') {
    const mappings = normalizeEdgeRoleMappings(value);
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <small className="subtle-text">
          Map Feature Detection classes such as board_start_edge and board_side_edge into board edge roles.
        </small>
        {labelDatalist}
        {mappings.map((mapping, index) => (
          <div key={`${mapping.feature_class}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--edge">
            <label>
              <FieldLabel label="Detected class" helpText="Class name emitted by the upstream feature detector that should be interpreted as a board edge." />
              <input
                list={authoringLabels.length ? labelDatalistId : undefined}
                value={mapping.feature_class}
                onChange={(event) =>
                  props.onChange(
                    mappings.map((candidate, candidateIndex) =>
                      candidateIndex === index ? { ...candidate, feature_class: event.target.value } : candidate
                    )
                  )
                }
                placeholder="Detected class"
              />
            </label>
            <label>
              <FieldLabel label="Edge role" helpText="Semantic role used by downstream coordinate-frame and measurement nodes." />
              <select
                value={mapping.edge_role}
                onChange={(event) =>
                  props.onChange(
                    mappings.map((candidate, candidateIndex) =>
                      candidateIndex === index ? { ...candidate, edge_role: event.target.value } : candidate
                    )
                  )
                }
              >
                {edgeRoleOptions.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <FieldLabel label="Box side" helpText="Bounding-box side used as the geometric edge when converting detections into reference edges." />
              <select
                value={mapping.bbox_side}
                onChange={(event) =>
                  props.onChange(
                    mappings.map((candidate, candidateIndex) =>
                      candidateIndex === index ? { ...candidate, bbox_side: event.target.value } : candidate
                    )
                  )
                }
              >
                {bboxSideOptions.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" onClick={() => props.onChange(mappings.filter((_, candidateIndex) => candidateIndex !== index))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={() => props.onChange([...mappings, blankEdgeRoleMapping()])}>
          Add mapping
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'measurement_rule_table') {
    const rules = normalizeMeasurementRules(value);
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <small className="subtle-text">
          Choose which geometry measurements are produced for each Feature Detection class.
        </small>
        {labelDatalist}
        {rules.map((rule, index) => (
          <div key={`${rule.feature_type}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--measurement">
            <label>
              <FieldLabel label="Feature type" helpText="Feature class to measure. Use * to apply the selected bases to every feature." />
              <input
                list={authoringLabels.length ? labelDatalistId : undefined}
                value={rule.feature_type}
                onChange={(event) =>
                  props.onChange(
                    rules.map((candidate, candidateIndex) =>
                      candidateIndex === index ? { ...candidate, feature_type: event.target.value } : candidate
                    )
                  )
                }
                placeholder="Feature type or *"
              />
            </label>
            <div className="runtime-check-grid">
              {measurementBasisOptions.map((basis) => (
                <label key={basis} className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={rule.bases.includes(basis)}
                    onChange={(event) =>
                      props.onChange(
                        rules.map((candidate, candidateIndex) =>
                          candidateIndex === index
                            ? {
                                ...candidate,
                                bases: event.target.checked
                                  ? Array.from(new Set([...candidate.bases, basis]))
                                  : candidate.bases.filter((item) => item !== basis)
                              }
                            : candidate
                        )
                      )
                    }
                  />
                  <span>{basis}</span>
                </label>
              ))}
            </div>
            <button type="button" onClick={() => props.onChange(rules.filter((_, candidateIndex) => candidateIndex !== index))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={() => props.onChange([...rules, blankMeasurementRule()])}>
          Add rule
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'clearance_rule_table') {
    const rules = normalizeClearanceRules(value);
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <small className="subtle-text">
          Generate blocked intervals along the board axis from measured features.
        </small>
        {labelDatalist}
        {rules.map((rule, index) => (
          <div key={`${rule.feature_type}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--clearance">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.enabled}
                onChange={(event) =>
                  props.onChange(
                    rules.map((candidate, candidateIndex) =>
                      candidateIndex === index ? { ...candidate, enabled: event.target.checked } : candidate
                    )
                  )
                }
              />
              <span>On</span>
            </label>
            <label>
              <FieldLabel label="Feature type" helpText="Measured feature class that creates blocked zones before or after the object." />
              <input
                list={authoringLabels.length ? labelDatalistId : undefined}
                value={rule.feature_type}
                onChange={(event) =>
                  props.onChange(
                    rules.map((candidate, candidateIndex) =>
                      candidateIndex === index ? { ...candidate, feature_type: event.target.value } : candidate
                    )
                  )
                }
                placeholder="Feature type"
              />
            </label>
            <label>
              <FieldLabel label="Measurement basis" helpText="Measurement value used as the base width/length for clearance calculations." />
              <select
                value={rule.measurement_basis}
                onChange={(event) =>
                  props.onChange(
                    rules.map((candidate, candidateIndex) =>
                      candidateIndex === index ? { ...candidate, measurement_basis: event.target.value } : candidate
                    )
                  )
                }
              >
                {measurementBasisOptions.map((basis) => (
                  <option key={basis} value={basis}>
                    {basis}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <FieldLabel label="Multiplier" helpText="Scales the selected measurement before generating clearance pixels." />
              <input
                type="number"
                step={0.1}
                min={0}
                value={rule.multiplier}
                onChange={(event) => props.onChange(updateClearanceRule(rules, index, 'multiplier', Number(event.target.value)))}
              />
            </label>
            <label>
              <FieldLabel label="Minimum px" helpText="Lower bound in pixels applied even when the measured feature is smaller." />
              <input
                type="number"
                step={1}
                min={0}
                value={rule.minimum_clearance_px}
                onChange={(event) =>
                  props.onChange(updateClearanceRule(rules, index, 'minimum_clearance_px', Number(event.target.value)))
                }
              />
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.apply_before}
                onChange={(event) => props.onChange(updateClearanceRule(rules, index, 'apply_before', event.target.checked))}
              />
              <span>Before</span>
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.apply_after}
                onChange={(event) => props.onChange(updateClearanceRule(rules, index, 'apply_after', event.target.checked))}
              />
              <span>After</span>
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.merge_overlapping_zones}
                onChange={(event) =>
                  props.onChange(updateClearanceRule(rules, index, 'merge_overlapping_zones', event.target.checked))
                }
              />
              <span>Merge</span>
            </label>
            <button type="button" onClick={() => props.onChange(rules.filter((_, candidateIndex) => candidateIndex !== index))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={() => props.onChange([...rules, blankClearanceRule()])}>
          Add clearance rule
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'cv_line_selection_rules') {
    const rules = normalizeCvLineSelectionRules(value);
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <small className="subtle-text">
          Pick reference edges from Canny lines without typing internal edge IDs.
        </small>
        {rules.map((rule, index) => (
          <div key={`${rule.edge_role}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--cv-line">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.required}
                onChange={(event) => props.onChange(updateCvLineSelectionRule(rules, index, 'required', event.target.checked))}
              />
              <span>Required</span>
            </label>
            <select
              value={rule.edge_role}
              onChange={(event) => props.onChange(updateCvLineSelectionRule(rules, index, 'edge_role', event.target.value))}
            >
              {edgeRoleOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              type="number"
              step={1}
              min={0}
              max={179}
              value={rule.target_angle_degrees}
              onChange={(event) =>
                props.onChange(updateCvLineSelectionRule(rules, index, 'target_angle_degrees', Number(event.target.value)))
              }
            />
            <input
              type="number"
              step={1}
              min={0}
              max={90}
              value={rule.angle_tolerance_degrees}
              onChange={(event) =>
                props.onChange(updateCvLineSelectionRule(rules, index, 'angle_tolerance_degrees', Number(event.target.value)))
              }
            />
            <input
              type="number"
              step={1}
              min={0}
              value={rule.minimum_line_length_px}
              onChange={(event) =>
                props.onChange(updateCvLineSelectionRule(rules, index, 'minimum_line_length_px', Number(event.target.value)))
              }
            />
            <select
              value={rule.region_preference}
              onChange={(event) => props.onChange(updateCvLineSelectionRule(rules, index, 'region_preference', event.target.value))}
            >
              {cvRegionOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <select
              value={rule.nearest_to_image_side}
              onChange={(event) =>
                props.onChange(updateCvLineSelectionRule(rules, index, 'nearest_to_image_side', event.target.value))
              }
            >
              {cvSideOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <select
              value={rule.ranking}
              onChange={(event) => props.onChange(updateCvLineSelectionRule(rules, index, 'ranking', event.target.value))}
            >
              {cvRankingOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <button type="button" onClick={() => props.onChange(rules.filter((_, candidateIndex) => candidateIndex !== index))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={() => props.onChange([...rules, blankCvLineSelectionRule()])}>
          Add role
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'metric_apply_to') {
    const selected = normalizeMetricApplyTo(value);
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <div className="runtime-check-grid runtime-check-grid--compact">
          {metricApplyOptions.map((option) => (
            <label key={option} className="checkbox-row">
              <input
                type="checkbox"
                checked={selected.includes(option)}
                onChange={(event) =>
                  props.onChange(
                    event.target.checked
                      ? Array.from(new Set([...selected, option]))
                      : selected.filter((item) => item !== option)
                  )
                }
              />
              <span>{option}</span>
            </label>
          ))}
        </div>
      </div>
    );
  }

  if (props.field.field_type === 'quality_rule_table') {
    const rules = normalizeQualityRules(value);
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <small className="subtle-text">
          Evaluate simple board quality checks over measured features and free-region distance.
        </small>
        {labelDatalist}
        {rules.map((rule, index) => (
          <div key={`${rule.rule_type}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--quality">
            <select
              value={rule.rule_type}
              onChange={(event) => props.onChange(updateQualityRule(rules, index, 'rule_type', event.target.value))}
            >
              {qualityRuleTypeOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              list={authoringLabels.length ? labelDatalistId : undefined}
              value={rule.feature_type}
              onChange={(event) => props.onChange(updateQualityRule(rules, index, 'feature_type', event.target.value))}
              placeholder="Feature type"
            />
            <select
              value={rule.measurement_basis}
              onChange={(event) => props.onChange(updateQualityRule(rules, index, 'measurement_basis', event.target.value))}
            >
              {measurementBasisOptions.map((basis) => (
                <option key={basis} value={basis}>
                  {basis}
                </option>
              ))}
            </select>
            <select
              value={rule.operator}
              onChange={(event) => props.onChange(updateQualityRule(rules, index, 'operator', event.target.value))}
            >
              {qualityOperatorOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              type="number"
              step={0.1}
              value={rule.threshold}
              onChange={(event) => props.onChange(updateQualityRule(rules, index, 'threshold', Number(event.target.value)))}
            />
            <select
              value={rule.status_on_fail}
              onChange={(event) => props.onChange(updateQualityRule(rules, index, 'status_on_fail', event.target.value))}
            >
              {qualityStatusOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <button type="button" onClick={() => props.onChange(rules.filter((_, candidateIndex) => candidateIndex !== index))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={() => props.onChange([...rules, blankQualityRule()])}>
          Add quality rule
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'color_profile_table') {
    const profiles = normalizeColorProfiles(value);
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {labelDatalist}
        {profiles.map((profile, index) => (
          <div key={`${profile.label}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--color-profile">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={profile.enabled}
                onChange={(event) => props.onChange(updateColorProfile(profiles, index, 'enabled', event.target.checked))}
              />
              <span>On</span>
            </label>
            <label className="runtime-color-picker-field">
              <span>Target color</span>
              <input
                type="color"
                value={profile.display_color}
                onChange={(event) => props.onChange(updateColorProfile(profiles, index, 'display_color', event.target.value))}
                title="Target color"
              />
            </label>
            <label>
              <span>Object label</span>
              <input
                list={authoringLabels.length ? labelDatalistId : undefined}
                value={profile.label}
                onChange={(event) => props.onChange(updateColorProfile(profiles, index, 'label', event.target.value))}
                placeholder="Object label"
              />
            </label>
            <label className="runtime-color-tolerance">
              <span>Color tolerance</span>
              <div className="runtime-color-tolerance__control">
                <input
                  type="range"
                  min={0}
                  max={100}
                  step={1}
                  value={profile.color_tolerance_percent}
                  onChange={(event) =>
                    props.onChange(updateColorProfile(profiles, index, 'color_tolerance_percent', Number(event.target.value)))
                  }
                />
                <output>{Math.round(profile.color_tolerance_percent)}%</output>
              </div>
            </label>
            <label>
              <span>Minimum area %</span>
              <input type="number" min={0} step={0.01} value={profile.min_area_percent} onChange={(event) => props.onChange(updateColorProfile(profiles, index, 'min_area_percent', Number(event.target.value)))} />
            </label>
            <label>
              <span>Maximum area %</span>
              <input type="number" min={0} step={0.01} value={profile.max_area_percent ?? ''} onChange={(event) => props.onChange(updateColorProfile(profiles, index, 'max_area_percent', nullableFiniteNumber(event.target.value, null)))} placeholder="No limit" />
            </label>
            <label>
              <span>Minimum aspect</span>
              <input type="number" min={1} step={0.1} value={profile.min_aspect_ratio} onChange={(event) => props.onChange(updateColorProfile(profiles, index, 'min_aspect_ratio', Number(event.target.value)))} />
            </label>
            <button type="button" onClick={() => props.onChange(profiles.filter((_, candidateIndex) => candidateIndex !== index))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={() => props.onChange([...profiles, blankColorProfile()])}>
          Add color profile
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'spatial_rule_table') {
    const rules = normalizeSpatialRules(value);
    const zones = normalizeBoxList(props.node.config.zones);
    const zoneOptions = zones.map((zone, index) => zone.label || `zone_${index + 1}`);
    if (props.node.node_type === 'analysis.zone_detection') {
      return (
        <ZoneSpatialRuleTable
          label={props.field.label}
          helpText={props.field.help_text}
          rules={rules}
          zoneOptions={zoneOptions}
          labelOptions={authoringLabels}
          onChange={props.onChange}
        />
      );
    }
    if (props.node.node_type === 'analysis.close_detection') {
      return (
        <CloseSpatialRuleTable
          label={props.field.label}
          helpText={props.field.help_text}
          rules={rules}
          labelOptions={authoringLabels}
          onChange={props.onChange}
        />
      );
    }
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {labelDatalist}
        {rules.map((rule, index) => (
          <div key={`${rule.rule_key || rule.relation}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--spatial">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.enabled}
                onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'enabled', event.target.checked))}
              />
              <span>On</span>
            </label>
            <select
              value={rule.relation}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'relation', event.target.value))}
            >
              {spatialRelationOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              value={rule.rule_key}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'rule_key', event.target.value))}
              placeholder="Rule key"
            />
            <input
              list={authoringLabels.length ? labelDatalistId : undefined}
              value={formatRuleLabels(rule.primary_labels)}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'primary_labels', normalizeRuleLabels(event.target.value)))}
              placeholder="Primary classes"
            />
            <input
              list={authoringLabels.length ? labelDatalistId : undefined}
              value={formatRuleLabels(rule.secondary_labels)}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'secondary_labels', normalizeRuleLabels(event.target.value)))}
              placeholder="Second classes"
              disabled={rule.relation === 'zone_occupancy'}
            />
            <select
              value={rule.label_pairing}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'label_pairing', event.target.value))}
            >
              {spatialLabelPairingOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={rule.max_distance_percent ?? ''}
              onChange={(event) =>
                props.onChange(updateSpatialRule(rules, index, 'max_distance_percent', nullableFiniteNumber(event.target.value, null)))
              }
              placeholder="Max distance %"
              disabled={rule.relation === 'overlap' || rule.relation === 'zone_occupancy'}
            />
            <input
              type="number"
              min={0}
              max={100}
              step={0.1}
              value={rule.relation === 'overlap' ? rule.min_overlap_percent : rule.min_closing_percent}
              onChange={(event) =>
                props.onChange(
                  updateSpatialRule(
                    rules,
                    index,
                    rule.relation === 'overlap' ? 'min_overlap_percent' : 'min_closing_percent',
                    Number(event.target.value)
                  )
                )
              }
              placeholder={rule.relation === 'overlap' ? 'Overlap %' : 'Closing %'}
              disabled={rule.relation === 'distance' || rule.relation === 'zone_occupancy'}
            />
            <select
              value={rule.zone_key}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'zone_key', event.target.value))}
              disabled={rule.relation !== 'zone_occupancy'}
            >
              <option value="">All fields</option>
              {zoneOptions.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
            <select
              value={rule.zone_match}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'zone_match', event.target.value))}
              disabled={rule.relation !== 'zone_occupancy'}
            >
              {spatialZoneMatchOptions.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
            <input
              type="number"
              min={1}
              step={1}
              value={rule.min_count}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'min_count', Number(event.target.value)))}
              disabled={rule.relation !== 'zone_occupancy'}
            />
            <input
              value={rule.signal_label}
              onChange={(event) => props.onChange(updateSpatialRule(rules, index, 'signal_label', event.target.value))}
              placeholder="Signal label"
            />
            <button type="button" onClick={() => props.onChange(rules.filter((_, candidateIndex) => candidateIndex !== index))}>
              Remove
            </button>
          </div>
        ))}
        <button type="button" onClick={() => props.onChange([...rules, blankSpatialRule()])}>
          Add spatial rule
        </button>
      </div>
    );
  }

  if (props.field.field_type === 'direction_reference') {
    const reference = normalizeDirectionReference(value);
    return (
      <div className="runtime-box-list">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {props.node.node_type === 'analysis.stem_measurement_eligibility' && !spatialEditorLiveState ? (
          <div className="inline-alert inline-alert--info">
            Finish Step 1 in the upstream canonical-view node. The crowd boundary then becomes
            editable on the exact model/Label Studio image without running segmentation first.
          </div>
        ) : null}
        <VisualLineEditor
          title={props.field.label}
          liveState={spatialEditorLiveState}
          line={reference}
          onChange={props.onChange}
        />
        <div className="runtime-box-list__row runtime-box-list__row--direction">
          <input
            value={reference.label}
            onChange={(event) => props.onChange({ ...reference, label: event.target.value })}
            placeholder="Direction label"
          />
          <input type="number" min={0} max={1} step={0.01} value={reference.start.x} onChange={(event) => props.onChange({ ...reference, start: { ...reference.start, x: Number(event.target.value) } })} />
          <input type="number" min={0} max={1} step={0.01} value={reference.start.y} onChange={(event) => props.onChange({ ...reference, start: { ...reference.start, y: Number(event.target.value) } })} />
          <input type="number" min={0} max={1} step={0.01} value={reference.end.x} onChange={(event) => props.onChange({ ...reference, end: { ...reference.end, x: Number(event.target.value) } })} />
          <input type="number" min={0} max={1} step={0.01} value={reference.end.y} onChange={(event) => props.onChange({ ...reference, end: { ...reference.end, y: Number(event.target.value) } })} />
        </div>
      </div>
    );
  }

  if (props.field.field_type === 'multi_select') {
    const selected = new Set(Array.isArray(value) ? value.map(String) : []);
    return (
      <fieldset>
        <legend>
          <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        </legend>
        {props.field.options.map((option) => (
          <label key={option.value} className="checkbox-row">
            <input
              type="checkbox"
              checked={selected.has(option.value)}
              onChange={(event) => {
                const next = new Set(selected);
                if (event.target.checked) next.add(option.value);
                else next.delete(option.value);
                props.onChange(props.field.options.filter((item) => next.has(item.value)).map((item) => item.value));
              }}
            />
            <span>{option.label}</span>
          </label>
        ))}
      </fieldset>
    );
  }

  if (props.field.field_type === 'select') {
    const currentValue = Array.isArray(value) ? String(value[0] ?? '') : String(value);
    return (
      <label>
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <select value={currentValue} onChange={(event) => props.onChange(event.target.value)}>
          {props.field.options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </label>
    );
  }

  if (props.field.field_type === 'expression') {
    return (
      <label>
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <textarea
          value={String(value)}
          onChange={(event) => props.onChange(event.target.value)}
        />
      </label>
    );
  }

  if (props.field.field_type === 'label_project_list') {
    const selectedIds = Array.isArray(value) ? value.map((item) => String(item)) : [];
    const knownIds = new Set(props.labelProjects.map((item) => item.label_project_id));
    const missingIds = selectedIds.filter((item) => !knownIds.has(item));
    return (
      <div className="runtime-project-picker">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {props.labelProjects.length ? (
          <div className="runtime-project-picker__items">
            {props.labelProjects.map((project) => {
              const checked = selectedIds.includes(project.label_project_id);
              return (
                <label key={project.label_project_id} className="runtime-project-picker__item">
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={(event) =>
                      props.onChange(
                        event.target.checked
                          ? [...selectedIds, project.label_project_id]
                          : selectedIds.filter((item) => item !== project.label_project_id)
                      )
                    }
                  />
                  <span>
                    {project.title}
                    <small className="subtle-text">{project.task_type}</small>
                  </span>
                </label>
              );
            })}
          </div>
        ) : (
          <p className="subtle-text">No Label Studio projects are currently available.</p>
        )}
        {missingIds.length ? (
          <small className="subtle-text">Unavailable selections: {missingIds.join(', ')}</small>
        ) : null}
      </div>
    );
  }

  const trackerPolygonRoi =
    props.node.node_type === 'analysis.active_stem_tracker' &&
    (props.field.key === 'transfer_rois' ||
      props.field.key === 'longitudinal_conveyor_rois');
  if (trackerPolygonRoi) {
    const regions = normalizeTrackerRoiList(value);
    const containsWholeViewPlaceholder = regions.some(trackerRoiIsWholeView);
    return (
      <div className="runtime-box-list">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {!spatialEditorLiveState ? (
          <div className="inline-alert inline-alert--info">
            Configure the upstream canonical view first. The tracker areas are drawn on the exact
            model image, not on the raw camera frame.
          </div>
        ) : null}
        {containsWholeViewPlaceholder ? (
          <div className="inline-alert inline-alert--info">
            The whole-view ROI is only a template placeholder. Applying the first drawn polygon
            replaces it automatically. Separate transfer and longitudinal areas prevent a waiting
            successor and the departing predecessor from becoming one ambiguous candidate group.
          </div>
        ) : null}
        <TrackerPolygonRoiEditor
          title={props.field.label}
          liveState={spatialEditorLiveState}
          regions={regions}
          defaultLabel={
            props.field.key === 'transfer_rois'
              ? 'Stem entry from the right'
              : 'Longitudinal conveyor'
          }
          onChange={props.onChange}
        />
      </div>
    );
  }

  if (props.field.field_type === 'zone_list' || props.field.field_type === 'roi') {
    const items = normalizeBoxList(value);
    const singleMeasurementCorridor =
      props.node.node_type === 'analysis.stem_measurement_eligibility' &&
      props.field.key === 'measurement_corridor';
    const updateItems = (next: StructuredBox[]) =>
      props.onChange(singleMeasurementCorridor ? next.slice(-1) : next);
    const addLabel = props.field.field_type === 'zone_list' ? 'Add zone' : 'Add ROI';
    const editorMode = props.field.field_type === 'zone_list' ? 'zone' : 'roi';
    return (
      <div className="runtime-box-list">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {singleMeasurementCorridor && !spatialEditorLiveState ? (
          <div className="inline-alert inline-alert--info">
            Finish Step 1 in the upstream canonical-view node. This editor then loads that image
            directly; segmentation and downstream analysis do not need to be running yet.
          </div>
        ) : null}
        <VisualBoxEditor
          title={props.field.label}
          liveState={spatialEditorLiveState}
          boxes={items}
          mode={editorMode}
          maxBoxes={singleMeasurementCorridor ? 1 : undefined}
          onChange={updateItems}
        />
        {items.map((item, index) => (
          <div key={`${item.label ?? 'item'}-${index}`} className="runtime-box-list__row">
            <input
              value={item.label ?? ''}
              onChange={(event) =>
                updateItems(
                  items.map((candidate, candidateIndex) =>
                    candidateIndex === index ? { ...candidate, label: event.target.value } : candidate
                  )
                )
              }
              placeholder={props.field.field_type === 'zone_list' ? 'Zone label' : 'ROI label'}
            />
            <input
              type="number"
              step={0.01}
              min={0}
              max={1}
              value={item.x}
              onChange={(event) =>
                updateItems(updateBoxListValue(items, index, 'x', Number(event.target.value)))
              }
            />
            <input
              type="number"
              step={0.01}
              min={0}
              max={1}
              value={item.y}
              onChange={(event) =>
                updateItems(updateBoxListValue(items, index, 'y', Number(event.target.value)))
              }
            />
            <input
              type="number"
              step={0.01}
              min={0}
              max={1}
              value={item.width}
              onChange={(event) =>
                updateItems(
                  updateBoxListValue(items, index, 'width', Number(event.target.value))
                )
              }
            />
            <input
              type="number"
              step={0.01}
              min={0}
              max={1}
              value={item.height}
              onChange={(event) =>
                updateItems(
                  updateBoxListValue(items, index, 'height', Number(event.target.value))
                )
              }
            />
            <input
              type="number"
              step={1}
              min={-180}
              max={180}
              value={item.angle_degrees}
              aria-label="Angle degrees"
              title="Angle degrees"
              onChange={(event) =>
                updateItems(
                  updateBoxListValue(items, index, 'angle_degrees', Number(event.target.value))
                )
              }
            />
            <button
              type="button"
              onClick={() => updateItems(items.filter((_, candidateIndex) => candidateIndex !== index))}
            >
              Remove
            </button>
          </div>
        ))}
        {!singleMeasurementCorridor || items.length === 0 ? (
          <button type="button" onClick={() => updateItems([...items, blankStructuredBox()])}>
            {addLabel}
          </button>
        ) : null}
      </div>
    );
  }

  if (props.field.field_type === 'slider') {
    const minimum = props.field.min_value ?? 0;
    const maximum = props.field.max_value ?? 100;
    const step = props.field.step_value ?? 1;
    const numericValue = clampNumber(Number(value), minimum, maximum);
    return (
      <label className="runtime-slider-field">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <span className="runtime-slider-field__controls">
          <input
            type="range"
            min={minimum}
            max={maximum}
            step={step}
            value={numericValue}
            aria-label={props.field.label}
            onChange={(event) => props.onChange(Number(event.target.value))}
          />
          <input
            type="number"
            min={minimum}
            max={maximum}
            step={step}
            value={numericValue}
            aria-label={`${props.field.label} exact value`}
            onChange={(event) => props.onChange(Number(event.target.value))}
          />
        </span>
      </label>
    );
  }

  if (props.field.field_type === 'integer' || props.field.field_type === 'number') {
    return (
      <label>
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <input
          type="number"
          step={props.field.field_type === 'integer' ? 1 : 0.01}
          min={props.field.min_value ?? undefined}
          max={props.field.max_value ?? undefined}
          value={Number(value)}
          onChange={(event) =>
            props.onChange(
              props.field.field_type === 'integer'
                ? Number.parseInt(event.target.value, 10)
                : Number.parseFloat(event.target.value)
            )
          }
        />
      </label>
    );
  }

  if (
    props.node.node_type === 'analysis.active_stem_tracker' &&
    props.field.key === 'class_filter'
  ) {
    return (
      <label>
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        {labelDatalist}
        <input
          list={authoringLabels.length ? labelDatalistId : undefined}
          value={String(value)}
          onChange={(event) => props.onChange(event.target.value)}
          placeholder="Empty = accept every segmentation class"
        />
        <small className="subtle-text">
          Leave this empty for a single-class stem model. Only enter exact, comma-separated model
          labels when the segmentation model also produces classes that must not be tracked.
        </small>
      </label>
    );
  }

  return (
    <label>
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <input value={String(value)} onChange={(event) => props.onChange(event.target.value)} />
    </label>
  );
}

type BoardCutClassRule = {
  class_id: number;
  label_snapshot: string;
  class_map_version: string | null;
  confirmed: boolean;
  relevant: boolean;
  measurement_basis: 'max_feret_diameter' | 'equivalent_diameter' | 'longitudinal_extent' | 'transverse_extent';
  safety_profile: 'standard' | 'conservative' | 'custom';
  clearance_multiplier: number;
  depth_factor: number;
  clearance_factor: number;
  minimum_confidence: number;
};

type BoardCalibrationPoint = { x: number; y: number };
type BoardSurfaceCalibration = {
  surface_role: 'top' | 'bottom' | 'side_a' | 'side_b' | 'end_face';
  image_points: BoardCalibrationPoint[];
  world_points_mm: BoardCalibrationPoint[];
};

type BoardSurfaceSemanticRole = 'board_main_face' | 'board_side_face' | 'board_end_face';
type BoardSurfaceClassSelection = {
  class_id: number;
  label_snapshot: string;
  class_map_version: string | null;
  deployment_release_id: string;
  confirmed: boolean;
};
type BoardSurfaceClassMapping = Partial<
  Record<BoardSurfaceSemanticRole, BoardSurfaceClassSelection>
>;

function BoardSurfaceClassMappingEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  modelCapabilities: RuntimeModelCapabilityOption[];
  onChange: (value: unknown) => void;
}) {
  const capability = upstreamModelCapability(
    props.graph,
    props.node,
    'segments',
    props.modelCapabilities
  );
  const labels = modelClassLabels(capability);
  const roles: BoardSurfaceSemanticRole[] =
    props.node.node_type === 'analysis.end_face_cut_constraints'
      ? ['board_end_face']
      : ['board_main_face', 'board_side_face'];
  const current = normalizeBoardSurfaceClassMapping(props.value);
  const reconciled = reconcileBoardSurfaceClassMapping(current, roles, capability);
  const expectedSignature = JSON.stringify(reconciled);
  const currentSignature = JSON.stringify(current);

  useEffect(() => {
    if (labels.length && expectedSignature !== currentSignature) {
      props.onChange(reconciled);
    }
  }, [currentSignature, expectedSignature, labels.length, props, reconciled]);

  if (!capability || !labels.length) {
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <div className="runtime-warning">
          Connect and select a deployed Segmentation model before assigning board surfaces.
        </div>
      </div>
    );
  }

  return (
    <div className="runtime-structured-table">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <small className="subtle-text">{formatModelCapabilityOption(capability)}</small>
      {roles.map((role) => {
        const selection = reconciled[role];
        return (
          <label key={role} className="runtime-structured-table__row">
            <span>{boardSurfaceSemanticRoleLabel(role)}</span>
            <select
              value={selection?.confirmed ? String(selection.class_id) : ''}
              onChange={(event) => {
                const classId = Number(event.target.value);
                const next = { ...reconciled };
                if (!Number.isInteger(classId) || !labels[classId]) {
                  delete next[role];
                } else {
                  next[role] = {
                    class_id: classId,
                    label_snapshot: labels[classId],
                    class_map_version:
                      String(capability.capability.class_map_version ?? '') || null,
                    deployment_release_id: capability.deployment_release_id,
                    confirmed: true
                  };
                }
                props.onChange(next);
              }}
            >
              <option value="">Select model class</option>
              {labels.map((label, classId) => (
                <option
                  key={`${classId}-${label}`}
                  value={classId}
                  disabled={roles.some(
                    (otherRole) =>
                      otherRole !== role && reconciled[otherRole]?.class_id === classId
                  )}
                >
                  {label}
                </option>
              ))}
            </select>
          </label>
        );
      })}
    </div>
  );
}

function BoardCutClassRulesEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  modelCapabilities: RuntimeModelCapabilityOption[];
  onChange: (value: unknown) => void;
}) {
  const capability = upstreamBoardCutModelCapability(
    props.graph,
    props.node,
    props.modelCapabilities
  );
  const labels = modelClassLabels(capability);
  const classMapVersion = String(capability?.capability.class_map_version ?? '') || null;
  const current = normalizeBoardCutClassRules(props.value);
  const rows = reconcileBoardCutClassRules(current, capability);
  const expectedSignature = JSON.stringify(rows);
  const currentSignature = JSON.stringify(current);

  useEffect(() => {
    if (labels.length && expectedSignature !== currentSignature) {
      props.onChange(rows);
    }
  }, [currentSignature, expectedSignature, labels.length, props, rows]);

  if (!capability || !labels.length) {
    return (
      <div className="runtime-structured-table">
        <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
        <div className="runtime-warning">
          Connect a deployed Detection or Segmentation node to generate the class rules.
        </div>
      </div>
    );
  }

  const update = (index: number, patch: Partial<BoardCutClassRule>) => {
    const next = rows.map((row, rowIndex) =>
      rowIndex === index ? { ...row, ...patch } : row
    );
    props.onChange(next);
  };
  const endFace = props.node.node_type === 'analysis.end_face_cut_constraints';

  return (
    <div className="runtime-structured-table">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <small className="subtle-text">
        {formatModelCapabilityOption(capability)} · {classMapVersion ?? 'class map from deployment'}
      </small>
      {rows.map((rule, index) => (
        <div key={`${rule.class_id}-${rule.label_snapshot}`} className="runtime-structured-table__row">
          <strong>{rule.label_snapshot}</strong>
          <label>
            <span>Cut relevance</span>
            <select
              value={!rule.confirmed ? 'pending' : rule.relevant ? 'relevant' : 'irrelevant'}
              onChange={(event) => {
                const state = event.target.value;
                update(index, {
                  confirmed: state !== 'pending',
                  relevant: state === 'relevant'
                });
              }}
            >
              <option value="pending">Review required</option>
              <option value="relevant">Relevant for cut</option>
              <option value="irrelevant">Not relevant</option>
            </select>
          </label>
          <label>
            <span>Measurement</span>
            <select
              value={rule.measurement_basis}
              disabled={!rule.relevant}
              onChange={(event) =>
                update(index, {
                  measurement_basis: event.target.value as BoardCutClassRule['measurement_basis']
                })
              }
            >
              <option value="max_feret_diameter">Maximum Feret diameter</option>
              <option value="equivalent_diameter">Equivalent diameter</option>
              <option value="longitudinal_extent">Longitudinal extent</option>
              <option value="transverse_extent">Transverse extent</option>
            </select>
          </label>
          <label>
            <span>Safety profile</span>
            <select
              value={rule.safety_profile}
              disabled={!rule.relevant}
              onChange={(event) => {
                const profile = event.target.value as BoardCutClassRule['safety_profile'];
                update(index, {
                  safety_profile: profile,
                  clearance_multiplier: profile === 'conservative' ? 1.5 : 1,
                  depth_factor: profile === 'conservative' ? 1.5 : 1,
                  clearance_factor: profile === 'conservative' ? 1.5 : 1
                });
              }}
            >
              <option value="standard">Standard (1×)</option>
              <option value="conservative">Conservative (1.5×)</option>
              <option value="custom">Custom</option>
            </select>
          </label>
          {endFace ? (
            <>
              <label>
                <span>Depth factor</span>
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  disabled={!rule.relevant}
                  value={rule.depth_factor}
                  onChange={(event) =>
                    update(index, {
                      safety_profile: 'custom',
                      depth_factor: finiteNumber(event.target.value, 1)
                    })
                  }
                />
              </label>
              <label>
                <span>Free-area factor</span>
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  disabled={!rule.relevant}
                  value={rule.clearance_factor}
                  onChange={(event) =>
                    update(index, {
                      safety_profile: 'custom',
                      clearance_factor: finiteNumber(event.target.value, 1)
                    })
                  }
                />
              </label>
            </>
          ) : (
            <label>
              <FieldLabel
                label="Clearance factor"
                helpText={
                  'Required free distance on both sides of the feature. A factor of 1 means one measured feature diameter must be free before the feature and another full diameter after it. If the distance from the board end to the first feature is already large enough, the result remains 0 mm.'
                }
              />
              <input
                type="number"
                min={0}
                step={0.1}
                disabled={!rule.relevant}
                value={rule.clearance_multiplier}
                onChange={(event) =>
                  update(index, {
                    safety_profile: 'custom',
                    clearance_multiplier: finiteNumber(event.target.value, 1)
                  })
                }
              />
            </label>
          )}
          <label>
            <span>Minimum confidence</span>
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={rule.minimum_confidence}
              onChange={(event) =>
                update(index, {
                  minimum_confidence: clampNumber(finiteNumber(event.target.value, 0.35), 0, 1)
                })
              }
            />
          </label>
        </div>
      ))}
    </div>
  );
}

type CameraCalibrationObservationDraft = RuntimeCameraCalibrationObservation & {
  included: boolean;
};

function CameraIntrinsicCalibrationEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  channelId: string | null;
  liveState: RuntimeLiveViewState | null;
  capturedFrame: RuntimeLiveFrame | null;
  captureAvailable: boolean;
  capturePending: boolean;
  onCapture: () => void;
  onChange: (value: unknown) => void;
}) {
  const { notify } = useNotifications();
  const [boardDefinition, setBoardDefinition] = useState<RuntimeCameraCalibrationBoardDefinition>({
    board_type: 'checkerboard',
    columns: 9,
    rows: 6,
    square_length: 1,
    marker_length: null,
    dictionary_name: 'DICT_4X4_50'
  });
  const [observations, setObservations] = useState<CameraCalibrationObservationDraft[]>([]);
  const [minimumObservationCount, setMinimumObservationCount] = useState(5);
  const [maximumRmsError, setMaximumRmsError] = useState(2);
  const [calculation, setCalculation] =
    useState<RuntimeCameraCalibrationCalculateResponse | null>(null);
  const lastCapturedFrameKey = useRef('');
  const currentProfile = normalizeCameraIntrinsicCalibrationProfile(props.value);
  const includedObservations = observations.filter((item) => item.included);
  const liveSourceFrame = intrinsicCalibrationSourceFrame(props.liveState);
  const liveSourceFrameKey = liveSourceFrame
    ? cameraCalibrationFrameKey(liveSourceFrame)
    : null;
  const liveSourceFrameAlreadyAdded = Boolean(
    liveSourceFrameKey &&
      observations.some(
        (observation) => cameraCalibrationFrameKey(observation.frame) === liveSourceFrameKey
      )
  );
  const cameraChannelActive = [
    'starting',
    'validating',
    'loading_pipeline',
    'running',
    'degraded',
    'stopping',
    'recovering'
  ].includes(props.liveState?.actual_state ?? '');

  useEffect(() => {
    setObservations([]);
    setCalculation(null);
    lastCapturedFrameKey.current = '';
  }, [props.channelId]);

  useEffect(() => {
    const frame = props.capturedFrame;
    if (!frame?.image_base64) return;
    const key = [frame.captured_at ?? '', frame.source_timestamp ?? '', frame.sequence].join(':');
    if (key === lastCapturedFrameKey.current) return;
    lastCapturedFrameKey.current = key;
    setCalculation(null);
    setObservations((current) => {
      const observationId = cameraCalibrationObservationId(frame, current.length + 1);
      if (current.some((item) => item.observation_id === observationId)) return current;
      return [...current, { observation_id: observationId, frame, included: true }];
    });
  }, [props.capturedFrame]);

  const calculationMutation = useMutation({
    mutationFn: (calibrationMethod: 'CHECKERBOARD_OR_CHARUCO' | 'IDENTITY_PASSTHROUGH') => {
      if (!props.channelId) throw new Error('Select a calibration camera channel first');
      if (calibrationMethod === 'IDENTITY_PASSTHROUGH') {
        if (!liveSourceFrame?.width || !liveSourceFrame.height) {
          throw new Error('Load one full-resolution camera image first');
        }
        return api.channels.calculateCameraCalibration(props.channelId, {
          board_definition: boardDefinition,
          observations: [],
          calibration_method: 'IDENTITY_PASSTHROUGH',
          image_width: liveSourceFrame.width,
          image_height: liveSourceFrame.height
        });
      }
      return api.channels.calculateCameraCalibration(props.channelId, {
        board_definition: boardDefinition,
        calibration_method: 'CHECKERBOARD_OR_CHARUCO',
        observations: includedObservations.map(({ observation_id, frame }) => ({
          observation_id,
          frame
        })),
        minimum_observation_count: minimumObservationCount,
        maximum_rms_reprojection_error_px: maximumRmsError,
        preview_observation_id: includedObservations[0]?.observation_id ?? null
      });
    },
    onSuccess: (response) => {
      setCalculation(response);
      if (response.status === 'valid' && response.profile) {
        props.onChange(response.profile);
      }
      const passthrough = response.profile?.calibration_method === 'IDENTITY_PASSTHROUGH';
      notify({
        title: passthrough
          ? 'Lens correction bypass applied'
          : response.status === 'valid'
            ? 'Camera calibration valid'
            : 'Camera calibration rejected',
        description:
          passthrough
            ? 'The node is runnable with unchanged camera pixels. Replace this profile with a measured calibration when the board is available.'
            : response.status === 'valid'
            ? `Calculated and applied automatically. RMS reprojection error ${formatCalibrationError(response.rms_reprojection_error_px)}`
            : response.failure_message ?? 'The observations did not produce a releasable profile.',
        tone: response.status === 'valid' ? 'success' : 'info'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Camera calibration failed'), tone: 'error' });
    }
  });

  const changeBoardDefinition = (next: RuntimeCameraCalibrationBoardDefinition) => {
    setBoardDefinition(next);
    setCalculation(null);
  };
  const canCalculate =
    Boolean(props.channelId) &&
    includedObservations.length >= minimumObservationCount &&
    !calculationMutation.isPending;
  const canUsePassthrough =
    Boolean(props.channelId && liveSourceFrame?.width && liveSourceFrame.height) &&
    !calculationMutation.isPending;

  return (
    <div className="runtime-structured-table runtime-camera-calibration">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <div className="inline-alert inline-alert--info">
        <strong>Calibrate now or continue temporarily without lens correction</strong>
        <p className="subtle-text">
          With the default setting, use a checkerboard with 10 x 7 squares. Add at least five
          views at different positions and tilts. Calculate once; a valid result is applied to
          this node automatically. If the board is not available, apply the temporary passthrough
          below. The node then keeps the camera pixels unchanged and the remaining pipeline can run.
        </p>
      </div>

      {liveSourceFrame?.image_base64 ? (
        <figure>
          <figcaption>
            Current full-resolution camera source / {liveSourceFrame.width ?? '?'} x{' '}
            {liveSourceFrame.height ?? '?'}
          </figcaption>
          <img
            src={calibrationFrameImageSource(liveSourceFrame) ?? undefined}
            alt="Current full-resolution intrinsic calibration source"
            style={{ width: '100%', maxHeight: 420, objectFit: 'contain' }}
          />
        </figure>
      ) : (
        <div className="runtime-warning">
          No full-resolution source frame is available yet. Start the camera stream; the
          inference runtime and the rest of the pipeline may remain stopped.
          If the stream is stopped too, use Capture new board observation for an exclusive
          single-frame acquisition.
        </div>
      )}
      <details className="runtime-advanced-graph">
        <summary>Board and quality settings (defaults normally work)</summary>
      <div className="runtime-structured-table__row runtime-structured-table__row--grid">
        <label>
          <span>Board type</span>
          <select
            value={boardDefinition.board_type}
            onChange={(event) => {
              const boardType = event.target.value as RuntimeCameraCalibrationBoardDefinition['board_type'];
              changeBoardDefinition({
                ...boardDefinition,
                board_type: boardType,
                marker_length: boardType === 'charuco' ? boardDefinition.marker_length ?? 0.7 : null
              });
            }}
          >
            <option value="checkerboard">Checkerboard</option>
            <option value="charuco">ChArUco</option>
          </select>
        </label>
        <label>
          <span>{boardDefinition.board_type === 'checkerboard' ? 'Inner columns' : 'Square columns'}</span>
          <input
            type="number"
            min={3}
            max={64}
            value={boardDefinition.columns}
            onChange={(event) =>
              changeBoardDefinition({
                ...boardDefinition,
                columns: clampNumber(Math.round(finiteNumber(event.target.value, 9)), 3, 64)
              })
            }
          />
        </label>
        <label>
          <span>{boardDefinition.board_type === 'checkerboard' ? 'Inner rows' : 'Square rows'}</span>
          <input
            type="number"
            min={3}
            max={64}
            value={boardDefinition.rows}
            onChange={(event) =>
              changeBoardDefinition({
                ...boardDefinition,
                rows: clampNumber(Math.round(finiteNumber(event.target.value, 6)), 3, 64)
              })
            }
          />
        </label>
        <label>
          <span>Square length [relative units]</span>
          <input
            type="number"
            min={0.001}
            step={0.01}
            value={boardDefinition.square_length}
            onChange={(event) =>
              changeBoardDefinition({
                ...boardDefinition,
                square_length: Math.max(finiteNumber(event.target.value, 1), 0.001)
              })
            }
          />
        </label>
        {boardDefinition.board_type === 'charuco' ? (
          <>
            <label>
              <span>Marker length [relative units]</span>
              <input
                type="number"
                min={0.001}
                step={0.01}
                value={boardDefinition.marker_length ?? 0.7}
                onChange={(event) =>
                  changeBoardDefinition({
                    ...boardDefinition,
                    marker_length: Math.min(
                      Math.max(finiteNumber(event.target.value, 0.7), 0.001),
                      boardDefinition.square_length * 0.99
                    )
                  })
                }
              />
            </label>
            <label>
              <span>ArUco dictionary</span>
              <select
                value={boardDefinition.dictionary_name}
                onChange={(event) =>
                  changeBoardDefinition({ ...boardDefinition, dictionary_name: event.target.value })
                }
              >
                <option value="DICT_4X4_50">4x4 / 50</option>
                <option value="DICT_5X5_100">5x5 / 100</option>
                <option value="DICT_6X6_250">6x6 / 250</option>
              </select>
            </label>
          </>
        ) : null}
        <label>
          <span>Minimum valid observations</span>
          <input
            type="number"
            min={3}
            max={50}
            value={minimumObservationCount}
            onChange={(event) =>
              setMinimumObservationCount(
                clampNumber(Math.round(finiteNumber(event.target.value, 5)), 3, 50)
              )
            }
          />
        </label>
        <label>
          <span>Maximum RMS reprojection error [px]</span>
          <input
            type="number"
            min={0.01}
            max={20}
            step={0.01}
            value={maximumRmsError}
            onChange={(event) =>
              setMaximumRmsError(clampNumber(finiteNumber(event.target.value, 2), 0.01, 20))
            }
          />
        </label>
      </div>
      </details>

      <div className="actions-row">
        <button
          type="button"
          className="button--primary"
          disabled={!liveSourceFrame?.image_base64 || liveSourceFrameAlreadyAdded}
          onClick={() => {
            if (!liveSourceFrame) return;
            setCalculation(null);
            setObservations((current) => {
              const frameKey = cameraCalibrationFrameKey(liveSourceFrame);
              if (
                current.some(
                  (observation) =>
                    cameraCalibrationFrameKey(observation.frame) === frameKey
                )
              ) {
                return current;
              }
              return [
                ...current,
                {
                  observation_id: cameraCalibrationObservationId(
                    liveSourceFrame,
                    current.length + 1
                  ),
                  frame: liveSourceFrame,
                  included: true
                }
              ];
            });
          }}
        >
          {liveSourceFrameAlreadyAdded
            ? 'Current live frame already added'
            : `Add current image (${includedObservations.length + 1}/${minimumObservationCount})`}
        </button>
        {!cameraChannelActive ? (
          <button
            type="button"
            disabled={!props.captureAvailable || props.capturePending}
            onClick={props.onCapture}
          >
            {props.capturePending ? 'Capturing...' : 'Capture one image from stopped stream'}
          </button>
        ) : null}
        <button
          type="button"
          className="button--primary"
          disabled={!canCalculate}
          onClick={() => calculationMutation.mutate('CHECKERBOARD_OR_CHARUCO')}
        >
          {calculationMutation.isPending ? 'Calculating...' : 'Calculate and apply calibration'}
        </button>
        <button
          type="button"
          disabled={!canUsePassthrough}
          onClick={() => calculationMutation.mutate('IDENTITY_PASSTHROUGH')}
        >
          Continue without lens correction
        </button>
      </div>
      <small className="subtle-text">
        Temporary passthrough is tied to the current image resolution. Replacing it with a measured
        calibration later changes the preprocessing version; recalculate the canonical view profile then.
      </small>
      {!props.captureAvailable ? (
        <small className="subtle-text">
          Open this pipeline on its camera channel or bind an authoring channel to the Channel frame source.
        </small>
      ) : cameraChannelActive ? (
        <small className="subtle-text">
          The channel is active: use the current full-resolution source frame above. Stop the
          channel only when an exclusive single-frame capture is required.
        </small>
      ) : liveSourceFrame?.metadata.calibration_capture_mode === 'camera_media_stream' ? (
        <small className="subtle-text">
          The source image comes directly from the running camera stream. The inference runtime
          is not required for calibration and remains stopped until the complete pipeline is activated.
        </small>
      ) : null}

      <details className="runtime-advanced-graph" open={Boolean(calculation && calculation.status !== 'valid')}>
        <summary>
          Observations: {includedObservations.length} selected / {observations.length} captured
        </summary>
        <div className="actions-row">
          <button type="button" disabled={!observations.length} onClick={() => {
            setObservations([]);
            setCalculation(null);
          }}>
            Clear all observations
          </button>
        </div>
      <div className="runtime-structured-table">
        {observations.map((observation, index) => {
          const result = calculation?.observations.find(
            (item) => item.observation_id === observation.observation_id
          );
          return (
            <div key={observation.observation_id} className="runtime-structured-table__row">
              <div className="actions-row">
                <img
                  src={calibrationFrameImageSource(observation.frame) ?? undefined}
                  alt={`Calibration observation ${index + 1}`}
                  style={{ width: 144, maxHeight: 96, objectFit: 'contain' }}
                />
                <label className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={observation.included}
                    onChange={(event) => {
                      setCalculation(null);
                      setObservations((current) =>
                        current.map((item) =>
                          item.observation_id === observation.observation_id
                            ? { ...item, included: event.target.checked }
                            : item
                        )
                      );
                    }}
                  />
                  <span>Use observation {index + 1}</span>
                </label>
                <span>
                  {result
                    ? result.accepted
                      ? `${result.detected_point_count} points / ${formatCalibrationError(result.reprojection_error_px)}`
                      : `${result.reason_code ?? 'rejected'}: ${result.message ?? ''}`
                    : `${observation.frame.width ?? '?'} x ${observation.frame.height ?? '?'}`}
                </span>
              </div>
            </div>
          );
        })}
      </div>
      </details>

      {calculation ? (
        <div className="runtime-structured-table">
          <div className={calculation.status === 'valid' ? 'runtime-success' : 'runtime-warning'}>
            {calculation.profile?.calibration_method === 'IDENTITY_PASSTHROUGH'
              ? 'Temporary passthrough is active; the image is forwarded without lens correction.'
              : calculation.status === 'valid'
              ? `Valid profile / RMS ${formatCalibrationError(calculation.rms_reprojection_error_px)}`
              : `${calculation.failure_code ?? 'Calibration invalid'}: ${calculation.failure_message ?? ''}`}
          </div>
          {calculation.preview_before && calculation.preview_after ? (
            <div
              className="runtime-structured-table__row runtime-structured-table__row--grid"
              aria-label="Raw and undistorted calibration comparison"
            >
              <figure>
                <figcaption>Raw</figcaption>
                <img
                  src={calibrationFrameImageSource(calculation.preview_before) ?? undefined}
                  alt="Raw calibration preview"
                  style={{ width: '100%', maxHeight: 320, objectFit: 'contain' }}
                />
              </figure>
              <figure>
                <figcaption>Undistorted (pose preserved)</figcaption>
                <img
                  src={calibrationFrameImageSource(calculation.preview_after) ?? undefined}
                  alt="Undistorted calibration preview"
                  style={{ width: '100%', maxHeight: 320, objectFit: 'contain' }}
                />
              </figure>
            </div>
          ) : null}
        </div>
      ) : null}

      {currentProfile ? (
        <div className={currentProfile.status === 'valid' ? 'runtime-success' : 'runtime-warning'}>
          {currentProfile.calibration_method === 'IDENTITY_PASSTHROUGH' ? (
            <>
              Applied temporary passthrough: {currentProfile.image_width} x{' '}
              {currentProfile.image_height}, hash {currentProfile.calibration_hash.slice(0, 12)}.
              Lens distortion is not corrected, but this node and downstream nodes are runnable.
            </>
          ) : (
            <>
              Applied profile: {currentProfile.image_width} x {currentProfile.image_height}, RMS{' '}
              {formatCalibrationError(currentProfile.rms_reprojection_error_px)}, hash{' '}
              {currentProfile.calibration_hash.slice(0, 12)}.
            </>
          )}{' '}
          The profile is already part of the current node settings; use Save changes in the pipeline toolbar.
        </div>
      ) : (
        <div className="runtime-warning">
          No profile has been calculated yet. You can still save the pipeline and continue
          editing; this node becomes runnable as soon as a valid result is calculated.
        </div>
      )}
    </div>
  );
}

function ImageViewProfileEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  channelId: string | null;
  liveState: RuntimeLiveViewState | null;
  onChange: (value: unknown) => void;
}) {
  const { notify } = useNotifications();
  const currentProfile = useMemo(() => normalizeImageViewProfile(props.value), [props.value]);
  const configuredViewProfileId = String(props.node.config.view_profile_id ?? '').trim();
  const intrinsicProfile = useMemo(
    () => upstreamIntrinsicCalibrationProfile(props.graph, props.node.node_key),
    [props.graph, props.node.node_key]
  );
  const undistortedFrame = stemProcessUndistortedCalibrationFrame(
    props.liveState,
    intrinsicProfile
  );
  const [viewProfileId, setViewProfileId] = useState(
    configuredViewProfileId || currentProfile?.view_profile_id || 'stem-measurement-view-v1'
  );
  const [sourceQuadrilateral, setSourceQuadrilateral] = useState<RuntimeNormalizedImagePoint[]>(
    currentProfile?.source_quadrilateral ?? []
  );
  const [outputWidth, setOutputWidth] = useState(currentProfile?.output_width ?? 1600);
  const [outputHeight, setOutputHeight] = useState(currentProfile?.output_height ?? 640);
  const [transformType, setTransformType] = useState<
    'POLYGON_MASKED_CROP' | 'PERSPECTIVE_RECTIFY'
  >(currentProfile?.transform_type ?? 'POLYGON_MASKED_CROP');
  const [interpolation, setInterpolation] = useState<'NEAREST' | 'LINEAR' | 'CUBIC'>(
    currentProfile?.interpolation ?? 'LINEAR'
  );
  const [notes, setNotes] = useState(currentProfile?.provenance.notes ?? '');
  const [calculation, setCalculation] =
    useState<RuntimeImageViewProfileCalculateResponse | null>(null);

  useEffect(() => {
    if (configuredViewProfileId) {
      setViewProfileId(configuredViewProfileId);
    } else if (currentProfile) {
      setViewProfileId(currentProfile.view_profile_id);
    }
    if (!currentProfile) return;
    setSourceQuadrilateral(currentProfile.source_quadrilateral);
    setOutputWidth(currentProfile.output_width);
    setOutputHeight(currentProfile.output_height);
    setTransformType(currentProfile.transform_type);
    setInterpolation(currentProfile.interpolation);
    setNotes(currentProfile.provenance.notes);
  }, [configuredViewProfileId, currentProfile]);

  const calculationMutation = useMutation({
    mutationFn: () => {
      if (!props.channelId) throw new Error('Select the process camera channel first');
      if (!intrinsicProfile) throw new Error('Connect a valid intrinsic undistort node first');
      if (!undistortedFrame?.width || !undistortedFrame.height) {
        throw new Error('Preview and freeze an intrinsically undistorted frame first');
      }
      return api.channels.calculateImageViewProfile(props.channelId, {
        view_profile_id: viewProfileId.trim(),
        source_image_width: undistortedFrame.width,
        source_image_height: undistortedFrame.height,
        intrinsic_calibration_hash: intrinsicProfile.calibration_hash,
        source_quadrilateral: sourceQuadrilateral,
        output_width: outputWidth,
        output_height: outputHeight,
        transform_type: transformType,
        interpolation,
        padding_mode: 'CONSTANT',
        padding_value: 0,
        enabled: true,
        source_frame_timestamp:
          undistortedFrame.source_timestamp ?? undistortedFrame.captured_at ?? null,
        notes,
        metadata: {
          purpose: 'canonical_model_and_label_view',
          preserves_source_perspective: transformType === 'POLYGON_MASKED_CROP'
        }
      });
    },
    onSuccess: (response) => {
      setCalculation(response);
      if (response.status === 'valid' && response.profile) {
        props.onChange(response.profile);
      }
      notify({
        title: response.status === 'valid' ? 'Canonical image view valid' : 'Image view rejected',
        description:
          response.status === 'valid'
            ? transformType === 'POLYGON_MASKED_CROP'
              ? 'The perspective-preserving corridor crop was calculated and applied automatically.'
              : 'The planar perspective rectification was calculated and applied automatically.'
            : response.failure_message ?? 'The source quadrilateral is invalid.',
        tone: response.status === 'valid' ? 'success' : 'info'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Image view calculation failed'), tone: 'error' });
    }
  });

  const addSourcePoint = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    const point = {
      x: clampNumber((event.clientX - rect.left) / rect.width, 0, 1),
      y: clampNumber((event.clientY - rect.top) / rect.height, 0, 1)
    };
    setCalculation(null);
    setSourceQuadrilateral((current) => (current.length >= 4 ? [point] : [...current, point]));
  };

  const previewProfile = calculation?.profile ?? currentProfile;
  const modelFrame = imageViewFrameForProfile(props.liveState?.frame ?? null, previewProfile);
  const fullFrame =
    undistortedFrame ??
    props.liveState?.source_frame ??
    intrinsicCalibrationSourceFrame(props.liveState);
  const canCalculate =
    Boolean(props.channelId && intrinsicProfile && undistortedFrame) &&
    viewProfileId.trim().length >= 3 &&
    sourceQuadrilateral.length === 4 &&
    outputWidth >= 64 &&
    outputHeight >= 64 &&
    !calculationMutation.isPending;

  return (
    <div className="runtime-structured-table">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <div className="inline-alert inline-alert--info">
        <strong>Step 1 — Select the exact model and Label Studio image</strong>
        <p className="subtle-text">
          Click the four corridor corners: top-left, top-right, bottom-right, bottom-left. Then
          calculate once. With the recommended perspective-preserving mode, the camera geometry
          remains unchanged: the polygon is cropped, everything outside it is black, and resizing
          is proportional with padding. Waiting stems outside the polygon are not model pixels and
          do not need labels.
        </p>
      </div>
      <label>
        <span>Model-image geometry</span>
        <select
          value={transformType}
          onChange={(event) => {
            setCalculation(null);
            setTransformType(
              event.target.value as 'POLYGON_MASKED_CROP' | 'PERSPECTIVE_RECTIFY'
            );
          }}
        >
          <option value="POLYGON_MASKED_CROP">
            Preserve camera perspective (recommended)
          </option>
          <option value="PERSPECTIVE_RECTIFY">
            Rectify polygon to rectangle (legacy planar mode)
          </option>
        </select>
        <small className="subtle-text">
          Perspective-preserving mode never stretches the four polygon corners to the output
          corners. Metric X/Y is calculated separately in Step 2.
        </small>
      </label>
      <details className="runtime-advanced-graph">
        <summary>Output and advanced settings</summary>
      <div className="runtime-structured-table__row runtime-structured-table__row--grid">
        <label>
          <span>View profile ID</span>
          <input
            value={viewProfileId}
            onChange={(event) => {
              setCalculation(null);
              setViewProfileId(event.target.value);
            }}
          />
        </label>
        <label>
          <span>Output width</span>
          <input
            type="number"
            min={64}
            max={8192}
            value={outputWidth}
            onChange={(event) => {
              setCalculation(null);
              setOutputWidth(Number(event.target.value));
            }}
          />
        </label>
        <label>
          <span>Output height</span>
          <input
            type="number"
            min={64}
            max={8192}
            value={outputHeight}
            onChange={(event) => {
              setCalculation(null);
              setOutputHeight(Number(event.target.value));
            }}
          />
        </label>
        <label>
          <span>Interpolation</span>
          <select
            value={interpolation}
            onChange={(event) => {
              setCalculation(null);
              setInterpolation(event.target.value as 'NEAREST' | 'LINEAR' | 'CUBIC');
            }}
          >
            <option value="LINEAR">Linear</option>
            <option value="NEAREST">Nearest</option>
            <option value="CUBIC">Cubic</option>
          </select>
        </label>
      </div>
        <label>
          <span>Provenance notes</span>
          <textarea value={notes} onChange={(event) => setNotes(event.target.value)} />
        </label>
        <small className="subtle-text">
          Linear interpolation is the recommended default. Changing interpolation only changes
          resampling; it does not fix missing calibration or geometry.
        </small>
      </details>

      {fullFrame?.image_base64 ? (
        <div
          className="runtime-live-editor__canvas"
          style={{
            ...previewMediaStyle(fullFrame.width ?? null, fullFrame.height ?? null),
            position: 'relative',
            cursor: undistortedFrame ? 'crosshair' : 'default'
          }}
          onPointerDown={undistortedFrame ? addSourcePoint : undefined}
          aria-label="Canonical image view source quadrilateral"
        >
          <img
            src={calibrationFrameImageSource(fullFrame) ?? undefined}
            alt={undistortedFrame ? 'Intrinsically undistorted full image' : 'Full camera image'}
            draggable={false}
            style={{ display: 'block', width: '100%', height: '100%', objectFit: 'fill' }}
          />
          <svg
            viewBox="0 0 100 100"
            preserveAspectRatio="none"
            style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
          >
            <polygon
              points={sourceQuadrilateral.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')}
              fill="#5db7ff26"
              stroke="#5db7ff"
              strokeWidth={0.6}
              vectorEffect="non-scaling-stroke"
            />
            {sourceQuadrilateral.map((point, index) => (
              <g key={`${index}-${point.x}-${point.y}`}>
                <circle cx={point.x * 100} cy={point.y * 100} r={1.2} fill="#ffffff" stroke="#111827" strokeWidth={0.4} />
                <text x={point.x * 100 + 1.4} y={point.y * 100 - 1.4} fill="#ffffff" stroke="#111827" strokeWidth={0.15} fontSize={3}>
                  {index + 1}
                </text>
              </g>
            ))}
          </svg>
        </div>
      ) : (
        <div className="runtime-warning">
          No camera image is available. Start only the camera stream; the inference runtime and
          the complete pipeline may remain stopped.
        </div>
      )}

      {fullFrame?.image_base64 && !undistortedFrame ? (
        <div className="runtime-warning">
          The raw camera image is visible, but corridor clicks stay disabled until the upstream
          intrinsic profile is calculated. No pipeline activation is required for that step.
        </div>
      ) : null}

      {undistortedFrame && intrinsicProfile?.calibration_method === 'IDENTITY_PASSTHROUGH' ? (
        <div className="inline-alert inline-alert--info">
          Temporary lens-correction passthrough is active. The displayed camera image is also
          the exact input to this node, so corridor clicks are enabled immediately.
        </div>
      ) : null}

      <div className="actions-row">
        <button
          type="button"
          disabled={!undistortedFrame}
          onClick={() => {
            setSourceQuadrilateral([
              { x: 0.01, y: 0.01 },
              { x: 0.99, y: 0.01 },
              { x: 0.99, y: 0.99 },
              { x: 0.01, y: 0.99 }
            ]);
            setCalculation(null);
          }}
        >
          Start with full image
        </button>
        <button
          type="button"
          onClick={() => {
            setSourceQuadrilateral([]);
            setCalculation(null);
          }}
        >
          Clear corners
        </button>
        <button
          type="button"
          className="button--primary"
          disabled={!canCalculate}
          onClick={() => calculationMutation.mutate()}
        >
          {calculationMutation.isPending ? 'Calculating...' : 'Calculate and apply view'}
        </button>
      </div>

      {calculation ? (
        <div className={calculation.status === 'valid' ? 'runtime-success' : 'runtime-warning'}>
          {calculation.status === 'valid' && calculation.profile
            ? `Valid ${calculation.profile.output_width} x ${calculation.profile.output_height} profile (${calculation.profile.transform_type}), checksum ${calculation.profile.profile_checksum.slice(0, 12)}.`
            : `${calculation.failure_code ?? 'Image view invalid'}: ${calculation.failure_message ?? ''}`}
        </div>
      ) : null}

      {currentProfile ? (
        <div className="runtime-success">
          Applied profile {currentProfile.view_profile_id}: {currentProfile.output_width} x{' '}
          {currentProfile.output_height}, {currentProfile.transform_type}, checksum{' '}
          {currentProfile.profile_checksum.slice(0, 12)}.
          It is already part of the current node settings. The canonical preview refreshes
          automatically; Run selected step remains available for a manual refresh.
        </div>
      ) : (
        <div className="runtime-warning">
          No canonical view is applied yet. The pipeline can still be saved and other nodes can be
          edited; this step becomes runnable after calculation.
        </div>
      )}

      {modelFrame ? (
        <div className="runtime-structured-table__row runtime-structured-table__row--grid">
          {props.liveState?.source_frame?.image_base64 ? (
            <figure>
              <figcaption>Full camera image (operator/diagnosis)</figcaption>
              <img
                src={calibrationFrameImageSource(props.liveState.source_frame) ?? undefined}
                alt="Full camera view"
                style={{ width: '100%', maxHeight: 320, objectFit: 'contain' }}
              />
            </figure>
          ) : null}
          <figure>
            <figcaption>
              Canonical model and Label Studio view —{' '}
              {previewProfile?.transform_type === 'POLYGON_MASKED_CROP'
                ? 'camera perspective preserved'
                : 'planar perspective rectified'}
            </figcaption>
            <img
              src={calibrationFrameImageSource(modelFrame) ?? undefined}
              alt="Canonical model view"
              style={{ width: '100%', maxHeight: 320, objectFit: 'contain' }}
            />
          </figure>
        </div>
      ) : null}
    </div>
  );
}

type StemProcessGeometryKey =
  | 'reference_points'
  | 'axis_origin'
  | 'axis_x_endpoint'
  | 'axis_y_endpoint'
  | 'axis_xy_corner'
  | 'axis_z_endpoint'
  | 'candidate_roi'
  | 'curvature_observation_roi'
  | 'surface_tracking_roi'
  | 'session_start_gate'
  | 'curvature_freeze_gate'
  | 'decision_gate';

type StemProcessPolygonDrafts = Record<
  'candidate_roi' | 'curvature_observation_roi' | 'surface_tracking_roi',
  RuntimeStemCurvatureImagePoint[]
>;

type StemProcessGateDrafts = Record<
  'session_start_gate' | 'curvature_freeze_gate' | 'decision_gate',
  RuntimeStemCurvatureImagePoint[]
>;

export type StemCoordinateFrameDraft = {
  origin_image_point: RuntimeStemCurvatureImagePoint | null;
  x_axis_image_endpoint: RuntimeStemCurvatureImagePoint | null;
  y_axis_image_endpoint: RuntimeStemCurvatureImagePoint | null;
  xy_corner_image_point: RuntimeStemCurvatureImagePoint | null;
  z_axis_image_endpoint: RuntimeStemCurvatureImagePoint | null;
};

const emptyStemCoordinateFrameDraft: StemCoordinateFrameDraft = {
  origin_image_point: null,
  x_axis_image_endpoint: null,
  y_axis_image_endpoint: null,
  xy_corner_image_point: null,
  z_axis_image_endpoint: null
};

const defaultStemReferencePlane: RuntimeStemCurvatureReferencePlane = {
  plane_id: 'longitudinal-conveyor-reference-plane',
  z_mm: 0,
  z_positive_direction: 'TOWARD_CAMERA',
  x_axis_description: 'X increases along the longitudinal conveyor toward the selected X endpoint.',
  y_axis_description: 'Y increases horizontally across the conveyor toward the selected Y endpoint.',
  z_axis_description: 'Z increases vertically upward, normal to the conveyor reference plane. Z is never the conveyor direction.',
  description: 'Planar reference used only for projective X/Y stem-curvature measurement.'
};

function stemCoordinateFrameDraftFromProfile(
  profile: RuntimeStemCurvatureProcessCalibrationProfile | null
): StemCoordinateFrameDraft {
  const definition = profile?.coordinate_frame_definition;
  return definition
    ? {
        origin_image_point: definition.origin_image_point,
        x_axis_image_endpoint: definition.x_axis_image_endpoint,
        y_axis_image_endpoint: definition.y_axis_image_endpoint,
        xy_corner_image_point: definition.xy_corner_image_point,
        z_axis_image_endpoint: definition.z_axis_image_endpoint
      }
    : { ...emptyStemCoordinateFrameDraft };
}

export function stemCoordinateFrameReferencePoints(
  draft: StemCoordinateFrameDraft,
  xLengthMm: number,
  yLengthMm: number
): RuntimeStemCurvatureProcessReferencePoint[] {
  if (
    !draft.origin_image_point ||
    !draft.x_axis_image_endpoint ||
    !draft.y_axis_image_endpoint ||
    !draft.xy_corner_image_point ||
    !Number.isFinite(xLengthMm) ||
    !Number.isFinite(yLengthMm) ||
    xLengthMm <= 0 ||
    yLengthMm <= 0
  ) {
    return [];
  }
  return [
    {
      point_id: 'axis-origin',
      image_point: draft.origin_image_point,
      process_point: { x_mm: 0, y_mm: 0 }
    },
    {
      point_id: 'axis-x-end',
      image_point: draft.x_axis_image_endpoint,
      process_point: { x_mm: xLengthMm, y_mm: 0 }
    },
    {
      point_id: 'axis-y-end',
      image_point: draft.y_axis_image_endpoint,
      process_point: { x_mm: 0, y_mm: yLengthMm }
    },
    {
      point_id: 'axis-xy-corner',
      image_point: draft.xy_corner_image_point,
      process_point: { x_mm: xLengthMm, y_mm: yLengthMm }
    }
  ];
}

export function stemCoordinateFrameDefinition(
  draft: StemCoordinateFrameDraft,
  xLengthMm: number,
  yLengthMm: number,
  zDirectionSemantics: RuntimeStemCurvatureCoordinateFrameDefinition['z_direction_semantics'],
  zReferenceLengthMm: number | null
): RuntimeStemCurvatureCoordinateFrameDefinition | null {
  const references = stemCoordinateFrameReferencePoints(draft, xLengthMm, yLengthMm);
  if (references.length !== 4 || !draft.z_axis_image_endpoint || !draft.origin_image_point) {
    return null;
  }
  return {
    method: 'ORTHOGONAL_XY_AXIS_ASSISTANT',
    origin_image_point: draft.origin_image_point,
    x_axis_image_endpoint: draft.x_axis_image_endpoint!,
    x_length_mm: xLengthMm,
    y_axis_image_endpoint: draft.y_axis_image_endpoint!,
    y_length_mm: yLengthMm,
    xy_corner_image_point: draft.xy_corner_image_point!,
    z_axis_image_endpoint: draft.z_axis_image_endpoint,
    z_direction_semantics: zDirectionSemantics,
    z_reference_length_mm:
      zReferenceLengthMm !== null && Number.isFinite(zReferenceLengthMm) && zReferenceLengthMm > 0
        ? zReferenceLengthMm
        : null
  };
}

export function stemProcessGeometryFromCanonicalView(
  profile: RuntimeImageViewProfile | null,
  conveyorDirection: 'POSITIVE_X' | 'NEGATIVE_X',
  coordinateFrame: RuntimeStemCurvatureCoordinateFrameDefinition | null = null
): {
  polygons: StemProcessPolygonDrafts;
  gates: StemProcessGateDrafts;
  decisionMarginMm: number;
} | null {
  if (!profile || profile.source_quadrilateral.length !== 4) return null;
  const points = profile.source_quadrilateral.map((point) => ({
    x_px: point.x * (profile.source_image_width - 1),
    y_px: point.y * (profile.source_image_height - 1)
  }));
  const edges = points.map((point, index) => [point, points[(index + 1) % points.length]]);
  const origin = coordinateFrame?.origin_image_point;
  const xEndpoint = coordinateFrame?.x_axis_image_endpoint;
  const xDirection =
    origin && xEndpoint
      ? { x: xEndpoint.x_px - origin.x_px, y: xEndpoint.y_px - origin.y_px }
      : { x: 1, y: 0 };
  const edgeScore = (edge: RuntimeStemCurvatureImagePoint[]) => {
    const midpointX = (edge[0].x_px + edge[1].x_px) / 2;
    const midpointY = (edge[0].y_px + edge[1].y_px) / 2;
    return midpointX * xDirection.x + midpointY * xDirection.y;
  };
  const orderedEdges = [...edges].sort((left, right) => edgeScore(left) - edgeScore(right));
  const minimumXEdge = orderedEdges[0];
  const maximumXEdge = orderedEdges[orderedEdges.length - 1];
  // In the stem installation, waiting logs enter transversely from the right-hand side of
  // the canonical view. Once transferred onto the longitudinal conveyor they move along +X.
  // Legacy/free-point profiles without an axis definition retain the former X-only fallback.
  const entryEdge = coordinateFrame
    ? edges[1]
    : conveyorDirection === 'POSITIVE_X'
      ? minimumXEdge
      : maximumXEdge;
  const exitEdge = conveyorDirection === 'POSITIVE_X' ? maximumXEdge : minimumXEdge;
  return {
    polygons: {
      candidate_roi: [...points],
      curvature_observation_roi: [...points],
      surface_tracking_roi: [...points]
    },
    gates: {
      session_start_gate: entryEdge,
      curvature_freeze_gate: exitEdge,
      decision_gate: exitEdge
    },
    decisionMarginMm: 0
  };
}

export function stemSpatialCalibrationUnavailableReason(
  intrinsic: RuntimeCameraIntrinsicCalibrationProfile | null,
  coordinateFrame: RuntimeStemCurvatureCoordinateFrameDefinition | null
): string | null {
  if (!intrinsic || intrinsic.status !== 'valid' ||
      intrinsic.calibration_method === 'IDENTITY_PASSTHROUGH' ||
      intrinsic.accepted_observation_count < 1) {
    return 'Calibrate the upstream camera with a checkerboard or ChArUco board first. Identity passthrough cannot determine spatial XYZ.';
  }
  if (!coordinateFrame || !coordinateFrame.z_reference_length_mm ||
      !Number.isFinite(coordinateFrame.z_reference_length_mm) || coordinateFrame.z_reference_length_mm <= 0) {
    return 'Use the coordinate-system assistant and enter the measured vertical distance from O to the Z endpoint.';
  }
  return null;
}

function StemCurvatureProcessCalibrationEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  channelId: string | null;
  liveState: RuntimeLiveViewState | null;
  onChange: (value: unknown) => void;
}) {
  const { notify } = useNotifications();
  const currentProfile = useMemo(
    () => normalizeStemCurvatureProcessCalibrationProfile(props.value),
    [props.value]
  );
  const [calibrationId, setCalibrationId] = useState(
    currentProfile?.calibration_id ?? 'stem-process-calibration'
  );
  const [referencePoints, setReferencePoints] = useState<RuntimeStemCurvatureProcessReferencePoint[]>(
    currentProfile?.reference_points ?? []
  );
  const [coordinateInputMode, setCoordinateInputMode] = useState<'axis_assistant' | 'free_points'>(
    currentProfile && !currentProfile.coordinate_frame_definition ? 'free_points' : 'axis_assistant'
  );
  const [measurementGeometryMode, setMeasurementGeometryMode] = useState<
    'canonical_view_corridor' | 'advanced_explicit'
  >(
    currentProfile?.measurement_geometry_mode === 'CANONICAL_VIEW_CORRIDOR_V1' ||
      normalizeImageViewProfile(props.node.config.view_profile)
      ? 'canonical_view_corridor'
      : currentProfile
        ? 'advanced_explicit'
        : 'canonical_view_corridor'
  );
  const [coordinateFrameDraft, setCoordinateFrameDraft] = useState<StemCoordinateFrameDraft>(() =>
    stemCoordinateFrameDraftFromProfile(currentProfile)
  );
  const [xReferenceLengthMm, setXReferenceLengthMm] = useState(
    currentProfile?.coordinate_frame_definition?.x_length_mm ?? 1000
  );
  const [yReferenceLengthMm, setYReferenceLengthMm] = useState(
    currentProfile?.coordinate_frame_definition?.y_length_mm ?? 500
  );
  const [zDirectionSemantics, setZDirectionSemantics] = useState<
    RuntimeStemCurvatureCoordinateFrameDefinition['z_direction_semantics']
  >(
    currentProfile?.coordinate_frame_definition?.z_direction_semantics ??
      'UP_FROM_REFERENCE_PLANE'
  );
  const [zReferenceLengthMm, setZReferenceLengthMm] = useState<number | null>(
    currentProfile?.coordinate_frame_definition?.z_reference_length_mm ?? null
  );
  const [spatialCalibrationEnabled, setSpatialCalibrationEnabled] = useState(
    Boolean(currentProfile?.spatial_calibration)
  );
  const [polygons, setPolygons] = useState<StemProcessPolygonDrafts>(() =>
    stemProcessPolygonsFromProfile(currentProfile)
  );
  const [gates, setGates] = useState<StemProcessGateDrafts>(() =>
    stemProcessGatesFromProfile(currentProfile)
  );
  const [referencePlane, setReferencePlane] = useState<RuntimeStemCurvatureReferencePlane>(
    currentProfile?.reference_plane ?? defaultStemReferencePlane
  );
  const [conveyorDirection, setConveyorDirection] = useState<'POSITIVE_X' | 'NEGATIVE_X'>(
    currentProfile?.conveyor_direction ?? 'POSITIVE_X'
  );
  const [rotationReferenceView, setRotationReferenceView] = useState<
    'LOOKING_IN_CONVEYOR_DIRECTION' | 'LOOKING_AGAINST_CONVEYOR_DIRECTION'
  >(currentProfile?.rotation_reference_view ?? 'LOOKING_IN_CONVEYOR_DIRECTION');
  const [positiveRotation, setPositiveRotation] = useState<'CLOCKWISE' | 'COUNTER_CLOCKWISE'>(
    currentProfile?.positive_rotation ?? 'CLOCKWISE'
  );
  const [decisionMarginMm, setDecisionMarginMm] = useState(
    currentProfile?.decision_margin_mm ?? 100
  );
  const [maximumRmsError, setMaximumRmsError] = useState(
    currentProfile?.maximum_allowed_reprojection_error_px ?? 2
  );
  const [diameterClass, setDiameterClass] = useState(currentProfile?.diameter_class ?? '');
  const [heightClass, setHeightClass] = useState(currentProfile?.height_class ?? '');
  const [notes, setNotes] = useState(currentProfile?.provenance.notes ?? '');
  const [activeGeometry, setActiveGeometry] = useState<StemProcessGeometryKey>(
    currentProfile && !currentProfile.coordinate_frame_definition
      ? 'reference_points'
      : 'axis_origin'
  );
  const [calculation, setCalculation] =
    useState<RuntimeStemCurvatureProcessCalibrationCalculateResponse | null>(null);
  const intrinsicProfile = useMemo(
    () => upstreamIntrinsicCalibrationProfile(props.graph, props.node.node_key),
    [props.graph, props.node.node_key]
  );
  const canonicalViewProfile = useMemo(
    () => normalizeImageViewProfile(props.node.config.view_profile),
    [props.node.config.view_profile]
  );
  const currentProfileMatchesBindings = Boolean(
    currentProfile &&
      intrinsicProfile &&
      currentProfile.intrinsic_calibration_hash === intrinsicProfile.calibration_hash &&
      currentProfile.source_channel_id === intrinsicProfile.source_channel_id &&
      currentProfile.image_width === intrinsicProfile.image_width &&
      currentProfile.image_height === intrinsicProfile.image_height &&
      (currentProfile.measurement_geometry_mode !== 'CANONICAL_VIEW_CORRIDOR_V1' ||
        (canonicalViewProfile &&
          currentProfile.canonical_view_profile_id === canonicalViewProfile.view_profile_id &&
          currentProfile.canonical_view_profile_checksum ===
            canonicalViewProfile.profile_checksum))
  );
  const calibrationFrame = stemProcessUndistortedCalibrationFrame(
    props.liveState,
    intrinsicProfile
  );

  useEffect(() => {
    if (!currentProfile) return;
    setCalibrationId(currentProfile.calibration_id);
    setReferencePoints(currentProfile.reference_points);
    setCoordinateInputMode(
      currentProfile.coordinate_frame_definition ? 'axis_assistant' : 'free_points'
    );
    setMeasurementGeometryMode(
      canonicalViewProfile ||
        currentProfile.measurement_geometry_mode === 'CANONICAL_VIEW_CORRIDOR_V1'
        ? 'canonical_view_corridor'
        : 'advanced_explicit'
    );
    setCoordinateFrameDraft(stemCoordinateFrameDraftFromProfile(currentProfile));
    setXReferenceLengthMm(
      currentProfile.coordinate_frame_definition?.x_length_mm ?? 1000
    );
    setYReferenceLengthMm(
      currentProfile.coordinate_frame_definition?.y_length_mm ?? 500
    );
    setZDirectionSemantics(
      currentProfile.coordinate_frame_definition?.z_direction_semantics ??
        'UP_FROM_REFERENCE_PLANE'
    );
    setZReferenceLengthMm(
      currentProfile.coordinate_frame_definition?.z_reference_length_mm ?? null
    );
    setSpatialCalibrationEnabled(Boolean(currentProfile.spatial_calibration));
    setPolygons(stemProcessPolygonsFromProfile(currentProfile));
    setGates(stemProcessGatesFromProfile(currentProfile));
    setReferencePlane(currentProfile.reference_plane);
    setConveyorDirection(currentProfile.conveyor_direction);
    setRotationReferenceView(currentProfile.rotation_reference_view);
    setPositiveRotation(currentProfile.positive_rotation);
    setDecisionMarginMm(currentProfile.decision_margin_mm);
    setMaximumRmsError(currentProfile.maximum_allowed_reprojection_error_px);
    setDiameterClass(currentProfile.diameter_class ?? '');
    setHeightClass(currentProfile.height_class ?? '');
    setNotes(currentProfile.provenance.notes);
  }, [canonicalViewProfile, currentProfile]);

  const assistantReferencePoints = useMemo(
    () =>
      stemCoordinateFrameReferencePoints(
        coordinateFrameDraft,
        xReferenceLengthMm,
        yReferenceLengthMm
      ),
    [coordinateFrameDraft, xReferenceLengthMm, yReferenceLengthMm]
  );
  const assistantCoordinateFrame = useMemo(
    () =>
      stemCoordinateFrameDefinition(
        coordinateFrameDraft,
        xReferenceLengthMm,
        yReferenceLengthMm,
        zDirectionSemantics,
        zReferenceLengthMm
      ),
    [
      coordinateFrameDraft,
      xReferenceLengthMm,
      yReferenceLengthMm,
      zDirectionSemantics,
      zReferenceLengthMm
    ]
  );
  const effectiveReferencePoints =
    coordinateInputMode === 'axis_assistant' ? assistantReferencePoints : referencePoints;
  const effectiveCoordinateFrame =
    coordinateInputMode === 'axis_assistant' ? assistantCoordinateFrame : null;
  const effectiveReferencePlane =
    coordinateInputMode === 'axis_assistant'
      ? {
          ...referencePlane,
          x_axis_description:
            'X increases along the longitudinal conveyor toward the selected X endpoint.',
          y_axis_description:
            'Y increases horizontally across the conveyor toward the selected Y endpoint.',
          z_axis_description: `${
            zDirectionSemantics === 'UP_FROM_REFERENCE_PLANE'
              ? 'Z increases vertically upward from the conveyor reference plane.'
              : 'Z increases vertically downward toward the machine base.'
          } Z is normal to the X/Y reference plane and is never the conveyor direction. Relative to the configured camera, positive Z points ${
            referencePlane.z_positive_direction === 'TOWARD_CAMERA'
              ? 'toward the camera.'
              : 'away from the camera.'
          }`
        }
      : referencePlane;
  const effectiveConveyorDirection =
    measurementGeometryMode === 'canonical_view_corridor' ? 'POSITIVE_X' : conveyorDirection;
  const canonicalViewGeometry = useMemo(
    () =>
      stemProcessGeometryFromCanonicalView(
        canonicalViewProfile,
        effectiveConveyorDirection,
        effectiveCoordinateFrame
      ),
    [canonicalViewProfile, effectiveConveyorDirection, effectiveCoordinateFrame]
  );
  const effectivePolygons =
    measurementGeometryMode === 'canonical_view_corridor' && canonicalViewGeometry
      ? canonicalViewGeometry.polygons
      : polygons;
  const effectiveGates =
    measurementGeometryMode === 'canonical_view_corridor' && canonicalViewGeometry
      ? canonicalViewGeometry.gates
      : gates;
  const effectiveDecisionMarginMm =
    measurementGeometryMode === 'canonical_view_corridor' && canonicalViewGeometry
      ? canonicalViewGeometry.decisionMarginMm
      : decisionMarginMm;

  const spatialUnavailableReason = spatialCalibrationEnabled
    ? stemSpatialCalibrationUnavailableReason(intrinsicProfile, effectiveCoordinateFrame)
    : null;
  const spatialResult = calculation?.profile?.spatial_calibration ??
    (currentProfileMatchesBindings ? currentProfile?.spatial_calibration : null);
  const calculationMutation = useMutation({
    mutationFn: () => {
      if (spatialUnavailableReason) throw new Error(spatialUnavailableReason);
      if (!props.channelId) throw new Error('Select the process camera channel first');
      if (!intrinsicProfile) throw new Error('Connect a valid intrinsic undistort node first');
      if (!calibrationFrame?.width || !calibrationFrame.height) {
        throw new Error('Preview and freeze an intrinsically undistorted frame first');
      }
      return api.channels.calculateStemProcessCalibration(props.channelId, {
        calibration_id: calibrationId.trim(),
        image_width: calibrationFrame.width,
        image_height: calibrationFrame.height,
        intrinsic_calibration_hash: intrinsicProfile.calibration_hash,
        reference_points: effectiveReferencePoints,
        coordinate_frame_definition: effectiveCoordinateFrame,
        spatial_calibration_enabled: spatialCalibrationEnabled,
        intrinsic_calibration: spatialCalibrationEnabled ? intrinsicProfile : null,
        measurement_geometry_mode:
          measurementGeometryMode === 'canonical_view_corridor'
            ? 'CANONICAL_VIEW_CORRIDOR_V1'
            : 'EXPLICIT_GEOMETRY_V1',
        canonical_view_profile_id:
          measurementGeometryMode === 'canonical_view_corridor'
            ? canonicalViewProfile?.view_profile_id ?? null
            : null,
        canonical_view_profile_checksum:
          measurementGeometryMode === 'canonical_view_corridor'
            ? canonicalViewProfile?.profile_checksum ?? null
            : null,
        reference_plane: effectiveReferencePlane,
        candidate_roi: { points: effectivePolygons.candidate_roi },
        curvature_observation_roi: { points: effectivePolygons.curvature_observation_roi },
        surface_tracking_roi: { points: effectivePolygons.surface_tracking_roi },
        session_start_gate: stemProcessGateRequest(effectiveGates.session_start_gate),
        curvature_freeze_gate: stemProcessGateRequest(effectiveGates.curvature_freeze_gate),
        decision_gate: stemProcessGateRequest(effectiveGates.decision_gate),
        decision_margin_mm: effectiveDecisionMarginMm,
        conveyor_direction: effectiveConveyorDirection,
        rotation_reference_view: rotationReferenceView,
        positive_rotation: positiveRotation,
        diameter_class: diameterClass.trim() || null,
        height_class: heightClass.trim() || null,
        maximum_rms_reprojection_error_px: maximumRmsError,
        reference_frame_timestamp:
          calibrationFrame.source_timestamp ?? calibrationFrame.captured_at ?? null,
        notes
      });
    },
    onSuccess: (response) => {
      setCalculation(response);
      if (response.status === 'valid' && response.profile) {
        props.onChange(response.profile);
      }
      notify({
        title:
          response.status === 'valid'
            ? 'Stem process calibration valid'
            : 'Stem process calibration rejected',
        description:
          response.status === 'valid'
            ? `Calculated and applied automatically. RMS reprojection error ${formatCalibrationError(response.rms_reprojection_error_px)}`
            : response.failure_message ?? 'The configured process geometry is invalid.',
        tone: response.status === 'valid' ? 'success' : 'info'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Stem process calibration failed'), tone: 'error' });
    }
  });

  const polygonReady = Object.values(effectivePolygons).every((points) => points.length >= 3);
  const gatesReady = Object.values(effectiveGates).every((points) => points.length === 2);
  const canCalculate =
    Boolean(props.channelId && intrinsicProfile && calibrationFrame) &&
    !spatialUnavailableReason &&
    calibrationId.trim().length >= 3 &&
    effectiveReferencePoints.length >= 4 &&
    (coordinateInputMode === 'free_points' || effectiveCoordinateFrame !== null) &&
    (measurementGeometryMode === 'advanced_explicit' || canonicalViewGeometry !== null) &&
    polygonReady &&
    gatesReady &&
    !calculationMutation.isPending;
  const activeGeometryInstruction: Record<StemProcessGeometryKey, string> = {
    reference_points: 'Click at least four known plane points, then enter their metric X/Y values below.',
    axis_origin: 'Click the coordinate origin O on the conveyor reference plane.',
    axis_x_endpoint: 'Click the endpoint of the known X distance. This defines positive X along the longitudinal conveyor.',
    axis_y_endpoint: 'Click the endpoint of the known Y distance. This defines positive Y horizontally across the conveyor.',
    axis_xy_corner: 'Click the physical corner that is X-length and Y-length away from O. This fourth plane point is required for perspective calibration.',
    axis_z_endpoint: spatialCalibrationEnabled
      ? 'Click the endpoint of the measured vertical distance directly above/below O, in the selected positive Z direction. An arbitrary direction arrow is not a spatial reference. X, Y and Z must form a right-handed frame.'
      : 'Click a visible direction point for Z. Z is vertical/normal to the conveyor plane and never points along the conveyor.',
    candidate_roi: 'Click three or more points around the area in which a new stem may enter.',
    curvature_observation_roi: 'Click three or more points around the area used for curvature observations.',
    surface_tracking_roi: 'Click three or more points around the bark area used for phase tracking.',
    session_start_gate: 'Click the two endpoints of the session-start line.',
    curvature_freeze_gate: 'Click the two endpoints after which no new curvature samples are accepted.',
    decision_gate: 'Click the two endpoints where the final angle decision must be available.'
  };

  const addImagePoint = (point: RuntimeStemCurvatureImagePoint) => {
    setCalculation(null);
    if (coordinateInputMode === 'axis_assistant') {
      const axisSelection: Partial<
        Record<StemProcessGeometryKey, keyof StemCoordinateFrameDraft>
      > = {
        axis_origin: 'origin_image_point',
        axis_x_endpoint: 'x_axis_image_endpoint',
        axis_y_endpoint: 'y_axis_image_endpoint',
        axis_xy_corner: 'xy_corner_image_point',
        axis_z_endpoint: 'z_axis_image_endpoint'
      };
      const draftKey = axisSelection[activeGeometry];
      if (draftKey) {
        setCoordinateFrameDraft((current) => ({ ...current, [draftKey]: point }));
        const nextGeometry: Partial<Record<StemProcessGeometryKey, StemProcessGeometryKey>> = {
          axis_origin: 'axis_x_endpoint',
          axis_x_endpoint: 'axis_y_endpoint',
          axis_y_endpoint: 'axis_xy_corner',
          axis_xy_corner: 'axis_z_endpoint',
          axis_z_endpoint:
            measurementGeometryMode === 'canonical_view_corridor'
              ? 'axis_z_endpoint'
              : 'candidate_roi'
        };
        setActiveGeometry(nextGeometry[activeGeometry] ?? activeGeometry);
        return;
      }
    }
    if (activeGeometry === 'reference_points') {
      setReferencePoints((current) => [
        ...current,
        {
          point_id: `reference-${current.length + 1}`,
          image_point: point,
          process_point: { x_mm: 0, y_mm: 0 }
        }
      ]);
      return;
    }
    if (activeGeometry in polygons) {
      const key = activeGeometry as keyof StemProcessPolygonDrafts;
      setPolygons((current) => ({ ...current, [key]: [...current[key], point] }));
      return;
    }
    const key = activeGeometry as keyof StemProcessGateDrafts;
    setGates((current) => ({
      ...current,
      [key]: current[key].length >= 2 ? [point] : [...current[key], point]
    }));
  };

  const clearActiveGeometry = () => {
    setCalculation(null);
    const axisSelection: Partial<
      Record<StemProcessGeometryKey, keyof StemCoordinateFrameDraft>
    > = {
      axis_origin: 'origin_image_point',
      axis_x_endpoint: 'x_axis_image_endpoint',
      axis_y_endpoint: 'y_axis_image_endpoint',
      axis_xy_corner: 'xy_corner_image_point',
      axis_z_endpoint: 'z_axis_image_endpoint'
    };
    const axisKey = axisSelection[activeGeometry];
    if (axisKey) {
      setCoordinateFrameDraft((current) => ({ ...current, [axisKey]: null }));
    } else if (activeGeometry === 'reference_points') {
      setReferencePoints([]);
    } else if (activeGeometry in polygons) {
      const key = activeGeometry as keyof StemProcessPolygonDrafts;
      setPolygons((current) => ({ ...current, [key]: [] }));
    } else {
      const key = activeGeometry as keyof StemProcessGateDrafts;
      setGates((current) => ({ ...current, [key]: [] }));
    }
  };

  return (
    <div className="runtime-structured-table runtime-stem-process-calibration">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <div className="inline-alert inline-alert--info">
        <strong>Step 2 — Calibrate plant coordinates and spatial Z</strong>
        <p className="subtle-text">
          Define one understandable coordinate frame instead of typing coordinates for arbitrary
          points: origin O, positive X, positive Y, the XY rectangle corner, and positive Z. X is
          the longitudinal conveyor direction, Y is horizontal across it, and Z is vertical/normal
          to the conveyor plane — Z is never the conveyor direction. The X/Y homography is used
          only for metric measurement and does not alter the model pixels from Step 1.
        </p>
      </div>

      <label className="runtime-checkbox">
        <input
          type="checkbox"
          checked={spatialCalibrationEnabled}
          onChange={(event) => {
            setCalculation(null);
            setSpatialCalibrationEnabled(event.target.checked);
          }}
        />
        Use measured Z reference for spatial camera calibration
      </label>
      {spatialCalibrationEnabled ? (
        <p className="subtle-text">
          Mark a real point vertically above or below O and enter its measured distance.
          Calculation fits the camera pose using X/Y and Z together. A rotating reference stem is
          not required. Unknown stem heights still need to be estimated by the measurement pipeline.
        </p>
      ) : null}
      {spatialUnavailableReason ? <div className="runtime-warning">{spatialUnavailableReason}</div> : null}
      {spatialResult ? (
        <div className="inline-alert inline-alert--info">
          <strong>Applied spatial camera calibration</strong>
          <p>
            Camera position X/Y/Z: {spatialResult.camera_center_mm.map((v) => v.toFixed(1)).join(' / ')} mm.
            {' '}Spatial RMS: {spatialResult.rms_reprojection_error_px.toFixed(3)} px;
            {' '}maximum: {spatialResult.maximum_reprojection_error_px.toFixed(3)} px;
            {' '}Z error: {spatialResult.z_reprojection_error_px.toFixed(3)} px.
          </p>
          <small className="subtle-text">These errors describe the fitted references, not an independent measurement accuracy.</small>
        </div>
      ) : null}

      {coordinateInputMode === 'axis_assistant' ? (
        <div className="runtime-structured-table">
          <strong>2A — Coordinate-system assistant</strong>
          <div className="runtime-property-overview">
            <div className="metric-pill">
              <span>+X</span>
              <strong>Longitudinal conveyor</strong>
              <small className="subtle-text">O → X endpoint, known distance in mm</small>
            </div>
            <div className="metric-pill">
              <span>+Y</span>
              <strong>Horizontal transverse</strong>
              <small className="subtle-text">O → Y endpoint, known distance in mm</small>
            </div>
            <div className="metric-pill">
              <span>+Z</span>
              <strong>Vertical plane normal</strong>
              <small className="subtle-text">Up/down from conveyor plane; not conveyor travel</small>
            </div>
          </div>
          <div className="runtime-structured-table__row runtime-structured-table__row--grid">
            <label>
              <span>Known X distance O → X [mm]</span>
              <input
                type="number"
                min={0.001}
                step={1}
                value={xReferenceLengthMm}
                onChange={(event) => {
                  setCalculation(null);
                  setXReferenceLengthMm(Math.max(finiteNumber(event.target.value, 0), 0));
                }}
              />
            </label>
            <label>
              <span>Known Y distance O → Y [mm]</span>
              <input
                type="number"
                min={0.001}
                step={1}
                value={yReferenceLengthMm}
                onChange={(event) => {
                  setCalculation(null);
                  setYReferenceLengthMm(Math.max(finiteNumber(event.target.value, 0), 0));
                }}
              />
              <small className="subtle-text">
                Required because horizontal Y curvature is the metric being measured.
              </small>
            </label>
            <label>
              <span>Physical +Z direction</span>
              <select
                value={zDirectionSemantics}
                onChange={(event) => {
                  setCalculation(null);
                  setZDirectionSemantics(
                    event.target.value as RuntimeStemCurvatureCoordinateFrameDefinition['z_direction_semantics']
                  );
                }}
              >
                <option value="UP_FROM_REFERENCE_PLANE">Up from conveyor plane</option>
                <option value="DOWN_TOWARD_MACHINE_BASE">Down toward machine base</option>
              </select>
            </label>
            <label>
              <span>{spatialCalibrationEnabled ? 'Measured Z distance O → Z [mm]' : 'Optional known Z reference length [mm]'}</span>
              <input
                type="number"
                min={0.001}
                step={1}
                value={zReferenceLengthMm ?? ''}
                placeholder="Optional"
                onChange={(event) => {
                  setCalculation(null);
                  setZReferenceLengthMm(
                    event.target.value.trim()
                      ? Math.max(finiteNumber(event.target.value, 0), 0)
                      : null
                  );
                }}
              />
              <small className="subtle-text">
                {spatialCalibrationEnabled
                  ? 'Required: distance along the physical vertical through O to the marked Z endpoint.'
                  : 'Enable spatial calibration to use this length in the camera pose calculation.'}
              </small>
            </label>
          </div>
          <div className="actions-row">
            {(
              [
                ['axis_origin', '1. Set origin O'],
                ['axis_x_endpoint', '2. Set +X endpoint'],
                ['axis_y_endpoint', '3. Set +Y endpoint'],
                ['axis_xy_corner', '4. Set XY corner'],
                ['axis_z_endpoint', '5. Set +Z direction']
              ] as Array<[StemProcessGeometryKey, string]>
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={activeGeometry === key ? 'button--primary' : ''}
                onClick={() => setActiveGeometry(key)}
              >
                {label}
              </button>
            ))}
          </div>
          {assistantCoordinateFrame ? (
            <div className="runtime-success">
              Coordinate frame complete: X {xReferenceLengthMm} mm / Y {yReferenceLengthMm} mm /
              Z {zDirectionSemantics === 'UP_FROM_REFERENCE_PLANE' ? 'up' : 'down'}.
            </div>
          ) : activeGeometry.startsWith('axis_') ? (
            <div className="inline-alert inline-alert--info">
              <strong>{activeGeometry.replaceAll('_', ' ')}</strong>
              <p className="subtle-text">{activeGeometryInstruction[activeGeometry]}</p>
              <p className="subtle-text">
                The XY corner is the fourth coplanar point needed to correct the oblique camera
                geometry. The Z arrow is stored as documentation and is not used as an X/Y point.
              </p>
            </div>
          ) : (
            <div className="runtime-warning">
              Coordinate frame incomplete. Select the missing axis step above and click the image.
            </div>
          )}
        </div>
      ) : (
        <div className="inline-alert inline-alert--info">
          This existing profile uses advanced free X/Y correspondences. It remains unchanged for
          backward compatibility.
          <div className="actions-row">
            <button
              type="button"
              onClick={() => {
                setCoordinateInputMode('axis_assistant');
                setCoordinateFrameDraft({ ...emptyStemCoordinateFrameDraft });
                setActiveGeometry('axis_origin');
                setCalculation(null);
              }}
            >
              Replace with coordinate-system assistant
            </button>
          </div>
        </div>
      )}

      <details className="runtime-advanced-graph">
        <summary>Coordinate, direction and advanced settings</summary>
      <div className="runtime-structured-table__row runtime-structured-table__row--grid">
        <label>
          <span>Calibration ID</span>
          <input value={calibrationId} onChange={(event) => setCalibrationId(event.target.value)} />
        </label>
        <label>
          <span>Reference plane ID</span>
          <input
            value={referencePlane.plane_id}
            onChange={(event) =>
              setReferencePlane({ ...referencePlane, plane_id: event.target.value })
            }
          />
        </label>
        <label>
          <span>Reference plane Z [mm]</span>
          <input
            type="number"
            step={0.1}
            value={referencePlane.z_mm}
            onChange={(event) =>
              setReferencePlane({
                ...referencePlane,
                z_mm: finiteNumber(event.target.value, 0)
              })
            }
          />
        </label>
        <label>
          <span>Camera relation of +Z</span>
          <select
            value={referencePlane.z_positive_direction}
            onChange={(event) => {
              const direction = event.target.value as RuntimeStemCurvatureReferencePlane['z_positive_direction'];
              setReferencePlane({
                ...referencePlane,
                z_positive_direction: direction,
                z_axis_description:
                  direction === 'TOWARD_CAMERA'
                    ? 'Z is normal to the process reference plane and increases toward the camera.'
                    : 'Z is normal to the process reference plane and increases away from the camera.'
              });
            }}
          >
            <option value="TOWARD_CAMERA">Positive Z points toward camera</option>
            <option value="AWAY_FROM_CAMERA">Positive Z points away from camera</option>
          </select>
          <small className="subtle-text">
            This describes the camera viewpoint. Physical Z remains vertical/normal to the X/Y
            conveyor plane and is never the conveyor direction.
          </small>
        </label>
        {measurementGeometryMode === 'advanced_explicit' ? (
          <label>
            <span>Conveyor direction</span>
            <select
              value={conveyorDirection}
              onChange={(event) =>
                setConveyorDirection(event.target.value as 'POSITIVE_X' | 'NEGATIVE_X')
              }
            >
              <option value="POSITIVE_X">Increasing X</option>
              <option value="NEGATIVE_X">Decreasing X</option>
            </select>
          </label>
        ) : (
          <small className="subtle-text">
            The selected +X endpoint defines the conveyor travel direction. No second direction
            setting is needed for the canonical corridor.
          </small>
        )}
        <label>
          <span>Rotation reference view</span>
          <select
            value={rotationReferenceView}
            onChange={(event) =>
              setRotationReferenceView(
                event.target.value as
                  | 'LOOKING_IN_CONVEYOR_DIRECTION'
                  | 'LOOKING_AGAINST_CONVEYOR_DIRECTION'
              )
            }
          >
            <option value="LOOKING_IN_CONVEYOR_DIRECTION">Looking in conveyor direction</option>
            <option value="LOOKING_AGAINST_CONVEYOR_DIRECTION">Looking against conveyor direction</option>
          </select>
        </label>
        <label>
          <span>Positive rotation</span>
          <select
            value={positiveRotation}
            onChange={(event) =>
              setPositiveRotation(event.target.value as 'CLOCKWISE' | 'COUNTER_CLOCKWISE')
            }
          >
            <option value="CLOCKWISE">Clockwise from reference view</option>
            <option value="COUNTER_CLOCKWISE">Counter-clockwise from reference view</option>
          </select>
        </label>
        {measurementGeometryMode === 'advanced_explicit' ? (
          <label>
            <span>Decision margin [mm along X]</span>
            <input
              type="number"
              min={0}
              step={1}
              value={decisionMarginMm}
              onChange={(event) =>
                setDecisionMarginMm(Math.max(finiteNumber(event.target.value, 100), 0))
              }
            />
          </label>
        ) : null}
        <label>
          <span>Maximum RMS reprojection error [px]</span>
          <input
            type="number"
            min={0.01}
            max={50}
            step={0.01}
            value={maximumRmsError}
            onChange={(event) =>
              setMaximumRmsError(clampNumber(finiteNumber(event.target.value, 2), 0.01, 50))
            }
          />
          {spatialCalibrationEnabled ? (
            <small className="subtle-text">
              Spatial calibration also requires every XY/Z point to remain within this limit.
            </small>
          ) : null}
        </label>
        <label>
          <span>Optional diameter class</span>
          <input value={diameterClass} onChange={(event) => setDiameterClass(event.target.value)} />
        </label>
        <label>
          <span>Optional height class</span>
          <input value={heightClass} onChange={(event) => setHeightClass(event.target.value)} />
        </label>
      </div>
      </details>

      {measurementGeometryMode === 'canonical_view_corridor' ? (
        <div className="inline-alert inline-alert--info">
          <strong>Canonical view is the measurement corridor</strong>
          {canonicalViewProfile && canonicalViewGeometry ? (
            <>
              <p className="subtle-text">
                No additional ROI or gate drawing is needed. When the stem enters{' '}
                <strong>{canonicalViewProfile.view_profile_id}</strong> transversely across the
                right-hand edge, the session starts. After transfer onto the longitudinal
                conveyor it is measured and surface-tracked along +X. At the red +X exit edge,
                curvature freezes and the decision becomes due.
              </p>
              <p className="subtle-text">
                The process profile is bound to checksum{' '}
                {canonicalViewProfile.profile_checksum.slice(0, 12)}. Changing the canonical view
                requires recalculating this process profile, preventing stale hidden geometry.
              </p>
            </>
          ) : (
            <p className="subtle-text">
              Calculate and apply the Canonical image view in Step 1 first. Its four corridor
              corners will be reused automatically here.
            </p>
          )}
          <details className="runtime-advanced-graph">
            <summary>Special case: configure separate areas and gates</summary>
            <p className="subtle-text">
              Only use this for an existing installation whose measurement, tracking and decision
              zones intentionally differ from the canonical model view.
            </p>
            <button
              type="button"
              onClick={() => {
                setMeasurementGeometryMode('advanced_explicit');
                setActiveGeometry(
                  coordinateInputMode === 'free_points' ? 'reference_points' : 'candidate_roi'
                );
                setCalculation(null);
              }}
            >
              Use advanced separate geometry
            </button>
          </details>
        </div>
      ) : (
        <div className="runtime-structured-table">
          <div className="inline-alert inline-alert--info">
            <strong>Advanced separate process geometry</strong>
            <p className="subtle-text">
              This mode is retained for existing profiles. For the normal stem pipeline, reuse
              the canonical view instead.
            </p>
            {canonicalViewProfile ? (
              <button
                type="button"
                onClick={() => {
                  setMeasurementGeometryMode('canonical_view_corridor');
                  setCalculation(null);
                }}
              >
                Use canonical view as measurement corridor
              </button>
            ) : null}
          </div>
          <div className="actions-row">
            {(
              [
                ...(coordinateInputMode === 'free_points'
                  ? ([['reference_points', 'Reference points']] as Array<[
                      StemProcessGeometryKey,
                      string
                    ]>)
                  : []),
                ['candidate_roi', 'Candidate ROI'],
                ['curvature_observation_roi', 'Curvature ROI'],
                ['surface_tracking_roi', 'Surface ROI'],
                ['session_start_gate', 'Session start'],
                ['curvature_freeze_gate', 'Curvature freeze'],
                ['decision_gate', 'Decision gate']
              ] as Array<[StemProcessGeometryKey, string]>
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={activeGeometry === key ? 'button--primary' : ''}
                onClick={() => setActiveGeometry(key)}
              >
                {label}
              </button>
            ))}
            <button type="button" onClick={clearActiveGeometry}>Clear selected</button>
          </div>
          {!activeGeometry.startsWith('axis_') ? (
            <div className="inline-alert inline-alert--info">
              <strong>{activeGeometry.replaceAll('_', ' ')}</strong>
              <p className="subtle-text">{activeGeometryInstruction[activeGeometry]}</p>
              <p className="subtle-text">
                Progress: {effectiveReferencePoints.length}/4 plane points;{' '}
                {Object.values(polygons).filter((points) => points.length >= 3).length}/3 areas;{' '}
                {Object.values(gates).filter((points) => points.length === 2).length}/3 gates.
              </p>
            </div>
          ) : null}
        </div>
      )}

      {calibrationFrame ? (
        <StemProcessCalibrationCanvas
          frame={calibrationFrame}
          activeGeometry={activeGeometry}
          referencePoints={effectiveReferencePoints}
          coordinateFrameDraft={
            coordinateInputMode === 'axis_assistant' ? coordinateFrameDraft : null
          }
          polygons={effectivePolygons}
          gates={effectiveGates}
          profile={calculation?.profile ?? currentProfile}
          onPoint={addImagePoint}
        />
      ) : (
        <div className="runtime-warning">
          No undistorted image is available yet. Configure the upstream camera calibration; this
          editor then loads its output automatically without activating the complete pipeline.
        </div>
      )}

      {coordinateInputMode === 'free_points' ? (
      <details className="runtime-advanced-graph" open>
        <summary>Advanced free X/Y correspondences ({referencePoints.length})</summary>
        <div className="runtime-structured-table">
        <small className="subtle-text">
          X and Y are plant coordinates in millimetres. This editor is retained only for existing
          profiles and special commissioning cases.
        </small>
        {referencePoints.map((point, index) => (
          <div key={`${point.point_id}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--grid">
            <label>
              <span>ID</span>
              <input
                value={point.point_id}
                onChange={(event) =>
                  setReferencePoints((current) =>
                    current.map((item, itemIndex) =>
                      itemIndex === index ? { ...item, point_id: event.target.value } : item
                    )
                  )
                }
              />
            </label>
            <span>Image: {point.image_point.x_px.toFixed(1)}, {point.image_point.y_px.toFixed(1)} px</span>
            <label>
              <span>X [mm]</span>
              <input
                type="number"
                step={0.1}
                value={point.process_point.x_mm}
                onChange={(event) =>
                  setReferencePoints((current) =>
                    current.map((item, itemIndex) =>
                      itemIndex === index
                        ? {
                            ...item,
                            process_point: {
                              ...item.process_point,
                              x_mm: finiteNumber(event.target.value, 0)
                            }
                          }
                        : item
                    )
                  )
                }
              />
            </label>
            <label>
              <span>Y [mm]</span>
              <input
                type="number"
                step={0.1}
                value={point.process_point.y_mm}
                onChange={(event) =>
                  setReferencePoints((current) =>
                    current.map((item, itemIndex) =>
                      itemIndex === index
                        ? {
                            ...item,
                            process_point: {
                              ...item.process_point,
                              y_mm: finiteNumber(event.target.value, 0)
                            }
                          }
                        : item
                    )
                  )
                }
              />
            </label>
            <button
              type="button"
              onClick={() =>
                setReferencePoints((current) => current.filter((_item, itemIndex) => itemIndex !== index))
              }
            >
              Remove
            </button>
          </div>
        ))}
        </div>
      </details>
      ) : null}

      <details className="runtime-advanced-graph">
        <summary>Axis documentation and provenance</summary>
      <div className="runtime-structured-table__row runtime-structured-table__row--grid">
        <label>
          <span>X-axis documentation</span>
          <input
            value={referencePlane.x_axis_description}
            onChange={(event) =>
              setReferencePlane({ ...referencePlane, x_axis_description: event.target.value })
            }
          />
        </label>
        <label>
          <span>Y-axis documentation</span>
          <input
            value={referencePlane.y_axis_description}
            onChange={(event) =>
              setReferencePlane({ ...referencePlane, y_axis_description: event.target.value })
            }
          />
        </label>
        <label>
          <span>Z-axis documentation</span>
          <input
            value={referencePlane.z_axis_description}
            onChange={(event) =>
              setReferencePlane({ ...referencePlane, z_axis_description: event.target.value })
            }
          />
        </label>
        <label>
          <span>Provenance notes</span>
          <input value={notes} onChange={(event) => setNotes(event.target.value)} />
        </label>
      </div>
      </details>

      <div className="actions-row">
        <button
          type="button"
          className="button--primary"
          disabled={!canCalculate}
          onClick={() => calculationMutation.mutate()}
        >
          {calculationMutation.isPending ? 'Calculating...' : 'Calculate and apply process calibration'}
        </button>
      </div>
      <small className="subtle-text">
        Required: O, X, Y, XY corner and Z direction plus known X/Y lengths. In the standard mode,
        the already configured Canonical image view supplies the complete measurement corridor;
        no additional ROI or gate clicks are required.
      </small>

      {calculation ? (
        <div className={calculation.status === 'valid' ? 'runtime-success' : 'runtime-warning'}>
          {calculation.status === 'valid'
            ? `Valid process profile / RMS ${formatCalibrationError(calculation.rms_reprojection_error_px)} / maximum ${formatCalibrationError(calculation.maximum_reprojection_error_px)}`
            : `${calculation.failure_code ?? 'Calibration invalid'}: ${calculation.failure_message ?? ''}`}
        </div>
      ) : null}

      {currentProfile ? (
        <div className={currentProfileMatchesBindings ? 'runtime-success' : 'runtime-warning'}>
          Applied process profile {currentProfile.calibration_id}, hash{' '}
          {currentProfile.calibration_hash.slice(0, 12)}, intrinsic hash{' '}
          {currentProfile.intrinsic_calibration_hash.slice(0, 12)}.{' '}
          {currentProfile.measurement_geometry_mode === 'CANONICAL_VIEW_CORRIDOR_V1'
            ? `Measurement corridor: canonical view ${currentProfile.canonical_view_profile_id}. `
            : 'Measurement corridor: advanced separate geometry. '}
          {currentProfileMatchesBindings
            ? 'It is already part of the current node settings.'
            : 'This applied profile belongs to the previous intrinsic or canonical view. Calculate and apply the process calibration once for the current view.'}
        </div>
      ) : (
        <div className="runtime-warning">
          No process profile is applied yet. You can still save the pipeline and continue editing.
        </div>
      )}
    </div>
  );
}

type StemSurfacePhaseCalibrationCaptureFrame = {
  capture_id: string;
  frame: RuntimeLiveFrame;
  track_id: string;
  frame_sequence: number;
  timestamp: string | null;
  raw_phase: number;
  delta_raw_phase: number | null;
  tracking_mode: string;
  relock_status: string;
  trace_generation: number;
  reference_eligible: boolean;
  measurement_eligible: boolean;
  failure_count: number;
  confidence: number;
  detected_feature_count: number;
  inlier_ratio: number;
  matched_feature_count: number;
  diameter_mm: number;
  image_center_x_px: number;
  image_center_y_px: number;
  reason_codes: string[];
};

export function stemSurfacePhaseCalibrationFitPreview(
  observations: RuntimeStemSurfacePhaseCalibrationObservation[]
) {
  if (observations.length < 2) return null;
  const denominator = observations.reduce(
    (sum, item) => sum + Math.max(item.weight, 0.01) * item.raw_phase * item.raw_phase,
    0
  );
  if (!Number.isFinite(denominator) || denominator <= 1e-12) return null;
  const numerator = observations.reduce(
    (sum, item) =>
      sum + Math.max(item.weight, 0.01) * item.raw_phase * item.known_angle_deg,
    0
  );
  const degreesPerRawPhase = numerator / denominator;
  if (!Number.isFinite(degreesPerRawPhase)) return null;
  const evaluated = observations.map((item) => {
    const calculatedPhaseDeg = degreesPerRawPhase * item.raw_phase;
    return {
      calculated_phase_deg: calculatedPhaseDeg,
      error_deg: Math.abs(item.known_angle_deg - calculatedPhaseDeg)
    };
  });
  const errors = evaluated.map((item) => item.error_deg);
  const maximumAbsErrorDeg = Math.max(...errors);
  return {
    degrees_per_raw_phase: degreesPerRawPhase,
    evaluated,
    mean_abs_error_deg: errors.reduce((sum, value) => sum + value, 0) / errors.length,
    rms_error_deg: Math.sqrt(
      errors.reduce((sum, value) => sum + value * value, 0) / errors.length
    ),
    maximum_abs_error_deg: maximumAbsErrorDeg,
    worst_observation_index: errors.indexOf(maximumAbsErrorDeg)
  };
}

export function stemSurfacePhaseCalibrationCaptureFromLiveState(
  liveState: RuntimeLiveViewState
): StemSurfacePhaseCalibrationCaptureFrame | null {
  const frame = liveState.frame;
  if (!frame?.image_base64) return null;
  const phaseOverlay = [...liveState.overlay]
    .reverse()
    .find((primitive) => primitive.layer === 'stem_surface_phase');
  const metadata = phaseOverlay?.metadata ?? {};
  const rawPhase = Number(metadata.raw_phase);
  const trackId = typeof metadata.track_id === 'string' ? metadata.track_id.trim() : '';
  const phaseFrameSequence = Number(metadata.frame_sequence);
  const frameSequence = Number.isFinite(phaseFrameSequence)
    ? phaseFrameSequence
    : frame.sequence;
  const diameterMm = Number(metadata.diameter_mm);
  const centerX = Number(metadata.image_center_x_px);
  const centerY = Number(metadata.image_center_y_px);
  const traceGeneration = Number(metadata.trace_generation);
  if (
    metadata.eligible !== true ||
    metadata.phase_locked !== true ||
    !trackId ||
    !Number.isFinite(rawPhase) ||
    !Number.isFinite(frameSequence) ||
    !Number.isFinite(diameterMm) ||
    diameterMm <= 0 ||
    !Number.isFinite(centerX) ||
    !Number.isFinite(centerY) ||
    !Number.isFinite(traceGeneration)
  ) {
    return null;
  }
  if (frame.sequence > 0 && frameSequence > 0 && frame.sequence !== frameSequence) {
    return null;
  }
  const timestamp =
    typeof metadata.timestamp === 'string' && metadata.timestamp
      ? metadata.timestamp
      : frame.source_timestamp ?? frame.captured_at ?? null;
  return {
    capture_id: `${trackId}:${Math.max(Math.trunc(traceGeneration), 0)}:${frameSequence}:${timestamp ?? 'frame'}`,
    frame,
    track_id: trackId,
    frame_sequence: frameSequence,
    timestamp,
    raw_phase: rawPhase,
    delta_raw_phase:
      metadata.delta_raw_phase !== null &&
      metadata.delta_raw_phase !== undefined &&
      Number.isFinite(Number(metadata.delta_raw_phase))
      ? Number(metadata.delta_raw_phase)
      : null,
    tracking_mode:
      typeof metadata.tracking_mode === 'string' ? metadata.tracking_mode : 'UNKNOWN',
    relock_status:
      typeof metadata.relock_status === 'string' ? metadata.relock_status : 'NOT_ATTEMPTED',
    trace_generation: Math.max(Math.trunc(traceGeneration), 0),
    reference_eligible: metadata.reference_eligible === true,
    measurement_eligible: metadata.measurement_eligible === true,
    failure_count: Math.max(Math.trunc(Number(metadata.failure_count) || 0), 0),
    confidence: clampNumber(Number(metadata.confidence) || 0, 0, 1),
    detected_feature_count: Math.max(
      Math.trunc(Number(metadata.detected_feature_count) || 0),
      0
    ),
    inlier_ratio: clampNumber(Number(metadata.inlier_ratio) || 0, 0, 1),
    matched_feature_count: Math.max(Math.trunc(Number(metadata.matched_feature_count) || 0), 0),
    diameter_mm: diameterMm,
    image_center_x_px: centerX,
    image_center_y_px: centerY,
    reason_codes: Array.isArray(metadata.reason_codes)
      ? metadata.reason_codes.map(String).filter(Boolean)
      : []
  };
}

function StemSurfacePhaseCalibrationEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  channelId: string | null;
  onChange: (value: unknown) => void;
}) {
  const { notify } = useNotifications();
  const currentProfile = useMemo(
    () => normalizeStemSurfacePhaseCalibrationProfile(props.value),
    [props.value]
  );
  const processProfile = useMemo(
    () => upstreamStemProcessCalibrationProfile(props.graph, props.node.node_key),
    [props.graph, props.node.node_key]
  );
  const viewProfile = useMemo(
    () => upstreamImageViewProfile(props.graph, props.node.node_key),
    [props.graph, props.node.node_key]
  );
  const phaseImageWidth = viewProfile?.output_width ?? processProfile?.image_width ?? 1;
  const phaseImageHeight = viewProfile?.output_height ?? processProfile?.image_height ?? 1;
  const phaseEstimatorVersion =
    props.node.config.phase_estimator_version === 'CYLINDRICAL_SURFACE_V2'
      ? 'CYLINDRICAL_SURFACE_V2'
      : 'LK_KEYFRAME_RELOCK_V1';
  const currentProfileMatchesBindings = Boolean(
    currentProfile &&
      processProfile &&
      currentProfile.process_calibration_hash === processProfile.calibration_hash &&
      currentProfile.intrinsic_calibration_hash === processProfile.intrinsic_calibration_hash &&
      currentProfile.source_channel_id === processProfile.source_channel_id &&
      currentProfile.rotation_reference_view === processProfile.rotation_reference_view &&
      currentProfile.positive_rotation === processProfile.positive_rotation &&
      (currentProfile.phase_estimator_version ?? 'LK_KEYFRAME_RELOCK_V1') ===
        phaseEstimatorVersion &&
      currentProfile.image_width === phaseImageWidth &&
      currentProfile.image_height === phaseImageHeight
  );
  const [calibrationId, setCalibrationId] = useState(
    currentProfile?.calibration_id ?? 'stem-surface-phase-calibration'
  );
  const [calibrationVersion, setCalibrationVersion] = useState(
    currentProfile?.calibration_version ?? '1'
  );
  const [method, setMethod] = useState<'MARKED_TEST_STEM' | 'REFERENCE_CYLINDER'>(
    currentProfile?.provenance.method ?? 'MARKED_TEST_STEM'
  );
  const [observations, setObservations] = useState<RuntimeStemSurfacePhaseCalibrationObservation[]>(
    currentProfile?.observations ?? []
  );
  const [maximumMeanError, setMaximumMeanError] = useState(
    currentProfile?.maximum_mean_abs_error_deg ?? 3
  );
  const [maximumError, setMaximumError] = useState(
    currentProfile?.maximum_abs_error_limit_deg ?? 7.5
  );
  const [validDiameterMin, setValidDiameterMin] = useState(
    currentProfile?.requested_valid_diameter_min_mm == null
      ? ''
      : String(currentProfile.requested_valid_diameter_min_mm)
  );
  const [validDiameterMax, setValidDiameterMax] = useState(
    currentProfile?.requested_valid_diameter_max_mm == null
      ? ''
      : String(currentProfile.requested_valid_diameter_max_mm)
  );
  const [diameterTolerancePercent, setDiameterTolerancePercent] = useState(
    (currentProfile?.diameter_tolerance_ratio ?? 0.25) * 100
  );
  const [imageCenterToleranceXPercent, setImageCenterToleranceXPercent] = useState(
    (currentProfile?.image_center_tolerance_x_ratio ?? 0.20) * 100
  );
  const [imageCenterToleranceYPercent, setImageCenterToleranceYPercent] = useState(
    (currentProfile?.image_center_tolerance_y_ratio ?? 0.20) * 100
  );
  const [referenceAssetId, setReferenceAssetId] = useState(
    currentProfile?.provenance.reference_asset_id ?? ''
  );
  const [notes, setNotes] = useState(currentProfile?.provenance.notes ?? '');
  const [calculation, setCalculation] =
    useState<RuntimeStemSurfacePhaseCalibrationCalculateResponse | null>(null);
  const [recordedFrames, setRecordedFrames] =
    useState<StemSurfacePhaseCalibrationCaptureFrame[]>([]);
  const [baselineCaptureId, setBaselineCaptureId] = useState<string | null>(null);
  const [captureAngles, setCaptureAngles] = useState<Record<string, string>>({});
  const [recordingDurationSeconds, setRecordingDurationSeconds] = useState(8);
  const [recording, setRecording] = useState(false);
  const [recordingStatus, setRecordingStatus] = useState<string | null>(null);
  const recordingRef = useRef(false);

  useEffect(() => {
    if (!currentProfile) return;
    setCalibrationId(currentProfile.calibration_id);
    setCalibrationVersion(currentProfile.calibration_version);
    setMethod(currentProfile.provenance.method);
    setObservations(currentProfile.observations);
    setMaximumMeanError(currentProfile.maximum_mean_abs_error_deg);
    setMaximumError(currentProfile.maximum_abs_error_limit_deg);
    setValidDiameterMin(
      currentProfile.requested_valid_diameter_min_mm == null
        ? ''
        : String(currentProfile.requested_valid_diameter_min_mm)
    );
    setValidDiameterMax(
      currentProfile.requested_valid_diameter_max_mm == null
        ? ''
        : String(currentProfile.requested_valid_diameter_max_mm)
    );
    setDiameterTolerancePercent((currentProfile.diameter_tolerance_ratio ?? 0.25) * 100);
    setImageCenterToleranceXPercent(
      (currentProfile.image_center_tolerance_x_ratio ?? 0.20) * 100
    );
    setImageCenterToleranceYPercent(
      (currentProfile.image_center_tolerance_y_ratio ?? 0.20) * 100
    );
    setReferenceAssetId(currentProfile.provenance.reference_asset_id ?? '');
    setNotes(currentProfile.provenance.notes);
  }, [currentProfile]);

  useEffect(
    () => () => {
      recordingRef.current = false;
    },
    []
  );

  const baselineCapture = useMemo(
    () => recordedFrames.find((item) => item.capture_id === baselineCaptureId) ?? null,
    [baselineCaptureId, recordedFrames]
  );
  const selectedCaptureFrames = useMemo(
    () =>
      recordedFrames.filter((item) =>
        Object.prototype.hasOwnProperty.call(captureAngles, item.capture_id)
      ),
    [captureAngles, recordedFrames]
  );
  const calibrationObservationIssue = useMemo(() => {
    if (observations.length < 4) return null;
    const rawValues = observations.map((item) => item.raw_phase);
    const rawSpan = Math.max(...rawValues) - Math.min(...rawValues);
    if (!Number.isFinite(rawSpan) || rawSpan <= 1e-6) {
      return 'Keine Rohphasenbewegung vorhanden: Alle Raw-phase-Werte sind gleich. Diese Werte werden automatisch aus der Mantelverfolgung erzeugt und dürfen nicht von Hand ergänzt werden.';
    }
    const angleValues = observations.map((item) => item.known_angle_deg);
    const angleSpan = Math.max(...angleValues) - Math.min(...angleValues);
    const requiredAngleSpan = phaseEstimatorVersion === 'CYLINDRICAL_SURFACE_V2' ? 90 : 30;
    if (!Number.isFinite(angleSpan) || angleSpan < requiredAngleSpan) {
      return `Die bekannten Referenzwinkel müssen zusammen mindestens ${requiredAngleSpan}° abdecken.`;
    }
    return null;
  }, [observations, phaseEstimatorVersion]);
  const calibrationRangeIssue = useMemo(() => {
    const hasMinimum = validDiameterMin.trim() !== '';
    const hasMaximum = validDiameterMax.trim() !== '';
    if (hasMinimum !== hasMaximum) {
      return 'Für einen festen Produktbereich müssen minimaler und maximaler Durchmesser gemeinsam angegeben werden.';
    }
    if (!hasMinimum || !hasMaximum) return null;
    const minimum = finiteNumber(validDiameterMin, 0);
    const maximum = finiteNumber(validDiameterMax, 0);
    if (minimum <= 0 || maximum < minimum) {
      return 'Der Produkt-Durchmesserbereich ist ungültig.';
    }
    if (
      observations.some(
        (item) => item.diameter_mm < minimum || item.diameter_mm > maximum
      )
    ) {
      return 'Der Produkt-Durchmesserbereich muss alle verwendeten Kalibrierframes enthalten.';
    }
    return null;
  }, [observations, validDiameterMax, validDiameterMin]);
  const fitPreview = useMemo(
    () => stemSurfacePhaseCalibrationFitPreview(observations),
    [observations]
  );
  const fitPreviewExceedsLimits = Boolean(
    fitPreview &&
      (fitPreview.mean_abs_error_deg > maximumMeanError ||
        fitPreview.maximum_abs_error_deg > maximumError)
  );

  const startCalibrationRecording = async () => {
    if (!props.channelId) {
      notify({
        title: 'Kalibrieraufnahme nicht möglich',
        description: 'Wähle zuerst den Kamerakanal der Pipeline aus.',
        tone: 'info'
      });
      return;
    }
    recordingRef.current = true;
    setRecording(true);
    setRecordedFrames([]);
    setBaselineCaptureId(null);
    setCaptureAngles({});
    setRecordingStatus(
      'Aufnahme läuft. Der Stamm muss dieselbe Track-ID und eine durchgehende Phasenspur behalten.'
    );
    const captured = new Map<string, StemSurfacePhaseCalibrationCaptureFrame>();
    const stopAt = Date.now() + clampNumber(recordingDurationSeconds, 3, 20) * 1000;
    let failure: unknown = null;
    try {
      while (recordingRef.current && Date.now() < stopAt && captured.size < 80) {
        try {
          const response = await api.channels.liveViewLatest(props.channelId, {
            includeFrame: true,
            maxFrameWidth: 1024
          });
          const capture = stemSurfacePhaseCalibrationCaptureFromLiveState(response.state);
          if (capture && !captured.has(capture.capture_id)) {
            captured.set(capture.capture_id, capture);
            const capturedValues = [...captured.values()];
            setRecordedFrames(capturedValues);
            const measurementCount = capturedValues.filter(
              (item) => item.measurement_eligible
            ).length;
            setRecordingStatus(
              `${captured.size} Einzelbilder erfasst · ${measurementCount} mit gemessener Rohphase · Track ${capture.track_id}`
            );
          }
        } catch (error) {
          failure = error;
          break;
        }
        await new Promise<void>((resolve) => window.setTimeout(resolve, 180));
      }
    } finally {
      recordingRef.current = false;
      setRecording(false);
    }
    if (failure) {
      const detail = describeError(failure, 'Kalibrieraufnahme fehlgeschlagen');
      setRecordingStatus(
        `Aufnahme abgebrochen: ${detail.title}${
          detail.description ? ` · ${detail.description}` : ''
        }`
      );
      return;
    }
    setRecordingStatus(
      captured.size > 0
        ? `${captured.size} Einzelbilder aufgenommen. Als Messframes sind nur INCREMENTAL/RELOCK-Bilder derselben Spur verwendbar.`
        : 'Keine verwendbaren Phasenframes empfangen. Prüfe Track, Raw phase und Phase lock.'
    );
  };

  const stopCalibrationRecording = () => {
    recordingRef.current = false;
  };

  const selectBaselineCapture = (captureId: string) => {
    const candidate = recordedFrames.find((item) => item.capture_id === captureId);
    if (!candidate?.reference_eligible) return;
    const previousBaselineId = baselineCaptureId;
    setBaselineCaptureId(captureId);
    setCaptureAngles((current) => {
      const next = { ...current };
      if (previousBaselineId && previousBaselineId !== captureId) {
        delete next[previousBaselineId];
      }
      next[captureId] = '0';
      return next;
    });
  };

  const toggleCalibrationCapture = (captureId: string) => {
    if (captureId === baselineCaptureId) return;
    setCaptureAngles((current) => {
      if (Object.prototype.hasOwnProperty.call(current, captureId)) {
        const next = { ...current };
        delete next[captureId];
        return next;
      }
      return { ...current, [captureId]: '' };
    });
  };

  const applyCapturedFramesAsObservations = () => {
    if (!baselineCapture) {
      notify({
        title: '0°-Referenz fehlt',
        description: 'Wähle zuerst ein Einzelbild als relative 0°-Ausgangslage.',
        tone: 'info'
      });
      return;
    }
    if (selectedCaptureFrames.length < 4) {
      notify({
        title: 'Zu wenige Messframes',
        description: 'Wähle mindestens vier Einzelbilder einschließlich des 0°-Frames.',
        tone: 'info'
      });
      return;
    }
    if (selectedCaptureFrames.some((item) => item.track_id !== baselineCapture.track_id)) {
      notify({
        title: 'Track-Wechsel in der Auswahl',
        description: 'Alle Kalibrierframes müssen vom selben Stamm und derselben Track-ID stammen.',
        tone: 'error'
      });
      return;
    }
    if (
      selectedCaptureFrames.some(
        (item) => item.trace_generation !== baselineCapture.trace_generation
      )
    ) {
      notify({
        title: 'Unterbrochene Phasenspur',
        description:
          'Alle Kalibrierframes müssen aus derselben durchgehenden Tracking-Spur stammen. Nach INITIALIZING/LOST beginnt eine neue Aufnahme.',
        tone: 'error'
      });
      return;
    }
    const invalidMeasurementFrame = selectedCaptureFrames.find(
      (item) => item.capture_id !== baselineCapture.capture_id && !item.measurement_eligible
    );
    if (invalidMeasurementFrame) {
      notify({
        title: 'Frame enthält keine gemessene Drehung',
        description: `Frame ${invalidMeasurementFrame.frame_sequence} ist ${invalidMeasurementFrame.tracking_mode}. Verwende außer der 0°-Referenz nur INCREMENTAL- oder RELOCK-Frames.`,
        tone: 'error'
      });
      return;
    }
    const missingAngle = selectedCaptureFrames.find(
      (item) => !captureAngles[item.capture_id]?.trim()
    );
    if (missingAngle) {
      notify({
        title: 'Winkel fehlt',
        description: `Trage für Frame ${missingAngle.frame_sequence} den sichtbaren relativen Winkel ein.`,
        tone: 'info'
      });
      return;
    }
    const relativeRawValues = selectedCaptureFrames.map(
      (item) => item.raw_phase - baselineCapture.raw_phase
    );
    const rawSpan = Math.max(...relativeRawValues) - Math.min(...relativeRawValues);
    if (!Number.isFinite(rawSpan) || rawSpan <= 1e-6) {
      notify({
        title: 'Keine Drehbewegung gemessen',
        description:
          'Alle ausgewählten Frames besitzen dieselbe Rohphase. Raw phase wird automatisch aus Mantelmerkmalen berechnet und darf nicht manuell eingetragen werden. Nimm neu auf, bis INCREMENTAL-Frames mit unterschiedlichen raw-Werten erscheinen.',
        tone: 'error'
      });
      return;
    }
    const knownAngles = selectedCaptureFrames.map((item) =>
      finiteNumber(captureAngles[item.capture_id], 0)
    );
    const knownAngleSpan = Math.max(...knownAngles) - Math.min(...knownAngles);
    const requiredAngleSpan = phaseEstimatorVersion === 'CYLINDRICAL_SURFACE_V2' ? 90 : 30;
    if (knownAngleSpan < requiredAngleSpan) {
      notify({
        title: 'Winkelspanne zu klein',
        description:
          `Die sichtbaren Referenzwinkel müssen zusammen mindestens ${requiredAngleSpan}° abdecken. Raw phase bleibt automatisch.`,
        tone: 'info'
      });
      return;
    }
    const nextObservations = [...selectedCaptureFrames]
      .sort((left, right) => left.frame_sequence - right.frame_sequence)
      .map((item, index): RuntimeStemSurfacePhaseCalibrationObservation => ({
        observation_id: `video-${item.frame_sequence}-${index + 1}`,
        raw_phase: item.raw_phase - baselineCapture.raw_phase,
        known_angle_deg: finiteNumber(captureAngles[item.capture_id], 0),
        actual_marking: `Frame ${item.frame_sequence}${item.timestamp ? ` · ${item.timestamp}` : ''}`,
        diameter_mm: item.diameter_mm,
        image_center_x_px: item.image_center_x_px,
        image_center_y_px: item.image_center_y_px,
        weight: Math.max(item.confidence, 0.1)
      }));
    setObservations(nextObservations);
    setCalculation(null);
    notify({
      title: 'Kalibrierframes übernommen',
      description: `${nextObservations.length} Messpaare sind bereit. Prüfe die Winkel und berechne anschließend die Gradkalibrierung.`,
      tone: 'success'
    });
  };

  const mutation = useMutation({
    mutationFn: () => {
      if (!props.channelId) throw new Error('Select the process camera channel first');
      if (!processProfile) {
        throw new Error('Connect a valid Stem process coordinate rectify node first');
      }
      return api.channels.calculateStemSurfacePhaseCalibration(props.channelId, {
        calibration_id: calibrationId.trim(),
        calibration_version: calibrationVersion.trim(),
        image_width: phaseImageWidth,
        image_height: phaseImageHeight,
        intrinsic_calibration_hash: processProfile.intrinsic_calibration_hash,
        process_calibration_hash: processProfile.calibration_hash,
        rotation_reference_view: processProfile.rotation_reference_view,
        positive_rotation: processProfile.positive_rotation,
        phase_estimator_version: phaseEstimatorVersion,
        method,
        observations,
        minimum_angle_span_deg:
          phaseEstimatorVersion === 'CYLINDRICAL_SURFACE_V2' ? 90 : 30,
        maximum_mean_abs_error_deg: maximumMeanError,
        maximum_abs_error_deg: maximumError,
        valid_diameter_min_mm:
          validDiameterMin.trim() === '' ? null : finiteNumber(validDiameterMin, 0),
        valid_diameter_max_mm:
          validDiameterMax.trim() === '' ? null : finiteNumber(validDiameterMax, 0),
        diameter_tolerance_ratio: clampNumber(diameterTolerancePercent / 100, 0, 2),
        image_center_tolerance_x_ratio: clampNumber(
          imageCenterToleranceXPercent / 100,
          0,
          1
        ),
        image_center_tolerance_y_ratio: clampNumber(
          imageCenterToleranceYPercent / 100,
          0,
          1
        ),
        reference_asset_id: referenceAssetId.trim() || null,
        notes
      });
    },
    onSuccess: (response) => {
      setCalculation(response);
      if (response.status === 'valid' && response.profile) {
        props.onChange(response.profile);
      }
      notify({
        title: response.status === 'valid' ? 'Surface phase calibration valid' : 'Surface phase calibration rejected',
        description:
          response.status === 'valid'
            ? `Calculated and applied automatically. Mean error ${formatCalibrationError(response.mean_abs_error_deg, 'deg')}`
            : response.failure_message ?? 'The raw-phase to angle fit is invalid.',
        tone: response.status === 'valid' ? 'success' : 'info'
      });
    },
    onError: (error) =>
      notify({
        title: 'Surface phase calibration failed',
        description: error instanceof Error ? error.message : String(error),
        tone: 'error'
      })
  });

  const updateObservation = (
    index: number,
    patch: Partial<RuntimeStemSurfacePhaseCalibrationObservation>
  ) =>
    setObservations((current) =>
      current.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item))
    );
  return (
    <div className="runtime-structured-table runtime-stem-surface-phase-calibration">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <div className="runtime-warning">
        Commissioning pairs must come from a marked test stem or reference cylinder. Raw phase
        is never interpreted as degrees until this versioned fit is valid and bound to the same
        intrinsic/process calibration, image range, diameter range, viewpoint and rotation sign.
        Raw phase is calculated automatically from tracked bark features; only the known visible
        angle is entered by the operator.
      </div>
      <section className="runtime-phase-calibration-recorder">
        <div className="runtime-phase-calibration-recorder__header">
          <div>
            <strong>1. Kurzen Kalibrierlauf aufnehmen</strong>
            <small>
              Die bestehende kanonische Liveansicht wird im Analysetakt als Einzelbilder erfasst.
              Rohphase, Track, Position, Durchmesser und Qualität werden automatisch zugeordnet.
            </small>
          </div>
          <label>
            <span>Dauer [s]</span>
            <input
              type="number"
              min={3}
              max={20}
              step={1}
              value={recordingDurationSeconds}
              disabled={recording}
              onChange={(event) =>
                setRecordingDurationSeconds(clampNumber(finiteNumber(event.target.value, 8), 3, 20))
              }
            />
          </label>
        </div>
        <div className="actions-row">
          <button
            type="button"
            className="button--primary"
            disabled={recording || !props.channelId}
            onClick={() => void startCalibrationRecording()}
          >
            {recording ? 'Aufnahme läuft …' : 'Live-Kalibrierlauf aufnehmen'}
          </button>
          <button type="button" disabled={!recording} onClick={stopCalibrationRecording}>
            Aufnahme stoppen
          </button>
          <button
            type="button"
            disabled={recording || recordedFrames.length === 0}
            onClick={() => {
              setRecordedFrames([]);
              setBaselineCaptureId(null);
              setCaptureAngles({});
              setRecordingStatus(null);
            }}
          >
            Aufnahme verwerfen
          </button>
        </div>
        {recordingStatus ? (
          <div
            className={
              recordingStatus.startsWith('Aufnahme abgebrochen') ||
              recordingStatus.startsWith('Keine verwendbaren')
                ? 'runtime-warning'
                : 'runtime-success'
            }
          >
            {recordingStatus}
          </div>
        ) : null}
        {recordedFrames.length > 0 ? (
          <>
            <div className="runtime-phase-calibration-recorder__instructions">
              <strong>2. Wenige eindeutige Frames auswählen</strong>
              <span>
                Zuerst ein scharfes Bild als 0°-Referenz setzen. Danach mindestens drei weitere
                Bilder verwenden und nur deren sichtbaren relativen Winkel eintragen. Raw phase
                wird vom Tracker berechnet und niemals von Hand eingetragen. Trage die wirklich
                sichtbare, signierte Markierungsstellung ein – bei einem Zurückrollen wird der
                Winkel wieder kleiner. Bei genau vier Paaren müssen alle innerhalb der
                Fehlergrenzen liegen; ab fünf Paaren kann der robuste Fit Ausreißer verwerfen.
                Sechs oder mehr Frames sind daher besser.
              </span>
            </div>
            <div className="runtime-phase-calibration-frames">
              {recordedFrames.map((item) => {
                const selected = Object.prototype.hasOwnProperty.call(
                  captureAngles,
                  item.capture_id
                );
                const baseline = item.capture_id === baselineCaptureId;
                const trackMismatch = Boolean(
                  baselineCapture && item.track_id !== baselineCapture.track_id
                );
                const traceMismatch = Boolean(
                  baselineCapture &&
                    item.trace_generation !== baselineCapture.trace_generation
                );
                const relativeRaw = baselineCapture
                  ? item.raw_phase - baselineCapture.raw_phase
                  : null;
                return (
                  <article
                    key={item.capture_id}
                    className={`runtime-phase-calibration-frame${
                      selected ? ' runtime-phase-calibration-frame--selected' : ''
                    }${baseline ? ' runtime-phase-calibration-frame--baseline' : ''}`}
                  >
                    <img
                      src={calibrationFrameImageSource(item.frame) ?? undefined}
                      alt={`Kalibrierframe ${item.frame_sequence}`}
                    />
                    <div className="runtime-phase-calibration-frame__metrics">
                      <strong>Frame {item.frame_sequence}</strong>
                      <span>Modus {item.tracking_mode}</span>
                      <span>Track {item.track_id.slice(-20)}</span>
                      <span>raw {item.raw_phase.toFixed(4)}</span>
                      <span>
                        Δraw {relativeRaw === null ? 'n/a' : relativeRaw.toFixed(4)}
                      </span>
                      <span>Confidence {(item.confidence * 100).toFixed(0)} %</span>
                      <span>Inlier {(item.inlier_ratio * 100).toFixed(0)} %</span>
                      <span>{item.matched_feature_count} passende Features</span>
                      <span>{item.detected_feature_count} erkannte Features</span>
                      <span>Spur {item.trace_generation}</span>
                      {item.relock_status !== 'NOT_ATTEMPTED' &&
                      item.relock_status !== 'NOT_REQUIRED' ? (
                        <span>Re-Lock {item.relock_status}</span>
                      ) : null}
                    </div>
                    {item.reason_codes.length ? (
                      <small className="subtle-text">
                        {item.reason_codes.join(' · ')}
                      </small>
                    ) : null}
                    {trackMismatch ? (
                      <small className="runtime-phase-calibration-frame__error">
                        Andere Track-ID
                      </small>
                    ) : null}
                    {traceMismatch ? (
                      <small className="runtime-phase-calibration-frame__error">
                        Andere Tracking-Spur nach Reset/Verlust
                      </small>
                    ) : null}
                    {!item.measurement_eligible && !baseline ? (
                      <small className="runtime-phase-calibration-frame__error">
                        {item.reference_eligible
                          ? 'Nur als 0°-Referenz geeignet; keine Drehung in diesem Frame gemessen'
                          : 'Nicht verwendbar; keine Mantelflächenmerkmale erkannt'}
                      </small>
                    ) : null}
                    <div className="actions-row">
                      <button
                        type="button"
                        disabled={
                          trackMismatch || traceMismatch || !item.reference_eligible
                        }
                        onClick={() => selectBaselineCapture(item.capture_id)}
                      >
                        {baseline ? '0°-Referenz' : 'Als 0° setzen'}
                      </button>
                      <button
                        type="button"
                        disabled={
                          !baselineCapture ||
                          baseline ||
                          trackMismatch ||
                          traceMismatch ||
                          !item.measurement_eligible
                        }
                        onClick={() => toggleCalibrationCapture(item.capture_id)}
                      >
                        {selected ? 'Nicht verwenden' : 'Frame verwenden'}
                      </button>
                    </div>
                    {selected ? (
                      <label>
                        <span>Relativer Winkel [°]</span>
                        <input
                          type="number"
                          step={0.1}
                          value={baseline ? '0' : captureAngles[item.capture_id] ?? ''}
                          disabled={baseline}
                          placeholder="z. B. +30"
                          onChange={(event) =>
                            setCaptureAngles((current) => ({
                              ...current,
                              [item.capture_id]: event.target.value
                            }))
                          }
                        />
                      </label>
                    ) : null}
                  </article>
                );
              })}
            </div>
            <div className="actions-row">
              <button
                type="button"
                className="button--primary"
                disabled={!baselineCapture || selectedCaptureFrames.length < 4}
                onClick={applyCapturedFramesAsObservations}
              >
                {selectedCaptureFrames.length} ausgewählte Frames als Messpaare übernehmen
              </button>
            </div>
          </>
        ) : null}
      </section>
      <div className="runtime-structured-table__row runtime-structured-table__row--grid">
        <label><span>Calibration ID</span><input value={calibrationId} onChange={(event) => setCalibrationId(event.target.value)} /></label>
        <label><span>Version</span><input value={calibrationVersion} onChange={(event) => setCalibrationVersion(event.target.value)} /></label>
        <label>
          <span>Reference object</span>
          <select value={method} onChange={(event) => setMethod(event.target.value as typeof method)}>
            <option value="MARKED_TEST_STEM">Marked test stem</option>
            <option value="REFERENCE_CYLINDER">Reference cylinder</option>
          </select>
        </label>
        <label><span>Reference asset ID</span><input value={referenceAssetId} onChange={(event) => setReferenceAssetId(event.target.value)} /></label>
        <label><span>Maximum mean error [deg]</span><input type="number" min={0.01} step={0.1} value={maximumMeanError} onChange={(event) => setMaximumMeanError(Math.max(finiteNumber(event.target.value, 3), 0.01))} /></label>
        <label><span>Maximum single error [deg]</span><input type="number" min={0.01} step={0.1} value={maximumError} onChange={(event) => setMaximumError(Math.max(finiteNumber(event.target.value, 7.5), 0.01))} /></label>
      </div>
      <details className="runtime-phase-calibration-advanced">
        <summary>Gültigkeitsbereich der Kalibrierung</summary>
        <p className="subtle-text">
          Lege hier den realen Produkt-Durchmesserbereich fest. Leer bedeutet: Aus den
          aufgenommenen Referenzframes wird automatisch eine tolerierte Spanne gebildet.
          Die Positionsmarge erweitert den beobachteten Bildbereich, damit die Kalibrierung
          nicht nur an exakt denselben Pixelkoordinaten gilt.
        </p>
        <div className="runtime-structured-table__row runtime-structured-table__row--grid">
          <label><span>Produktdurchmesser min. [mm]</span><input type="number" min={0.1} step={1} value={validDiameterMin} placeholder="automatisch" onChange={(event) => setValidDiameterMin(event.target.value)} /></label>
          <label><span>Produktdurchmesser max. [mm]</span><input type="number" min={0.1} step={1} value={validDiameterMax} placeholder="automatisch" onChange={(event) => setValidDiameterMax(event.target.value)} /></label>
          <label><span>Automatische Durchmessertoleranz [%]</span><input type="number" min={0} max={200} step={5} value={diameterTolerancePercent} disabled={validDiameterMin.trim() !== '' || validDiameterMax.trim() !== ''} onChange={(event) => setDiameterTolerancePercent(clampNumber(finiteNumber(event.target.value, 25), 0, 200))} /></label>
          <label><span>Positionsmarge X [% Bildbreite]</span><input type="number" min={0} max={100} step={5} value={imageCenterToleranceXPercent} onChange={(event) => setImageCenterToleranceXPercent(clampNumber(finiteNumber(event.target.value, 20), 0, 100))} /></label>
          <label><span>Positionsmarge Y [% Bildhöhe]</span><input type="number" min={0} max={100} step={5} value={imageCenterToleranceYPercent} onChange={(event) => setImageCenterToleranceYPercent(clampNumber(finiteNumber(event.target.value, 20), 0, 100))} /></label>
        </div>
        {currentProfile ? (
          <small className="subtle-text">
            Aktuell angewendet: {currentProfile.valid_diameter_min_mm.toFixed(1)}–{currentProfile.valid_diameter_max_mm.toFixed(1)} mm; Bildmitte X {currentProfile.valid_image_center_x_min_px.toFixed(0)}–{currentProfile.valid_image_center_x_max_px.toFixed(0)} px, Y {currentProfile.valid_image_center_y_min_px.toFixed(0)}–{currentProfile.valid_image_center_y_max_px.toFixed(0)} px.
          </small>
        ) : null}
      </details>
      <details className="runtime-phase-calibration-advanced">
        <summary>Erweiterte Messpaar-Tabelle ({observations.length}; mindestens 4)</summary>
        <div className="runtime-structured-table">
        <strong>Raw phase ↔ known angle pairs</strong>
        <small className="subtle-text">
          Raw phase ist ein dimensionsloser relativer Bildmesswert des Trackers. Er ist hier nur
          zur Diagnose sichtbar und wird nicht manuell vorgegeben.
        </small>
        {observations.map((item, index) => (
          <div key={`${item.observation_id}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--grid">
            <label><span>ID</span><input value={item.observation_id} onChange={(event) => updateObservation(index, { observation_id: event.target.value })} /></label>
            <label><span>Raw phase (automatisch)</span><input type="number" step={0.001} value={item.raw_phase} readOnly /></label>
            <label><span>Known angle [deg]</span><input type="number" step={0.1} value={item.known_angle_deg} onChange={(event) => updateObservation(index, { known_angle_deg: finiteNumber(event.target.value, 0) })} /></label>
            <label><span>Physical marking</span><input value={item.actual_marking ?? ''} onChange={(event) => updateObservation(index, { actual_marking: event.target.value })} /></label>
            <label><span>Diameter [mm]</span><input type="number" min={0.1} step={1} value={item.diameter_mm} onChange={(event) => updateObservation(index, { diameter_mm: Math.max(finiteNumber(event.target.value, 300), 0.1) })} /></label>
            <label><span>Image center X [px]</span><input type="number" min={0} step={1} value={item.image_center_x_px} onChange={(event) => updateObservation(index, { image_center_x_px: Math.max(finiteNumber(event.target.value, 0), 0) })} /></label>
            <label><span>Image center Y [px]</span><input type="number" min={0} step={1} value={item.image_center_y_px} onChange={(event) => updateObservation(index, { image_center_y_px: Math.max(finiteNumber(event.target.value, 0), 0) })} /></label>
            <label><span>Weight</span><input type="number" min={0.01} max={100} step={0.1} value={item.weight} onChange={(event) => updateObservation(index, { weight: clampNumber(finiteNumber(event.target.value, 1), 0.01, 100) })} /></label>
            <label><span>Fit preview [deg]</span><output>{formatCalibrationError(fitPreview?.evaluated[index]?.calculated_phase_deg ?? item.calculated_phase_deg, 'deg')}</output></label>
            <label><span>Preview error [deg]</span><output>{formatCalibrationError(fitPreview?.evaluated[index]?.error_deg ?? item.error_deg, 'deg')}</output></label>
            <button type="button" onClick={() => setObservations((current) => current.filter((_, itemIndex) => itemIndex !== index))}>Remove pair</button>
          </div>
        ))}
        </div>
      </details>
      <label><span>Provenance notes</span><input value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
      {processProfile ? (
        <small className="subtle-text">
          Bound to process profile {processProfile.calibration_id} / {processProfile.calibration_hash.slice(0, 12)} / {phaseImageWidth}×{phaseImageHeight}{viewProfile ? ` canonical view ${viewProfile.view_profile_id}` : ' legacy full view'}; positive {processProfile.positive_rotation.toLowerCase()} from the configured reference view.
        </small>
      ) : (
        <div className="runtime-warning">No unique valid upstream process calibration is connected.</div>
      )}
      {fitPreview ? (
        <div className={fitPreviewExceedsLimits ? 'runtime-warning' : 'runtime-success'}>
          Fit preview: 1 raw = {fitPreview.degrees_per_raw_phase.toFixed(2)}° · mean error{' '}
          {fitPreview.mean_abs_error_deg.toFixed(2)}° · RMS{' '}
          {fitPreview.rms_error_deg.toFixed(2)}° · maximum{' '}
          {fitPreview.maximum_abs_error_deg.toFixed(2)}°.
          {' Größte Abweichung: '}
          {observations[fitPreview.worst_observation_index]?.actual_marking ||
            observations[fitPreview.worst_observation_index]?.observation_id}
          {' – eingegeben '}
          {observations[
            fitPreview.worst_observation_index
          ]?.known_angle_deg.toFixed(2)}°, Fit{' '}
          {fitPreview.evaluated[
            fitPreview.worst_observation_index
          ]?.calculated_phase_deg.toFixed(2)}°.
          {fitPreviewExceedsLimits
            ? ' Prüfe die tatsächlich sichtbaren Winkel, Vorzeichen und eine mögliche Rückdrehung. Höhere Fehlergrenzen verbessern die Messung nicht.'
            : ' Die Messpaare liegen innerhalb der eingestellten Fehlergrenzen.'}
        </div>
      ) : null}
      <div className="actions-row">
        <button type="button" className="button--primary" disabled={!processProfile || observations.length < 4 || Boolean(calibrationObservationIssue) || Boolean(calibrationRangeIssue) || mutation.isPending} onClick={() => mutation.mutate()}>
          {mutation.isPending ? 'Fitting...' : 'Fit and apply degree calibration'}
        </button>
      </div>
      {calibrationObservationIssue ? (
        <div className="runtime-warning">{calibrationObservationIssue}</div>
      ) : null}
      {calibrationRangeIssue ? (
        <div className="runtime-warning">{calibrationRangeIssue}</div>
      ) : null}
      {calculation ? (
        <div className={calculation.status === 'valid' ? 'runtime-success' : 'runtime-warning'}>
          {calculation.status === 'valid'
            ? `Valid fit / mean ${formatCalibrationError(calculation.mean_abs_error_deg, 'deg')} / RMS ${formatCalibrationError(calculation.rms_error_deg, 'deg')} / max ${formatCalibrationError(calculation.maximum_abs_error_deg, 'deg')}`
            : `${calculation.failure_code ?? 'Calibration invalid'}: ${calculation.failure_message ?? ''}${calculation.mean_abs_error_deg !== null && calculation.mean_abs_error_deg !== undefined ? ` · mean ${formatCalibrationError(calculation.mean_abs_error_deg, 'deg')}` : ''}${calculation.maximum_abs_error_deg !== null && calculation.maximum_abs_error_deg !== undefined ? ` · max ${formatCalibrationError(calculation.maximum_abs_error_deg, 'deg')}` : ''}`}
        </div>
      ) : null}
      {currentProfile ? (
        <div className={currentProfileMatchesBindings ? 'runtime-success' : 'runtime-warning'}>
          Applied phase calibration {currentProfile.calibration_id} v{currentProfile.calibration_version}, hash {currentProfile.calibration_hash.slice(0, 12)}.{' '}
          Estimator {currentProfile.phase_estimator_version ?? 'LK_KEYFRAME_RELOCK_V1'}.{' '}
          {currentProfileMatchesBindings
            ? 'It matches the current upstream process calibration.'
            : `It belongs to an older process calibration (${currentProfile.process_calibration_hash.slice(0, 12)}). Re-fit and apply it once for the current process profile${processProfile ? ` (${processProfile.calibration_hash.slice(0, 12)})` : ''}; the existing angle observations remain available.`}
        </div>
      ) : (
        <div className="runtime-warning">No valid degree calibration is applied. Runtime raw phase remains available; degree fields stay empty. The pipeline draft can still be saved; only a degree result remains unavailable.</div>
      )}
    </div>
  );
}

const defaultStemMinimumPrincipleValidationThresholds: RuntimeStemMinimumPrincipleValidationThresholds = {
  minimum_total_observations: 9,
  minimum_observations_per_setup: 2,
  minimum_valid_angle_observations: 6,
  maximum_median_angle_error_deg: 5,
  maximum_p95_angle_error_deg: 10,
  maximum_sign_error_count: 0,
  maximum_target_phase_repeatability_deg: 5,
  maximum_homography_error_mm: 5,
  maximum_straight_curvature_mm: 2,
  maximum_translation_phase_error_deg: 3,
  maximum_curvature_abs_error_mm: 2,
  maximum_curvature_repeatability_mm: 2,
  minimum_relock_success_rate: 0.8,
  maximum_uncertainty_rate: 0.2
};

function StemMinimumPrincipleValidationEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  channelId: string | null;
  onChange: (value: unknown) => void;
}) {
  const { notify } = useNotifications();
  const currentArtifact = useMemo(
    () => normalizeStemMinimumPrincipleValidationArtifact(props.value),
    [props.value]
  );
  const processProfile = useMemo(
    () => upstreamStemProcessCalibrationProfile(props.graph, props.node.node_key),
    [props.graph, props.node.node_key]
  );
  const viewProfile = useMemo(
    () => upstreamImageViewProfile(props.graph, props.node.node_key),
    [props.graph, props.node.node_key]
  );
  const validationImageWidth = viewProfile?.output_width ?? processProfile?.image_width ?? 1;
  const validationImageHeight = viewProfile?.output_height ?? processProfile?.image_height ?? 1;
  const phaseProfile = useMemo(
    () => upstreamStemSurfacePhaseCalibrationProfile(props.graph, props.node.node_key),
    [props.graph, props.node.node_key]
  );
  const [artifactId, setArtifactId] = useState(
    currentArtifact?.artifact_id ?? 'stem-minimum-principle-validation'
  );
  const [artifactVersion, setArtifactVersion] = useState(
    currentArtifact?.artifact_version ?? '1'
  );
  const [thresholds, setThresholds] = useState<RuntimeStemMinimumPrincipleValidationThresholds>(
    currentArtifact?.thresholds ?? defaultStemMinimumPrincipleValidationThresholds
  );
  const [observations, setObservations] = useState<RuntimeStemMinimumPrincipleValidationObservation[]>(
    currentArtifact?.observations.map(stemMinimumPrincipleObservationInput) ?? []
  );
  const [referenceAssetIds, setReferenceAssetIds] = useState(
    currentArtifact?.provenance.reference_asset_ids.join(', ') ?? ''
  );
  const [notes, setNotes] = useState(currentArtifact?.provenance.notes ?? '');
  const [calculation, setCalculation] =
    useState<RuntimeStemMinimumPrincipleValidationCalculateResponse | null>(null);

  useEffect(() => {
    if (!currentArtifact) return;
    setArtifactId(currentArtifact.artifact_id);
    setArtifactVersion(currentArtifact.artifact_version);
    setThresholds(currentArtifact.thresholds);
    setObservations(currentArtifact.observations.map(stemMinimumPrincipleObservationInput));
    setReferenceAssetIds(currentArtifact.provenance.reference_asset_ids.join(', '));
    setNotes(currentArtifact.provenance.notes);
  }, [currentArtifact]);

  const bindingsMatch = Boolean(
    processProfile &&
      phaseProfile &&
      phaseProfile.process_calibration_hash === processProfile.calibration_hash &&
      phaseProfile.intrinsic_calibration_hash === processProfile.intrinsic_calibration_hash &&
      phaseProfile.image_width === validationImageWidth &&
      phaseProfile.image_height === validationImageHeight &&
      phaseProfile.source_channel_id === processProfile.source_channel_id &&
      (!props.channelId || processProfile.source_channel_id === props.channelId)
  );

  const mutation = useMutation({
    mutationFn: () => {
      if (!props.channelId) throw new Error('Select the process camera channel first');
      if (!processProfile || !phaseProfile || !bindingsMatch) {
        throw new Error('Connect matching valid process and surface-phase calibrations first');
      }
      return api.channels.calculateStemMinimumPrincipleValidation(props.channelId, {
        artifact_id: artifactId.trim(),
        artifact_version: artifactVersion.trim(),
        image_width: validationImageWidth,
        image_height: validationImageHeight,
        process_calibration_id: processProfile.calibration_id,
        process_calibration_hash: processProfile.calibration_hash,
        phase_calibration_id: phaseProfile.calibration_id,
        phase_calibration_version: phaseProfile.calibration_version,
        phase_calibration_hash: phaseProfile.calibration_hash,
        thresholds_confirmed: true,
        thresholds,
        observations,
        reference_asset_ids: normalizeRuleLabels(referenceAssetIds),
        notes
      });
    },
    onSuccess: (response) => {
      setCalculation(response);
      props.onChange(response.artifact);
      notify({
        title: minimumPrincipleValidationStatusLabel(response.status),
        description: `${response.artifact.metrics.total_observation_count} objective observations; the versioned result was applied automatically, hash ${response.artifact.artifact_hash.slice(0, 12)}`,
        tone: response.status === 'VALIDATION_PASSED' ? 'success' : 'info'
      });
    },
    onError: (error) =>
      notify({
        title: 'Objective validation failed to calculate',
        description: error instanceof Error ? error.message : String(error),
        tone: 'error'
      })
  });

  const updateThreshold = (
    key: keyof RuntimeStemMinimumPrincipleValidationThresholds,
    value: number
  ) => {
    setThresholds((current) => ({ ...current, [key]: value }));
    setCalculation(null);
  };
  const updateObservation = (
    index: number,
    patch: Partial<RuntimeStemMinimumPrincipleValidationObservation>
  ) => {
    setObservations((current) =>
      current.map((item, itemIndex) => (itemIndex === index ? { ...item, ...patch } : item))
    );
    setCalculation(null);
  };
  const addObservation = (
    referenceSetup: RuntimeStemMinimumPrincipleValidationObservation['reference_setup']
  ) => {
    const index = observations.length + 1;
    const setupPrefix = {
      STRAIGHT_REFERENCE_CYLINDER: 'cylinder',
      CURVED_REFERENCE_BODY: 'curved-body',
      MARKED_TEST_STEM: 'test-stem'
    }[referenceSetup];
    setObservations((current) => [
      ...current,
      {
        observation_id: `${setupPrefix}-${index}`,
        reference_setup: referenceSetup,
        reference_body_id: `${setupPrefix}-1`,
        run_id: `run-${index}`,
        actual_marking: `mark-${index}`,
        known_angle_deg: null,
        calculated_phase_deg: null,
        expected_target_phase_deg: null,
        calculated_target_phase_deg: null,
        expected_correction_deg: null,
        calculated_correction_deg: null,
        decision: 'UNCERTAIN',
        observed_phase_span_deg: 0,
        diameter_mm: 300,
        diameter_class: 'commissioning-default',
        image_center_x_px: validationImageWidth / 2,
        image_center_y_px: validationImageHeight / 2,
        known_x_mm: null,
        known_y_mm: null,
        measured_x_mm: null,
        measured_y_mm: null,
        expected_curvature_min_mm: referenceSetup === 'STRAIGHT_REFERENCE_CYLINDER' ? 0 : null,
        expected_curvature_max_mm: referenceSetup === 'STRAIGHT_REFERENCE_CYLINDER' ? 0 : null,
        measured_curvature_min_mm: null,
        measured_curvature_max_mm: null,
        translation_distance_mm: 0,
        relock_attempted: false,
        relock_succeeded: false,
        confidence: 0,
        reason_codes: []
      }
    ]);
    setCalculation(null);
  };
  const visibleArtifact = calculation?.artifact ?? currentArtifact;

  return (
    <div className="runtime-structured-table runtime-stem-minimum-principle-validation">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <div className="runtime-warning">
        Acceptance is calculated only from known reference angles, markings, X/Y positions and
        reference-body curvature. No target frame or subjective orientation label is captured.
        Missing evidence produces INSUFFICIENT_DATA; failed limits remain visible and versionable.
      </div>
      <div className="runtime-structured-table__row runtime-structured-table__row--grid">
        <label><span>Artifact ID</span><input value={artifactId} onChange={(event) => setArtifactId(event.target.value)} /></label>
        <label><span>Version</span><input value={artifactVersion} onChange={(event) => setArtifactVersion(event.target.value)} /></label>
        <label><span>Reference asset IDs</span><input value={referenceAssetIds} onChange={(event) => setReferenceAssetIds(event.target.value)} placeholder="cylinder-cert-1, curved-body-cert-1" /></label>
        <label><span>Provenance notes</span><input value={notes} onChange={(event) => setNotes(event.target.value)} /></label>
      </div>

      {bindingsMatch && processProfile && phaseProfile ? (
        <small className="subtle-text">
          Bound to channel {processProfile.source_channel_id}, process {processProfile.calibration_id}
          {' / '}{processProfile.calibration_hash.slice(0, 12)}, phase {phaseProfile.calibration_id}
          {' v'}{phaseProfile.calibration_version} / {phaseProfile.calibration_hash.slice(0, 12)},
          {' '}{validationImageWidth}×{validationImageHeight}{viewProfile ? ` / view ${viewProfile.view_profile_id}` : ''}.
        </small>
      ) : (
        <div className="runtime-warning">
          No unique, matching process and surface-phase calibration is connected upstream.
        </div>
      )}

      <details>
        <summary>Objective acceptance thresholds</summary>
        <div className="runtime-structured-table__row runtime-structured-table__row--grid">
          <ValidationThresholdInput label="Minimum total observations" value={thresholds.minimum_total_observations} minimum={3} integer onChange={(value) => updateThreshold('minimum_total_observations', value)} />
          <ValidationThresholdInput label="Minimum per setup" value={thresholds.minimum_observations_per_setup} minimum={1} integer onChange={(value) => updateThreshold('minimum_observations_per_setup', value)} />
          <ValidationThresholdInput label="Minimum valid angles" value={thresholds.minimum_valid_angle_observations} minimum={1} integer onChange={(value) => updateThreshold('minimum_valid_angle_observations', value)} />
          <ValidationThresholdInput label="Maximum median angle [deg]" value={thresholds.maximum_median_angle_error_deg} minimum={0.01} onChange={(value) => updateThreshold('maximum_median_angle_error_deg', value)} />
          <ValidationThresholdInput label="Maximum P95 angle [deg]" value={thresholds.maximum_p95_angle_error_deg} minimum={0.01} onChange={(value) => updateThreshold('maximum_p95_angle_error_deg', value)} />
          <ValidationThresholdInput label="Maximum sign errors" value={thresholds.maximum_sign_error_count} integer onChange={(value) => updateThreshold('maximum_sign_error_count', value)} />
          <ValidationThresholdInput label="Maximum target repeatability [deg]" value={thresholds.maximum_target_phase_repeatability_deg} minimum={0.01} onChange={(value) => updateThreshold('maximum_target_phase_repeatability_deg', value)} />
          <ValidationThresholdInput label="Maximum homography error [mm]" value={thresholds.maximum_homography_error_mm} minimum={0.01} onChange={(value) => updateThreshold('maximum_homography_error_mm', value)} />
          <ValidationThresholdInput label="Maximum straight curvature [mm]" value={thresholds.maximum_straight_curvature_mm} minimum={0.01} onChange={(value) => updateThreshold('maximum_straight_curvature_mm', value)} />
          <ValidationThresholdInput label="Maximum translation phase error [deg]" value={thresholds.maximum_translation_phase_error_deg} minimum={0.01} onChange={(value) => updateThreshold('maximum_translation_phase_error_deg', value)} />
          <ValidationThresholdInput label="Maximum curvature error [mm]" value={thresholds.maximum_curvature_abs_error_mm} minimum={0.01} onChange={(value) => updateThreshold('maximum_curvature_abs_error_mm', value)} />
          <ValidationThresholdInput label="Maximum curvature repeatability [mm]" value={thresholds.maximum_curvature_repeatability_mm} minimum={0.01} onChange={(value) => updateThreshold('maximum_curvature_repeatability_mm', value)} />
          <ValidationThresholdInput label="Minimum Re-Lock success [0..1]" value={thresholds.minimum_relock_success_rate} maximum={1} step={0.01} onChange={(value) => updateThreshold('minimum_relock_success_rate', value)} />
          <ValidationThresholdInput label="Maximum uncertainty [0..1]" value={thresholds.maximum_uncertainty_rate} maximum={1} step={0.01} onChange={(value) => updateThreshold('maximum_uncertainty_rate', value)} />
        </div>
        <small className="subtle-text">
          These limits are stored in the generated artifact. Changing a value creates a new
          objectively reproducible result; no separate confirmation step is required.
        </small>
      </details>

      <div className="actions-row">
        <button type="button" onClick={() => addObservation('STRAIGHT_REFERENCE_CYLINDER')}>Add straight cylinder run</button>
        <button type="button" onClick={() => addObservation('CURVED_REFERENCE_BODY')}>Add curved reference run</button>
        <button type="button" onClick={() => addObservation('MARKED_TEST_STEM')}>Add marked test-stem run</button>
      </div>
      <div className="runtime-structured-table">
        <strong>Objective runs ({observations.length})</strong>
        {observations.map((item, index) => (
          <details key={`${item.observation_id}-${index}`}>
            <summary>{item.reference_setup} / {item.observation_id} / {item.run_id}</summary>
            <div className="runtime-structured-table__row runtime-structured-table__row--grid">
              <label><span>Observation ID</span><input value={item.observation_id} onChange={(event) => updateObservation(index, { observation_id: event.target.value })} /></label>
              <label><span>Reference setup</span><select value={item.reference_setup} onChange={(event) => updateObservation(index, { reference_setup: event.target.value as typeof item.reference_setup })}><option value="STRAIGHT_REFERENCE_CYLINDER">Straight reference cylinder</option><option value="CURVED_REFERENCE_BODY">Curved reference body</option><option value="MARKED_TEST_STEM">Marked test stem</option></select></label>
              <label><span>Reference body ID</span><input value={item.reference_body_id} onChange={(event) => updateObservation(index, { reference_body_id: event.target.value })} /></label>
              <label><span>Run ID</span><input value={item.run_id} onChange={(event) => updateObservation(index, { run_id: event.target.value })} /></label>
              <label><span>Actual marking</span><input value={item.actual_marking} onChange={(event) => updateObservation(index, { actual_marking: event.target.value })} /></label>
              <ValidationObservationNumber label="Known angle [deg]" value={item.known_angle_deg} nullable onChange={(value) => updateObservation(index, { known_angle_deg: value })} />
              <ValidationObservationNumber label="Calculated phase [deg]" value={item.calculated_phase_deg} nullable onChange={(value) => updateObservation(index, { calculated_phase_deg: value })} />
              <ValidationObservationNumber label="Expected target phase [deg]" value={item.expected_target_phase_deg} nullable onChange={(value) => updateObservation(index, { expected_target_phase_deg: value })} />
              <ValidationObservationNumber label="Calculated target phase [deg]" value={item.calculated_target_phase_deg} nullable onChange={(value) => updateObservation(index, { calculated_target_phase_deg: value })} />
              <ValidationObservationNumber label="Expected correction [deg]" value={item.expected_correction_deg} nullable minimum={-90} maximum={90} onChange={(value) => updateObservation(index, { expected_correction_deg: value })} />
              <ValidationObservationNumber label="Calculated correction [deg]" value={item.calculated_correction_deg} nullable minimum={-90} maximum={90} onChange={(value) => updateObservation(index, { calculated_correction_deg: value })} />
              <label><span>Decision</span><select value={item.decision} onChange={(event) => updateObservation(index, { decision: event.target.value as typeof item.decision })}><option value="ROTATE">ROTATE</option><option value="NO_ROTATION">NO_ROTATION</option><option value="UNCERTAIN">UNCERTAIN</option></select></label>
              <ValidationObservationNumber label="Observed phase span [deg]" value={item.observed_phase_span_deg} minimum={0} maximum={720} onChange={(value) => updateObservation(index, { observed_phase_span_deg: value ?? 0 })} />
              <ValidationObservationNumber label="Diameter [mm]" value={item.diameter_mm} minimum={0.1} onChange={(value) => updateObservation(index, { diameter_mm: value ?? 0.1 })} />
              <label><span>Diameter class</span><input value={item.diameter_class} onChange={(event) => updateObservation(index, { diameter_class: event.target.value })} /></label>
              <ValidationObservationNumber label="Image center X [px]" value={item.image_center_x_px} minimum={0} maximum={validationImageWidth - 0.001} onChange={(value) => updateObservation(index, { image_center_x_px: value ?? 0 })} />
              <ValidationObservationNumber label="Image center Y [px]" value={item.image_center_y_px} minimum={0} maximum={validationImageHeight - 0.001} onChange={(value) => updateObservation(index, { image_center_y_px: value ?? 0 })} />
              <ValidationObservationNumber label="Known X [mm]" value={item.known_x_mm} nullable onChange={(value) => updateObservation(index, { known_x_mm: value })} />
              <ValidationObservationNumber label="Known Y [mm]" value={item.known_y_mm} nullable onChange={(value) => updateObservation(index, { known_y_mm: value })} />
              <ValidationObservationNumber label="Measured X [mm]" value={item.measured_x_mm} nullable onChange={(value) => updateObservation(index, { measured_x_mm: value })} />
              <ValidationObservationNumber label="Measured Y [mm]" value={item.measured_y_mm} nullable onChange={(value) => updateObservation(index, { measured_y_mm: value })} />
              <ValidationObservationNumber label="Expected minimum curvature [mm]" value={item.expected_curvature_min_mm} nullable minimum={0} onChange={(value) => updateObservation(index, { expected_curvature_min_mm: value })} />
              <ValidationObservationNumber label="Measured minimum curvature [mm]" value={item.measured_curvature_min_mm} nullable minimum={0} onChange={(value) => updateObservation(index, { measured_curvature_min_mm: value })} />
              <ValidationObservationNumber label="Expected maximum curvature [mm]" value={item.expected_curvature_max_mm} nullable minimum={0} onChange={(value) => updateObservation(index, { expected_curvature_max_mm: value })} />
              <ValidationObservationNumber label="Measured maximum curvature [mm]" value={item.measured_curvature_max_mm} nullable minimum={0} onChange={(value) => updateObservation(index, { measured_curvature_max_mm: value })} />
              <ValidationObservationNumber label="Translation [mm]" value={item.translation_distance_mm} minimum={0} onChange={(value) => updateObservation(index, { translation_distance_mm: value ?? 0 })} />
              <ValidationObservationNumber label="Confidence [0..1]" value={item.confidence} minimum={0} maximum={1} step={0.01} onChange={(value) => updateObservation(index, { confidence: value ?? 0 })} />
              <label><span>Reason codes</span><input value={item.reason_codes.join(', ')} onChange={(event) => updateObservation(index, { reason_codes: normalizeRuleLabels(event.target.value) })} /></label>
              <label className="checkbox-row"><input type="checkbox" checked={item.relock_attempted} onChange={(event) => updateObservation(index, { relock_attempted: event.target.checked, relock_succeeded: event.target.checked ? item.relock_succeeded : false })} /><span>Re-Lock attempted</span></label>
              <label className="checkbox-row"><input type="checkbox" checked={item.relock_succeeded} disabled={!item.relock_attempted} onChange={(event) => updateObservation(index, { relock_succeeded: event.target.checked })} /><span>Re-Lock succeeded</span></label>
              <button type="button" onClick={() => { setObservations((current) => current.filter((_, itemIndex) => itemIndex !== index)); setCalculation(null); }}>Remove run</button>
            </div>
          </details>
        ))}
      </div>

      <div className="actions-row">
        <button type="button" className="button--primary" disabled={!bindingsMatch || mutation.isPending} onClick={() => mutation.mutate()}>
          {mutation.isPending ? 'Calculating...' : 'Calculate objective acceptance'}
        </button>
      </div>

      {visibleArtifact ? (
        <StemMinimumPrincipleValidationResult
          artifact={visibleArtifact}
          reportJson={calculation?.report_json ?? visibleArtifact}
          onApply={null}
        />
      ) : null}
    </div>
  );
}

function ValidationThresholdInput(props: {
  label: string;
  value: number;
  integer?: boolean;
  minimum?: number;
  maximum?: number;
  step?: number;
  onChange: (value: number) => void;
}) {
  return (
    <label>
      <span>{props.label}</span>
      <input
        type="number"
        min={props.minimum ?? 0}
        max={props.maximum}
        step={props.step ?? (props.integer ? 1 : 0.1)}
        value={props.value}
        onChange={(event) =>
          props.onChange(
            clampNumber(
              props.integer
                ? Math.round(finiteNumber(event.target.value, props.value))
                : finiteNumber(event.target.value, props.value),
              props.minimum ?? 0,
              props.maximum ?? Number.MAX_SAFE_INTEGER
            )
          )
        }
      />
    </label>
  );
}

function ValidationObservationNumber(props: {
  label: string;
  value: number | null | undefined;
  nullable?: boolean;
  minimum?: number;
  maximum?: number;
  step?: number;
  onChange: (value: number | null) => void;
}) {
  return (
    <label>
      <span>{props.label}</span>
      <input
        type="number"
        min={props.minimum}
        max={props.maximum}
        step={props.step ?? 0.1}
        value={props.value ?? ''}
        onChange={(event) => {
          if (props.nullable && event.target.value.trim() === '') {
            props.onChange(null);
            return;
          }
          const parsed = finiteNumber(event.target.value, props.value ?? props.minimum ?? 0);
          props.onChange(
            clampNumber(
              parsed,
              props.minimum ?? -Number.MAX_SAFE_INTEGER,
              props.maximum ?? Number.MAX_SAFE_INTEGER
            )
          );
        }}
      />
    </label>
  );
}

function StemMinimumPrincipleValidationResult(props: {
  artifact: RuntimeStemMinimumPrincipleValidationArtifact;
  reportJson: unknown;
  onApply: (() => void) | null;
}) {
  const statusClass =
    props.artifact.status === 'VALIDATION_PASSED' ? 'runtime-success' : 'runtime-warning';
  return (
    <div className={statusClass}>
      <strong>{minimumPrincipleValidationStatusLabel(props.artifact.status)}</strong>
      <div className="metric-row">
        <MetricPill label="Median angle" value={formatValidationMetric(props.artifact.metrics.median_angle_error_deg, '°')} />
        <MetricPill label="P90 angle" value={formatValidationMetric(props.artifact.metrics.p90_angle_error_deg, '°')} />
        <MetricPill label="P95 angle" value={formatValidationMetric(props.artifact.metrics.p95_angle_error_deg, '°')} />
        <MetricPill label="Sign errors" value={String(props.artifact.metrics.sign_error_count)} />
        <MetricPill label="Re-Lock" value={formatValidationPercent(props.artifact.metrics.relock_success_rate)} />
        <MetricPill label="Uncertain" value={formatValidationPercent(props.artifact.metrics.uncertainty_rate)} />
      </div>
      <div className="runtime-structured-table">
        {props.artifact.checks.map((check) => (
          <div key={check.check_id} className="runtime-structured-table__row">
            <strong>{check.status}</strong>
            <span>{check.message}</span>
            <span>{check.value ?? 'n/a'} {check.comparison} {check.limit ?? 'evidence'}</span>
          </div>
        ))}
      </div>
      <div className="actions-row">
        {props.onApply ? <button type="button" className="button--primary" onClick={props.onApply}>Apply versioned artifact to pipeline</button> : null}
        <button type="button" onClick={() => downloadTextReport(`${props.artifact.artifact_id}-${props.artifact.artifact_version}.json`, JSON.stringify(props.reportJson, null, 2), 'application/json')}>Download JSON</button>
        <button type="button" onClick={() => downloadTextReport(`${props.artifact.artifact_id}-${props.artifact.artifact_version}.md`, props.artifact.report_markdown, 'text/markdown')}>Download Markdown</button>
        <button type="button" onClick={() => downloadTextReport(`${props.artifact.artifact_id}-${props.artifact.artifact_version}.html`, props.artifact.report_html, 'text/html')}>Download HTML</button>
      </div>
      <details><summary>Markdown report</summary><pre>{props.artifact.report_markdown}</pre></details>
      <small className="subtle-text">Artifact hash {props.artifact.artifact_hash}; saving the pipeline creates an immutable version.</small>
    </div>
  );
}

function StemProcessCalibrationCanvas(props: {
  frame: RuntimeLiveFrame;
  activeGeometry: StemProcessGeometryKey;
  referencePoints: RuntimeStemCurvatureProcessReferencePoint[];
  coordinateFrameDraft: StemCoordinateFrameDraft | null;
  polygons: StemProcessPolygonDrafts;
  gates: StemProcessGateDrafts;
  profile: RuntimeStemCurvatureProcessCalibrationProfile | null;
  onPoint: (point: RuntimeStemCurvatureImagePoint) => void;
}) {
  const width = props.frame.width ?? 1;
  const height = props.frame.height ?? 1;
  const axes = props.profile ? stemProcessAxesInImage(props.profile) : null;
  const addPoint = (event: ReactPointerEvent<HTMLDivElement>) => {
    const rect = event.currentTarget.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    props.onPoint({
      x_px: clampNumber(((event.clientX - rect.left) / rect.width) * width, 0, width - 1),
      y_px: clampNumber(((event.clientY - rect.top) / rect.height) * height, 0, height - 1)
    });
  };
  const polygonColors: Record<keyof StemProcessPolygonDrafts, string> = {
    candidate_roi: '#f4b740',
    curvature_observation_roi: '#5db7ff',
    surface_tracking_roi: '#7bd389'
  };
  const gateColors: Record<keyof StemProcessGateDrafts, string> = {
    session_start_gate: '#f4b740',
    curvature_freeze_gate: '#ff8c42',
    decision_gate: '#ef476f'
  };
  return (
    <div
      className="runtime-live-editor__canvas"
      style={{ ...previewMediaStyle(width, height), position: 'relative', cursor: 'crosshair' }}
      onPointerDown={addPoint}
      aria-label={`Stem process calibration canvas: ${props.activeGeometry}`}
    >
      <img
        src={calibrationFrameImageSource(props.frame) ?? undefined}
        alt="Intrinsically undistorted stem process calibration frame"
        draggable={false}
        style={{ display: 'block', width: '100%', height: '100%', objectFit: 'fill' }}
      />
      <svg
        viewBox={`0 0 ${width} ${height}`}
        style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
      >
        {Object.entries(props.polygons).map(([key, points]) => (
          <polygon
            key={key}
            points={points.map((point) => `${point.x_px},${point.y_px}`).join(' ')}
            fill={`${polygonColors[key as keyof StemProcessPolygonDrafts]}26`}
            stroke={polygonColors[key as keyof StemProcessPolygonDrafts]}
            strokeWidth={2}
          />
        ))}
        {Object.entries(props.gates).map(([key, points]) =>
          points.length === 2 ? (
            <line
              key={key}
              x1={points[0].x_px}
              y1={points[0].y_px}
              x2={points[1].x_px}
              y2={points[1].y_px}
              stroke={gateColors[key as keyof StemProcessGateDrafts]}
              strokeWidth={3}
            />
          ) : null
        )}
        {props.referencePoints.map((point, index) => (
          <g key={`${point.point_id}-${index}`}>
            <circle cx={point.image_point.x_px} cy={point.image_point.y_px} r={5} fill="#ffffff" stroke="#111827" strokeWidth={2} />
            <text x={point.image_point.x_px + 7} y={point.image_point.y_px - 7} fill="#ffffff" stroke="#111827" strokeWidth={0.5} fontSize={12}>
              {point.point_id === 'axis-origin'
                ? 'O (0/0)'
                : point.point_id === 'axis-x-end'
                  ? `+X (${point.process_point.x_mm} mm)`
                  : point.point_id === 'axis-y-end'
                    ? `+Y (${point.process_point.y_mm} mm)`
                    : point.point_id === 'axis-xy-corner'
                      ? 'XY corner'
                      : `${index + 1}: ${point.process_point.x_mm}/${point.process_point.y_mm}`}
            </text>
          </g>
        ))}
        {props.coordinateFrameDraft?.origin_image_point ? (
          <>
            {props.coordinateFrameDraft.x_axis_image_endpoint ? (
              <line
                x1={props.coordinateFrameDraft.origin_image_point.x_px}
                y1={props.coordinateFrameDraft.origin_image_point.y_px}
                x2={props.coordinateFrameDraft.x_axis_image_endpoint.x_px}
                y2={props.coordinateFrameDraft.x_axis_image_endpoint.y_px}
                stroke="#ff4d4d"
                strokeWidth={4}
              />
            ) : null}
            {props.coordinateFrameDraft.y_axis_image_endpoint ? (
              <line
                x1={props.coordinateFrameDraft.origin_image_point.x_px}
                y1={props.coordinateFrameDraft.origin_image_point.y_px}
                x2={props.coordinateFrameDraft.y_axis_image_endpoint.x_px}
                y2={props.coordinateFrameDraft.y_axis_image_endpoint.y_px}
                stroke="#4dff88"
                strokeWidth={4}
              />
            ) : null}
            {props.coordinateFrameDraft.z_axis_image_endpoint ? (
              <>
                <line
                  x1={props.coordinateFrameDraft.origin_image_point.x_px}
                  y1={props.coordinateFrameDraft.origin_image_point.y_px}
                  x2={props.coordinateFrameDraft.z_axis_image_endpoint.x_px}
                  y2={props.coordinateFrameDraft.z_axis_image_endpoint.y_px}
                  stroke="#5db7ff"
                  strokeWidth={4}
                  strokeDasharray="8 5"
                />
                <text
                  x={props.coordinateFrameDraft.z_axis_image_endpoint.x_px + 7}
                  y={props.coordinateFrameDraft.z_axis_image_endpoint.y_px - 7}
                  fill="#5db7ff"
                  fontSize={16}
                >
                  +Z (vertical)
                </text>
              </>
            ) : null}
            {props.coordinateFrameDraft.xy_corner_image_point &&
            props.coordinateFrameDraft.x_axis_image_endpoint &&
            props.coordinateFrameDraft.y_axis_image_endpoint ? (
              <polygon
                points={[
                  props.coordinateFrameDraft.origin_image_point,
                  props.coordinateFrameDraft.x_axis_image_endpoint,
                  props.coordinateFrameDraft.xy_corner_image_point,
                  props.coordinateFrameDraft.y_axis_image_endpoint
                ]
                  .map((point) => `${point.x_px},${point.y_px}`)
                  .join(' ')}
                fill="none"
                stroke="#ffffff"
                strokeWidth={2}
                strokeDasharray="6 4"
              />
            ) : null}
          </>
        ) : null}
        {axes ? (
          <>
            <line x1={axes.origin.x_px} y1={axes.origin.y_px} x2={axes.xEnd.x_px} y2={axes.xEnd.y_px} stroke="#ff4d4d" strokeWidth={4} />
            <line x1={axes.origin.x_px} y1={axes.origin.y_px} x2={axes.yEnd.x_px} y2={axes.yEnd.y_px} stroke="#4dff88" strokeWidth={4} />
            <text x={axes.xEnd.x_px} y={axes.xEnd.y_px} fill="#ff4d4d" fontSize={16}>+X</text>
            <text x={axes.yEnd.x_px} y={axes.yEnd.y_px} fill="#4dff88" fontSize={16}>+Y</text>
          </>
        ) : null}
      </svg>
    </div>
  );
}

function BoardSurfaceCalibrationEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  node: RuntimeNodeSpec;
  liveState: RuntimeLiveViewState | null;
  captureAvailable: boolean;
  capturePending: boolean;
  onCapture: () => void;
  onChange: (value: unknown) => void;
}) {
  const roles = boardSurfaceRoles(props.node);
  const existing = normalizeBoardSurfaceCalibrations(props.value);
  const surfaces = roles.map(
    (role) => existing.find((surface) => surface.surface_role === role) ?? blankBoardSurface(role, props.node)
  );
  const [activeRole, setActiveRole] = useState<BoardSurfaceCalibration['surface_role']>(roles[0]);
  const imageSrc = liveFrameImageSource(props.liveState);
  const frame = props.liveState?.frame ?? null;
  const cameraChannelActive = [
    'starting',
    'validating',
    'loading_pipeline',
    'running',
    'degraded',
    'stopping',
    'recovering'
  ].includes(props.liveState?.actual_state ?? '');

  useEffect(() => {
    if (!roles.includes(activeRole)) {
      setActiveRole(roles[0]);
    }
  }, [activeRole, roles]);

  const emit = (next: BoardSurfaceCalibration[]) => {
    props.onChange(props.node.node_type === 'analysis.end_face_cut_constraints' ? next[0] : next);
  };
  const updateSurface = (
    role: BoardSurfaceCalibration['surface_role'],
    update: (surface: BoardSurfaceCalibration) => BoardSurfaceCalibration
  ) => emit(surfaces.map((surface) => (surface.surface_role === role ? update(surface) : surface)));
  const active = surfaces.find((surface) => surface.surface_role === activeRole) ?? surfaces[0];

  return (
    <div className="runtime-structured-table">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <div className="actions-row">
        {roles.map((role) => (
          <button
            key={role}
            type="button"
            className={activeRole === role ? 'button--primary' : undefined}
            onClick={() => setActiveRole(role)}
          >
            {boardSurfaceLabel(role)}
          </button>
        ))}
        <button
          type="button"
          className="button--primary"
          disabled={!props.captureAvailable || props.capturePending || cameraChannelActive}
          onClick={props.onCapture}
        >
          {props.capturePending
            ? 'Capturing calibration frame...'
            : cameraChannelActive
              ? 'Using current live frame'
              : 'Capture calibration frame'}
        </button>
      </div>
      {imageSrc ? (
        <div className="runtime-live-editor runtime-live-editor--roi">
          <div className="runtime-live-editor__topline">
            <span>
              {active.surface_role === 'end_face'
                ? `${boardSurfaceLabel(active.surface_role)}: click the 4 corners clockwise`
                : `${boardSurfaceLabel(active.surface_role)}: 1 board end, 2 along board, 3 directly below, 4 back at board end`}
            </span>
            <small className="subtle-text">{active.image_points.length}/4 points</small>
          </div>
          {active.surface_role !== 'end_face' ? (
            <small className="subtle-text">
              This commissioning polygon sets the millimetre scale. During evaluation it follows
              the detected board end. Line 4–1 defines its tilt; 1–2 and 4–3 define the board
              direction. Line 2–3 is only the far image boundary and is ignored for board-pose
              detection.
            </small>
          ) : null}
          <div
            className="runtime-live-editor__canvas"
            style={previewMediaStyle(frame?.width ?? null, frame?.height ?? null)}
            onPointerDown={(event) => {
              if (event.button !== 0) return;
              const point = pointerPoint(event);
              updateSurface(active.surface_role, (surface) => ({
                ...surface,
                image_points:
                  surface.image_points.length >= 4 ? [point] : [...surface.image_points, point]
              }));
            }}
          >
            <img src={imageSrc} alt="Calibration frame" />
            <svg className="runtime-live-editor__line-layer" viewBox="0 0 100 100" preserveAspectRatio="none">
              {active.image_points.length > 1 ? (
                <polyline
                  className="runtime-live-editor__line"
                  fill="rgba(30, 136, 229, 0.18)"
                  points={active.image_points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')}
                />
              ) : null}
              {active.image_points.map((point, index) => (
                <g key={`${point.x}-${point.y}-${index}`}>
                  <circle cx={point.x * 100} cy={point.y * 100} r="1.2" />
                  <text
                    x={point.x * 100 + 1.5}
                    y={point.y * 100 - 1.5}
                    fill="#ffffff"
                    fontSize="3"
                    stroke="#10212b"
                    strokeWidth="0.5"
                    paintOrder="stroke"
                  >
                    {index + 1}
                  </text>
                </g>
              ))}
            </svg>
          </div>
          <button
            type="button"
            onClick={() => updateSurface(active.surface_role, (surface) => ({ ...surface, image_points: [] }))}
          >
            Reset points
          </button>
        </div>
      ) : (
        <div className="runtime-warning">
          {props.captureAvailable
            ? 'The camera may remain stopped. Capture one calibration frame; it will be released immediately afterward.'
            : 'Select a calibration camera in the connected Channel frame input, or edit this pipeline directly on its camera channel.'}
        </div>
      )}
      {surfaces.map((surface) => {
        const dimensions = boardSurfaceDimensions(surface);
        return (
          <div key={surface.surface_role} className="runtime-structured-table__row">
            <strong>{boardSurfaceLabel(surface.surface_role)}</strong>
            <label>
              <span>{surface.surface_role === 'end_face' ? 'Board width [mm]' : 'Visible length [mm]'}</span>
              <input
                type="number"
                min={1}
                step={0.1}
                value={dimensions.length}
                onChange={(event) =>
                  updateSurface(surface.surface_role, (current) =>
                    withBoardSurfaceDimensions(current, finiteNumber(event.target.value, dimensions.length), dimensions.width)
                  )
                }
              />
            </label>
            <label>
              <span>{surface.surface_role === 'end_face' ? 'Board thickness [mm]' : 'Surface width [mm]'}</span>
              <input
                type="number"
                min={1}
                step={0.1}
                value={dimensions.width}
                onChange={(event) =>
                  updateSurface(surface.surface_role, (current) =>
                    withBoardSurfaceDimensions(current, dimensions.length, finiteNumber(event.target.value, dimensions.width))
                  )
                }
              />
            </label>
            <span>{surface.image_points.length === 4 ? '4 points selected' : 'Calibration incomplete'}</span>
          </div>
        );
      })}
    </div>
  );
}

function ResultProjectionEditor(props: {
  field: RuntimeNodePropertySpec;
  value: unknown;
  graph: RuntimePipelineGraph;
  node: RuntimeNodeSpec;
  nodeTypes: Map<string, RuntimeNodeTypeSummary>;
  outputCatalog: RuntimePipelineOutputCatalog | null;
  onChange: (value: unknown) => void;
}) {
  const projections = normalizeResultProjections(props.value);
  const fieldOptions = projectionFieldOptionsForNode(
    props.graph,
    props.node,
    props.nodeTypes,
    props.outputCatalog
  );
  const optionByKey = new Map(fieldOptions.map((option) => [projectionOptionKey(option), option]));
  const defaultOption = fieldOptions[0] ?? null;

  const updateProjection = (index: number, projection: RuntimeResultProjection) => {
    props.onChange(
      projections.map((candidate, candidateIndex) =>
        candidateIndex === index ? projection : candidate
      )
    );
  };

  const responsePreview = projections
    .filter((projection) => projection.destinations.includes('response'))
    .reduce<Record<string, unknown>>((preview, projection) => {
      preview[projection.key] = projectionPreviewValue(projection);
      return preview;
    }, {});

  return (
    <div className="runtime-structured-table runtime-projection-editor">
      <FieldLabel label={props.field.label} helpText={fieldHelpText(props.field)} />
      <small className="subtle-text">
        Define the flat result fields that this pipeline returns, stores, or shows in live views.
      </small>
      {!fieldOptions.length ? (
        <div className="runtime-warning">
          Connect an upstream result-producing node and run validation to populate selectable values.
        </div>
      ) : null}
      {projections.map((projection, index) => {
        const selectedOption = optionByKey.get(projectionSourceKey(projection)) ?? null;
        const aggregateOps = projectionAggregateOpsForOption(selectedOption, props.outputCatalog);
        const itemFields = selectedOption?.listItemFields ?? [];
        return (
          <div key={`${projection.key}-${index}`} className="runtime-projection-row">
            <div className="runtime-projection-row__header">
              <label>
                <span>Output name</span>
                <input
                  value={projection.key}
                  onChange={(event) =>
                    updateProjection(index, {
                      ...projection,
                      key: slugKey(event.target.value) || projection.key
                    })
                  }
                  placeholder="quality"
                />
              </label>
              <label>
                <span>Label</span>
                <input
                  value={projection.label ?? ''}
                  onChange={(event) =>
                    updateProjection(index, {
                      ...projection,
                      label: event.target.value || null
                    })
                  }
                  placeholder="Quality"
                />
              </label>
              <button
                type="button"
                className="button--danger"
                onClick={() => props.onChange(projections.filter((_, candidateIndex) => candidateIndex !== index))}
              >
                Remove
              </button>
            </div>

            <div className="runtime-projection-row__grid">
              <label>
                <span>Value</span>
                <select
                  value={projectionSourceKey(projection)}
                  onChange={(event) => {
                    const option = optionByKey.get(event.target.value);
                    if (!option) {
                      return;
                    }
                    const nextAggregate = nextProjectionAggregate(projection, option, props.outputCatalog);
                    updateProjection(index, {
                      ...projection,
                      key: projection.key || suggestedResponseKey(option.value),
                      label: projection.label || option.label,
                      source: {
                        node_key: option.nodeKey,
                        output_port: option.outputPort ?? null,
                        path: option.value,
                        value_type: option.valueType ?? 'auto'
                      },
                      aggregate: nextAggregate,
                      fields: fieldsCompatibleWithOption(projection.fields, option)
                    });
                  }}
                >
                  <option value="">Choose value</option>
                  {fieldOptions.map((option) => (
                    <option key={projectionOptionKey(option)} value={projectionOptionKey(option)}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Calculate</span>
                <select
                  value={projection.aggregate.op}
                  onChange={(event) => {
                    const op = event.target.value as RuntimeResultProjection['aggregate']['op'];
                    updateProjection(index, {
                      ...projection,
                      aggregate: {
                        ...projection.aggregate,
                        op,
                        path: aggregateNeedsPath(op) ? projection.aggregate.path ?? itemFields[0] ?? null : null
                      }
                    });
                  }}
                >
                  {aggregateOps.map((op) => (
                    <option key={op} value={op}>
                      {aggregateLabel(op)}
                    </option>
                  ))}
                </select>
              </label>
              {aggregateNeedsPath(projection.aggregate.op) && itemFields.length ? (
                <label>
                  <span>Sub value</span>
                  <select
                    value={projection.aggregate.path ?? ''}
                    onChange={(event) =>
                      updateProjection(index, {
                        ...projection,
                        aggregate: { ...projection.aggregate, path: event.target.value || null }
                      })
                    }
                  >
                    <option value="">Whole value</option>
                    {itemFields.map((field) => (
                      <option key={field} value={field}>
                        {projectionFieldLabel(field)}
                      </option>
                    ))}
                  </select>
                </label>
              ) : null}
            </div>

            <div className="runtime-projection-destinations">
              {resultProjectionDestinations.map((destination) => (
                <label key={destination} className="checkbox-row">
                  <input
                    type="checkbox"
                    checked={projection.destinations.includes(destination)}
                    onChange={(event) =>
                      updateProjection(index, {
                        ...projection,
                        destinations: toggleDestination(
                          projection.destinations,
                          destination,
                          event.target.checked
                        )
                      })
                    }
                  />
                  <span>{destinationLabel(destination)}</span>
                </label>
              ))}
            </div>

            {selectedOption?.filterableFields?.length ? (
              <div className="runtime-projection-nested">
                <strong>Filters</strong>
                {projection.filters.map((filter, filterIndex) => (
                  <div key={`${filter.path}-${filterIndex}`} className="runtime-projection-filter-row">
                    <select
                      value={filter.path}
                      onChange={(event) =>
                        updateProjection(index, {
                          ...projection,
                          filters: projection.filters.map((candidate, candidateIndex) =>
                            candidateIndex === filterIndex
                              ? { ...candidate, path: event.target.value }
                              : candidate
                          )
                        })
                      }
                    >
                      {selectedOption.filterableFields?.map((field) => (
                        <option key={field} value={field}>
                          {projectionFieldLabel(field)}
                        </option>
                      ))}
                    </select>
                    <select
                      value={filter.op}
                      onChange={(event) =>
                        updateProjection(index, {
                          ...projection,
                          filters: projection.filters.map((candidate, candidateIndex) =>
                            candidateIndex === filterIndex
                              ? {
                                  ...candidate,
                                  op: event.target.value as RuntimeResultProjection['filters'][number]['op']
                                }
                              : candidate
                          )
                        })
                      }
                    >
                      {projectionFilterOps.map((op) => (
                        <option key={op} value={op}>
                          {filterOperatorLabel(op)}
                        </option>
                      ))}
                    </select>
                    {!filterOpHasNoValue(filter.op) ? (
                      <input
                        value={filter.value == null ? '' : String(filter.value)}
                        onChange={(event) =>
                          updateProjection(index, {
                            ...projection,
                            filters: projection.filters.map((candidate, candidateIndex) =>
                              candidateIndex === filterIndex
                                ? { ...candidate, value: parseProjectionLiteral(event.target.value) }
                                : candidate
                            )
                          })
                        }
                        placeholder="Value"
                      />
                    ) : null}
                    <button
                      type="button"
                      onClick={() =>
                        updateProjection(index, {
                          ...projection,
                          filters: projection.filters.filter((_, candidateIndex) => candidateIndex !== filterIndex)
                        })
                      }
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  onClick={() =>
                    updateProjection(index, {
                      ...projection,
                      filters: [
                        ...projection.filters,
                        {
                          path: selectedOption.filterableFields?.[0] ?? 'label',
                          op: 'eq',
                          value: ''
                        }
                      ]
                    })
                  }
                >
                  Add filter
                </button>
              </div>
            ) : null}

            {projection.aggregate.op === 'list' && itemFields.length ? (
              <div className="runtime-projection-nested">
                <strong>List fields</strong>
                <div className="runtime-projection-field-grid">
                  {itemFields.map((field) => {
                    const checked = projection.fields.some((item) => item.source_path === field);
                    return (
                      <label key={field} className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(event) =>
                            updateProjection(index, {
                              ...projection,
                              fields: event.target.checked
                                ? [
                                    ...projection.fields,
                                    {
                                      source_path: field,
                                      output_key: suggestedResponseKey(field),
                                      required: false,
                                      default_value: null
                                    }
                                  ]
                                : projection.fields.filter((item) => item.source_path !== field)
                            })
                          }
                        />
                        <span>{projectionFieldLabel(field)}</span>
                      </label>
                    );
                  })}
                </div>
              </div>
            ) : null}

            <div className="runtime-projection-nested">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={Boolean(projection.decision)}
                  onChange={(event) =>
                    updateProjection(index, {
                      ...projection,
                      decision: event.target.checked
                        ? { op: 'gt', value: 0, when_true: 'FAIL', when_false: 'PASS' }
                        : null
                    })
                  }
                />
                <span>Convert to PASS/FAIL decision</span>
              </label>
              {projection.decision ? (
                <div className="runtime-projection-row__grid">
                  <label>
                    <span>Condition</span>
                    <select
                      value={projection.decision.op}
                      onChange={(event) =>
                        updateProjection(index, {
                          ...projection,
                          decision: projection.decision
                            ? {
                                ...projection.decision,
                                op: event.target.value as NonNullable<RuntimeResultProjection['decision']>['op']
                              }
                            : null
                        })
                      }
                    >
                      {projectionFilterOps.map((op) => (
                        <option key={op} value={op}>
                          {filterOperatorLabel(op)}
                        </option>
                      ))}
                    </select>
                  </label>
                  {!filterOpHasNoValue(projection.decision.op) ? (
                    <label>
                      <span>Compare value</span>
                      <input
                        value={projection.decision.value == null ? '' : String(projection.decision.value)}
                        onChange={(event) =>
                          updateProjection(index, {
                            ...projection,
                            decision: projection.decision
                              ? {
                                  ...projection.decision,
                                  value: parseProjectionLiteral(event.target.value)
                                }
                              : null
                          })
                        }
                      />
                    </label>
                  ) : null}
                  <label>
                    <span>True</span>
                    <input
                      value={String(projection.decision.when_true)}
                      onChange={(event) =>
                        updateProjection(index, {
                          ...projection,
                          decision: projection.decision
                            ? { ...projection.decision, when_true: event.target.value }
                            : null
                        })
                      }
                    />
                  </label>
                  <label>
                    <span>False</span>
                    <input
                      value={String(projection.decision.when_false)}
                      onChange={(event) =>
                        updateProjection(index, {
                          ...projection,
                          decision: projection.decision
                            ? { ...projection.decision, when_false: event.target.value }
                            : null
                        })
                      }
                    />
                  </label>
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
      <div className="actions-row">
        <button
          type="button"
          onClick={() => props.onChange([...projections, blankResultProjection(defaultOption, projections.length)])}
          disabled={!defaultOption}
        >
          Add result field
        </button>
      </div>
      {Object.keys(responsePreview).length ? (
        <div className="runtime-response-preview">
          <strong>Response preview</strong>
          <pre>{JSON.stringify(responsePreview, null, 2)}</pre>
        </div>
      ) : null}
    </div>
  );
}

function SiemensSpsMappingEditor(props: {
  mapping: SiemensSpsMappingRow;
  index: number;
  datalistId: string;
  fieldOptionsAvailable: boolean;
  sourceKnown: boolean;
  onChange: (mapping: SiemensSpsMappingRow) => void;
  onRemove: () => void;
}) {
  const mapping = props.mapping;
  const conversion = mapping.conversion;
  const target = mapping.target;
  const update = (patch: Partial<SiemensSpsMappingRow>) => props.onChange({ ...mapping, ...patch });
  const updateSource = (patch: Partial<SiemensSpsMappingRow['source']>) =>
    update({ source: { ...mapping.source, ...patch } });
  const updateTarget = (patch: Partial<SiemensSpsMappingRow['target']>) =>
    update({ target: { ...mapping.target, ...patch } });
  const updateConversion = (patch: Partial<SiemensSpsMappingRow['conversion']>) =>
    update({ conversion: { ...mapping.conversion, ...patch } });
  return (
    <div className="runtime-structured-table__row runtime-plc-mapping-row">
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={mapping.enabled}
          onChange={(event) => update({ enabled: event.target.checked })}
        />
        <span>Enabled</span>
      </label>
      <label>
        <span>Name</span>
        <input
          value={mapping.label}
          onChange={(event) => update({ label: event.target.value, key: mapping.key || slugKey(event.target.value) })}
          placeholder={`Mapping ${props.index + 1}`}
        />
      </label>
      <label>
        <span>Source field</span>
        <input
          list={props.datalistId}
          value={mapping.source.field}
          onChange={(event) => updateSource({ field: event.target.value })}
          placeholder={props.fieldOptionsAvailable ? 'Choose upstream field' : 'Connect upstream result first'}
        />
        {!props.sourceKnown ? <small className="subtle-text">Unknown upstream field</small> : null}
      </label>
      <label className="checkbox-row">
        <input
          type="checkbox"
          checked={mapping.source.allow_advanced_path}
          onChange={(event) => updateSource({ allow_advanced_path: event.target.checked })}
        />
        <span>Advanced path</span>
      </label>
      {mapping.source.allow_advanced_path ? (
        <label>
          <span>Advanced path</span>
          <input
            value={mapping.source.advanced_path}
            onChange={(event) => updateSource({ advanced_path: event.target.value })}
            placeholder="custom.result.path"
          />
        </label>
      ) : null}
      <div className="runtime-plc-address">
        <label>
          <span>DB</span>
          <input
            type="number"
            min={1}
            max={65535}
            value={target.db_number}
            onChange={(event) => updateTarget({ db_number: Number(event.target.value) })}
          />
        </label>
        <label>
          <span>Byte</span>
          <input
            type="number"
            min={0}
            max={65535}
            value={target.byte_offset}
            onChange={(event) => updateTarget({ byte_offset: Number(event.target.value) })}
          />
        </label>
        <label>
          <span>Bit</span>
          <input
            type="number"
            min={0}
            max={7}
            value={target.bit_offset ?? ''}
            disabled={target.data_type !== 'BOOL'}
            onChange={(event) => updateTarget({ bit_offset: nullableFiniteNumber(event.target.value, null) })}
          />
        </label>
        <label>
          <span>Datatype</span>
          <select
            value={target.data_type}
            onChange={(event) => {
              const dataType = event.target.value as SiemensSpsDataType;
              updateTarget({ data_type: dataType, bit_offset: dataType === 'BOOL' ? target.bit_offset ?? 0 : null });
            }}
          >
            {siemensSpsDataTypes.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </select>
        </label>
        <code>{formatSiemensSpsAddress(target)}</code>
      </div>
      <label>
        <span>Mapping mode</span>
        <select
          value={conversion.mode}
          onChange={(event) => updateConversion({ mode: event.target.value as SiemensSpsConversionMode })}
        >
          {siemensSpsConversionModes.map((item) => (
            <option key={item} value={item}>
              {item}
            </option>
          ))}
        </select>
      </label>
      {conversion.mode === 'enum' ? (
        <div className="runtime-plc-enum-table">
          {conversion.enum_values.map((entry, entryIndex) => (
            <div key={`${entry.source}-${entryIndex}`} className="runtime-plc-enum-row">
              <input
                value={entry.source}
                onChange={(event) =>
                  updateConversion({
                    enum_values: conversion.enum_values.map((candidate, candidateIndex) =>
                      candidateIndex === entryIndex ? { ...candidate, source: event.target.value } : candidate
                    )
                  })
                }
                placeholder="gut"
              />
              <input
                value={String(entry.value)}
                onChange={(event) =>
                  updateConversion({
                    enum_values: conversion.enum_values.map((candidate, candidateIndex) =>
                      candidateIndex === entryIndex
                        ? { ...candidate, value: parsePlcLiteral(event.target.value) }
                        : candidate
                    )
                  })
                }
                placeholder="1"
              />
              <button
                type="button"
                onClick={() =>
                  updateConversion({
                    enum_values: conversion.enum_values.filter((_, candidateIndex) => candidateIndex !== entryIndex)
                  })
                }
              >
                Remove
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={() =>
              updateConversion({
                enum_values: [...conversion.enum_values, { source: '', value: 0 }]
              })
            }
          >
            Add enum value
          </button>
        </div>
      ) : null}
      {conversion.mode === 'scale' ? (
        <div className="runtime-plc-address">
          <label>
            <span>Multiplier</span>
            <input type="number" step={0.01} value={conversion.multiplier} onChange={(event) => updateConversion({ multiplier: Number(event.target.value) })} />
          </label>
          <label>
            <span>Add</span>
            <input type="number" step={0.01} value={conversion.addend} onChange={(event) => updateConversion({ addend: Number(event.target.value) })} />
          </label>
          <label>
            <span>Round</span>
            <select
              value={conversion.rounding}
              onChange={(event) => updateConversion({ rounding: event.target.value as SiemensSpsMappingRow['conversion']['rounding'] })}
            >
              <option value="none">None</option>
              <option value="nearest">Nearest</option>
              <option value="floor">Floor</option>
              <option value="ceil">Ceil</option>
            </select>
          </label>
          <label>
            <span>Clamp min</span>
            <input type="number" step={0.01} value={conversion.clamp_min ?? ''} onChange={(event) => updateConversion({ clamp_min: nullableFiniteNumber(event.target.value, null) })} />
          </label>
          <label>
            <span>Clamp max</span>
            <input type="number" step={0.01} value={conversion.clamp_max ?? ''} onChange={(event) => updateConversion({ clamp_max: nullableFiniteNumber(event.target.value, null) })} />
          </label>
        </div>
      ) : null}
      {conversion.mode === 'threshold' ? (
        <div className="runtime-plc-address">
          <label>
            <span>Operator</span>
            <select
              value={conversion.operator}
              onChange={(event) => updateConversion({ operator: event.target.value as SiemensSpsMappingRow['conversion']['operator'] })}
            >
              {['>=', '>', '<=', '<', '==', '!='].map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Threshold</span>
            <input type="number" step={0.01} value={conversion.threshold} onChange={(event) => updateConversion({ threshold: Number(event.target.value) })} />
          </label>
          <label>
            <span>True value</span>
            <input value={conversion.true_value == null ? '' : String(conversion.true_value)} onChange={(event) => updateConversion({ true_value: parseNullablePlcLiteral(event.target.value) })} />
          </label>
          <label>
            <span>False value</span>
            <input value={conversion.false_value == null ? '' : String(conversion.false_value)} onChange={(event) => updateConversion({ false_value: parseNullablePlcLiteral(event.target.value) })} />
          </label>
        </div>
      ) : null}
      {conversion.mode === 'boolean' ? (
        <div className="runtime-plc-address">
          <label>
            <span>True value</span>
            <input value={conversion.true_value == null ? '' : String(conversion.true_value)} onChange={(event) => updateConversion({ true_value: parseNullablePlcLiteral(event.target.value) })} />
          </label>
          <label>
            <span>False value</span>
            <input value={conversion.false_value == null ? '' : String(conversion.false_value)} onChange={(event) => updateConversion({ false_value: parseNullablePlcLiteral(event.target.value) })} />
          </label>
        </div>
      ) : null}
      <div className="runtime-plc-address">
        <label>
          <span>Missing value</span>
          <select
            value={conversion.default_behavior}
            onChange={(event) => updateConversion({ default_behavior: event.target.value as SiemensSpsMappingRow['conversion']['default_behavior'] })}
          >
            <option value="fail">Fail</option>
            <option value="skip">Skip</option>
            <option value="write_default">Write default</option>
          </select>
        </label>
        {conversion.default_behavior === 'write_default' ? (
          <label>
            <span>Default value</span>
            <input value={conversion.default_value == null ? '' : String(conversion.default_value)} onChange={(event) => updateConversion({ default_value: parseNullablePlcLiteral(event.target.value) })} />
          </label>
        ) : null}
        <label>
          <span>Failure behavior</span>
          <select
            value={mapping.failure_behavior}
            onChange={(event) => update({ failure_behavior: event.target.value as SiemensSpsMappingRow['failure_behavior'] })}
          >
            <option value="inherit">Inherit</option>
            <option value="fail_pipeline">Fail pipeline</option>
            <option value="warn_continue">Warn and continue</option>
            <option value="skip_mapping">Skip mapping</option>
          </select>
        </label>
      </div>
      <button type="button" onClick={props.onRemove}>
        Remove mapping
      </button>
    </div>
  );
}

function ZoneSpatialRuleTable(props: {
  label: string;
  helpText?: string | null;
  rules: SpatialRule[];
  zoneOptions: string[];
  labelOptions: string[];
  onChange: (value: unknown) => void;
}) {
  const datalistId = `runtime-zone-spatial-labels-${slugKey(props.label)}`;
  return (
    <div className="runtime-structured-table runtime-spatial-editor">
      <FieldLabel
        label={props.label}
        helpText={
          props.helpText ||
          'Each row is one zone requirement. Use policy All above to require combinations such as LKW and Lader in the same zone.'
        }
      />
      <small className="subtle-text">
        {props.helpText ||
          'Each row is one zone requirement. Use policy All above to require combinations such as LKW and Lader in the same zone.'}
      </small>
      {props.labelOptions.length ? (
        <datalist id={datalistId}>
          {props.labelOptions.map((label) => (
            <option key={label} value={label} />
          ))}
        </datalist>
      ) : null}
      {props.rules.map((rule, index) => (
        <div key={`${rule.rule_key || rule.zone_key || 'zone'}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--spatial-zone">
          <div className="runtime-spatial-editor__topline">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.enabled}
                onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'enabled', event.target.checked))}
              />
              <span>Enabled</span>
            </label>
            <input
              value={rule.rule_key}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'rule_key', event.target.value))}
              placeholder="Rule key, e.g. zone_a_lkw"
            />
          </div>
          <label>
            <span>Object classes</span>
            <input
              list={props.labelOptions.length ? datalistId : undefined}
              value={formatRuleLabels(rule.primary_labels)}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'primary_labels', normalizeRuleLabels(event.target.value)))}
              placeholder="LKW, Lader or empty for every class"
            />
          </label>
          <label>
            <span>Zone</span>
            <select
              value={rule.zone_key}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'zone_key', event.target.value))}
            >
              <option value="">All zones</option>
              {props.zoneOptions.map((zone) => (
                <option key={zone} value={zone}>
                  {zone}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Zone match</span>
            <select
              value={rule.zone_match}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'zone_match', event.target.value))}
            >
              {spatialZoneMatchOptions.map((option) => (
                <option key={option} value={option}>
                  {zoneMatchLabel(option)}
                </option>
              ))}
            </select>
          </label>
          {rule.zone_match === 'box_overlaps' ? (
            <label>
              <span>Minimum overlap %</span>
              <input
                type="number"
                min={0}
                max={100}
                step={0.1}
                value={rule.min_overlap_percent}
                onChange={(event) =>
                  props.onChange(updateSpatialRule(props.rules, index, 'min_overlap_percent', Number(event.target.value)))
                }
              />
              <small className="subtle-text">Percent of the object box that must lie inside the zone.</small>
            </label>
          ) : null}
          <label>
            <span>Minimum objects</span>
            <input
              type="number"
              min={1}
              step={1}
              value={rule.min_count}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'min_count', Number(event.target.value)))}
            />
          </label>
          <label>
            <span>Maximum objects</span>
            <input
              type="number"
              min={0}
              step={1}
              value={rule.max_count ?? ''}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'max_count', nullableFiniteNumber(event.target.value, null)))}
              placeholder="No limit"
            />
          </label>
          <label>
            <span>Result label</span>
            <input
              value={rule.signal_label}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'signal_label', event.target.value))}
              placeholder="LKW in Zone A"
            />
          </label>
          <button type="button" onClick={() => props.onChange(props.rules.filter((_, candidateIndex) => candidateIndex !== index))}>
            Remove
          </button>
        </div>
      ))}
      <button type="button" onClick={() => props.onChange([...props.rules, blankZoneSpatialRule()])}>
        Add zone requirement
      </button>
    </div>
  );
}

function CloseSpatialRuleTable(props: {
  label: string;
  helpText?: string | null;
  rules: SpatialRule[];
  labelOptions: string[];
  onChange: (value: unknown) => void;
}) {
  const datalistId = `runtime-close-spatial-labels-${slugKey(props.label)}`;
  return (
    <div className="runtime-structured-table runtime-spatial-editor">
      <FieldLabel
        label={props.label}
        helpText={
          props.helpText ||
          'Each row checks object pairs. Use overlap 100% when one object should fully cover the smaller box.'
        }
      />
      <small className="subtle-text">
        {props.helpText ||
          'Each row checks object pairs. Use overlap 100% when one object should fully cover the smaller box.'}
      </small>
      {props.labelOptions.length ? (
        <datalist id={datalistId}>
          {props.labelOptions.map((label) => (
            <option key={label} value={label} />
          ))}
        </datalist>
      ) : null}
      {props.rules.map((rule, index) => (
        <div key={`${rule.rule_key || rule.relation}-${index}`} className="runtime-structured-table__row runtime-structured-table__row--spatial-close">
          <div className="runtime-spatial-editor__topline">
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={rule.enabled}
                onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'enabled', event.target.checked))}
              />
              <span>Enabled</span>
            </label>
            <select
              value={rule.relation}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'relation', event.target.value))}
            >
              <option value="overlap">Overlap / full cover</option>
              <option value="distance">Near by distance</option>
              <option value="approaching">Approaching</option>
            </select>
          </div>
          <input
            value={rule.rule_key}
            onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'rule_key', event.target.value))}
            placeholder="Rule key, e.g. lkw_lader_overlap"
          />
          <label>
            <span>First object classes</span>
            <input
              list={props.labelOptions.length ? datalistId : undefined}
              value={formatRuleLabels(rule.primary_labels)}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'primary_labels', normalizeRuleLabels(event.target.value)))}
              placeholder="LKW or empty for every class"
            />
          </label>
          <label>
            <span>Second object classes</span>
            <input
              list={props.labelOptions.length ? datalistId : undefined}
              value={formatRuleLabels(rule.secondary_labels)}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'secondary_labels', normalizeRuleLabels(event.target.value)))}
              placeholder="Lader or empty for every class"
            />
          </label>
          <label>
            <span>Class pairing</span>
            <select
              value={rule.label_pairing}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'label_pairing', event.target.value))}
            >
              {spatialLabelPairingOptions.map((option) => (
                <option key={option} value={option}>
                  {labelPairingLabel(option)}
                </option>
              ))}
            </select>
          </label>
          {rule.relation === 'overlap' ? (
            <label>
              <span>Minimum overlap %</span>
              <input
                type="number"
                min={0}
                max={100}
                step={0.1}
                value={rule.min_overlap_percent}
                onChange={(event) =>
                  props.onChange(updateSpatialRule(props.rules, index, 'min_overlap_percent', Number(event.target.value)))
                }
              />
              <small className="subtle-text">100% means the smaller box is fully covered.</small>
            </label>
          ) : (
            <label>
              <span>Maximum distance %</span>
              <input
                type="number"
                min={0}
                max={100}
                step={0.1}
                value={rule.max_distance_percent ?? ''}
                onChange={(event) =>
                  props.onChange(updateSpatialRule(props.rules, index, 'max_distance_percent', nullableFiniteNumber(event.target.value, null)))
                }
                placeholder="No limit"
              />
              <small className="subtle-text">Percent of the frame diagonal between object edges.</small>
            </label>
          )}
          {rule.relation === 'approaching' ? (
            <label>
              <span>Minimum closing %</span>
              <input
                type="number"
                min={0}
                max={100}
                step={0.1}
                value={rule.min_closing_percent}
                onChange={(event) =>
                  props.onChange(updateSpatialRule(props.rules, index, 'min_closing_percent', Number(event.target.value)))
                }
              />
            </label>
          ) : null}
          <label>
            <span>Result label</span>
            <input
              value={rule.signal_label}
              onChange={(event) => props.onChange(updateSpatialRule(props.rules, index, 'signal_label', event.target.value))}
              placeholder="Objects overlap"
            />
          </label>
          <button type="button" onClick={() => props.onChange(props.rules.filter((_, candidateIndex) => candidateIndex !== index))}>
            Remove
          </button>
        </div>
      ))}
      <button type="button" onClick={() => props.onChange([...props.rules, blankCloseSpatialRule()])}>
        Add close rule
      </button>
    </div>
  );
}

function VisualBoxEditor(props: {
  title: string;
  liveState: RuntimeLiveViewState | null;
  boxes: StructuredBox[];
  mode: 'crop' | 'roi' | 'zone';
  maxBoxes?: number;
  onChange: (boxes: StructuredBox[]) => void;
}) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<{
    startX: number;
    startY: number;
    currentX: number;
    currentY: number;
  } | null>(null);
  const [cropInteraction, setCropInteraction] = useState<{
    kind: 'move' | 'resize';
    startPoint: { x: number; y: number };
    startBox: StructuredBox;
    box: StructuredBox;
  } | null>(null);
  const imageSrc = liveFrameImageSource(props.liveState);
  const frame = props.liveState?.frame ?? null;
  const visibleBoxes = props.boxes.map((box) => normalizedBox(box)).filter((box): box is StructuredBox => Boolean(box));
  const draftBox = draftToBox(draft);
  const displayedBoxes =
    props.mode === 'crop' && cropInteraction && visibleBoxes.length === 1
      ? [cropInteraction.box]
      : visibleBoxes;

  if (!imageSrc) {
    return (
      <div className="runtime-live-editor runtime-live-editor--empty">
        <span>{props.title}</span>
        <p className="subtle-text">No live frame is available for on-screen editing.</p>
      </div>
    );
  }

  const completeDraft = () => {
    const box = draftToBox(draft);
    setDraft(null);
    if (!box || box.width < 0.005 || box.height < 0.005) {
      return;
    }
    const nextBox = {
      ...box,
      label:
        props.mode === 'crop'
          ? 'Crop'
          : `${props.mode}_${visibleBoxes.length + 1}`
    };
    props.onChange(props.maxBoxes === 1 ? [nextBox] : [...visibleBoxes, nextBox]);
  };

  const updateCropInteraction = (point: { x: number; y: number }) => {
    if (!cropInteraction) {
      return;
    }
    setCropInteraction({
      ...cropInteraction,
      box: cropEditorAdjustedBox(cropInteraction.startBox, cropInteraction.startPoint, point, cropInteraction.kind)
    });
  };

  const completeCropInteraction = () => {
    if (!cropInteraction) {
      return;
    }
    setCropInteraction(null);
    props.onChange([cropInteraction.box]);
  };

  return (
    <div className={`runtime-live-editor runtime-live-editor--${props.mode}`}>
      <div className="runtime-live-editor__topline">
        <span>{props.title}</span>
        <small className="subtle-text">
          {frame?.width && frame?.height ? `${frame.width} x ${frame.height}` : 'Frame'}
        </small>
      </div>
      <div
        ref={canvasRef}
        className="runtime-live-editor__canvas"
        style={previewMediaStyle(frame?.width ?? null, frame?.height ?? null)}
        onPointerDown={(event) => {
          if (event.button !== 0) {
            return;
          }
          if (cropInteraction) {
            return;
          }
          const point = pointerPoint(event);
          event.currentTarget.setPointerCapture(event.pointerId);
          setDraft({ startX: point.x, startY: point.y, currentX: point.x, currentY: point.y });
        }}
        onPointerMove={(event) => {
          if (cropInteraction) {
            updateCropInteraction(pointerPoint(event));
            return;
          }
          if (!draft) {
            return;
          }
          const point = pointerPoint(event);
          setDraft({ ...draft, currentX: point.x, currentY: point.y });
        }}
        onPointerUp={() => {
          if (cropInteraction) {
            completeCropInteraction();
            return;
          }
          completeDraft();
        }}
        onPointerCancel={() => {
          setDraft(null);
          setCropInteraction(null);
        }}
      >
        <img src={imageSrc} alt="" aria-hidden="true" />
        {displayedBoxes.map((box, index) => (
          <button
            key={`${box.label ?? props.mode}-${index}`}
            type="button"
            className={`runtime-live-editor__box runtime-live-editor__box--${props.mode}`}
            style={visualBoxStyle(box)}
            onPointerDown={(event) => {
              event.stopPropagation();
              if (props.mode !== 'crop' || event.button !== 0) {
                return;
              }
              const point = pointerPointInElement(event, canvasRef.current);
              event.currentTarget.setPointerCapture(event.pointerId);
              setCropInteraction({ kind: 'move', startPoint: point, startBox: box, box });
            }}
            onClick={() => {
              if (props.maxBoxes === 1 && props.mode === 'crop') {
                return;
              }
              props.onChange(visibleBoxes.filter((_, candidateIndex) => candidateIndex !== index));
            }}
            title={
              props.mode === 'crop'
                ? 'Drag to move; use the lower-right handle to resize'
                : props.maxBoxes === 1
                  ? 'Draw a new selection to replace it'
                  : 'Remove selection'
            }
          >
            <span>{box.label || `${props.mode} ${index + 1}`}</span>
            {props.mode === 'crop' ? (
              <i
                className="runtime-live-editor__resize-handle"
                aria-label="Resize crop window"
                onPointerDown={(event) => {
                  if (event.button !== 0) {
                    return;
                  }
                  event.stopPropagation();
                  const point = pointerPointInElement(event, canvasRef.current);
                  event.currentTarget.setPointerCapture(event.pointerId);
                  setCropInteraction({ kind: 'resize', startPoint: point, startBox: box, box });
                }}
              />
            ) : null}
          </button>
        ))}
        {draftBox ? (
          <span
            className={`runtime-live-editor__box runtime-live-editor__box--${props.mode} runtime-live-editor__box--draft`}
            style={visualBoxStyle(draftBox)}
          />
        ) : null}
      </div>
      <small className="subtle-text">
        {props.mode === 'crop'
          ? 'Drag the window to move it. Drag the lower-right handle to resize it; draw on the frame to replace it.'
          : props.maxBoxes === 1
            ? 'Draw on the frame to replace the selection.'
            : 'Draw on the frame to add a selection.'}
      </small>
    </div>
  );
}

function TrackerPolygonRoiEditor(props: {
  title: string;
  liveState: RuntimeLiveViewState | null;
  regions: TrackerRoiRegion[];
  defaultLabel: string;
  onChange: (regions: TrackerRoiRegion[]) => void;
}) {
  const [draft, setDraft] = useState<TrackerRoiPoint[]>([]);
  const imageSrc = liveFrameImageSource(props.liveState);
  const frame = props.liveState?.frame ?? null;

  if (!imageSrc) {
    return (
      <div className="runtime-live-editor runtime-live-editor--empty">
        <span>{props.title}</span>
        <p className="subtle-text">No canonical model frame is available for on-screen editing.</p>
      </div>
    );
  }

  const saveDraft = () => {
    if (draft.length < 3) return;
    const existingRegions =
      props.regions.length === 1 && trackerRoiIsWholeView(props.regions[0])
        ? []
        : props.regions;
    props.onChange([
      ...existingRegions,
      {
        label: `${props.defaultLabel}${existingRegions.length ? ` ${existingRegions.length + 1}` : ''}`,
        points: draft
      }
    ]);
    setDraft([]);
  };

  return (
    <div className="runtime-live-editor runtime-live-editor--roi">
      <div className="runtime-live-editor__topline">
        <span>{props.title}</span>
        <small className="subtle-text">
          {frame?.width && frame?.height ? `${frame.width} x ${frame.height}` : 'Canonical frame'}
        </small>
      </div>
      <div
        className="runtime-live-editor__canvas"
        style={previewMediaStyle(frame?.width ?? null, frame?.height ?? null)}
        onPointerDown={(event) => {
          if (event.button !== 0) return;
          const point = pointerPoint(event);
          setDraft((current) => [...current, point]);
        }}
      >
        <img src={imageSrc} alt="" aria-hidden="true" draggable={false} />
        <svg
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}
        >
          {props.regions.map((region, index) => (
            <polygon
              key={`${region.label}-${index}`}
              points={region.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')}
              fill="rgba(93, 183, 255, 0.18)"
              stroke="#5db7ff"
              strokeWidth={0.5}
            />
          ))}
          {draft.length ? (
            <>
              <polyline
                points={draft.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')}
                fill={draft.length >= 3 ? 'rgba(244, 183, 64, 0.16)' : 'none'}
                stroke="#f4b740"
                strokeWidth={0.5}
                strokeDasharray="1.5 1"
              />
              {draft.map((point, index) => (
                <circle
                  key={`${point.x}-${point.y}-${index}`}
                  cx={point.x * 100}
                  cy={point.y * 100}
                  r={0.8}
                  fill="#f4b740"
                />
              ))}
            </>
          ) : null}
        </svg>
      </div>
      <small className="subtle-text">
        Click the corners in order around one convex conveyor area. Three points are the minimum;
        four points are usually easiest. The last edge closes automatically.
      </small>
      <div className="actions-row">
        <button type="button" disabled={draft.length < 3} onClick={saveDraft}>
          Apply polygon
        </button>
        <button type="button" disabled={!draft.length} onClick={() => setDraft([])}>
          Clear points
        </button>
        <button
          type="button"
          onClick={() =>
            props.onChange([
              ...props.regions,
              {
                label: props.defaultLabel,
                points: [
                  { x: 0, y: 0 },
                  { x: 1, y: 0 },
                  { x: 1, y: 1 },
                  { x: 0, y: 1 }
                ]
              }
            ])
          }
        >
          Add whole view (diagnostic only)
        </button>
      </div>
      {props.regions.map((region, index) => (
        <div key={`${region.label}-row-${index}`} className="runtime-box-list__row">
          <input
            value={region.label}
            onChange={(event) =>
              props.onChange(
                props.regions.map((candidate, candidateIndex) =>
                  candidateIndex === index
                    ? { ...candidate, label: event.target.value }
                    : candidate
                )
              )
            }
            placeholder="Area label"
          />
          <span className="subtle-text">{region.points.length} polygon points</span>
          <button
            type="button"
            onClick={() =>
              props.onChange(
                props.regions.filter((_, candidateIndex) => candidateIndex !== index)
              )
            }
          >
            Remove
          </button>
        </div>
      ))}
    </div>
  );
}

function VisualLineEditor(props: {
  title: string;
  liveState: RuntimeLiveViewState | null;
  line: DirectionReference;
  onChange: (line: DirectionReference) => void;
}) {
  const [draft, setDraft] = useState<{
    startX: number;
    startY: number;
    currentX: number;
    currentY: number;
  } | null>(null);
  const imageSrc = liveFrameImageSource(props.liveState);
  const frame = props.liveState?.frame ?? null;
  const line = normalizeDirectionReference(props.line);
  const draftLine = draft
    ? {
        start: { x: draft.startX, y: draft.startY },
        end: { x: draft.currentX, y: draft.currentY }
      }
    : null;

  if (!imageSrc) {
    return (
      <div className="runtime-live-editor runtime-live-editor--empty">
        <span>{props.title}</span>
        <p className="subtle-text">No live frame is available for on-screen editing.</p>
      </div>
    );
  }

  const completeDraft = () => {
    if (!draft || Math.hypot(draft.currentX - draft.startX, draft.currentY - draft.startY) < 0.01) {
      setDraft(null);
      return;
    }
    props.onChange({
      ...line,
      start: { x: draft.startX, y: draft.startY },
      end: { x: draft.currentX, y: draft.currentY }
    });
    setDraft(null);
  };

  return (
    <div className="runtime-live-editor runtime-live-editor--direction">
      <div className="runtime-live-editor__topline">
        <span>{props.title}</span>
        <small className="subtle-text">
          {frame?.width && frame?.height ? `${frame.width} x ${frame.height}` : 'Frame'}
        </small>
      </div>
      <div
        className="runtime-live-editor__canvas"
        style={previewMediaStyle(frame?.width ?? null, frame?.height ?? null)}
        onPointerDown={(event) => {
          if (event.button !== 0) {
            return;
          }
          const point = pointerPoint(event);
          event.currentTarget.setPointerCapture(event.pointerId);
          setDraft({ startX: point.x, startY: point.y, currentX: point.x, currentY: point.y });
        }}
        onPointerMove={(event) => {
          if (!draft) {
            return;
          }
          const point = pointerPoint(event);
          setDraft({ ...draft, currentX: point.x, currentY: point.y });
        }}
        onPointerUp={completeDraft}
        onPointerCancel={() => setDraft(null)}
      >
        <img src={imageSrc} alt="" aria-hidden="true" />
        <VisualDirectionLine line={line} label={line.label || 'Direction'} />
        {draftLine ? <VisualDirectionLine line={draftLine} draft /> : null}
      </div>
      <small className="subtle-text">Draw on the frame to replace the reference direction.</small>
    </div>
  );
}

function VisualDirectionLine(props: {
  line: Pick<DirectionReference, 'start' | 'end'>;
  label?: string;
  draft?: boolean;
}) {
  const x1 = props.line.start.x * 100;
  const y1 = props.line.start.y * 100;
  const x2 = props.line.end.x * 100;
  const y2 = props.line.end.y * 100;
  return (
    <svg className="runtime-live-editor__line-layer" viewBox="0 0 100 100" preserveAspectRatio="none">
      <line
        className={props.draft ? 'runtime-live-editor__line runtime-live-editor__line--draft' : 'runtime-live-editor__line'}
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
      />
      {props.label && !props.draft ? (
        <text className="runtime-live-editor__line-label" x={(x1 + x2) / 2} y={Math.max((y1 + y2) / 2 - 1.5, 2)}>
          {props.label}
        </text>
      ) : null}
    </svg>
  );
}

function liveFrameImageSource(liveState: RuntimeLiveViewState | null) {
  const frame = liveState?.frame ?? null;
  const imageBase64 = frame?.image_base64;
  if (!imageBase64) {
    return null;
  }
  if (imageBase64.startsWith('data:')) {
    return imageBase64;
  }
  return `data:${frame.mime_type || 'image/jpeg'};base64,${imageBase64}`;
}

function pointerPoint(event: ReactPointerEvent<HTMLElement>) {
  return pointerPointInElement(event, event.currentTarget);
}

function pointerPointInElement(event: ReactPointerEvent<HTMLElement>, element: HTMLElement | null) {
  const rect = element?.getBoundingClientRect();
  return {
    x: clampNumber((event.clientX - (rect?.left ?? event.clientX)) / Math.max(rect?.width ?? 1, 1), 0, 1),
    y: clampNumber((event.clientY - (rect?.top ?? event.clientY)) / Math.max(rect?.height ?? 1, 1), 0, 1)
  };
}

function draftToBox(
  draft: { startX: number; startY: number; currentX: number; currentY: number } | null
): StructuredBox | null {
  if (!draft) {
    return null;
  }
  const x = Math.min(draft.startX, draft.currentX);
  const y = Math.min(draft.startY, draft.currentY);
  const width = Math.abs(draft.currentX - draft.startX);
  const height = Math.abs(draft.currentY - draft.startY);
  return { x, y, width, height, angle_degrees: 0 };
}

function normalizedBox(box: StructuredBox): StructuredBox | null {
  const x = clampNumber(finiteNumber(box.x, 0), 0, 1);
  const y = clampNumber(finiteNumber(box.y, 0), 0, 1);
  const width = clampNumber(finiteNumber(box.width, 0), 0, 1 - x);
  const height = clampNumber(finiteNumber(box.height, 0), 0, 1 - y);
  const angle_degrees = normalizeAngleDegrees(box.angle_degrees);
  if (width <= 0 || height <= 0) {
    return null;
  }
  return { ...box, x, y, width, height, angle_degrees };
}

export function cropEditorAdjustedBox(
  startBox: StructuredBox,
  startPoint: { x: number; y: number },
  point: { x: number; y: number },
  kind: 'move' | 'resize'
): StructuredBox {
  const box = normalizedBox(startBox);
  if (!box) {
    return startBox;
  }
  const minimumSize = 0.005;
  if (kind === 'move') {
    return {
      ...box,
      x: clampNumber(box.x + point.x - startPoint.x, 0, 1 - box.width),
      y: clampNumber(box.y + point.y - startPoint.y, 0, 1 - box.height)
    };
  }
  return {
    ...box,
    width: clampNumber(box.width + point.x - startPoint.x, minimumSize, 1 - box.x),
    height: clampNumber(box.height + point.y - startPoint.y, minimumSize, 1 - box.y)
  };
}

function visualBoxStyle(box: StructuredBox) {
  return {
    left: `${box.x * 100}%`,
    top: `${box.y * 100}%`,
    width: `${box.width * 100}%`,
    height: `${box.height * 100}%`,
    transform: box.angle_degrees ? `rotate(${box.angle_degrees}deg)` : undefined,
    transformOrigin: '50% 50%'
  };
}

function buildNode(
  nodeType: RuntimeNodeTypeSummary,
  existingNodes: RuntimeNodeSpec[],
  position: { x: number; y: number }
): RuntimeNodeSpec {
  const baseKey = nodeType.node_type.split('.').at(-1)?.replaceAll('-', '_') ?? 'node';
  let nodeKey = baseKey;
  let suffix = 2;
  const existing = new Set(existingNodes.map((node) => node.node_key));
  while (existing.has(nodeKey)) {
    nodeKey = `${baseKey}_${suffix}`;
    suffix += 1;
  }
  const taskType = requiredTaskTypeForNode(nodeType.node_type);
  return {
    node_key: nodeKey,
    node_type: nodeType.node_type,
    node_category: nodeType.node_category,
    node_contract_version: nodeType.contract_version,
    input_ports: nodeType.input_ports,
    output_ports: nodeType.output_ports,
    config: nodeType.default_config,
    logical_model_ref:
      nodeType.node_category === 'inference' ? defaultModelRef(taskType) : null,
    position: { x: Math.max(8, position.x), y: Math.max(8, position.y) }
  };
}

type ResponseMapping = {
  source_field: string;
  response_key: string;
  required: boolean;
};

type ResultFieldOption = {
  value: string;
  label: string;
  description?: string;
  nodeKey?: string;
  outputPort?: string | null;
  payloadType?: string | null;
  valueType?: RuntimePipelineOutputCatalogField['value_type'];
  aggregateOps?: string[];
  filterableFields?: string[];
  listItemFields?: string[];
};

type ProjectionFieldOption = Required<
  Pick<ResultFieldOption, 'nodeKey' | 'value' | 'label'>
> &
  Omit<ResultFieldOption, 'nodeKey' | 'value' | 'label'>;

type SiemensSpsDataType = 'BOOL' | 'BYTE' | 'WORD' | 'DWORD' | 'INT' | 'DINT' | 'REAL';
type SiemensSpsConversionMode = 'direct' | 'enum' | 'boolean' | 'scale' | 'threshold';

type SiemensSpsConnectionConfig = {
  name: string;
  host: string;
  port: number;
  rack: number;
  slot: number;
  connect_timeout_ms: number;
  write_timeout_ms: number;
  reconnect: 'disabled' | 'on_failure';
  reuse_policy: 'per_execution' | 'per_runtime';
  connection_ref: string | null;
};

type SiemensSpsMappingRow = {
  key: string;
  label: string;
  enabled: boolean;
  source: {
    field: string;
    extractor: 'nested_result_path' | 'top1_label' | 'top1_score' | 'numeric_field' | 'boolean_field' | 'string_field';
    advanced_path: string;
    allow_advanced_path: boolean;
  };
  target: {
    area: 'DB';
    db_number: number;
    byte_offset: number;
    bit_offset: number | null;
    data_type: SiemensSpsDataType;
  };
  conversion: {
    mode: SiemensSpsConversionMode;
    enum_values: { source: string; value: boolean | number | string }[];
    true_value: boolean | number | string | null;
    false_value: boolean | number | string | null;
    multiplier: number;
    addend: number;
    rounding: 'none' | 'nearest' | 'floor' | 'ceil';
    clamp_min: number | null;
    clamp_max: number | null;
    operator: '>=' | '>' | '<=' | '<' | '==' | '!=';
    threshold: number;
    default_behavior: 'fail' | 'skip' | 'write_default';
    default_value: boolean | number | string | null;
  };
  condition: {
    mode: 'always' | 'when_field';
    field: string;
    operator: 'truthy' | 'falsy' | '==' | '!=' | '>=' | '>' | '<=' | '<';
    value: boolean | number | string | null;
  };
  failure_behavior: 'inherit' | 'fail_pipeline' | 'warn_continue' | 'skip_mapping';
};

type EdgeRoleMapping = {
  feature_class: string;
  edge_role: string;
  bbox_side: string;
};

type MeasurementRule = {
  feature_type: string;
  bases: string[];
};

type ClearanceRule = {
  feature_type: string;
  enabled: boolean;
  measurement_basis: string;
  multiplier: number;
  minimum_clearance_px: number;
  maximum_clearance_px: number | null;
  apply_before: boolean;
  apply_after: boolean;
  merge_overlapping_zones: boolean;
  severity: string;
  category: string;
};

type CvLineSelectionRule = {
  edge_role: string;
  target_angle_degrees: number;
  angle_tolerance_degrees: number;
  minimum_line_length_px: number;
  region_preference: string;
  nearest_to_image_side: string;
  ranking: string;
  required: boolean;
};

type QualityRule = {
  rule_type: string;
  feature_type: string;
  measurement_basis: string;
  operator: string;
  threshold: number;
  status_on_fail: string;
};

type SpatialRule = {
  rule_key: string;
  relation: string;
  enabled: boolean;
  primary_labels: string[];
  secondary_labels: string[];
  label_pairing: string;
  max_distance_percent: number | null;
  min_distance_percent: number | null;
  min_overlap_percent: number;
  min_closing_percent: number;
  zone_key: string;
  zone_match: string;
  min_count: number;
  max_count: number | null;
  signal_label: string;
};

type ColorProfile = {
  label: string;
  enabled: boolean;
  color_space: string;
  color_tolerance_percent: number;
  hue_min: number;
  hue_max: number;
  saturation_min: number;
  saturation_max: number;
  value_min: number;
  value_max: number;
  min_area_percent: number;
  max_area_percent: number | null;
  min_aspect_ratio: number;
  display_color: string;
};

type DirectionReference = {
  label: string;
  start: { x: number; y: number };
  end: { x: number; y: number };
};

const edgeRoleOptions = ['start_edge', 'side_edge', 'left_edge', 'right_edge', 'custom'];
const bboxSideOptions = ['left', 'right', 'top', 'bottom', 'longest_side'];
const measurementBasisOptions = [
  'bbox_width',
  'bbox_height',
  'approximate_diameter',
  'polygon_length',
  'mask_area',
  'axis_projected_width',
  'axis_projected_length'
];
const metricApplyOptions = [
  'feature_measurements',
  'clearance_zones',
  'free_region',
  'selected_result_fields'
];
const cvRegionOptions = ['any', 'left', 'right', 'top', 'bottom', 'center'];
const cvSideOptions = ['none', 'left', 'right', 'top', 'bottom'];
const cvRankingOptions = ['strongest_longest', 'longest', 'nearest_to_side'];
const qualityRuleTypeOptions = [
  'feature_count_by_type',
  'feature_size_by_type',
  'total_feature_length_area_by_type',
  'free_region_distance'
];
const qualityOperatorOptions = ['<=', '<', '>=', '>', '==', '!='];
const qualityStatusOptions = ['fail', 'warning'];
const spatialRelationOptions = ['distance', 'approaching', 'overlap', 'zone_occupancy'];
const spatialLabelPairingOptions = ['any', 'same_label', 'different_label'];
const spatialZoneMatchOptions = ['center_inside', 'box_overlaps', 'fully_inside'];
const colorSpaceOptions = ['hsv'];
const defaultColorTolerancePercent = 55;
const neutralColorSaturationCutoff = 105;
const siemensSpsDataTypes: SiemensSpsDataType[] = ['BOOL', 'BYTE', 'WORD', 'DWORD', 'INT', 'DINT', 'REAL'];
const siemensSpsConversionModes: SiemensSpsConversionMode[] = ['direct', 'enum', 'boolean', 'scale', 'threshold'];
const resultProjectionDestinations: RuntimeProjectionDestination[] = [
  'response',
  'history',
  'live_view',
  'overlay'
];
const resultProjectionAggregateOps: RuntimeResultProjection['aggregate']['op'][] = [
  'none',
  'count',
  'list',
  'first',
  'any',
  'all',
  'sum',
  'avg',
  'min',
  'max'
];
const projectionFilterOps = [
  'exists',
  'not_exists',
  'eq',
  'ne',
  'gt',
  'gte',
  'lt',
  'lte',
  'contains',
  'truthy',
  'falsy'
] as const;

export function normalizeResultProjections(value: unknown): RuntimeResultProjection[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item, index) => {
      const source = recordValue(item.source);
      const aggregate = recordValue(item.aggregate);
      const decision = recordValue(item.decision);
      const key = slugKey(String(item.key || item.label || `result_${index + 1}`)) || `result_${index + 1}`;
      return {
        key,
        label: typeof item.label === 'string' && item.label.trim() ? item.label : null,
        source: {
          node_key: stringOrNull(source.node_key),
          output_port: stringOrNull(source.output_port),
          path: String(source.path ?? ''),
          value_type: normalizeProjectionValueType(source.value_type)
        },
        filters: Array.isArray(item.filters)
          ? item.filters
              .filter((filter): filter is Record<string, unknown> => typeof filter === 'object' && filter !== null)
              .map((filter) => ({
                path: String(filter.path ?? ''),
                op: normalizeProjectionFilterOp(filter.op),
                value: filter.value ?? null
              }))
          : [],
        aggregate: {
          op: normalizeProjectionAggregateOp(aggregate.op),
          path: stringOrNull(aggregate.path)
        },
        fields: Array.isArray(item.fields)
          ? item.fields
              .filter((field): field is Record<string, unknown> => typeof field === 'object' && field !== null)
              .map((field) => ({
                source_path: String(field.source_path ?? ''),
                output_key: slugKey(String(field.output_key || field.source_path || 'value')) || 'value',
                required: Boolean(field.required),
                default_value: field.default_value ?? null
              }))
              .filter((field) => field.source_path)
          : [],
        decision:
          decision && Object.keys(decision).length
            ? {
                op: normalizeProjectionFilterOp(decision.op),
                value: decision.value ?? 0,
                when_true: decision.when_true ?? 'FAIL',
                when_false: decision.when_false ?? 'PASS'
              }
            : null,
        destinations: normalizeProjectionDestinations(item.destinations),
        required: Boolean(item.required),
        default_value: item.default_value ?? null
      };
    });
}

function recordValue(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function normalizeProjectionValueType(value: unknown): RuntimeResultProjection['source']['value_type'] {
  const allowed: RuntimeResultProjection['source']['value_type'][] = [
    'auto',
    'scalar',
    'number',
    'boolean',
    'text',
    'object',
    'object_list',
    'list',
    'map'
  ];
  return allowed.includes(value as RuntimeResultProjection['source']['value_type'])
    ? (value as RuntimeResultProjection['source']['value_type'])
    : 'auto';
}

function normalizeProjectionAggregateOp(value: unknown): RuntimeResultProjection['aggregate']['op'] {
  return resultProjectionAggregateOps.includes(value as RuntimeResultProjection['aggregate']['op'])
    ? (value as RuntimeResultProjection['aggregate']['op'])
    : 'none';
}

function normalizeProjectionFilterOp(value: unknown): RuntimeResultProjection['filters'][number]['op'] {
  return (projectionFilterOps as readonly string[]).includes(String(value))
    ? (value as RuntimeResultProjection['filters'][number]['op'])
    : 'eq';
}

function normalizeProjectionDestinations(value: unknown): RuntimeProjectionDestination[] {
  if (!Array.isArray(value)) {
    return ['response', 'history'];
  }
  const destinations = value.filter((item): item is RuntimeProjectionDestination =>
    resultProjectionDestinations.includes(item as RuntimeProjectionDestination)
  );
  return destinations.length ? uniqueDestinations(destinations) : ['response', 'history'];
}

function uniqueDestinations(destinations: RuntimeProjectionDestination[]) {
  return Array.from(new Set(destinations));
}

function projectionFieldOptionsForNode(
  graph: RuntimePipelineGraph,
  node: RuntimeNodeSpec,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>,
  outputCatalog: RuntimePipelineOutputCatalog | null
): ProjectionFieldOption[] {
  const catalogOptions = outputCatalog
    ? catalogProjectionOptions(graph, node, nodeTypes, outputCatalog)
    : [];
  const fallbackOptions = resultFieldOptionsForNode(graph, node, nodeTypes)
    .filter((option) => option.value)
    .map((option) => ({
      ...option,
      nodeKey: option.nodeKey ?? fallbackSourceNodeKey(graph, node) ?? '',
      outputPort: option.outputPort ?? null,
      payloadType: option.payloadType ?? null,
      valueType: option.valueType ?? 'auto',
      aggregateOps: option.aggregateOps ?? resultProjectionAggregateOps,
      filterableFields: option.filterableFields ?? [],
      listItemFields: option.listItemFields ?? []
    }))
    .filter((option) => option.nodeKey);
  return uniqueProjectionFieldOptions([...catalogOptions, ...fallbackOptions]);
}

function catalogProjectionOptions(
  graph: RuntimePipelineGraph,
  node: RuntimeNodeSpec,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>,
  outputCatalog: RuntimePipelineOutputCatalog
): ProjectionFieldOption[] {
  const sourceKeys = sourceNodeKeysForNode(graph, node);
  const nodeLabel = (nodeKey: string) => {
    const sourceNode = graph.nodes.find((candidate) => candidate.node_key === nodeKey);
    const sourceType = sourceNode ? nodeTypes.get(sourceNode.node_type) : undefined;
    return sourceType?.display_name ?? sourceNode?.node_type ?? nodeKey;
  };
  return outputCatalog.entries
    .filter((entry) => sourceKeys.has(entry.node_key))
    .flatMap((entry) => catalogEntryFields(entry).map((field) => ({
      value: field.key,
      label: `${nodeLabel(entry.node_key)} / ${field.label}`,
      description: entry.payload_type ? `${entry.payload_type} from ${entry.node_key}` : entry.node_key,
      nodeKey: entry.node_key,
      outputPort: entry.output_port ?? null,
      payloadType: entry.payload_type ?? null,
      valueType: field.value_type,
      aggregateOps: field.aggregate_ops.length ? field.aggregate_ops : resultProjectionAggregateOps,
      filterableFields: field.filterable_fields,
      listItemFields: field.list_item_fields
    })));
}

function catalogEntryFields(entry: RuntimePipelineOutputCatalogEntry): RuntimePipelineOutputCatalogField[] {
  if (entry.field_options.length) {
    return entry.field_options;
  }
  return entry.fields.map((field) => ({
    key: field,
    label: projectionFieldLabel(field),
    value_type: 'auto',
    aggregate_ops: [],
    filterable_fields: [],
    list_item_fields: []
  }));
}

function sourceNodeKeysForNode(graph: RuntimePipelineGraph, node: RuntimeNodeSpec) {
  const incoming = graph.edges
    .filter((edge) => edge.to_node_key === node.node_key)
    .map((edge) => edge.from_node_key);
  if (incoming.length) {
    return new Set(incoming);
  }
  return new Set(
    graph.nodes
      .filter((candidate) => candidate.node_key !== node.node_key && candidate.node_category !== 'output')
      .map((candidate) => candidate.node_key)
  );
}

function fallbackSourceNodeKey(graph: RuntimePipelineGraph, node: RuntimeNodeSpec) {
  return Array.from(sourceNodeKeysForNode(graph, node))[0] ?? null;
}

function projectionOptionKey(option: Pick<ProjectionFieldOption, 'nodeKey' | 'outputPort' | 'value'>) {
  return `${option.nodeKey}::${option.outputPort ?? ''}::${option.value}`;
}

function uniqueProjectionFieldOptions(options: ProjectionFieldOption[]) {
  const seen = new Map<string, ProjectionFieldOption>();
  for (const option of options) {
    const key = projectionOptionKey(option);
    if (!seen.has(key)) {
      seen.set(key, option);
    }
  }
  return Array.from(seen.values());
}

function projectionSourceKey(projection: RuntimeResultProjection) {
  return `${projection.source.node_key ?? ''}::${projection.source.output_port ?? ''}::${projection.source.path}`;
}

function projectionAggregateOpsForOption(
  option: ProjectionFieldOption | null,
  outputCatalog: RuntimePipelineOutputCatalog | null
): RuntimeResultProjection['aggregate']['op'][] {
  const sourceOps = option?.aggregateOps?.length ? option.aggregateOps : outputCatalog?.aggregate_ops;
  const ops = (sourceOps ?? resultProjectionAggregateOps).filter((op): op is RuntimeResultProjection['aggregate']['op'] =>
    resultProjectionAggregateOps.includes(op as RuntimeResultProjection['aggregate']['op'])
  );
  return ops.length ? ops : resultProjectionAggregateOps;
}

function nextProjectionAggregate(
  projection: RuntimeResultProjection,
  option: ProjectionFieldOption,
  outputCatalog: RuntimePipelineOutputCatalog | null
): RuntimeResultProjection['aggregate'] {
  const ops = projectionAggregateOpsForOption(option, outputCatalog);
  const currentOp = ops.includes(projection.aggregate.op) ? projection.aggregate.op : suggestedAggregateOp(option);
  return {
    op: ops.includes(currentOp) ? currentOp : ops[0] ?? 'none',
    path: aggregateNeedsPath(currentOp) ? projection.aggregate.path ?? option.listItemFields?.[0] ?? null : null
  };
}

function suggestedAggregateOp(option: ProjectionFieldOption): RuntimeResultProjection['aggregate']['op'] {
  if (option.valueType === 'object_list' || option.valueType === 'list') {
    return 'count';
  }
  return 'none';
}

function fieldsCompatibleWithOption(
  fields: RuntimeResultProjection['fields'],
  option: ProjectionFieldOption
) {
  const allowed = new Set(option.listItemFields ?? []);
  if (!allowed.size) {
    return [];
  }
  return fields.filter((field) => allowed.has(field.source_path));
}

function aggregateNeedsPath(op: RuntimeResultProjection['aggregate']['op']) {
  return ['sum', 'avg', 'min', 'max', 'any', 'all'].includes(op);
}

function blankResultProjection(
  option: ProjectionFieldOption | null,
  index: number
): RuntimeResultProjection {
  const key = option ? suggestedResponseKey(option.value) : `result_${index + 1}`;
  return {
    key: slugKey(key) || `result_${index + 1}`,
    label: option?.label ?? null,
    source: {
      node_key: option?.nodeKey ?? null,
      output_port: option?.outputPort ?? null,
      path: option?.value ?? '',
      value_type: option?.valueType ?? 'auto'
    },
    filters: [],
    aggregate: {
      op: option ? suggestedAggregateOp(option) : 'none',
      path: null
    },
    fields: [],
    decision: null,
    destinations: ['response', 'history'],
    required: false,
    default_value: null
  };
}

function toggleDestination(
  destinations: RuntimeProjectionDestination[],
  destination: RuntimeProjectionDestination,
  enabled: boolean
): RuntimeProjectionDestination[] {
  const next = enabled
    ? uniqueDestinations([...destinations, destination])
    : destinations.filter((item) => item !== destination);
  return next.length ? next : ['history'];
}

function aggregateLabel(op: RuntimeResultProjection['aggregate']['op']) {
  const labels: Record<RuntimeResultProjection['aggregate']['op'], string> = {
    none: 'Raw value',
    count: 'Count',
    list: 'List',
    first: 'First',
    any: 'Any true',
    all: 'All true',
    sum: 'Sum',
    avg: 'Average',
    min: 'Minimum',
    max: 'Maximum'
  };
  return labels[op];
}

function destinationLabel(destination: RuntimeProjectionDestination) {
  const labels: Record<RuntimeProjectionDestination, string> = {
    response: 'Response',
    history: 'History',
    live_view: 'Live view',
    overlay: 'Overlay',
    plc: 'PLC',
    connector: 'Connector'
  };
  return labels[destination] ?? destination;
}

function projectionFieldLabel(field: string) {
  return field.replace(/[._]+/g, ' ').replace(/\b\w/g, (char) => char.toUpperCase());
}

function filterOperatorLabel(op: RuntimeResultProjection['filters'][number]['op']) {
  const labels: Record<RuntimeResultProjection['filters'][number]['op'], string> = {
    exists: 'Exists',
    not_exists: 'Does not exist',
    eq: 'Equals',
    ne: 'Not equal',
    gt: 'Greater than',
    gte: 'At least',
    lt: 'Less than',
    lte: 'At most',
    contains: 'Contains',
    in: 'In list',
    not_in: 'Not in list',
    truthy: 'Is true',
    falsy: 'Is false'
  };
  return labels[op] ?? op;
}

function filterOpHasNoValue(op: RuntimeResultProjection['filters'][number]['op']) {
  return ['exists', 'not_exists', 'truthy', 'falsy'].includes(op);
}

function parseProjectionLiteral(value: string): unknown {
  const trimmed = value.trim();
  if (!trimmed) {
    return '';
  }
  if (trimmed === 'true') {
    return true;
  }
  if (trimmed === 'false') {
    return false;
  }
  const numeric = Number(trimmed);
  return Number.isFinite(numeric) && trimmed !== '' ? numeric : trimmed;
}

function projectionPreviewValue(projection: RuntimeResultProjection): unknown {
  if (projection.decision) {
    return projection.decision.when_true;
  }
  if (projection.aggregate.op === 'count') {
    return 3;
  }
  if (projection.aggregate.op === 'list') {
    if (projection.fields.length) {
      return [Object.fromEntries(projection.fields.map((field) => [field.output_key, `sample_${field.output_key}`]))];
    }
    return ['sample'];
  }
  if (projection.source.value_type === 'boolean') {
    return true;
  }
  if (projection.source.value_type === 'number') {
    return 1;
  }
  return `sample_${projection.key}`;
}

export function resultFieldOptionsForNode(
  graph: RuntimePipelineGraph,
  node: RuntimeNodeSpec,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>,
  authoringContext: RuntimeChannelAuthoringContext | null = null,
  outputCatalog: RuntimePipelineOutputCatalog | null = null
): ResultFieldOption[] {
  const catalogOptions = outputCatalog ? catalogProjectionOptions(graph, node, nodeTypes, outputCatalog) : [];
  const labels = pipelineObjectLabels(graph, authoringContext);
  const incomingEdges = graph.edges.filter((edge) => edge.to_node_key === node.node_key);
  const sourceEdges = incomingEdges.length
    ? incomingEdges
    : graph.nodes
        .filter((candidate) => candidate.node_key !== node.node_key && candidate.node_category !== 'output')
        .map((candidate) => ({
          from_node_key: candidate.node_key,
          from_port: candidate.output_ports[0]?.key ?? '',
          to_node_key: node.node_key,
          to_port: 'result'
        }));
  const options = [
    fieldOption('result', 'Decision result', 'Boolean decision emitted by the upstream node.'),
    fieldOption('decision', 'Decision alias', 'Same boolean decision as result.')
  ];
  for (const edge of sourceEdges) {
    const sourceNode = graph.nodes.find((candidate) => candidate.node_key === edge.from_node_key);
    if (!sourceNode) {
      continue;
    }
    const sourceType = nodeTypes.get(sourceNode.node_type);
    const outputPort =
      sourceNode.output_ports.find((port) => port.key === edge.from_port) ??
      sourceType?.output_ports.find((port) => port.key === edge.from_port);
    options.push(...resultFieldOptionsForSource(sourceNode, sourceType, outputPort?.data_type ?? '', labels));
  }
  return uniqueResultFieldOptions([...catalogOptions, ...options]);
}

function resultFieldOptionsForSource(
  node: RuntimeNodeSpec,
  nodeType: RuntimeNodeTypeSummary | undefined,
  dataType: string,
  pipelineLabels: string[]
): ResultFieldOption[] {
  const source = nodeType?.display_name ?? node.node_type;
  const option = (value: string, label: string, description?: string) =>
    fieldOption(value, label, description ? `${description} Source: ${source}.` : `Source: ${source}.`);
  if (node.node_type === 'analysis.object_counting') {
    return objectCountingFieldOptions(node, pipelineLabels).map((item) =>
      option(item.value, item.label, item.description)
    );
  }
  if (node.node_type === 'analysis.zone_detection') {
    return [
      option('signal', 'Zone signal'),
      option('active', 'Zone active'),
      option('match_count', 'Zone match count'),
      option('zone_count', 'Configured zone count'),
      option('inside_count', 'Objects inside zones'),
      option('outside_count', 'Objects outside zones'),
      option('zones', 'Zone summaries'),
      option('outside_objects', 'Outside objects'),
      option('matches', 'Zone requirement matches'),
      option('rules', 'Zone rule results')
    ];
  }
  if (node.node_type === 'analysis.close_detection' || node.node_type === 'analysis.object_spatial_signal') {
    return [
      option('signal', 'Close signal'),
      option('active', 'Close active'),
      option('match_count', 'Close match count'),
      option('matches', 'Matched object pairs'),
      option('rules', 'Close rule results'),
      option('match_policy', 'Match policy')
    ];
  }
  if (node.node_type === 'inference.classification' || dataType === 'classifications') {
    return [
      option('classification.top1.label', 'Classification top1 label'),
      option('classification.top1.score', 'Classification top1 score'),
      option('class', 'Class'),
      option('classification', 'Classification'),
      option('score', 'Score'),
      option('top_label.label', 'Top label'),
      option('top_label.score', 'Top score'),
      option('labels', 'All labels'),
      option('count', 'Label count'),
      option('image_context_id', 'Image context')
    ];
  }
  if (node.node_type === 'inference.detection' || dataType === 'detections') {
    return [
      option('detection.count', 'Detection count'),
      option('count', 'Object count'),
      option('detections', 'Detections'),
      option('objects', 'Objects'),
      option('image_context_id', 'Image context')
    ];
  }
  if (node.node_type === 'inference.segmentation' || dataType === 'segments') {
    return [
      option('count', 'Segment count'),
      option('segments', 'Segments'),
      option('objects', 'Objects'),
      option('image_context_id', 'Image context')
    ];
  }
  if (node.node_type === 'inference.anomaly' || dataType === 'anomaly') {
    return [
      option('anomaly.score', 'Anomaly score'),
      option('anomaly.decision', 'Anomaly decision'),
      option('anomaly_score', 'Anomaly score'),
      option('predicted_label', 'Predicted label'),
      option('is_anomalous', 'Is anomalous'),
      option('threshold', 'Threshold'),
      option('image_context_id', 'Image context')
    ];
  }
  if (dataType === 'measurements') {
    return [option('count', 'First numeric measurement')];
  }
  if (dataType === 'feature_measurements') {
    return [
      option('feature_count', 'Feature count'),
      option('feature_measurements', 'Feature measurements'),
      option('features', 'Feature summaries')
    ];
  }
  if (dataType === 'free_region') {
    return [
      option('free_region_found', 'Free region found'),
      option('distance_px', 'Distance px'),
      option('measurement.distance_mm', 'Measurement distance mm'),
      option('distance_mm', 'Distance mm'),
      option('start_position_px', 'Start position px'),
      option('start_position_mm', 'Start position mm')
    ];
  }
  if (dataType === 'quality_decision') {
    return [
      option('quality.decision', 'Quality decision'),
      option('quality.pass', 'Quality pass'),
      option('status', 'Quality status'),
      option('result_label', 'Result label'),
      option('result', 'Quality result'),
      option('messages', 'Messages'),
      option('evaluated_rules', 'Evaluated rules')
    ];
  }
  if (dataType === 'cv_edges') {
    return [
      option('edge_pixel_count', 'Edge pixels'),
      option('contour_count', 'Contour count'),
      option('line_candidate_count', 'Line candidate count'),
      option('image_context_id', 'Image context')
    ];
  }
  if (dataType === 'frame') {
    return [option('frame', 'Frame summary')];
  }
  return [
    option('count', 'Count'),
    option('objects', 'Objects'),
    option('matches', 'Matches'),
    option('image_context_id', 'Image context')
  ];
}

function objectCountingFieldOptions(
  node: RuntimeNodeSpec,
  pipelineLabels: string[]
): ResultFieldOption[] {
  const groupBy = String(node.config.group_by || 'label');
  if (groupBy === 'total') {
    return [
      fieldOption('count_total', 'Total object count'),
      fieldOption('count', 'Total object count fallback')
    ];
  }
  if (groupBy === 'zone') {
    const zones = normalizeBoxList(node.config.zones).map((zone, index) => zone.label || `zone_${index + 1}`);
    const zoneFields = zones.map((zone) => fieldOption(`count_${zone}`, `Count ${zone}`));
    return [
      ...zoneFields,
      fieldOption('count_outside', 'Count outside zones'),
      fieldOption('count', 'First zone count fallback')
    ];
  }
  const labelFields = pipelineLabels.map((label) => fieldOption(`count_${label}`, `Count ${label}`));
  return [
    ...labelFields,
    fieldOption('count', 'First class count fallback')
  ];
}

function pipelineObjectLabels(
  graph: RuntimePipelineGraph,
  authoringContext: RuntimeChannelAuthoringContext | null = null
) {
  const labels = new Set<string>(authoringContext?.labels ?? []);
  for (const node of graph.nodes) {
    for (const rule of normalizeSpatialRules(node.config.rules)) {
      for (const label of [...rule.primary_labels, ...rule.secondary_labels]) {
        if (label) {
          labels.add(label);
        }
      }
    }
    for (const profile of normalizeColorProfiles(node.config.color_profiles)) {
      if (profile.label) {
        labels.add(profile.label);
      }
    }
    for (const label of normalizeRuleLabels(node.config.labels)) {
      labels.add(label);
    }
  }
  return Array.from(labels).sort((first, second) => first.localeCompare(second));
}

function authoringLabelOptions(
  graph: RuntimePipelineGraph,
  authoringContext: RuntimeChannelAuthoringContext | null
) {
  return pipelineObjectLabels(graph, authoringContext);
}

function fieldOption(value: string, label: string, description?: string): ResultFieldOption {
  return { value, label, description };
}

function uniqueResultFieldOptions(options: ResultFieldOption[]) {
  const seen = new Map<string, ResultFieldOption>();
  for (const option of options) {
    if (option.value && !seen.has(option.value)) {
      seen.set(option.value, option);
    }
  }
  return Array.from(seen.values());
}

function firstUnusedResultField(options: ResultFieldOption[], mappings: ResponseMapping[]) {
  const used = new Set(mappings.map((mapping) => mapping.source_field).filter(Boolean));
  return options.find((option) => !used.has(option.value))?.value ?? '';
}

export function suggestedResponseKey(sourceField: string) {
  if (sourceField === 'top_label.label' || sourceField === 'classification') {
    return 'class';
  }
  if (sourceField === 'top_label.score') {
    return 'score';
  }
  return sourceField.replace(/\./g, '_');
}

export function normalizeResponseMappings(value: unknown): ResponseMapping[] {
  if (Array.isArray(value)) {
    return value
      .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
      .map((item) => {
        const sourceField = String(item.source_field ?? '');
        return {
          source_field: sourceField,
          response_key: String(item.response_key ?? sourceField),
          required: Boolean(item.required)
        };
      });
  }
  if (typeof value === 'string') {
    return value
      .split(/[,\n]+/)
      .map((item) => item.trim())
      .filter(Boolean)
      .map((item) => ({ source_field: item, response_key: item, required: false }));
  }
  return [];
}

function defaultSiemensSpsConnection(): SiemensSpsConnectionConfig {
  return {
    name: 'Siemens SPS',
    host: '',
    port: 102,
    rack: 0,
    slot: 1,
    connect_timeout_ms: 1000,
    write_timeout_ms: 1000,
    reconnect: 'on_failure',
    reuse_policy: 'per_execution',
    connection_ref: null
  };
}

function normalizeSiemensSpsConnection(value: unknown): SiemensSpsConnectionConfig {
  if (typeof value !== 'object' || value === null) {
    return defaultSiemensSpsConnection();
  }
  const record = value as Record<string, unknown>;
  return {
    name: String(record.name ?? 'Siemens SPS'),
    host: String(record.host ?? ''),
    port: clampNumber(Math.round(finiteNumber(record.port, 102)), 1, 65535),
    rack: clampNumber(Math.round(finiteNumber(record.rack, 0)), 0, 7),
    slot: clampNumber(Math.round(finiteNumber(record.slot, 1)), 0, 31),
    connect_timeout_ms: clampNumber(Math.round(finiteNumber(record.connect_timeout_ms, 1000)), 50, 30000),
    write_timeout_ms: clampNumber(Math.round(finiteNumber(record.write_timeout_ms, 1000)), 50, 30000),
    reconnect: record.reconnect === 'disabled' ? 'disabled' : 'on_failure',
    reuse_policy: record.reuse_policy === 'per_runtime' ? 'per_runtime' : 'per_execution',
    connection_ref: typeof record.connection_ref === 'string' && record.connection_ref ? record.connection_ref : null
  };
}

function blankSiemensSpsMapping(sourceField: string): SiemensSpsMappingRow {
  return {
    key: sourceField ? slugKey(sourceField) : '',
    label: sourceField ? suggestedResponseKey(sourceField) : '',
    enabled: true,
    source: {
      field: sourceField,
      extractor: 'nested_result_path',
      advanced_path: '',
      allow_advanced_path: false
    },
    target: {
      area: 'DB',
      db_number: 101,
      byte_offset: 0,
      bit_offset: null,
      data_type: 'INT'
    },
    conversion: {
      mode: 'direct',
      enum_values: [],
      true_value: null,
      false_value: null,
      multiplier: 1,
      addend: 0,
      rounding: 'none',
      clamp_min: null,
      clamp_max: null,
      operator: '>=',
      threshold: 0.5,
      default_behavior: 'fail',
      default_value: null
    },
    condition: {
      mode: 'always',
      field: '',
      operator: 'truthy',
      value: null
    },
    failure_behavior: 'inherit'
  };
}

export function normalizeSiemensSpsMappings(value: unknown): SiemensSpsMappingRow[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item, index) => {
      const source = normalizeSpsRecord(item.source);
      const target = normalizeSpsRecord(item.target);
      const conversion = normalizeSpsRecord(item.conversion);
      const condition = normalizeSpsRecord(item.condition);
      const sourceField = String(source.field ?? item.source_field ?? '');
      const dataType = siemensSpsDataTypes.includes(String(target.data_type) as SiemensSpsDataType)
        ? (String(target.data_type) as SiemensSpsDataType)
        : 'INT';
      const mode = siemensSpsConversionModes.includes(String(conversion.mode) as SiemensSpsConversionMode)
        ? (String(conversion.mode) as SiemensSpsConversionMode)
        : 'direct';
      return {
        key: String(item.key ?? slugKey(sourceField || `mapping_${index + 1}`)),
        label: String(item.label ?? suggestedResponseKey(sourceField)),
        enabled: item.enabled !== false,
        source: {
          field: sourceField,
          extractor: isSpsExtractor(source.extractor) ? source.extractor : 'nested_result_path',
          advanced_path: String(source.advanced_path ?? ''),
          allow_advanced_path: Boolean(source.allow_advanced_path)
        },
        target: {
          area: 'DB',
          db_number: clampNumber(Math.round(finiteNumber(target.db_number, 101)), 1, 65535),
          byte_offset: clampNumber(Math.round(finiteNumber(target.byte_offset, 0)), 0, 65535),
          bit_offset:
            dataType === 'BOOL'
              ? clampNumber(Math.round(finiteNumber(target.bit_offset, 0)), 0, 7)
              : null,
          data_type: dataType
        },
        conversion: {
          mode,
          enum_values: Array.isArray(conversion.enum_values)
            ? conversion.enum_values
                .filter((entry): entry is Record<string, unknown> => typeof entry === 'object' && entry !== null)
                .map((entry) => ({
                  source: String(entry.source ?? ''),
                  value: parsePlcLiteral(String(entry.value ?? '0'))
                }))
            : [],
          true_value: parseNullablePlcLiteral(conversion.true_value),
          false_value: parseNullablePlcLiteral(conversion.false_value),
          multiplier: finiteNumber(conversion.multiplier, 1),
          addend: finiteNumber(conversion.addend, 0),
          rounding: ['none', 'nearest', 'floor', 'ceil'].includes(String(conversion.rounding))
            ? (String(conversion.rounding) as SiemensSpsMappingRow['conversion']['rounding'])
            : 'none',
          clamp_min: nullableFiniteNumber(conversion.clamp_min, null),
          clamp_max: nullableFiniteNumber(conversion.clamp_max, null),
          operator: ['>=', '>', '<=', '<', '==', '!='].includes(String(conversion.operator))
            ? (String(conversion.operator) as SiemensSpsMappingRow['conversion']['operator'])
            : '>=',
          threshold: finiteNumber(conversion.threshold, 0.5),
          default_behavior: ['fail', 'skip', 'write_default'].includes(String(conversion.default_behavior))
            ? (String(conversion.default_behavior) as SiemensSpsMappingRow['conversion']['default_behavior'])
            : 'fail',
          default_value: parseNullablePlcLiteral(conversion.default_value)
        },
        condition: {
          mode: condition.mode === 'when_field' ? 'when_field' : 'always',
          field: String(condition.field ?? ''),
          operator: ['truthy', 'falsy', '==', '!=', '>=', '>', '<=', '<'].includes(String(condition.operator))
            ? (String(condition.operator) as SiemensSpsMappingRow['condition']['operator'])
            : 'truthy',
          value: parseNullablePlcLiteral(condition.value)
        },
        failure_behavior: ['inherit', 'fail_pipeline', 'warn_continue', 'skip_mapping'].includes(
          String(item.failure_behavior)
        )
          ? (String(item.failure_behavior) as SiemensSpsMappingRow['failure_behavior'])
          : 'inherit'
      };
    });
}

function normalizeSpsRecord(value: unknown): Record<string, unknown> {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : {};
}

function isSpsExtractor(value: unknown): value is SiemensSpsMappingRow['source']['extractor'] {
  return ['nested_result_path', 'top1_label', 'top1_score', 'numeric_field', 'boolean_field', 'string_field'].includes(
    String(value)
  );
}

function firstSpsSourceField(options: ResultFieldOption[]) {
  return options.find((option) => option.value !== 'result' && option.value !== 'decision')?.value ?? '';
}

function parsePlcLiteral(value: unknown): boolean | number | string {
  if (typeof value === 'boolean' || typeof value === 'number') {
    return value;
  }
  const raw = String(value).trim();
  if (raw.toLowerCase() === 'true') {
    return true;
  }
  if (raw.toLowerCase() === 'false') {
    return false;
  }
  if (raw && Number.isFinite(Number(raw))) {
    return Number(raw);
  }
  return raw;
}

function parseNullablePlcLiteral(value: unknown): boolean | number | string | null {
  if (value === null || value === undefined || value === '') {
    return null;
  }
  return parsePlcLiteral(value);
}

function formatSiemensSpsAddress(target: SiemensSpsMappingRow['target']) {
  if (target.data_type === 'BOOL') {
    return `DB${target.db_number}.DBX${target.byte_offset}.${target.bit_offset ?? 0}`;
  }
  if (target.data_type === 'BYTE') {
    return `DB${target.db_number}.DBB${target.byte_offset}`;
  }
  if (target.data_type === 'INT' || target.data_type === 'WORD') {
    return `DB${target.db_number}.DBW${target.byte_offset}`;
  }
  return `DB${target.db_number}.DBD${target.byte_offset}`;
}

function slugKey(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'mapping';
}

function blankEdgeRoleMapping(): EdgeRoleMapping {
  return { feature_class: '', edge_role: 'custom', bbox_side: 'longest_side' };
}

export function normalizeEdgeRoleMappings(value: unknown): EdgeRoleMapping[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => {
      const role = String(item.edge_role ?? item.role ?? 'custom');
      const side = String(item.bbox_side ?? 'longest_side');
      return {
        feature_class: String(item.feature_class ?? item.class_name ?? ''),
        edge_role: edgeRoleOptions.includes(role) ? role : 'custom',
        bbox_side: bboxSideOptions.includes(side) ? side : 'longest_side'
      };
    });
}

function blankMeasurementRule(): MeasurementRule {
  return { feature_type: '', bases: ['bbox_width'] };
}

export function normalizeMeasurementRules(value: unknown): MeasurementRule[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => {
      const rawBases = Array.isArray(item.bases)
        ? item.bases
        : Array.isArray(item.measurement_bases)
          ? item.measurement_bases
          : [];
      const bases = rawBases
        .map((basis) => String(basis))
        .filter((basis) => measurementBasisOptions.includes(basis));
      return {
        feature_type: String(item.feature_type ?? ''),
        bases: bases.length ? bases : ['bbox_width']
      };
    });
}

function blankClearanceRule(): ClearanceRule {
  return {
    feature_type: '',
    enabled: true,
    measurement_basis: 'bbox_width',
    multiplier: 1,
    minimum_clearance_px: 0,
    maximum_clearance_px: null,
    apply_before: true,
    apply_after: true,
    merge_overlapping_zones: true,
    severity: 'warning',
    category: 'defect_clearance'
  };
}

export function normalizeClearanceRules(value: unknown): ClearanceRule[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => {
      const basis = String(item.measurement_basis ?? 'bbox_width');
      return {
        feature_type: String(item.feature_type ?? ''),
        enabled: item.enabled !== false,
        measurement_basis: measurementBasisOptions.includes(basis) ? basis : 'bbox_width',
        multiplier: finiteNumber(item.multiplier, 1),
        minimum_clearance_px: finiteNumber(item.minimum_clearance_px, 0),
        maximum_clearance_px:
          item.maximum_clearance_px === null || item.maximum_clearance_px === undefined
            ? null
            : finiteNumber(item.maximum_clearance_px, 0),
        apply_before: item.apply_before !== false,
        apply_after: item.apply_after !== false,
        merge_overlapping_zones: item.merge_overlapping_zones !== false,
        severity: String(item.severity ?? 'warning'),
        category: String(item.category ?? 'defect_clearance')
      };
    });
}

function updateClearanceRule(
  rules: ClearanceRule[],
  index: number,
  key: keyof ClearanceRule,
  value: ClearanceRule[keyof ClearanceRule]
) {
  return rules.map((rule, candidateIndex) =>
    candidateIndex === index ? { ...rule, [key]: value } : rule
  );
}

function blankCvLineSelectionRule(): CvLineSelectionRule {
  return {
    edge_role: 'custom',
    target_angle_degrees: 0,
    angle_tolerance_degrees: 15,
    minimum_line_length_px: 25,
    region_preference: 'any',
    nearest_to_image_side: 'none',
    ranking: 'strongest_longest',
    required: false
  };
}

export function normalizeCvLineSelectionRules(value: unknown): CvLineSelectionRule[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => {
      const role = String(item.edge_role ?? item.role ?? 'custom');
      const region = String(item.region_preference ?? 'any');
      const side = String(item.nearest_to_image_side ?? 'none');
      const ranking = String(item.ranking ?? 'strongest_longest');
      return {
        edge_role: edgeRoleOptions.includes(role) ? role : 'custom',
        target_angle_degrees: clampNumber(finiteNumber(item.target_angle_degrees, role === 'start_edge' ? 90 : 0), 0, 179),
        angle_tolerance_degrees: clampNumber(finiteNumber(item.angle_tolerance_degrees, 15), 0, 90),
        minimum_line_length_px: Math.max(0, finiteNumber(item.minimum_line_length_px, 25)),
        region_preference: cvRegionOptions.includes(region) ? region : 'any',
        nearest_to_image_side: cvSideOptions.includes(side) ? side : 'none',
        ranking: cvRankingOptions.includes(ranking) ? ranking : 'strongest_longest',
        required: typeof item.required === 'boolean' ? item.required : ['start_edge', 'side_edge'].includes(role)
      };
    });
}

function updateCvLineSelectionRule(
  rules: CvLineSelectionRule[],
  index: number,
  key: keyof CvLineSelectionRule,
  value: CvLineSelectionRule[keyof CvLineSelectionRule]
) {
  return rules.map((rule, candidateIndex) =>
    candidateIndex === index ? { ...rule, [key]: value } : rule
  );
}

export function normalizeMetricApplyTo(value: unknown): string[] {
  const raw = Array.isArray(value)
    ? value
    : typeof value === 'string'
      ? value.split(/[,\n]+/)
      : [];
  return raw.map((item) => String(item).trim()).filter((item) => metricApplyOptions.includes(item));
}

function blankQualityRule(): QualityRule {
  return {
    rule_type: 'feature_count_by_type',
    feature_type: 'knot',
    measurement_basis: 'bbox_width',
    operator: '<=',
    threshold: 3,
    status_on_fail: 'warning'
  };
}

export function normalizeQualityRules(value: unknown): QualityRule[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => {
      const ruleType = String(item.rule_type ?? 'feature_count_by_type');
      const basis = String(item.measurement_basis ?? 'bbox_width');
      const operator = String(item.operator ?? '<=');
      const status = String(item.status_on_fail ?? 'warning');
      return {
        rule_type: qualityRuleTypeOptions.includes(ruleType) ? ruleType : 'feature_count_by_type',
        feature_type: String(item.feature_type ?? ''),
        measurement_basis: measurementBasisOptions.includes(basis) ? basis : 'bbox_width',
        operator: qualityOperatorOptions.includes(operator) ? operator : '<=',
        threshold: finiteNumber(item.threshold, 0),
        status_on_fail: qualityStatusOptions.includes(status) ? status : 'warning'
      };
    });
}

function updateQualityRule(
  rules: QualityRule[],
  index: number,
  key: keyof QualityRule,
  value: QualityRule[keyof QualityRule]
) {
  return rules.map((rule, candidateIndex) =>
    candidateIndex === index ? { ...rule, [key]: value } : rule
  );
}

function blankColorProfile(): ColorProfile {
  return applyColorPickerToProfile({
    label: 'object',
    enabled: true,
    color_space: 'hsv',
    color_tolerance_percent: defaultColorTolerancePercent,
    hue_min: 0,
    hue_max: 179,
    saturation_min: 0,
    saturation_max: 90,
    value_min: 130,
    value_max: 255,
    min_area_percent: 0.05,
    max_area_percent: null,
    min_aspect_ratio: 1,
    display_color: '#f3d7a5'
  });
}

export function normalizeColorProfiles(value: unknown): ColorProfile[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => {
      const colorSpace = String(item.color_space ?? 'hsv');
      const displayColor = normalizeCssColor(item.display_color, '#f3d7a5');
      const tolerance = clampNumber(
        finiteNumber(
          item.color_tolerance_percent ?? item.tolerance_percent ?? item.color_tolerance,
          defaultColorTolerancePercent
        ),
        0,
        100
      );
      return applyColorPickerToProfile({
        label: String(item.label ?? item.class_name ?? 'object'),
        enabled: item.enabled !== false,
        color_space: colorSpaceOptions.includes(colorSpace) ? colorSpace : 'hsv',
        color_tolerance_percent: tolerance,
        hue_min: clampNumber(finiteNumber(item.hue_min, 0), 0, 179),
        hue_max: clampNumber(finiteNumber(item.hue_max, 179), 0, 179),
        saturation_min: clampNumber(finiteNumber(item.saturation_min, 0), 0, 255),
        saturation_max: clampNumber(finiteNumber(item.saturation_max, 255), 0, 255),
        value_min: clampNumber(finiteNumber(item.value_min, 0), 0, 255),
        value_max: clampNumber(finiteNumber(item.value_max, 255), 0, 255),
        min_area_percent: Math.max(0, finiteNumber(item.min_area_percent, 0.05)),
        max_area_percent: nullableFiniteNumber(item.max_area_percent, null),
        min_aspect_ratio: Math.max(1, finiteNumber(item.min_aspect_ratio, 1)),
        display_color: displayColor
      });
    });
}

function updateColorProfile(
  profiles: ColorProfile[],
  index: number,
  key: keyof ColorProfile,
  value: ColorProfile[keyof ColorProfile]
) {
  return profiles.map((profile, candidateIndex) =>
    candidateIndex === index ? applyColorPickerToProfile({ ...profile, [key]: value }) : profile
  );
}

function applyColorPickerToProfile(profile: ColorProfile): ColorProfile {
  const displayColor = normalizeCssColor(profile.display_color, '#f3d7a5');
  const tolerance = clampNumber(finiteNumber(profile.color_tolerance_percent, defaultColorTolerancePercent), 0, 100);
  const hsv = rgbToOpenCvHsv(hexToRgb(displayColor));
  const hueTolerance = Math.round(4 + tolerance * 0.35);
  const saturationTolerance = Math.round(24 + tolerance * 1.35);
  const valueTolerance = Math.round(30 + tolerance * 1.55);
  const hueRange =
    hsv.saturation <= neutralColorSaturationCutoff
      ? { min: 0, max: 179 }
      : wrappedHueRange(hsv.hue, hueTolerance);
  return {
    ...profile,
    color_space: 'hsv',
    color_tolerance_percent: tolerance,
    display_color: displayColor,
    hue_min: hueRange.min,
    hue_max: hueRange.max,
    saturation_min: clampNumber(hsv.saturation - saturationTolerance, 0, 255),
    saturation_max: clampNumber(hsv.saturation + saturationTolerance, 0, 255),
    value_min: clampNumber(hsv.value - valueTolerance, 0, 255),
    value_max: clampNumber(hsv.value + valueTolerance, 0, 255)
  };
}

function wrappedHueRange(hue: number, tolerance: number) {
  return {
    min: positiveModulo(hue - tolerance, 180),
    max: positiveModulo(hue + tolerance, 180)
  };
}

function positiveModulo(value: number, modulo: number) {
  return ((value % modulo) + modulo) % modulo;
}

function hexToRgb(color: string) {
  return {
    red: parseInt(color.slice(1, 3), 16),
    green: parseInt(color.slice(3, 5), 16),
    blue: parseInt(color.slice(5, 7), 16)
  };
}

function rgbToOpenCvHsv(color: { red: number; green: number; blue: number }) {
  const red = color.red / 255;
  const green = color.green / 255;
  const blue = color.blue / 255;
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  const delta = max - min;
  let hueDegrees = 0;
  if (delta > 0) {
    if (max === red) {
      hueDegrees = 60 * (((green - blue) / delta) % 6);
    } else if (max === green) {
      hueDegrees = 60 * ((blue - red) / delta + 2);
    } else {
      hueDegrees = 60 * ((red - green) / delta + 4);
    }
  }
  if (hueDegrees < 0) {
    hueDegrees += 360;
  }
  return {
    hue: Math.round(hueDegrees / 2) % 180,
    saturation: Math.round((max === 0 ? 0 : delta / max) * 255),
    value: Math.round(max * 255)
  };
}

export function normalizeDirectionReference(value: unknown): DirectionReference {
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const start = normalizeDirectionPoint(record.start, { x: 0.15, y: 0.5 });
    const end = normalizeDirectionPoint(record.end, { x: 0.85, y: 0.5 });
    return {
      label: String(record.label ?? 'Expected direction'),
      start,
      end: start.x === end.x && start.y === end.y ? { x: 0.85, y: 0.5 } : end
    };
  }
  return {
    label: 'Expected direction',
    start: { x: 0.15, y: 0.5 },
    end: { x: 0.85, y: 0.5 }
  };
}

function normalizeDirectionPoint(value: unknown, fallback: { x: number; y: number }) {
  if (!value || typeof value !== 'object') {
    return fallback;
  }
  const record = value as Record<string, unknown>;
  return {
    x: clampNumber(finiteNumber(record.x, fallback.x), 0, 1),
    y: clampNumber(finiteNumber(record.y, fallback.y), 0, 1)
  };
}

function normalizeCssColor(value: unknown, fallback: string) {
  const raw = String(value ?? '').trim();
  return /^#[0-9a-fA-F]{6}$/.test(raw) ? raw : fallback;
}

function blankSpatialRule(): SpatialRule {
  return {
    rule_key: '',
    relation: 'distance',
    enabled: true,
    primary_labels: [],
    secondary_labels: [],
    label_pairing: 'any',
    max_distance_percent: 10,
    min_distance_percent: null,
    min_overlap_percent: 1,
    min_closing_percent: 1,
    zone_key: '',
    zone_match: 'center_inside',
    min_count: 1,
    max_count: null,
    signal_label: ''
  };
}

function blankZoneSpatialRule(): SpatialRule {
  return {
    ...blankSpatialRule(),
    rule_key: 'zone_requirement',
    relation: 'zone_occupancy',
    zone_match: 'center_inside',
    min_overlap_percent: 1,
    signal_label: 'Zone requirement'
  };
}

function blankCloseSpatialRule(): SpatialRule {
  return {
    ...blankSpatialRule(),
    rule_key: 'close_objects',
    relation: 'overlap',
    min_overlap_percent: 1,
    signal_label: 'Objects overlap'
  };
}

export function normalizeSpatialRules(value: unknown): SpatialRule[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => typeof item === 'object' && item !== null)
    .map((item) => {
      const relation = String(item.relation ?? 'distance');
      const pairing = String(item.label_pairing ?? 'any');
      const zoneMatch = String(item.zone_match ?? 'center_inside');
      return {
        rule_key: String(item.rule_key ?? item.key ?? ''),
        relation: spatialRelationOptions.includes(relation) ? relation : 'distance',
        enabled: item.enabled !== false,
        primary_labels: normalizeRuleLabels(item.primary_labels ?? item.labels),
        secondary_labels: normalizeRuleLabels(item.secondary_labels),
        label_pairing: spatialLabelPairingOptions.includes(pairing) ? pairing : 'any',
        max_distance_percent: nullableFiniteNumber(item.max_distance_percent, 10),
        min_distance_percent: nullableFiniteNumber(item.min_distance_percent, null),
        min_overlap_percent: clampNumber(finiteNumber(item.min_overlap_percent, 1), 0, 100),
        min_closing_percent: Math.max(0, finiteNumber(item.min_closing_percent, 1)),
        zone_key: String(item.zone_key ?? item.zone ?? ''),
        zone_match: spatialZoneMatchOptions.includes(zoneMatch) ? zoneMatch : 'center_inside',
        min_count: Math.max(1, Math.round(finiteNumber(item.min_count, 1))),
        max_count: nullableFiniteNumber(item.max_count, null),
        signal_label: String(item.signal_label ?? '')
      };
    });
}

function updateSpatialRule(
  rules: SpatialRule[],
  index: number,
  key: keyof SpatialRule,
  value: SpatialRule[keyof SpatialRule]
) {
  return rules.map((rule, candidateIndex) =>
    candidateIndex === index ? { ...rule, [key]: value } : rule
  );
}

function normalizeRuleLabels(value: unknown): string[] {
  const raw = Array.isArray(value)
    ? value
    : typeof value === 'string'
      ? value.split(/[,\n]+/)
      : [];
  return raw.map((item) => String(item).trim()).filter(Boolean);
}

function formatRuleLabels(value: string[]) {
  return value.join(', ');
}

function zoneMatchLabel(value: string) {
  if (value === 'box_overlaps') {
    return 'Box overlaps zone';
  }
  if (value === 'fully_inside') {
    return 'Box fully inside zone';
  }
  return 'Center point inside zone';
}

function labelPairingLabel(value: string) {
  if (value === 'same_label') {
    return 'Same class only';
  }
  if (value === 'different_label') {
    return 'Different classes only';
  }
  return 'Any classes';
}

function nullableFiniteNumber(value: unknown, fallback: number | null) {
  if (value === null || value === undefined || value === '') {
    return fallback;
  }
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function finiteNumber(value: unknown, fallback: number) {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function clampNumber(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), maximum);
}

function channelsUsingDefinition(
  definition: RuntimePipelineDefinitionSummary,
  channels: RuntimeChannelSummary[]
) {
  const versionIds = new Set(
    [definition.published_version_id, definition.latest_version_id].filter(
      (versionId): versionId is string => Boolean(versionId)
    )
  );
  const definitionName = definition.name.toLocaleLowerCase();
  return channels.filter((channel) => {
    if (channel.pipeline_version_id && versionIds.has(channel.pipeline_version_id)) {
      return true;
    }
    return (channel.assigned_pipeline ?? '').toLocaleLowerCase() === definitionName;
  });
}

export function triggerablePipelineSummary(
  graph: RuntimePipelineGraph,
  definition: RuntimePipelineDefinitionSummary | undefined
) {
  const trigger = graph.nodes.find((node) => node.node_type === 'trigger.http_put');
  const channelInput = graph.nodes.find((node) => node.node_type === 'source.channel_frame');
  const response = graph.nodes.find((node) =>
    ['output.http_trigger_response', 'output.result'].includes(node.node_type)
  );
  if (!trigger || !channelInput || !response) {
    return null;
  }
  const slug = slugify(String(trigger.config.endpoint_slug || `${definition?.slug ?? 'pipeline'}-${trigger.node_key}`));
  return {
    method: String(trigger.config.method || 'PUT'),
    path: `/api/v1/runtime/triggers/${slug}`,
    authLabel: trigger.config.auth_required === true ? 'API key' : 'no API key',
    timeoutMs: Number(trigger.config.timeout_ms || 2500)
  };
}

export function inferGraphMode(graph: RuntimePipelineGraph, fallback: RuntimeMode): RuntimeMode {
  if (graph.nodes.some((node) => node.node_type === 'trigger.http_put')) {
    return 'trigger';
  }
  if (graph.nodes.some((node) => node.node_type === 'trigger.interval')) {
    return 'interval';
  }
  return fallback === 'trigger' || fallback === 'interval' ? 'realtime' : fallback;
}

function slugify(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'pipeline-trigger';
}

export function buildCompatibleConnections(
  graph: RuntimePipelineGraph,
  fromNode: RuntimeNodeSpec,
  fromNodeType: RuntimeNodeTypeSummary,
  nodeTypes: Map<string, RuntimeNodeTypeSummary>
) {
  const existing = new Set(
    graph.edges.map((edge) => `${edge.from_node_key}:${edge.from_port}:${edge.to_node_key}:${edge.to_port}`)
  );
  const occupiedInputs = new Set(
    graph.edges.map((edge) => `${edge.to_node_key}:${edge.to_port}`)
  );
  return fromNodeType.output_ports.flatMap((fromPort) =>
    graph.nodes
      .filter((candidate) => candidate.node_key !== fromNode.node_key)
      .flatMap((toNode) => {
        const toNodeType = nodeTypes.get(toNode.node_type);
        if (
          !toNodeType ||
          (toNodeType.node_category === 'source' && !canConnectToSourceNode(fromPort, toNode, toNodeType))
        ) {
          return [];
        }
        return toNodeType.input_ports
          .filter(
            (toPort) =>
              portsCompatible(fromPort, toPort, toNodeType) &&
              !occupiedInputs.has(`${toNode.node_key}:${toPort.key}`)
          )
          .map((toPort) => {
            const edge: RuntimeGraphEdge = {
              from_node_key: fromNode.node_key,
              from_port: fromPort.key,
              to_node_key: toNode.node_key,
              to_port: toPort.key
            };
            const key = `${edge.from_node_key}:${edge.from_port}:${edge.to_node_key}:${edge.to_port}`;
            return {
              key,
              label: `${fromPort.key} to ${toNodeType.display_name}.${toPort.key}`,
              edge
            };
          })
          .filter((connection) => !existing.has(connection.key));
      })
  );
}

function canConnectToSourceNode(
  fromPort: RuntimeNodePort,
  toNode: RuntimeNodeSpec,
  toNodeType: RuntimeNodeTypeSummary
) {
  return (
    fromPort.data_type === 'trigger' &&
    ['source.channel_snapshot', 'source.channel_frame', 'source.test_photo'].includes(toNode.node_type) &&
    toNodeType.input_ports.some((port) => port.key === 'trigger' && port.data_type === 'trigger')
  );
}

export function portsCompatible(
  fromPort: RuntimeNodePort,
  toPort: RuntimeNodePort,
  toNodeType: RuntimeNodeTypeSummary
) {
  if (fromPort.data_type === toPort.data_type) {
    return true;
  }
  const aliases = toNodeType.compatibility_constraints.port_aliases as
    | Record<string, string[]>
    | undefined;
  if (aliases?.[toPort.data_type]?.includes(fromPort.data_type)) {
    return true;
  }
  return (
    toPort.data_type === 'result' &&
    [
      'detections',
      'classifications',
      'segments',
      'positions',
      'measurements',
      'stem_horizontal_curvature',
      'cv_edges',
      'reference_edges',
      'coordinate_frame',
      'feature_measurements',
      'clearance_zones',
      'free_region',
      'quality_decision',
      'events',
      'roi_set',
      'frame',
      'overlay',
      'object_set',
      'anomaly'
    ].includes(fromPort.data_type)
  ) || (
    toPort.data_type === 'object_set' &&
    ['detections', 'segments', 'region_set', 'object_set'].includes(fromPort.data_type)
  );
}

function visualContractLabel(nodeType: RuntimeNodeTypeSummary) {
  if (nodeType.node_type === 'output.display_output') {
    return 'Chooses live-view context';
  }
  if (nodeType.input_ports.some((port) => port.data_type === 'frame') && nodeType.output_ports.some((port) => port.data_type === 'frame')) {
    return 'Transforms image context';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'anomaly')) {
    return 'Produces anomaly score and heatmap';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'stem_horizontal_curvature')) {
    return 'Measures horizontal curvature in calibrated X/Y space';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'cv_edges')) {
    return 'Produces CV edge artifacts';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'reference_edges')) {
    return 'Produces reference edges';
  }
  if (nodeType.output_ports.some((port) => ['detections', 'segments'].includes(port.data_type))) {
    return 'Produces object references';
  }
  if (nodeType.node_type === 'output.overlay') {
    return 'Creates context-bound overlays';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'roi_set')) {
    return 'Creates ROI contexts';
  }
  if (nodeType.input_ports.some((port) => ['detections', 'segments', 'object_set'].includes(port.data_type))) {
    return 'Consumes object data';
  }
  return 'Preserves runtime data';
}

function visualContractHelp(nodeType: RuntimeNodeTypeSummary) {
  if (nodeType.node_type === 'output.display_output') {
    return 'Connect a frame input; operators choose the display by wiring the node, not by typing internal IDs.';
  }
  if (nodeType.node_type === 'output.overlay') {
    return 'Overlay primitives follow the image or object context produced upstream.';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'anomaly')) {
    return 'Anomaly outputs can feed result rules and live heatmap overlays.';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'cv_edges')) {
    return 'Canny creates a provenance-linked edge mask and debug overlay layers from the input image.';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'reference_edges')) {
    return 'Reference edges can feed Coordinate Frame From Two Edges regardless of AI or CV source.';
  }
  if (nodeType.output_ports.some((port) => ['detections', 'segments'].includes(port.data_type))) {
    return 'Downstream ROI and enrichment paths can attach results back to these objects.';
  }
  if (nodeType.output_ports.some((port) => port.data_type === 'roi_set')) {
    return 'Each ROI keeps its source image and source object relationship.';
  }
  return 'Connect compatible ports; validation checks visual branches before publishing.';
}

function removeNode(graph: RuntimePipelineGraph, nodeKey: string): RuntimePipelineGraph {
  return {
    nodes: graph.nodes.filter((node) => node.node_key !== nodeKey),
    edges: graph.edges.filter((edge) => edge.from_node_key !== nodeKey && edge.to_node_key !== nodeKey)
  };
}

function readDragPayload(event: DragEvent<HTMLElement>): DragPayload | null {
  try {
    const raw = event.dataTransfer.getData('application/x-runtime-node');
    return raw ? (JSON.parse(raw) as DragPayload) : null;
  } catch {
    return null;
  }
}

function runtimeEdgeKey(edge: RuntimeGraphEdge) {
  return `${edge.from_node_key}:${edge.from_port}->${edge.to_node_key}:${edge.to_port}`;
}

function isEditableKeyboardTarget(target: EventTarget | null) {
  if (!(target instanceof HTMLElement)) {
    return false;
  }
  return Boolean(
    target.closest('input, textarea, select, [contenteditable="true"]')
  );
}

function nodeCenter(node: RuntimeNodeSpec) {
  return {
    x: (node.position.x ?? 0) + nodeWidth / 2,
    y: (node.position.y ?? 0) + nodeHeight / 2
  };
}

export function defaultModelRef(taskType: TaskType): RuntimeLogicalModelRef {
  return {
    model_family_slug: `required-${taskType}-model`,
    target_path: 'realtime',
    contract_major: 1,
    selection_policy: 'active_production',
    required_task_type: taskType
  };
}

export function requiredTaskTypeForNode(nodeType: string): TaskType {
  if (nodeType.includes('anomaly')) {
    return 'anomaly';
  }
  if (nodeType.includes('classification')) {
    return 'classification';
  }
  if (nodeType.includes('segmentation')) {
    return 'segmentation';
  }
  return 'detection';
}

function compatibleTaskTypesForModelNode(taskType: TaskType): TaskType[] {
  if (taskType === 'detection') {
    return ['detection', 'oriented_detection'];
  }
  return [taskType];
}

function categoryLabel(category: RuntimeNodeCategory) {
  const labels: Record<RuntimeNodeCategory, string> = {
    trigger: 'Trigger',
    source: 'Source',
    preprocessing: 'Preprocessing',
    computer_vision: 'Computer Vision',
    inference: 'Inference',
    analysis: 'Analysis / computation',
    capture: 'Capture',
    output: 'Output'
  };
  return labels[category];
}

export function paletteGroupsForCategory(
  category: RuntimeNodeCategory,
  items: RuntimeNodeTypeSummary[]
): { label: string; items: RuntimeNodeTypeSummary[] }[] {
  if (category === 'output') {
    const industrial = items.filter((item) => item.compatibility_constraints?.palette_group === 'Industrial / PLC');
    const remaining = items.filter((item) => !industrial.includes(item));
    return [
      { label: categoryLabel(category), items: remaining },
      { label: 'Industrial / PLC', items: industrial }
    ].filter((group) => group.items.length);
  }
  if (category !== 'analysis') {
    return [{ label: categoryLabel(category), items }];
  }
  const geometry = items.filter((item) =>
    [
      'analysis.reference_edge_from_features',
      'analysis.coordinate_frame_from_edges'
    ].includes(item.node_type)
  );
  const measurement = items.filter((item) =>
    [
      'analysis.feature_measurement',
      'analysis.clearance_zone_generator',
      'analysis.first_free_region_finder',
      'analysis.pixel_to_metric'
    ].includes(item.node_type)
  );
  const decision = items.filter((item) =>
    [
      'analysis.quality_rule_evaluation',
      'analysis.zone_detection',
      'analysis.close_detection',
      'analysis.object_spatial_signal',
      'analysis.object_orientation_signal',
      'analysis.rule_evaluation'
    ].includes(item.node_type)
  );
  const assigned = new Set([...geometry, ...measurement, ...decision].map((item) => item.node_type));
  const remaining = items.filter((item) => !assigned.has(item.node_type));
  return [
    { label: 'Reference / Geometry', items: geometry },
    { label: 'Measurement', items: measurement },
    { label: 'Rules / Decision', items: decision },
    { label: categoryLabel(category), items: remaining }
  ].filter((group) => group.items.length);
}

function categoryIcon(category: RuntimeNodeCategory): AppIconName {
  if (category === 'trigger' || category === 'source' || category === 'output') {
    return 'endpoint';
  }
  if (category === 'inference') {
    return 'model';
  }
  if (category === 'capture') {
    return 'realtime';
  }
  if (category === 'analysis' || category === 'computer_vision') {
    return 'activity';
  }
  return 'settings';
}

function modeLabel(mode: RuntimeMode) {
  const labels: Record<RuntimeMode, string> = {
    realtime: 'Realtime',
    trigger: 'Trigger',
    interval: 'Interval'
  };
  return labels[mode];
}

type StructuredBox = {
  label?: string;
  x: number;
  y: number;
  width: number;
  height: number;
  angle_degrees: number;
};

type TrackerRoiPoint = { x: number; y: number };
type TrackerRoiRegion = {
  label: string;
  points: TrackerRoiPoint[];
};

export function normalizeTrackerRoiList(value: unknown): TrackerRoiRegion[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item, index) => {
    if (!item || typeof item !== 'object') return [];
    const record = item as Record<string, unknown>;
    const label =
      typeof record.label === 'string' && record.label.trim()
        ? record.label
        : `ROI ${index + 1}`;
    const rawPoints = Array.isArray(record.points)
      ? record.points
      : Array.isArray(record.polygon)
        ? record.polygon
        : null;
    if (rawPoints) {
      const points = rawPoints.flatMap((point) => {
        if (!point || typeof point !== 'object') return [];
        const candidate = point as Record<string, unknown>;
        const x = Number(candidate.x);
        const y = Number(candidate.y);
        return Number.isFinite(x) && Number.isFinite(y)
          ? [{ x: clampNumber(x, 0, 1), y: clampNumber(y, 0, 1) }]
          : [];
      });
      return points.length >= 3 ? [{ label, points }] : [];
    }
    const boxes = normalizeBoxList([record]);
    if (!boxes.length) return [];
    const box = boxes[0];
    return [
      {
        label,
        points: [
          { x: box.x, y: box.y },
          { x: box.x + box.width, y: box.y },
          { x: box.x + box.width, y: box.y + box.height },
          { x: box.x, y: box.y + box.height }
        ]
      }
    ];
  });
}

export function trackerRoiIsWholeView(region: TrackerRoiRegion): boolean {
  if (region.points.length < 3) return false;
  const xs = region.points.map((point) => point.x);
  const ys = region.points.map((point) => point.y);
  return (
    Math.min(...xs) <= 0.001 &&
    Math.max(...xs) >= 0.999 &&
    Math.min(...ys) <= 0.001 &&
    Math.max(...ys) >= 0.999
  );
}

function blankStructuredBox(): StructuredBox {
  return { label: '', x: 0, y: 0, width: 0.25, height: 0.25, angle_degrees: 0 };
}

export function normalizeBoxList(value: unknown): StructuredBox[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') {
      return [];
    }
    const record = item as Record<string, unknown>;
    if (
      typeof record.x === 'number' &&
      typeof record.y === 'number' &&
      typeof record.width === 'number' &&
      typeof record.height === 'number'
    ) {
      return [
        {
          label: typeof record.label === 'string' ? record.label : '',
          x: record.x,
          y: record.y,
          width: record.width,
          height: record.height,
          angle_degrees: boxAngleDegrees(record)
        }
      ];
    }
    if (
      typeof record.x_min === 'number' &&
      typeof record.y_min === 'number' &&
      typeof record.x_max === 'number' &&
      typeof record.y_max === 'number'
    ) {
      return [
        {
          label: typeof record.label === 'string' ? record.label : '',
          x: record.x_min,
          y: record.y_min,
          width: Math.max(record.x_max - record.x_min, 0),
          height: Math.max(record.y_max - record.y_min, 0),
          angle_degrees: boxAngleDegrees(record)
        }
      ];
    }
    return [];
  });
}

function updateBoxListValue(
  items: StructuredBox[],
  index: number,
  key: keyof StructuredBox,
  nextValue: number
) {
  return items.map((item, candidateIndex) =>
    candidateIndex === index ? { ...item, [key]: Number.isFinite(nextValue) ? nextValue : 0 } : item
  );
}

function boxAngleDegrees(record: Record<string, unknown>) {
  const nestedBox = record.box && typeof record.box === 'object'
    ? (record.box as Record<string, unknown>)
    : null;
  return normalizeAngleDegrees(
    record.angle_degrees ??
      record.rotation_degrees ??
      record.angle ??
      nestedBox?.angle_degrees ??
      nestedBox?.rotation_degrees ??
      nestedBox?.angle
  );
}

function normalizeAngleDegrees(value: unknown) {
  const parsed = finiteNumber(value, 0);
  const normalized = ((((parsed + 180) % 360) + 360) % 360) - 180;
  return Object.is(normalized, -0) ? 0 : normalized;
}

function upstreamBoardCutModelCapability(
  graph: RuntimePipelineGraph,
  node: RuntimeNodeSpec,
  capabilities: RuntimeModelCapabilityOption[]
) {
  return upstreamModelCapability(graph, node, 'features', capabilities);
}

function upstreamModelCapability(
  graph: RuntimePipelineGraph,
  node: RuntimeNodeSpec,
  inputPort: string,
  capabilities: RuntimeModelCapabilityOption[]
) {
  const inferenceNode = upstreamInferenceNode(graph, node.node_key, inputPort);
  const modelRef = inferenceNode?.logical_model_ref;
  if (!modelRef) {
    return null;
  }
  const compatible = capabilities.filter(
    (item) =>
      modelCapabilityMatchesReference(item, modelRef) &&
      item.contract_major === modelRef.contract_major &&
      item.target_path === modelRef.target_path &&
      modelCapabilityMatchesSelectionPolicy(item, modelRef.selection_policy)
  );
  const releaseId = resolveSelectedModelReleaseId(modelRef, compatible);
  return compatible.find((item) => item.deployment_release_id === releaseId) ?? compatible[0] ?? null;
}

function upstreamInferenceNode(
  graph: RuntimePipelineGraph,
  nodeKey: string,
  inputPort: string
): RuntimeNodeSpec | null {
  const visited = new Set<string>();
  let currentNodeKey = nodeKey;
  let currentPort = inputPort;
  while (!visited.has(currentNodeKey)) {
    visited.add(currentNodeKey);
    const edge = graph.edges.find(
      (candidate) =>
        candidate.to_node_key === currentNodeKey && candidate.to_port === currentPort
    );
    const source = graph.nodes.find((candidate) => candidate.node_key === edge?.from_node_key);
    if (!source) return null;
    if (source.node_type === 'inference.detection' || source.node_type === 'inference.segmentation') {
      return source;
    }
    if (source.node_type !== 'analysis.segmentation_polygon_simplification') return null;
    currentNodeKey = source.node_key;
    currentPort = 'segments';
  }
  return null;
}

function modelClassLabels(capability: RuntimeModelCapabilityOption | null) {
  return normalizeModelClassLabels(capability?.capability.class_labels);
}

function normalizeModelClassLabels(raw: unknown) {
  if (!Array.isArray(raw)) {
    return [];
  }
  return raw
    .map((item) => {
      if (typeof item === 'string') return item;
      if (!item || typeof item !== 'object') return '';
      const entry = item as Record<string, unknown>;
      return String(entry.label ?? entry.name ?? entry.class_name ?? '');
    })
    .filter(Boolean);
}

function isExplicitClassSubsetCapability(
  capability: RuntimeModelCapabilityOption | null,
  labels: string[]
) {
  if (capability?.capability.uses_all_project_classes !== false) {
    return false;
  }
  const configuredLabels = normalizeModelClassLabels(
    capability.capability.configured_class_names
  );
  return (
    configuredLabels.length === labels.length &&
    configuredLabels.every((label, index) => label === labels[index])
  );
}

export function reconcileBoardCutClassRules(
  value: unknown,
  capability: RuntimeModelCapabilityOption | null
): BoardCutClassRule[] {
  const labels = modelClassLabels(capability);
  const classMapVersion = String(capability?.capability.class_map_version ?? '') || null;
  const current = normalizeBoardCutClassRules(value);
  const canRemapByLabel = isExplicitClassSubsetCapability(capability, labels);
  return labels.map((label, classId) => {
    const existing = current.find(
      (rule) => rule.class_id === classId && rule.label_snapshot === label
    );
    if (existing && existing.class_map_version === classMapVersion) {
      return existing;
    }
    if (canRemapByLabel) {
      const matchingLabel = current.find((rule) => rule.label_snapshot === label);
      if (matchingLabel) {
        return {
          ...matchingLabel,
          class_id: classId,
          class_map_version: classMapVersion
        };
      }
    }
    return blankBoardCutClassRule(classId, label, classMapVersion);
  });
}

export function reconcileBoardCutClassRulesInGraph(
  graph: RuntimePipelineGraph,
  capabilities: RuntimeModelCapabilityOption[]
): RuntimePipelineGraph {
  let changed = false;
  const nodes = graph.nodes.map((node) => {
    let config = node.config;
    let nodeChanged = false;
    if (
      node.node_type === 'analysis.board_view_cut_constraints' ||
      node.node_type === 'analysis.end_face_cut_constraints'
    ) {
      const capability = upstreamBoardCutModelCapability(graph, node, capabilities);
      if (modelClassLabels(capability).length) {
        const current = normalizeBoardCutClassRules(config.class_rules);
        const reconciled = reconcileBoardCutClassRules(current, capability);
        if (JSON.stringify(current) !== JSON.stringify(reconciled)) {
          config = { ...config, class_rules: reconciled };
          nodeChanged = true;
        }
      }
    }
    const surfaceRoles: BoardSurfaceSemanticRole[] | null =
      node.node_type === 'analysis.board_end_coordinate_frame' &&
      String(node.config.geometry_source ?? 'image_edges') === 'segmentation'
        ? ['board_main_face', 'board_side_face']
        : node.node_type === 'analysis.end_face_cut_constraints' &&
            graph.edges.some(
              (edge) => edge.to_node_key === node.node_key && edge.to_port === 'segments'
            )
          ? ['board_end_face']
          : null;
    if (surfaceRoles) {
      const capability = upstreamModelCapability(graph, node, 'segments', capabilities);
      if (modelClassLabels(capability).length) {
        const current = normalizeBoardSurfaceClassMapping(config.board_surface_class_mapping);
        const reconciled = reconcileBoardSurfaceClassMapping(current, surfaceRoles, capability);
        if (JSON.stringify(current) !== JSON.stringify(reconciled)) {
          config = { ...config, board_surface_class_mapping: reconciled };
          nodeChanged = true;
        }
      }
    }
    changed = changed || nodeChanged;
    return nodeChanged ? { ...node, config } : node;
  });
  return changed ? { ...graph, nodes } : graph;
}

function normalizeBoardSurfaceClassMapping(value: unknown): BoardSurfaceClassMapping {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const result: BoardSurfaceClassMapping = {};
  for (const role of [
    'board_main_face',
    'board_side_face',
    'board_end_face'
  ] as BoardSurfaceSemanticRole[]) {
    const raw = (value as Record<string, unknown>)[role];
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const item = raw as Record<string, unknown>;
    result[role] = {
      class_id: Math.max(0, Math.floor(finiteNumber(item.class_id, 0))),
      label_snapshot: String(item.label_snapshot ?? ''),
      class_map_version: item.class_map_version == null ? null : String(item.class_map_version),
      deployment_release_id: String(item.deployment_release_id ?? ''),
      confirmed: Boolean(item.confirmed)
    };
  }
  return result;
}

export function reconcileBoardSurfaceClassMapping(
  value: BoardSurfaceClassMapping,
  roles: BoardSurfaceSemanticRole[],
  capability: RuntimeModelCapabilityOption | null
): BoardSurfaceClassMapping {
  const labels = modelClassLabels(capability);
  const classMapVersion = String(capability?.capability.class_map_version ?? '') || null;
  const deploymentReleaseId = capability?.deployment_release_id ?? '';
  const result: BoardSurfaceClassMapping = {};
  for (const role of roles) {
    const current = value[role];
    if (!current) continue;
    const currentLabel = labels[current.class_id];
    const unchanged =
      currentLabel === current.label_snapshot &&
      current.class_map_version === classMapVersion &&
      current.deployment_release_id === deploymentReleaseId;
    result[role] = unchanged ? current : { ...current, confirmed: false };
  }
  return result;
}

function boardSurfaceSemanticRoleLabel(role: BoardSurfaceSemanticRole) {
  return {
    board_main_face: 'Main board face',
    board_side_face: 'Board side face',
    board_end_face: 'Board end face'
  }[role];
}

function normalizeBoardCutClassRules(value: unknown): BoardCutClassRule[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .map((item) => ({
      class_id: Math.max(0, Math.floor(finiteNumber(item.class_id, 0))),
      label_snapshot: String(item.label_snapshot ?? ''),
      class_map_version: item.class_map_version == null ? null : String(item.class_map_version),
      confirmed: Boolean(item.confirmed),
      relevant: Boolean(item.relevant),
      measurement_basis: boardMeasurementBasis(item.measurement_basis),
      safety_profile:
        item.safety_profile === 'conservative' || item.safety_profile === 'custom'
          ? item.safety_profile
          : 'standard',
      clearance_multiplier: Math.max(finiteNumber(item.clearance_multiplier, 1), 0),
      depth_factor: Math.max(finiteNumber(item.depth_factor, 1), 0),
      clearance_factor: Math.max(finiteNumber(item.clearance_factor, 1), 0),
      minimum_confidence: clampNumber(finiteNumber(item.minimum_confidence, 0.35), 0, 1)
    }));
}

function blankBoardCutClassRule(
  classId: number,
  label: string,
  classMapVersion: string | null
): BoardCutClassRule {
  return {
    class_id: classId,
    label_snapshot: label,
    class_map_version: classMapVersion,
    confirmed: false,
    relevant: false,
    measurement_basis: 'max_feret_diameter',
    safety_profile: 'standard',
    clearance_multiplier: 1,
    depth_factor: 1,
    clearance_factor: 1,
    minimum_confidence: 0.35
  };
}

export function boardCutSiemensSpsMappings(): SiemensSpsMappingRow[] {
  const valid = blankSiemensSpsMapping('valid');
  const shift = blankSiemensSpsMapping('longitudinal_shift_mm');
  return [
    {
      ...valid,
      key: 'board_cut_valid',
      label: 'Measurement valid',
      source: { ...valid.source, extractor: 'boolean_field' },
      target: {
        area: 'DB',
        db_number: 101,
        byte_offset: 0,
        bit_offset: 0,
        data_type: 'BOOL'
      },
      conversion: { ...valid.conversion, mode: 'boolean', true_value: true, false_value: false }
    },
    {
      ...shift,
      key: 'board_longitudinal_shift_mm',
      label: 'Longitudinal shift [mm]',
      source: { ...shift.source, extractor: 'numeric_field' },
      target: {
        area: 'DB',
        db_number: 101,
        byte_offset: 4,
        bit_offset: null,
        data_type: 'REAL'
      },
      conversion: { ...shift.conversion, mode: 'direct', rounding: 'none' },
      condition: { mode: 'when_field', field: 'valid', operator: 'truthy', value: null }
    }
  ];
}

function boardMeasurementBasis(value: unknown): BoardCutClassRule['measurement_basis'] {
  if (
    value === 'equivalent_diameter' ||
    value === 'longitudinal_extent' ||
    value === 'transverse_extent'
  ) {
    return value;
  }
  return 'max_feret_diameter';
}

function boardSurfaceRoles(node: RuntimeNodeSpec): BoardSurfaceCalibration['surface_role'][] {
  if (node.node_type === 'analysis.end_face_cut_constraints') {
    return ['end_face'];
  }
  return node.config.view_role === 'lower' ? ['bottom', 'side_b'] : ['top', 'side_a'];
}

export function selectedBoardCalibrationChannelId(
  graph: RuntimePipelineGraph,
  selectedNodeKey: string | null
) {
  const selectedNode = graph.nodes.find((node) => node.node_key === selectedNodeKey);
  if (
    selectedNode?.node_type === 'preprocessing.camera_undistort' ||
    selectedNode?.node_type === 'preprocessing.process_coordinate_rectify'
  ) {
    const channelSource = graph.nodes.find((node) => node.node_type === 'source.channel_frame');
    const channelId = channelSource?.config.authoring_channel_id;
    return typeof channelId === 'string' && channelId ? channelId : null;
  }
  if (
    selectedNode?.node_type !== 'analysis.board_view_cut_constraints' &&
    selectedNode?.node_type !== 'analysis.end_face_cut_constraints'
  ) {
    return null;
  }
  const source = graph.nodes.find(
    (node) =>
      node.node_type === 'source.synchronized_camera_frames' ||
      node.node_type === 'source.single_board_camera_test' ||
      node.node_type === 'source.channel_frame'
  );
  if (!source) return null;
  const role =
    selectedNode.node_type === 'analysis.end_face_cut_constraints'
      ? 'end_face'
      : selectedNode.config.view_role === 'lower'
        ? 'lower'
        : 'upper';
  let channelId: unknown;
  if (source.node_type === 'source.channel_frame') {
    channelId = source.config.authoring_channel_id;
  } else if (source.node_type === 'source.single_board_camera_test') {
    channelId = source.config.camera_role === role ? source.config.camera_channel_id : null;
  } else {
    channelId = source.config[`${role}_channel_id`];
  }
  return typeof channelId === 'string' && channelId ? channelId : null;
}

export function commissioningPreviewTargetNodeKey(
  graph: RuntimePipelineGraph,
  selectedNodeKey: string | null
): string | null {
  const selectedNode = graph.nodes.find((node) => node.node_key === selectedNodeKey);
  if (
    selectedNode?.node_type !== 'preprocessing.process_coordinate_rectify' &&
    selectedNode?.node_type !== 'analysis.stem_measurement_eligibility'
  ) {
    return null;
  }
  if (
    selectedNode.node_type === 'preprocessing.process_coordinate_rectify' &&
    normalizeImageViewProfile(selectedNode.config.view_profile)
  ) {
    return selectedNode.node_key;
  }
  const nodes = new Map(graph.nodes.map((node) => [node.node_key, node]));
  const pending = [selectedNode.node_key];
  const visited = new Set<string>();
  while (pending.length) {
    const current = pending.shift();
    if (!current || visited.has(current)) continue;
    visited.add(current);
    for (const edge of graph.edges.filter((item) => item.to_node_key === current)) {
      const upstream = nodes.get(edge.from_node_key);
      if (!upstream) continue;
      if (
        selectedNode.node_type === 'analysis.stem_measurement_eligibility' &&
        upstream.node_type === 'preprocessing.process_coordinate_rectify' &&
        normalizeImageViewProfile(upstream.config.view_profile)
      ) {
        return upstream.node_key;
      }
      if (upstream.node_type === 'preprocessing.camera_undistort') {
        if (selectedNode.node_type === 'preprocessing.process_coordinate_rectify') {
          return upstream.node_key;
        }
        continue;
      }
      pending.push(upstream.node_key);
    }
  }
  return null;
}

export function validationIssueSummary(
  report: RuntimePipelineValidationReport,
  maximumIssues = 3
) {
  if (!report.error_count && !report.warning_count) return 'No remaining pipeline issues.';
  const ordered = [
    ...report.issues.filter((issue) => issue.severity === 'error'),
    ...report.issues.filter((issue) => issue.severity === 'warning')
  ];
  const shown = ordered.slice(0, maximumIssues).map((issue) => {
    const location = issue.node_key ? `${issue.node_key}${issue.field ? `.${issue.field}` : ''}` : 'graph';
    return `${location}: ${issue.message}`;
  });
  const remaining = Math.max(ordered.length - shown.length, 0);
  return `${report.error_count} errors / ${report.warning_count} warnings: ${shown.join(' | ')}${
    remaining ? ` | +${remaining} more below` : ''
  }`;
}

function pipelineCheckStatusLabel(
  report: RuntimePipelineValidationReport | null,
  dirty: boolean
) {
  if (dirty) return 'changes open';
  if (!report) return 'not checked';
  return report.status === 'valid' ? 'ready' : `${report.error_count} settings open`;
}

export function normalizeCameraIntrinsicCalibrationProfile(
  value: unknown
): RuntimeCameraIntrinsicCalibrationProfile | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Partial<RuntimeCameraIntrinsicCalibrationProfile>;
  if (
    candidate.schema_version !== 'camera.intrinsic-calibration.v1' ||
    !['valid', 'invalid'].includes(candidate.status ?? '') ||
    typeof candidate.source_channel_id !== 'string' ||
    !Number.isFinite(candidate.image_width) ||
    !Number.isFinite(candidate.image_height) ||
    !Array.isArray(candidate.camera_matrix) ||
    candidate.camera_matrix.length !== 9 ||
    !Array.isArray(candidate.output_camera_matrix) ||
    candidate.output_camera_matrix.length !== 9 ||
    !Array.isArray(candidate.distortion_coefficients) ||
    typeof candidate.calibration_hash !== 'string'
  ) {
    return null;
  }
  return candidate as RuntimeCameraIntrinsicCalibrationProfile;
}

export function normalizeImageViewProfile(value: unknown): RuntimeImageViewProfile | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Partial<RuntimeImageViewProfile>;
  if (
    candidate.schema_version !== 'image.view-profile.v1' ||
    candidate.status !== 'valid' ||
    candidate.validation_status !== 'VALID' ||
    typeof candidate.view_profile_id !== 'string' ||
    typeof candidate.source_channel_id !== 'string' ||
    !Number.isFinite(candidate.source_image_width) ||
    !Number.isFinite(candidate.source_image_height) ||
    typeof candidate.intrinsic_calibration_hash !== 'string' ||
    !Array.isArray(candidate.source_quadrilateral) ||
    candidate.source_quadrilateral.length !== 4 ||
    !Number.isFinite(candidate.output_width) ||
    !Number.isFinite(candidate.output_height) ||
    !Array.isArray(candidate.source_to_view_homography) ||
    candidate.source_to_view_homography.length !== 9 ||
    !Array.isArray(candidate.view_to_source_homography) ||
    candidate.view_to_source_homography.length !== 9 ||
    typeof candidate.profile_checksum !== 'string' ||
    candidate.profile_checksum.length !== 64 ||
    !candidate.provenance
  ) {
    return null;
  }
  return candidate as RuntimeImageViewProfile;
}

function imageViewFrameForProfile(
  frame: RuntimeLiveFrame | null,
  profile: RuntimeImageViewProfile | null
): RuntimeLiveFrame | null {
  if (!frame?.image_base64 || !profile) return null;
  const context = recordValue(frame.image_context);
  const transform = recordValue(context.transform);
  const parameters = recordValue(transform.parameters);
  const frameProfileId = frame.metadata.view_profile_id ?? parameters.view_profile_id;
  const frameProfileChecksum =
    frame.metadata.view_profile_checksum ?? parameters.view_profile_checksum;
  const expectedRuntimeTransform =
    profile.transform_type === 'POLYGON_MASKED_CROP' ? 'masked_crop' : 'perspective_rectify';
  return transform.transform_type === expectedRuntimeTransform &&
    frameProfileId === profile.view_profile_id &&
    frameProfileChecksum === profile.profile_checksum &&
    frame.width === profile.output_width &&
    frame.height === profile.output_height
    ? frame
    : null;
}

export function normalizeStemCurvatureProcessCalibrationProfile(
  value: unknown
): RuntimeStemCurvatureProcessCalibrationProfile | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Partial<RuntimeStemCurvatureProcessCalibrationProfile>;
  if (
    candidate.schema_version !== 'stem.curvature-process-calibration.v1' ||
    candidate.status !== 'valid' ||
    typeof candidate.calibration_id !== 'string' ||
    typeof candidate.source_channel_id !== 'string' ||
    !Number.isFinite(candidate.image_width) ||
    !Number.isFinite(candidate.image_height) ||
    typeof candidate.intrinsic_calibration_hash !== 'string' ||
    !Array.isArray(candidate.reference_points) ||
    candidate.reference_points.length < 4 ||
    !Array.isArray(candidate.homography_image_to_process) ||
    candidate.homography_image_to_process.length !== 9 ||
    !Array.isArray(candidate.homography_process_to_image) ||
    candidate.homography_process_to_image.length !== 9 ||
    typeof candidate.calibration_hash !== 'string' ||
    candidate.calibration_hash.length !== 64 ||
    !candidate.candidate_roi ||
    !candidate.curvature_observation_roi ||
    !candidate.surface_tracking_roi ||
    !candidate.session_start_gate ||
    !candidate.curvature_freeze_gate ||
    !candidate.decision_gate ||
    !candidate.reference_plane ||
    !candidate.provenance
  ) {
    return null;
  }
  const spatial = candidate.spatial_calibration;
  if (spatial != null) {
    const matrices = [
      [spatial.camera_matrix, 9],
      [spatial.rotation_world_to_camera, 9],
      [spatial.translation_world_to_camera_mm, 3],
      [spatial.camera_center_mm, 3],
      [spatial.projection_matrix, 12]
    ] as const;
    if (spatial.method !== 'CALIBRATED_XY_Z_PNP_V1' ||
        matrices.some(([values, length]) => !Array.isArray(values) ||
          values.length !== length || !values.every(Number.isFinite)) ||
        ![spatial.rms_reprojection_error_px, spatial.maximum_reprojection_error_px,
          spatial.z_reprojection_error_px, spatial.z_reference_displacement_px,
          spatial.pose_condition_number, spatial.reference_point_count].every(Number.isFinite)) {
      return null;
    }
  }
  return candidate as RuntimeStemCurvatureProcessCalibrationProfile;
}

export function normalizeStemSurfacePhaseCalibrationProfile(
  value: unknown
): RuntimeStemSurfacePhaseCalibrationProfile | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Partial<RuntimeStemSurfacePhaseCalibrationProfile>;
  if (
    candidate.schema_version !== 'stem.surface-phase-calibration.v1' ||
    candidate.status !== 'valid' ||
    typeof candidate.calibration_id !== 'string' ||
    typeof candidate.calibration_version !== 'string' ||
    typeof candidate.source_channel_id !== 'string' ||
    !Number.isFinite(candidate.image_width) ||
    !Number.isFinite(candidate.image_height) ||
    typeof candidate.intrinsic_calibration_hash !== 'string' ||
    typeof candidate.process_calibration_hash !== 'string' ||
    candidate.fit_model !== 'LINEAR_THROUGH_ORIGIN' ||
    !Number.isFinite(candidate.degrees_per_raw_phase) ||
    !Array.isArray(candidate.observations) ||
    candidate.observations.length < 4 ||
    !candidate.provenance ||
    typeof candidate.calibration_hash !== 'string' ||
    candidate.calibration_hash.length !== 64
  ) {
    return null;
  }
  return candidate as RuntimeStemSurfacePhaseCalibrationProfile;
}

export function normalizeStemMinimumPrincipleValidationArtifact(
  value: unknown
): RuntimeStemMinimumPrincipleValidationArtifact | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const candidate = value as Partial<RuntimeStemMinimumPrincipleValidationArtifact>;
  if (
    candidate.schema_version !== 'stem.minimum-principle-validation.v1' ||
    !['VALIDATION_PASSED', 'VALIDATION_FAILED', 'INSUFFICIENT_DATA'].includes(
      candidate.status ?? ''
    ) ||
    candidate.algorithm !== 'CURVATURE_PHASE_MINIMUM' ||
    typeof candidate.artifact_id !== 'string' ||
    typeof candidate.artifact_version !== 'string' ||
    typeof candidate.source_channel_id !== 'string' ||
    !Number.isFinite(candidate.image_width) ||
    !Number.isFinite(candidate.image_height) ||
    typeof candidate.process_calibration_hash !== 'string' ||
    candidate.process_calibration_hash.length !== 64 ||
    typeof candidate.phase_calibration_hash !== 'string' ||
    candidate.phase_calibration_hash.length !== 64 ||
    !candidate.thresholds ||
    !Array.isArray(candidate.observations) ||
    candidate.observations.length < 1 ||
    !candidate.metrics ||
    !Array.isArray(candidate.checks) ||
    candidate.checks.length < 1 ||
    !candidate.provenance ||
    typeof candidate.report_markdown !== 'string' ||
    typeof candidate.report_html !== 'string' ||
    typeof candidate.artifact_hash !== 'string' ||
    candidate.artifact_hash.length !== 64
  ) {
    return null;
  }
  return candidate as RuntimeStemMinimumPrincipleValidationArtifact;
}

function stemMinimumPrincipleObservationInput(
  item: RuntimeStemMinimumPrincipleValidationArtifact['observations'][number]
): RuntimeStemMinimumPrincipleValidationObservation {
  const result = { ...item } as Record<string, unknown>;
  for (const key of [
    'angle_error_deg',
    'phase_error_deg_signed',
    'target_phase_error_deg_signed',
    'correction_error_deg_signed',
    'sign_error',
    'homography_error_mm',
    'curvature_min_error_mm',
    'curvature_max_error_mm',
    'image_position_bucket',
    'phase_span_bucket'
  ]) {
    delete result[key];
  }
  return result as unknown as RuntimeStemMinimumPrincipleValidationObservation;
}

export function upstreamImageViewProfile(
  graph: RuntimePipelineGraph,
  nodeKey: string
): RuntimeImageViewProfile | null {
  const nodes = new Map(graph.nodes.map((node) => [node.node_key, node]));
  const pending = [nodeKey];
  const visited = new Set<string>();
  const candidates: RuntimeImageViewProfile[] = [];
  while (pending.length) {
    const current = pending.shift();
    if (!current || visited.has(current)) continue;
    visited.add(current);
    for (const edge of graph.edges.filter((item) => item.to_node_key === current)) {
      const upstream = nodes.get(edge.from_node_key);
      if (!upstream) continue;
      if (upstream.node_type === 'preprocessing.process_coordinate_rectify') {
        const profile = normalizeImageViewProfile(upstream.config.view_profile);
        if (profile) candidates.push(profile);
      } else {
        pending.push(upstream.node_key);
      }
    }
  }
  const checksums = new Set(candidates.map((item) => item.profile_checksum));
  return candidates.length && checksums.size === 1 ? candidates[0] : null;
}

export function upstreamStemProcessCalibrationProfile(
  graph: RuntimePipelineGraph,
  nodeKey: string
): RuntimeStemCurvatureProcessCalibrationProfile | null {
  const nodes = new Map(graph.nodes.map((node) => [node.node_key, node]));
  const pending = [nodeKey];
  const visited = new Set<string>();
  const candidates: RuntimeStemCurvatureProcessCalibrationProfile[] = [];
  while (pending.length) {
    const current = pending.shift();
    if (!current || visited.has(current)) continue;
    visited.add(current);
    for (const edge of graph.edges.filter((item) => item.to_node_key === current)) {
      const upstream = nodes.get(edge.from_node_key);
      if (!upstream) continue;
      if (upstream.node_type === 'preprocessing.process_coordinate_rectify') {
        const profile = normalizeStemCurvatureProcessCalibrationProfile(upstream.config.calibration);
        if (profile) candidates.push(profile);
      } else {
        pending.push(upstream.node_key);
      }
    }
  }
  const hashes = new Set(candidates.map((item) => item.calibration_hash));
  return candidates.length && hashes.size === 1 ? candidates[0] : null;
}

export function upstreamStemSurfacePhaseCalibrationProfile(
  graph: RuntimePipelineGraph,
  nodeKey: string
): RuntimeStemSurfacePhaseCalibrationProfile | null {
  const nodes = new Map(graph.nodes.map((node) => [node.node_key, node]));
  const pending = [nodeKey];
  const visited = new Set<string>();
  const candidates: RuntimeStemSurfacePhaseCalibrationProfile[] = [];
  while (pending.length) {
    const current = pending.shift();
    if (!current || visited.has(current)) continue;
    visited.add(current);
    for (const edge of graph.edges.filter((item) => item.to_node_key === current)) {
      const upstream = nodes.get(edge.from_node_key);
      if (!upstream) continue;
      if (upstream.node_type === 'analysis.stem_surface_phase') {
        const profile = normalizeStemSurfacePhaseCalibrationProfile(
          upstream.config.surface_phase_calibration
        );
        if (profile) candidates.push(profile);
      } else {
        pending.push(upstream.node_key);
      }
    }
  }
  const hashes = new Set(candidates.map((item) => item.calibration_hash));
  return candidates.length && hashes.size === 1 ? candidates[0] : null;
}

export function upstreamIntrinsicCalibrationProfile(
  graph: RuntimePipelineGraph,
  nodeKey: string
): RuntimeCameraIntrinsicCalibrationProfile | null {
  const nodes = new Map(graph.nodes.map((node) => [node.node_key, node]));
  const pending = [nodeKey];
  const visited = new Set<string>();
  const candidates: RuntimeCameraIntrinsicCalibrationProfile[] = [];
  while (pending.length) {
    const current = pending.shift();
    if (!current || visited.has(current)) continue;
    visited.add(current);
    for (const edge of graph.edges.filter((item) => item.to_node_key === current)) {
      const upstream = nodes.get(edge.from_node_key);
      if (!upstream) continue;
      if (upstream.node_type === 'preprocessing.camera_undistort') {
        const profile = normalizeCameraIntrinsicCalibrationProfile(upstream.config.calibration);
        if (profile?.status === 'valid') candidates.push(profile);
      } else {
        pending.push(upstream.node_key);
      }
    }
  }
  const hashes = new Set(candidates.map((item) => item.calibration_hash));
  return candidates.length && hashes.size === 1 ? candidates[0] : null;
}

export function stemProcessUndistortedCalibrationFrame(
  liveState: RuntimeLiveViewState | null,
  intrinsicProfile: RuntimeCameraIntrinsicCalibrationProfile | null
): RuntimeLiveFrame | null {
  const candidates = [liveState?.undistorted_frame ?? null, liveState?.frame ?? null];
  for (const frame of candidates) {
    if (!frame?.image_base64 || !frame.width || !frame.height || !intrinsicProfile) continue;
    if (
      frame.width === intrinsicProfile.image_width &&
      frame.height === intrinsicProfile.image_height &&
      frame.metadata.perspective_rectified !== true
    ) {
      const imageContext =
        frame.image_context && typeof frame.image_context === 'object'
          ? (frame.image_context as Record<string, unknown>)
          : null;
      const transform =
        imageContext?.transform && typeof imageContext.transform === 'object'
          ? (imageContext.transform as Record<string, unknown>)
          : null;
      const parameters =
        transform?.parameters && typeof transform.parameters === 'object'
          ? (transform.parameters as Record<string, unknown>)
          : null;
      const intrinsicHash =
        frame.metadata.calibration_hash ??
        frame.metadata.intrinsic_calibration_hash ??
        parameters?.calibration_hash;
      if (
        transform?.transform_type === 'undistort' &&
        intrinsicHash === intrinsicProfile.calibration_hash
      ) {
        return frame;
      }
    }
  }

  if (intrinsicProfile?.calibration_method !== 'IDENTITY_PASSTHROUGH') return null;
  const sourceFrame = intrinsicCalibrationSourceFrame(liveState);
  return sourceFrame?.image_base64 &&
    sourceFrame.width === intrinsicProfile.image_width &&
    sourceFrame.height === intrinsicProfile.image_height
    ? sourceFrame
    : null;
}

function stemProcessPolygonsFromProfile(
  profile: RuntimeStemCurvatureProcessCalibrationProfile | null
): StemProcessPolygonDrafts {
  return {
    candidate_roi: profile?.candidate_roi.image_points ?? [],
    curvature_observation_roi: profile?.curvature_observation_roi.image_points ?? [],
    surface_tracking_roi: profile?.surface_tracking_roi.image_points ?? []
  };
}

function stemProcessGatesFromProfile(
  profile: RuntimeStemCurvatureProcessCalibrationProfile | null
): StemProcessGateDrafts {
  return {
    session_start_gate: profile
      ? [profile.session_start_gate.image_start, profile.session_start_gate.image_end]
      : [],
    curvature_freeze_gate: profile
      ? [profile.curvature_freeze_gate.image_start, profile.curvature_freeze_gate.image_end]
      : [],
    decision_gate: profile
      ? [profile.decision_gate.image_start, profile.decision_gate.image_end]
      : []
  };
}

function stemProcessGateRequest(
  points: RuntimeStemCurvatureImagePoint[]
): RuntimeStemCurvatureImageGate {
  if (points.length !== 2) throw new Error('Each process gate requires exactly two points');
  return { start: points[0], end: points[1] };
}

function stemProcessAxesInImage(profile: RuntimeStemCurvatureProcessCalibrationProfile) {
  const bounds = profile.process_bounds;
  const origin = projectProcessPointToImage(
    profile.homography_process_to_image,
    bounds.x_min_mm,
    bounds.y_min_mm
  );
  const xEnd = projectProcessPointToImage(
    profile.homography_process_to_image,
    bounds.x_min_mm + (bounds.x_max_mm - bounds.x_min_mm) * 0.2,
    bounds.y_min_mm
  );
  const yEnd = projectProcessPointToImage(
    profile.homography_process_to_image,
    bounds.x_min_mm,
    bounds.y_min_mm + (bounds.y_max_mm - bounds.y_min_mm) * 0.2
  );
  return origin && xEnd && yEnd ? { origin, xEnd, yEnd } : null;
}

export function projectProcessPointToImage(
  homography: number[],
  xMm: number,
  yMm: number
): RuntimeStemCurvatureImagePoint | null {
  if (homography.length !== 9 || homography.some((value) => !Number.isFinite(value))) return null;
  const denominator = homography[6] * xMm + homography[7] * yMm + homography[8];
  if (!Number.isFinite(denominator) || Math.abs(denominator) <= 1e-12) return null;
  const xPx = (homography[0] * xMm + homography[1] * yMm + homography[2]) / denominator;
  const yPx = (homography[3] * xMm + homography[4] * yMm + homography[5]) / denominator;
  return Number.isFinite(xPx) && Number.isFinite(yPx) ? { x_px: xPx, y_px: yPx } : null;
}

function cameraCalibrationObservationId(frame: RuntimeLiveFrame, index: number) {
  const stamp = `${frame.captured_at ?? frame.source_timestamp ?? 'frame'}`
    .replace(/[^a-zA-Z0-9._-]+/g, '-')
    .slice(0, 48);
  return `observation-${stamp || 'frame'}-${Math.max(frame.sequence, 0)}-${index}`.slice(0, 80);
}

function cameraCalibrationFrameKey(frame: RuntimeLiveFrame) {
  return [
    frame.captured_at ?? '',
    frame.source_timestamp ?? '',
    frame.sequence,
    frame.width ?? '',
    frame.height ?? ''
  ].join(':');
}

export function intrinsicCalibrationSourceFrame(
  liveState: RuntimeLiveViewState | null
): RuntimeLiveFrame | null {
  if (liveState?.source_frame?.image_base64) {
    return liveState.source_frame;
  }
  const frame = liveState?.frame ?? null;
  if (!frame?.image_base64) return null;
  const imageContextKind = String(frame.image_context?.kind ?? '').toLocaleLowerCase();
  const displayContextKind = String(liveState?.display_context?.kind ?? '').toLocaleLowerCase();
  const purpose = String(liveState?.display_context?.purpose ?? '').toLocaleLowerCase();
  if (
    ['original', 'source'].includes(imageContextKind) ||
    ['original', 'source'].includes(displayContextKind) ||
    purpose === 'runtime_calibration'
  ) {
    return frame;
  }
  return null;
}

function calibrationFrameImageSource(frame: RuntimeLiveFrame | null | undefined) {
  if (!frame?.image_base64) return null;
  return `data:${frame.mime_type || 'image/jpeg'};base64,${frame.image_base64}`;
}

function formatCalibrationError(value: number | null | undefined, unit = 'px') {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${value.toFixed(4)} ${unit}`
    : 'n/a';
}

function minimumPrincipleValidationStatusLabel(
  status: RuntimeStemMinimumPrincipleValidationArtifact['status']
) {
  return {
    VALIDATION_PASSED: 'Objective validation passed',
    VALIDATION_FAILED: 'Objective validation failed',
    INSUFFICIENT_DATA: 'Insufficient objective validation data'
  }[status];
}

function formatValidationMetric(value: number | null | undefined, unit: string) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${value.toFixed(2)}${unit}`
    : 'n/a';
}

function formatValidationPercent(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : 'n/a';
}

function downloadTextReport(fileName: string, content: string, mimeType: string) {
  const blob = new Blob([content], { type: `${mimeType};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName.replace(/[^a-zA-Z0-9._-]+/g, '-');
  link.click();
  URL.revokeObjectURL(url);
}

function boardSurfaceLabel(role: BoardSurfaceCalibration['surface_role']) {
  return {
    top: 'Top surface',
    bottom: 'Bottom surface',
    side_a: 'Side A',
    side_b: 'Side B',
    end_face: 'End face'
  }[role];
}

function normalizeBoardSurfaceCalibrations(value: unknown): BoardSurfaceCalibration[] {
  const values = Array.isArray(value) ? value : value && typeof value === 'object' ? [value] : [];
  return values
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .filter((item) => ['top', 'bottom', 'side_a', 'side_b', 'end_face'].includes(String(item.surface_role)))
    .map((item) => ({
      surface_role: item.surface_role as BoardSurfaceCalibration['surface_role'],
      image_points: normalizeBoardCalibrationPoints(item.image_points, true),
      world_points_mm: normalizeBoardCalibrationPoints(item.world_points_mm, false)
    }));
}

function normalizeBoardCalibrationPoints(value: unknown, normalized: boolean) {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .slice(0, 4)
    .map((item) => ({
      x: normalized
        ? clampNumber(finiteNumber(item.x, 0), 0, 1)
        : finiteNumber(item.x, 0),
      y: normalized
        ? clampNumber(finiteNumber(item.y, 0), 0, 1)
        : finiteNumber(item.y, 0)
    }));
}

function blankBoardSurface(
  role: BoardSurfaceCalibration['surface_role'],
  node: RuntimeNodeSpec
): BoardSurfaceCalibration {
  const inspection = finiteNumber(node.config.inspection_length_mm, 300);
  const margin = finiteNumber(node.config.safe_visibility_margin_mm, 100);
  const length = role === 'end_face' ? 100 : inspection + margin;
  const width = role === 'end_face' ? 30 : 100;
  return withBoardSurfaceDimensions(
    { surface_role: role, image_points: [], world_points_mm: [] },
    length,
    width
  );
}

function boardSurfaceDimensions(surface: BoardSurfaceCalibration) {
  const xs = surface.world_points_mm.map((point) => point.x);
  const ys = surface.world_points_mm.map((point) => point.y);
  return {
    length: xs.length ? Math.max(...xs) - Math.min(...xs) : 1,
    width: ys.length ? Math.max(...ys) - Math.min(...ys) : 1
  };
}

function withBoardSurfaceDimensions(
  surface: BoardSurfaceCalibration,
  length: number,
  width: number
): BoardSurfaceCalibration {
  const safeLength = Math.max(length, 1);
  const safeWidth = Math.max(width, 1);
  return {
    ...surface,
    world_points_mm: [
      { x: 0, y: 0 },
      { x: safeLength, y: 0 },
      { x: safeLength, y: safeWidth },
      { x: 0, y: safeWidth }
    ]
  };
}

export function formatModelCapabilityOption(option: RuntimeModelCapabilityOption) {
  const project = option.capability.label_project_title;
  const prefix = typeof project === 'string' && project.trim() ? `${project} / ` : '';
  return `${prefix}${option.display_name} | ${option.version_label} | ${modelArchitectureLabel(option)} | ${option.deployment_status}`;
}

function modelArchitectureLabel(option: RuntimeModelCapabilityOption) {
  const displayName = option.architecture_display_name?.trim();
  if (displayName) {
    return displayName;
  }
  const family = option.architecture_family?.trim().toUpperCase();
  const version = option.architecture_version?.trim();
  const size = option.architecture_size?.trim();
  const name = `${family ?? ''}${version ?? ''}`.trim();
  return [name || 'Architecture not recorded', size].filter(Boolean).join(' ');
}

export function modelCapabilityMatchesSelectionPolicy(
  option: RuntimeModelCapabilityOption,
  selectionPolicy: RuntimeLogicalModelRef['selection_policy']
) {
  if (!option.active) {
    return false;
  }
  const status = option.deployment_status.trim().toLowerCase();
  if (selectionPolicy === 'active_production') {
    return status === 'production' && (
      !option.capability.label_project_id || option.capability.project_active_production === true
    );
  }
  if (selectionPolicy === 'staged_test') {
    return ['staged', 'testing', 'test', 'production', 'activated', 'active'].includes(status);
  }
  return ['production', 'staged', 'activated', 'active', 'testing', 'test'].includes(status);
}

function describeModelBinding(option: RuntimeModelCapabilityOption | null) {
  if (!option) {
    return 'Choose a deployed capability before publishing.';
  }
  const family = option.architecture_family ?? option.model_family_slug;
  const version = option.architecture_version ?? option.version_label;
  const size = option.architecture_size ? ` / ${option.architecture_size}` : '';
  return `${family} / ${version}${size} / ${option.target_path} / ${option.deployment_status}`;
}

function modelCapabilityMatchesReference(
  option: RuntimeModelCapabilityOption,
  modelRef: RuntimeLogicalModelRef
) {
  return modelRef.selection_policy === 'active_production' && modelRef.label_project_id
    ? option.capability.label_project_id === modelRef.label_project_id &&
        option.capability.project_active_production === true
    : option.model_family_slug === modelRef.model_family_slug;
}

export function resolveSelectedModelReleaseId(
  modelRef: RuntimeLogicalModelRef,
  options: RuntimeModelCapabilityOption[]
) {
  if (modelRef.selection_policy === 'explicit_release' && modelRef.deployment_release_id) {
    return modelRef.deployment_release_id;
  }
  return (
    options.find(
      (item) =>
        modelCapabilityMatchesReference(item, modelRef) &&
        item.contract_major === modelRef.contract_major &&
        item.target_path === modelRef.target_path &&
        item.task_type === modelRef.required_task_type
    )?.deployment_release_id ?? ''
  );
}

export function buildLogicalModelRefFromCapability(
  capability: RuntimeModelCapabilityOption,
  taskType: TaskType,
  selectionPolicy: RuntimeLogicalModelRef['selection_policy']
): RuntimeLogicalModelRef {
  return {
    model_family_slug: capability.model_family_slug,
    ...(selectionPolicy === 'active_production' && typeof capability.capability.label_project_id === 'string'
      ? { label_project_id: capability.capability.label_project_id }
      : {}),
    target_path: capability.target_path,
    contract_major: capability.contract_major,
    selection_policy: selectionPolicy,
    required_task_type: capability.task_type ?? taskType,
    deployment_release_id:
      selectionPolicy === 'explicit_release' ? capability.deployment_release_id : null
  };
}
