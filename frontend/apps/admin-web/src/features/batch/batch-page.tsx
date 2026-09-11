import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type {
  BatchInputItem,
  BatchResultRecord,
  ModelCapabilitySummary,
  TaskType
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import { architectureLabel, taskTypeLabel } from '../pipelines/modeling';

type PreviewMap = Record<string, string>;

export function BatchPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [deploymentTarget, setDeploymentTarget] = useState('widget-inspection-batch');
  const [selectedReleaseId, setSelectedReleaseId] = useState<string | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [manifestUri, setManifestUri] = useState('');
  const [pendingItems, setPendingItems] = useState<BatchInputItem[]>([]);
  const [previewMap, setPreviewMap] = useState<PreviewMap>({});

  const capabilitiesQuery = useQuery({
    queryKey: ['batch-capabilities'],
    queryFn: api.inference.batchCapabilities,
    refetchInterval: 10000
  });
  const statusSummaryQuery = useQuery({
    queryKey: ['batch-status-summary'],
    queryFn: api.inference.batchStatusSummary,
    refetchInterval: 5000
  });
  const jobsQuery = useQuery({
    queryKey: ['batch-jobs'],
    queryFn: api.inference.listBatchJobs,
    refetchInterval: 5000
  });

  const releaseOptions = capabilitiesQuery.data?.items;

  useEffect(() => {
    if (!selectedReleaseId && releaseOptions?.[0]) {
      setSelectedReleaseId(releaseOptions[0].deployment_release_id);
    }
  }, [releaseOptions, selectedReleaseId]);

  useEffect(() => {
    if (!selectedJobId && jobsQuery.data?.items[0]) {
      setSelectedJobId(jobsQuery.data.items[0].batch_job_id);
    }
  }, [jobsQuery.data, selectedJobId]);

  const activeStatus =
    statusSummaryQuery.data?.items.find((item) => item.active) ?? statusSummaryQuery.data?.items[0];

  const submitMutation = useMutation({
    mutationFn: async () => {
      if (!selectedReleaseId) {
        throw new Error('Select a batch release first.');
      }
      if (!pendingItems.length && !manifestUri) {
        throw new Error('Upload a test batch or provide an input manifest URI.');
      }
      return api.inference.submitBatch({
        deployment_target: deploymentTarget,
        deployment_release_id: selectedReleaseId,
        input_manifest_uri: manifestUri || undefined,
        inputs: pendingItems.length ? pendingItems : undefined
      });
    },
    onSuccess: async (job) => {
      setSelectedJobId(job.batch_job_id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['batch-jobs'] }),
        queryClient.invalidateQueries({ queryKey: ['batch-status-summary'] })
      ]);
      notify({
        title: 'Batch job submitted',
        description: `Batch job ${job.batch_job_id} was accepted and will progress asynchronously through chunk execution and result publication.`,
        tone: 'success'
      });
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Batch submission failed'), tone: 'error' });
    }
  });

  const detailQuery = useQuery({
    queryKey: ['batch-job-detail', selectedJobId],
    queryFn: () => api.inference.batchStatus(selectedJobId as string),
    enabled: Boolean(selectedJobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === 'completed' || status === 'failed' ? false : 3000;
    }
  });

  const resultsQuery = useQuery({
    queryKey: ['batch-job-results', selectedJobId],
    queryFn: () => api.inference.batchResults(selectedJobId as string),
    enabled: Boolean(selectedJobId && detailQuery.data?.output_manifest_uri)
  });

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Batch Inference</p>
          <h2>Upload a small image set and submit a batch inference job.</h2>
          <p className="subtle-text">
            Batch target, release, manifest URI, chunk internals, and raw result metadata are still
            available under Advanced for deeper troubleshooting.
          </p>
        </div>
        <div className="training-summary-pills">
          <Metric label="Jobs" value={String(jobsQuery.data?.items.length ?? 0)} />
          <Metric
            label="Active Release"
            value={activeStatus?.route.deployment_release_id ?? 'n/a'}
          />
          <Metric label="Task" value={taskTypeLabel(activeStatus?.route.task_type ?? 'classification')} />
          <Metric
            label="Throughput"
            value={formatThroughput(activeStatus?.latency.throughput_per_second)}
          />
          <Metric label="p95" value={formatLatency(activeStatus?.latency.p95_ms)} />
        </div>
      </section>

      <div className="training-grid training-grid--details">
        <Panel title="Submit Test Batch">
          <div className="panel-stack">
            <div className="training-form-grid">
              <label className="training-form-grid__full">
                <span>Upload test batch</span>
                <input
                  type="file"
                  accept="image/*"
                  multiple
                  onChange={async (event) => {
                    const files = Array.from(event.target.files ?? []);
                    const prepared = await Promise.all(
                      files.map(async (file, index) => {
                        const encoded = await readFileAsBase64(file);
                        const itemId = `${Date.now()}-${index}-${file.name}`;
                        return {
                          input: {
                            item_id: itemId,
                            image_base64: encoded.base64,
                            external_ref: file.name,
                            metadata: {
                              file_name: file.name
                            }
                          } satisfies BatchInputItem,
                          preview: encoded.dataUrl
                        };
                      })
                    );
                    setPendingItems(prepared.map((item) => item.input));
                    setPreviewMap(
                      Object.fromEntries(
                        prepared.map((item) => [item.input.item_id, item.preview])
                      )
                    );
                  }}
                />
              </label>
            </div>
            <details className="advanced-section">
              <summary>Batch target, release, and manifest</summary>
              <div className="training-form-grid">
                <label>
                  <span>Deployment target</span>
                  <input
                    value={deploymentTarget}
                    onChange={(event) => setDeploymentTarget(event.target.value)}
                  />
                </label>
                <label>
                  <span>Batch release</span>
                  <select
                    value={selectedReleaseId ?? ''}
                    onChange={(event) => setSelectedReleaseId(event.target.value)}
                  >
                    {(releaseOptions ?? []).map((option) => (
                      <option
                        key={option.deployment_release_id}
                        value={option.deployment_release_id}
                      >
                        {option.model_family_slug} - {architectureLabel(option.architecture)} -{' '}
                        {taskTypeLabel(option.task_type)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="training-form-grid__full">
                  <span>Existing input manifest URI</span>
                  <input
                    value={manifestUri}
                    onChange={(event) => setManifestUri(event.target.value)}
                    placeholder="file:///srv/cv-platform/examples/batch-input.json"
                  />
                </label>
              </div>
            </details>
            <div className="actions-row">
              <button
                type="button"
                className="button--primary"
                onClick={() => submitMutation.mutate()}
                disabled={submitMutation.isPending}
              >
                Submit Batch Job
              </button>
              <span className="subtle-text">
                {pendingItems.length > 0
                  ? `${pendingItems.length} inline items prepared`
                  : manifestUri
                    ? 'Manifest URI will be materialized by the batch pipeline'
                    : 'Choose images or a manifest to start'}
              </span>
            </div>
            {pendingItems.length > 0 ? (
              <div className="file-chip-row">
                {pendingItems.map((item) => (
                  <span key={item.item_id} className="badge badge--neutral">
                    {item.external_ref ?? item.item_id}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </Panel>

        <Panel title="Batch Runtime Status">
          <details className="advanced-section">
            <summary>Runtime release table</summary>
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>Release</th>
                  <th>Active</th>
                  <th>Warm</th>
                  <th>Inflight</th>
                  <th>p95</th>
                  <th>p99</th>
                </tr>
              </thead>
              <tbody>
                {statusSummaryQuery.data?.items.map((item) => (
                  <tr key={item.route.deployment_release_id}>
                    <td>
                      <strong>{item.route.deployment_release_id}</strong>
                      <div className="subtle-text">{item.route.model_version_id}</div>
                    </td>
                    <td>{item.active ? 'Yes' : item.rollback_candidate ? 'Rollback' : 'No'}</td>
                    <td>{item.warm_model ? 'Warm' : 'Cold'}</td>
                    <td>
                      {item.current_inflight}/{item.concurrency_limit}
                    </td>
                    <td>{formatLatency(item.latency.p95_ms)}</td>
                    <td>{formatLatency(item.latency.p99_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="Queued Jobs">
          {jobsQuery.data?.items.length ? (
            <table className="data-table">
              <thead>
                <tr>
                  <th>Job</th>
                  <th>Status</th>
                  <th>Progress</th>
                </tr>
              </thead>
              <tbody>
                {jobsQuery.data.items.map((job) => (
                  <tr
                    key={job.batch_job_id}
                    className={
                      job.batch_job_id === selectedJobId
                        ? 'training-row training-row--selected'
                        : 'training-row'
                    }
                    onClick={() => setSelectedJobId(job.batch_job_id)}
                  >
                    <td>
                      <strong>{job.batch_job_id}</strong>
                      <div className="subtle-text">{job.deployment_target}</div>
                    </td>
                    <td>{job.status}</td>
                    <td>
                      {job.progress.progress_percent.toFixed(1)}% -{' '}
                      {job.progress.completed_items}/{job.progress.total_items}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <EmptyState
              title="No batch jobs yet"
              body="Submit a small image set to exercise the chunked batch pipeline end to end."
            />
          )}
        </Panel>

        <Panel title="Selected Job Detail">
          {!detailQuery.data ? (
            <EmptyState
              title="Choose a batch job"
              body="Select a row to inspect chunk progress, artifacts, and API-readable outputs."
            />
          ) : (
            <div className="panel-stack">
              <div className="detail-grid">
                <Metric label="Status" value={detailQuery.data.status} />
                <Metric
                  label="Items"
                  value={`${detailQuery.data.progress.completed_items}/${detailQuery.data.progress.total_items}`}
                />
                <Metric
                  label="Bundle"
                  value={detailQuery.data.artifacts.bundle_uri ? 'Ready' : 'Pending'}
                />
              </div>
              {detailQuery.data.artifacts.incomplete_chunk_nos.length ? (
                <div className="inline-alert inline-alert--danger">
                  <strong>Incomplete outputs detected</strong>
                  <p>
                    Chunks {detailQuery.data.artifacts.incomplete_chunk_nos.join(', ')} did not
                    produce completed outputs. The batch result remains recoverable by rerunning the
                    missing work only.
                  </p>
                </div>
              ) : null}
              <details className="advanced-section">
                <summary>Chunk details and raw job payload</summary>
                <div className="detail-grid">
                  <Metric label="Chunks" value={String(detailQuery.data.progress.total_chunks)} />
                  <Metric
                    label="Recoverable"
                    value={detailQuery.data.artifacts.recoverable ? 'Yes' : 'No'}
                  />
                  <Metric label="Output Manifest" value={detailQuery.data.output_manifest_uri ?? 'pending'} />
                </div>
                <pre className="code-block">{JSON.stringify(detailQuery.data, null, 2)}</pre>
              </details>
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Visual Result Inspection">
        {!resultsQuery.data ? (
          <EmptyState
            title="Results will appear here"
            body="Once the selected batch job publishes its outputs, the UI shows normalized predictions and any local test previews still available in this browser session."
          />
        ) : (
          <div className="result-gallery">
            {resultsQuery.data.items.map((item) => (
              <ResultCard
                key={item.item_id}
                item={item}
                preview={previewMap[item.item_id]}
                taskType={resultsQuery.data.route.task_type}
              />
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Batch Capabilities">
        {(releaseOptions ?? []).length === 0 ? (
          <EmptyState
            title="No batch releases available"
            body="Stage or promote a batch target to expose capability metadata in the gateway."
          />
        ) : (
          <details className="advanced-section">
            <summary>Capability metadata</summary>
            <div className="capability-grid">
              {(releaseOptions ?? []).map((item) => (
                <CapabilityCard key={item.deployment_release_id} item={item} />
              ))}
            </div>
          </details>
        )}
      </Panel>
    </div>
  );
}

function ResultCard(props: {
  item: BatchResultRecord;
  preview?: string;
  taskType: TaskType;
}) {
  return (
    <article className="result-card">
      <div className="result-card__image">
        {props.preview ? (
          <img
            src={props.preview}
            alt={props.item.external_ref ?? props.item.item_id}
          />
        ) : (
          <div className="result-card__placeholder">No local preview</div>
        )}
        {props.preview && props.taskType === 'segmentation' ? (
          <svg className="visual-overlay" viewBox="0 0 100 100" preserveAspectRatio="none">
            {props.item.segments.map((segment, index) => (
              <polygon
                key={`${props.item.item_id}-${index}`}
                className="visual-overlay__polygon"
                points={segment.polygon.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')}
              />
            ))}
          </svg>
        ) : null}
        {props.preview && props.taskType === 'anomaly' && props.item.heatmap ? (
          <img
            className="visual-overlay visual-overlay__heatmap"
            src={`data:image/png;base64,${props.item.heatmap.data}`}
            alt=""
            aria-hidden="true"
          />
        ) : null}
      </div>
      <div className="result-card__content">
        <strong>{props.item.external_ref ?? props.item.item_id}</strong>
        <div className="subtle-text">
          {props.item.status} -{' '}
          {props.item.timing ? `${props.item.timing.total_ms.toFixed(1)} ms` : 'n/a'}
        </div>
        {props.taskType === 'classification' ? (
          <ul className="result-card__list">
            {props.item.labels.map((label) => (
              <li key={`${props.item.item_id}-${label.label}`}>
                {label.label}: {(label.score * 100).toFixed(1)}%
              </li>
            ))}
          </ul>
        ) : props.taskType === 'detection' || props.taskType === 'oriented_detection' ? (
          <ul className="result-card__list">
            {props.item.detections.map((detection, index) => (
              <li key={`${props.item.item_id}-${index}`}>
                {detection.label}: {(detection.score * 100).toFixed(1)}% at{' '}
                {detection.box.x_min.toFixed(2)},{detection.box.y_min.toFixed(2)}
                {detection.oriented_box ? ' OBB' : ''}
              </li>
            ))}
          </ul>
        ) : props.taskType === 'segmentation' ? (
          <ul className="result-card__list">
            {props.item.segments.map((segment, index) => (
              <li key={`${props.item.item_id}-${index}`}>
                {segment.label}: {(segment.score * 100).toFixed(1)}% • {segment.polygon.length} points
              </li>
            ))}
          </ul>
        ) : (
          <ul className="result-card__list">
            <li>Decision: {props.item.predicted_label ?? 'n/a'}</li>
            <li>
              Score:{' '}
              {props.item.anomaly_score !== null && props.item.anomaly_score !== undefined
                ? props.item.anomaly_score.toFixed(4)
                : 'n/a'}
            </li>
          </ul>
        )}
        {props.item.error ? (
          <div className="inline-alert inline-alert--danger">{props.item.error}</div>
        ) : null}
      </div>
    </article>
  );
}

function CapabilityCard(props: { item: ModelCapabilitySummary }) {
  return (
    <article className="metric-pill">
      <span>{props.item.model_family_slug}</span>
      <strong>{architectureLabel(props.item.architecture)}</strong>
      <div className="subtle-text">
        {taskTypeLabel(props.item.task_type)} - batch size {props.item.max_batch_size}
      </div>
      <div className="subtle-text">
        Classes: {props.item.class_labels.join(', ') || 'n/a'}
      </div>
      <div className="subtle-text">
        Schemas: {props.item.input_schema_ref ?? 'n/a'} to{' '}
        {props.item.output_schema_ref ?? 'n/a'}
      </div>
    </article>
  );
}

function Metric(props: { label: string; value: string }) {
  return (
    <div className="metric-pill">
      <span>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

function formatLatency(value: number | undefined) {
  return typeof value === 'number' ? `${value.toFixed(1)} ms` : 'n/a';
}

function formatThroughput(value: number | undefined) {
  return typeof value === 'number' ? `${value.toFixed(2)} item/s` : 'n/a';
}

async function readFileAsBase64(file: File) {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  const [, base64] = dataUrl.split(',', 2);
  return { dataUrl, base64 };
}
