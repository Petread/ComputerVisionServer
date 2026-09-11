import { useEffect, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import type { ClassBalanceStrategy, LabelProjectSummary, TaskType } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import { formatDate, statusTone, stringifyClassDistribution } from './presentation';

const defaultActions = [
  'ANNOTATION_CREATED',
  'ANNOTATION_UPDATED',
  'ANNOTATIONS_CREATED',
  'REVIEW_CREATED',
  'REVIEW_UPDATED'
].join(', ');

export function ProjectMappingPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const datasetsQuery = useQuery({
    queryKey: ['datasets'],
    queryFn: api.datasets.list
  });
  const projectsQuery = useQuery({
    queryKey: ['label-projects'],
    queryFn: api.datasets.projects,
    refetchInterval: 15000
  });
  const annotationSchemasQuery = useQuery({
    queryKey: ['annotation-schemas'],
    queryFn: api.datasets.annotationSchemas
  });

  const [datasetId, setDatasetId] = useState('');
  const [provisionDatasetId, setProvisionDatasetId] = useState('');
  const [annotationSchemaId, setAnnotationSchemaId] = useState('');
  const [provisionTitle, setProvisionTitle] = useState('');
  const [provisionPipelineSlug, setProvisionPipelineSlug] = useState('');
  const [provisionAutoSyncEnabled, setProvisionAutoSyncEnabled] = useState(true);
  const [provisionAutoSnapshotEnabled, setProvisionAutoSnapshotEnabled] = useState(true);
  const [provisionMinimumAnnotationCount, setProvisionMinimumAnnotationCount] = useState('1');
  const [provisionTrainRatio, setProvisionTrainRatio] = useState('80');
  const [provisionValRatio, setProvisionValRatio] = useState('10');
  const [provisionTestRatio, setProvisionTestRatio] = useState('10');
  const [provisionIdempotencyKey, setProvisionIdempotencyKey] = useState('');
  const [labelStudioProjectId, setLabelStudioProjectId] = useState('101');
  const [title, setTitle] = useState('Widget Inspection Labeling');
  const [taskType, setTaskType] = useState<TaskType>('classification');
  const [pipelineSlug, setPipelineSlug] = useState('widget-inspection-baseline');
  const [autoSnapshotEnabled, setAutoSnapshotEnabled] = useState(true);
  const [minimumAnnotationCount, setMinimumAnnotationCount] = useState('25');
  const [requiredClasses, setRequiredClasses] = useState('ok, defect');
  const [trainRatio, setTrainRatio] = useState('80');
  const [valRatio, setValRatio] = useState('10');
  const [testRatio, setTestRatio] = useState('10');
  const [classBalanceStrategy, setClassBalanceStrategy] = useState<ClassBalanceStrategy>('none');
  const [qualifyingActions, setQualifyingActions] = useState(defaultActions);
  const [labelConfigJson, setLabelConfigJson] = useState('{}');
  const [syncStatusMessage, setSyncStatusMessage] = useState<string>('Click refresh to discover Label Studio projects.');
  const hasDatasets = (datasetsQuery.data?.items.length ?? 0) > 0;

  useEffect(() => {
    if (!datasetId && datasetsQuery.data?.items[0]) {
      setDatasetId(datasetsQuery.data.items[0].dataset_id);
    }
  }, [datasetId, datasetsQuery.data]);

  useEffect(() => {
    if (provisionDatasetId || !datasetsQuery.data?.items[0]) {
      return;
    }
    const dataset = datasetsQuery.data.items[0];
    setProvisionDatasetId(dataset.dataset_id);
    setProvisionTitle(`${dataset.name} Labeling`.slice(0, 50));
    setProvisionPipelineSlug(`${dataset.slug}-labeling`.slice(0, 120));
  }, [datasetsQuery.data, provisionDatasetId]);

  const selectedProvisionDataset = datasetsQuery.data?.items.find(
    (dataset) => dataset.dataset_id === provisionDatasetId
  );
  const compatibleSchemas = (annotationSchemasQuery.data?.items ?? []).filter(
    (schema) => schema.task_type === selectedProvisionDataset?.task_type
  );

  useEffect(() => {
    const matchingSchema = compatibleSchemas.find(
      (schema) => schema.annotation_schema_id === selectedProvisionDataset?.label_schema_version
    );
    if (matchingSchema && annotationSchemaId !== matchingSchema.annotation_schema_id) {
      setAnnotationSchemaId(matchingSchema.annotation_schema_id);
    } else if (!matchingSchema && annotationSchemaId) {
      setAnnotationSchemaId('');
    }
  }, [annotationSchemaId, compatibleSchemas, selectedProvisionDataset?.label_schema_version]);

  const syncMutation = useMutation({
    mutationFn: api.datasets.syncProjects,
    onSuccess: async () => {
      setSyncStatusMessage('Project sync queued. Refreshing the discovered project list...');
      await queryClient.invalidateQueries({ queryKey: ['label-projects'] });
      for (const delayMs of [1500, 4000]) {
        window.setTimeout(() => {
          void queryClient.invalidateQueries({ queryKey: ['label-projects'] });
        }, delayMs);
      }
      notify({
        title: 'Project sync queued',
        description: 'Known Label Studio projects will refresh automatically within a few seconds.',
        tone: 'success'
      });
    },
    onError: (error) => {
      setSyncStatusMessage('Project sync failed. Check the error notification for details.');
      notify({ ...describeError(error, 'Project sync failed'), tone: 'error' });
    }
  });

  const mappingMutation = useMutation({
    mutationFn: api.datasets.mapProject,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['label-projects'] }),
        queryClient.invalidateQueries({ queryKey: ['datasets'] })
      ]);
      notify({ title: 'Project mapping saved', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Project mapping failed'), tone: 'error' });
    }
  });

  const provisionMutation = useMutation({
    mutationFn: api.datasets.provisionProject,
    onSuccess: async (accepted) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['label-projects'] }),
        queryClient.invalidateQueries({ queryKey: ['datasets'] })
      ]);
      for (const delayMs of [1500, 4000]) {
        window.setTimeout(() => {
          void queryClient.invalidateQueries({ queryKey: ['label-projects'] });
          void queryClient.invalidateQueries({ queryKey: ['datasets'] });
        }, delayMs);
      }
      notify({
        title: 'Label Studio project provisioning queued',
        description: `Job ${accepted.job_id} will create or update exactly one project mapping.`,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Project provisioning failed'), tone: 'error' });
    }
  });

  const createSnapshotMutation = useMutation({
    mutationFn: api.datasets.createSnapshot,
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
        queryClient.invalidateQueries({ queryKey: ['label-projects'] })
      ]);
      notify({ title: 'Snapshot request queued', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Snapshot request failed'), tone: 'error' });
    }
  });

  function populateFromProject(project: LabelProjectSummary) {
    setLabelStudioProjectId(String(project.label_studio_project_id));
    setTitle(project.title);
    setTaskType(project.task_type);
    setPipelineSlug(project.training_pipeline_slug ?? 'widget-inspection-baseline');
    setAutoSnapshotEnabled(project.auto_snapshot_enabled);
    setMinimumAnnotationCount(String(project.minimum_annotation_count));
    setRequiredClasses(project.required_classes.join(', '));
    setTrainRatio(String(project.train_ratio));
    setValRatio(String(project.val_ratio));
    setTestRatio(String(project.test_ratio));
    setClassBalanceStrategy(project.class_balance_strategy);
    setQualifyingActions((project.qualifying_actions ?? []).join(', ') || defaultActions);
    setDatasetId(project.dataset_id ?? datasetId);
    setLabelConfigJson(JSON.stringify(project.label_config ?? {}, null, 2));
  }

  return (
    <div className="panel-stack">
      <Panel title="Provision Label Studio Project">
        <p className="subtle-text">
          Select a registered annotation schema. The background worker generates and versions the Label Studio
          configuration; no project ID or raw XML is required.
        </p>
        <form
          className="panel-stack"
          onSubmit={(event) => {
            event.preventDefault();
            provisionMutation.mutate({
              dataset_id: provisionDatasetId,
              title: provisionTitle,
              annotation_schema_id: annotationSchemaId,
              training_pipeline_slug: provisionPipelineSlug,
              auto_sync_enabled: provisionAutoSyncEnabled,
              auto_snapshot_enabled: provisionAutoSnapshotEnabled,
              minimum_annotation_count: Number(provisionMinimumAnnotationCount),
              train_ratio: Number(provisionTrainRatio),
              val_ratio: Number(provisionValRatio),
              test_ratio: Number(provisionTestRatio),
              class_balance_strategy: 'none',
              idempotency_key: provisionIdempotencyKey.trim() || undefined
            });
          }}
        >
          <div className="training-form-grid">
            <label>
              <span>Dataset</span>
              <select
                value={provisionDatasetId}
                onChange={(event) => {
                  const nextDataset = datasetsQuery.data?.items.find(
                    (dataset) => dataset.dataset_id === event.target.value
                  );
                  setProvisionDatasetId(event.target.value);
                  setAnnotationSchemaId('');
                  if (nextDataset) {
                    setProvisionTitle(`${nextDataset.name} Labeling`.slice(0, 50));
                    setProvisionPipelineSlug(`${nextDataset.slug}-labeling`.slice(0, 120));
                  }
                }}
              >
                <option value="">Choose a dataset</option>
                {datasetsQuery.data?.items.map((dataset) => (
                  <option key={dataset.dataset_id} value={dataset.dataset_id}>
                    {dataset.name} ({dataset.task_type})
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Registered annotation schema</span>
              <select value={annotationSchemaId} onChange={(event) => setAnnotationSchemaId(event.target.value)}>
                <option value="">Choose a matching schema</option>
                {compatibleSchemas.map((schema) => (
                  <option
                    key={schema.annotation_schema_id}
                    value={schema.annotation_schema_id}
                    disabled={schema.annotation_schema_id !== selectedProvisionDataset?.label_schema_version}
                  >
                    {schema.title} ({schema.annotation_schema_id})
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Project title</span>
              <input
                minLength={3}
                maxLength={50}
                value={provisionTitle}
                onChange={(event) => setProvisionTitle(event.target.value)}
              />
            </label>
            <label>
              <span>Training pipeline slug / recipe reference</span>
              <input
                value={provisionPipelineSlug}
                onChange={(event) => setProvisionPipelineSlug(event.target.value)}
              />
            </label>
            <label>
              <span>Minimum annotations</span>
              <input
                type="number"
                min={0}
                value={provisionMinimumAnnotationCount}
                onChange={(event) => setProvisionMinimumAnnotationCount(event.target.value)}
              />
            </label>
            <label>
              <span>Optional idempotency key</span>
              <input
                value={provisionIdempotencyKey}
                onChange={(event) => setProvisionIdempotencyKey(event.target.value)}
                placeholder="Defaults to dataset + schema"
              />
            </label>
            <label>
              <span>Train split (%)</span>
              <input type="number" min={0} max={100} value={provisionTrainRatio} onChange={(event) => setProvisionTrainRatio(event.target.value)} />
            </label>
            <label>
              <span>Validation split (%)</span>
              <input type="number" min={0} max={100} value={provisionValRatio} onChange={(event) => setProvisionValRatio(event.target.value)} />
            </label>
            <label>
              <span>Test split (%)</span>
              <input type="number" min={0} max={100} value={provisionTestRatio} onChange={(event) => setProvisionTestRatio(event.target.value)} />
            </label>
          </div>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={provisionAutoSyncEnabled}
              onChange={(event) => setProvisionAutoSyncEnabled(event.target.checked)}
            />
            <span>Automatically synchronize project metadata</span>
          </label>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={provisionAutoSnapshotEnabled}
              onChange={(event) => setProvisionAutoSnapshotEnabled(event.target.checked)}
            />
            <span>Automatically request snapshots after qualifying annotation events</span>
          </label>
          {selectedProvisionDataset && !annotationSchemaId ? (
            <div className="inline-alert inline-alert--danger">
              Dataset schema `{selectedProvisionDataset.label_schema_version}` is not registered for task type `
              {selectedProvisionDataset.task_type}`. Create or select a dataset with a registered schema before
              provisioning.
            </div>
          ) : null}
          <div className="actions-row">
            <button
              type="submit"
              disabled={
                provisionMutation.isPending ||
                !provisionDatasetId ||
                !annotationSchemaId ||
                !provisionTitle.trim() ||
                !provisionPipelineSlug.trim() ||
                Number(provisionTrainRatio) + Number(provisionValRatio) + Number(provisionTestRatio) !== 100
              }
            >
              {provisionMutation.isPending ? 'Queueing...' : 'Create Label Studio Project'}
            </button>
          </div>
        </form>
      </Panel>

      <Panel
        title="Legacy Manual Project Mapping"
        action={
          <div className="actions-row">
            <button type="button" onClick={() => syncMutation.mutate()} disabled={syncMutation.isPending}>
              {syncMutation.isPending ? 'Syncing...' : 'Refresh From Label Studio'}
            </button>
            <a className="primary-link" href="/label-studio/" target="_blank" rel="noreferrer">
              Open Label Studio
            </a>
            <span className="subtle-text">
              {syncMutation.isPending
                ? 'Submitting the sync request to the background worker.'
                : projectsQuery.isFetching
                  ? 'Refreshing the known project list.'
                  : syncStatusMessage}
            </span>
          </div>
        }
      >
        <p className="subtle-text">
          This compatibility path remains available for Label Studio projects that already exist. New projects
          should use the schema-driven provisioning form above.
        </p>
        {!hasDatasets ? (
          <div className="inline-alert inline-alert--danger">
            Create a dataset first on the `Dataset Snapshots` page. Until a Label Studio project is mapped to a
            dataset, `Save Mapping` and `Create Snapshot` cannot complete the dataset workflow.
          </div>
        ) : null}
        <form
          className="panel-stack"
          onSubmit={(event) => {
            event.preventDefault();
            let parsedLabelConfig: Record<string, unknown>;
            try {
              parsedLabelConfig = JSON.parse(labelConfigJson) as Record<string, unknown>;
            } catch {
              notify({ title: 'Label config JSON is invalid', tone: 'error' });
              return;
            }
            mappingMutation.mutate({
              dataset_id: datasetId,
              label_studio_project_id: Number(labelStudioProjectId),
              title,
              task_type: taskType,
              training_pipeline_slug: pipelineSlug,
              auto_snapshot_enabled: autoSnapshotEnabled,
              minimum_annotation_count: Number(minimumAnnotationCount),
              required_classes: requiredClasses
                .split(',')
                .map((value) => value.trim())
                .filter(Boolean),
              train_ratio: Number(trainRatio),
              val_ratio: Number(valRatio),
              test_ratio: Number(testRatio),
              class_balance_strategy: classBalanceStrategy,
              qualifying_actions: qualifyingActions
                .split(',')
                .map((value) => value.trim())
                .filter(Boolean),
              label_config: parsedLabelConfig
            });
          }}
        >
          <div className="training-form-grid">
            <label>
              <span>Dataset target (dataset)</span>
              <select value={datasetId} onChange={(event) => setDatasetId(event.target.value)}>
                {datasetsQuery.data?.items.map((dataset) => (
                  <option key={dataset.dataset_id} value={dataset.dataset_id}>
                    {dataset.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Label Studio project (id)</span>
              <input
                value={labelStudioProjectId}
                onChange={(event) => setLabelStudioProjectId(event.target.value)}
              />
            </label>
            <label>
              <span>Project title (name)</span>
              <input value={title} onChange={(event) => setTitle(event.target.value)} />
            </label>
            <label>
              <span>Task type (task)</span>
              <select value={taskType} onChange={(event) => setTaskType(event.target.value as TaskType)}>
                <option value="classification">classification</option>
                <option value="detection">detection</option>
                <option value="oriented_detection">oriented_detection</option>
                <option value="segmentation">segmentation</option>
                <option value="anomaly">anomaly</option>
                <option value="temporal_sequence">temporal_sequence</option>
              </select>
            </label>
            <label>
              <span>Pipeline slug (slug)</span>
              <input value={pipelineSlug} onChange={(event) => setPipelineSlug(event.target.value)} />
            </label>
            <label>
              <span>Minimum annotations (items)</span>
              <input
                type="number"
                min={0}
                value={minimumAnnotationCount}
                onChange={(event) => setMinimumAnnotationCount(event.target.value)}
              />
            </label>
            <label>
              <span>Required classes (labels)</span>
              <input value={requiredClasses} onChange={(event) => setRequiredClasses(event.target.value)} />
            </label>
            <label>
              <span>Class balance strategy (mode)</span>
              <select
                value={classBalanceStrategy}
                onChange={(event) => setClassBalanceStrategy(event.target.value as ClassBalanceStrategy)}
              >
                <option value="none">none</option>
                <option value="weighted_loss">weighted_loss</option>
                <option value="balanced_sampler">balanced_sampler</option>
              </select>
            </label>
            <label>
              <span>Train split (%)</span>
              <input type="number" min={0} max={100} value={trainRatio} onChange={(event) => setTrainRatio(event.target.value)} />
            </label>
            <label>
              <span>Validation split (%)</span>
              <input type="number" min={0} max={100} value={valRatio} onChange={(event) => setValRatio(event.target.value)} />
            </label>
            <label>
              <span>Test split (%)</span>
              <input type="number" min={0} max={100} value={testRatio} onChange={(event) => setTestRatio(event.target.value)} />
            </label>
          </div>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={autoSnapshotEnabled}
              onChange={(event) => setAutoSnapshotEnabled(event.target.checked)}
            />
            <span>Automatically create snapshots after qualifying annotation events</span>
          </label>
          <label>
            <span>Qualifying actions (events)</span>
            <input value={qualifyingActions} onChange={(event) => setQualifyingActions(event.target.value)} />
          </label>
          <label>
            <span>Label config (JSON)</span>
            <textarea
              className="text-area"
              value={labelConfigJson}
              onChange={(event) => setLabelConfigJson(event.target.value)}
            />
          </label>
          <div className="actions-row">
            <button type="submit" disabled={!hasDatasets}>
              Save Mapping
            </button>
            {createSnapshotMutation.data ? (
              <Link
                className="primary-link"
                to="/datasets/snapshots/$snapshotId"
                params={{ snapshotId: createSnapshotMutation.data.job_id }}
              >
                Open Requested Snapshot
              </Link>
            ) : null}
          </div>
        </form>
      </Panel>

      <Panel title="Known Label Studio Projects">
        <table className="data-table">
          <thead>
            <tr>
              <th>Project</th>
              <th>Status</th>
              <th>Dataset</th>
              <th>Pipeline</th>
              <th>Schema</th>
              <th>Annotations</th>
              <th>Distribution</th>
              <th>Balance</th>
              <th>Last Activity</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {!projectsQuery.data?.items.length ? (
              <tr>
                <td colSpan={10} className="subtle-text">
                  No Label Studio projects are known yet. Click `Refresh From Label Studio` and wait a few seconds for
                  the asynchronous sync to complete.
                </td>
              </tr>
            ) : null}
            {projectsQuery.data?.items.map((project) => (
              <tr key={project.label_project_id}>
                <td>
                  <strong>{project.title}</strong>
                  <div className="subtle-text">LS #{project.label_studio_project_id}</div>
                </td>
                <td>
                  <span className={`badge badge--${statusTone(project.status)}`}>{project.status}</span>
                </td>
                <td>{project.dataset_name ?? 'Not mapped yet'}</td>
                <td>{project.training_pipeline_slug ?? 'Unassigned'}</td>
                <td>
                  {project.label_schema_version ?? 'Unversioned'}
                  {project.label_config_sha256 ? (
                    <div className="subtle-text">config {project.label_config_sha256.slice(0, 12)}</div>
                  ) : null}
                </td>
                <td>
                  {project.annotation_count} annotations
                  <div className="subtle-text">
                    {project.annotated_task_count}/{project.task_count} tasks annotated
                  </div>
                </td>
                <td>{stringifyClassDistribution(project.class_distribution)}</td>
                <td>{project.class_balance_strategy}</td>
                <td>{formatDate(project.last_activity_at)}</td>
                <td>
                  <div className="table-actions">
                    <button type="button" onClick={() => populateFromProject(project)}>
                      Use In Form
                    </button>
                    <button
                      type="button"
                      disabled={!project.dataset_id}
                      onClick={() =>
                        createSnapshotMutation.mutate({
                          dataset_id: project.dataset_id ?? '',
                          label_project_id: project.label_project_id,
                          export_format: 'JSON',
                          trigger: 'manual',
                          reason: `Manual snapshot from project ${project.title}`
                        })
                      }
                    >
                      Create Snapshot
                    </button>
                    {!project.dataset_id ? (
                      <div className="subtle-text">
                        Map this project to a dataset first, then save the mapping before creating a snapshot.
                      </div>
                    ) : null}
                    <Link to="/datasets/stats" className="primary-link">
                      Stats
                    </Link>
                    <a
                      className="primary-link"
                      href={`/label-studio/projects/${project.label_studio_project_id}/data`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open Project
                    </a>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
