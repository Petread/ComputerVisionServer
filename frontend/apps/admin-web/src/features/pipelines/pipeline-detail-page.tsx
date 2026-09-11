import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  CleanupExecutionRequest,
  CleanupExecutionResult,
  ClassBalanceStrategy,
  LabelProjectMappingRequest,
  LabelProjectSummary,
  ModelTemplateSummary,
  PromotionCriteriaPolicy,
  ProjectAutomationPolicy,
  ProjectModelCreateRequest,
  ProjectPipelineDetail,
  ProjectTrainRequest,
  TaskType,
  TrainingJobStatus,
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { confirmConsequentialAction, describeError } from '../../app/errors';
import { CleanupDialog } from '../../components/cleanup-dialog';
import { useNotifications } from '../../components/notification-provider';
import {
  architectureKeyFromSummary,
  architectureLabel,
  architectureSecondaryLabel,
  datasetAwareTrainingRecommendation,
  defaultTemplateForTask,
  formatTrainingNumber,
  promotionPolicySummary,
  recommendedTemplateForSelection,
  splitRatioLabel,
  supportedArchitecturesForTask,
  taskTypeLabel,
  templateDefaultsSummary,
  templateOptionLabel,
  trainingDefaultsSummary,
  weightDecayFromDefaults
} from './modeling';
import {
  endpointAvailabilityLabel,
  formatDate,
  snapshotLabel,
  snapshotTone,
  trainingTone
} from './presentation';
import { getTrainActionState } from './workflow';

const emptyModelDraft: ProjectModelCreateRequest = {
  display_name: '',
  task_type: 'classification',
  model_template_key: 'classification-resnet-18',
  architecture_family: 'resnet',
  architecture_version: 'v1',
  architecture_size: '18',
  description: ''
};

type UploadMode = 'raw' | 'classified';

function isTerminalTrainingStatus(status: TrainingJobStatus) {
  return ['completed', 'failed', 'cancelled', 'rolled_back_if_needed'].includes(status);
}

function mappingRequestFromProject(project: LabelProjectSummary): LabelProjectMappingRequest {
  return {
    dataset_id: project.dataset_id ?? '',
    label_studio_project_id: project.label_studio_project_id,
    title: project.title,
    task_type: project.task_type,
    training_pipeline_slug: project.training_pipeline_slug ?? project.title.toLowerCase().replace(/[^a-z0-9]+/g, '-'),
    auto_snapshot_enabled: project.auto_snapshot_enabled,
    minimum_annotation_count: project.minimum_annotation_count,
    required_classes: project.required_classes,
    qualifying_actions: project.qualifying_actions,
    label_config: project.label_config ?? {},
    train_ratio: project.train_ratio,
    val_ratio: project.val_ratio,
    test_ratio: project.test_ratio,
    class_balance_strategy: project.class_balance_strategy
  };
}

function commaList(value: string) {
  return value
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function classBalanceStrategyLabel(strategy: ClassBalanceStrategy) {
  if (strategy === 'weighted_loss') {
    return 'weighted loss';
  }
  if (strategy === 'balanced_sampler') {
    return 'balanced sampler';
  }
  return 'none';
}

export function PipelineDetailPage() {
  const { labelProjectId } = useParams({ from: '/authenticated/pipelines/$labelProjectId' });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [selectedTrainingId, setSelectedTrainingId] = useState<string | null>(null);
  const [selectedModelId, setSelectedModelId] = useState<string | null>(null);
  const [createModelOpen, setCreateModelOpen] = useState(false);
  const [createModelIntent, setCreateModelIntent] = useState<'create' | 'train'>('create');
  const [cleanupOpen, setCleanupOpen] = useState(false);
  const [deleteEntirePipeline, setDeleteEntirePipeline] = useState(false);
  const [policyDraft, setPolicyDraft] = useState<ProjectAutomationPolicy | null>(null);
  const [mappingDraft, setMappingDraft] = useState<LabelProjectMappingRequest | null>(null);
  const [mappingDraftProjectId, setMappingDraftProjectId] = useState<string | null>(null);
  const [mappingLabelConfigJson, setMappingLabelConfigJson] = useState('{}');
  const [modelDraft, setModelDraft] = useState<ProjectModelCreateRequest>(emptyModelDraft);
  const [uploadMode, setUploadMode] = useState<UploadMode>('raw');
  const [pendingUploads, setPendingUploads] = useState<File[]>([]);
  const [classifiedUploadLabel, setClassifiedUploadLabel] = useState('');
  const [classifiedFolderName, setClassifiedFolderName] = useState('');
  const [activeUploadSessionId, setActiveUploadSessionId] = useState<string | null>(null);
  const [uploadedParts, setUploadedParts] = useState(0);
  const [uploadPhase, setUploadPhase] = useState<
    'idle' | 'creating_session' | 'uploading' | 'finalizing'
  >('idle');

  const pipelineQuery = useQuery({
    queryKey: ['project-pipeline', labelProjectId],
    queryFn: () => api.pipelines.detail(labelProjectId),
    refetchInterval: 5000
  });

  const datasetsQuery = useQuery({
    queryKey: ['datasets'],
    queryFn: api.datasets.list
  });

  const labelProjectsQuery = useQuery({
    queryKey: ['label-projects'],
    queryFn: api.datasets.projects,
    refetchInterval: 15000
  });
  const allModelTemplatesQuery = useQuery({
    queryKey: ['model-templates', 'all'],
    queryFn: () => api.pipelines.modelTemplates()
  });
  const allModelCapabilitiesQuery = useQuery({
    queryKey: ['model-capabilities', 'all'],
    queryFn: () => api.pipelines.modelCapabilities()
  });

  const selectedTrainingQuery = useQuery({
    queryKey: ['training-job', selectedTrainingId],
    queryFn: () => api.training.detail(selectedTrainingId!),
    enabled: Boolean(selectedTrainingId),
    refetchInterval: 2000
  });

  const uploadSessionQuery = useQuery({
    queryKey: ['upload-session', activeUploadSessionId],
    queryFn: () => api.datasets.uploadSession(activeUploadSessionId as string),
    enabled: Boolean(activeUploadSessionId),
    refetchInterval: uploadPhase === 'finalizing' ? 1000 : false
  });

  const cleanupPreviewQuery = useQuery({
    queryKey: ['pipeline-cleanup-preview', labelProjectId, deleteEntirePipeline],
    queryFn: () => api.pipelines.cleanupPreview(labelProjectId, { deleteEntirePipeline }),
    enabled: cleanupOpen
  });

  const currentLabelProject = useMemo(
    () => labelProjectsQuery.data?.items.find((item) => item.label_project_id === labelProjectId) ?? null,
    [labelProjectId, labelProjectsQuery.data?.items]
  );
  const declaredUploadLabels = useMemo(
    () => extractDeclaredLabels(currentLabelProject?.label_config),
    [currentLabelProject?.label_config]
  );

  useEffect(() => {
    if (!cleanupOpen) {
      setDeleteEntirePipeline(false);
    }
  }, [cleanupOpen]);

  useEffect(() => {
    setUploadMode('raw');
    setPendingUploads([]);
    setClassifiedUploadLabel('');
    setClassifiedFolderName('');
    setActiveUploadSessionId(null);
    setUploadedParts(0);
    setUploadPhase('idle');
  }, [labelProjectId]);

  useEffect(() => {
    if (pipelineQuery.data?.task_type !== 'classification' && uploadMode === 'classified') {
      setUploadMode('raw');
    }
  }, [pipelineQuery.data?.task_type, uploadMode]);

  useEffect(() => {
    if (pipelineQuery.data && !policyDraft) {
      setPolicyDraft(pipelineQuery.data.automation_policy);
    }
  }, [pipelineQuery.data, policyDraft]);

  useEffect(() => {
    if (!currentLabelProject) {
      return;
    }
    if (mappingDraft && mappingDraftProjectId === currentLabelProject.label_project_id) {
      return;
    }
    const nextDraft = mappingRequestFromProject(currentLabelProject);
    setMappingDraft(nextDraft);
    setMappingDraftProjectId(currentLabelProject.label_project_id);
    setMappingLabelConfigJson(JSON.stringify(nextDraft.label_config, null, 2));
  }, [currentLabelProject, mappingDraft, mappingDraftProjectId]);

  useEffect(() => {
    if (!pipelineQuery.data) {
      return;
    }
    if (!selectedTrainingId && pipelineQuery.data.last_training?.training_job_id) {
      setSelectedTrainingId(pipelineQuery.data.last_training.training_job_id);
    }
    if (!selectedModelId && pipelineQuery.data.models[0]) {
      setSelectedModelId(pipelineQuery.data.models[0].model_family_id);
    }
    if (!createModelOpen) {
      setModelDraft((current) => initializeModelDraft(pipelineQuery.data, current));
    }
  }, [createModelOpen, pipelineQuery.data, selectedModelId, selectedTrainingId]);

  const invalidatePipeline = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['project-pipeline', labelProjectId] }),
      queryClient.invalidateQueries({ queryKey: ['project-pipelines'] }),
      queryClient.invalidateQueries({ queryKey: ['training-jobs'] }),
      queryClient.invalidateQueries({ queryKey: ['model-versions'] }),
      queryClient.invalidateQueries({ queryKey: ['model-families'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-releases'] }),
      queryClient.invalidateQueries({ queryKey: ['deployment-targets'] }),
      queryClient.invalidateQueries({ queryKey: ['all-snapshots'] }),
      queryClient.invalidateQueries({ queryKey: ['datasets'] }),
      queryClient.invalidateQueries({ queryKey: ['label-projects'] })
    ]);
  };

  const syncMutation = useMutation({
    mutationFn: () => api.pipelines.sync(labelProjectId),
    onSuccess: async () => {
      await invalidatePipeline();
      notify({ title: 'Pipeline sync queued', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Pipeline sync failed'), tone: 'error' });
    }
  });

  const closeCreateModel = () => {
    setCreateModelOpen(false);
    setCreateModelIntent('create');
  };

  const policyMutation = useMutation({
    mutationFn: (payload: ProjectAutomationPolicy) => api.pipelines.updatePolicy(labelProjectId, payload),
    onSuccess: async (item) => {
      setPolicyDraft(item);
      await invalidatePipeline();
      await queryClient.invalidateQueries({ queryKey: ['label-projects'] });
      notify({ title: 'Automation policy updated', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Automation policy update failed'), tone: 'error' });
    }
  });

  const mappingMutation = useMutation({
    mutationFn: (payload: LabelProjectMappingRequest) => api.datasets.mapProject(payload),
    onSuccess: async (item) => {
      const nextDraft = mappingRequestFromProject(item);
      setMappingDraft(nextDraft);
      setMappingDraftProjectId(item.label_project_id);
      setMappingLabelConfigJson(JSON.stringify(nextDraft.label_config, null, 2));
      await Promise.all([
        invalidatePipeline(),
        queryClient.invalidateQueries({ queryKey: ['label-projects'] }),
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
        queryClient.invalidateQueries({ queryKey: ['all-snapshots'] })
      ]);
      notify({ title: 'Project mapping updated', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Project mapping update failed'), tone: 'error' });
    }
  });

  const uploadMutation = useMutation({
    mutationFn: async () => {
      const pipeline = pipelineQuery.data;
      if (!pipeline?.dataset_id) {
        throw new Error('This pipeline is not mapped to a dataset yet.');
      }
      if (!pendingUploads.length) {
        throw new Error('Choose one or more image files for this upload session.');
      }
      if (uploadMode === 'classified') {
        if (pipeline.task_type !== 'classification') {
          throw new Error('Pre-classified folder uploads require a classification pipeline.');
        }
        if (!classifiedUploadLabel.trim()) {
          throw new Error('Enter the classification label for this folder.');
        }
      }

      setUploadedParts(0);
      setUploadPhase('creating_session');
      const session = await api.datasets.createUploadSession(pipeline.dataset_id, {
        expected_file_count: pendingUploads.length,
        target_label_project_id: uploadMode === 'classified' ? labelProjectId : undefined,
        classification_label: uploadMode === 'classified' ? classifiedUploadLabel.trim() : undefined,
        source_folder_name:
          uploadMode === 'classified'
            ? classifiedFolderName || classifiedUploadLabel.trim()
            : undefined
      });
      setActiveUploadSessionId(session.upload_session_id);
      setUploadPhase('uploading');

      for (const [index, file] of pendingUploads.entries()) {
        await api.datasets.uploadPart(
          session.upload_session_id,
          index + 1,
          file.name,
          file,
          file.type || inferUploadContentType(file.name)
        );
        setUploadedParts(index + 1);
      }

      setUploadPhase('finalizing');
      const accepted = await api.datasets.completeUploadSession(session.upload_session_id, {});
      return { accepted, sessionId: session.upload_session_id, fileCount: pendingUploads.length };
    },
    onSuccess: async ({ accepted, fileCount, sessionId }) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['upload-session', sessionId] }),
        queryClient.invalidateQueries({ queryKey: ['project-pipeline', labelProjectId] }),
        queryClient.invalidateQueries({ queryKey: ['project-pipelines'] }),
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
        queryClient.invalidateQueries({ queryKey: ['label-projects'] })
      ]);
      notify({
        title: 'Upload session finalized into the dataset queue',
        description: `Session ${sessionId} uploaded ${fileCount} files and is now finishing asynchronously as job ${accepted.job_id}.`,
        tone: 'success'
      });
    },
    onError: (error) => {
      setUploadPhase('idle');
      notify({ ...describeError(error, 'Dataset upload failed'), tone: 'error' });
    }
  });

  useEffect(() => {
    const session = uploadSessionQuery.data;
    if (!session) {
      return;
    }
    if (session.status === 'completed' && uploadPhase === 'finalizing') {
      setUploadPhase('idle');
      setPendingUploads([]);
      notify({
        title: 'Dataset assets are ready',
        description: `${session.received_file_count} files were materialized into immutable dataset storage.`,
        tone: 'success'
      });
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['project-pipeline', labelProjectId] }),
        queryClient.invalidateQueries({ queryKey: ['project-pipelines'] }),
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
        queryClient.invalidateQueries({ queryKey: ['label-projects'] }),
        queryClient.invalidateQueries({ queryKey: ['all-snapshots'] })
      ]);
      return;
    }
    if (session.status === 'failed' && uploadPhase === 'finalizing') {
      setUploadPhase('idle');
      notify({
        title: 'Upload finalization failed',
        description: `Session ${session.upload_session_id} needs operator review before the dataset can be used.`,
        tone: 'error'
      });
    }
  }, [labelProjectId, notify, queryClient, uploadPhase, uploadSessionQuery.data]);

  const trainMutation = useMutation({
    mutationFn: (payload: ProjectTrainRequest) => api.pipelines.train(labelProjectId, payload),
    onSuccess: async (accepted) => {
      setSelectedTrainingId(accepted.job_id);
      await invalidatePipeline();
      notify({ title: 'Training queued', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Training could not be started'), tone: 'error' });
    }
  });

  const cancelTrainingMutation = useMutation({
    mutationFn: (payload: { trainingJobId: string; force?: boolean }) =>
      api.training.cancel(payload.trainingJobId, {
        reason: payload.force
          ? 'Operator forced cancellation from the project pipeline because the run is stale or already waiting to stop.'
          : 'Operator requested cancellation from the project pipeline.',
        force: payload.force ?? false
      }),
    onSuccess: async () => {
      await invalidatePipeline();
      if (selectedTrainingId) {
        await queryClient.invalidateQueries({ queryKey: ['training-job', selectedTrainingId] });
      }
      notify({ title: 'Training cancellation updated', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Training cancellation failed'), tone: 'error' });
    }
  });

  const createModelMutation = useMutation({
    mutationFn: (payload: ProjectModelCreateRequest) => api.pipelines.createModel(labelProjectId, payload),
    onSuccess: async (item) => {
      const modelTypeChanged = item.task_type !== pipelineQuery.data?.task_type;
      const shouldTrainAfterCreate =
        createModelIntent === 'train' &&
        Boolean(pipelineQuery.data?.latest_valid_snapshot) &&
        !modelTypeChanged;
      closeCreateModel();
      setSelectedModelId(item.model_family_id);
      await invalidatePipeline();
      if (shouldTrainAfterCreate) {
        trainMutation.mutate({
          model_family_id: item.model_family_id,
          model_template_key: item.template_key ?? undefined,
          allow_duplicate_snapshot_training: false
        });
        return;
      }
      notify({
        title: modelTypeChanged ? 'Model created and project type updated' : 'Model created',
        description: modelTypeChanged
          ? 'Create a fresh dataset snapshot before starting training for the new model type.'
          : item.display_name,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Model creation failed'), tone: 'error' });
    }
  });

  const retrainMutation = useMutation({
    mutationFn: () => api.pipelines.retrainLatest(labelProjectId),
    onSuccess: async (accepted) => {
      setSelectedTrainingId(accepted.job_id);
      await invalidatePipeline();
      notify({ title: 'Retraining queued', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Retraining could not be started'), tone: 'error' });
    }
  });

  const deployMutation = useMutation({
    mutationFn: () => api.pipelines.deployLatest(labelProjectId),
    onSuccess: async () => {
      await invalidatePipeline();
      notify({ title: 'Deployment orchestration queued', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Deployment could not be started'), tone: 'error' });
    }
  });

  const forceDeployMutation = useMutation({
    mutationFn: () =>
      api.pipelines.deployLatest(labelProjectId, {
        allow_quality_gate_bypass: true
      }),
    onSuccess: async () => {
      await invalidatePipeline();
      notify({
        title: 'Advanced test deployment queued',
        description: 'Quality gates were bypassed intentionally for this dev/admin deployment.',
        tone: 'info'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Advanced deployment could not be started'), tone: 'error' });
    }
  });

  const linkPredictionsMutation = useMutation({
    mutationFn: () => api.pipelines.linkLabelStudioPredictions(labelProjectId),
    onSuccess: async (summary) => {
      await invalidatePipeline();
      notify({
        title: summary.enabled ? 'Deployed model connected to Label Studio' : 'Model connection refreshed',
        description: summary.model_version_no
          ? `Label Studio will request predictions from deployed model version ${summary.model_version_no}.`
          : undefined,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'The deployed model could not be connected to Label Studio'), tone: 'error' });
    }
  });

  const cleanupMutation = useMutation({
    mutationFn: (payload: CleanupExecutionRequest) => api.pipelines.cleanup(labelProjectId, payload),
    onSuccess: async (result: CleanupExecutionResult, payload) => {
      await invalidatePipeline();
      notify({
        title: payload.delete_entire_pipeline
          ? 'Pipeline deleted'
          : result.already_deleted
            ? 'Pipeline already cleaned up'
            : 'Old pipeline data cleaned up',
        description: `${result.deleted_entities.reduce((sum, item) => sum + item.count, 0)} records affected.`,
        tone: 'success'
      });
      setCleanupOpen(false);
      if (payload.delete_entire_pipeline) {
        await navigate({ to: '/pipelines' });
      }
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Pipeline cleanup failed'), tone: 'error' });
    }
  });

  const selectedModel = useMemo(
    () => pipelineQuery.data?.models.find((item) => item.model_family_id === selectedModelId) ?? pipelineQuery.data?.models[0],
    [pipelineQuery.data, selectedModelId]
  );

  const liveEndpoint = pipelineQuery.data?.current_endpoints.find(
    (endpoint) => endpoint.realtime_available || endpoint.batch_available
  );

  if (!pipelineQuery.data) {
    return (
      <div className="panel-stack panel-stack--wide">
        <Panel title="Project Pipeline">
          <p className="subtle-text">Loading pipeline state...</p>
        </Panel>
      </div>
    );
  }

  const pipeline = pipelineQuery.data;
  const predictionBackend = pipeline.prediction_backend;
  const predictionRealtimeEndpoint = pipeline.current_endpoints.find((endpoint) => endpoint.realtime_available);
  const canLinkLabelStudioPredictions = Boolean(predictionRealtimeEndpoint);
  const latestTrainingFailedQualityGate =
    pipeline.last_training?.status === 'completed' && !pipeline.last_training.promotion_ready;
  const trainAction = getTrainActionState({
    hasValidSnapshot: Boolean(pipeline.latest_valid_snapshot),
    hasSelectedModel: Boolean(selectedModel)
  });
  const allModelTemplates = allModelTemplatesQuery.data?.items ?? pipeline.model_templates;
  const allSupportedModelArchitectures =
    allModelCapabilitiesQuery.data?.items ?? pipeline.supported_model_architectures;
  const modelTaskTypeOptions = modelCreationTaskOptions(pipeline);
  const rawModelDraftTaskType = modelDraft.task_type ?? pipeline.task_type;
  const modelDraftTaskType = modelTaskTypeOptions.includes(rawModelDraftTaskType)
    ? rawModelDraftTaskType
    : modelTaskTypeOptions[0] ?? pipeline.task_type;
  const modelTypeChanged = modelDraftTaskType !== pipeline.task_type;
  const pipelineSupportedArchitectures = supportedArchitecturesForTask(
    pipeline.supported_model_architectures,
    pipeline.task_type
  );
  const creationSupportedArchitectures = supportedArchitecturesForTask(
    allSupportedModelArchitectures,
    modelDraftTaskType
  );
  const pipelineTaskTemplates = pipeline.model_templates.filter((template) => template.task_type === pipeline.task_type);
  const policySelectableTemplates = pipelineTaskTemplates.filter((template) => template.active);
  const creationTaskTemplates = allModelTemplates.filter((template) => template.task_type === modelDraftTaskType);
  const selectableTemplates = creationTaskTemplates.filter((template) => template.active);
  const selectedTemplate =
    selectableTemplates.find((template) => template.template_key === modelDraft.model_template_key) ??
    selectableTemplates.find((template) => template.recommended) ??
    selectableTemplates[0] ??
    null;
  const modelDraftArchitectureKey = `${modelDraft.architecture_family}:${modelDraft.architecture_version}:${modelDraft.architecture_size}`;
  const creationArchitectureKeys = new Set(
    creationSupportedArchitectures.map((item) => architectureKeyFromSummary(item.architecture))
  );
  const creationArchitectureValue = creationArchitectureKeys.has(modelDraftArchitectureKey)
    ? modelDraftArchitectureKey
    : selectedTemplate
      ? architectureKeyFromSummary(selectedTemplate.architecture)
      : modelDraftArchitectureKey;
  const selectedModelTemplate = selectedModel?.template_key
    ? allModelTemplates.find((template) => template.template_key === selectedModel.template_key) ?? null
    : null;
  const policySelectedTemplate =
    policySelectableTemplates.find((template) => template.template_key === policyDraft?.default_model_template) ??
    policySelectableTemplates.find((template) => template.recommended) ??
    policySelectableTemplates[0] ??
    null;
  const selectedTraining = selectedTrainingQuery.data;
  const selectedTrainingSummary = selectedTrainingId
    ? pipeline.training_history.find((job) => job.training_job_id === selectedTrainingId)
    : undefined;
  const selectedTrainingForAction = selectedTraining ?? selectedTrainingSummary;
  const selectedTrainingActionStatus = selectedTrainingForAction?.status;
  const canCancelSelectedTraining = Boolean(
    selectedTrainingId &&
      (!selectedTrainingActionStatus || !isTerminalTrainingStatus(selectedTrainingActionStatus))
  );
  const shouldForceCancelSelectedTraining =
    !selectedTraining || selectedTrainingActionStatus === 'cancel_requested';
  const recommendationTemplate = selectedModelTemplate ?? policySelectedTemplate;
  const recommendationCapability = recommendationTemplate
    ? pipelineSupportedArchitectures.find(
        (item) =>
          architectureKeyFromSummary(item.architecture) ===
          architectureKeyFromSummary(recommendationTemplate.architecture)
      ) ?? null
    : null;
  const trainingRecommendation = datasetAwareTrainingRecommendation({
    taskType: pipeline.task_type,
    readiness: pipeline.readiness,
    template: recommendationTemplate,
    capability: recommendationCapability
  });
  const recommendedWeightDecay = weightDecayFromDefaults(trainingRecommendation?.recommendedDefaults);
  const mappingSplitTotal = mappingDraft
    ? Number(mappingDraft.train_ratio) + Number(mappingDraft.val_ratio) + Number(mappingDraft.test_ratio)
    : 0;
  const currentClassBalanceStrategy = mappingDraft?.class_balance_strategy ?? 'none';
  const recommendedClassBalanceStrategy = trainingRecommendation?.classBalanceStrategy ?? 'none';
  const canApplyRecommendedSplit = Boolean(
    mappingDraft &&
      trainingRecommendation &&
      splitRatioLabel(trainingRecommendation.split) !==
        splitRatioLabel({
          train_ratio: mappingDraft.train_ratio,
          val_ratio: mappingDraft.val_ratio,
          test_ratio: mappingDraft.test_ratio
        })
  );
  const canApplyRecommendedClassBalance = Boolean(
    mappingDraft &&
      trainingRecommendation &&
      currentClassBalanceStrategy !== recommendedClassBalanceStrategy
  );
  const selectedModelTemplateSearch = {
    templateKey: selectedModel?.template_key ?? undefined,
    returnToLabelProjectId: labelProjectId
  };

  const saveMappingDraft = (patch: Partial<LabelProjectMappingRequest> = {}) => {
    if (!mappingDraft) {
      return;
    }
    let parsedLabelConfig: Record<string, unknown>;
    try {
      parsedLabelConfig = JSON.parse(mappingLabelConfigJson) as Record<string, unknown>;
    } catch {
      notify({ title: 'Label config JSON is invalid', tone: 'error' });
      return;
    }
    mappingMutation.mutate({
      ...mappingDraft,
      ...patch,
      auto_snapshot_enabled: policyDraft?.auto_snapshot_enabled ?? mappingDraft.auto_snapshot_enabled,
      label_config: parsedLabelConfig
    });
  };

  const handleTrainAction = () => {
    if (trainAction.kind === 'wait-for-dataset') {
      notify({
        title: 'Dataset not ready yet',
        description: 'Wait for the latest valid dataset snapshot, then training can start from this page.',
        tone: 'info'
      });
      return;
    }
    if (trainAction.kind === 'create-model') {
      setCreateModelIntent('train');
      setCreateModelOpen(true);
      return;
    }
    trainMutation.mutate({
      model_family_id: selectedModel?.model_family_id,
      model_template_key: selectedModel?.template_key ?? undefined,
      allow_duplicate_snapshot_training: false
    });
  };

  return (
    <div className="panel-stack panel-stack--wide">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Project Pipeline</p>
          <h2>{pipeline.title}</h2>
          <p className="subtle-text">
            Follow the normal flow from dataset readiness to model creation, training, approval,
            deployment, and endpoint testing. Technical policy controls are still available below.
          </p>
        </div>
        <div className="training-summary-pills">
          <MetricPill label="Task" value={taskTypeLabel(pipeline.task_type)} tone="info" />
          <MetricPill label="Eligible submitted" value={String(pipeline.readiness.eligible_task_count)} tone="success" />
          <MetricPill label="Models" value={String(pipeline.models.length)} />
          <MetricPill
            label="Endpoint"
            value={liveEndpoint?.endpoint_name ?? 'Not deployed'}
            tone={liveEndpoint ? 'success' : 'neutral'}
          />
        </div>
      </section>

      <Panel
        title="Operator Flow"
        action={
          <div className="actions-row">
            <button type="button" onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
              {syncMutation.isPending ? 'Syncing...' : 'Sync now'}
            </button>
            <button
              type="button"
              onClick={() => {
                setCreateModelIntent('create');
                setCreateModelOpen(true);
              }}
            >
              Create model
            </button>
            <button
              type="button"
              className="button--primary"
              disabled={trainMutation.isPending || createModelMutation.isPending}
              onClick={handleTrainAction}
            >
              {trainMutation.isPending ? 'Queueing training...' : trainAction.label}
            </button>
            {liveEndpoint ? (
              <Link to="/endpoints/$modelId" params={{ modelId: liveEndpoint.endpoint_name }} className="primary-link">
                Open endpoint
              </Link>
            ) : null}
          </div>
        }
      >
        <div className="detail-grid">
          <div className="metric-pill">
            <span>Recommended next action</span>
            <strong>{pipeline.next_recommended_action}</strong>
            <small className="subtle-text">{pipeline.next_action_detail ?? 'No additional detail.'}</small>
          </div>
          <div className="metric-pill">
            <span>Readiness</span>
            <strong>{pipeline.readiness.readiness_percent.toFixed(0)}%</strong>
            <small className="subtle-text">{pipeline.readiness.readiness_reason}</small>
          </div>
          <div className="metric-pill">
            <span>Dataset policy</span>
            <strong>Finalized items only</strong>
            <small className="subtle-text">
              Draft items are ignored. Submitted unlabeled items remain valid background samples.
            </small>
          </div>
          <div className="metric-pill">
            <span>Current deployment</span>
            <strong>{liveEndpoint ? endpointAvailabilityLabel(liveEndpoint) : 'Not deployed'}</strong>
            <small className="subtle-text">
              {liveEndpoint ? `Last deployed ${formatDate(liveEndpoint.last_deployed_at)}` : 'No endpoint published yet.'}
            </small>
          </div>
          <div className="metric-pill">
            <span>Label Studio model</span>
            <strong>{predictionBackend.enabled ? predictionBackend.status : 'Not linked'}</strong>
            <small className="subtle-text">
              {predictionBackend.enabled && predictionBackend.model_version_no
                ? `Connected to realtime deployment version ${predictionBackend.model_version_no}.`
                : 'Connect the deployed realtime model when Label Studio should request predictions.'}
            </small>
          </div>
        </div>
        {latestTrainingFailedQualityGate ? (
          <div className="inline-alert inline-alert--warn">
            <strong>Deployment blocked by quality gates</strong>
            <p className="subtle-text">
              The latest completed run is registered and packaged, but the standard deploy action stays blocked until
              promotion criteria pass. Use the testing override only for isolated dev/admin validation.
            </p>
          </div>
        ) : null}
        <details className="advanced-section">
          <summary>Secondary and advanced actions</summary>
          <div className="actions-row">
            <button
              type="button"
              disabled={!selectedModel || !pipeline.latest_valid_snapshot || retrainMutation.isPending}
              onClick={() => retrainMutation.mutate()}
            >
              {retrainMutation.isPending ? 'Queueing retrain...' : 'Retrain latest'}
            </button>
            <button
              type="button"
              disabled={!pipeline.models.length || deployMutation.isPending || forceDeployMutation.isPending}
              onClick={() => deployMutation.mutate()}
            >
              {deployMutation.isPending ? 'Queueing deploy...' : 'Deploy latest approved'}
            </button>
            <button
              type="button"
              disabled={!canLinkLabelStudioPredictions || linkPredictionsMutation.isPending}
              onClick={() => linkPredictionsMutation.mutate()}
            >
              {linkPredictionsMutation.isPending
                ? 'Connecting model...'
                : predictionBackend.enabled
                  ? 'Refresh Label Studio model'
                  : 'Connect deployed model to Label Studio'}
            </button>
            {latestTrainingFailedQualityGate ? (
              <button
                type="button"
                className="button--danger"
                disabled={deployMutation.isPending || forceDeployMutation.isPending}
                onClick={() => {
                  const confirmed = confirmConsequentialAction({
                    title: 'Force deploy latest model for testing?',
                    consequence:
                      'This bypasses the normal quality gates and should only be used for dev/admin testing.',
                    secondary:
                      'The current healthy deployment stays in place until the replacement release finishes staging and promotion.'
                  });
                  if (confirmed) {
                    forceDeployMutation.mutate();
                  }
                }}
              >
                {forceDeployMutation.isPending ? 'Queueing override...' : 'Force deploy for testing'}
              </button>
            ) : null}
            <button type="button" className="button--danger" onClick={() => setCleanupOpen(true)}>
              Cleanup old runs
            </button>
          </div>
        </details>
      </Panel>

      <Panel title="Dataset Upload">
        <div className="panel-stack">
          <div className="training-form-grid">
            <label>
              <span>Dataset</span>
              <input value={pipeline.dataset_name ?? pipeline.dataset_id ?? 'No dataset mapping'} disabled />
            </label>
            <label>
              <span>Upload Mode</span>
              <select
                value={uploadMode}
                disabled={!pipeline.dataset_id}
                onChange={(event) => {
                  setUploadMode(event.target.value as UploadMode);
                  setPendingUploads([]);
                  setClassifiedFolderName('');
                  setUploadedParts(0);
                  setActiveUploadSessionId(null);
                }}
              >
                <option value="raw">unlabeled images</option>
                {pipeline.task_type === 'classification' ? (
                  <option value="classified">classified folder</option>
                ) : null}
              </select>
            </label>
            {uploadMode === 'classified' && pipeline.task_type === 'classification' ? (
              <>
                <label>
                  <span>Classification Project</span>
                  <input value={currentLabelProject?.title ?? pipeline.title} disabled />
                </label>
                <label>
                  <span>Folder Label</span>
                  <input
                    value={classifiedUploadLabel}
                    list="pipeline-classified-upload-labels"
                    onChange={(event) => setClassifiedUploadLabel(event.target.value)}
                    placeholder="Label name"
                  />
                  <datalist id="pipeline-classified-upload-labels">
                    {declaredUploadLabels.map((label) => (
                      <option key={label} value={label} />
                    ))}
                  </datalist>
                </label>
              </>
            ) : null}
            <label className="training-form-grid__full">
              <span>{uploadMode === 'classified' ? 'Image Folder' : 'Image Files'}</span>
              <input
                key={`${labelProjectId}-${uploadMode}`}
                type="file"
                accept="image/*"
                multiple
                disabled={!pipeline.dataset_id}
                {...(uploadMode === 'classified' ? { webkitdirectory: '', directory: '' } : {})}
                onChange={(event) => {
                  const files = Array.from(event.target.files ?? []).filter(isImageFile);
                  setPendingUploads(files);
                  const folderName = inferCommonFolderName(files);
                  setClassifiedFolderName(folderName ?? '');
                  if (uploadMode === 'classified' && folderName) {
                    setClassifiedUploadLabel(folderName);
                  }
                }}
              />
            </label>
          </div>

          <div className="actions-row">
            <button
              type="button"
              disabled={
                !pipeline.dataset_id ||
                !pendingUploads.length ||
                (uploadMode === 'classified' &&
                  (pipeline.task_type !== 'classification' || !classifiedUploadLabel.trim())) ||
                uploadMutation.isPending
              }
              onClick={() => uploadMutation.mutate()}
            >
              {uploadMode === 'classified' ? 'Upload Classified Folder' : 'Upload Files'}
            </button>
            <span className="subtle-text">
              {uploadPhase === 'creating_session'
                ? 'Creating upload session...'
                : uploadPhase === 'uploading'
                  ? `Uploading ${uploadedParts}/${pendingUploads.length} files...`
                  : uploadPhase === 'finalizing'
                    ? 'Finalizing dataset assets and syncing Label Studio...'
                    : pendingUploads.length
                      ? uploadMode === 'classified'
                        ? `${pendingUploads.length} files staged as ${classifiedUploadLabel || 'one class'}`
                        : `${pendingUploads.length} files staged for upload`
                      : pipeline.dataset_id
                        ? uploadMode === 'classified'
                          ? 'Choose a class folder to import pre-labeled images.'
                          : 'Choose images to upload into this pipeline dataset.'
                        : 'Map this pipeline to a dataset before uploading.'}
            </span>
          </div>

          {pendingUploads.length ? (
            <div className="file-chip-row">
              {pendingUploads.slice(0, 24).map((file) => (
                <span key={`${file.name}-${file.size}-${file.lastModified}`} className="badge badge--neutral">
                  {file.name}
                </span>
              ))}
              {pendingUploads.length > 24 ? (
                <span className="badge badge--neutral">+{pendingUploads.length - 24} more</span>
              ) : null}
            </div>
          ) : null}

          {activeUploadSessionId ? (
            <div className="detail-grid">
              <article className="status-card">
                <h3>Session</h3>
                <p className="subtle-text">{activeUploadSessionId}</p>
              </article>
              <article className="status-card">
                <h3>Status</h3>
                <p className="subtle-text">{uploadSessionQuery.data?.status ?? uploadPhase}</p>
              </article>
              <article className="status-card">
                <h3>Received Files</h3>
                <p className="subtle-text">
                  {uploadSessionQuery.data?.received_file_count ?? uploadedParts}/
                  {uploadSessionQuery.data?.expected_file_count ?? pendingUploads.length}
                </p>
              </article>
              <article className="status-card">
                <h3>Transferred Bytes</h3>
                <p className="subtle-text">
                  {formatBytes(uploadSessionQuery.data?.total_bytes ?? totalSelectedBytes(pendingUploads))}
                </p>
              </article>
            </div>
          ) : null}
        </div>
      </Panel>

      <div className="training-grid training-grid--details">
        <Panel title="Dataset Automation Status">
          <div className="panel-stack">
            <div className="detail-grid">
              <MetricPill
                label="Total uploaded"
                value={String(pipeline.readiness.task_count)}
                tone="neutral"
              />
              <MetricPill
                label="Draft / unsubmitted"
                value={String(pipeline.readiness.draft_task_count)}
                tone={pipeline.readiness.draft_task_count ? 'warn' : 'neutral'}
              />
              <MetricPill
                label="Submitted labeled"
                value={String(pipeline.readiness.submitted_labeled_task_count)}
                tone="success"
              />
              <MetricPill
                label="Background images"
                value={String(pipeline.readiness.submitted_background_task_count)}
                tone="info"
              />
              <MetricPill
                label="Invalid / excluded"
                value={String(pipeline.readiness.invalid_task_count)}
                tone={pipeline.readiness.invalid_task_count ? 'danger' : 'neutral'}
              />
            </div>

            <div className="detail-grid">
              <div className="metric-pill">
                <span>Latest dataset</span>
                <strong>{snapshotLabel(pipeline.latest_snapshot)}</strong>
                <small className={`badge badge--${snapshotTone(pipeline.latest_snapshot?.status)}`}>
                  {pipeline.latest_snapshot?.status ?? 'pending'}
                </small>
              </div>
              <div className="metric-pill">
                <span>Latest valid dataset</span>
                <strong>{snapshotLabel(pipeline.latest_valid_snapshot)}</strong>
                <small className={`badge badge--${snapshotTone(pipeline.latest_valid_snapshot?.status)}`}>
                  {pipeline.latest_valid_snapshot?.status ?? 'waiting'}
                </small>
              </div>
              <div className="metric-pill">
                <span>Eligible sample count</span>
                <strong>{pipeline.readiness.eligible_task_count}</strong>
                <small className="subtle-text">Training and export use only finalized eligible items.</small>
              </div>
            </div>

            {policyDraft ? (
              <section className="advanced-section policy-section">
                <div className="section-heading">
                  <h3>Automation & advanced policy</h3>
                </div>
                {trainingRecommendation ? (
                  <div className="training-recommendation">
                    <div className="section-heading">
                      <h4>Dataset-aware training recommendation</h4>
                      <span className="badge badge--info">{trainingRecommendation.profileLabel}</span>
                    </div>
                    <div className="pipeline-template-grid">
                      <div className="metric-pill">
                        <span>Dataset</span>
                        <strong>{trainingRecommendation.datasetLabel}</strong>
                        <small className="subtle-text">{trainingRecommendation.datasetDetail}</small>
                      </div>
                      <div className="metric-pill">
                        <span>Model preset</span>
                        <strong>{trainingRecommendation.modelLabel}</strong>
                        <small className="subtle-text">{trainingRecommendation.modelDetail}</small>
                      </div>
                      <div className="metric-pill">
                        <span>Initialization</span>
                        <strong>{trainingRecommendation.initializationLabel}</strong>
                        <small className="subtle-text">{trainingRecommendation.initializationDetail}</small>
                      </div>
                      <div className="metric-pill">
                        <span>Epochs</span>
                        <strong>{trainingRecommendation.recommendedDefaults.epochs}</strong>
                        <small className="subtle-text">
                          Template default: {recommendationTemplate?.defaults.training.epochs ?? 'n/a'}
                        </small>
                      </div>
                      <div className="metric-pill">
                        <span>Learning rate</span>
                        <strong>{formatTrainingNumber(trainingRecommendation.recommendedDefaults.learning_rate)}</strong>
                        <small className="subtle-text">
                          Template default:{' '}
                          {recommendationTemplate
                            ? formatTrainingNumber(recommendationTemplate.defaults.training.learning_rate)
                            : 'n/a'}
                        </small>
                      </div>
                      <div className="metric-pill">
                        <span>Batch size</span>
                        <strong>{trainingRecommendation.recommendedDefaults.batch_size ?? 'auto'}</strong>
                        <small className="subtle-text">
                          Mostly constrained by GPU memory, image size, and model size.
                        </small>
                      </div>
                      <div className="metric-pill">
                        <span>Weight decay</span>
                        <strong>{recommendedWeightDecay === null ? 'optimizer default' : formatTrainingNumber(recommendedWeightDecay)}</strong>
                        <small className="subtle-text">Raised slightly for small datasets when configured.</small>
                      </div>
                      <div className="metric-pill">
                        <span>Validation split</span>
                        <strong>{trainingRecommendation.split.val_ratio}%</strong>
                        <small className="subtle-text">
                          Train/val/test {splitRatioLabel(trainingRecommendation.split)}; current{' '}
                          {splitRatioLabel(trainingRecommendation.currentSplit)}
                        </small>
                      </div>
                      <div className="metric-pill">
                        <span>Class balance strategy</span>
                        <strong>{trainingRecommendation.classBalanceLabel}</strong>
                        <small className="subtle-text">
                          Current {classBalanceStrategyLabel(currentClassBalanceStrategy)};{' '}
                          {trainingRecommendation.classBalanceDetail}
                        </small>
                      </div>
                    </div>
                    {canApplyRecommendedSplit || canApplyRecommendedClassBalance ? (
                      <div className="actions-row">
                        {canApplyRecommendedSplit ? (
                          <button
                            type="button"
                            disabled={mappingMutation.isPending}
                            onClick={() =>
                              saveMappingDraft({
                                train_ratio: trainingRecommendation.split.train_ratio,
                                val_ratio: trainingRecommendation.split.val_ratio,
                                test_ratio: trainingRecommendation.split.test_ratio
                              })
                            }
                          >
                            {mappingMutation.isPending ? 'Applying...' : 'Apply recommended split'}
                          </button>
                        ) : null}
                        {canApplyRecommendedClassBalance ? (
                          <button
                            type="button"
                            disabled={mappingMutation.isPending}
                            onClick={() =>
                              saveMappingDraft({
                                class_balance_strategy: trainingRecommendation.classBalanceStrategy
                              })
                            }
                          >
                            {mappingMutation.isPending ? 'Applying...' : 'Apply class balance'}
                          </button>
                        ) : null}
                      </div>
                    ) : null}
                    {trainingRecommendation.warnings.length ? (
                      <div className="inline-alert inline-alert--warn">
                        <strong>Recommendation caveats</strong>
                        <ul className="training-recommendation__notes">
                          {trainingRecommendation.warnings.map((warning) => (
                            <li key={warning}>{warning}</li>
                          ))}
                        </ul>
                      </div>
                    ) : null}
                    <details className="advanced-section">
                      <summary>Recommendation rationale</summary>
                      <ul className="training-recommendation__notes">
                        {trainingRecommendation.notes.map((note) => (
                          <li key={note}>{note}</li>
                        ))}
                      </ul>
                    </details>
                  </div>
                ) : null}
                {mappingDraft ? (
                  <form
                    className="training-form-grid"
                    onSubmit={(event) => {
                      event.preventDefault();
                      saveMappingDraft();
                    }}
                  >
                    <label>
                      <span>Dataset target (dataset)</span>
                      <select
                        value={mappingDraft.dataset_id}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, dataset_id: event.target.value } : current
                          )
                        }
                      >
                        {!mappingDraft.dataset_id ? <option value="">Select dataset</option> : null}
                        {datasetsQuery.data?.items.map((dataset) => (
                          <option key={dataset.dataset_id} value={dataset.dataset_id}>
                            {dataset.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      <span>Label Studio project (id)</span>
                      <input value={mappingDraft.label_studio_project_id} disabled />
                    </label>
                    <label>
                      <span>Project title (name)</span>
                      <input
                        value={mappingDraft.title}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, title: event.target.value } : current
                          )
                        }
                      />
                    </label>
                    <label>
                      <span>Task type (task)</span>
                      <input value={taskTypeLabel(mappingDraft.task_type)} disabled />
                    </label>
                    <label>
                      <span>Pipeline slug (slug)</span>
                      <input
                        value={mappingDraft.training_pipeline_slug}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, training_pipeline_slug: event.target.value } : current
                          )
                        }
                      />
                    </label>
                    <label>
                      <span>Minimum annotations (items)</span>
                      <input
                        type="number"
                        min={0}
                        value={mappingDraft.minimum_annotation_count}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current
                              ? { ...current, minimum_annotation_count: Number(event.target.value) }
                              : current
                          )
                        }
                      />
                    </label>
                    <label>
                      <span>Required classes (labels)</span>
                      <input
                        value={mappingDraft.required_classes.join(', ')}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, required_classes: commaList(event.target.value) } : current
                          )
                        }
                      />
                    </label>
                    <label>
                      <span>Class balance strategy (mode)</span>
                      <select
                        value={mappingDraft.class_balance_strategy}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current
                              ? {
                                  ...current,
                                  class_balance_strategy: event.target.value as ClassBalanceStrategy
                                }
                              : current
                          )
                        }
                      >
                        <option value="none">none</option>
                        <option value="weighted_loss">weighted_loss</option>
                        <option value="balanced_sampler">balanced_sampler</option>
                      </select>
                    </label>
                    <label>
                      <span>Train split (%)</span>
                      <input
                        type="number"
                        min={0}
                        max={100}
                        value={mappingDraft.train_ratio}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, train_ratio: Number(event.target.value) } : current
                          )
                        }
                      />
                    </label>
                    <label>
                      <span>Validation split (%)</span>
                      <input
                        type="number"
                        min={0}
                        max={100}
                        value={mappingDraft.val_ratio}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, val_ratio: Number(event.target.value) } : current
                          )
                        }
                      />
                    </label>
                    <label>
                      <span>Test split (%)</span>
                      <input
                        type="number"
                        min={0}
                        max={100}
                        value={mappingDraft.test_ratio}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, test_ratio: Number(event.target.value) } : current
                          )
                        }
                      />
                    </label>
                    <label>
                      <span>Qualifying actions (events)</span>
                      <input
                        value={mappingDraft.qualifying_actions.join(', ')}
                        onChange={(event) =>
                          setMappingDraft((current) =>
                            current ? { ...current, qualifying_actions: commaList(event.target.value) } : current
                          )
                        }
                      />
                    </label>
                    <label className="training-form-grid__full">
                      <span>Label config (JSON)</span>
                      <textarea
                        className="text-area"
                        value={mappingLabelConfigJson}
                        onChange={(event) => setMappingLabelConfigJson(event.target.value)}
                      />
                    </label>
                    {mappingSplitTotal !== 100 ? (
                      <div className="inline-alert inline-alert--warn training-form-grid__full">
                        <strong>Split total is {mappingSplitTotal}%</strong>
                        <p className="subtle-text">Train, validation, and test split percentages must add up to 100%.</p>
                      </div>
                    ) : null}
                    <div className="actions-row training-form-grid__full">
                      <button
                        type="submit"
                        disabled={mappingMutation.isPending || !mappingDraft.dataset_id || mappingSplitTotal !== 100}
                      >
                        {mappingMutation.isPending ? 'Saving...' : 'Save project mapping'}
                      </button>
                    </div>
                  </form>
                ) : null}
                <form
                  className="training-form-grid"
                  onSubmit={(event) => {
                    event.preventDefault();
                    policyMutation.mutate(policyDraft);
                  }}
                >
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={policyDraft.auto_snapshot_enabled}
                      onChange={(event) =>
                        setPolicyDraft((current) =>
                          current ? { ...current, auto_snapshot_enabled: event.target.checked } : current
                        )
                      }
                    />
                    <span>Automatically snapshot after qualifying Label Studio activity</span>
                  </label>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={policyDraft.auto_deploy_enabled}
                      onChange={(event) =>
                        setPolicyDraft((current) =>
                          current ? { ...current, auto_deploy_enabled: event.target.checked } : current
                        )
                      }
                    />
                    <span>Automatically deploy after training passes the quality gates</span>
                  </label>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={policyDraft.auto_train_enabled}
                      onChange={(event) =>
                        setPolicyDraft((current) =>
                          current ? { ...current, auto_train_enabled: event.target.checked } : current
                        )
                      }
                    />
                    <span>Automatically train when a new valid dataset arrives</span>
                  </label>
                  <label>
                    <span>Default model template</span>
                    <select
                      value={policyDraft.default_model_template ?? policySelectedTemplate?.template_key ?? ''}
                      onChange={(event) =>
                        setPolicyDraft((current) =>
                          current ? { ...current, default_model_template: event.target.value } : current
                        )
                      }
                    >
                      {policySelectableTemplates.map((template) => (
                        <option key={template.template_key} value={template.template_key}>
                          {templateOptionLabel(template)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    <span>Preferred endpoint mode</span>
                    <select
                      value={policyDraft.preferred_endpoint_mode}
                      onChange={(event) =>
                        setPolicyDraft((current) =>
                          current
                            ? {
                                ...current,
                                preferred_endpoint_mode: event.target.value as ProjectAutomationPolicy['preferred_endpoint_mode']
                              }
                            : current
                        )
                      }
                    >
                      <option value="realtime">realtime</option>
                      <option value="batch">batch</option>
                      <option value="both">both</option>
                    </select>
                  </label>
                  <div className="actions-row training-form-grid__full">
                    <button type="submit" disabled={policyMutation.isPending}>
                      {policyMutation.isPending ? 'Saving...' : 'Save automation policy'}
                    </button>
                    <Link to="/operations/model-templates" search={selectedModelTemplateSearch} className="primary-link">
                      Manage templates
                    </Link>
                  </div>
                </form>
              </section>
            ) : null}
          </div>
        </Panel>

        <Panel
          title="Models and Templates"
          action={
            <Link to="/operations/model-templates" search={selectedModelTemplateSearch} className="primary-link">
              Template administration
            </Link>
          }
        >
          <div className="selection-layout">
            <section className="selection-pane">
              <div className="section-heading">
                <h3>Model selection</h3>
                <span className="badge badge--neutral">{pipeline.models.length} models</span>
              </div>
              {pipeline.models.length ? (
                <div className="pipeline-model-list pipeline-model-list--compact">
                  {pipeline.models.map((model) => (
                    <button
                      key={model.model_family_id}
                      type="button"
                      className={
                        selectedModelId === model.model_family_id
                          ? 'pipeline-model pipeline-model--selected'
                          : 'pipeline-model'
                      }
                      onClick={() => setSelectedModelId(model.model_family_id)}
                    >
                      <strong>{model.display_name}</strong>
                      <span>{architectureLabel(model.architecture)}</span>
                      <span className="subtle-text">
                        {model.template_key ?? 'custom template'} /{' '}
                        {model.latest_model_version_no ? `v${model.latest_model_version_no}` : 'Not trained yet'}
                      </span>
                    </button>
                  ))}
                </div>
              ) : (
                <EmptyState
                  title="No project model yet"
                  body="Create one model using a template. Internal slugs and endpoint names are still generated automatically."
                />
              )}
            </section>

            <section className="details-pane">
              <div className="section-heading">
                <h3>Selected model details</h3>
              </div>
              {selectedModel ? (
                <dl className="definition-list">
                  <div>
                    <dt>Name</dt>
                    <dd>{selectedModel.display_name}</dd>
                  </div>
                  <div>
                    <dt>Architecture</dt>
                    <dd>
                      {architectureLabel(selectedModel.architecture)}
                      <small>{architectureSecondaryLabel(selectedModel.architecture)}</small>
                    </dd>
                  </div>
                  <div>
                    <dt>Template</dt>
                    <dd>{selectedModel.template_key ?? 'custom template'}</dd>
                  </div>
                  <div>
                    <dt>Training defaults</dt>
                    <dd>
                      {selectedModelTemplate
                        ? trainingDefaultsSummary(selectedModelTemplate.defaults.training)
                        : 'Defaults captured on training run detail.'}
                    </dd>
                  </div>
                  <div>
                    <dt>Promotion criteria</dt>
                    <dd>{promotionPolicySummary(selectedModel.promotion_policy)}</dd>
                  </div>
                  <div>
                    <dt>Note</dt>
                    <dd>{selectedModel.description ?? 'No operator note recorded.'}</dd>
                  </div>
                </dl>
              ) : (
                <p className="subtle-text">Select a model to inspect its architecture, template, and note.</p>
              )}
            </section>
          </div>

          <details className="advanced-section">
            <summary>How model and template values are used</summary>
            <div className="details-pane details-pane--plain">
              <div className="section-heading">
                <h3>Effective training inputs</h3>
              </div>
              <dl className="definition-list">
                <div>
                  <dt>Train action</dt>
                  <dd>Uses the selected model family and its saved template key.</dd>
                </div>
                <div>
                  <dt>Create model</dt>
                  <dd>Uses the template chosen in the create-model dialog and stores it on the new model.</dd>
                </div>
                <div>
                  <dt>Automation</dt>
                  <dd>Uses the default template selected in Automation & advanced policy for future automatic runs.</dd>
                </div>
              </dl>
            </div>
          </details>
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="Training History">
          {pipeline.training_history.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Architecture</th>
                  <th>Template</th>
                  <th>Status</th>
                  <th>Progress</th>
                  <th>Started</th>
                </tr>
              </thead>
              <tbody>
                {pipeline.training_history.map((job) => (
                  <tr
                    key={job.training_job_id}
                    className={selectedTrainingId === job.training_job_id ? 'training-row training-row--selected' : 'training-row'}
                    onClick={() => setSelectedTrainingId(job.training_job_id)}
                  >
                    <td>
                      <strong>{job.training_job_id}</strong>
                      <div className="subtle-text">{job.model_family_slug}</div>
                    </td>
                    <td>{architectureLabel(job.architecture)}</td>
                    <td>{job.model_template_key ?? 'custom'}</td>
                    <td>
                      <span className={`badge badge--${trainingTone(job.status)}`}>{job.status}</span>
                    </td>
                    <td>{job.progress_percent.toFixed(1)}%</td>
                    <td>{formatDate(job.started_at ?? job.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title="No training run yet"
              body="Once a model exists and a valid dataset is available, start training from this page."
            />
          )}
        </Panel>

        <Panel
          title="Selected Run"
          action={
            canCancelSelectedTraining ? (
              <div className="actions-row">
                <button
                  type="button"
                  className={shouldForceCancelSelectedTraining ? 'button--danger' : undefined}
                  disabled={cancelTrainingMutation.isPending}
                  onClick={() => {
                    if (!selectedTrainingId) {
                      return;
                    }
                    const force = shouldForceCancelSelectedTraining;
                    const confirmed = confirmConsequentialAction({
                      title: force
                        ? `Force cancel training job ${selectedTrainingId}?`
                        : `Cancel training job ${selectedTrainingId}?`,
                      consequence: force
                        ? 'The job will be marked cancelled immediately and stale worker ownership will be cleared.'
                        : 'A live worker will stop at the next safe boundary. Stale jobs are marked cancelled immediately.'
                    });
                    if (confirmed) {
                      cancelTrainingMutation.mutate({
                        trainingJobId: selectedTrainingId,
                        force
                      });
                    }
                  }}
                >
                  {shouldForceCancelSelectedTraining ? 'Force cancel' : 'Cancel training'}
                </button>
              </div>
            ) : null
          }
        >
          {!selectedTrainingQuery.data ? (
            <EmptyState
              title={selectedTrainingId ? 'Run detail unavailable' : 'Choose a run'}
              body={
                selectedTrainingId
                  ? 'The detail view did not load, but operator actions for the selected run are still available above.'
                  : 'Select a training job to inspect progress, resolved defaults, logs, and promotion readiness.'
              }
            />
          ) : (
            <div className="panel-stack">
              <div className="detail-grid">
                <MetricPill
                  label="Status"
                  value={selectedTrainingQuery.data.status}
                  tone={selectedTrainingQuery.data.status === 'completed' ? 'success' : 'neutral'}
                />
                <MetricPill label="Architecture" value={architectureLabel(selectedTrainingQuery.data.architecture)} tone="info" />
                <MetricPill label="Epoch" value={`${selectedTrainingQuery.data.current_epoch}/${selectedTrainingQuery.data.total_epochs}`} />
                <MetricPill label="Template" value={selectedTrainingQuery.data.model_template_key ?? 'custom'} />
              </div>
              <div className="detail-grid">
                <div className="metric-pill">
                  <span>Resolved defaults</span>
                  <strong>{trainingDefaultsSummary(selectedTrainingQuery.data.resolved_training_defaults)}</strong>
                  <small className="subtle-text">Exact parameters captured with the immutable run.</small>
                </div>
                <div className="metric-pill">
                  <span>Promotion gate</span>
                  <strong>
                    {selectedTrainingQuery.data.evaluation_summary.promotion_ready ? 'Passed' : 'Needs review'}
                  </strong>
                  <small className="subtle-text">
                    {selectedTrainingQuery.data.evaluation_summary.promotion_ready
                      ? 'Packaging and deployment can continue according to policy.'
                    : 'Quality gates have not passed yet, so deployment automation will not replace the current live model.'}
                  </small>
                </div>
                <div className="metric-pill">
                  <span>Resolved criteria</span>
                  <strong>{promotionPolicySummary(selectedTrainingQuery.data.promotion_policy)}</strong>
                  <small className="subtle-text">Stored with the training job and used for deployment readiness.</small>
                </div>
              </div>
              <details className="advanced-section">
                <summary>Promotion gate and run logs</summary>
                {selectedTrainingQuery.data.evaluation_summary.promotion_results.length ? (
                  <table className="data-table compact-table">
                    <thead>
                      <tr>
                        <th>Metric</th>
                        <th>Threshold</th>
                        <th>Actual</th>
                        <th>Passed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedTrainingQuery.data.evaluation_summary.promotion_results.map((result) => (
                        <tr key={`${result.metric_name}-${result.phase}`}>
                          <td>{result.metric_name}</td>
                          <td>
                            {result.comparison} {result.threshold}
                          </td>
                          <td>{result.actual_value ?? 'n/a'}</td>
                          <td>{result.passed ? 'yes' : 'no'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                ) : null}
                <div className="training-log-list">
                  {selectedTrainingQuery.data.logs.slice(0, 12).map((entry) => (
                    <article key={entry.sequence_no} className="training-log">
                      <div className="training-log__meta">
                        <strong>{entry.event_type}</strong>
                        <span>{formatDate(entry.occurred_at)}</span>
                      </div>
                      <p>{entry.message}</p>
                    </article>
                  ))}
                </div>
              </details>
            </div>
          )}
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="Current Deployment">
          <div className="inline-alert inline-alert--info">
            <strong>Label Studio model connection</strong>
            <p className="subtle-text">
              {predictionBackend.enabled
                ? `Connected backend ${predictionBackend.backend_id ?? 'n/a'} uses the active realtime deployment for this project.`
                : canLinkLabelStudioPredictions
                  ? 'Ready to connect. Label Studio will call this deployed model when it needs predictions.'
                  : 'Deploy a realtime model before connecting this Label Studio project.'}
            </p>
          </div>
          {pipeline.current_endpoints.length ? (
            <div className="pipeline-template-grid">
              {pipeline.current_endpoints.map((endpoint) => (
                <article key={endpoint.endpoint_name} className="status-card">
                  <div className="status-card__header">
                    <div>
                      <h3>{endpoint.display_name}</h3>
                      <p className="subtle-text">{endpoint.endpoint_name}</p>
                    </div>
                    <span className={`badge badge--${endpoint.realtime_available || endpoint.batch_available ? 'success' : 'neutral'}`}>
                      {endpointAvailabilityLabel(endpoint)}
                    </span>
                  </div>
                  <p className="subtle-text">
                    {taskTypeLabel(endpoint.task_type)} / {architectureLabel(endpoint.architecture)} / version{' '}
                    {endpoint.current_model_version_no ?? 'n/a'}
                  </p>
                  <div className="pipeline-card__footer">
                    <Link to="/endpoints/$modelId" params={{ modelId: endpoint.endpoint_name }} className="primary-link">
                      Open endpoint details
                    </Link>
                    <span>Version {endpoint.current_model_version_no ?? 'n/a'}</span>
                  </div>
                  <details className="advanced-section">
                    <summary>Release identifiers</summary>
                    <p className="subtle-text">
                      Realtime: {endpoint.realtime_release_id ?? 'none'} / Batch:{' '}
                      {endpoint.batch_release_id ?? 'none'}
                    </p>
                  </details>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState
              title="No endpoint published yet"
              body="After a successful training run, packaging and deployment continue automatically or through one approval action based on policy."
            />
          )}
        </Panel>

        <Panel title="Activity Timeline">
          {pipeline.activity_timeline.length ? (
            <div className="timeline-list">
              {pipeline.activity_timeline.map((entry) => (
                <article key={`${entry.event_type}-${entry.aggregate_id}-${entry.occurred_at}`} className="timeline-item">
                  <strong>{entry.title}</strong>
                  <p>{entry.detail}</p>
                  <small className="subtle-text">{formatDate(entry.occurred_at)}</small>
                </article>
              ))}
            </div>
          ) : (
            <EmptyState
              title="No recent activity"
              body="Pipeline events from sync, snapshots, training, deployment, and cleanup appear here."
            />
          )}
        </Panel>
      </div>

      {createModelOpen ? (
        <div className="modal-backdrop" onClick={closeCreateModel}>
          <div className="modal-panel" onClick={(event) => event.stopPropagation()}>
            <div className="panel__header">
              <div>
                <h2>{createModelIntent === 'train' ? 'Create model and start training' : 'Create model'}</h2>
                <p className="subtle-text">
                  Choose an operator-facing name and preset. The task type, architecture, slugs, and endpoint names
                  stay automated unless you open the advanced settings.
                </p>
              </div>
              <button type="button" onClick={closeCreateModel}>
                Close
              </button>
            </div>

            <form
              className="training-form-grid"
              onSubmit={(event) => {
                event.preventDefault();
                createModelMutation.mutate({
                  ...modelDraft,
                  display_name: modelDraft.display_name?.trim() || null,
                  description: modelDraft.description?.trim() || null,
                  model_template_key: selectedTemplate?.template_key ?? modelDraft.model_template_key,
                  architecture_family: selectedTemplate?.architecture.family ?? modelDraft.architecture_family,
                  architecture_version: selectedTemplate?.architecture.version ?? modelDraft.architecture_version,
                  architecture_size: selectedTemplate?.architecture.size ?? modelDraft.architecture_size,
                  promotion_policy: modelDraft.promotion_policy ?? null,
                  task_type: modelDraftTaskType
                });
              }}
            >
              <label>
                <span>Model type</span>
                <select
                  value={modelDraftTaskType}
                  onChange={(event) => {
                    const nextTaskType = event.target.value as TaskType;
                    setModelDraft((current) =>
                      initializeModelDraftForTask(
                        current,
                        nextTaskType,
                        allModelTemplates,
                        allSupportedModelArchitectures
                      )
                    );
                  }}
                >
                  {modelTaskTypeOptions.map((taskType) => (
                    <option key={taskType} value={taskType}>
                      {taskTypeLabel(taskType)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Display name</span>
                <input
                  value={modelDraft.display_name ?? ''}
                  onChange={(event) =>
                    setModelDraft((current) => ({ ...current, display_name: event.target.value }))
                  }
                  placeholder={`${pipeline.title} ${selectedTemplate?.display_name_suffix ?? 'Model'}`}
                />
              </label>
              <label>
                <span>Template</span>
                <select
                  value={selectedTemplate?.template_key ?? modelDraft.model_template_key}
                  onChange={(event) => {
                    const template = selectableTemplates.find((item) => item.template_key === event.target.value);
                    setModelDraft((current) =>
                      template
                        ? applyTemplateToModelDraft(current, template, modelDraftTaskType)
                        : { ...current, model_template_key: event.target.value }
                    );
                  }}
                >
                  {selectableTemplates.map((template) => (
                    <option key={template.template_key} value={template.template_key}>
                      {templateOptionLabel(template)}
                    </option>
                  ))}
                </select>
              </label>
              {modelTypeChanged ? (
                <div className="inline-alert inline-alert--info training-form-grid__full">
                  <strong>Project type will change to {taskTypeLabel(modelDraftTaskType)}</strong>
                  <p className="subtle-text">
                    Existing ready snapshots are superseded because they were materialized for {taskTypeLabel(pipeline.task_type)}.
                    Create a fresh snapshot before training this model type.
                  </p>
                </div>
              ) : null}
              <details className="advanced-section training-form-grid__full">
                <summary>Advanced model settings</summary>
                <div className="training-form-grid">
                  <label>
                    <span>Task type</span>
                    <input value={taskTypeLabel(modelDraftTaskType)} readOnly />
                  </label>
                  <label>
                    <span>Supported model architecture</span>
                    <select
                      value={creationArchitectureValue}
                      onChange={(event) => {
                        const [family, version, size] = event.target.value.split(':', 3);
                        const nextTemplate = recommendedTemplateForSelection({
                          templates: selectableTemplates,
                          taskType: modelDraftTaskType,
                          family,
                          version,
                          size
                        });
                        setModelDraft((current) => ({
                          ...current,
                          task_type: modelDraftTaskType,
                          architecture_family: family,
                          architecture_version: version,
                          architecture_size: size,
                          model_template_key: nextTemplate?.template_key ?? current.model_template_key,
                          promotion_policy:
                            nextTemplate?.template_key && nextTemplate.template_key !== current.model_template_key
                              ? null
                              : current.promotion_policy
                        }));
                      }}
                    >
                      {creationSupportedArchitectures.map((item) => (
                        <option
                          key={architectureKeyFromSummary(item.architecture)}
                          value={`${item.architecture.family}:${item.architecture.version}:${item.architecture.size}`}
                        >
                          {architectureLabel(item.architecture)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <div className="metric-pill">
                    <span>Template key</span>
                    <strong>{selectedTemplate?.template_key ?? modelDraft.model_template_key}</strong>
                    <small className="subtle-text">
                      {selectedTemplate ? templateDefaultsSummary(selectedTemplate.defaults) : 'No template selected'}
                    </small>
                  </div>
                </div>
              </details>

              {selectedTemplate ? (
                <div className="template-summary training-form-grid__full">
                  <div className="section-heading">
                    <h3>{selectedTemplate.label}</h3>
                    <span className={`badge badge--${selectedTemplate.recommended ? 'success' : 'neutral'}`}>
                      {selectedTemplate.recommended ? 'Recommended' : 'Optional'}
                    </span>
                  </div>
                  <dl className="definition-list definition-list--inline">
                    <div>
                      <dt>Architecture</dt>
                      <dd>{architectureLabel(selectedTemplate.architecture)}</dd>
                    </div>
                    <div>
                      <dt>Training profile</dt>
                      <dd>{selectedTemplate.default_training_profile}</dd>
                    </div>
                    <div>
                      <dt>Defaults</dt>
                      <dd>{trainingDefaultsSummary(selectedTemplate.defaults.training)}</dd>
                    </div>
                    <div>
                      <dt>Promotion criteria</dt>
                      <dd>{promotionPolicySummary(modelDraft.promotion_policy ?? selectedTemplate.default_promotion_policy)}</dd>
                    </div>
                    <div>
                      <dt>Description</dt>
                      <dd>{selectedTemplate.description}</dd>
                    </div>
                  </dl>
                  <div className="inline-alert inline-alert--info">
                    <strong>Deployment gating</strong>
                    <p className="subtle-text">
                      New models inherit the template promotion criteria. Overrides are saved on the model and used
                      for every training run and deployment decision.
                    </p>
                  </div>
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={Boolean(modelDraft.promotion_policy)}
                      disabled={!selectedTemplate.default_promotion_policy.allow_model_override}
                      onChange={(event) =>
                        setModelDraft((current) => ({
                          ...current,
                          promotion_policy: event.target.checked
                            ? selectedTemplate.default_promotion_policy
                            : null
                        }))
                      }
                    />
                    <span>Override inherited promotion criteria</span>
                  </label>
                  {modelDraft.promotion_policy ? (
                    <div className="pipeline-template-grid">
                      {modelDraft.promotion_policy.criteria.map((criterion, index) => (
                        <div key={`${criterion.metric_name}-${index}`} className="metric-pill">
                          <label>
                            <span>Metric</span>
                            <input
                              value={criterion.metric_name}
                              onChange={(event) =>
                                setModelDraft((current) =>
                                  updateModelDraftPromotionCriterion(current, index, {
                                    metric_name: event.target.value
                                  })
                                )
                              }
                            />
                          </label>
                          <label>
                            <span>Comparison</span>
                            <select
                              value={criterion.comparison}
                              onChange={(event) =>
                                setModelDraft((current) =>
                                  updateModelDraftPromotionCriterion(current, index, {
                                    comparison: event.target.value as 'gte' | 'lte'
                                  })
                                )
                              }
                            >
                              <option value="gte">greater or equal</option>
                              <option value="lte">less or equal</option>
                            </select>
                          </label>
                          <label>
                            <span>Threshold</span>
                            <input
                              type="number"
                              step="0.001"
                              value={criterion.threshold}
                              onChange={(event) =>
                                setModelDraft((current) =>
                                  updateModelDraftPromotionCriterion(current, index, {
                                    threshold: Number(event.target.value)
                                  })
                                )
                              }
                            />
                          </label>
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className="inline-alert inline-alert--danger">
                  <strong>No active template is available</strong>
                  <p className="subtle-text">
                    Activate or create a template for this task type in the administration page before creating a model.
                  </p>
                </div>
              )}

              <label className="training-form-grid__full">
                <span>Optional note</span>
                <textarea
                  value={modelDraft.description ?? ''}
                  onChange={(event) =>
                    setModelDraft((current) => ({ ...current, description: event.target.value }))
                  }
                  placeholder="Optional operator-facing note"
                />
              </label>

              <div className="actions-row training-form-grid__full">
                <button type="submit" disabled={createModelMutation.isPending || !selectedTemplate}>
                  {createModelMutation.isPending
                    ? 'Creating...'
                    : createModelIntent === 'train' && modelTypeChanged
                      ? 'Create model and update project type'
                      : createModelIntent === 'train'
                      ? 'Create model and start training'
                      : 'Create model'}
                </button>
                <Link
                  to="/operations/model-templates"
                  search={{
                    templateKey: selectedTemplate?.template_key ?? undefined,
                    returnToLabelProjectId: labelProjectId
                  }}
                  className="primary-link"
                >
                  Manage templates
                </Link>
              </div>
            </form>
          </div>
        </div>
      ) : null}

      <CleanupDialog
        open={cleanupOpen}
        title={deleteEntirePipeline ? 'Delete entire project pipeline' : 'Cleanup old pipeline data'}
        description={
          deleteEntirePipeline
            ? 'Delete this project pipeline, its Label Studio project, dataset records, generated models, deployments, and managed artifacts. Runtime channels remain and may need reassignment.'
            : 'Remove older snapshots, training runs, model versions, and deployment history while keeping this project pipeline and its latest usable state available.'
        }
        preview={cleanupPreviewQuery.data}
        pending={cleanupMutation.isPending}
        defaultDeleteArtifacts
        forceDeleteArtifacts={deleteEntirePipeline}
        extraPayload={{ delete_entire_pipeline: deleteEntirePipeline }}
        destructiveOptionLabel="Gesamte Pipeline inkl. aller Abhängigkeiten löschen"
        destructiveOptionDescription="Löscht Label-Studio-Projekt, Dataset, erzeugte Modelle, Deployments und Artefakte. Kanäle bleiben bestehen."
        destructiveOptionChecked={deleteEntirePipeline}
        onDestructiveOptionChange={setDeleteEntirePipeline}
        acknowledgementLabel={
          deleteEntirePipeline
            ? 'I understand this permanently removes the project pipeline and all generated project data.'
            : 'I understand this cleanup removes older project history while keeping the current pipeline usable.'
        }
        confirmLabel={deleteEntirePipeline ? 'Delete entire pipeline' : 'Cleanup old data'}
        pendingLabel={deleteEntirePipeline ? 'Deleting pipeline...' : 'Cleaning up...'}
        onClose={() => setCleanupOpen(false)}
        onConfirm={(payload) => cleanupMutation.mutate(payload)}
      />
    </div>
  );
}

function totalSelectedBytes(files: File[]) {
  return files.reduce((sum, file) => sum + file.size, 0);
}

function formatBytes(value: number) {
  if (!value) {
    return '0 B';
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let current = value;
  let unitIndex = 0;
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024;
    unitIndex += 1;
  }
  return `${current.toFixed(current >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}

function inferUploadContentType(fileName: string) {
  const lower = fileName.toLowerCase();
  if (lower.endsWith('.png')) {
    return 'image/png';
  }
  if (lower.endsWith('.bmp')) {
    return 'image/bmp';
  }
  if (lower.endsWith('.webp')) {
    return 'image/webp';
  }
  return 'image/jpeg';
}

function isImageFile(file: File) {
  const lower = file.name.toLowerCase();
  return (
    file.type.startsWith('image/') ||
    ['.jpg', '.jpeg', '.png', '.bmp', '.webp'].some((extension) => lower.endsWith(extension))
  );
}

function inferCommonFolderName(files: File[]) {
  const folderNames = new Set(
    files
      .map((file) => (file as File & { webkitRelativePath?: string }).webkitRelativePath)
      .filter((value): value is string => Boolean(value && value.includes('/')))
      .map((value) => value.split('/')[0])
      .filter(Boolean)
  );
  return folderNames.size === 1 ? [...folderNames][0] : null;
}

function extractDeclaredLabels(labelConfig: LabelProjectSummary['label_config'] | undefined) {
  const labels = new Set<string>();
  const visit = (value: unknown, key?: string) => {
    if (typeof value === 'string') {
      if (key === 'raw') {
        for (const match of value.matchAll(/<(?:Choice|Label)\b[^>]*\bvalue=(['"])(.*?)\1/gi)) {
          if (match[2]) {
            labels.add(match[2]);
          }
        }
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const item of value) {
        visit(item, key);
      }
      return;
    }
    if (!value || typeof value !== 'object') {
      return;
    }
    const record = value as Record<string, unknown>;
    if (typeof record.value === 'string' && ['choices', 'labels', undefined].includes(key)) {
      labels.add(record.value);
    }
    for (const [childKey, childValue] of Object.entries(record)) {
      if ((childKey === 'choices' || childKey === 'labels') && typeof childValue === 'string') {
        labels.add(childValue);
        continue;
      }
      if (childKey === 'choices_attrs' || childKey === 'labels_attrs') {
        if (childValue && typeof childValue === 'object' && !Array.isArray(childValue)) {
          for (const label of Object.keys(childValue as Record<string, unknown>)) {
            labels.add(label);
          }
        }
      }
      visit(childValue, childKey);
    }
  };
  visit(labelConfig);
  return [...labels].sort((left, right) => left.localeCompare(right));
}

function applyTemplateToModelDraft(
  current: ProjectModelCreateRequest,
  template: ModelTemplateSummary,
  taskType = template.task_type
): ProjectModelCreateRequest {
  return {
    ...current,
    task_type: taskType,
    model_template_key: template.template_key,
    architecture_family: template.architecture.family,
    architecture_version: template.architecture.version,
    architecture_size: template.architecture.size,
    promotion_policy: null
  };
}

function updateModelDraftPromotionCriterion(
  draft: ProjectModelCreateRequest,
  index: number,
  patch: Partial<PromotionCriteriaPolicy['criteria'][number]>
): ProjectModelCreateRequest {
  if (!draft.promotion_policy) {
    return draft;
  }
  return {
    ...draft,
    promotion_policy: {
      ...draft.promotion_policy,
      criteria: draft.promotion_policy.criteria.map((criterion, currentIndex) =>
        currentIndex === index ? { ...criterion, ...patch } : criterion
      )
    }
  };
}

function modelCreationTaskOptions(pipeline: ProjectPipelineDetail): TaskType[] {
  const existingModelTaskTypes = Array.from(new Set(pipeline.models.map((model) => model.task_type)));
  if (existingModelTaskTypes.length) {
    return existingModelTaskTypes;
  }
  const taskType = pipeline.task_type;
  if (taskType === 'classification' || taskType === 'anomaly') {
    return ['classification', 'anomaly'];
  }
  if (taskType === 'detection' || taskType === 'oriented_detection') {
    return ['detection', 'oriented_detection'];
  }
  return [taskType];
}

function initializeModelDraft(
  pipeline: ProjectPipelineDetail,
  current: ProjectModelCreateRequest
): ProjectModelCreateRequest {
  return initializeModelDraftForTask(
    current,
    pipeline.task_type,
    pipeline.model_templates,
    pipeline.supported_model_architectures
  );
}

function initializeModelDraftForTask(
  current: ProjectModelCreateRequest,
  taskType: TaskType,
  templates: ModelTemplateSummary[],
  architectures: ProjectPipelineDetail['supported_model_architectures']
): ProjectModelCreateRequest {
  const supportedArchitectures = supportedArchitecturesForTask(
    architectures,
    taskType
  );
  const taskTemplates = templates.filter(
    (template) => template.active && template.task_type === taskType
  );
  const currentTemplate = taskTemplates.find((template) => template.template_key === current.model_template_key);
  const recommendedTemplate =
    currentTemplate ??
    taskTemplates.find((template) => template.recommended) ??
    taskTemplates[0] ??
    null;
  const defaultArchitecture =
    recommendedTemplate?.architecture ?? supportedArchitectures[0]?.architecture ?? null;

  const candidate: ProjectModelCreateRequest = {
    ...current,
    task_type: taskType,
    model_template_key:
      recommendedTemplate?.template_key ??
      (current.task_type === taskType && current.model_template_key
        ? current.model_template_key
        : defaultTemplateForTask(taskType)),
    architecture_family: defaultArchitecture?.family || current.architecture_family || emptyModelDraft.architecture_family,
    architecture_version: defaultArchitecture?.version || current.architecture_version || emptyModelDraft.architecture_version,
    architecture_size: defaultArchitecture?.size || current.architecture_size || emptyModelDraft.architecture_size
  };

  const supported = supportedArchitectures.some(
    (item) =>
      item.architecture.family === candidate.architecture_family &&
      item.architecture.version === candidate.architecture_version &&
      item.architecture.size === candidate.architecture_size
  );

  const next = supported
    ? candidate
    : {
        ...candidate,
        architecture_family: defaultArchitecture?.family || candidate.architecture_family,
        architecture_version: defaultArchitecture?.version || candidate.architecture_version,
        architecture_size: defaultArchitecture?.size || candidate.architecture_size
      };

  return next;
}
