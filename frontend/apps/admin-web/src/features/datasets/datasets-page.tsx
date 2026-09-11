import { Link } from '@tanstack/react-router';
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import type { LabelProjectSummary, TaskType } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import { formatDate, statusTone } from './presentation';
import { taskTypeLabel } from '../pipelines/modeling';

type SnapshotSort = 'newest' | 'oldest' | 'status';
type UploadMode = 'raw' | 'classified';

export function DatasetsPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [name, setName] = useState('Widget Inspection Set');
  const [slug, setSlug] = useState('widget-inspection-set');
  const [taskType, setTaskType] = useState<TaskType>('classification');
  const [labelSchemaVersion, setLabelSchemaVersion] = useState('1');
  const [selectedDatasetId, setSelectedDatasetId] = useState('');
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const [snapshotReason, setSnapshotReason] = useState('Operator-requested immutable checkpoint for training readiness review.');
  const [search, setSearch] = useState('');
  const [snapshotSort, setSnapshotSort] = useState<SnapshotSort>('newest');
  const [pendingUploads, setPendingUploads] = useState<File[]>([]);
  const [uploadMode, setUploadMode] = useState<UploadMode>('raw');
  const [classifiedUploadProjectId, setClassifiedUploadProjectId] = useState('');
  const [classifiedUploadLabel, setClassifiedUploadLabel] = useState('');
  const [classifiedFolderName, setClassifiedFolderName] = useState('');
  const [activeUploadSessionId, setActiveUploadSessionId] = useState<string | null>(null);
  const [uploadedParts, setUploadedParts] = useState(0);
  const [uploadPhase, setUploadPhase] = useState<'idle' | 'creating_session' | 'uploading' | 'finalizing'>('idle');

  const datasetsQuery = useQuery({
    queryKey: ['datasets'],
    queryFn: api.datasets.list,
    refetchInterval: 15000
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
  const snapshotsQuery = useQuery({
    queryKey: ['all-snapshots'],
    queryFn: () => api.datasets.allSnapshots(),
    refetchInterval: 10000
  });
  const uploadSessionQuery = useQuery({
    queryKey: ['upload-session', activeUploadSessionId],
    queryFn: () => api.datasets.uploadSession(activeUploadSessionId as string),
    enabled: Boolean(activeUploadSessionId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'completed' || status === 'failed' ? false : 2000;
    }
  });

  useEffect(() => {
    if (!selectedDatasetId && datasetsQuery.data?.items[0]) {
      setSelectedDatasetId(datasetsQuery.data.items[0].dataset_id);
    }
  }, [datasetsQuery.data?.items, selectedDatasetId]);

  const mappedProjects = useMemo(
    () =>
      (projectsQuery.data?.items ?? []).filter((project) =>
        selectedDatasetId ? project.dataset_id === selectedDatasetId : Boolean(project.dataset_id)
      ),
    [projectsQuery.data?.items, selectedDatasetId]
  );
  const classificationProjects = useMemo(
    () => mappedProjects.filter((project) => project.task_type === 'classification'),
    [mappedProjects]
  );
  const selectedDataset = useMemo(
    () => datasetsQuery.data?.items.find((item) => item.dataset_id === selectedDatasetId),
    [datasetsQuery.data?.items, selectedDatasetId]
  );
  const creationSchemas = useMemo(
    () => (annotationSchemasQuery.data?.items ?? []).filter((schema) => schema.task_type === taskType),
    [annotationSchemasQuery.data?.items, taskType]
  );
  const selectedClassifiedProject = useMemo(
    () => classificationProjects.find((project) => project.label_project_id === classifiedUploadProjectId),
    [classificationProjects, classifiedUploadProjectId]
  );
  const declaredUploadLabels = useMemo(
    () => extractDeclaredLabels(selectedClassifiedProject?.label_config),
    [selectedClassifiedProject?.label_config]
  );

  useEffect(() => {
    if (!selectedProjectId && mappedProjects[0]) {
      setSelectedProjectId(mappedProjects[0].label_project_id);
    }
  }, [mappedProjects, selectedProjectId]);

  useEffect(() => {
    if (uploadMode !== 'classified') {
      return;
    }
    if (
      !classifiedUploadProjectId ||
      !classificationProjects.some((project) => project.label_project_id === classifiedUploadProjectId)
    ) {
      setClassifiedUploadProjectId(classificationProjects[0]?.label_project_id ?? '');
    }
  }, [classificationProjects, classifiedUploadProjectId, uploadMode]);

  useEffect(() => {
    if (creationSchemas.length && !creationSchemas.some((schema) => schema.annotation_schema_id === labelSchemaVersion)) {
      setLabelSchemaVersion(creationSchemas[0].annotation_schema_id);
    }
  }, [creationSchemas, labelSchemaVersion]);

  const filteredSnapshots = useMemo(() => {
    const normalized = search.trim().toLowerCase();
    const items = (snapshotsQuery.data?.items ?? []).filter((item) => {
      const matchesDataset = selectedDatasetId ? item.dataset_id === selectedDatasetId : true;
      const matchesSearch =
        !normalized ||
        [
          item.snapshot_id,
          item.dataset_id,
          item.source_project_title,
          item.source_label_studio_project_id ? String(item.source_label_studio_project_id) : ''
        ]
          .filter(Boolean)
          .some((value) => value!.toLowerCase().includes(normalized));
      return matchesDataset && matchesSearch;
    });
    return [...items].sort((left, right) => {
      if (snapshotSort === 'oldest') {
        return left.created_at.localeCompare(right.created_at);
      }
      if (snapshotSort === 'status') {
        return `${left.status}-${left.snapshot_no}`.localeCompare(`${right.status}-${right.snapshot_no}`);
      }
      return right.created_at.localeCompare(left.created_at);
    });
  }, [search, selectedDatasetId, snapshotSort, snapshotsQuery.data?.items]);

  const createMutation = useMutation({
    mutationFn: api.datasets.create,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['datasets'] });
      notify({ title: 'Dataset created', tone: 'success' });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Dataset creation failed'), tone: 'error' });
    }
  });

  const snapshotMutation = useMutation({
    mutationFn: () =>
      api.datasets.createSnapshot({
        dataset_id: selectedDatasetId,
        label_project_id: selectedProjectId,
        export_format: 'JSON',
        trigger: 'manual',
        reason: snapshotReason
      }),
    onSuccess: async (accepted) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['all-snapshots'] }),
        queryClient.invalidateQueries({ queryKey: ['datasets'] })
      ]);
      notify({
        title: 'Snapshot request queued',
        description: `Snapshot job ${accepted.job_id} is progressing asynchronously through export, materialization, validation, and DVC tracking.`,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Snapshot request failed'), tone: 'error' });
    }
  });
  const uploadMutation = useMutation({
    mutationFn: async () => {
      if (!selectedDatasetId) {
        throw new Error('Choose a dataset before starting an upload session.');
      }
      if (!pendingUploads.length) {
        throw new Error('Choose one or more image files for this upload session.');
      }
      if (selectedDataset?.task_type === 'temporal_sequence') {
        throw new Error('Temporal sequence import is not available in this phase.');
      }
      if (uploadMode === 'classified') {
        if (selectedDataset?.task_type !== 'classification') {
          throw new Error('Pre-classified folder uploads require a classification dataset.');
        }
        if (!classifiedUploadProjectId) {
          throw new Error('Choose the target Label Studio classification project for this folder.');
        }
        if (!classifiedUploadLabel.trim()) {
          throw new Error('Enter the classification label for this folder.');
        }
      }

      setUploadedParts(0);
      setUploadPhase('creating_session');
      const session = await api.datasets.createUploadSession(selectedDatasetId, {
        expected_file_count: pendingUploads.length,
        target_label_project_id: uploadMode === 'classified' ? classifiedUploadProjectId : undefined,
        classification_label: uploadMode === 'classified' ? classifiedUploadLabel.trim() : undefined,
        source_folder_name: uploadMode === 'classified' ? classifiedFolderName || classifiedUploadLabel.trim() : undefined
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
        queryClient.invalidateQueries({ queryKey: ['datasets'] })
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
        description: `${session.received_file_count} files were materialized into immutable dataset storage for ${session.dataset_id}.`,
        tone: 'success'
      });
      void Promise.all([
        queryClient.invalidateQueries({ queryKey: ['datasets'] }),
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
  }, [notify, queryClient, uploadPhase, uploadSessionQuery.data]);

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Dataset Snapshots</p>
          <h2>Upload browser assets, then create, validate, and inspect immutable dataset snapshots without leaving the operator UI.</h2>
          <p className="subtle-text">
            Snapshot requests are durable control-plane operations: the backend writes the request
            first, then exports from Label Studio, materializes the immutable dataset, validates it,
            and tracks it for reproducibility.
          </p>
        </div>
      </section>

      <Panel title="Browser Upload Sessions">
        <div className="panel-stack">
          <div className="training-form-grid">
            <label>
              <span>Target Dataset</span>
              <select value={selectedDatasetId} onChange={(event) => setSelectedDatasetId(event.target.value)}>
                <option value="">Choose a dataset</option>
                {datasetsQuery.data?.items.map((item) => (
                  <option key={item.dataset_id} value={item.dataset_id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Upload Mode</span>
              <select
                value={uploadMode}
                onChange={(event) => {
                  setUploadMode(event.target.value as UploadMode);
                  setPendingUploads([]);
                  setUploadedParts(0);
                  setActiveUploadSessionId(null);
                }}
              >
                <option value="raw">unlabeled images</option>
                <option value="classified">classified folder</option>
              </select>
            </label>
            {uploadMode === 'classified' ? (
              <>
                <label>
                  <span>Classification Project</span>
                  <select
                    value={classifiedUploadProjectId}
                    onChange={(event) => setClassifiedUploadProjectId(event.target.value)}
                  >
                    <option value="">Choose a mapped classification project</option>
                    {classificationProjects.map((project) => (
                      <option key={project.label_project_id} value={project.label_project_id}>
                        {project.title}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Folder Label</span>
                  <input
                    value={classifiedUploadLabel}
                    list="classified-upload-labels"
                    onChange={(event) => setClassifiedUploadLabel(event.target.value)}
                    placeholder="Label name"
                  />
                  <datalist id="classified-upload-labels">
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
                key={uploadMode}
                type="file"
                accept="image/*"
                multiple
                disabled={selectedDataset?.task_type === 'temporal_sequence'}
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
                !selectedDatasetId ||
                !pendingUploads.length ||
                selectedDataset?.task_type === 'temporal_sequence' ||
                (uploadMode === 'classified' &&
                  (!classifiedUploadProjectId ||
                    !classifiedUploadLabel.trim() ||
                    selectedDataset?.task_type !== 'classification')) ||
                uploadMutation.isPending
              }
              onClick={() => uploadMutation.mutate()}
            >
              {uploadMode === 'classified' ? 'Upload Classified Folder' : 'Upload Files'}
            </button>
            <span className="subtle-text">
              {selectedDataset?.task_type === 'temporal_sequence'
                ? 'Temporal sequence import is intentionally not available in this phase.'
                : uploadPhase === 'creating_session'
                ? 'Creating a deterministic upload session record before any file transfer starts.'
                : uploadPhase === 'uploading'
                  ? `Uploading ${uploadedParts}/${pendingUploads.length} files through the browser session.`
                  : uploadPhase === 'finalizing'
                    ? 'Background finalization is materializing durable dataset assets and syncing mapped Label Studio projects.'
                    : pendingUploads.length
                      ? uploadMode === 'classified'
                        ? `${pendingUploads.length} files staged as ${classifiedUploadLabel || 'one class'}`
                        : `${pendingUploads.length} files staged for upload`
                      : uploadMode === 'classified'
                        ? 'Choose a class folder to import pre-labeled images.'
                        : 'Choose images to open a resumable upload session.'}
            </span>
          </div>
          {pendingUploads.length ? (
            <div className="file-chip-row">
              {pendingUploads.map((file) => (
                <span key={`${file.name}-${file.size}-${file.lastModified}`} className="badge badge--neutral">
                  {file.name}
                </span>
              ))}
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

      <Panel
        title="Datasets And Snapshot Request"
        action={
          <div className="actions-row">
            <Link className="primary-link" to="/projects">
              Provision Label Studio Project
            </Link>
            <Link className="primary-link" to="/audit">
              Audit Log
            </Link>
          </div>
        }
      >
        <form
          className="training-form-grid"
          onSubmit={(event) => {
            event.preventDefault();
            createMutation.mutate({
              name,
              slug,
              task_type: taskType,
              label_schema_version: labelSchemaVersion
            });
          }}
        >
          <label>
            <span>Dataset Name</span>
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Dataset name" />
          </label>
          <label>
            <span>Slug</span>
            <input value={slug} onChange={(event) => setSlug(event.target.value)} placeholder="dataset-slug" />
          </label>
          <label>
            <span>Task Type</span>
            <select
              value={taskType}
              onChange={(event) => {
                const nextTaskType = event.target.value as TaskType;
                const registeredSchema = annotationSchemasQuery.data?.items.find(
                  (schema) => schema.task_type === nextTaskType
                );
                setTaskType(nextTaskType);
                setLabelSchemaVersion(registeredSchema?.annotation_schema_id ?? '1');
              }}
            >
              <option value="classification">classification</option>
              <option value="detection">detection</option>
              <option value="oriented_detection">oriented_detection</option>
              <option value="segmentation">segmentation</option>
              <option value="anomaly">anomaly</option>
              <option value="temporal_sequence">temporal_sequence</option>
            </select>
          </label>
          <label>
            <span>Schema Version</span>
            {creationSchemas.length ? (
              <select value={labelSchemaVersion} onChange={(event) => setLabelSchemaVersion(event.target.value)}>
                {creationSchemas.map((schema) => (
                  <option key={schema.annotation_schema_id} value={schema.annotation_schema_id}>
                    {schema.title} ({schema.annotation_schema_id})
                  </option>
                ))}
              </select>
            ) : (
              <input
                value={labelSchemaVersion}
                onChange={(event) => setLabelSchemaVersion(event.target.value)}
                placeholder="1"
              />
            )}
          </label>
          <label>
            <span>Snapshot Dataset</span>
            <select value={selectedDatasetId} onChange={(event) => setSelectedDatasetId(event.target.value)}>
              <option value="">Choose a dataset</option>
              {datasetsQuery.data?.items.map((item) => (
                <option key={item.dataset_id} value={item.dataset_id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Mapped Label Studio Project</span>
            <select value={selectedProjectId} onChange={(event) => setSelectedProjectId(event.target.value)}>
              <option value="">Choose a mapped project</option>
              {mappedProjects.map((project) => (
                <option key={project.label_project_id} value={project.label_project_id}>
                  {project.title}
                </option>
              ))}
            </select>
          </label>
          <label className="training-form-grid__full">
            <span>Snapshot Reason</span>
            <input
              value={snapshotReason}
              onChange={(event) => setSnapshotReason(event.target.value)}
              placeholder="Why this snapshot is being captured"
            />
          </label>
          <div className="actions-row">
            <button type="submit" disabled={createMutation.isPending}>
              Create Dataset
            </button>
            <button
              type="button"
              disabled={
                !selectedDatasetId ||
                !selectedProjectId ||
                selectedDataset?.task_type === 'temporal_sequence' ||
                snapshotMutation.isPending
              }
              onClick={() => snapshotMutation.mutate()}
            >
              Create Snapshot
            </button>
          </div>
        </form>

        <table className="data-table">
          <thead>
            <tr>
              <th>Dataset</th>
              <th>Task</th>
              <th>Status</th>
              <th>Projects</th>
              <th>Snapshots</th>
              <th>Latest Snapshot</th>
            </tr>
          </thead>
          <tbody>
            {datasetsQuery.data?.items.map((item) => (
              <tr key={item.dataset_id}>
                <td>
                  <strong>{item.name}</strong>
                  <div className="subtle-text">{item.slug}</div>
                </td>
                <td>{taskTypeLabel(item.task_type)}</td>
                <td>
                  <span className={`badge badge--${statusTone(item.status)}`}>{item.status}</span>
                </td>
                <td>{item.project_count}</td>
                <td>{item.snapshot_count}</td>
                <td>
                  {item.latest_snapshot_id ? (
                    <Link to="/datasets/snapshots/$snapshotId" params={{ snapshotId: item.latest_snapshot_id }}>
                      {item.latest_snapshot_id}
                    </Link>
                  ) : (
                    'No snapshots yet'
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <Panel
        title="Snapshot Inventory"
        action={
          <div className="actions-row">
            <input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Filter by snapshot, dataset, or project"
            />
            <select value={snapshotSort} onChange={(event) => setSnapshotSort(event.target.value as SnapshotSort)}>
              <option value="newest">Newest first</option>
              <option value="oldest">Oldest first</option>
              <option value="status">Status</option>
            </select>
          </div>
        }
      >
        <table className="data-table">
          <thead>
            <tr>
              <th>Snapshot</th>
              <th>Status</th>
              <th>Validation</th>
              <th>Items</th>
              <th>Eligible</th>
              <th>Background</th>
              <th>Annotations</th>
              <th>Source Project</th>
              <th>Created</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {filteredSnapshots.map((item) => (
              <tr key={item.snapshot_id}>
                <td>
                  <strong>#{item.snapshot_no}</strong>
                  <div className="subtle-text">{item.snapshot_id}</div>
                </td>
                <td>
                  <span className={`badge badge--${statusTone(item.status)}`}>{item.status}</span>
                </td>
                <td>{item.validation_status ?? 'pending'}</td>
                <td>{item.item_count}</td>
                <td>{item.eligible_item_count}</td>
                <td>{item.submitted_background_item_count}</td>
                <td>{item.annotation_count}</td>
                <td>
                  {item.source_project_title ?? 'n/a'}
                  <div className="subtle-text">LS #{item.source_label_studio_project_id ?? 'n/a'}</div>
                </td>
                <td>{formatDate(item.created_at)}</td>
                <td>
                  <div className="table-actions">
                    <Link to="/datasets/snapshots/$snapshotId" params={{ snapshotId: item.snapshot_id }}>
                      Details
                    </Link>
                    <Link
                      to="/datasets/snapshots/$snapshotId/validation"
                      params={{ snapshotId: item.snapshot_id }}
                    >
                      Validation
                    </Link>
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
