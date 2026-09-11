import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  BestCheckpointRule,
  DatasetSummary,
  ModelArchitectureSummary,
  ModelTemplateSummary,
  PromotionCriterion,
  TaskType,
  TrainingDefaults,
  TrainingJobCreateRequest,
  TrainingJobDetail,
  TrainingJobStatus
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { confirmConsequentialAction, describeError } from '../../app/errors';
import { CleanupDialog } from '../../components/cleanup-dialog';
import { useNotifications } from '../../components/notification-provider';
import { architectureLabel, taskTypeLabel, trainingDefaultsSummary } from '../pipelines/modeling';

type TrainingFormState = {
  datasetId: string;
  snapshotId: string;
  modelFamilySlug: string;
  taskType: TaskType;
  modelTemplateKey: string;
  architectureFamily: string;
  architectureVersion: string;
  architectureSize: string;
  recipeName: string;
  recipeVersion: string;
  maxEpochs: number;
  learningRate: number;
  imageSize?: number | null;
  batchSize?: number | null;
  optimizer?: string | null;
  optimizerSettings: Record<string, unknown>;
  checkpointIntervalSteps: number;
  bestMetricName: string;
  bestMetricMode: 'max' | 'min';
  promotionMetricName: string;
  promotionComparison: 'gte' | 'lte';
  promotionThreshold: number;
  secondaryPromotionMetricName: string;
  secondaryPromotionComparison: 'gte' | 'lte';
  secondaryPromotionThreshold: number;
  allowDuplicateSnapshotTraining: boolean;
};

function defaultForm(taskType: TaskType = 'detection'): TrainingFormState {
  if (taskType === 'anomaly') {
    return {
      datasetId: '',
      snapshotId: '',
      modelFamilySlug: 'surface-anomaly-ad',
      taskType,
      modelTemplateKey: 'anomaly-patchcore-wide-resnet50-2',
      architectureFamily: 'patchcore',
      architectureVersion: 'anomalib',
      architectureSize: 'wide_resnet50_2',
      recipeName: 'baseline_anomaly',
      recipeVersion: '1.0.0',
      maxEpochs: 1,
      learningRate: 0.001,
      imageSize: 256,
      batchSize: 8,
      optimizer: 'anomalib-default',
      optimizerSettings: { method: 'patchcore', backbone: 'wide_resnet50_2' },
      checkpointIntervalSteps: 25,
      bestMetricName: 'image_auroc',
      bestMetricMode: 'max',
      promotionMetricName: 'image_auroc',
      promotionComparison: 'gte',
      promotionThreshold: 0.9,
      secondaryPromotionMetricName: 'image_f1',
      secondaryPromotionComparison: 'gte',
      secondaryPromotionThreshold: 0.8,
      allowDuplicateSnapshotTraining: false
    };
  }
  if (taskType === 'segmentation') {
    return {
      datasetId: '',
      snapshotId: '',
      modelFamilySlug: 'defect-segmentation-seg',
      taskType,
      modelTemplateKey: 'segmentation-yolov8-n',
      architectureFamily: 'yolo',
      architectureVersion: 'v8',
      architectureSize: 'n',
      recipeName: 'baseline_seg_yolo',
      recipeVersion: '1.2.0',
      maxEpochs: 50,
      learningRate: 0.01,
      imageSize: 640,
      batchSize: 12,
      optimizer: 'sgd',
      optimizerSettings: { momentum: 0.937, weight_decay: 0.0005 },
      checkpointIntervalSteps: 25,
      bestMetricName: 'mask_map_50_95',
      bestMetricMode: 'max',
      promotionMetricName: 'mask_map_50_95',
      promotionComparison: 'gte',
      promotionThreshold: 0.45,
      secondaryPromotionMetricName: 'mask_map_50',
      secondaryPromotionComparison: 'gte',
      secondaryPromotionThreshold: 0.65,
      allowDuplicateSnapshotTraining: false
    };
  }
  if (taskType === 'detection') {
    return {
      datasetId: '',
      snapshotId: '',
      modelFamilySlug: 'defect-detection-det',
      taskType,
      modelTemplateKey: 'detection-yolov8-n',
      architectureFamily: 'yolo',
      architectureVersion: 'v8',
      architectureSize: 'n',
      recipeName: 'baseline_det_mmyolo',
      recipeVersion: '1.2.0',
      maxEpochs: 50,
      learningRate: 0.01,
      imageSize: 640,
      batchSize: 16,
      optimizer: 'sgd',
      optimizerSettings: { momentum: 0.937, weight_decay: 0.0005 },
      checkpointIntervalSteps: 25,
      bestMetricName: 'map_50_95',
      bestMetricMode: 'max',
      promotionMetricName: 'map_50_95',
      promotionComparison: 'gte',
      promotionThreshold: 0.55,
      secondaryPromotionMetricName: 'map_50',
      secondaryPromotionComparison: 'gte',
      secondaryPromotionThreshold: 0.7,
      allowDuplicateSnapshotTraining: false
    };
  }
  if (taskType === 'oriented_detection') {
    return {
      datasetId: '',
      snapshotId: '',
      modelFamilySlug: 'defect-oriented-detection-obb',
      taskType,
      modelTemplateKey: 'oriented_detection-yolov26-n',
      architectureFamily: 'yolo',
      architectureVersion: 'v26',
      architectureSize: 'n',
      recipeName: 'baseline_obb_yolo',
      recipeVersion: '1.0.0',
      maxEpochs: 80,
      learningRate: 0.01,
      imageSize: 1024,
      batchSize: 8,
      optimizer: 'sgd',
      optimizerSettings: { momentum: 0.937, weight_decay: 0.0005 },
      checkpointIntervalSteps: 25,
      bestMetricName: 'map_50_95',
      bestMetricMode: 'max',
      promotionMetricName: 'map_50_95',
      promotionComparison: 'gte',
      promotionThreshold: 0.55,
      secondaryPromotionMetricName: 'map_50',
      secondaryPromotionComparison: 'gte',
      secondaryPromotionThreshold: 0.72,
      allowDuplicateSnapshotTraining: false
    };
  }
  return {
    datasetId: '',
    snapshotId: '',
    modelFamilySlug: 'widget-inspection-cls',
    taskType,
    modelTemplateKey: 'classification-resnet-18',
    architectureFamily: 'resnet',
    architectureVersion: 'v1',
    architectureSize: '18',
    recipeName: 'baseline_cls_torchvision',
    recipeVersion: '1.2.0',
    maxEpochs: 30,
    learningRate: 0.001,
    imageSize: 224,
    batchSize: 32,
    optimizer: 'adamw',
    optimizerSettings: { weight_decay: 0.0005 },
    checkpointIntervalSteps: 20,
    bestMetricName: 'accuracy_top1',
    bestMetricMode: 'max',
    promotionMetricName: 'accuracy_top1',
    promotionComparison: 'gte',
    promotionThreshold: 0.9,
    secondaryPromotionMetricName: 'f1_macro',
    secondaryPromotionComparison: 'gte',
    secondaryPromotionThreshold: 0.85,
    allowDuplicateSnapshotTraining: false
  };
}

function recipeForArchitecture(
  taskType: TaskType,
  architecture: Pick<ModelArchitectureSummary, 'family' | 'version'>
): Pick<TrainingFormState, 'recipeName' | 'recipeVersion'> {
  const family = architecture.family.toLowerCase();
  const version = architecture.version.toLowerCase();
  if (taskType === 'classification') {
    return family === 'yolo'
      ? { recipeName: 'baseline_cls_yolo', recipeVersion: '1.2.0' }
      : { recipeName: 'baseline_cls_torchvision', recipeVersion: '1.2.0' };
  }
  if (taskType === 'detection') {
    return family === 'yolo' && version === 'v8'
      ? { recipeName: 'baseline_det_mmyolo', recipeVersion: '1.2.0' }
      : { recipeName: 'baseline_det_yolo', recipeVersion: '1.2.0' };
  }
  if (taskType === 'oriented_detection') {
    return { recipeName: 'baseline_obb_yolo', recipeVersion: '1.0.0' };
  }
  if (taskType === 'segmentation') {
    return family === 'yolo'
      ? { recipeName: 'baseline_seg_yolo', recipeVersion: '1.2.0' }
      : { recipeName: 'baseline_seg_torchvision', recipeVersion: '1.2.0' };
  }
  return { recipeName: 'baseline_anomaly', recipeVersion: '1.0.0' };
}

function buildBestCheckpointRule(form: TrainingFormState): BestCheckpointRule {
  return {
    metric_name: form.bestMetricName,
    mode: form.bestMetricMode,
    phase: 'validation',
    minimum_epoch: 1
  };
}

function buildPromotionCriteria(form: TrainingFormState): PromotionCriterion[] {
  const primary: PromotionCriterion = {
    metric_name: form.promotionMetricName,
    comparison: form.promotionComparison,
    threshold: Number(form.promotionThreshold),
    phase: 'evaluation'
  };
  const secondary: PromotionCriterion | null = form.secondaryPromotionMetricName
    ? {
        metric_name: form.secondaryPromotionMetricName,
        comparison: form.secondaryPromotionComparison,
        threshold: Number(form.secondaryPromotionThreshold),
        phase: 'evaluation'
      }
    : null;
  return secondary ? [primary, secondary] : [primary];
}

function formArchitecture(form: TrainingFormState): ModelArchitectureSummary {
  return {
    task_type: form.taskType,
    family: form.architectureFamily,
    version: form.architectureVersion,
    size: form.architectureSize,
    display_name: architectureLabelFromFields(form),
    internal_key: `${form.taskType}:${form.architectureFamily}:${form.architectureVersion}:${form.architectureSize}`
  };
}

function resolvedTrainingDefaults(form: TrainingFormState): TrainingDefaults {
  return {
    epochs: Number(form.maxEpochs),
    learning_rate: Number(form.learningRate),
    image_size: form.imageSize ? Number(form.imageSize) : null,
    batch_size: form.batchSize ? Number(form.batchSize) : null,
    optimizer: form.optimizer || null,
    optimizer_settings: form.optimizerSettings,
    checkpoint_interval_steps: Number(form.checkpointIntervalSteps)
  };
}

function buildCreatePayload(form: TrainingFormState): TrainingJobCreateRequest {
  const defaults = resolvedTrainingDefaults(form);
  return {
    model_family_slug: form.modelFamilySlug,
    dataset_snapshot_id: form.snapshotId,
    task_type: form.taskType,
    architecture: formArchitecture(form),
    model_template_key: form.modelTemplateKey,
    model_template_version: null,
    recipe_name: form.recipeName,
    recipe_version: form.recipeVersion,
    max_epochs: Number(form.maxEpochs),
    learning_rate: Number(form.learningRate),
    image_size: form.imageSize ? Number(form.imageSize) : null,
    batch_size: form.batchSize ? Number(form.batchSize) : null,
    optimizer: form.optimizer || null,
    optimizer_settings: form.optimizerSettings,
    checkpoint_interval_steps: Number(form.checkpointIntervalSteps),
    parameter_overrides: {
      epochs: Number(form.maxEpochs),
      learning_rate: Number(form.learningRate),
      image_size: form.imageSize ? Number(form.imageSize) : null,
      batch_size: form.batchSize ? Number(form.batchSize) : null,
      optimizer: form.optimizer || null,
      optimizer_settings: form.optimizerSettings,
      checkpoint_interval_steps: Number(form.checkpointIntervalSteps),
      template_key: form.modelTemplateKey
    },
    resolved_training_defaults: defaults,
    best_checkpoint_rule: buildBestCheckpointRule(form),
    promotion_criteria: buildPromotionCriteria(form),
    allow_duplicate_snapshot_training: form.allowDuplicateSnapshotTraining,
    idempotency_key: crypto.randomUUID()
  };
}

function applyTemplateDefaults(
  current: TrainingFormState,
  template: ModelTemplateSummary | undefined
): TrainingFormState {
  if (!template) {
    return current;
  }
  const defaults = template.defaults.training;
  const recipe = recipeForArchitecture(template.task_type, template.architecture);
  return {
    ...current,
    taskType: template.task_type,
    modelTemplateKey: template.template_key,
    architectureFamily: template.architecture.family,
    architectureVersion: template.architecture.version,
    architectureSize: template.architecture.size,
    recipeName: recipe.recipeName,
    recipeVersion: recipe.recipeVersion,
    maxEpochs: defaults.epochs,
    learningRate: defaults.learning_rate,
    imageSize: defaults.image_size,
    batchSize: defaults.batch_size,
    optimizer: defaults.optimizer,
    optimizerSettings: defaults.optimizer_settings,
    checkpointIntervalSteps: defaults.checkpoint_interval_steps
  };
}

function architectureLabelFromFields(form: Pick<TrainingFormState, 'architectureFamily' | 'architectureVersion' | 'architectureSize'>) {
  const family = form.architectureFamily.toLowerCase();
  const sizeMap: Record<string, string> = { n: 'Nano', s: 'Small', m: 'Medium', l: 'Large', x: 'X-Large' };
  if (family === 'yolo') {
    return `YOLO${form.architectureVersion.toLowerCase()} ${sizeMap[form.architectureSize] ?? form.architectureSize.toUpperCase()}`;
  }
  if (family === 'resnet') {
    return `ResNet-${form.architectureSize.toUpperCase()}`;
  }
  if (family === 'mobilenet') {
    return `MobileNet${form.architectureVersion.toUpperCase()} ${form.architectureSize}`;
  }
  if (family === 'efficientnet') {
    return `EfficientNet-${form.architectureSize.toUpperCase()}`;
  }
  if (family === 'patchcore') {
    return `PatchCore ${form.architectureSize.replaceAll('_', ' ')}`;
  }
  return `${form.architectureFamily.toUpperCase()} ${form.architectureVersion.toUpperCase()} ${form.architectureSize.toUpperCase()}`;
}

function formatDate(value?: string | null) {
  if (!value) {
    return 'n/a';
  }
  return new Intl.DateTimeFormat('en-GB', {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(new Date(value));
}

function statusTone(status: TrainingJobStatus): 'neutral' | 'success' | 'warn' {
  if (status === 'completed') {
    return 'success';
  }
  if (status === 'failed' || status === 'cancelled' || status === 'rolled_back_if_needed') {
    return 'warn';
  }
  return 'neutral';
}

function isTerminalStatus(status: TrainingJobStatus) {
  return ['completed', 'failed', 'cancelled', 'rolled_back_if_needed'].includes(status);
}

function canResume(job: TrainingJobDetail | undefined) {
  return Boolean(
    job &&
      ['failed', 'cancelled'].includes(job.status) &&
      job.latest_checkpoint_id
  );
}

function trainingHeadline(status: TrainingJobStatus) {
  switch (status) {
    case 'queued':
      return 'Queued for background execution';
    case 'waiting_for_gpu':
      return 'Waiting for a safe GPU reservation';
    case 'preparing_data':
      return 'Preparing immutable training workspace';
    case 'validating_snapshot':
      return 'Validating snapshot before trainer startup';
    case 'initializing':
      return 'Initializing trainer runtime';
    case 'training':
      return 'Training actively on the reserved GPU';
    case 'checkpointing':
      return 'Persisting a resume-safe checkpoint';
    case 'evaluating':
      return 'Running offline evaluation';
    case 'exporting':
      return 'Exporting immutable runtime artifacts';
    case 'registering':
      return 'Registering the trained model version';
    case 'resuming':
      return 'Restoring from the latest good checkpoint';
    case 'cancel_requested':
      return 'Graceful stop requested at the next safe boundary';
    case 'completed':
      return 'Training completed and artifacts were registered';
    case 'failed':
      return 'Training failed and needs review';
    case 'cancelled':
      return 'Training stopped cleanly with checkpoint preservation';
    case 'rolled_back_if_needed':
      return 'A partial downstream side effect was rolled back';
    default:
      return status;
  }
}

function datasetLabel(dataset: DatasetSummary) {
  return `${dataset.name} (${taskTypeLabel(dataset.task_type)})`;
}

export function TrainingPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [form, setForm] = useState<TrainingFormState>(defaultForm('detection'));
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [cleanupOpen, setCleanupOpen] = useState(false);

  const jobsQuery = useQuery({
    queryKey: ['training-jobs'],
    queryFn: api.training.list,
    refetchInterval: 5000
  });
  const datasetsQuery = useQuery({
    queryKey: ['datasets'],
    queryFn: api.datasets.list,
    refetchInterval: 15000
  });
  const familiesQuery = useQuery({
    queryKey: ['model-families'],
    queryFn: api.registry.families,
    refetchInterval: 30000
  });
  const allTemplatesQuery = useQuery({
    queryKey: ['training-model-templates-all'],
    queryFn: () => api.pipelines.modelTemplates(),
    refetchInterval: 30000
  });
  const templatesQuery = useQuery({
    queryKey: ['training-model-templates', form.taskType],
    queryFn: () => api.pipelines.modelTemplates(form.taskType),
    refetchInterval: 30000
  });
  const selectedJobQuery = useQuery({
    queryKey: ['training-job', selectedJobId],
    queryFn: () => api.training.detail(selectedJobId!),
    enabled: Boolean(selectedJobId),
    refetchInterval: 2000
  });
  const comparisonQuery = useQuery({
    queryKey: ['training-compare', compareIds],
    queryFn: () => api.training.compare(compareIds),
    enabled: compareIds.length > 1,
    refetchInterval: 4000
  });
  const cleanupPreviewQuery = useQuery({
    queryKey: ['training-job-cleanup-preview', selectedJobId],
    queryFn: () => api.training.cleanupPreview(selectedJobId as string),
    enabled: cleanupOpen && Boolean(selectedJobId)
  });

  const completedJobs = jobsQuery.data?.items.filter((job) => job.status === 'completed') ?? [];
  const selectedJob = selectedJobQuery.data;
  const selectedJobSummary = selectedJobId
    ? jobsQuery.data?.items.find((job) => job.training_job_id === selectedJobId)
    : undefined;
  const selectedJobForAction = selectedJob ?? selectedJobSummary;
  const selectedJobActionStatus = selectedJobForAction?.status;
  const canCancelSelectedJob = Boolean(
    selectedJobId && (!selectedJobActionStatus || !isTerminalStatus(selectedJobActionStatus))
  );
  const shouldForceCancelSelectedJob = !selectedJob || selectedJobActionStatus === 'cancel_requested';
  const supportedTaskTypes = Array.from(
    new Set((allTemplatesQuery.data?.items ?? []).map((template) => template.task_type))
  );
  const activeDatasets =
    datasetsQuery.data?.items.filter((dataset) => supportedTaskTypes.includes(dataset.task_type)) ?? [];
  const familySuggestions = familiesQuery.data?.items ?? [];
  const selectedTemplate =
    templatesQuery.data?.items.find((template) => template.template_key === form.modelTemplateKey) ??
    templatesQuery.data?.items.find((template) => template.recommended) ??
    templatesQuery.data?.items[0];

  useEffect(() => {
    if (!selectedJobId && jobsQuery.data?.items.length) {
      setSelectedJobId(jobsQuery.data.items[0].training_job_id);
    }
  }, [jobsQuery.data, selectedJobId]);

  useEffect(() => {
    if (!selectedTemplate) {
      return;
    }
    setForm((current) => {
      if (current.modelTemplateKey === selectedTemplate.template_key) {
        return current;
      }
      return applyTemplateDefaults(current, selectedTemplate);
    });
  }, [selectedTemplate]);

  const createMutation = useMutation({
    mutationFn: api.training.create,
    onSuccess: async (accepted) => {
      await queryClient.invalidateQueries({ queryKey: ['training-jobs'] });
      setSelectedJobId(accepted.job_id);
      notify({ title: 'Training job queued', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Training job request failed'), tone: 'error' });
    }
  });

  const cancelMutation = useMutation({
    mutationFn: (payload: { trainingJobId: string; force?: boolean }) =>
      api.training.cancel(payload.trainingJobId, {
        reason: payload.force
          ? 'Operator forced cancellation from the admin UI because the run is stale or already waiting to stop.'
          : 'Operator requested a graceful cancellation from the admin UI.',
        force: payload.force ?? false
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['training-jobs'] }),
        queryClient.invalidateQueries({ queryKey: ['training-job', selectedJobId] })
      ]);
      notify({ title: 'Cancellation requested', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Cancellation request failed'), tone: 'error' });
    }
  });

  const resumeMutation = useMutation({
    mutationFn: (trainingJobId: string) =>
      api.training.resume(trainingJobId, {
        reason: 'Operator requested a resume from the latest good checkpoint.'
      }),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['training-jobs'] }),
        queryClient.invalidateQueries({ queryKey: ['training-job', selectedJobId] })
      ]);
      notify({ title: 'Resume requested', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Resume request failed'), tone: 'error' });
    }
  });

  const cleanupMutation = useMutation({
    mutationFn: (payload: { confirmation_token: string; reason?: string | null; delete_artifacts?: boolean }) =>
      api.training.cleanup(selectedJobId as string, payload),
    onSuccess: async (result) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['training-jobs'] }),
        queryClient.invalidateQueries({ queryKey: ['training-job', selectedJobId] }),
        queryClient.invalidateQueries({ queryKey: ['model-versions'] }),
        queryClient.invalidateQueries({ queryKey: ['project-pipeline'] })
      ]);
      notify({
        title: result.already_deleted ? 'Training job already removed' : 'Training job cleanup completed',
        description: `${result.deleted_entities.reduce((sum, item) => sum + item.count, 0)} entities affected.`,
        tone: 'success'
      });
      setCleanupOpen(false);
    },
    onError: (error) => {
      notify({ ...describeError(error as Error, 'Training job cleanup failed'), tone: 'error' });
    }
  });

  const liveMetrics = selectedJob
    ? Object.entries(selectedJob.evaluation_summary.metrics).slice(0, 4)
    : [];

  return (
    <div className="panel-stack">
      <div className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Training Plane</p>
          <h2>Start a guided training job from dataset and preset.</h2>
          <p className="subtle-text">
            Snapshot, architecture, recipe, checkpointing, and promotion gates are resolved from the
            selected dataset and template unless an operator opens Advanced overrides.
          </p>
        </div>
        <div className="training-summary-pills">
          <MetricPill label="Jobs" value={String(jobsQuery.data?.items.length ?? 0)} />
          <MetricPill label="Completed" value={String(completedJobs.length)} tone="success" />
          <MetricPill
            label="Active"
            value={String(
              jobsQuery.data?.items.filter((job) => !isTerminalStatus(job.status)).length ?? 0
            )}
          />
        </div>
      </div>

      <div className="training-grid">
        <Panel title="Create Training Job">
          <form
            className="training-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              createMutation.mutate(buildCreatePayload(form));
            }}
          >
            <label>
              <span>Dataset</span>
              <select
                value={form.datasetId}
                onChange={(event) => {
                  const dataset = activeDatasets.find((item) => item.dataset_id === event.target.value);
                  if (!dataset) {
                    setForm((current) => ({ ...current, datasetId: event.target.value }));
                    return;
                  }
                  const nextDefaults = defaultForm(dataset.task_type);
                  setForm((current) => ({
                    ...current,
                    ...nextDefaults,
                    datasetId: dataset.dataset_id,
                    snapshotId: dataset.latest_snapshot_id ?? current.snapshotId,
                    modelFamilySlug: current.modelFamilySlug || nextDefaults.modelFamilySlug
                  }));
                }}
              >
                <option value="">Choose a dataset</option>
                {activeDatasets.map((dataset) => (
                  <option key={dataset.dataset_id} value={dataset.dataset_id}>
                    {datasetLabel(dataset)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Template</span>
              <select
                value={form.modelTemplateKey}
                onChange={(event) => {
                  const template = templatesQuery.data?.items.find((item) => item.template_key === event.target.value);
                  setForm((current) =>
                    applyTemplateDefaults(
                      {
                        ...current,
                        modelTemplateKey: event.target.value
                      },
                      template
                    )
                  );
                }}
              >
                {(templatesQuery.data?.items ?? []).map((template) => (
                  <option key={template.template_key} value={template.template_key}>
                    {template.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Model Family Slug</span>
              <input
                list="training-family-suggestions"
                value={form.modelFamilySlug}
                onChange={(event) =>
                  setForm((current) => ({ ...current, modelFamilySlug: event.target.value }))
                }
                required
              />
            </label>
            <datalist id="training-family-suggestions">
              {familySuggestions.map((family) => (
                <option key={family.model_family_id} value={family.slug} />
              ))}
            </datalist>
            {selectedTemplate ? (
              <div className="training-form-grid__full detail-grid">
                <div className="metric-pill">
                  <span>Visible defaults</span>
                  <strong>{trainingDefaultsSummary(selectedTemplate.defaults.training)}</strong>
                  <small className="subtle-text">{selectedTemplate.description}</small>
                </div>
                <div className="metric-pill">
                  <span>Preset architecture</span>
                  <strong>{architectureLabel(selectedTemplate.architecture)}</strong>
                  <small className="subtle-text">{selectedTemplate.default_training_profile}</small>
                </div>
              </div>
            ) : null}
            <details className="status-header__details training-form-grid__full">
              <summary>Advanced training overrides</summary>
              <div className="training-form-grid">
                <label>
                  <span>Snapshot ID</span>
                  <input
                    value={form.snapshotId}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, snapshotId: event.target.value }))
                    }
                    placeholder="dss_..."
                    required
                  />
                </label>
                <label>
                  <span>Task Type</span>
                  <select
                    value={form.taskType}
                    onChange={(event) => {
                      const nextTaskType = event.target.value as TaskType;
                      const nextDefaults = defaultForm(nextTaskType);
                      setForm((current) => ({
                        ...current,
                        ...nextDefaults,
                        datasetId: current.datasetId,
                        snapshotId: current.snapshotId
                      }));
                    }}
                  >
                    {supportedTaskTypes.map((taskType) => (
                      <option key={taskType} value={taskType}>
                        {taskTypeLabel(taskType)}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Architecture</span>
                  <input
                    value={architectureLabelFromFields(form)}
                    readOnly
                  />
                </label>
                <label>
                  <span>Recipe</span>
                  <input
                    value={`${form.recipeName} / ${form.recipeVersion}`}
                    readOnly
                  />
                </label>
                <label>
                  <span>Epochs</span>
                  <input
                    type="number"
                    min={1}
                    value={form.maxEpochs}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, maxEpochs: Number(event.target.value) }))
                    }
                    required
                  />
                </label>
                <label>
                  <span>Learning Rate</span>
                  <input
                    type="number"
                    step="0.0001"
                    value={form.learningRate}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, learningRate: Number(event.target.value) }))
                    }
                    required
                  />
                </label>
                <label>
                  <span>Image Size</span>
                  <input
                    type="number"
                    min={32}
                    max={4096}
                    step={32}
                    title="Die Bildgröße muss durch 32 teilbar sein."
                    value={form.imageSize ?? ''}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        imageSize: event.target.value ? Number(event.target.value) : null
                      }))
                    }
                  />
                  <small>Vielfaches von 32, z. B. 1184 oder 1216.</small>
                </label>
                <label>
                  <span>Batch Size</span>
                  <input
                    type="number"
                    min={1}
                    value={form.batchSize ?? ''}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        batchSize: event.target.value ? Number(event.target.value) : null
                      }))
                    }
                  />
                </label>
                <label>
                  <span>Optimizer</span>
                  <input
                    value={form.optimizer ?? ''}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        optimizer: event.target.value || null
                      }))
                    }
                  />
                </label>
                <label>
                  <span>Checkpoint Interval</span>
                  <input
                    type="number"
                    min={1}
                    value={form.checkpointIntervalSteps}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        checkpointIntervalSteps: Number(event.target.value)
                      }))
                    }
                    required
                  />
                </label>
                <label>
                  <span>Best Checkpoint Metric</span>
                  <input
                    value={form.bestMetricName}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, bestMetricName: event.target.value }))
                    }
                    required
                  />
                </label>
                <label>
                  <span>Best Metric Mode</span>
                  <select
                    value={form.bestMetricMode}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        bestMetricMode: event.target.value as 'max' | 'min'
                      }))
                    }
                  >
                    <option value="max">max</option>
                    <option value="min">min</option>
                  </select>
                </label>
                <label>
                  <span>Promotion Metric</span>
                  <input
                    value={form.promotionMetricName}
                    onChange={(event) =>
                      setForm((current) => ({ ...current, promotionMetricName: event.target.value }))
                    }
                    required
                  />
                </label>
                <label>
                  <span>Promotion Threshold</span>
                  <input
                    type="number"
                    step="0.01"
                    value={form.promotionThreshold}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        promotionThreshold: Number(event.target.value)
                      }))
                    }
                    required
                  />
                </label>
                <label>
                  <span>Secondary Promotion Metric</span>
                  <input
                    value={form.secondaryPromotionMetricName}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        secondaryPromotionMetricName: event.target.value
                      }))
                    }
                  />
                </label>
                <label>
                  <span>Secondary Threshold</span>
                  <input
                    type="number"
                    step="0.01"
                    value={form.secondaryPromotionThreshold}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        secondaryPromotionThreshold: Number(event.target.value)
                      }))
                    }
                  />
                </label>
                <label className="checkbox-row training-form-grid__full">
                  <input
                    type="checkbox"
                    checked={form.allowDuplicateSnapshotTraining}
                    onChange={(event) =>
                      setForm((current) => ({
                        ...current,
                        allowDuplicateSnapshotTraining: event.target.checked
                      }))
                    }
                  />
                  <span>Allow this snapshot to be trained again even if another active run already uses it.</span>
                </label>
              </div>
            </details>
            {!selectedTemplate ? (
              <div className="inline-alert inline-alert--danger training-form-grid__full">
                <strong>Unsupported task/backend combination</strong>
                <p>
                  No real training template is available for <code>{form.taskType}</code> in this environment.
                </p>
              </div>
            ) : null}
            <div className="actions-row training-form-grid__full">
              <button type="submit" disabled={createMutation.isPending || !selectedTemplate}>
                Queue Training Job
              </button>
              <p className="subtle-text">
                Supported task types are driven by the real backend matrix. The current production path
                exposes only combinations that can produce real checkpoints, evaluation, and export artifacts.
              </p>
            </div>
          </form>
        </Panel>

        <Panel title="Training Jobs">
          <table className="data-table compact-table">
            <thead>
              <tr>
                <th>Job</th>
                <th>Model</th>
                <th>Status</th>
                <th>Started</th>
                <th>Progress</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {jobsQuery.data?.items.map((job) => {
                const selected = selectedJobId === job.training_job_id;
                const canCompare = job.status === 'completed';
                return (
                  <tr
                    key={job.training_job_id}
                    className={selected ? 'training-row training-row--selected' : 'training-row'}
                    onClick={() => setSelectedJobId(job.training_job_id)}
                  >
                    <td>{job.training_job_id}</td>
                    <td>{job.model_family_slug}</td>
                    <td>
                      <span className={`badge badge--${statusTone(job.status)}`}>{job.status}</span>
                    </td>
                    <td>{formatDate(job.started_at ?? job.created_at)}</td>
                    <td>{job.progress_percent.toFixed(1)}%</td>
                    <td onClick={(event) => event.stopPropagation()}>
                      <label className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={compareIds.includes(job.training_job_id)}
                          disabled={!canCompare}
                          onChange={(event) => {
                            setCompareIds((current) =>
                              event.target.checked
                                ? [...current, job.training_job_id]
                                : current.filter((item) => item !== job.training_job_id)
                            );
                          }}
                        />
                        <span>Compare</span>
                      </label>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel
          title="Live Run"
          action={
            selectedJobId ? (
              <div className="actions-row">
                {canCancelSelectedJob ? (
                  <button
                    type="button"
                    className={shouldForceCancelSelectedJob ? 'button--danger' : undefined}
                    onClick={() => {
                      if (
                        confirmConsequentialAction({
                          title: shouldForceCancelSelectedJob
                            ? `Force cancel training job ${selectedJobId}?`
                            : `Cancel training job ${selectedJobId}?`,
                          consequence: shouldForceCancelSelectedJob
                            ? 'The job will be marked cancelled immediately and stale worker ownership will be cleared.'
                            : 'The worker will stop at the next safe checkpoint boundary and the current run will not continue automatically.'
                        })
                      ) {
                        cancelMutation.mutate({
                          trainingJobId: selectedJobId,
                          force: shouldForceCancelSelectedJob
                        });
                      }
                    }}
                    disabled={cancelMutation.isPending}
                  >
                    {shouldForceCancelSelectedJob ? 'Force Cancel' : 'Request Cancel'}
                  </button>
                ) : null}
                {canResume(selectedJob) ? (
                  <button
                    type="button"
                    onClick={() => resumeMutation.mutate(selectedJobId)}
                    disabled={resumeMutation.isPending}
                  >
                    Resume
                  </button>
                ) : null}
                <button type="button" onClick={() => setCleanupOpen(true)}>
                  Cleanup
                </button>
              </div>
            ) : null
          }
        >
          {selectedJob ? (
            <div className="training-detail-stack">
              <div className="training-run-header">
                <div>
                  <h3>{selectedJob.training_job_id}</h3>
                  <p className="subtle-text">{trainingHeadline(selectedJob.status)}</p>
                </div>
                <span className={`badge badge--${statusTone(selectedJob.status)}`}>{selectedJob.status}</span>
              </div>
              <div className="detail-grid">
                <MetricPill label="Epoch" value={`${selectedJob.current_epoch}/${selectedJob.total_epochs}`} />
                <MetricPill label="Progress" value={`${selectedJob.progress_percent.toFixed(1)}%`} />
                <MetricPill label="Architecture" value={architectureLabel(selectedJob.architecture)} tone="info" />
                <MetricPill label="Started" value={formatDate(selectedJob.started_at ?? selectedJob.created_at)} />
              </div>
              <details className="advanced-section">
                <summary>Run identifiers and resolved defaults</summary>
                <div className="detail-grid">
                  <MetricPill label="Step" value={String(selectedJob.current_step)} />
                  <MetricPill label="GPU" value={selectedJob.gpu_device ?? 'unassigned'} />
                  <MetricPill label="Mode" value={selectedJob.trainer_mode ?? 'unknown'} />
                  <MetricPill label="Backend" value={selectedJob.trainer_backend ?? 'unknown'} />
                </div>
                <div className="meta-grid">
                  <div>
                    <strong>Snapshot</strong>
                    <p className="subtle-text">{selectedJob.dataset_snapshot_id}</p>
                  </div>
                  <div>
                    <strong>Recipe</strong>
                    <p className="subtle-text">
                      {selectedJob.recipe_name} / {selectedJob.recipe_version}
                    </p>
                  </div>
                  <div>
                    <strong>Template</strong>
                    <p className="subtle-text">{selectedJob.model_template_key ?? 'custom template'}</p>
                  </div>
                  <div>
                    <strong>Latest Checkpoint</strong>
                    <p className="subtle-text">{selectedJob.latest_checkpoint_id ?? 'not yet written'}</p>
                  </div>
                  <div>
                    <strong>Model Version</strong>
                    <p className="subtle-text">{selectedJob.model_version_id ?? 'not registered yet'}</p>
                  </div>
                  <div>
                    <strong>Artifact Validation</strong>
                    <p className="subtle-text">
                      {String((selectedJob.artifact_validation as Record<string, unknown>)?.status ?? 'pending')}
                    </p>
                  </div>
                  <div>
                    <strong>Checkpoint Count</strong>
                    <p className="subtle-text">{selectedJob.checkpoints.length}</p>
                  </div>
                </div>
                <div className="metric-pill">
                  <span>Resolved defaults</span>
                  <strong>{trainingDefaultsSummary(selectedJob.resolved_training_defaults)}</strong>
                </div>
              </details>
              {selectedJob.last_error ? (
                <div className="inline-alert inline-alert--danger">
                  <strong>{selectedJob.failure_class ?? 'failure'}</strong>
                  <p>{selectedJob.last_error}</p>
                </div>
              ) : null}
              <div className="training-inline-grid">
                <div>
                  <h3>Evaluation</h3>
                  {liveMetrics.length ? (
                    <div className="detail-grid">
                      {liveMetrics.map(([metricName, metricValue]) => (
                        <MetricPill
                          key={metricName}
                          label={metricName}
                          value={metricValue.toFixed(3)}
                          tone={selectedJob.evaluation_summary.promotion_ready ? 'success' : 'neutral'}
                        />
                      ))}
                    </div>
                  ) : (
                    <p className="subtle-text">Evaluation metrics will appear here after offline evaluation finishes.</p>
                  )}
                </div>
                <div>
                  <h3>Promotion Gate</h3>
                  <p className="subtle-text">
                    {selectedJob.evaluation_summary.promotion_ready
                      ? 'All explicit promotion criteria passed.'
                      : 'One or more promotion criteria still need review.'}
                  </p>
                  <details className="advanced-section">
                    <summary>Metric gate details</summary>
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
                        {selectedJob.evaluation_summary.promotion_results.map((result) => (
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
                  </details>
                </div>
              </div>
              <details className="advanced-section">
                <summary>Checkpoints and structured logs</summary>
                <div className="training-inline-grid">
                  <div>
                    <h3>Checkpoints</h3>
                    <table className="data-table compact-table">
                      <thead>
                        <tr>
                          <th>ID</th>
                          <th>Kind</th>
                          <th>Epoch</th>
                          <th>Metric</th>
                          <th>Best</th>
                        </tr>
                      </thead>
                      <tbody>
                        {selectedJob.checkpoints.map((checkpoint) => (
                          <tr key={checkpoint.checkpoint_id}>
                            <td>{checkpoint.checkpoint_id}</td>
                            <td>{checkpoint.checkpoint_kind}</td>
                            <td>{checkpoint.epoch_no}</td>
                            <td>{checkpoint.metric_primary ?? 'n/a'}</td>
                            <td>{checkpoint.is_best ? 'yes' : 'no'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <div>
                    <h3>Structured Logs</h3>
                    <div className="training-log-list">
                      {selectedJob.logs.map((entry) => (
                        <article key={entry.sequence_no} className="training-log">
                          <div className="training-log__meta">
                            <strong>{entry.event_type}</strong>
                            <span>{entry.occurred_at}</span>
                          </div>
                          <p>{entry.message}</p>
                        </article>
                      ))}
                    </div>
                  </div>
                </div>
              </details>
            </div>
          ) : (
            <EmptyState
              title={selectedJobId ? "Run detail unavailable" : "No training job selected"}
              body={
                selectedJobId
                  ? "The detail view did not load, but operator actions for the selected run are still available above."
                  : "Select a run from the table to inspect progress, checkpoints, logs, and evaluation state."
              }
            />
          )}
        </Panel>

        <Panel title="Compare Completed Runs">
          {compareIds.length > 1 && comparisonQuery.data ? (
            <div className="training-detail-stack">
              <p className="subtle-text">
                Recommended run: <strong>{comparisonQuery.data.recommended_job_id ?? 'n/a'}</strong>
              </p>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Metric</th>
                    {comparisonQuery.data.jobs.map((job) => (
                      <th key={job.training_job_id}>{job.training_job_id}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {comparisonQuery.data.metrics.map((metric) => (
                    <tr key={metric.metric_name}>
                      <td>{metric.metric_name}</td>
                      {comparisonQuery.data.jobs.map((job) => (
                        <td key={`${metric.metric_name}-${job.training_job_id}`}>
                          {metric.values[job.training_job_id] ?? 'n/a'}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <EmptyState
              title="Select completed runs"
              body="Use the compare column in the jobs table to choose at least two completed runs for side-by-side evaluation."
            />
          )}
        </Panel>
      </div>

      <CleanupDialog
        open={cleanupOpen}
        title="Cleanup training job"
        preview={cleanupPreviewQuery.data}
        pending={cleanupMutation.isPending}
        onClose={() => setCleanupOpen(false)}
        onConfirm={(payload) => cleanupMutation.mutate(payload)}
      />
    </div>
  );
}
