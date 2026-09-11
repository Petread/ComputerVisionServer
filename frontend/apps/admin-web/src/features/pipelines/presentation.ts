import type {
  DatasetSnapshotSummary,
  ProjectEndpointSummary,
  ProjectModelSummary,
  ProjectPipelineSummary,
  TrainingJobStatus
} from '@cv-platform/schemas';

export function formatDate(value?: string | null) {
  if (!value) {
    return 'n/a';
  }
  return new Date(value).toLocaleString();
}

export function snapshotLabel(snapshot?: DatasetSnapshotSummary | null) {
  if (!snapshot) {
    return 'Not available';
  }
  return `Snapshot ${snapshot.snapshot_no}`;
}

export function snapshotTone(status?: string | null) {
  if (status === 'ready') {
    return 'success';
  }
  if (status === 'failed') {
    return 'danger';
  }
  if (!status) {
    return 'neutral';
  }
  return 'warn';
}

export function trainingTone(status?: TrainingJobStatus | string | null) {
  if (status === 'completed') {
    return 'success';
  }
  if (status === 'failed' || status === 'cancelled' || status === 'rolled_back_if_needed') {
    return 'danger';
  }
  if (!status) {
    return 'neutral';
  }
  return 'warn';
}

export function endpointAvailabilityLabel(endpoint: ProjectEndpointSummary) {
  if (endpoint.realtime_available && endpoint.batch_available) {
    return 'Realtime + batch live';
  }
  if (endpoint.realtime_available) {
    return 'Realtime live';
  }
  if (endpoint.batch_available) {
    return 'Batch live';
  }
  return 'Not deployed';
}

export function pipelineReadyForTraining(pipeline: ProjectPipelineSummary) {
  return Boolean(pipeline.latest_valid_snapshot && pipeline.models.length > 0);
}

export function flattenProjectModels(
  pipelines: ProjectPipelineSummary[]
): Array<ProjectModelSummary & { projectTitle: string; nextAction: string }> {
  return pipelines.flatMap((pipeline) =>
    pipeline.models.map((model) => ({
      ...model,
      projectTitle: pipeline.title,
      nextAction: pipeline.next_recommended_action
    }))
  );
}

export function flattenProjectEndpoints(
  pipelines: ProjectPipelineSummary[]
): Array<ProjectEndpointSummary & { projectTitle: string }> {
  return pipelines.flatMap((pipeline) =>
    pipeline.current_endpoints.map((endpoint) => ({
      ...endpoint,
      projectTitle: pipeline.title
    }))
  );
}
