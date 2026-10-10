import { Tabs } from '../../components/workspace-ui';
import { LabelStudioProjectLink } from './label-studio-link';
import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  AnomalyInferenceResponse,
  ClassificationInferenceResponse,
  CleanupExecutionRequest,
  DatasetSummary,
  DeploymentReleaseSummary,
  DetectionInferenceResponse,
  LabelProjectSummary,
  LabelProjectMappingRequest,
  ModelLifecycleStageState,
  ModelTemplateSummary,
  PipelineActivityItem,
  ProjectAutomationPolicy,
  ProjectModelCreateRequest,
  ProjectModelSummary,
  ProjectModelWorkspaceDetail,
  ProjectPipelineDetail,
  PromotionCriteriaPolicy,
  SegmentationInferenceResponse,
  SupportedModelArchitecture,
  TrainingDefaults,
  TrainingJobDetail,
  TrainingJobSummary,
  TrainingMetricRecord
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { confirmConsequentialAction, describeError } from '../../app/errors';
import { CleanupDialog } from '../../components/cleanup-dialog';
import { useNotifications } from '../../components/notification-provider';
import { INTERACTIVE_INFERENCE_TIMEOUT_MS } from '../inference/test-timeout';
import {
  architectureLabel,
  deploymentModelsForVersions,
  metricChartDomain,
  resolveTrainingProgress,
  taskTypeLabel,
  templateOptionLabel
} from './modeling';
import { formatDate, trainingTone } from './presentation';

type ModelView = 'status' | 'metrics' | 'parameters' | 'deployment';
type UploadMode = 'raw' | 'classified';
type UploadPhase = 'idle' | 'creating' | 'uploading' | 'finalizing';
type ProjectInferenceResult =
  | ClassificationInferenceResponse
  | DetectionInferenceResponse
  | SegmentationInferenceResponse
  | AnomalyInferenceResponse;

const terminalTrainingStates = ['completed', 'failed', 'cancelled', 'rolled_back_if_needed'];
const TEST_INFERENCE_MAX_IMAGE_BYTES = 30 * 1024 ** 2;

export function ProjectWorkspacePage() {
  const [workspaceTab, setWorkspaceTab] = useState(() => new URLSearchParams(window.location.search).has('modelFamilyId') ? 'Modelle' : 'Dataset');
  const { labelProjectId } = useParams({ from: '/authenticated/pipelines/$labelProjectId' });
  const search = useSearch({ from: '/authenticated/pipelines/$labelProjectId' });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [uploadMode, setUploadMode] = useState<UploadMode>('raw');
  const [classificationLabel, setClassificationLabel] = useState('');
  const [uploadPhase, setUploadPhase] = useState<UploadPhase>('idle');
  const [uploadedFiles, setUploadedFiles] = useState(0);
  const [uploadedBytes, setUploadedBytes] = useState(0);
  const [activeUploadSessionId, setActiveUploadSessionId] = useState<string | null>(null);
  const [createModelOpen, setCreateModelOpen] = useState(false);
  const [templateDrawerOpen, setTemplateDrawerOpen] = useState(false);
  const [modelName, setModelName] = useState('');
  const [modelDescription, setModelDescription] = useState('');
  const [modelTemplateKey, setModelTemplateKey] = useState('');
  const [modelArchitectureKey, setModelArchitectureKey] = useState('');
  const [modelClassNames, setModelClassNames] = useState<string[]>([]);
  const [modelUseAllClasses, setModelUseAllClasses] = useState(true);
  const [modelSegmentationMode, setModelSegmentationMode] = useState<'instance' | 'semantic' | null>(null);
  const [modelTrainingOverrides, setModelTrainingOverrides] = useState<TrainingDefaults | null>(null);
  const [modelGatePolicy, setModelGatePolicy] = useState<PromotionCriteriaPolicy | null>(null);
  const [policyDraft, setPolicyDraft] = useState<ProjectAutomationPolicy | null>(null);
  const [mappingDraft, setMappingDraft] = useState<LabelProjectMappingRequest | null>(null);
  const [labelConfigJson, setLabelConfigJson] = useState('{}');
  const [inferenceFile, setInferenceFile] = useState<File | null>(null);
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [deleteEntireProject, setDeleteEntireProject] = useState(false);

  const pipelineQuery = useQuery({
    queryKey: ['project-pipeline', labelProjectId],
    queryFn: () => api.pipelines.detail(labelProjectId),
    refetchInterval: 5000
  });
  const labelProjectsQuery = useQuery({
    queryKey: ['label-projects'],
    queryFn: api.datasets.projects,
    refetchInterval: 15000
  });
  const datasetsQuery = useQuery({ queryKey: ['datasets'], queryFn: api.datasets.list });
  const sessionQuery = useQuery({ queryKey: ['session'], queryFn: api.admin.me });
  const selectedModelId = search.modelFamilyId;
  const modelView = (search.modelView ?? 'status') as ModelView;
  const workspaceQuery = useQuery({
    queryKey: ['project-model-workspace', labelProjectId, selectedModelId],
    queryFn: () => api.pipelines.modelWorkspace(labelProjectId, selectedModelId!),
    enabled: Boolean(selectedModelId),
    refetchInterval: (query) =>
      query.state.data?.lifecycle.some((stage) => stage.state === 'running') ? 2000 : 10000
  });
  const deploymentReleasesQuery = useQuery({
    queryKey: ['deployment-releases'],
    queryFn: api.deployments.releases,
    enabled: Boolean(selectedModelId) && modelView === 'deployment',
    refetchInterval: modelView === 'deployment' ? 5000 : false
  });
  const trainingQuery = useQuery({
    queryKey: ['training-job', selectedRunId],
    queryFn: () => api.training.detail(selectedRunId!),
    enabled: Boolean(selectedRunId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status && !terminalTrainingStates.includes(status) ? 2000 : false;
    }
  });
  const uploadSessionQuery = useQuery({
    queryKey: ['upload-session', activeUploadSessionId],
    queryFn: () => api.datasets.uploadSession(activeUploadSessionId!),
    enabled: Boolean(activeUploadSessionId) && uploadPhase === 'finalizing',
    refetchInterval: 1000
  });
  const cleanupPreviewQuery = useQuery({
    queryKey: ['pipeline-cleanup-preview', labelProjectId, deleteEntireProject],
    queryFn: () =>
      api.pipelines.cleanupPreview(labelProjectId, { deleteEntirePipeline: deleteEntireProject }),
    enabled: cleanupOpen
  });

  const pipeline = pipelineQuery.data;
  const currentLabelProject = labelProjectsQuery.data?.items.find(
    (item) => item.label_project_id === labelProjectId
  );
  const selectedModel = pipeline?.models.find((model) => model.model_family_id === selectedModelId);
  const fallbackTrainingHistory = useMemo(
    () =>
      selectedModel
        ? (pipeline?.training_history ?? []).filter(
            (run) => run.model_family_slug === selectedModel.slug
          )
        : [],
    [pipeline?.training_history, selectedModel]
  );
  const modelTrainingHistory = useMemo(() => {
    const byId = new Map<string, TrainingJobSummary>();
    [...(workspaceQuery.data?.training_history ?? []), ...fallbackTrainingHistory].forEach(
      (run) => byId.set(run.training_job_id, run)
    );
    return [...byId.values()].sort(
      (left, right) => Date.parse(right.created_at) - Date.parse(left.created_at)
    );
  }, [fallbackTrainingHistory, workspaceQuery.data?.training_history]);
  const modelDeploymentReleases = useMemo(
    () => deploymentModelsForVersions(
      deploymentReleasesQuery.data?.items ?? [],
      workspaceQuery.data?.versions ?? []
    ),
    [deploymentReleasesQuery.data?.items, workspaceQuery.data?.versions]
  );
  const deploymentReleasesError = deploymentReleasesQuery.isError
    ? describeError(deploymentReleasesQuery.error, 'Deployment-Status konnte nicht geladen werden')
    : null;
  const workspaceError = workspaceQuery.isError
    ? describeError(workspaceQuery.error, 'Modell-Lifecycle konnte nicht geladen werden')
    : null;
  const canWrite = sessionQuery.data?.user?.role !== 'viewer';
  const selectedTemplate = pipeline?.model_templates.find(
    (template) => template.template_key === modelTemplateKey
  );
  const totalBytes = useMemo(() => files.reduce((sum, file) => sum + file.size, 0), [files]);
  const filePreviews = useMemo(
    () => files.slice(0, 8).map((file) => ({ file, url: URL.createObjectURL(file) })),
    [files]
  );

  useEffect(() => () => filePreviews.forEach((preview) => URL.revokeObjectURL(preview.url)), [filePreviews]);

  useEffect(() => {
    if (!pipeline) return;
    const validSelection = pipeline.models.some((model) => model.model_family_id === selectedModelId);
    const nextModelId = validSelection ? selectedModelId : pipeline.models[0]?.model_family_id;
    if (nextModelId !== selectedModelId) {
      void navigate({
        to: '/pipelines/$labelProjectId',
        params: { labelProjectId },
        search: { modelFamilyId: nextModelId, modelView },
        replace: true,
        resetScroll: false
      });
    }
    if (!modelTemplateKey) {
      const template = pipeline.model_templates.find((item) => item.recommended && item.active)
        ?? pipeline.model_templates.find((item) => item.active);
      if (template) setModelTemplateKey(template.template_key);
    }
  }, [labelProjectId, modelTemplateKey, modelView, navigate, pipeline, selectedModelId]);

  useEffect(() => {
    setSelectedRunId(null);
  }, [selectedModelId]);

  useEffect(() => {
    if (!selectedRunId && modelTrainingHistory[0]) {
      setSelectedRunId(modelTrainingHistory[0].training_job_id);
    }
  }, [modelTrainingHistory, selectedRunId]);

  useEffect(() => {
    setPolicyDraft(null);
    setMappingDraft(null);
    setFiles([]);
    setSelectedRunId(null);
  }, [labelProjectId]);

  useEffect(() => {
    if (pipeline && !policyDraft) setPolicyDraft(pipeline.automation_policy);
  }, [pipeline, policyDraft]);

  useEffect(() => {
    if (!currentLabelProject) return;
    const nextDraft: LabelProjectMappingRequest = {
      dataset_id: currentLabelProject.dataset_id ?? '',
      label_studio_project_id: currentLabelProject.label_studio_project_id,
      title: currentLabelProject.title,
      task_type: currentLabelProject.task_type,
      training_pipeline_slug:
        currentLabelProject.training_pipeline_slug
        ?? currentLabelProject.title.toLowerCase().replace(/[^a-z0-9]+/g, '-'),
      auto_snapshot_enabled: currentLabelProject.auto_snapshot_enabled,
      minimum_annotation_count: currentLabelProject.minimum_annotation_count,
      required_classes: currentLabelProject.required_classes,
      qualifying_actions: currentLabelProject.qualifying_actions,
      label_config: currentLabelProject.label_config,
      train_ratio: currentLabelProject.train_ratio,
      val_ratio: currentLabelProject.val_ratio,
      test_ratio: currentLabelProject.test_ratio,
      class_balance_strategy: currentLabelProject.class_balance_strategy
    };
    setMappingDraft(nextDraft);
    setLabelConfigJson(JSON.stringify(nextDraft.label_config, null, 2));
  }, [currentLabelProject?.label_project_id]);

  useEffect(() => {
    if (!selectedTemplate) return;
    setModelGatePolicy(selectedTemplate.default_promotion_policy);
    setModelArchitectureKey(architectureKey(selectedTemplate.architecture));
    setModelTrainingOverrides(selectedTemplate.defaults.training);
    const templateArchitecture = pipeline?.supported_model_architectures.find(
      (item) => architectureKey(item.architecture) === architectureKey(selectedTemplate.architecture)
    );
    setModelSegmentationMode(
      selectedTemplate.task_type === 'segmentation'
        ? (templateArchitecture?.capability_metadata.segmentation_mode as 'instance' | 'semantic' | undefined)
          ?? (selectedTemplate.architecture.family === 'yolo' ? 'instance' : 'semantic')
        : null
    );
    // Query polling must not reset operator edits while the selected template stays unchanged.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTemplate?.template_key]);

  const invalidateWorkspace = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['project-pipeline', labelProjectId] }),
      queryClient.invalidateQueries({ queryKey: ['project-model-workspace', labelProjectId] }),
      queryClient.invalidateQueries({ queryKey: ['project-pipelines'] }),
      queryClient.invalidateQueries({ queryKey: ['label-projects'] }),
      queryClient.invalidateQueries({ queryKey: ['datasets'] }),
      queryClient.invalidateQueries({ queryKey: ['training-job'] })
    ]);
  };
  const mutationError = (error: unknown, title: string) =>
    notify({ ...describeError(error, title), tone: 'error' });

  const syncMutation = useMutation({
    mutationFn: () => api.pipelines.sync(labelProjectId),
    onSuccess: async () => {
      await invalidateWorkspace();
      notify({ title: 'Synchronisierung wurde gestartet', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Synchronisierung fehlgeschlagen')
  });

  const uploadMutation = useMutation({
    mutationFn: async () => {
      if (!pipeline?.dataset_id) throw new Error('Dem Projekt ist noch kein Dataset zugeordnet.');
      if (!files.length) throw new Error('Bitte zuerst Bilder auswählen.');
      if (uploadMode === 'classified' && !classificationLabel.trim()) {
        throw new Error('Für den klassifizierten Upload muss eine Klasse gewählt werden.');
      }
      setUploadPhase('creating');
      setUploadedBytes(0);
      setUploadedFiles(0);
      const uploadSession = await api.datasets.createUploadSession(pipeline.dataset_id, {
        expected_file_count: files.length,
        target_label_project_id: uploadMode === 'classified' ? labelProjectId : undefined,
        classification_label: uploadMode === 'classified' ? classificationLabel.trim() : undefined,
        source_folder_name: uploadMode === 'classified' ? classificationLabel.trim() : undefined
      });
      setActiveUploadSessionId(uploadSession.upload_session_id);
      setUploadPhase('uploading');
      let completedBytes = 0;
      for (const [index, file] of files.entries()) {
        await api.datasets.uploadPart(
          uploadSession.upload_session_id,
          index + 1,
          file.webkitRelativePath || file.name,
          file,
          file.type || inferContentType(file.name)
        );
        completedBytes += file.size;
        setUploadedFiles(index + 1);
        setUploadedBytes(completedBytes);
      }
      setUploadPhase('finalizing');
      return api.datasets.completeUploadSession(uploadSession.upload_session_id, {});
    },
    onSuccess: () => notify({ title: 'Upload wird im Hintergrund abgeschlossen', tone: 'success' }),
    onError: (error) => {
      setUploadPhase('idle');
      mutationError(error, 'Dataset-Upload fehlgeschlagen');
    }
  });

  useEffect(() => {
    const session = uploadSessionQuery.data;
    if (session?.status === 'completed') {
      setUploadPhase('idle');
      setFiles([]);
      void invalidateWorkspace();
      notify({ title: `${session.received_file_count} Dateien sind im Dataset verfügbar`, tone: 'success' });
    } else if (session?.status === 'failed') {
      setUploadPhase('idle');
      notify({ title: 'Verarbeitung des Uploads fehlgeschlagen', tone: 'error' });
    }
  }, [uploadSessionQuery.data?.status]);

  const createModelMutation = useMutation({
    mutationFn: () => {
      if (!selectedTemplate) throw new Error('Bitte ein Modell-Template auswählen.');
      const architecture = pipeline?.supported_model_architectures.find(
        (item) => architectureKey(item.architecture) === modelArchitectureKey
      )?.architecture ?? selectedTemplate.architecture;
      const payload: ProjectModelCreateRequest = {
        display_name: modelName.trim() || undefined,
        description: modelDescription.trim() || undefined,
        task_type: selectedTemplate.task_type,
        model_template_key: selectedTemplate.template_key,
        architecture_family: architecture.family,
        architecture_version: architecture.version,
        architecture_size: architecture.size,
        class_names: modelUseAllClasses ? [] : modelClassNames,
        segmentation_mode: selectedTemplate.task_type === 'segmentation'
          ? modelSegmentationMode
          : null,
        training_overrides: modelTrainingOverrides,
        promotion_policy: modelGatePolicy?.allow_model_override ? modelGatePolicy : undefined
      };
      return api.pipelines.createModel(labelProjectId, payload);
    },
    onSuccess: async (model) => {
      setCreateModelOpen(false);
      setModelName('');
      setModelDescription('');
      setModelClassNames([]);
      setModelUseAllClasses(true);
      await invalidateWorkspace();
      await navigate({
        to: '/pipelines/$labelProjectId',
        params: { labelProjectId },
        search: { modelFamilyId: model.model_family_id, modelView: 'status' },
        replace: true,
        resetScroll: false
      });
      notify({ title: `Modell „${model.display_name}“ wurde angelegt`, tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Modell konnte nicht angelegt werden')
  });

  const trainMutation = useMutation({
    mutationFn: () =>
      api.pipelines.train(labelProjectId, {
        model_family_id: selectedModelId,
        dataset_snapshot_id: pipeline?.latest_valid_snapshot?.snapshot_id,
        allow_duplicate_snapshot_training: false
      }),
    onSuccess: async (job) => {
      setSelectedRunId(job.job_id);
      await invalidateWorkspace();
      setModelView('metrics');
      notify({ title: 'Training wurde gestartet', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Training konnte nicht gestartet werden')
  });

  const cancelMutation = useMutation({
    mutationFn: () => api.training.cancel(selectedRunId!, { reason: 'Abbruch in der Projektansicht.' }),
    onSuccess: invalidateWorkspace,
    onError: (error) => mutationError(error, 'Training konnte nicht abgebrochen werden')
  });

  const deployMutation = useMutation({
    mutationFn: ({ bypass }: { bypass: boolean }) =>
      api.pipelines.deployModel(labelProjectId, selectedModelId!, {
        model_family_id: selectedModelId,
        preferred_endpoint_mode: policyDraft?.preferred_endpoint_mode,
        allow_quality_gate_bypass: bypass
      }),
    onSuccess: async () => {
      await invalidateWorkspace();
      notify({ title: 'Deployment wurde für das ausgewählte Modell gestartet', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Deployment konnte nicht gestartet werden')
  });

  const refreshModelDeployment = async () => {
    await Promise.all([
      invalidateWorkspace(),
      queryClient.invalidateQueries({ queryKey: ['deployment-releases'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-release-detail'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-targets'] })
    ]);
  };

  const deactivateModelMutation = useMutation({
    mutationFn: (deploymentReleaseId: string) =>
      api.deployments.deactivateModel(deploymentReleaseId, {
        reason: 'Modellbetrieb in der Projekt-Modellansicht deaktiviert.'
      }),
    onSuccess: async () => {
      await refreshModelDeployment();
      notify({ title: 'Deaktivierung des Modells wurde gestartet', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Modell konnte nicht deaktiviert werden')
  });

  const activateModelMutation = useMutation({
    mutationFn: (deploymentReleaseId: string) =>
      api.deployments.activateModel(deploymentReleaseId, {
        reason: 'Modellbetrieb in der Projekt-Modellansicht aktiviert.'
      }),
    onSuccess: async () => {
      await refreshModelDeployment();
      notify({ title: 'Aktivierung des Modells wurde gestartet', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Modell konnte nicht aktiviert werden')
  });

  const linkPredictionsMutation = useMutation({
    mutationFn: () => api.pipelines.linkLabelStudioPredictions(labelProjectId, selectedModelId),
    onSuccess: async () => {
      await invalidateWorkspace();
      notify({ title: 'Label-Studio-Verbindung wurde aktualisiert', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Label Studio konnte nicht verbunden werden')
  });

  const policyMutation = useMutation({
    mutationFn: (policy: ProjectAutomationPolicy) => api.pipelines.updatePolicy(labelProjectId, policy),
    onSuccess: async (policy) => {
      setPolicyDraft(policy);
      await invalidateWorkspace();
      notify({ title: 'Automatismus wurde gespeichert', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Automatismus konnte nicht gespeichert werden')
  });

  const mappingMutation = useMutation({
    mutationFn: (draft: LabelProjectMappingRequest) => api.datasets.mapProject(draft),
    onSuccess: async () => {
      await invalidateWorkspace();
      notify({ title: 'Projekt- und Dataset-Regeln wurden gespeichert', tone: 'success' });
    },
    onError: (error) => mutationError(error, 'Dataset-Regeln konnten nicht gespeichert werden')
  });

  const gateMutation = useMutation({
    mutationFn: (payload: { model: ProjectModelSummary; policy: PromotionCriteriaPolicy }) =>
      api.pipelines.updateModel(labelProjectId, payload.model.model_family_id, {
        promotion_policy: payload.policy
      }),
    onSuccess: invalidateWorkspace,
    onError: (error) => mutationError(error, 'Quality Gates konnten nicht gespeichert werden')
  });

  const inferenceMutation = useMutation({
    mutationFn: async () => {
      if (!inferenceFile || !selectedModel) throw new Error('Bitte ein Testbild auswählen.');
      const endpoint = pipeline?.current_endpoints.find(
        (item) => item.model_family_id === selectedModel.model_family_id
      );
      if (!endpoint?.realtime_available || !endpoint.realtime_release_id) {
        throw new Error('Für dieses Modell ist kein aktives Realtime-Deployment verfügbar.');
      }
      const params = {
        deploymentTarget: 'realtime',
        deploymentReleaseId: endpoint.realtime_release_id,
        timeoutMs: INTERACTIVE_INFERENCE_TIMEOUT_MS
      };
      if (selectedModel.task_type === 'classification') {
        return api.inference.realtimeClassifyBytes(inferenceFile, params);
      }
      if (selectedModel.task_type === 'detection' || selectedModel.task_type === 'oriented_detection') {
        return api.inference.realtimeDetectBytes(inferenceFile, params);
      }
      if (selectedModel.task_type === 'anomaly') {
        return api.inference.realtimeAnomalyBytes(inferenceFile, params);
      }
      return api.inference.realtimeSegmentBytes(inferenceFile, params);
    },
    onError: (error) => mutationError(error, 'Test-Inferenz fehlgeschlagen')
  });

  const cleanupMutation = useMutation({
    mutationFn: (payload: CleanupExecutionRequest) => api.pipelines.cleanup(labelProjectId, payload),
    onSuccess: async (_, payload) => {
      setCleanupOpen(false);
      if (payload.delete_entire_pipeline) await navigate({ to: '/pipelines' });
      else await invalidateWorkspace();
    },
    onError: (error) => mutationError(error, 'Projektbereinigung fehlgeschlagen')
  });

  function setModelView(view: ModelView) {
    void navigate({
      to: '/pipelines/$labelProjectId',
      params: { labelProjectId },
      search: { modelFamilyId: selectedModelId, modelView: view },
      replace: true,
      resetScroll: false
    });
  }

  if (!pipeline) {
    return <div className="screen-center">Projekt wird geladen...</div>;
  }

  const activeRun =
    trainingQuery.data?.training_job_id === selectedRunId ? trainingQuery.data : undefined;
  const automationModel = pipeline.models.find(
    (model) => model.model_family_id === policyDraft?.default_model_family_id
  );

  return (
    <div className="project-workspace panel-stack panel-stack--wide">
      <ProjectHeader pipeline={pipeline} />
      <Tabs tabs={['Dataset', 'Modelle', 'Automatisierung', 'Administration']} value={workspaceTab} onChange={setWorkspaceTab}/>

      <div className="workspace-tab-body" hidden={workspaceTab !== 'Dataset'}><DatasetSection
        pipeline={pipeline}
        labelProject={currentLabelProject}
        canWrite={canWrite}
        files={files}
        previews={filePreviews}
        uploadMode={uploadMode}
        classificationLabel={classificationLabel}
        uploadPhase={uploadPhase}
        uploadedFiles={uploadedFiles}
        uploadedBytes={uploadedBytes}
        totalBytes={totalBytes}
        mappingDraft={mappingDraft}
        labelConfigJson={labelConfigJson}
        datasets={datasetsQuery.data?.items ?? []}
        syncPending={syncMutation.isPending}
        uploadPending={uploadMutation.isPending}
        mappingPending={mappingMutation.isPending}
        onSync={() => syncMutation.mutate()}
        onFiles={setFiles}
        onUploadMode={setUploadMode}
        onClassificationLabel={setClassificationLabel}
        onUpload={() => uploadMutation.mutate()}
        onMappingDraft={setMappingDraft}
        onLabelConfigJson={setLabelConfigJson}
        onSaveMapping={() => {
          if (!mappingDraft) return;
          try {
            mappingMutation.mutate({
              ...mappingDraft,
              label_config: JSON.parse(labelConfigJson) as Record<string, unknown>
            });
          } catch {
            notify({ title: 'Die Label-Config enthält ungültiges JSON', tone: 'error' });
          }
        }}
      />

      </div><div className="workspace-tab-body" hidden={workspaceTab !== 'Modelle'}><ModelsSection
        pipeline={pipeline}
        selectedModel={selectedModel}
        workspace={workspaceQuery.data}
        workspaceLoading={workspaceQuery.isPending}
        workspaceError={workspaceError}
        trainingHistory={modelTrainingHistory}
        modelView={modelView}
        activeRun={activeRun}
        selectedRunId={selectedRunId}
        canWrite={canWrite}
        deployPending={deployMutation.isPending}
        deploymentReleases={modelDeploymentReleases}
        deploymentReleasesLoading={deploymentReleasesQuery.isPending || workspaceQuery.isPending}
        deploymentReleasesError={deploymentReleasesError}
        deploymentServingPending={deactivateModelMutation.isPending || activateModelMutation.isPending}
        trainPending={trainMutation.isPending}
        cancelPending={cancelMutation.isPending}
        linkPending={linkPredictionsMutation.isPending}
        inferenceFile={inferenceFile}
        inferencePending={inferenceMutation.isPending}
        inferenceResult={inferenceMutation.data}
        inferenceError={inferenceMutation.isError ? describeError(inferenceMutation.error, 'Test-Inferenz fehlgeschlagen') : null}
        onSelectModel={(modelFamilyId) =>
          void navigate({
            to: '/pipelines/$labelProjectId',
            params: { labelProjectId },
            search: { modelFamilyId, modelView: 'status' },
            replace: true,
            resetScroll: false
          })
        }
        onRetryWorkspace={() => void workspaceQuery.refetch()}
        onView={setModelView}
        onSelectRun={setSelectedRunId}
        onCreateModel={() => setCreateModelOpen(true)}
        onTemplates={() => setTemplateDrawerOpen(true)}
        onTrain={() => trainMutation.mutate()}
        onCancel={() => cancelMutation.mutate()}
        onDeploy={() => deployMutation.mutate({ bypass: false })}
        onForceDeploy={() => deployMutation.mutate({ bypass: true })}
        onRetryDeploymentReleases={() => void deploymentReleasesQuery.refetch()}
        onDeactivateModel={(release) => {
          if (
            confirmConsequentialAction({
              title: `${release.triton_model_name} deaktivieren?`,
              consequence:
                'Alle Versionen dieses Triton-Modells werden entladen. Deployment-Pakete, Berichte und Historie bleiben erhalten.',
              secondary:
                'Aktive Produktions-, Rollback-, Batch- oder laufende Kanalreferenzen verhindern die Deaktivierung.'
            })
          ) {
            deactivateModelMutation.mutate(release.deployment_release_id);
          }
        }}
        onActivateModel={(release) => activateModelMutation.mutate(release.deployment_release_id)}
        onLinkPredictions={() => linkPredictionsMutation.mutate()}
        onInferenceFile={(file) => {
          setInferenceFile(file);
          inferenceMutation.reset();
        }}
        onInference={() => inferenceMutation.mutate()}
      />

      </div><div className="workspace-tab-body" hidden={workspaceTab !== 'Automatisierung'}><AutomationSection
        pipeline={pipeline}
        policy={policyDraft}
        automationModel={automationModel}
        canWrite={canWrite}
        pending={policyMutation.isPending}
        gatePending={gateMutation.isPending}
        onPolicy={setPolicyDraft}
        onSave={() => policyDraft && policyMutation.mutate(policyDraft)}
        onSaveGates={(model, policy) => gateMutation.mutate({ model, policy })}
      />

      </div><div className="workspace-tab-body" hidden={workspaceTab !== 'Administration'}><details open className="advanced-section project-admin-section">
        <summary>Administration und Projektbereinigung</summary>
        <p className="subtle-text">
          Diagnoseansichten bleiben unter Advanced verfügbar. Bereinigungen werden vor der Ausführung vollständig
          aufgelistet und müssen bestätigt werden.
        </p>
        <div className="actions-row">
          <Link to="/operations/registry" className="button-link">Technische Registry</Link>
          <Link to="/operations/deployments" className="button-link">Deployment-Diagnose</Link>
          {canWrite ? <button className="button--danger" onClick={() => setCleanupOpen(true)}>Projekt bereinigen</button> : null}
        </div>
      </details>

      </div><CreateModelDialog
        open={createModelOpen}
        templates={pipeline.model_templates}
        selectedTemplateKey={modelTemplateKey}
        name={modelName}
        description={modelDescription}
        gatePolicy={modelGatePolicy}
        architectures={pipeline.supported_model_architectures}
        availableClasses={pipeline.available_classes ?? Object.keys(pipeline.readiness.class_distribution)}
        architectureKey={modelArchitectureKey}
        classNames={modelClassNames}
        useAllClasses={modelUseAllClasses}
        segmentationMode={modelSegmentationMode}
        trainingOverrides={modelTrainingOverrides}
        pending={createModelMutation.isPending}
        onClose={() => setCreateModelOpen(false)}
        onTemplate={setModelTemplateKey}
        onName={setModelName}
        onDescription={setModelDescription}
        onGatePolicy={setModelGatePolicy}
        onArchitecture={setModelArchitectureKey}
        onClassNames={setModelClassNames}
        onUseAllClasses={setModelUseAllClasses}
        onSegmentationMode={setModelSegmentationMode}
        onTrainingOverrides={setModelTrainingOverrides}
        onSubmit={() => createModelMutation.mutate()}
      />
      <TemplateDrawer
        open={templateDrawerOpen}
        templates={pipeline.model_templates}
        labelProjectId={labelProjectId}
        onClose={() => setTemplateDrawerOpen(false)}
      />
      <CleanupDialog
        open={cleanupOpen}
        title={deleteEntireProject ? 'Gesamtes Projekt löschen' : 'Alte Projektdaten bereinigen'}
        preview={cleanupPreviewQuery.data}
        pending={cleanupMutation.isPending}
        defaultDeleteArtifacts
        forceDeleteArtifacts={deleteEntireProject}
        extraPayload={{ delete_entire_pipeline: deleteEntireProject }}
        destructiveOptionLabel="Gesamtes Projekt inklusive Abhängigkeiten löschen"
        destructiveOptionChecked={deleteEntireProject}
        onDestructiveOptionChange={setDeleteEntireProject}
        onClose={() => setCleanupOpen(false)}
        onConfirm={(payload) => cleanupMutation.mutate(payload)}
      />
    </div>
  );
}

function ProjectHeader({ pipeline }: { pipeline: ProjectPipelineDetail }) {
  return (
    <header className="project-header">
      <Link to="/pipelines" className="project-header__back" aria-label="Zurück zu allen Projekten">
        ← Projekte & KI-Modelle
      </Link>
      <div>
        <p className="training-hero__eyebrow">{taskTypeLabel(pipeline.task_type)}</p>
        <h1>{pipeline.title}</h1>
        <LabelStudioProjectLink projectId={pipeline.label_studio_project_id}/>
      </div>
    </header>
  );
}

type DatasetSectionProps = {
  pipeline: ProjectPipelineDetail;
  labelProject?: LabelProjectSummary;
  datasets: DatasetSummary[];
  canWrite: boolean;
  files: File[];
  previews: Array<{ file: File; url: string }>;
  uploadMode: UploadMode;
  classificationLabel: string;
  uploadPhase: UploadPhase;
  uploadedFiles: number;
  uploadedBytes: number;
  totalBytes: number;
  mappingDraft: LabelProjectMappingRequest | null;
  labelConfigJson: string;
  syncPending: boolean;
  uploadPending: boolean;
  mappingPending: boolean;
  onSync: () => void;
  onFiles: (files: File[]) => void;
  onUploadMode: (mode: UploadMode) => void;
  onClassificationLabel: (label: string) => void;
  onUpload: () => void;
  onMappingDraft: (draft: LabelProjectMappingRequest | null) => void;
  onLabelConfigJson: (value: string) => void;
  onSaveMapping: () => void;
};

function DatasetSection(props: DatasetSectionProps) {
  const { pipeline } = props;
  const readiness = pipeline.readiness;
  const snapshot = pipeline.latest_snapshot;
  const validSnapshot = pipeline.latest_valid_snapshot;
  const uploadProgress = props.totalBytes ? (props.uploadedBytes / props.totalBytes) * 100 : 0;
  const flow = [
    {
      label: 'Label Studio',
      state: readiness.task_count > 0 ? 'success' : 'pending',
      detail: `${readiness.task_count} Aufgaben`
    },
    {
      label: 'Annotationen',
      state: readiness.annotations_ready ? 'success' : readiness.annotation_count > 0 ? 'running' : 'pending',
      detail: readiness.readiness_reason
    },
    {
      label: 'Snapshot',
      state: snapshot?.status === 'failed' ? 'failed' : snapshot?.status === 'ready' ? 'success' : snapshot ? 'running' : 'pending',
      detail: snapshot ? `#${snapshot.snapshot_no} · ${snapshot.status}` : 'Noch nicht erstellt'
    },
    {
      label: 'Validierung',
      state: snapshot?.validation_status === 'failed' ? 'failed' : snapshot?.validation_status === 'passed' ? 'success' : snapshot ? 'running' : 'pending',
      detail: snapshot?.validation_status ?? 'Ausstehend'
    },
    {
      label: 'Trainingsbereit',
      state: validSnapshot ? 'success' : 'pending',
      detail: validSnapshot ? `Snapshot #${validSnapshot.snapshot_no}` : 'Wartet auf gültigen Snapshot'
    }
  ] as const;

  return (
    <section id="dataset" className="project-section">
      <div className="project-section__heading">
        <div>
          <p className="training-hero__eyebrow">01</p>
          <h2>Dataset</h2>
          <p>Vom Label-Studio-Projekt bis zum reproduzierbaren, trainingsbereiten Snapshot.</p>
        </div>
        <div className="actions-row">
          <LabelStudioProjectLink projectId={pipeline.label_studio_project_id}/>
          {props.canWrite ? (
            <button onClick={props.onSync} disabled={props.syncPending}>
              {props.syncPending ? 'Synchronisiert…' : 'Jetzt synchronisieren'}
            </button>
          ) : null}
        </div>
      </div>

      <section className="project-readiness" aria-labelledby="project-readiness-title">
        <header><h3 id="project-readiness-title">Datenbereitschaft</h3></header>
        <dl className="project-readiness__facts">
          <div><dt>Bereitschaft</dt><dd>{readiness.readiness_percent.toFixed(0)}%</dd></div>
          <div><dt>Verwendbare Bilder</dt><dd>{readiness.eligible_task_count}</dd></div>
          <div><dt>Entwürfe</dt><dd>{readiness.draft_task_count}</dd></div>
          <div><dt>Ausgeschlossen</dt><dd>{readiness.invalid_task_count}</dd></div>
          <div><dt>Letzter gültiger Snapshot</dt><dd>{validSnapshot ? `#${validSnapshot.snapshot_no}` : 'Keiner'}</dd></div>
          <div><dt>Letzte Label-Studio-Aktivität</dt><dd>{formatDate(props.labelProject?.last_activity_at)}</dd></div>
        </dl>
        <ol className="dataset-flow" aria-label="Fortschritt zur Trainingsbereitschaft">
          {flow.map((step, index) => (
            <li key={step.label} className={`dataset-flow__step lifecycle--${step.state}`}>
              <span className="dataset-flow__index">{index + 1}</span>
              <div><strong>{step.label}</strong><small>{step.detail}</small></div>
            </li>
          ))}
        </ol>
        {snapshot?.status === 'failed' ? (
          <div className="inline-alert inline-alert--danger">
            <strong>Snapshot konnte nicht erstellt werden</strong>
            <p>{readiness.readiness_reason}</p>
            {props.canWrite ? <button onClick={props.onSync} disabled={props.syncPending}>{props.syncPending ? 'Synchronisiert…' : 'Erneut synchronisieren'}</button> : null}
          </div>
        ) : null}
        <div className="dataset-distribution">
          <h3>Klassenverteilung</h3>
          {Object.keys(readiness.class_distribution).length ? (
            <div className="class-distribution-list">
              {Object.entries(readiness.class_distribution).map(([label, count]) => (
                <div key={label}>
                  <span>{label}</span>
                  <progress aria-label={`Anzahl für ${label}`} value={count} max={Math.max(...Object.values(readiness.class_distribution), 1)} />
                  <strong>{count}</strong>
                </div>
              ))}
            </div>
          ) : <p className="subtle-text">Noch keine Klassenstatistik verfügbar.</p>}
        </div>
      </section>

      <Panel title="Bilder hinzufügen">
        {!pipeline.dataset_id ? (
          <EmptyState title="Dataset-Zuordnung fehlt" body="Ordne das Label-Studio-Projekt unter Projektdetails einem Dataset zu." />
        ) : (
          <div className="upload-workspace">
            <div className="upload-mode-switch" role="group" aria-label="Uploadmodus">
              <button className={props.uploadMode === 'raw' ? 'is-active' : ''} onClick={() => props.onUploadMode('raw')}>Unsortierte Bilder</button>
              {pipeline.task_type === 'classification' ? (
                <button className={props.uploadMode === 'classified' ? 'is-active' : ''} onClick={() => props.onUploadMode('classified')}>Bereits klassifiziert</button>
              ) : null}
            </div>
            {props.uploadMode === 'classified' ? (
              <label><span>Klasse für diesen Upload</span><input value={props.classificationLabel} onChange={(event) => props.onClassificationLabel(event.target.value)} placeholder="z. B. Gutteil" /></label>
            ) : null}
            <label className="dataset-dropzone">
              <strong>Dateien hier ablegen oder auswählen</strong>
              <span>Unterstützte Bilddateien werden als eine Upload-Session verarbeitet.</span>
              <input type="file" accept="image/*" multiple onChange={(event) => props.onFiles(Array.from(event.target.files ?? []))} />
            </label>
            <label className="folder-picker">
              <span>Alternativ ganzen Ordner auswählen</span>
              <input
                type="file"
                accept="image/*"
                multiple
                {...({ webkitdirectory: '', directory: '' } as Record<string, string>)}
                onChange={(event) => props.onFiles(Array.from(event.target.files ?? []))}
              />
            </label>
            {props.files.length ? (
              <>
                <div className="upload-preview-grid">
                  {props.previews.map(({ file, url }) => <figure key={`${file.name}-${file.lastModified}`}><img src={url} alt="" /><figcaption>{file.name}</figcaption></figure>)}
                </div>
                <div className="upload-progress-copy">
                  <strong>{props.uploadedFiles} / {props.files.length} Dateien</strong>
                  <span>{formatBytes(props.uploadedBytes)} / {formatBytes(props.totalBytes)}</span>
                </div>
                <div className="native-progress" role="progressbar" aria-label="Uploadfortschritt" aria-valuemin={0} aria-valuemax={100} aria-valuenow={uploadProgress}>
                  <span style={{ width: `${uploadProgress}%` }} />
                </div>
              </>
            ) : null}
            {props.canWrite ? (
              <div className="actions-row">
                <button className="button--primary" onClick={props.onUpload} disabled={!props.files.length || props.uploadPending}>
                  {props.uploadPhase === 'creating' ? 'Session wird erstellt…' : props.uploadPhase === 'uploading' ? 'Dateien werden hochgeladen…' : props.uploadPhase === 'finalizing' ? 'Dataset wird verarbeitet…' : `${props.files.length || ''} Dateien hochladen`}
                </button>
              </div>
            ) : null}
          </div>
        )}
      </Panel>

      <details className="advanced-section dataset-rules">
        <summary>Projektdetails und Dataset-Regeln</summary>
        {props.mappingDraft ? (
          <form className="training-form-grid" onSubmit={(event) => { event.preventDefault(); props.onSaveMapping(); }}>
            <label><span>Dataset</span><select value={props.mappingDraft.dataset_id} disabled={!props.canWrite} onChange={(event) => props.onMappingDraft({ ...props.mappingDraft!, dataset_id: event.target.value })}>
              <option value="">Dataset auswählen</option>
              {props.datasets.map((dataset) => <option key={dataset.dataset_id} value={dataset.dataset_id}>{dataset.name}</option>)}
            </select></label>
            <label><span>Label-Studio-ID</span><input value={props.mappingDraft.label_studio_project_id} disabled /></label>
            <label><span>Train / Val / Test</span><input value={`${props.mappingDraft.train_ratio} / ${props.mappingDraft.val_ratio} / ${props.mappingDraft.test_ratio}`} disabled /></label>
            <label><span>Mindestannotationen</span><input type="number" value={props.mappingDraft.minimum_annotation_count} disabled={!props.canWrite} onChange={(event) => props.onMappingDraft({ ...props.mappingDraft!, minimum_annotation_count: Number(event.target.value) })} /></label>
            <label><span>Pflichtklassen</span><input value={props.mappingDraft.required_classes.join(', ')} disabled={!props.canWrite} onChange={(event) => props.onMappingDraft({ ...props.mappingDraft!, required_classes: commaList(event.target.value) })} /></label>
            <label><span>Qualifying Actions</span><input value={props.mappingDraft.qualifying_actions.join(', ')} disabled={!props.canWrite} onChange={(event) => props.onMappingDraft({ ...props.mappingDraft!, qualifying_actions: commaList(event.target.value) })} /></label>
            <label><span>Class Balance</span><select value={props.mappingDraft.class_balance_strategy} disabled={!props.canWrite} onChange={(event) => props.onMappingDraft({ ...props.mappingDraft!, class_balance_strategy: event.target.value as LabelProjectMappingRequest['class_balance_strategy'] })}>
              <option value="none">Keine</option><option value="weighted_loss">Weighted Loss</option><option value="balanced_sampler">Balanced Sampler</option>
            </select></label>
            <label className="training-form-grid__full"><span>Label-Config (JSON)</span><textarea value={props.labelConfigJson} disabled={!props.canWrite} onChange={(event) => props.onLabelConfigJson(event.target.value)} /></label>
            {props.canWrite ? <div className="actions-row training-form-grid__full"><button type="submit" disabled={props.mappingPending}>{props.mappingPending ? 'Speichert…' : 'Dataset-Regeln speichern'}</button></div> : null}
          </form>
        ) : <p className="subtle-text">Projektinformationen werden geladen.</p>}
      </details>
    </section>
  );
}

type ModelsSectionProps = {
  pipeline: ProjectPipelineDetail;
  selectedModel?: ProjectModelSummary;
  workspace?: ProjectModelWorkspaceDetail;
  workspaceLoading: boolean;
  workspaceError: { title: string; description?: string } | null;
  trainingHistory: TrainingJobSummary[];
  modelView: ModelView;
  activeRun?: TrainingJobDetail;
  selectedRunId: string | null;
  canWrite: boolean;
  deployPending: boolean;
  deploymentReleases: DeploymentReleaseSummary[];
  deploymentReleasesLoading: boolean;
  deploymentReleasesError: { title: string; description?: string } | null;
  deploymentServingPending: boolean;
  trainPending: boolean;
  cancelPending: boolean;
  linkPending: boolean;
  inferenceFile: File | null;
  inferencePending: boolean;
  inferenceResult: ProjectInferenceResult | undefined;
  inferenceError: { title: string; description?: string } | null;
  onSelectModel: (modelFamilyId: string) => void;
  onRetryWorkspace: () => void;
  onView: (view: ModelView) => void;
  onSelectRun: (trainingJobId: string) => void;
  onCreateModel: () => void;
  onTemplates: () => void;
  onTrain: () => void;
  onCancel: () => void;
  onDeploy: () => void;
  onForceDeploy: () => void;
  onRetryDeploymentReleases: () => void;
  onDeactivateModel: (release: DeploymentReleaseSummary) => void;
  onActivateModel: (release: DeploymentReleaseSummary) => void;
  onLinkPredictions: () => void;
  onInferenceFile: (file: File | null) => void;
  onInference: () => void;
};

function ModelsSection(props: ModelsSectionProps) {
  const activeRunStatus = props.activeRun?.status;
  const canCancel = Boolean(activeRunStatus && !terminalTrainingStates.includes(activeRunStatus));
  const latestRun = props.trainingHistory[0];
  const deploymentBlocked = Boolean(latestRun?.status === 'completed' && !latestRun.promotion_ready);
  const activeModelId = props.pipeline.prediction_backend.model_family_id;
  const activeModel = props.pipeline.models.find((model) => model.model_family_id === activeModelId);

  return (
    <section id="models" className="project-section">
      <div className="project-section__heading">
        <div>
          <p className="training-hero__eyebrow">02</p>
          <h2>Modelle</h2>
          <p>Modelle erstellen, trainieren, vergleichen und sicher bereitstellen.</p>
        </div>
        <div className="actions-row">
          <button onClick={props.onTemplates}>Templates</button>
          {props.canWrite ? <button className="button--primary" onClick={props.onCreateModel}>Neues Modell</button> : null}
        </div>
      </div>

      <div className="model-workspace">
        <aside className="model-master" aria-label="Projektmodelle">
          <div className="model-master__heading"><strong>Projektmodelle</strong><span>{props.pipeline.models.length}</span></div>
          <div className={`model-active-summary${activeModel ? ' is-active' : ''}`}>
            <span aria-hidden="true" />
            <div>
              <strong>{activeModel ? `Label-Studio-Standard: ${activeModel.display_name}` : 'Kein Label-Studio-Standardmodell'}</strong>
              <small>{activeModel ? 'Nur für Vorhersagen im Labeling; Runtime-Modelle bleiben unabhängig.' : 'Deployte Modelle sind trotzdem für Runtime-Pipelines verfügbar.'}</small>
            </div>
          </div>
          {props.pipeline.models.length ? props.pipeline.models.map((model) => {
            const endpoint = props.pipeline.current_endpoints.find((item) => item.model_family_id === model.model_family_id);
            const run = props.pipeline.training_history.find((item) => item.model_family_slug === model.slug);
            const selected = model.model_family_id === props.selectedModel?.model_family_id;
            const active = model.model_family_id === activeModelId;
            const deployed = Boolean(endpoint?.realtime_available || endpoint?.batch_available);
            return (
              <button key={model.model_family_id} className={`model-master-card${selected ? ' is-selected' : ''}`} aria-pressed={selected} onClick={() => props.onSelectModel(model.model_family_id)}>
                <span className="model-master-card__top"><strong>{model.display_name}</strong><span className={`badge badge--${deployed ? 'success' : 'neutral'}`}>{deployed ? 'Deployed' : 'Nicht deployed'}</span></span>
                <small>{architectureLabel(model.architecture)}{active ? ' · Label-Studio-Standard' : ''}</small>
                <div className="model-scope-summary">
                  {model.segmentation_mode ? <span>{segmentationModeLabel(model.segmentation_mode)}</span> : null}
                  <span>{(model.uses_all_classes ?? !model.class_names?.length) ? 'Alle Klassen' : `${model.class_names?.length ?? 0} Klassen`}</span>
                </div>
                <dl>
                  <div><dt>Template</dt><dd>{model.template_key ?? 'Individuell'}</dd></div>
                  <div><dt>Version</dt><dd>{model.latest_model_version_no ? `v${model.latest_model_version_no}` : '—'}</dd></div>
                  <div><dt>Training</dt><dd className={`badge badge--${trainingTone(run?.status)}`}>{run?.status ?? 'Noch keines'}</dd></div>
                  <div><dt>Gates</dt><dd>{run?.status === 'completed' ? run.promotion_ready ? 'Bestanden' : 'Blockiert' : 'Offen'}</dd></div>
                </dl>
              </button>
            );
          }) : (
            <EmptyState title="Noch kein Modell" body="Lege das erste Projektmodell aus einem freigegebenen Template an.">
              {props.canWrite ? <button onClick={props.onCreateModel}>Erstes Modell anlegen</button> : null}
            </EmptyState>
          )}
        </aside>

        <main className="model-detail">
          {!props.selectedModel ? (
            <EmptyState title="Modell auswählen" body="Wähle links ein Modell oder lege ein neues an." />
          ) : (
            <>
              <header className="model-detail__header">
                <div>
                  <div className="model-detail__identity">
                    <p className="training-hero__eyebrow">{props.selectedModel.template_key ?? 'Projektmodell'}</p>
                    <span className={`badge badge--${props.selectedModel.model_family_id === activeModelId ? 'success' : 'neutral'}`}>
                      {props.selectedModel.model_family_id === activeModelId ? 'Label-Studio-Standard' : 'Runtime-unabhängig'}
                    </span>
                  </div>
                  <h3>{props.selectedModel.display_name}</h3>
                  <p>{props.selectedModel.description ?? architectureLabel(props.selectedModel.architecture)}</p>
                  <div className="model-scope-summary model-scope-summary--detail">
                    {props.selectedModel.segmentation_mode ? <span>{segmentationModeLabel(props.selectedModel.segmentation_mode)}</span> : null}
                    <span>{projectModelClassLabel(props.selectedModel, props.pipeline.available_classes ?? [])}</span>
                  </div>
                </div>
                {props.canWrite ? (
                  <div className="actions-row">
                    <button className="button--primary" disabled={!props.pipeline.latest_valid_snapshot || props.trainPending} onClick={props.onTrain}>
                      {props.trainPending ? 'Wird gestartet…' : latestRun ? 'Neu trainieren' : 'Training starten'}
                    </button>
                    {canCancel ? <button className="button--danger" disabled={props.cancelPending} onClick={props.onCancel}>Training abbrechen</button> : null}
                  </div>
                ) : null}
              </header>

              <div className="model-tabs" role="tablist" aria-label="Modellarbeitsbereich">
                <button role="tab" aria-selected={props.modelView === 'status'} className={props.modelView === 'status' ? 'is-active' : ''} onClick={() => props.onView('status')}>Status & Fortschritt</button>
                <button role="tab" aria-selected={props.modelView === 'metrics'} className={props.modelView === 'metrics' ? 'is-active' : ''} onClick={() => props.onView('metrics')}>Metriken & Versionen</button>
                <button role="tab" aria-selected={props.modelView === 'parameters'} className={props.modelView === 'parameters' ? 'is-active' : ''} onClick={() => props.onView('parameters')}>Parameter & Konfiguration</button>
                <button role="tab" aria-selected={props.modelView === 'deployment'} className={props.modelView === 'deployment' ? 'is-active' : ''} onClick={() => props.onView('deployment')}>Deployment & API</button>
              </div>

              {props.modelView === 'status' ? (
                <LifecycleView
                  workspace={props.workspace}
                  loading={props.workspaceLoading}
                  error={props.workspaceError}
                  run={props.activeRun ?? latestRun}
                  onRetry={props.onRetryWorkspace}
                />
              ) : props.modelView === 'metrics' ? (
                <MetricsView
                  workspace={props.workspace}
                  trainingHistory={props.trainingHistory}
                  activeRun={props.activeRun}
                  selectedRunId={props.selectedRunId}
                  loading={props.workspaceLoading}
                  error={props.workspaceError}
                  onRetry={props.onRetryWorkspace}
                  onSelectRun={props.onSelectRun}
                />
              ) : props.modelView === 'parameters' ? (
                <ParametersView
                  model={props.selectedModel}
                  trainingHistory={props.trainingHistory}
                  activeRun={props.activeRun}
                  selectedRunId={props.selectedRunId}
                  onSelectRun={props.onSelectRun}
                />
              ) : (
                <DeploymentView
                  model={props.selectedModel}
                  workspace={props.workspace}
                  active={props.selectedModel.model_family_id === activeModelId}
                  blocked={deploymentBlocked}
                  canWrite={props.canWrite}
                  deployPending={props.deployPending}
                  deploymentReleases={props.deploymentReleases}
                  deploymentReleasesLoading={props.deploymentReleasesLoading}
                  deploymentReleasesError={props.deploymentReleasesError}
                  deploymentServingPending={props.deploymentServingPending}
                  linkPending={props.linkPending}
                  inferenceFile={props.inferenceFile}
                  inferencePending={props.inferencePending}
                  inferenceResult={props.inferenceResult}
                  inferenceError={props.inferenceError}
                  onDeploy={props.onDeploy}
                  onForceDeploy={props.onForceDeploy}
                  onRetryDeploymentReleases={props.onRetryDeploymentReleases}
                  onDeactivateModel={props.onDeactivateModel}
                  onActivateModel={props.onActivateModel}
                  onLinkPredictions={props.onLinkPredictions}
                  onInferenceFile={props.onInferenceFile}
                  onInference={props.onInference}
                />
              )}
            </>
          )}
        </main>
      </div>
    </section>
  );
}

function LifecycleView(props: {
  workspace?: ProjectModelWorkspaceDetail;
  loading: boolean;
  error: { title: string; description?: string } | null;
  run?: TrainingJobSummary | TrainingJobDetail;
  onRetry: () => void;
}) {
  return (
    <div className="lifecycle-view" role="list" aria-label="Modell-Lifecycle">
      {props.run ? <TrainingProgressSummary run={props.run} /> : null}
      {props.error ? (
        <div className="inline-alert inline-alert--warn lifecycle-load-alert">
          <div>
            <strong>{props.error.title}</strong>
            <p>{props.error.description ?? 'Die weiteren Lifecycle-Schritte sind vorübergehend nicht verfügbar.'}</p>
          </div>
          <button type="button" onClick={props.onRetry}>Erneut laden</button>
        </div>
      ) : null}
      {!props.workspace && props.loading && !props.run ? (
        <p className="lifecycle-loading-state">Modellstatus wird geladen…</p>
      ) : null}
      {!props.workspace && !props.loading && !props.error && !props.run ? (
        <EmptyState title="Noch kein Lifecycle vorhanden" body="Starte das erste Training für dieses Modell." />
      ) : null}
      {props.workspace?.lifecycle.map((stage, index) => (
        <article key={stage.key} className={`lifecycle-stage lifecycle--${stage.state}`} role="listitem">
          <div className="lifecycle-stage__marker"><span>{index + 1}</span></div>
          <div className="lifecycle-stage__body">
            <header>
              <div><h4>{stage.label}</h4><p>{stage.detail}</p></div>
              <span className={`badge badge--${stageTone(stage.state)}`}>{stageStateLabel(stage.state)}</span>
            </header>
            {stage.key === 'training' && !props.run && stage.progress_percent !== null && stage.progress_percent !== undefined ? (
              <div className="lifecycle-progress">
                <div role="progressbar" aria-label="Trainingsfortschritt" aria-valuemin={0} aria-valuemax={100} aria-valuenow={stage.progress_percent}><span style={{ width: `${stage.progress_percent}%` }} /></div>
                <strong>{stage.progress_percent.toFixed(0)}%</strong>
              </div>
            ) : stage.state === 'running' ? <span className="running-indicator">Läuft…</span> : null}
            {stage.error ? <div className="inline-alert inline-alert--danger"><strong>Fehler</strong><p>{stage.error}</p></div> : null}
            <div className="lifecycle-stage__meta">
              {stage.started_at ? <span>Start: {formatDate(stage.started_at)}</span> : null}
              {stage.finished_at ? <span>Ende: {formatDate(stage.finished_at)}</span> : null}
            </div>
            {stage.activities.length ? <ActivityDetails activities={stage.activities} label={`Aktivitäten für ${stage.label}`} /> : null}
          </div>
        </article>
      ))}
      {props.workspace?.activity_timeline.length ? <ActivityDetails activities={props.workspace.activity_timeline} label="Gesamte chronologische Historie" /> : null}
    </div>
  );
}

function TrainingProgressSummary({ run }: { run: TrainingJobSummary | TrainingJobDetail }) {
  const metrics = 'metrics' in run ? run.metrics : [];
  const latestMetricEpoch = metrics.reduce(
    (latest, metric) => Math.max(latest, metric.epoch_no),
    0
  );
  const progress = resolveTrainingProgress({
    currentEpoch: run.current_epoch,
    totalEpochs: run.total_epochs,
    progressPercent: run.progress_percent,
    latestMetricEpoch
  });
  const latestMetrics = latestMetricEpoch
    ? metrics.filter((metric) => metric.epoch_no === latestMetricEpoch).slice(-4)
    : [];
  const active = !terminalTrainingStates.includes(run.status);

  return (
    <section className="training-live-progress" aria-live="polite">
      <header>
        <div>
          <span className="training-live-progress__eyebrow">
            {active ? 'Live-Training' : 'Letzter Trainingslauf'}
          </span>
          <h4>{architectureLabel(run.architecture)}</h4>
        </div>
        <span className={`badge badge--${trainingTone(run.status)}`}>
          {trainingStatusLabel(run.status)}
        </span>
      </header>
      <div className="training-live-progress__numbers">
        <strong>
          {progress.totalEpochs
            ? `Epoche ${progress.currentEpoch} von ${progress.totalEpochs}`
            : `Epoche ${progress.currentEpoch}`}
        </strong>
        <span>{progress.progressPercent.toFixed(0)}%</span>
      </div>
      <div
        className="training-live-progress__bar"
        role="progressbar"
        aria-label="Trainingsfortschritt nach Epochen"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={progress.progressPercent}
        aria-valuetext={
          progress.totalEpochs
            ? `Epoche ${progress.currentEpoch} von ${progress.totalEpochs}`
            : `${progress.progressPercent.toFixed(0)} Prozent`
        }
      >
        <span style={{ width: `${progress.progressPercent}%` }} />
      </div>
      <dl className="training-live-progress__meta">
        <div><dt>Schritt</dt><dd>{run.current_step || '—'}</dd></div>
        <div><dt>Gestartet</dt><dd>{formatDate(run.started_at)}</dd></div>
        <div><dt>Letztes Update</dt><dd>{latestMetrics.length ? `Epoche ${latestMetricEpoch}` : 'Warte auf Metriken'}</dd></div>
      </dl>
      {latestMetrics.length ? (
        <div className="training-live-progress__metrics" aria-label={`Metriken aus Epoche ${latestMetricEpoch}`}>
          {latestMetrics.map((metric) => (
            <span key={metric.metric_id}>
              {metricLabel(metric.metric_name)} <strong>{formatMetric(metric.metric_value)}</strong>
            </span>
          ))}
        </div>
      ) : active ? (
        <small className="subtle-text">Metriken erscheinen nach der ersten abgeschlossenen Epoche.</small>
      ) : null}
    </section>
  );
}

function ActivityDetails({ activities, label }: { activities: PipelineActivityItem[]; label: string }) {
  return (
    <details className="activity-details">
      <summary>{label} ({activities.length})</summary>
      <ol>
        {activities.map((activity, index) => (
          <li key={`${activity.occurred_at}-${activity.event_type}-${index}`}>
            <time>{formatDate(activity.occurred_at)}</time>
            <div><strong>{activity.title}</strong><p>{activity.detail}</p></div>
          </li>
        ))}
      </ol>
    </details>
  );
}

function MetricsView(props: {
  workspace?: ProjectModelWorkspaceDetail;
  trainingHistory: TrainingJobSummary[];
  activeRun?: TrainingJobDetail;
  selectedRunId: string | null;
  loading: boolean;
  error: { title: string; description?: string } | null;
  onRetry: () => void;
  onSelectRun: (trainingJobId: string) => void;
}) {
  const run = props.activeRun;
  const selectedSummary = props.trainingHistory.find(
    (item) => item.training_job_id === props.selectedRunId
  ) ?? props.trainingHistory[0];
  if (!props.trainingHistory.length) {
    return <EmptyState title="Noch keine Trainingsläufe" body="Starte ein Training, um Live-Metriken, Checkpoints und Ergebnisse zu sehen." />;
  }
  const evaluation = run?.evaluation_summary;
  const summaryMetrics = Object.entries(evaluation?.metrics ?? run?.metrics_summary ?? {});
  return (
    <div className="metrics-workspace">
      {props.error ? (
        <div className="inline-alert inline-alert--warn lifecycle-load-alert">
          <div><strong>{props.error.title}</strong><p>{props.error.description}</p></div>
          <button type="button" onClick={props.onRetry}>Erneut laden</button>
        </div>
      ) : null}
      <div className="run-selector">
        <label><span>Trainingslauf</span><select value={props.selectedRunId ?? ''} onChange={(event) => props.onSelectRun(event.target.value)}>
          {props.trainingHistory.map((item) => <option key={item.training_job_id} value={item.training_job_id}>{formatDate(item.created_at)} · {trainingStatusLabel(item.status)} · {resolveTrainingProgress({ currentEpoch: item.current_epoch, totalEpochs: item.total_epochs, progressPercent: item.progress_percent }).progressPercent.toFixed(0)}%</option>)}
        </select></label>
        {selectedSummary ? <span className={`badge badge--${trainingTone(selectedSummary.status)}`}>{trainingStatusLabel(selectedSummary.status)}</span> : null}
      </div>
      {run ? (
        <>
          <div className="dataset-overview-grid">
            <MetricPill label="Primäre Metrik" value={evaluation?.primary_metric_name ?? '—'} />
            <MetricPill label="Ergebnis" value={formatMetric(evaluation?.primary_metric_value)} tone={evaluation?.promotion_ready ? 'success' : 'neutral'} />
            <MetricPill label="Bester Checkpoint" value={evaluation?.best_checkpoint_id ?? run.best_checkpoint_id ?? '—'} />
            <MetricPill label="Deployment" value={evaluation?.promotion_ready ? 'Möglich' : run.status === 'completed' ? 'Blockiert' : 'Noch offen'} tone={evaluation?.promotion_ready ? 'success' : run.status === 'completed' ? 'danger' : 'warn'} />
          </div>
          <TrainingMetricsChart metrics={run.metrics} />
          {summaryMetrics.length ? (
            <div className="metrics-summary-grid">{summaryMetrics.map(([name, value]) => <MetricPill key={name} label={metricLabel(name)} value={formatMetric(value)} />)}</div>
          ) : null}
          <section className="quality-gate-results">
            <h4>Quality Gates</h4>
            {evaluation?.promotion_results?.length ? (
              <table className="data-table"><thead><tr><th>Metrik</th><th>Ist</th><th>Bedingung</th><th>Ergebnis</th></tr></thead><tbody>
                {evaluation.promotion_results.map((gate) => <tr key={`${gate.metric_name}-${gate.phase}`}><td>{metricLabel(gate.metric_name)}</td><td>{formatMetric(gate.actual_value)}</td><td>{gate.comparison === 'gte' ? '≥' : '≤'} {formatMetric(gate.threshold)}</td><td><span className={`badge badge--${gate.passed ? 'success' : 'danger'}`}>{gate.passed ? 'Bestanden' : 'Blockiert'}</span></td></tr>)}
              </tbody></table>
            ) : <p className="subtle-text">Gate-Ergebnisse erscheinen nach der Evaluation.</p>}
          </section>
          <details className="advanced-section"><summary>Checkpoints ({run.checkpoints.length})</summary>
            {run.checkpoints.length ? <table className="data-table"><thead><tr><th>Epoche</th><th>Art</th><th>Metrik</th><th>Best</th></tr></thead><tbody>{run.checkpoints.map((checkpoint) => <tr key={checkpoint.checkpoint_id}><td>{checkpoint.epoch_no}</td><td>{checkpoint.checkpoint_kind}</td><td>{formatMetric(checkpoint.metric_primary)}</td><td>{checkpoint.is_best ? 'Ja' : 'Nein'}</td></tr>)}</tbody></table> : <p>Keine Checkpoints.</p>}
          </details>
        </>
      ) : selectedSummary ? (
        <>
          <TrainingProgressSummary run={selectedSummary} />
          <p className="lifecycle-loading-state">
            {props.loading ? 'Trainingsdetails und Metriken werden geladen…' : 'Trainingsdetails werden nachgeladen…'}
          </p>
        </>
      ) : null}
      <section className="version-history"><h4>Versionen vergleichen</h4>
        {props.workspace?.versions.length ? <table className="data-table"><thead><tr><th>Version</th><th>Status</th><th>Primäre Metrik</th><th>Wert</th><th>Erstellt</th></tr></thead><tbody>{props.workspace.versions.map((version) => <tr key={version.model_version_id}><td>v{version.version_no}</td><td>{version.status}</td><td>{version.primary_metric_name ?? '—'}</td><td>{formatMetric(version.primary_metric_value)}</td><td>{formatDate(version.created_at)}</td></tr>)}</tbody></table> : <p className="subtle-text">Noch keine registrierte Modellversion.</p>}
      </section>
    </div>
  );
}

function ParametersView(props: {
  model: ProjectModelSummary;
  trainingHistory: TrainingJobSummary[];
  activeRun?: TrainingJobDetail;
  selectedRunId: string | null;
  onSelectRun: (trainingJobId: string) => void;
}) {
  const selectedSummary = props.trainingHistory.find(
    (item) => item.training_job_id === props.selectedRunId
  ) ?? props.trainingHistory[0];
  const run = props.activeRun;
  const defaults = run?.resolved_training_defaults ?? props.model.training_overrides;
  const effectiveEpochs = run?.total_epochs || defaults?.epochs;
  const qualityGates = run?.promotion_criteria ?? props.model.promotion_policy.criteria;
  const sourceLabel = run
    ? `Tatsächlich verwendete Werte des Laufs vom ${formatDate(run.created_at)}`
    : defaults
      ? 'Gespeicherte Modell-Overrides für zukünftige Trainingsläufe'
      : 'Modellkonfiguration';
  const completeConfiguration = run
    ? {
        architecture: run.architecture,
        template: {
          key: run.model_template_key,
          version: run.model_template_version
        },
        recipe: {
          name: run.recipe_name,
          version: run.recipe_version
        },
        resolved_training_defaults: run.resolved_training_defaults,
        parameter_overrides: run.parameter_overrides,
        optimizer_settings: run.resolved_training_defaults.optimizer_settings,
        best_checkpoint_rule: run.best_checkpoint_rule,
        promotion_criteria: run.promotion_criteria,
        promotion_policy: run.promotion_policy,
        runtime: {
          trainer_mode: run.trainer_mode,
          trainer_backend: run.trainer_backend,
          gpu_device: run.gpu_device,
          queue_name: run.queue_name
        }
      }
    : {
        architecture: props.model.architecture,
        training_overrides: props.model.training_overrides,
        promotion_policy: props.model.promotion_policy
      };

  return (
    <div className="parameter-workspace">
      {props.trainingHistory.length ? (
        <div className="run-selector">
          <label><span>Trainingslauf</span><select value={props.selectedRunId ?? ''} onChange={(event) => props.onSelectRun(event.target.value)}>
            {props.trainingHistory.map((item) => <option key={item.training_job_id} value={item.training_job_id}>{formatDate(item.created_at)} · {trainingStatusLabel(item.status)} · {item.architecture.display_name}</option>)}
          </select></label>
          {selectedSummary ? <span className={`badge badge--${trainingTone(selectedSummary.status)}`}>{trainingStatusLabel(selectedSummary.status)}</span> : null}
        </div>
      ) : null}

      <div className="parameter-workspace__intro">
        <div><p className="training-hero__eyebrow">Konfigurationsquelle</p><strong>{sourceLabel}</strong></div>
        {selectedSummary && !run ? <span className="subtle-text">Laufdetails werden geladen…</span> : null}
      </div>

      <section className="parameter-group" aria-labelledby="model-parameter-basics">
        <h4 id="model-parameter-basics">Modell und Laufzeit</h4>
        <dl className="definition-list definition-list--inline parameter-definition-list">
          <div><dt>Architektur</dt><dd>{architectureLabel(run?.architecture ?? props.model.architecture)}</dd></div>
          <div><dt>Task</dt><dd>{taskTypeLabel(run?.task_type ?? props.model.task_type)}</dd></div>
          <div><dt>Segmentierung</dt><dd>{props.model.segmentation_mode ? segmentationModeLabel(props.model.segmentation_mode) : 'Nicht zutreffend'}</dd></div>
          <div><dt>Klassenumfang</dt><dd>{projectModelClassLabel(props.model, [])}</dd></div>
          <div><dt>Template</dt><dd>{run?.model_template_key ?? props.model.template_key ?? 'Individuell'}{run?.model_template_version || props.model.template_version ? ` · v${run?.model_template_version ?? props.model.template_version}` : ''}</dd></div>
          <div><dt>Trainings-Rezept</dt><dd>{run ? `${run.recipe_name} · ${run.recipe_version}` : props.model.training_profile_key ?? 'Template-Standard'}</dd></div>
          <div><dt>Trainer</dt><dd>{run?.trainer_backend ?? '—'}{run?.trainer_mode ? ` · ${run.trainer_mode}` : ''}</dd></div>
          <div><dt>GPU</dt><dd>{run?.gpu_device ?? '—'}</dd></div>
          <div><dt>Dataset-Snapshot</dt><dd>{run?.dataset_snapshot_id ?? selectedSummary?.dataset_snapshot_id ?? 'Noch kein Lauf'}</dd></div>
          <div><dt>Trainingslauf</dt><dd>{run?.training_job_id ?? selectedSummary?.training_job_id ?? 'Noch kein Lauf'}</dd></div>
        </dl>
      </section>

      <section className="parameter-group" aria-labelledby="model-training-parameters">
        <h4 id="model-training-parameters">Effektive Trainingsparameter</h4>
        <dl className="definition-list definition-list--inline parameter-definition-list">
          <div><dt>Auflösung</dt><dd>{defaults?.image_size ? `${defaults.image_size} × ${defaults.image_size} px` : 'Automatisch'}</dd></div>
          <div><dt>Epochen</dt><dd>{effectiveEpochs ?? '—'}</dd></div>
          <div><dt>Batch-Größe</dt><dd>{defaults?.batch_size ?? 'Automatisch'}</dd></div>
          <div><dt>Lernrate</dt><dd>{formatParameterNumber(defaults?.learning_rate)}</dd></div>
          <div><dt>Optimizer</dt><dd>{defaults?.optimizer ?? 'Template-Standard'}</dd></div>
          <div><dt>Checkpoint-Intervall</dt><dd>{defaults?.checkpoint_interval_steps ? `${defaults.checkpoint_interval_steps} Schritte` : '—'}</dd></div>
        </dl>
      </section>

      <section className="parameter-group" aria-labelledby="model-quality-parameters">
        <h4 id="model-quality-parameters">Checkpoint und Quality Gates</h4>
        {run ? (
          <dl className="definition-list definition-list--inline parameter-definition-list">
            <div><dt>Best-Checkpoint-Metrik</dt><dd>{metricLabel(run.best_checkpoint_rule.metric_name)}</dd></div>
            <div><dt>Optimierung</dt><dd>{run.best_checkpoint_rule.mode === 'max' ? 'Höchster Wert' : 'Niedrigster Wert'}</dd></div>
            <div><dt>Phase</dt><dd>{run.best_checkpoint_rule.phase}</dd></div>
            <div><dt>Früheste Epoche</dt><dd>{run.best_checkpoint_rule.minimum_epoch}</dd></div>
          </dl>
        ) : null}
        {qualityGates.length ? (
          <table className="data-table parameter-gates-table"><thead><tr><th>Metrik</th><th>Phase</th><th>Bedingung</th></tr></thead><tbody>
            {qualityGates.map((gate, index) => <tr key={`${gate.metric_name}-${gate.phase}-${index}`}><td>{metricLabel(gate.metric_name)}</td><td>{gate.phase}</td><td>{gate.comparison === 'gte' ? '≥' : '≤'} {formatParameterNumber(gate.threshold)}</td></tr>)}
          </tbody></table>
        ) : <p className="subtle-text">Keine Quality Gates konfiguriert.</p>}
      </section>

      <details className="advanced-section parameter-raw-config">
        <summary>Vollständige aufgelöste Konfiguration</summary>
        <p className="subtle-text">Diese Ansicht enthält auch Optimizer-, Runtime- und individuelle Override-Werte.</p>
        <pre className="code-block">{JSON.stringify(completeConfiguration, null, 2)}</pre>
      </details>
    </div>
  );
}

function DeploymentView(props: {
  model: ProjectModelSummary;
  workspace?: ProjectModelWorkspaceDetail;
  active: boolean;
  blocked: boolean;
  canWrite: boolean;
  deployPending: boolean;
  deploymentReleases: DeploymentReleaseSummary[];
  deploymentReleasesLoading: boolean;
  deploymentReleasesError: { title: string; description?: string } | null;
  deploymentServingPending: boolean;
  linkPending: boolean;
  inferenceFile: File | null;
  inferencePending: boolean;
  inferenceResult: ProjectInferenceResult | undefined;
  inferenceError: { title: string; description?: string } | null;
  onDeploy: () => void;
  onForceDeploy: () => void;
  onRetryDeploymentReleases: () => void;
  onDeactivateModel: (release: DeploymentReleaseSummary) => void;
  onActivateModel: (release: DeploymentReleaseSummary) => void;
  onLinkPredictions: () => void;
  onInferenceFile: (file: File | null) => void;
  onInference: () => void;
}) {
  const endpoint = props.workspace?.endpoints[0];
  const realtime = Boolean(endpoint?.realtime_available);
  const batch = Boolean(endpoint?.batch_available);
  return (
    <div className="deployment-workspace">
      <div className="dataset-overview-grid">
        <MetricPill label="Label Studio" value={props.active ? 'Standardmodell' : 'Nicht verknüpft'} tone={props.active ? 'success' : 'neutral'} />
        <MetricPill label="Deployment" value={realtime || batch ? 'Bereitgestellt' : 'Nicht deployed'} tone={realtime || batch ? 'success' : 'neutral'} />
        <MetricPill label="Realtime" value={realtime ? 'Verfügbar' : 'Offline'} tone={realtime ? 'success' : 'neutral'} />
        <MetricPill label="Batch" value={batch ? 'Verfügbar' : 'Offline'} tone={batch ? 'success' : 'neutral'} />
        <MetricPill label="Version" value={endpoint?.current_model_version_no ? `v${endpoint.current_model_version_no}` : '—'} />
      </div>
      <p className="subtle-text">Jedes bereitgestellte Modell bleibt über seine eigene Modellfamilie für Kanäle, Runtime-Pipelines und API-Aufrufe verfügbar. Die Label-Studio-Verknüpfung bestimmt lediglich das Standardmodell für Vorschläge beim Labeling.</p>
      {props.blocked ? <div className="inline-alert inline-alert--warn"><strong>Deployment durch Quality Gates blockiert</strong><p>Das bestehende gesunde Deployment bleibt unverändert aktiv.</p></div> : null}
      {props.canWrite ? <div className="actions-row">
        <button className="button--primary" disabled={!props.model.latest_model_version_id || props.deployPending || props.blocked} onClick={props.onDeploy}>{props.deployPending ? 'Wird bereitgestellt…' : 'Ausgewähltes Modell deployen'}</button>
        {realtime ? <button disabled={props.linkPending} onClick={props.onLinkPredictions}>{props.linkPending ? 'Verbindet…' : 'Mit Label Studio verbinden'}</button> : null}
      </div> : null}
      <section>
        <h4>Triton-Modellbetrieb</h4>
        <p className="subtle-text">
          Nicht verwendete Modelle können hier aus Triton entladen und später ohne erneutes Training wieder aktiviert werden.
        </p>
        {props.deploymentReleasesLoading ? (
          <p className="subtle-text">Deployment-Status wird geladen…</p>
        ) : props.deploymentReleasesError ? (
          <div className="inline-alert inline-alert--warn">
            <div>
              <strong>{props.deploymentReleasesError.title}</strong>
              <p>{props.deploymentReleasesError.description ?? 'Die Modellaktionen sind vorübergehend nicht verfügbar.'}</p>
            </div>
            <button type="button" onClick={props.onRetryDeploymentReleases}>Erneut laden</button>
          </div>
        ) : props.deploymentReleases.length ? (
          <table className="data-table compact-table">
            <thead>
              <tr>
                <th>Modus</th>
                <th>Triton-Modell</th>
                <th>Version</th>
                <th>Betrieb</th>
                {props.canWrite ? <th>Aktion</th> : null}
              </tr>
            </thead>
            <tbody>
              {props.deploymentReleases.map((release) => (
                <tr key={`${release.target_path}:${release.triton_model_name}`}>
                  <td>{release.target_path === 'realtime' ? 'Echtzeit' : 'Batch'}</td>
                  <td><code>{release.triton_model_name}</code></td>
                  <td>{release.triton_model_version}</td>
                  <td>
                    <span className={`badge badge--${release.serving_enabled ? 'success' : 'neutral'}`}>
                      {release.serving_enabled ? 'Aktiv' : deploymentReleaseStatusLabel(release.status)}
                    </span>
                  </td>
                  {props.canWrite ? (
                    <td>
                      {release.serving_enabled ? (
                        <button
                          type="button"
                          className="button--danger"
                          disabled={props.deploymentServingPending}
                          onClick={() => props.onDeactivateModel(release)}
                        >
                          Modell deaktivieren
                        </button>
                      ) : release.status === 'deactivated' ? (
                        <button
                          type="button"
                          className="button--primary"
                          disabled={props.deploymentServingPending}
                          onClick={() => props.onActivateModel(release)}
                        >
                          Modell aktivieren
                        </button>
                      ) : '—'}
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="subtle-text">Für dieses Modell existiert noch kein Deployment.</p>
        )}
      </section>
      <div className="deployment-api-grid">
        <section><h4>API-Routen</h4>
          {endpoint?.api_examples.length ? <div className="endpoint-route-list">{endpoint.api_examples.map((example) => <div className="endpoint-snippet" key={`${example.method}-${example.path}`}><strong>{example.description}</strong><code className="endpoint-route">{example.method} {example.path}</code></div>)}</div> : <p className="subtle-text">API-Routen werden nach dem ersten Deployment veröffentlicht.</p>}
        </section>
        <section className="deployment-api-test"><h4>Test-Inferenz</h4>
          {realtime ? <ProjectInferenceTestPanel {...props} /> : <p className="subtle-text">Für einen direkten Test muss Realtime aktiviert sein.</p>}
        </section>
      </div>
      {props.canWrite ? <details className="advanced-section"><summary>Gefährlicher Test-Override</summary><p className="subtle-text">Nur für isolierte Tests: veröffentlicht auch eine Version mit nicht bestandenen Gates.</p><button className="button--danger" disabled={props.deployPending} onClick={props.onForceDeploy}>Quality Gates einmalig übergehen</button></details> : null}
    </div>
  );
}

function ProjectInferenceTestPanel(props: {
  inferenceFile: File | null;
  inferencePending: boolean;
  inferenceResult: ProjectInferenceResult | undefined;
  inferenceError: { title: string; description?: string } | null;
  onInferenceFile: (file: File | null) => void;
  onInference: () => void;
}) {
  const previewUrl = useMemo(
    () => props.inferenceFile ? URL.createObjectURL(props.inferenceFile) : null,
    [props.inferenceFile]
  );
  const imageTooLarge = Boolean(
    props.inferenceFile && props.inferenceFile.size > TEST_INFERENCE_MAX_IMAGE_BYTES
  );

  useEffect(() => () => {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
  }, [previewUrl]);

  return (
    <div className="panel-stack">
      <div className="project-inference-layout">
        <label className="inference-upload project-inference-upload">
          <span className="inference-upload__preview">
            {previewUrl ? <img src={previewUrl} alt="Ausgewähltes Testbild" /> : <span className="result-card__placeholder">Noch kein Testbild ausgewählt</span>}
          </span>
          <span className="inference-upload__body">
            <span>Testbild</span>
            <strong>{props.inferenceFile?.name ?? 'Bilddatei auswählen'}</strong>
            <small>{props.inferenceFile ? formatBytes(props.inferenceFile.size) : `JPEG, PNG, BMP oder WebP · bis ${formatBytes(TEST_INFERENCE_MAX_IMAGE_BYTES)}`}</small>
            <input
              type="file"
              accept="image/jpeg,image/png,image/bmp,image/webp"
              onChange={(event) => props.onInferenceFile(event.target.files?.[0] ?? null)}
            />
          </span>
        </label>
        <div className="project-inference-controls">
          <div>
            <strong>Direkt am Realtime-Endpunkt testen</strong>
            <p className="subtle-text">Das Bild wird nicht im Projekt gespeichert und benötigt keinen gestarteten Kanal. Das aktive Realtime-Deployment wird bei Bedarf geladen und direkt getestet; externe API-Routen bleiben für Maschinen-API-Keys vorgesehen.</p>
          </div>
          {imageTooLarge ? <div className="inline-alert inline-alert--warn"><strong>Datei zu groß</strong><p>Für die Test-Inferenz sind Bilder bis {formatBytes(TEST_INFERENCE_MAX_IMAGE_BYTES)} zugelassen.</p></div> : null}
          {props.inferenceError ? <div className="inline-alert inline-alert--danger"><strong>{props.inferenceError.title}</strong>{props.inferenceError.description ? <p>{props.inferenceError.description}</p> : null}</div> : null}
          <div className="actions-row">
            <button type="button" className="button--primary" disabled={!props.inferenceFile || imageTooLarge || props.inferencePending} onClick={props.onInference}>
              {props.inferencePending ? 'Analysiert…' : 'Testbild analysieren'}
            </button>
            <span className="subtle-text">Serverlimit: {formatBytes(TEST_INFERENCE_MAX_IMAGE_BYTES)} pro Bild</span>
          </div>
        </div>
      </div>
      {props.inferenceResult && previewUrl ? <ProjectInferenceResultPanel result={props.inferenceResult} imageUrl={previewUrl} /> : null}
    </div>
  );
}

function ProjectInferenceResultPanel(props: { result: ProjectInferenceResult; imageUrl: string }) {
  const detections = isProjectDetectionResult(props.result) ? props.result.detections : null;
  const segments = isProjectSegmentationResult(props.result) ? props.result.segments : null;
  const anomaly = isProjectAnomalyResult(props.result) ? props.result : null;
  const classifications = isProjectClassificationResult(props.result) ? props.result.labels : null;
  const imageWidth = 'image_width' in props.result ? props.result.image_width : undefined;
  const imageHeight = 'image_height' in props.result ? props.result.image_height : undefined;
  const aspectRatio = imageWidth && imageHeight ? `${imageWidth} / ${imageHeight}` : undefined;

  return (
    <div className="project-inference-result">
      <div className="detail-grid">
        <MetricPill label="Deployment" value={props.result.route.deployment_release_id} tone="success" />
        <MetricPill label="Gesamtlatenz" value={`${props.result.timing.total_ms.toFixed(1)} ms`} />
        <MetricPill label="Modell" value={props.result.route.triton_model_name} />
        <MetricPill label="Ergebnis" value={projectInferenceCountLabel(props.result)} />
      </div>
      <div className="visual-inspection-card project-inference-result__visual">
        <div className="visual-inspection-card__image" style={{ aspectRatio }}>
          <img src={props.imageUrl} alt="Testbild mit Inferenz-Overlay" />
          {detections ? <ProjectDetectionOverlay detections={detections} /> : null}
          {segments ? <svg className="visual-overlay" viewBox="0 0 100 100" preserveAspectRatio="none">{segments.map((segment, index) => <polygon key={`${segment.label}-${index}`} className="visual-overlay__polygon" points={segment.polygon.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')} />)}</svg> : null}
          {anomaly ? <img className="visual-overlay visual-overlay__heatmap" src={`data:image/png;base64,${anomaly.heatmap.data}`} alt="Anomalie-Heatmap" /> : null}
        </div>
        <div className="visual-inspection-card__details">
          {classifications ? <ProjectClassificationResult predictions={classifications} /> : null}
          {detections ? <ProjectDetectionResult detections={detections} /> : null}
          {segments ? <ProjectSegmentationResult segments={segments} /> : null}
          {anomaly ? <ProjectAnomalyResult result={anomaly} /> : null}
        </div>
      </div>
      <details className="advanced-section">
        <summary>Rohantwort und technische Details</summary>
        <pre className="code-block inference-raw-result">{JSON.stringify(props.result, null, 2)}</pre>
      </details>
    </div>
  );
}

function ProjectDetectionOverlay(props: { detections: DetectionInferenceResponse['detections'] }) {
  const orientedDetections = props.detections.filter((detection) => detection.oriented_box);
  return <>
    <div className="visual-overlay">{props.detections.map((detection, index) => <div key={`${detection.label}-${index}`} className="visual-overlay__box" style={{ left: `${detection.box.x_min * 100}%`, top: `${detection.box.y_min * 100}%`, width: `${(detection.box.x_max - detection.box.x_min) * 100}%`, height: `${(detection.box.y_max - detection.box.y_min) * 100}%` }}><span>{detection.label}: {(detection.score * 100).toFixed(1)}%</span></div>)}</div>
    {orientedDetections.length ? <svg className="visual-overlay project-inference-oriented-overlay" viewBox="0 0 100 100" preserveAspectRatio="none">{orientedDetections.map((detection, index) => <polygon key={`${detection.label}-${index}`} className="visual-overlay__polygon" points={detection.oriented_box!.points.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')} />)}</svg> : null}
  </>;
}

function ProjectClassificationResult(props: { predictions: ClassificationInferenceResponse['labels'] }) {
  return <table className="data-table compact-table"><thead><tr><th>Klasse</th><th>Konfidenz</th></tr></thead><tbody>{props.predictions.length ? props.predictions.map((prediction) => <tr key={prediction.label}><td>{prediction.label}</td><td>{(prediction.score * 100).toFixed(2)}%</td></tr>) : <tr><td colSpan={2}>Keine Klassifikation zurückgegeben.</td></tr>}</tbody></table>;
}

function ProjectDetectionResult(props: { detections: DetectionInferenceResponse['detections'] }) {
  return <table className="data-table compact-table"><thead><tr><th>Objekt</th><th>Konfidenz</th><th>Geometrie</th></tr></thead><tbody>{props.detections.length ? props.detections.map((detection, index) => <tr key={`${detection.label}-${index}`}><td>{detection.label}</td><td>{(detection.score * 100).toFixed(2)}%</td><td>{detection.oriented_box ? `Orientiert · ${detection.oriented_box.rotation_degrees?.toFixed(1) ?? '—'}°` : 'Rechteck'}</td></tr>) : <tr><td colSpan={3}>Keine Objekte erkannt.</td></tr>}</tbody></table>;
}

function ProjectSegmentationResult(props: { segments: SegmentationInferenceResponse['segments'] }) {
  return <table className="data-table compact-table"><thead><tr><th>Segment</th><th>Konfidenz</th><th>Punkte</th></tr></thead><tbody>{props.segments.length ? props.segments.map((segment, index) => <tr key={`${segment.label}-${index}`}><td>{segment.label}</td><td>{(segment.score * 100).toFixed(2)}%</td><td>{segment.polygon.length}</td></tr>) : <tr><td colSpan={3}>Keine Segmente erkannt.</td></tr>}</tbody></table>;
}

function ProjectAnomalyResult(props: { result: AnomalyInferenceResponse }) {
  return <table className="data-table compact-table"><tbody><tr><th>Entscheidung</th><td>{props.result.predicted_label === 'anomalous' ? 'Auffällig' : 'Unauffällig'}</td></tr><tr><th>Score</th><td>{props.result.anomaly_score.toFixed(4)}</td></tr><tr><th>Schwelle</th><td>{props.result.threshold === null || props.result.threshold === undefined ? '—' : props.result.threshold.toFixed(4)}</td></tr></tbody></table>;
}

function isProjectClassificationResult(result: ProjectInferenceResult): result is ClassificationInferenceResponse {
  return 'labels' in result;
}

function isProjectDetectionResult(result: ProjectInferenceResult): result is DetectionInferenceResponse {
  return 'detections' in result;
}

function isProjectSegmentationResult(result: ProjectInferenceResult): result is SegmentationInferenceResponse {
  return 'segments' in result;
}

function isProjectAnomalyResult(result: ProjectInferenceResult): result is AnomalyInferenceResponse {
  return 'anomaly_score' in result;
}

function projectInferenceCountLabel(result: ProjectInferenceResult) {
  if (isProjectClassificationResult(result)) return `${result.labels.length} Klassen`;
  if (isProjectDetectionResult(result)) return `${result.detections.length} Objekte`;
  if (isProjectSegmentationResult(result)) return `${result.segments.length} Segmente`;
  return result.predicted_label === 'anomalous' ? 'Auffällig' : 'Unauffällig';
}

function deploymentReleaseStatusLabel(status: DeploymentReleaseSummary['status']) {
  if (status === 'deactivated') return 'Deaktiviert';
  if (status === 'activation_failed') return 'Aktivierung fehlgeschlagen';
  if (status === 'loading') return 'Wird geladen';
  if (status === 'smoke_testing') return 'Smoke-Test';
  if (status === 'repository_sync') return 'Repository-Sync';
  if (status === 'packaging') return 'Paketierung';
  if (status === 'requested') return 'Angefordert';
  if (status === 'rollback_requested') return 'Rollback angefordert';
  if (status === 'rolled_back') return 'Zurückgerollt';
  if (status === 'retired') return 'Außer Betrieb';
  return 'Aktiv';
}

function AutomationSection(props: {
  pipeline: ProjectPipelineDetail;
  policy: ProjectAutomationPolicy | null;
  automationModel?: ProjectModelSummary;
  canWrite: boolean;
  pending: boolean;
  gatePending: boolean;
  onPolicy: (policy: ProjectAutomationPolicy | null) => void;
  onSave: () => void;
  onSaveGates: (model: ProjectModelSummary, policy: PromotionCriteriaPolicy) => void;
}) {
  const policy = props.policy;
  if (!policy) return null;
  const update = (patch: Partial<ProjectAutomationPolicy>) => props.onPolicy({ ...policy, ...patch });
  const incomplete = policy.auto_train_enabled && !props.automationModel;
  return (
    <section id="automation" className="project-section">
      <div className="project-section__heading">
        <div><p className="training-hero__eyebrow">03</p><h2>Automatismus</h2><p>Lege nachvollziehbar fest, was nach neuer Label-Studio-Aktivität automatisch geschieht.</p></div>
      </div>
      <Panel title="Automatischer Ablauf">
        <div className="automation-flow" aria-label="Automatisierter Projektablauf">
          {[
            ['Synchronisierung', policy.auto_sync_enabled],
            ['Snapshot', policy.auto_snapshot_enabled],
            ['Training', policy.auto_train_enabled],
            ['Deployment', policy.auto_deploy_enabled]
          ].map(([label, enabled], index) => <div key={String(label)} className={enabled ? 'is-enabled' : ''}><span>{index + 1}</span><strong>{label}</strong><small>{enabled ? 'Aktiv' : 'Manuell'}</small></div>)}
        </div>
        {incomplete ? <div className="inline-alert inline-alert--danger"><strong>Konfiguration unvollständig</strong><p>Wähle ein gültiges Standardmodell oder deaktiviere automatisches Training.</p></div> : null}
        <form className="automation-form" onSubmit={(event) => { event.preventDefault(); props.onSave(); }}>
          <div className="automation-switches">
            <AutomationSwitch label="Label-Studio-Aktivität automatisch synchronisieren" checked={policy.auto_sync_enabled} disabled={!props.canWrite} onChange={(checked) => update({ auto_sync_enabled: checked })} />
            <AutomationSwitch label="Nach qualifizierender Aktivität Snapshot erzeugen" checked={policy.auto_snapshot_enabled} disabled={!props.canWrite} onChange={(checked) => update({ auto_snapshot_enabled: checked })} />
            <AutomationSwitch label="Gültigen neuen Snapshot automatisch trainieren" checked={policy.auto_train_enabled} disabled={!props.canWrite || !policy.default_model_family_id} onChange={(checked) => update({ auto_train_enabled: checked })} />
            <AutomationSwitch label="Nach bestandenen Gates automatisch deployen" checked={policy.auto_deploy_enabled} disabled={!props.canWrite} onChange={(checked) => update({ auto_deploy_enabled: checked })} />
          </div>
          <div className="training-form-grid">
            <label><span>Standardmodell für neue Snapshots</span><select value={policy.default_model_family_id ?? ''} disabled={!props.canWrite} onChange={(event) => update({ default_model_family_id: event.target.value || null, auto_train_enabled: event.target.value ? policy.auto_train_enabled : false })}>
              <option value="">Nicht konfiguriert</option>
              {props.pipeline.models.map((model) => <option key={model.model_family_id} value={model.model_family_id}>{model.display_name} · {architectureLabel(model.architecture)}</option>)}
            </select><small>Ein konkretes bestehendes Projektmodell wird auf jedem neuen gültigen Snapshot erneut trainiert.</small></label>
            <label><span>Template für neue Modelle</span><select value={policy.default_model_template ?? ''} disabled={!props.canWrite} onChange={(event) => update({ default_model_template: event.target.value || null })}>
              <option value="">Empfohlenes Template</option>
              {props.pipeline.model_templates.filter((item) => item.active).map((template) => <option key={template.template_key} value={template.template_key}>{templateOptionLabel(template)}</option>)}
            </select></label>
            <label><span>Deployment-Ziel</span><select value={policy.preferred_endpoint_mode} disabled={!props.canWrite} onChange={(event) => update({ preferred_endpoint_mode: event.target.value as ProjectAutomationPolicy['preferred_endpoint_mode'] })}><option value="realtime">Realtime</option><option value="batch">Batch</option><option value="both">Realtime & Batch</option></select></label>
          </div>
          {props.canWrite ? <div className="actions-row"><button className="button--primary" type="submit" disabled={props.pending || incomplete}>{props.pending ? 'Speichert…' : 'Automatismus speichern'}</button></div> : null}
        </form>
      </Panel>
      <Panel title="Quality Gates des Standardmodells">
        {props.automationModel ? (
          <QualityGateEditor
            key={`${props.automationModel.model_family_id}-${JSON.stringify(props.automationModel.promotion_policy)}`}
            policy={props.automationModel.promotion_policy}
            editable={props.canWrite && props.automationModel.promotion_policy.allow_model_override}
            pending={props.gatePending}
            onSave={(nextPolicy) => props.onSaveGates(props.automationModel!, nextPolicy)}
          />
        ) : <EmptyState title="Kein Standardmodell ausgewählt" body="Quality Gates werden angezeigt, sobald ein konkretes Standardmodell gewählt wurde." />}
      </Panel>
    </section>
  );
}

function AutomationSwitch(props: { label: string; checked: boolean; disabled: boolean; onChange: (checked: boolean) => void }) {
  return <label className="automation-switch"><input type="checkbox" checked={props.checked} disabled={props.disabled} onChange={(event) => props.onChange(event.target.checked)} /><span><strong>{props.label}</strong><small>{props.checked ? 'Aktiviert' : 'Deaktiviert'}</small></span></label>;
}

function QualityGateEditor(props: {
  policy: PromotionCriteriaPolicy;
  editable: boolean;
  pending: boolean;
  onSave: (policy: PromotionCriteriaPolicy) => void;
}) {
  const [draft, setDraft] = useState(props.policy);
  return (
    <div className="quality-gate-editor">
      <p className="subtle-text">Änderungen gelten nur für zukünftige Trainingsläufe. Bereits laufende und abgeschlossene Runs bleiben unverändert.</p>
      <div className="quality-gate-list">
        {draft.criteria.map((gate, index) => <div key={`${gate.metric_name}-${gate.phase}-${index}`} className="quality-gate-row"><div><strong>{metricLabel(gate.metric_name)}</strong><small>{gate.phase} · {gate.comparison === 'gte' ? 'mindestens' : 'höchstens'}</small></div><input type="number" step="0.001" value={gate.threshold} disabled={!props.editable} aria-label={`Schwellwert ${gate.metric_name}`} onChange={(event) => setDraft({ ...draft, criteria: draft.criteria.map((item, itemIndex) => itemIndex === index ? { ...item, threshold: Number(event.target.value) } : item) })} /></div>)}
      </div>
      {props.editable ? <button disabled={props.pending} onClick={() => props.onSave(draft)}>{props.pending ? 'Speichert…' : 'Gates für zukünftige Runs speichern'}</button> : <p className="subtle-text">Dieses Template erlaubt keine modellbezogenen Gate-Overrides.</p>}
    </div>
  );
}

function CreateModelDialog(props: {
  open: boolean;
  templates: ModelTemplateSummary[];
  architectures: SupportedModelArchitecture[];
  availableClasses: string[];
  selectedTemplateKey: string;
  architectureKey: string;
  classNames: string[];
  useAllClasses: boolean;
  segmentationMode: 'instance' | 'semantic' | null;
  name: string;
  description: string;
  trainingOverrides: TrainingDefaults | null;
  gatePolicy: PromotionCriteriaPolicy | null;
  pending: boolean;
  onClose: () => void;
  onTemplate: (key: string) => void;
  onName: (name: string) => void;
  onDescription: (description: string) => void;
  onArchitecture: (key: string) => void;
  onClassNames: (classNames: string[]) => void;
  onUseAllClasses: (useAllClasses: boolean) => void;
  onSegmentationMode: (mode: 'instance' | 'semantic' | null) => void;
  onTrainingOverrides: (defaults: TrainingDefaults | null) => void;
  onGatePolicy: (policy: PromotionCriteriaPolicy | null) => void;
  onSubmit: () => void;
}) {
  if (!props.open) return null;
  const template = props.templates.find((item) => item.template_key === props.selectedTemplateKey);
  const matchingArchitectures = props.architectures.filter((item) => {
    if (!item.supported || item.status === 'hidden') return false;
    if (template?.task_type !== 'segmentation' || !props.segmentationMode) return true;
    return item.capability_metadata.segmentation_mode === props.segmentationMode;
  });
  const selectSegmentationMode = (mode: 'instance' | 'semantic') => {
    props.onSegmentationMode(mode);
    const nextArchitecture = props.architectures.find(
      (item) =>
        item.supported
        && item.status !== 'hidden'
        && item.capability_metadata.segmentation_mode === mode
    );
    if (!nextArchitecture) return;
    const nextArchitectureKey = architectureKey(nextArchitecture.architecture);
    const matchingTemplate = props.templates.find(
      (item) =>
        item.active
        && item.task_type === 'segmentation'
        && architectureKey(item.architecture) === nextArchitectureKey
    );
    if (matchingTemplate) {
      props.onTemplate(matchingTemplate.template_key);
      return;
    }
    props.onArchitecture(nextArchitectureKey);
  };
  const toggleClass = (className: string) => {
    props.onClassNames(
      props.classNames.includes(className)
        ? props.classNames.filter((item) => item !== className)
        : [...props.classNames, className]
    );
  };
  return (
    <div className="modal-backdrop" onClick={props.onClose}>
      <div className="modal-panel modal-panel--wide" role="dialog" aria-modal="true" aria-labelledby="create-model-title" onClick={(event) => event.stopPropagation()}>
        <div className="panel__header"><div><p className="training-hero__eyebrow">Geführte Erstellung</p><h2 id="create-model-title">Neues Modell</h2><p className="subtle-text">Lege sichtbar fest, welche Klassen und welche Ausgabe dieses Modell lernen soll.</p></div><button onClick={props.onClose}>Schließen</button></div>
        <form className="panel-stack" onSubmit={(event) => { event.preventDefault(); props.onSubmit(); }}>
          <div className="training-form-grid"><label><span>Modellname</span><input autoFocus value={props.name} onChange={(event) => props.onName(event.target.value)} placeholder="z. B. Kantenfehler Segmentierung" /></label><label><span>Template</span><select value={props.selectedTemplateKey} onChange={(event) => props.onTemplate(event.target.value)}>{props.templates.filter((item) => item.active).map((item) => <option key={item.template_key} value={item.template_key}>{templateOptionLabel(item)}</option>)}</select></label></div>
          {template ? <div className="model-template-summary"><MetricPill label="Architektur" value={architectureLabel(template.architecture)} /><MetricPill label="Task" value={taskTypeLabel(template.task_type)} /><MetricPill label="Epochen" value={String(template.defaults.training.epochs)} /><MetricPill label="Batch" value={String(template.defaults.training.batch_size ?? 'auto')} /></div> : null}
          {template?.task_type === 'segmentation' ? (
            <fieldset className="model-scope-fieldset">
              <legend>Segmentierungsart</legend>
              <div className="model-scope-options">
                <label className={props.segmentationMode === 'instance' ? 'is-selected' : ''}>
                  <input type="radio" name="segmentation-mode" checked={props.segmentationMode === 'instance'} onChange={() => selectSegmentationMode('instance')} />
                  <span><strong>Instanz-Segmentierung</strong><small>Einzelne Objekte mit getrennten Masken, z. B. YOLO26-seg.</small></span>
                </label>
                <label className={props.segmentationMode === 'semantic' ? 'is-selected' : ''}>
                  <input type="radio" name="segmentation-mode" checked={props.segmentationMode === 'semantic'} onChange={() => selectSegmentationMode('semantic')} />
                  <span><strong>Semantische Segmentierung</strong><small>Eine Klassenmaske pro Pixel, z. B. DeepLabV3 oder FCN.</small></span>
                </label>
              </div>
            </fieldset>
          ) : null}
          <fieldset className="model-scope-fieldset">
            <legend>Trainingsklassen</legend>
            <div className="model-scope-options model-scope-options--compact">
              <label className={props.useAllClasses ? 'is-selected' : ''}>
                <input type="radio" name="class-scope" checked={props.useAllClasses} onChange={() => props.onUseAllClasses(true)} />
                <span><strong>Alle Klassen</strong><small>Folgt automatisch dem vollständigen Label-Studio-Klassensatz.</small></span>
              </label>
              <label className={!props.useAllClasses ? 'is-selected' : ''}>
                <input type="radio" name="class-scope" checked={!props.useAllClasses} onChange={() => props.onUseAllClasses(false)} />
                <span><strong>Ausgewählte Klassen</strong><small>Andere Annotationen werden beim Staging dieses Modells ausgefiltert.</small></span>
              </label>
            </div>
            {!props.useAllClasses ? (
              props.availableClasses.length ? (
                <div className="model-class-grid">
                  {props.availableClasses.map((className) => (
                    <label key={className} className={props.classNames.includes(className) ? 'is-selected' : ''}>
                      <input type="checkbox" checked={props.classNames.includes(className)} onChange={() => toggleClass(className)} />
                      <span>{className}</span>
                    </label>
                  ))}
                </div>
              ) : (
                <p className="inline-alert inline-alert--warn">Das Projekt liefert noch keine erkennbaren Klassen. Synchronisiere zuerst die Label-Konfiguration.</p>
              )
            ) : null}
          </fieldset>
          <details className="advanced-section"><summary>Erweitert: Architektur, Training und Gates</summary>
            <div className="training-form-grid">
              <label className="training-form-grid__full"><span>Beschreibung</span><textarea value={props.description} onChange={(event) => props.onDescription(event.target.value)} /></label>
              <label><span>Architektur</span><select value={props.architectureKey} onChange={(event) => props.onArchitecture(event.target.value)}>{matchingArchitectures.map((item) => <option key={architectureKey(item.architecture)} value={architectureKey(item.architecture)}>{architectureLabel(item.architecture)} · {item.status}</option>)}</select></label>
              <label><span>Epochen</span><input type="number" min={1} max={1000} value={props.trainingOverrides?.epochs ?? ''} onChange={(event) => props.trainingOverrides && props.onTrainingOverrides({ ...props.trainingOverrides, epochs: Number(event.target.value) })} /></label>
              <label><span>Lernrate</span><input type="number" min={0.000001} max={10} step={0.000001} value={props.trainingOverrides?.learning_rate ?? ''} onChange={(event) => props.trainingOverrides && props.onTrainingOverrides({ ...props.trainingOverrides, learning_rate: Number(event.target.value) })} /></label>
              <label><span>Bildgröße</span><input type="number" min={32} max={4096} step={32} title="Die Bildgröße muss durch 32 teilbar sein." value={props.trainingOverrides?.image_size ?? ''} onChange={(event) => props.trainingOverrides && props.onTrainingOverrides({ ...props.trainingOverrides, image_size: event.target.value ? Number(event.target.value) : null })} /><small>Vielfaches von 32, z. B. 1184 oder 1216.</small></label>
              <label><span>Batch-Größe</span><input type="number" min={1} max={4096} value={props.trainingOverrides?.batch_size ?? ''} onChange={(event) => props.trainingOverrides && props.onTrainingOverrides({ ...props.trainingOverrides, batch_size: event.target.value ? Number(event.target.value) : null })} /></label>
              <label><span>Optimizer</span><input value={props.trainingOverrides?.optimizer ?? ''} onChange={(event) => props.trainingOverrides && props.onTrainingOverrides({ ...props.trainingOverrides, optimizer: event.target.value || null })} /></label>
              <label><span>Checkpoint-Intervall</span><input type="number" min={1} max={100000} value={props.trainingOverrides?.checkpoint_interval_steps ?? ''} onChange={(event) => props.trainingOverrides && props.onTrainingOverrides({ ...props.trainingOverrides, checkpoint_interval_steps: Number(event.target.value) })} /></label>
            </div>
            <p className="subtle-text">Diese Overrides werden am Modell gespeichert und gelten für dessen zukünftige Trainingsläufe. Das globale Template bleibt unverändert.</p>
            {props.gatePolicy ? <QualityGateEditor policy={props.gatePolicy} editable={Boolean(props.gatePolicy.allow_model_override)} pending={false} onSave={props.onGatePolicy} /> : null}
          </details>
          <div className="actions-row"><button className="button--primary" type="submit" disabled={!template || props.pending || (!props.useAllClasses && !props.classNames.length)}>{props.pending ? 'Wird angelegt…' : 'Modell anlegen'}</button><button type="button" onClick={props.onClose}>Abbrechen</button></div>
        </form>
      </div>
    </div>
  );
}

function TemplateDrawer(props: { open: boolean; templates: ModelTemplateSummary[]; labelProjectId: string; onClose: () => void }) {
  if (!props.open) return null;
  return <div className="drawer-backdrop" onClick={props.onClose}><aside className="template-drawer" role="dialog" aria-modal="true" aria-labelledby="template-drawer-title" onClick={(event) => event.stopPropagation()}><header><div><p className="training-hero__eyebrow">Global</p><h2 id="template-drawer-title">Template-Administration</h2></div><button onClick={props.onClose}>Schließen</button></header><div className="inline-alert inline-alert--warn"><strong>Globale Auswirkung</strong><p>Änderungen gelten plattformweit und nur für zukünftige Modelle beziehungsweise Trainingsläufe.</p></div><div className="template-drawer__list">{props.templates.map((template) => <article key={template.template_key}><div><strong>{template.label}</strong><span className={`badge badge--${template.active ? 'success' : 'neutral'}`}>{template.active ? 'Aktiv' : 'Inaktiv'}</span></div><p>{template.description}</p><small>{architectureLabel(template.architecture)} · Version {template.template_version}</small></article>)}</div><Link to="/operations/model-templates" search={{ templateKey: undefined, returnToLabelProjectId: props.labelProjectId }} className="primary-link">Globale Administration öffnen</Link></aside></div>;
}

function TrainingMetricsChart({ metrics }: { metrics: TrainingMetricRecord[] }) {
  const palette = ['#006734', '#006c8a', '#c47b00', '#9b3f92', '#b94134', '#46566a'];
  const plot = { left: 68, right: 876, top: 20, bottom: 244 };
  const grouped = new Map<string, TrainingMetricRecord[]>();
  metrics.forEach((metric) => {
    const key = `${metric.phase}:${metric.metric_name}`;
    grouped.set(key, [...(grouped.get(key) ?? []), metric]);
  });
  const series = Array.from(grouped.entries())
    .map(([key, values]) => ({ key, values: values.sort((left, right) => left.epoch_no - right.epoch_no || left.step_no - right.step_no) }))
    .sort((left, right) => Number(right.values.some((item) => item.is_primary)) - Number(left.values.some((item) => item.is_primary)))
    .slice(0, 6)
    .map((item, index) => ({ ...item, color: palette[index] }));
  const [hiddenSeriesKeys, setHiddenSeriesKeys] = useState<string[]>([]);
  const [hoveredPoint, setHoveredPoint] = useState<{
    seriesKey: string;
    metric: TrainingMetricRecord;
  } | null>(null);
  if (!series.length) return <div className="metrics-chart-empty"><strong>Live-Metriken warten auf den ersten Messpunkt</strong><p>Während eines aktiven Runs wird diese Ansicht alle zwei Sekunden aktualisiert.</p></div>;

  const hiddenSeries = new Set(hiddenSeriesKeys);
  const visibleSeries = series.filter((item) => !hiddenSeries.has(item.key));
  const visibleMetrics = visibleSeries.flatMap((item) => item.values);
  const xAxisLabel = visibleMetrics.some((metric) => metric.epoch_no > 0) ? 'Epoche' : 'Schritt';
  const xValues = visibleMetrics.map(metricXValue);
  const observedMinX = xValues.length ? Math.min(...xValues) : 0;
  const observedMaxX = xValues.length ? Math.max(...xValues) : 1;
  const minX = observedMinX === observedMaxX ? 0 : observedMinX;
  const maxX = observedMinX === observedMaxX ? Math.max(observedMaxX, 1) : observedMaxX;
  const rangeX = Math.max(maxX - minX, 1);
  const yDomain = metricChartDomain(visibleMetrics.map((metric) => metric.metric_value));
  const xPosition = (value: number) =>
    plot.left + ((value - minX) / rangeX) * (plot.right - plot.left);
  const yPosition = (value: number) =>
    plot.top + (1 - (value - yDomain.min) / yDomain.range) * (plot.bottom - plot.top);
  const points = (values: TrainingMetricRecord[]) =>
    values.map((metric) => `${xPosition(metricXValue(metric))},${yPosition(metric.metric_value)}`).join(' ');
  const yTicks = Array.from({ length: 5 }, (_, index) => {
    const ratio = index / 4;
    return {
      y: plot.top + ratio * (plot.bottom - plot.top),
      value: yDomain.max - ratio * yDomain.range
    };
  });
  const xTicks = Array.from(new Set(
    Array.from({ length: 5 }, (_, index) => {
      const value = minX + (index / 4) * rangeX;
      return xAxisLabel === 'Epoche' ? Math.round(value) : Number(value.toFixed(2));
    })
  ));
  const hoveredSeries = hoveredPoint
    ? visibleSeries.find((item) => item.key === hoveredPoint.seriesKey)
    : undefined;
  const hoveredMetric = hoveredSeries ? hoveredPoint?.metric : undefined;
  const hoveredX = hoveredMetric ? xPosition(metricXValue(hoveredMetric)) : 0;
  const hoveredY = hoveredMetric ? yPosition(hoveredMetric.metric_value) : 0;
  const tooltipWidth = 238;
  const tooltipHeight = 64;
  const tooltipX = hoveredX + tooltipWidth + 18 < 900
    ? hoveredX + 12
    : hoveredX - tooltipWidth - 12;
  const tooltipY = Math.max(6, Math.min(hoveredY - tooltipHeight - 10, 292 - tooltipHeight));
  const toggleSeries = (seriesKey: string) => {
    setHiddenSeriesKeys((current) =>
      current.includes(seriesKey)
        ? current.filter((item) => item !== seriesKey)
        : [...current, seriesKey]
    );
    setHoveredPoint((current) => current?.seriesKey === seriesKey ? null : current);
  };

  return (
    <section className="training-metrics-chart" aria-labelledby="metrics-chart-title">
      <div className="training-metrics-chart__header">
        <div>
          <h4 id="metrics-chart-title">Metriken über Epochen</h4>
          <p className="subtle-text">Linien in der Legende ein- oder ausblenden. Für einen Messwert über die Linie fahren.</p>
        </div>
        <div className="metrics-legend" aria-label="Sichtbare Metriklinien">
          {series.map((item) => {
            const visible = !hiddenSeries.has(item.key);
            return (
              <button
                key={item.key}
                type="button"
                className={visible ? '' : 'is-hidden'}
                aria-pressed={visible}
                onClick={() => toggleSeries(item.key)}
              >
                <i style={{ background: item.color }} />
                {metricSeriesLabel(item.key)}
              </button>
            );
          })}
          {hiddenSeriesKeys.length ? (
            <button type="button" className="metrics-legend__reset" onClick={() => setHiddenSeriesKeys([])}>
              Alle einblenden
            </button>
          ) : null}
        </div>
      </div>
      <div className="metrics-chart-canvas">
        <svg
          viewBox="0 0 900 292"
          role="img"
          aria-labelledby="metrics-svg-title metrics-svg-desc"
          preserveAspectRatio="none"
          onPointerLeave={() => setHoveredPoint(null)}
        >
          <title id="metrics-svg-title">Trainings- und Validierungsmetriken</title>
          <desc id="metrics-svg-desc">Interaktives Liniendiagramm der sichtbaren Messwerte. Die X-Achse zeigt Epochen beziehungsweise Schritte. Eine tabellarische Ansicht folgt unterhalb.</desc>
          <line x1={plot.left} y1={plot.top} x2={plot.left} y2={plot.bottom} className="chart-axis" />
          <line x1={plot.left} y1={plot.bottom} x2={plot.right} y2={plot.bottom} className="chart-axis" />
          {yTicks.map((tick, index) => (
            <g key={`y-${index}`}>
              <line x1={plot.left} y1={tick.y} x2={plot.right} y2={tick.y} className="chart-grid" />
              <text x={plot.left - 9} y={tick.y + 4} textAnchor="end">{formatAxisNumber(tick.value)}</text>
            </g>
          ))}
          {xTicks.map((tick) => (
            <g key={`x-${tick}`}>
              <line x1={xPosition(tick)} y1={plot.bottom} x2={xPosition(tick)} y2={plot.bottom + 5} className="chart-axis" />
              <text x={xPosition(tick)} y={plot.bottom + 19} textAnchor="middle">{formatAxisNumber(tick)}</text>
            </g>
          ))}
          <text x={(plot.left + plot.right) / 2} y="284" textAnchor="middle" className="chart-axis-label">{xAxisLabel}</text>
          {visibleSeries.map((item) => (
            <g key={item.key}>
              <polyline
                className="chart-series-line"
                points={points(item.values)}
                fill="none"
                stroke={item.color}
                strokeWidth="3"
                vectorEffect="non-scaling-stroke"
              />
              {item.values.map((metric) => (
                <circle
                  key={metric.metric_id}
                  className={`chart-series-point${hoveredMetric?.metric_id === metric.metric_id && hoveredPoint?.seriesKey === item.key ? ' is-active' : ''}`}
                  cx={xPosition(metricXValue(metric))}
                  cy={yPosition(metric.metric_value)}
                  r={hoveredMetric?.metric_id === metric.metric_id && hoveredPoint?.seriesKey === item.key ? 5 : 2.5}
                  fill="#fff"
                  stroke={item.color}
                  vectorEffect="non-scaling-stroke"
                  onPointerEnter={() => setHoveredPoint({ seriesKey: item.key, metric })}
                >
                  <title>{metricSeriesLabel(item.key)} · {xAxisLabel} {formatAxisNumber(metricXValue(metric))} · {formatMetric(metric.metric_value)}</title>
                </circle>
              ))}
              <polyline
                className="chart-series-hit"
                points={points(item.values)}
                fill="none"
                stroke="transparent"
                strokeWidth="16"
                vectorEffect="non-scaling-stroke"
                onPointerMove={(event) => {
                  const svg = event.currentTarget.ownerSVGElement;
                  if (!svg) return;
                  const bounds = svg.getBoundingClientRect();
                  const viewX = ((event.clientX - bounds.left) / bounds.width) * 900;
                  const clampedX = Math.max(plot.left, Math.min(viewX, plot.right));
                  const valueAtPointer = minX + ((clampedX - plot.left) / (plot.right - plot.left)) * rangeX;
                  const nearest = item.values.reduce((closest, metric) =>
                    Math.abs(metricXValue(metric) - valueAtPointer) < Math.abs(metricXValue(closest) - valueAtPointer)
                      ? metric
                      : closest
                  );
                  setHoveredPoint({ seriesKey: item.key, metric: nearest });
                }}
              />
            </g>
          ))}
          {hoveredSeries && hoveredMetric ? (
            <g className="chart-tooltip" transform={`translate(${tooltipX} ${tooltipY})`} pointerEvents="none">
              <rect width={tooltipWidth} height={tooltipHeight} rx="9" />
              <circle cx="13" cy="15" r="4" fill={hoveredSeries.color} />
              <text x="23" y="19" className="chart-tooltip__title">{metricSeriesLabel(hoveredSeries.key)}</text>
              <text x="13" y="40">{xAxisLabel}: {formatAxisNumber(metricXValue(hoveredMetric))}</text>
              <text x="13" y="56">Wert: {formatMetric(hoveredMetric.metric_value)}</text>
            </g>
          ) : null}
          {!visibleSeries.length ? (
            <text x={(plot.left + plot.right) / 2} y={(plot.top + plot.bottom) / 2} textAnchor="middle" className="chart-empty-label">
              Alle Linien sind ausgeblendet.
            </text>
          ) : null}
        </svg>
      </div>
      <details className="advanced-section"><summary>Zugängliche Datentabelle ({metrics.length} Messpunkte)</summary><table className="data-table"><thead><tr><th>Phase</th><th>Metrik</th><th>Epoche</th><th>Schritt</th><th>Wert</th></tr></thead><tbody>{metrics.map((metric) => <tr key={metric.metric_id}><td>{metric.phase}</td><td>{metricLabel(metric.metric_name)}</td><td>{metric.epoch_no}</td><td>{metric.step_no}</td><td>{formatMetric(metric.metric_value)}</td></tr>)}</tbody></table></details>
    </section>
  );
}

function metricXValue(metric: TrainingMetricRecord) {
  return metric.epoch_no || metric.step_no;
}

function metricSeriesLabel(key: string) {
  const [phase, ...metricParts] = key.split(':');
  return `${metricLabel(phase)} · ${metricLabel(metricParts.join(':'))}`;
}

function formatAxisNumber(value: number) {
  if (value === 0) return '0';
  const absolute = Math.abs(value);
  if (absolute >= 1000) return value.toFixed(0);
  if (absolute >= 10) return value.toFixed(1).replace(/\.0$/, '');
  if (absolute >= 1) return value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
  return value.toPrecision(3).replace(/0+$/, '').replace(/\.$/, '');
}

function segmentationModeLabel(mode: 'instance' | 'semantic') {
  return mode === 'instance' ? 'Instanz-Segmentierung' : 'Semantische Segmentierung';
}

function projectModelClassLabel(model: ProjectModelSummary, availableClasses: string[]) {
  if (model.uses_all_classes ?? !(model.class_names?.length)) {
    return availableClasses.length
      ? `Alle ${availableClasses.length} Projektklassen`
      : 'Alle Projektklassen';
  }
  return model.class_names?.join(', ') || 'Keine Klassen ausgewählt';
}

function trainingStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: 'In Warteschlange',
    waiting_for_gpu: 'Wartet auf GPU',
    preparing_data: 'Daten werden vorbereitet',
    validating_snapshot: 'Snapshot wird validiert',
    initializing: 'Training wird initialisiert',
    training: 'Training läuft',
    checkpointing: 'Checkpoint wird gespeichert',
    evaluating: 'Wird evaluiert',
    exporting: 'Wird exportiert',
    registering: 'Wird registriert',
    resuming: 'Training wird fortgesetzt',
    cancel_requested: 'Abbruch angefordert',
    completed: 'Abgeschlossen',
    failed: 'Fehlgeschlagen',
    cancelled: 'Abgebrochen',
    rolled_back_if_needed: 'Zurückgesetzt'
  };
  return labels[status] ?? status.replaceAll('_', ' ');
}

function stageTone(state: ModelLifecycleStageState) {
  if (state === 'success') return 'success';
  if (state === 'running' || state === 'warning' || state === 'pending') return 'warn';
  if (state === 'failed' || state === 'blocked') return 'danger';
  return 'neutral';
}

function stageStateLabel(state: ModelLifecycleStageState) {
  return ({ pending: 'Ausstehend', running: 'Läuft', success: 'Erfolgreich', warning: 'Prüfen', failed: 'Fehlgeschlagen', blocked: 'Blockiert', not_applicable: 'Nicht erforderlich' } as const)[state];
}

function formatMetric(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return Math.abs(value) >= 100 ? value.toFixed(1) : value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
}

function formatParameterNumber(value: number | null | undefined) {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  return new Intl.NumberFormat('de-AT', { maximumSignificantDigits: 8 }).format(value);
}

function metricLabel(value: string) {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase());
}

function architectureKey(architecture: { family: string; version: string; size: string }) {
  return `${architecture.family}:${architecture.version}:${architecture.size}`;
}

function commaList(value: string) {
  return value.split(',').map((item) => item.trim()).filter(Boolean);
}

function formatBytes(value: number) {
  if (!value) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[index]}`;
}

function inferContentType(fileName: string) {
  const extension = fileName.toLowerCase().split('.').pop();
  if (extension === 'png') return 'image/png';
  if (extension === 'bmp') return 'image/bmp';
  if (extension === 'webp') return 'image/webp';
  return 'image/jpeg';
}
