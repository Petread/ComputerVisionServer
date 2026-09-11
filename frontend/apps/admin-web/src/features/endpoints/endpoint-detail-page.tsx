import { useState } from 'react';
import { Link, useParams } from '@tanstack/react-router';
import { useMutation, useQuery } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  PublicAnomalyResponse,
  PublicClassificationResponse,
  PublicDetectionResponse,
  PublicModelDetail,
  PublicSegmentationResponse
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import { INTERACTIVE_INFERENCE_TIMEOUT_MS } from '../inference/test-timeout';
import { architectureLabel, taskTypeLabel } from '../pipelines/modeling';
import { flattenProjectEndpoints, formatDate } from '../pipelines/presentation';

type EndpointTestResult =
  | PublicClassificationResponse
  | PublicDetectionResponse
  | PublicSegmentationResponse
  | PublicAnomalyResponse;

export function EndpointDetailPage() {
  const { modelId } = useParams({ from: '/authenticated/endpoints/$modelId' });
  const { notify } = useNotifications();
  const [imageFile, setImageFile] = useState<File | null>(null);

  const endpointQuery = useQuery({
    queryKey: ['endpoint-detail', modelId],
    queryFn: async () => {
      const [pipelinesResult, realtimeResult, batchResult] = await Promise.allSettled([
        api.pipelines.list(),
        api.inference.publicRealtimeModelDetail(modelId),
        api.inference.publicBatchModelDetail(modelId)
      ]);

      const pipelines = pipelinesResult.status === 'fulfilled' ? pipelinesResult.value.items : [];
      const endpoint =
        flattenProjectEndpoints(pipelines).find((item) => item.endpoint_name === modelId) ?? null;
      const realtime = realtimeResult.status === 'fulfilled' ? realtimeResult.value.item : null;
      const batch = batchResult.status === 'fulfilled' ? batchResult.value.item : null;

      if (!endpoint && !realtime && !batch) {
        throw new Error(`Endpoint ${modelId} was not found.`);
      }

      return { endpoint, realtime, batch };
    },
    refetchInterval: 10000
  });

  const testMutation = useMutation({
    mutationFn: async () => {
      const taskType =
        endpointQuery.data?.realtime?.task_type ??
        endpointQuery.data?.batch?.task_type ??
        endpointQuery.data?.endpoint?.task_type;
      if (!imageFile) {
        throw new Error('Choose an image first.');
      }
      if (!endpointQuery.data?.realtime?.availability.realtime.available || !taskType) {
        throw new Error('Realtime inference is not currently available for this endpoint.');
      }
      const timeoutParams = { timeoutMs: INTERACTIVE_INFERENCE_TIMEOUT_MS };
      if (taskType === 'classification') {
        return api.inference.publicClassify(modelId, imageFile, timeoutParams);
      }
      if (taskType === 'detection' || taskType === 'oriented_detection') {
        return api.inference.publicDetect(modelId, imageFile, timeoutParams);
      }
      if (taskType === 'anomaly') {
        return api.inference.publicAnomaly(modelId, imageFile, timeoutParams);
      }
      return api.inference.publicSegment(modelId, imageFile, timeoutParams);
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Endpoint test inference failed'), tone: 'error' });
    }
  });

  if (!endpointQuery.data) {
    return (
      <div className="panel-stack">
        <Panel title="Endpoint Details">
          <p className="subtle-text">Loading endpoint contract...</p>
        </Panel>
      </div>
    );
  }

  const { endpoint, realtime, batch } = endpointQuery.data;
  const displayName = endpoint?.display_name ?? realtime?.display_name ?? batch?.display_name ?? modelId;
  const taskType = endpoint?.task_type ?? realtime?.task_type ?? batch?.task_type ?? 'classification';
  const architecture = endpoint?.architecture ?? realtime?.architecture ?? batch?.architecture;
  const apiExamples = endpoint?.api_examples ?? buildDefaultExamples(taskType, modelId);
  const supportsRealtime = Boolean(realtime?.availability.realtime.available);
  const supportsBatch = Boolean(batch?.availability.batch.available);

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Inference Endpoint</p>
          <h2>{displayName}</h2>
          <p className="subtle-text">
            This is the application-facing view of the deployed capability. It shows the stable logical endpoint name,
            supported task type, architecture, and public contract without exposing internal deployment or Triton IDs.
          </p>
        </div>
        <div className="training-summary-pills">
          <MetricPill label="Endpoint name" value={modelId} tone="success" />
          <MetricPill label="Task" value={taskTypeLabel(taskType)} tone="info" />
          <MetricPill label="Architecture" value={architectureLabel(architecture)} />
          <MetricPill label="Realtime" value={supportsRealtime ? 'available' : 'offline'} tone={supportsRealtime ? 'success' : undefined} />
        </div>
      </section>

      <div className="training-grid training-grid--details">
        <Panel title="Endpoint Summary">
          <div className="panel-stack">
            <div className="detail-grid">
              <div className="metric-pill">
                <span>Project</span>
                <strong>{endpoint?.projectTitle ?? 'Not linked in pipeline view'}</strong>
                <small className="subtle-text">{endpoint?.label_project_id ?? 'No project id available'}</small>
              </div>
              <div className="metric-pill">
                <span>Current version</span>
                <strong>
                  {endpoint?.current_model_version_no
                    ? `v${endpoint.current_model_version_no}`
                    : realtime?.model_version ?? batch?.model_version ?? 'n/a'}
                </strong>
                <small className="subtle-text">Last deployed {formatDate(endpoint?.last_deployed_at)}</small>
              </div>
              <div className="metric-pill">
                <span>Realtime route</span>
                <strong>{supportsRealtime ? '/infer/... live' : 'Not available'}</strong>
                <small className="subtle-text">{realtime?.response_schema.schema_id ?? 'No realtime contract loaded'}</small>
              </div>
              <div className="metric-pill">
                <span>Batch route</span>
                <strong>{supportsBatch ? '/batch/jobs live' : 'Not available'}</strong>
                <small className="subtle-text">{batch?.response_schema.schema_id ?? 'No batch contract loaded'}</small>
              </div>
            </div>

            {endpoint ? (
              <div className="actions-row">
                <Link to="/pipelines/$labelProjectId" params={{ labelProjectId: endpoint.label_project_id }} className="primary-link">
                  Open source pipeline
                </Link>
              </div>
            ) : null}
          </div>
        </Panel>

        <Panel title="Quick Test Inference">
          {supportsRealtime ? (
            <div className="panel-stack">
              <label className="training-form-grid__full">
                <span>Test image</span>
                <input
                  type="file"
                  accept="image/*"
                  onChange={(event) => setImageFile(event.target.files?.[0] ?? null)}
                />
              </label>
              <div className="actions-row">
                <button type="button" onClick={() => testMutation.mutate()} disabled={!imageFile || testMutation.isPending}>
                  {testMutation.isPending ? 'Running...' : 'Test inference'}
                </button>
                <span className="subtle-text">
                  {imageFile ? imageFile.name : 'Choose an image to exercise the live realtime endpoint.'}
                </span>
              </div>

              {testMutation.data ? <EndpointResultPanel result={testMutation.data} /> : null}
            </div>
          ) : (
            <EmptyState
              title="Realtime testing unavailable"
              body="This endpoint is not currently published on the realtime gateway. Use the batch route or deploy a realtime target from the source pipeline."
            />
          )}
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="API Route Examples">
          <div className="endpoint-route-list">
            {apiExamples.map((example) => (
              <div key={`${example.method}-${example.path}`} className="endpoint-snippet">
                <strong>{example.description}</strong>
                <code className="endpoint-route">
                  {example.method} {example.path}
                </code>
              </div>
            ))}
          </div>
          <pre className="code-block">{buildRealtimeExample(taskType, modelId)}</pre>
          <pre className="code-block">{buildBatchExample(taskType, modelId)}</pre>
        </Panel>

        <Panel title="Published Contract">
          <div className="panel-stack">
            <ContractCard title="Realtime" detail={realtime} modeAvailable={supportsRealtime} />
            <ContractCard title="Batch" detail={batch} modeAvailable={supportsBatch} />
          </div>
        </Panel>
      </div>
    </div>
  );
}

function ContractCard(props: {
  title: string;
  detail: PublicModelDetail | null;
  modeAvailable: boolean;
}) {
  if (!props.detail) {
    return (
      <div className="inline-alert">
        <strong>{props.title}</strong>
        <p className="subtle-text">No public contract metadata is currently available for this mode.</p>
      </div>
    );
  }

  return (
    <article className="status-card">
      <div className="status-card__header">
        <div>
          <h3>{props.title}</h3>
          <p className="subtle-text">{props.detail.display_name}</p>
        </div>
        <span className={`badge badge--${props.modeAvailable ? 'success' : 'neutral'}`}>
          {props.modeAvailable ? 'Available' : 'Offline'}
        </span>
      </div>
      <div className="detail-grid">
        <div className="metric-pill">
          <span>Model id</span>
          <strong>{props.detail.model_id}</strong>
          <small className="subtle-text">{props.detail.model_family}</small>
        </div>
        <div className="metric-pill">
          <span>Architecture</span>
          <strong>{architectureLabel(props.detail.architecture)}</strong>
          <small className="subtle-text">{taskTypeLabel(props.detail.task_type)}</small>
        </div>
        <div className="metric-pill">
          <span>Input limits</span>
          <strong>{props.detail.input_constraints.max_image_bytes} bytes</strong>
          <small className="subtle-text">{props.detail.input_constraints.canonical_form}</small>
        </div>
        <div className="metric-pill">
          <span>Class labels</span>
          <strong>{props.detail.class_labels.length || 0}</strong>
          <small className="subtle-text">{props.detail.class_labels.join(', ') || 'Not declared'}</small>
        </div>
      </div>
    </article>
  );
}

function EndpointResultPanel(props: { result: EndpointTestResult }) {
  return (
    <div className="panel-stack">
      <div className="detail-grid">
        <MetricPill label="Model version" value={String(props.result.model_version)} tone="success" />
        <MetricPill label="Architecture" value={architectureLabel(props.result.architecture)} />
        <MetricPill label="Total latency" value={`${props.result.timing.total_ms.toFixed(1)} ms`} />
        <MetricPill label="Image bytes" value={String(props.result.input.size_bytes)} />
      </div>
      {'predictions' in props.result ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Score</th>
            </tr>
          </thead>
          <tbody>
            {props.result.predictions.map((prediction) => (
              <tr key={prediction.label}>
                <td>{prediction.label}</td>
                <td>{(prediction.score * 100).toFixed(2)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : 'detections' in props.result ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Confidence</th>
              <th>Box</th>
              <th>Geometry</th>
            </tr>
          </thead>
          <tbody>
            {props.result.detections.map((detection, index) => (
              <tr key={`${detection.label}-${index}`}>
                <td>{detection.label}</td>
                <td>{(detection.confidence * 100).toFixed(2)}%</td>
                <td>
                  {detection.bounding_box.x_min.toFixed(2)}, {detection.bounding_box.y_min.toFixed(2)} to{' '}
                  {detection.bounding_box.x_max.toFixed(2)}, {detection.bounding_box.y_max.toFixed(2)}
                </td>
                <td>
                  {detection.oriented_box
                    ? `OBB ${detection.oriented_box.rotation_degrees?.toFixed(1) ?? '0.0'} deg`
                    : 'Axis-aligned'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : 'segments' in props.result ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Confidence</th>
              <th>Points</th>
            </tr>
          </thead>
          <tbody>
            {props.result.segments.map((segment, index) => (
              <tr key={`${segment.label}-${index}`}>
                <td>{segment.label}</td>
                <td>{(segment.confidence * 100).toFixed(2)}%</td>
                <td>{segment.polygon.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <table className="data-table">
          <tbody>
            <tr>
              <th>Decision</th>
              <td>{props.result.predicted_label}</td>
            </tr>
            <tr>
              <th>Score</th>
              <td>{props.result.anomaly_score.toFixed(4)}</td>
            </tr>
            <tr>
              <th>Threshold</th>
              <td>{props.result.threshold !== null && props.result.threshold !== undefined ? props.result.threshold.toFixed(4) : 'n/a'}</td>
            </tr>
          </tbody>
        </table>
      )}
      <details className="advanced-section">
        <summary>Raw response</summary>
        <pre className="code-block">{JSON.stringify(props.result, null, 2)}</pre>
      </details>
    </div>
  );
}

function buildDefaultExamples(taskType: string, modelId: string) {
  const realtimePath =
    taskType === 'classification'
      ? '/infer/classify'
      : taskType === 'detection' || taskType === 'oriented_detection'
        ? '/infer/detect'
        : taskType === 'anomaly'
          ? '/infer/anomaly'
          : '/infer/segment';
  return [
    {
      method: 'GET',
      path: `/models/${modelId}`,
      description: 'Discover the published contract and current availability.'
    },
    {
      method: 'POST',
      path: realtimePath,
      description: 'Send one image to the realtime gateway using multipart form-data.'
    },
    {
      method: 'POST',
      path: '/batch/jobs',
      description: 'Submit asynchronous batch inference with the same logical endpoint name.'
    }
  ];
}

function buildRealtimeExample(taskType: string, modelId: string) {
  const route =
    taskType === 'classification'
      ? '/infer/classify'
      : taskType === 'detection' || taskType === 'oriented_detection'
        ? '/infer/detect'
        : taskType === 'anomaly'
          ? '/infer/anomaly'
          : '/infer/segment';
  return [`curl -X POST "${route}" \\`, `  -F "model_id=${modelId}" \\`, '  -F "image=@sample.jpg"'].join('\n');
}

function buildBatchExample(taskType: string, modelId: string) {
  return JSON.stringify(
    {
      path: '/batch/jobs',
      method: 'POST',
      body: {
        model_id: modelId,
        task_type: taskType,
        inputs: [
          {
            item_id: 'sample-1',
            image_base64: '<base64-image>'
          }
        ]
      }
    },
    null,
    2
  );
}
