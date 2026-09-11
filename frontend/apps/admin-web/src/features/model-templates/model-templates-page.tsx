import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useNavigate, useSearch } from '@tanstack/react-router';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  ModelTemplateCreateRequest,
  ModelTemplateSummary,
  ModelTemplateUpdateRequest,
  PromotionCriteriaPolicy,
  SupportedModelArchitecture,
  TaskType,
  TrainingDefaults
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import {
  architectureLabel,
  architectureSecondaryLabel,
  promotionPolicySummary,
  taskTypeLabel,
  taskTypeOptions,
  templateDefaultsSummary
} from '../pipelines/modeling';

type TaskFilter = TaskType | 'all';

type TemplateEditorDraft = {
  template_key: string;
  label: string;
  description: string;
  task_type: TaskType;
  architecture_family: string;
  architecture_version: string;
  architecture_size: string;
  display_name_suffix: string;
  default_training_profile: string;
  defaults: TrainingDefaults;
  default_promotion_policy: PromotionCriteriaPolicy;
  recommended: boolean;
  active: boolean;
};

const defaultDraft: TemplateEditorDraft = {
  template_key: 'detection-yolov8-n-custom',
  label: 'YOLOv8 Nano Detector Custom',
  description: 'Operator-managed preset derived from the supported production architecture matrix.',
  task_type: 'detection',
  architecture_family: 'yolo',
  architecture_version: 'v8',
  architecture_size: 'n',
  display_name_suffix: 'Detector',
  default_training_profile: 'safe-default',
  defaults: {
    epochs: 40,
    learning_rate: 0.01,
    image_size: 640,
    batch_size: 16,
    optimizer: 'sgd',
    optimizer_settings: { momentum: 0.937, weight_decay: 0.0005 },
    checkpoint_interval_steps: 25
  },
  default_promotion_policy: {
    allow_model_override: true,
    criteria: [
      { metric_name: 'map_50_95', comparison: 'gte', threshold: 0.55, phase: 'evaluation' },
      { metric_name: 'map_50', comparison: 'gte', threshold: 0.72, phase: 'evaluation' }
    ]
  },
  recommended: false,
  active: true
};

export function ModelTemplatesPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const navigate = useNavigate();
  const search = useSearch({ from: '/authenticated/operations/model-templates' });
  const [taskFilter, setTaskFilter] = useState<TaskFilter>('all');
  const [editorOpen, setEditorOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<'create' | 'edit'>('create');
  const [selectedTemplateKey, setSelectedTemplateKey] = useState<string | null>(search.templateKey ?? null);
  const [openedSearchTemplateKey, setOpenedSearchTemplateKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<TemplateEditorDraft>(defaultDraft);

  const templatesQuery = useQuery({
    queryKey: ['model-templates', taskFilter],
    queryFn: () => api.pipelines.modelTemplates(taskFilter === 'all' ? undefined : taskFilter)
  });

  const capabilitiesQuery = useQuery({
    queryKey: ['model-capabilities', taskFilter],
    queryFn: () => api.pipelines.modelCapabilities(taskFilter === 'all' ? undefined : taskFilter)
  });

  const templates = templatesQuery.data?.items ?? [];
  const selectedTemplate =
    templates.find((template) => template.template_key === selectedTemplateKey) ?? templates[0] ?? null;

  useEffect(() => {
    if (search.templateKey) {
      setSelectedTemplateKey(search.templateKey);
    }
  }, [search.templateKey]);

  const architectureOptions = useMemo(() => {
    const distinct = new Map<string, SupportedModelArchitecture['architecture']>();
    for (const capability of capabilitiesQuery.data?.items ?? []) {
      if (!capability.supported || capability.status !== 'supported') {
        continue;
      }
      const architecture = capability.architecture;
      const key = `${architecture.task_type}:${architecture.family}:${architecture.version}:${architecture.size}`;
      if (!distinct.has(key)) {
        distinct.set(key, architecture);
      }
    }
    return [...distinct.values()].sort((left, right) => left.display_name.localeCompare(right.display_name));
  }, [capabilitiesQuery.data?.items]);

  const filteredArchitectureOptions = architectureOptions.filter(
    (item) => item.task_type === draft.task_type
  );

  const invalidateTemplates = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['model-templates'] }),
      queryClient.invalidateQueries({ queryKey: ['project-pipeline'] }),
      queryClient.invalidateQueries({ queryKey: ['project-pipelines'] })
    ]);
  };

  const returnToPipeline = async () => {
    if (!search.returnToLabelProjectId) {
      return;
    }
    await navigate({
      to: '/pipelines/$labelProjectId',
      params: { labelProjectId: search.returnToLabelProjectId }
    });
  };

  const createMutation = useMutation({
    mutationFn: (payload: ModelTemplateCreateRequest) => api.pipelines.createModelTemplate(payload),
    onSuccess: async (item) => {
      await invalidateTemplates();
      setSelectedTemplateKey(item.template_key);
      setEditorOpen(false);
      notify({ title: 'Template created', description: item.label, tone: 'success' });
      await returnToPipeline();
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Template creation failed'), tone: 'error' });
    }
  });

  const updateMutation = useMutation({
    mutationFn: (payload: { templateKey: string; body: ModelTemplateUpdateRequest }) =>
      api.pipelines.updateModelTemplate(payload.templateKey, payload.body),
    onSuccess: async (item) => {
      await invalidateTemplates();
      setSelectedTemplateKey(item.template_key);
      setEditorOpen(false);
      notify({ title: 'Template updated', description: item.label, tone: 'success' });
      await returnToPipeline();
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Template update failed'), tone: 'error' });
    }
  });

  const openCreate = () => {
    const firstArchitecture =
      architectureOptions.find((item) => item.task_type === (taskFilter === 'all' ? 'classification' : taskFilter)) ??
      architectureOptions[0];
    setEditorMode('create');
    setDraft({
      ...defaultDraft,
      task_type: (taskFilter === 'all' ? firstArchitecture?.task_type : taskFilter) ?? 'classification',
      architecture_family: firstArchitecture?.family ?? defaultDraft.architecture_family,
      architecture_version: firstArchitecture?.version ?? defaultDraft.architecture_version,
      architecture_size: firstArchitecture?.size ?? defaultDraft.architecture_size,
      defaults: defaultTrainingDefaultsForTask(
        (taskFilter === 'all' ? firstArchitecture?.task_type : taskFilter) ?? 'classification'
      ),
      default_promotion_policy: defaultPromotionPolicyForTask(
        (taskFilter === 'all' ? firstArchitecture?.task_type : taskFilter) ?? 'classification'
      )
    });
    setEditorOpen(true);
  };

  const openEdit = (template: ModelTemplateSummary) => {
    setSelectedTemplateKey(template.template_key);
    setEditorMode('edit');
    setDraft({
      template_key: template.template_key,
      label: template.label,
      description: template.description,
      task_type: template.task_type,
      architecture_family: template.architecture.family,
      architecture_version: template.architecture.version,
      architecture_size: template.architecture.size,
      display_name_suffix: template.display_name_suffix,
      default_training_profile: template.default_training_profile,
      defaults: template.defaults.training,
      default_promotion_policy: template.default_promotion_policy,
      recommended: template.recommended,
      active: template.active
    });
    setEditorOpen(true);
  };

  useEffect(() => {
    if (!search.templateKey) {
      setOpenedSearchTemplateKey(null);
      return;
    }
    const template = templates.find((item) => item.template_key === search.templateKey);
    if (!template) {
      if (taskFilter !== 'all') {
        setTaskFilter('all');
      }
      return;
    }
    if (openedSearchTemplateKey === search.templateKey) {
      return;
    }
    openEdit(template);
    setOpenedSearchTemplateKey(search.templateKey);
  }, [openedSearchTemplateKey, search.templateKey, taskFilter, templates]);

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Model Templates</p>
          <h2>Manage operator presets without exposing framework internals.</h2>
          <p className="subtle-text">
            Templates define the visible defaults for future model creation and automated training.
            Runtime backend compatibility is still validated when a training job starts.
          </p>
        </div>
        <div className="training-summary-pills">
          <MetricPill label="Templates" value={String(templates.length)} />
          <MetricPill
            label="Recommended"
            value={String(templates.filter((item) => item.recommended).length)}
            tone="success"
          />
          <MetricPill
            label="Segmentation"
            value={String(templates.filter((item) => item.task_type === 'segmentation').length)}
            tone="info"
          />
          <MetricPill
            label="Anomaly"
            value={String(templates.filter((item) => item.task_type === 'anomaly').length)}
            tone="info"
          />
          <MetricPill label="Supported variants" value={String(architectureOptions.length)} />
        </div>
      </section>

      <Panel
        title="Template Catalog"
        action={
          <div className="actions-row">
            <select value={taskFilter} onChange={(event) => setTaskFilter(event.target.value as TaskFilter)}>
              <option value="all">all task types</option>
              {taskTypeOptions.map((taskType) => (
                <option key={taskType} value={taskType}>
                  {taskTypeLabel(taskType)}
                </option>
              ))}
            </select>
            <button type="button" onClick={openCreate}>
              Create template
            </button>
          </div>
        }
      >
        {!templates.length ? (
          <EmptyState
            title="No templates available"
            body="Template definitions will appear here once the backend exposes supported operator presets."
          />
        ) : (
          <div className="pipeline-grid">
            {templates.map((template) => (
              <article key={template.template_key} className="pipeline-card">
                <div className="status-card__header">
                  <div>
                    <p className="training-hero__eyebrow">{taskTypeLabel(template.task_type)}</p>
                    <h3>{template.label}</h3>
                  </div>
                  <span className={`badge badge--${template.recommended ? 'success' : template.active ? 'info' : 'warn'}`}>
                    {template.recommended ? 'Recommended' : template.active ? 'Active' : 'Inactive'}
                  </span>
                </div>

                <p className="subtle-text">{template.description}</p>
                <div className="detail-grid">
                  <div className="metric-pill">
                    <span>Architecture</span>
                    <strong>{architectureLabel(template.architecture)}</strong>
                    <small className="subtle-text">{architectureSecondaryLabel(template.architecture)}</small>
                  </div>
                  <div className="metric-pill">
                    <span>Defaults</span>
                    <strong>{templateDefaultsSummary(template.defaults)}</strong>
                    <small className="subtle-text">{template.default_training_profile}</small>
                  </div>
                  <div className="metric-pill">
                    <span>Promotion gate</span>
                    <strong>{promotionPolicySummary(template.default_promotion_policy)}</strong>
                    <small className="subtle-text">Default deployment gating for new models.</small>
                  </div>
                  <div className="metric-pill">
                    <span>Version</span>
                    <strong>{template.template_version}</strong>
                    <small className="subtle-text">{template.template_key}</small>
                  </div>
                </div>

                <div className="pipeline-card__footer">
                  <span>{template.display_name_suffix}</span>
                  <button type="button" onClick={() => openEdit(template)}>
                    Edit defaults
                  </button>
                </div>
              </article>
            ))}
          </div>
        )}
      </Panel>

      <div className="training-grid training-grid--details">
        <Panel title="Selected Template">
          {!selectedTemplate ? (
            <EmptyState
              title="Choose a template"
              body="Select or create a template to inspect its architecture, defaults, and operator-facing presentation."
            />
          ) : (
            <div className="panel-stack">
              <div className="detail-grid">
                <MetricPill label="Task" value={taskTypeLabel(selectedTemplate.task_type)} tone="info" />
                <MetricPill label="Architecture" value={architectureLabel(selectedTemplate.architecture)} />
                <MetricPill
                  label="Template version"
                  value={String(selectedTemplate.template_version)}
                  tone="neutral"
                />
                <MetricPill
                  label="Recommended"
                  value={selectedTemplate.recommended ? 'yes' : 'no'}
                  tone={selectedTemplate.recommended ? 'success' : 'neutral'}
                />
              </div>
              <div className="detail-grid">
                <div className="metric-pill">
                  <span>Defaults</span>
                  <strong>{templateDefaultsSummary(selectedTemplate.defaults)}</strong>
                  <small className="subtle-text">Training jobs capture the exact resolved values used at runtime.</small>
                </div>
                <div className="metric-pill">
                  <span>Profile</span>
                  <strong>{selectedTemplate.default_training_profile}</strong>
                  <small className="subtle-text">{selectedTemplate.display_name_suffix}</small>
                </div>
                <div className="metric-pill">
                  <span>Promotion criteria</span>
                  <strong>{promotionPolicySummary(selectedTemplate.default_promotion_policy)}</strong>
                  <small className="subtle-text">
                    New models inherit this policy unless model-level overrides are allowed.
                  </small>
                </div>
                <div className="metric-pill">
                  <span>Updated</span>
                  <strong>Version {selectedTemplate.template_version}</strong>
                  <small className="subtle-text">Changes are audited and affect future runs only.</small>
                </div>
              </div>
              <details className="advanced-section">
                <summary>Raw template JSON</summary>
                <pre className="code-block">{JSON.stringify(selectedTemplate, null, 2)}</pre>
              </details>
            </div>
          )}
        </Panel>

        <Panel title="Backend Capability Matrix">
          <div className="panel-stack">
            <p className="subtle-text">
              These variants come from the backend capability source. Normal operators only see entries that are
              marked supported across training, export, deployment, and inference.
            </p>
            <div className="pipeline-template-grid">
              {(capabilitiesQuery.data?.items ?? []).map((capability) => (
                <article
                  key={`${capability.architecture.task_type}:${capability.architecture.family}:${capability.architecture.version}:${capability.architecture.size}`}
                  className="status-card"
                >
                  <h3>{architectureLabel(capability.architecture)}</h3>
                  <p className="subtle-text">{taskTypeLabel(capability.architecture.task_type)}</p>
                  <p className="subtle-text">{capability.trainer_backend}</p>
                  <p className="subtle-text">{architectureSecondaryLabel(capability.architecture)}</p>
                </article>
              ))}
            </div>
          </div>
        </Panel>
      </div>

      {editorOpen ? (
        <div className="modal-backdrop" onClick={() => setEditorOpen(false)}>
          <div className="modal-panel" onClick={(event) => event.stopPropagation()}>
            <div className="panel__header">
              <div>
                <h2>{editorMode === 'create' ? 'Create model template' : 'Edit model template'}</h2>
                <p className="subtle-text">
                  Operators see these defaults in the project happy path. Technical complexity stays centralized here.
                </p>
              </div>
              <button type="button" onClick={() => setEditorOpen(false)}>
                Close
              </button>
            </div>

            <form
              className="training-form-grid"
              onSubmit={(event) => {
                event.preventDefault();
                if (editorMode === 'create') {
                  createMutation.mutate(toCreateRequest(draft));
                  return;
                }
                updateMutation.mutate({
                  templateKey: draft.template_key,
                  body: toUpdateRequest(draft)
                });
              }}
            >
              <label>
                <span>Template key</span>
                <input
                  value={draft.template_key}
                  onChange={(event) => setDraft((current) => ({ ...current, template_key: event.target.value }))}
                  readOnly={editorMode === 'edit'}
                />
              </label>
              <label>
                <span>Label</span>
                <input
                  value={draft.label}
                  onChange={(event) => setDraft((current) => ({ ...current, label: event.target.value }))}
                />
              </label>
              <label className="training-form-grid__full">
                <span>Description</span>
                <input
                  value={draft.description}
                  onChange={(event) => setDraft((current) => ({ ...current, description: event.target.value }))}
                />
              </label>
              <label>
                <span>Task type</span>
                <select
                  value={draft.task_type}
                  disabled={editorMode === 'edit'}
                  onChange={(event) => {
                    const nextTaskType = event.target.value as TaskType;
                    const nextArchitecture =
                      architectureOptions.find((item) => item.task_type === nextTaskType) ?? architectureOptions[0];
                    setDraft((current) => ({
                      ...current,
                      task_type: nextTaskType,
                      architecture_family: nextArchitecture?.family ?? current.architecture_family,
                      architecture_version: nextArchitecture?.version ?? current.architecture_version,
                      architecture_size: nextArchitecture?.size ?? current.architecture_size,
                      defaults: defaultTrainingDefaultsForTask(nextTaskType),
                      default_promotion_policy: defaultPromotionPolicyForTask(nextTaskType)
                    }));
                  }}
                >
                  {taskTypeOptions.map((taskType) => (
                    <option key={taskType} value={taskType}>
                      {taskTypeLabel(taskType)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Architecture</span>
                <select
                  value={`${draft.architecture_family}:${draft.architecture_version}:${draft.architecture_size}`}
                  disabled={editorMode === 'edit'}
                  onChange={(event) => {
                    const [family, version, size] = event.target.value.split(':', 3);
                    setDraft((current) => ({
                      ...current,
                      architecture_family: family,
                      architecture_version: version,
                      architecture_size: size
                    }));
                  }}
                >
                  {filteredArchitectureOptions.map((architecture) => (
                    <option
                      key={`${architecture.task_type}:${architecture.family}:${architecture.version}:${architecture.size}`}
                      value={`${architecture.family}:${architecture.version}:${architecture.size}`}
                    >
                      {architectureLabel(architecture)}
                    </option>
                  ))}
                </select>
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={draft.recommended}
                  onChange={(event) => setDraft((current) => ({ ...current, recommended: event.target.checked }))}
                />
                <span>Recommended</span>
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={draft.active}
                  onChange={(event) => setDraft((current) => ({ ...current, active: event.target.checked }))}
                />
                <span>Active</span>
              </label>
              <details className="advanced-section training-form-grid__full">
                <summary>Advanced defaults</summary>
                <div className="training-form-grid">
              <label>
                <span>Display name suffix</span>
                <input
                  value={draft.display_name_suffix}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, display_name_suffix: event.target.value }))
                  }
                />
              </label>
              <label>
                <span>Training profile</span>
                <input
                  value={draft.default_training_profile}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, default_training_profile: event.target.value }))
                  }
                />
              </label>
              <label>
                <span>Epochs</span>
                <input
                  type="number"
                  min={1}
                  value={draft.defaults.epochs}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      defaults: { ...current.defaults, epochs: Number(event.target.value) }
                    }))
                  }
                />
              </label>
              <label>
                <span>Learning rate</span>
                <input
                  type="number"
                  step="0.0001"
                  value={draft.defaults.learning_rate}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      defaults: { ...current.defaults, learning_rate: Number(event.target.value) }
                    }))
                  }
                />
              </label>
              <label>
                <span>Image size</span>
                <input
                  type="number"
                  min={32}
                  max={4096}
                  step={32}
                  title="Die Bildgröße muss durch 32 teilbar sein."
                  value={draft.defaults.image_size ?? ''}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      defaults: {
                        ...current.defaults,
                        image_size: event.target.value ? Number(event.target.value) : null
                      }
                    }))
                  }
                />
                <small>Vielfaches von 32, z. B. 1184 oder 1216.</small>
              </label>
              <label>
                <span>Batch size</span>
                <input
                  type="number"
                  min={1}
                  value={draft.defaults.batch_size ?? ''}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      defaults: {
                        ...current.defaults,
                        batch_size: event.target.value ? Number(event.target.value) : null
                      }
                    }))
                  }
                />
              </label>
              <label>
                <span>Optimizer</span>
                <input
                  value={draft.defaults.optimizer ?? ''}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      defaults: {
                        ...current.defaults,
                        optimizer: event.target.value || null
                      }
                    }))
                  }
                />
              </label>
              <label>
                <span>Checkpoint interval</span>
                <input
                  type="number"
                  min={1}
                  value={draft.defaults.checkpoint_interval_steps}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      defaults: {
                        ...current.defaults,
                        checkpoint_interval_steps: Number(event.target.value)
                      }
                    }))
                  }
                />
              </label>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={draft.default_promotion_policy.allow_model_override}
                  onChange={(event) =>
                    setDraft((current) => ({
                      ...current,
                      default_promotion_policy: {
                        ...current.default_promotion_policy,
                        allow_model_override: event.target.checked
                      }
                    }))
                  }
                />
                <span>Allow model-level promotion override</span>
              </label>
              <div className="training-form-grid__full">
                <span className="field-label">Default promotion criteria</span>
                <div className="pipeline-template-grid">
                  {draft.default_promotion_policy.criteria.map((criterion, index) => (
                    <div key={`${criterion.metric_name}-${index}`} className="metric-pill">
                      <label>
                        <span>Metric</span>
                        <input
                          value={criterion.metric_name}
                          onChange={(event) =>
                            setDraft((current) =>
                              updateDraftPromotionCriterion(current, index, { metric_name: event.target.value })
                            )
                          }
                        />
                      </label>
                      <label>
                        <span>Comparison</span>
                        <select
                          value={criterion.comparison}
                          onChange={(event) =>
                            setDraft((current) =>
                              updateDraftPromotionCriterion(current, index, {
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
                            setDraft((current) =>
                              updateDraftPromotionCriterion(current, index, {
                                threshold: Number(event.target.value)
                              })
                            )
                          }
                        />
                      </label>
                      <label>
                        <span>Phase</span>
                        <select
                          value={criterion.phase}
                          onChange={(event) =>
                            setDraft((current) =>
                              updateDraftPromotionCriterion(current, index, {
                                phase: event.target.value as 'validation' | 'evaluation'
                              })
                            )
                          }
                        >
                          <option value="evaluation">evaluation</option>
                          <option value="validation">validation</option>
                        </select>
                      </label>
                    </div>
                  ))}
                </div>
              </div>
              <label className="training-form-grid__full">
                <span>Optimizer settings (JSON)</span>
                <textarea
                  value={JSON.stringify(draft.defaults.optimizer_settings, null, 2)}
                  onChange={(event) => {
                    try {
                      const value = JSON.parse(event.target.value) as Record<string, unknown>;
                      setDraft((current) => ({
                        ...current,
                        defaults: { ...current.defaults, optimizer_settings: value }
                      }));
                    } catch {
                      return;
                    }
                  }}
                />
              </label>
                </div>
              </details>
              <div className="actions-row training-form-grid__full">
                <button type="submit" disabled={createMutation.isPending || updateMutation.isPending}>
                  {createMutation.isPending || updateMutation.isPending
                    ? 'Saving...'
                    : editorMode === 'create'
                      ? 'Create template'
                      : 'Save template'}
                </button>
              </div>
            </form>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function toCreateRequest(draft: TemplateEditorDraft): ModelTemplateCreateRequest {
  return {
    template_key: draft.template_key.trim(),
    label: draft.label.trim(),
    description: draft.description.trim(),
    task_type: draft.task_type,
    architecture_family: draft.architecture_family,
    architecture_version: draft.architecture_version,
    architecture_size: draft.architecture_size,
    display_name_suffix: draft.display_name_suffix.trim(),
    default_training_profile: draft.default_training_profile.trim(),
    defaults: {
      training: draft.defaults,
      promotion: draft.default_promotion_policy
    },
    default_promotion_policy: draft.default_promotion_policy,
    recommended: draft.recommended,
    active: draft.active
  };
}

function toUpdateRequest(draft: TemplateEditorDraft): ModelTemplateUpdateRequest {
  return {
    label: draft.label.trim(),
    description: draft.description.trim(),
    display_name_suffix: draft.display_name_suffix.trim(),
    default_training_profile: draft.default_training_profile.trim(),
    defaults: {
      training: draft.defaults,
      promotion: draft.default_promotion_policy
    },
    default_promotion_policy: draft.default_promotion_policy,
    recommended: draft.recommended,
    active: draft.active
  };
}

function defaultPromotionPolicyForTask(taskType: TaskType): PromotionCriteriaPolicy {
  if (taskType === 'anomaly') {
    return {
      allow_model_override: true,
      criteria: [
        { metric_name: 'image_auroc', comparison: 'gte', threshold: 0.9, phase: 'evaluation' },
        { metric_name: 'image_f1', comparison: 'gte', threshold: 0.8, phase: 'evaluation' }
      ]
    };
  }
  if (taskType === 'detection' || taskType === 'oriented_detection') {
    return {
      allow_model_override: true,
      criteria: [
        { metric_name: 'map_50_95', comparison: 'gte', threshold: 0.55, phase: 'evaluation' },
        { metric_name: 'map_50', comparison: 'gte', threshold: 0.72, phase: 'evaluation' }
      ]
    };
  }
  if (taskType === 'segmentation') {
    return {
      allow_model_override: true,
      criteria: [
        { metric_name: 'mask_map_50_95', comparison: 'gte', threshold: 0.5, phase: 'evaluation' },
        { metric_name: 'mask_map_50', comparison: 'gte', threshold: 0.7, phase: 'evaluation' }
      ]
    };
  }
  return {
    allow_model_override: true,
    criteria: [
      { metric_name: 'accuracy_top1', comparison: 'gte', threshold: 0.9, phase: 'evaluation' },
      { metric_name: 'f1_macro', comparison: 'gte', threshold: 0.85, phase: 'evaluation' }
    ]
  };
}

function defaultTrainingDefaultsForTask(taskType: TaskType): TrainingDefaults {
  if (taskType === 'anomaly') {
    return {
      epochs: 1,
      learning_rate: 0.001,
      image_size: 256,
      batch_size: 8,
      optimizer: 'anomalib-default',
      optimizer_settings: { method: 'patchcore', backbone: 'wide_resnet50_2' },
      checkpoint_interval_steps: 25
    };
  }
  if (taskType === 'segmentation') {
    return {
      epochs: 50,
      learning_rate: 0.01,
      image_size: 640,
      batch_size: 12,
      optimizer: 'sgd',
      optimizer_settings: { momentum: 0.937, weight_decay: 0.0005 },
      checkpoint_interval_steps: 25
    };
  }
  if (taskType === 'oriented_detection') {
    return {
      epochs: 80,
      learning_rate: 0.01,
      image_size: 1024,
      batch_size: 8,
      optimizer: 'sgd',
      optimizer_settings: { momentum: 0.937, weight_decay: 0.0005 },
      checkpoint_interval_steps: 25
    };
  }
  if (taskType === 'classification') {
    return {
      epochs: 30,
      learning_rate: 0.001,
      image_size: 224,
      batch_size: 32,
      optimizer: 'adamw',
      optimizer_settings: { weight_decay: 0.0005 },
      checkpoint_interval_steps: 20
    };
  }
  return defaultDraft.defaults;
}

function updateDraftPromotionCriterion(
  draft: TemplateEditorDraft,
  index: number,
  patch: Partial<PromotionCriteriaPolicy['criteria'][number]>
): TemplateEditorDraft {
  return {
    ...draft,
    default_promotion_policy: {
      ...draft.default_promotion_policy,
      criteria: draft.default_promotion_policy.criteria.map((criterion, currentIndex) =>
        currentIndex === index ? { ...criterion, ...patch } : criterion
      )
    }
  };
}
