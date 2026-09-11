import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type {
  ClassificationInferenceResponse,
  ClassificationLabelScore,
  AnomalyInferenceResponse,
  DetectionInferenceResponse,
  DetectionPrediction,
  ModelCapabilitySummary,
  SegmentationInferenceResponse,
  SegmentationPrediction
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { AppIcon } from '../../components/app-icon';
import { useNotifications } from '../../components/notification-provider';
import { architectureLabel, taskTypeLabel } from '../pipelines/modeling';
import { INTERACTIVE_INFERENCE_TIMEOUT_MS } from './test-timeout';

const SAMPLE_IMAGE =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8/5+hHgAHggJ/l8LojQAAAABJRU5ErkJggg==';

type InferenceResult =
  | ClassificationInferenceResponse
  | DetectionInferenceResponse
  | SegmentationInferenceResponse
  | AnomalyInferenceResponse;

type ImageDimensions = {
  width: number;
  height: number;
};

export function InferencePage() {
  const { notify } = useNotifications();
  const [deploymentTarget, setDeploymentTarget] = useState('widget-inspection-rt');
  const [selectedReleaseId, setSelectedReleaseId] = useState<string | null>(null);
  const [comparisonReleaseId, setComparisonReleaseId] = useState<string | null>(null);
  const [imageBase64, setImageBase64] = useState(SAMPLE_IMAGE);
  const [imagePreview, setImagePreview] = useState(`data:image/png;base64,${SAMPLE_IMAGE}`);
  const [imageName, setImageName] = useState('Sample image');
  const [imageDimensions, setImageDimensions] = useState<ImageDimensions>({ width: 1, height: 1 });

  const capabilitiesQuery = useQuery({
    queryKey: ['realtime-capabilities'],
    queryFn: api.inference.realtimeCapabilities,
    refetchInterval: 10000
  });
  const statusQuery = useQuery({
    queryKey: ['realtime-status'],
    queryFn: api.inference.realtimeStatus,
    refetchInterval: 5000
  });

  const releaseOptions = useMemo(() => capabilitiesQuery.data?.items ?? [], [capabilitiesQuery.data]);

  useEffect(() => {
    if (!selectedReleaseId && releaseOptions[0]) {
      setSelectedReleaseId(releaseOptions[0].deployment_release_id);
    }
  }, [releaseOptions, selectedReleaseId]);

  useEffect(() => {
    if (!comparisonReleaseId && releaseOptions[1]) {
      setComparisonReleaseId(releaseOptions[1].deployment_release_id);
    }
  }, [comparisonReleaseId, releaseOptions]);

  const selectedCapability = releaseOptions.find((item) => item.deployment_release_id === selectedReleaseId);
  const comparisonCapability = releaseOptions.find((item) => item.deployment_release_id === comparisonReleaseId);

  async function runInference(releaseId: string) {
    const capability = releaseOptions.find((item) => item.deployment_release_id === releaseId);
    if (!capability) {
      throw new Error('Select a deployed release first.');
    }
    if (capability.task_type === 'classification') {
      return api.inference.classify({
        deployment_target: deploymentTarget,
        image_base64: imageBase64,
        deployment_release_id: releaseId,
        timeout_ms: INTERACTIVE_INFERENCE_TIMEOUT_MS,
        top_k: Math.min(capability.class_labels.length || 3, 5)
      });
    }
    if (capability.task_type === 'detection' || capability.task_type === 'oriented_detection') {
      return api.inference.detect({
        deployment_target: deploymentTarget,
        image_base64: imageBase64,
        deployment_release_id: releaseId,
        timeout_ms: INTERACTIVE_INFERENCE_TIMEOUT_MS,
        max_detections: 8,
        score_threshold: capability.confidence_threshold_default
      });
    }
    if (capability.task_type === 'anomaly') {
      return api.inference.anomaly({
        deployment_target: deploymentTarget,
        image_base64: imageBase64,
        deployment_release_id: releaseId,
        timeout_ms: INTERACTIVE_INFERENCE_TIMEOUT_MS,
        threshold: (capability.anomaly?.threshold as number | undefined) ?? null,
        return_mask: false
      });
    }
    return api.inference.segment({
      deployment_target: deploymentTarget,
      image_base64: imageBase64,
      deployment_release_id: releaseId,
      timeout_ms: INTERACTIVE_INFERENCE_TIMEOUT_MS,
      max_segments: 8,
      score_threshold: capability.confidence_threshold_default
    });
  }

  const singleRunMutation = useMutation({
    mutationFn: runInference,
    onError: (error) => {
      notify({ ...describeError(error, 'Realtime inference failed'), tone: 'error' });
    }
  });

  const compareMutation = useMutation({
    mutationFn: async () => {
      if (!selectedReleaseId || !comparisonReleaseId) {
        throw new Error('Choose two deployment releases to compare.');
      }
      const [left, right] = await Promise.all([runInference(selectedReleaseId), runInference(comparisonReleaseId)]);
      return { left, right };
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Release comparison failed'), tone: 'error' });
    }
  });

  const activeStatus = statusQuery.data?.items.find((item) => item.active) ?? statusQuery.data?.items[0];

  return (
    <div className="panel-stack panel-stack--inference">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Realtime Inference</p>
          <h2>Upload one image and run a live endpoint test.</h2>
          <p className="subtle-text">
            The active release is selected automatically when available. Target overrides, release
            comparisons, status tables, and raw timing details stay under Advanced.
          </p>
        </div>
        <div className="training-summary-pills">
          <Metric label="Active Release" value={activeStatus?.route.deployment_release_id ?? 'n/a'} />
          <Metric label="Task" value={taskTypeLabel(activeStatus?.route.task_type ?? 'classification')} />
          <Metric label="Realtime" value={activeStatus?.active ? 'live' : 'not active'} />
        </div>
      </section>

      <div className="training-grid training-grid--details">
        <Panel title="Single-Image Test">
          <form
            className="inference-test-layout"
            onSubmit={(event) => {
              event.preventDefault();
              if (selectedReleaseId) {
                compareMutation.reset();
                singleRunMutation.mutate(selectedReleaseId);
              }
            }}
          >
            <label className="inference-upload">
              <span className="inference-upload__preview">
                <img src={imagePreview} alt="Selected test input" />
              </span>
              <span className="inference-upload__body">
                <span>Test image</span>
                <strong>{imageName}</strong>
                <input
                  type="file"
                  accept="image/*"
                  onChange={async (event) => {
                    const file = event.target.files?.[0];
                    if (!file) {
                      return;
                    }
                    const encoded = await readFileAsBase64(file);
                    setImageBase64(encoded.base64);
                    setImagePreview(encoded.dataUrl);
                    setImageName(file.name);
                    setImageDimensions(encoded.dimensions);
                    singleRunMutation.reset();
                    compareMutation.reset();
                  }}
                />
              </span>
            </label>

            <div className="inference-test-controls">
              <div className="training-form-grid">
                <label className="training-form-grid__full">
                  <span>Primary release</span>
                  <select
                    value={selectedReleaseId ?? ''}
                    onChange={(event) => {
                      setSelectedReleaseId(event.target.value);
                      singleRunMutation.reset();
                      compareMutation.reset();
                    }}
                  >
                    {releaseOptions.map((option) => (
                      <option key={option.deployment_release_id} value={option.deployment_release_id}>
                        {option.model_family_slug} - {architectureLabel(option.architecture)} - {taskTypeLabel(option.task_type)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="actions-row">
                <button type="submit" className="button--primary" disabled={!selectedReleaseId || singleRunMutation.isPending}>
                  <AppIcon name="sparkle" />
                  Run Test
                </button>
              </div>
            </div>

            <details className="advanced-section">
              <summary>Release selection and comparison</summary>
              <div className="training-form-grid">
                <label>
                  <span>Deployment target</span>
                  <input value={deploymentTarget} onChange={(event) => setDeploymentTarget(event.target.value)} />
                </label>
                <label>
                  <span>Comparison release</span>
                  <select
                    value={comparisonReleaseId ?? ''}
                    onChange={(event) => {
                      setComparisonReleaseId(event.target.value);
                      compareMutation.reset();
                    }}
                  >
                    <option value="">No comparison</option>
                    {releaseOptions.map((option) => (
                      <option key={option.deployment_release_id} value={option.deployment_release_id}>
                        {option.model_family_slug} - {architectureLabel(option.architecture)}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="actions-row">
                <button
                  type="button"
                  disabled={!selectedReleaseId || !comparisonReleaseId || compareMutation.isPending}
                  onClick={() => {
                    singleRunMutation.reset();
                    compareMutation.mutate();
                  }}
                >
                  Compare Releases
                </button>
              </div>
            </details>
          </form>
        </Panel>

        <Panel title="Release Status">
          <details className="advanced-section">
            <summary>Release status table</summary>
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>Release</th>
                  <th>Task</th>
                  <th>Architecture</th>
                  <th>Active</th>
                  <th>Warm</th>
                  <th>Serving</th>
                  <th>Triton compute</th>
                  <th>p95</th>
                </tr>
              </thead>
              <tbody>
                {statusQuery.data?.items.map((item) => {
                  const capability = releaseOptions.find(
                    (option) => option.deployment_release_id === item.route.deployment_release_id
                  );
                  return (
                    <tr key={item.route.deployment_release_id}>
                      <td>
                        <strong>{item.route.deployment_release_id}</strong>
                        <div className="subtle-text">{item.route.model_version_id}</div>
                      </td>
                      <td>{taskTypeLabel(item.route.task_type)}</td>
                      <td>{architectureLabel(capability?.architecture)}</td>
                      <td>{item.active ? 'Yes' : item.rollback_candidate ? 'Rollback' : 'No'}</td>
                      <td>{item.warm_model ? 'Warm' : 'Cold'}</td>
                      <td>{servingFormatLabel(item.route.serving_artifact_format)}</td>
                      <td>{formatBackendPhase(item.backend_statistics, 'compute_infer_avg_ms')}</td>
                      <td>{formatLatency(item.latency.p95_ms)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </details>
        </Panel>
      </div>

      <div className="inference-result-stack">
        <ResultPanel
          title="Primary Output"
          capability={selectedCapability}
          result={singleRunMutation.data ?? compareMutation.data?.left}
          imagePreview={imagePreview}
          imageDimensions={imageDimensions}
        />
        {compareMutation.data?.right ? (
          <ResultPanel
            title="Comparison Output"
            capability={comparisonCapability}
            result={compareMutation.data.right}
            imagePreview={imagePreview}
            imageDimensions={imageDimensions}
          />
        ) : null}
      </div>

      <Panel title="Available Model Capabilities">
        {releaseOptions.length === 0 ? (
          <EmptyState
            title="No releases discovered"
            body="Stage or promote a release first so the realtime gateway has a versioned deployment to expose."
          />
        ) : (
          <details className="advanced-section">
            <summary>Capability metadata</summary>
            <div className="capability-grid">
              {releaseOptions.map((item) => (
                <article key={item.deployment_release_id} className="metric-pill">
                  <span>{item.model_family_slug}</span>
                  <strong>{architectureLabel(item.architecture)}</strong>
                  <div className="subtle-text">
                    {taskTypeLabel(item.task_type)} - contract v{item.contract_major}
                  </div>
                  <div className="subtle-text">
                    Classes: {item.class_labels.join(', ') || 'n/a'}
                  </div>
                  <div className="subtle-text">
                    Schemas: {item.input_schema_ref ?? 'n/a'} to {item.output_schema_ref ?? 'n/a'}
                  </div>
                </article>
              ))}
            </div>
          </details>
        )}
      </Panel>
    </div>
  );
}

function ResultPanel(props: {
  title: string;
  capability?: ModelCapabilitySummary;
  result?: InferenceResult;
  imagePreview: string;
  imageDimensions: ImageDimensions;
}) {
  if (!props.result || !props.capability) {
    return (
      <Panel title={props.title}>
        <EmptyState
          title="No output yet"
          body="Run an image through a release to inspect normalized predictions."
        />
      </Panel>
    );
  }

  const taskType = props.capability.task_type;
  const classificationLabels =
    taskType === 'classification' && hasLabels(props.result) ? props.result.labels : null;
  const detections =
    (taskType === 'detection' || taskType === 'oriented_detection') && hasDetections(props.result)
      ? props.result.detections
      : null;
  const segments =
    taskType === 'segmentation' && hasSegments(props.result) ? props.result.segments : null;
  const anomalyResult = taskType === 'anomaly' && hasAnomaly(props.result) ? props.result : null;
  const aspectRatio =
    props.imageDimensions.width > 0 && props.imageDimensions.height > 0
      ? `${props.imageDimensions.width} / ${props.imageDimensions.height}`
      : undefined;

  return (
    <Panel title={props.title}>
      <div className="panel-stack">
        <div className="detail-grid">
          <Metric label="Task" value={taskTypeLabel(props.capability.task_type)} />
          <Metric label="Architecture" value={architectureLabel(props.result.architecture)} />
        </div>

        <div className="visual-inspection-card">
          <div className="visual-inspection-card__image" style={{ aspectRatio }}>
            <img src={props.imagePreview} alt="Realtime input" />
            {detections ? (
              <div className="visual-overlay">
                {detections.map((detection, index) => (
                  <div
                    key={`${detection.label}-${index}`}
                    className="visual-overlay__box"
                    style={{
                      left: `${detection.box.x_min * 100}%`,
                      top: `${detection.box.y_min * 100}%`,
                      width: `${(detection.box.x_max - detection.box.x_min) * 100}%`,
                      height: `${(detection.box.y_max - detection.box.y_min) * 100}%`
                    }}
                  >
                    <span>
                      {detection.label}: {(detection.score * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            ) : null}
            {segments ? (
              <svg className="visual-overlay" viewBox="0 0 100 100" preserveAspectRatio="none">
                {segments.map((segment, index) => (
                  <polygon
                    key={`${segment.label}-${index}`}
                    className="visual-overlay__polygon"
                    points={segment.polygon.map((point) => `${point.x * 100},${point.y * 100}`).join(' ')}
                  />
                ))}
              </svg>
            ) : null}
            {anomalyResult ? (
              <img
                className="visual-overlay visual-overlay__heatmap"
                src={`data:image/png;base64,${anomalyResult.heatmap.data}`}
                alt=""
                aria-hidden="true"
              />
            ) : null}
          </div>
          <div className="visual-inspection-card__details">
            {classificationLabels ? <ScoreTable labels={classificationLabels} /> : null}
            {detections ? <DetectionTable detections={detections} /> : null}
            {segments ? <SegmentationTable segments={segments} /> : null}
            {anomalyResult ? <AnomalySummary result={anomalyResult} /> : null}
          </div>
        </div>

        <details className="advanced-section">
          <summary>Raw response and timing</summary>
          <div className="detail-grid">
            <Metric label="Release" value={props.result.route.deployment_release_id} />
            <Metric label="Model Version" value={props.result.route.model_version_id} />
            <Metric label="Total" value={formatLatency(props.result.timing.total_ms)} />
          </div>
          <pre className="code-block">{JSON.stringify(props.result, null, 2)}</pre>
        </details>
      </div>
    </Panel>
  );
}

function hasLabels(result: InferenceResult): result is ClassificationInferenceResponse {
  return Array.isArray((result as ClassificationInferenceResponse).labels);
}

function hasDetections(result: InferenceResult): result is DetectionInferenceResponse {
  return Array.isArray((result as DetectionInferenceResponse).detections);
}

function hasSegments(result: InferenceResult): result is SegmentationInferenceResponse {
  return Array.isArray((result as SegmentationInferenceResponse).segments);
}

function hasAnomaly(result: InferenceResult): result is AnomalyInferenceResponse {
  return typeof (result as AnomalyInferenceResponse).anomaly_score === 'number';
}

function ScoreTable(props: { labels: ClassificationLabelScore[] }) {
  return (
    <table className="data-table compact-table">
      <thead>
        <tr>
          <th>Rank</th>
          <th>Label</th>
          <th>Score</th>
        </tr>
      </thead>
      <tbody>
        {props.labels.map((label) => (
          <tr key={`${label.rank}-${label.label}`}>
            <td>{label.rank}</td>
            <td>{label.label}</td>
            <td>{(label.score * 100).toFixed(2)}%</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function DetectionTable(props: { detections: DetectionPrediction[] }) {
  return (
    <table className="data-table compact-table">
      <thead>
        <tr>
          <th>Label</th>
          <th>Score</th>
          <th>Box</th>
          <th>Geometry</th>
        </tr>
      </thead>
      <tbody>
        {props.detections.map((detection, index) => (
          <tr key={`${detection.label}-${index}`}>
            <td>{detection.label}</td>
            <td>{(detection.score * 100).toFixed(2)}%</td>
            <td>
              {detection.box.x_min.toFixed(2)}, {detection.box.y_min.toFixed(2)} to{' '}
              {detection.box.x_max.toFixed(2)}, {detection.box.y_max.toFixed(2)}
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
  );
}

function SegmentationTable(props: { segments: SegmentationPrediction[] }) {
  return (
    <table className="data-table compact-table">
      <thead>
        <tr>
          <th>Label</th>
          <th>Score</th>
          <th>Polygon points</th>
        </tr>
      </thead>
      <tbody>
        {props.segments.map((segment, index) => (
          <tr key={`${segment.label}-${index}`}>
            <td>{segment.label}</td>
            <td>{(segment.score * 100).toFixed(2)}%</td>
            <td>{segment.polygon.length}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function AnomalySummary(props: { result: AnomalyInferenceResponse }) {
  return (
    <table className="data-table compact-table">
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

function servingFormatLabel(value: 'tensorrt_plan' | 'onnx' | null | undefined) {
  if (value === 'tensorrt_plan') {
    return 'TensorRT';
  }
  if (value === 'onnx') {
    return 'ONNX';
  }
  return 'n/a';
}

function formatBackendPhase(statistics: Record<string, number>, key: string) {
  const value = statistics[key];
  return Number.isFinite(value) ? `${value.toFixed(1)} ms` : 'n/a';
}

async function readFileAsBase64(file: File) {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
  const [, base64] = dataUrl.split(',', 2);
  const dimensions = await readImageDimensions(dataUrl);
  return { dataUrl, base64, dimensions };
}

async function readImageDimensions(dataUrl: string): Promise<ImageDimensions> {
  return new Promise((resolve) => {
    const image = new Image();
    image.onload = () => {
      resolve({
        width: image.naturalWidth || 1,
        height: image.naturalHeight || 1
      });
    };
    image.onerror = () => resolve({ width: 1, height: 1 });
    image.src = dataUrl;
  });
}
