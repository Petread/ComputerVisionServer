import { describe, expect, it } from 'vitest';
import type { ProjectPipelineSummary } from '@cv-platform/schemas';
import {
  endpointAvailabilityLabel,
  flattenProjectEndpoints,
  flattenProjectModels,
  pipelineReadyForTraining
} from './presentation';

const detectionArchitecture = {
  task_type: 'detection' as const,
  family: 'yolo',
  version: 'v8',
  size: 'n',
  display_name: 'YOLOv8 Nano',
  internal_key: 'detection:yolo:v8:n'
};

const basePipeline: ProjectPipelineSummary = {
  label_project_id: 'lpr_1',
  label_studio_project_id: 101,
  title: 'Cars Inspection',
  task_type: 'detection',
  status: 'active',
  dataset_id: 'dst_1',
  dataset_name: 'Cars Inspection',
  training_pipeline_slug: 'cars-inspection',
  automation_policy: {
    auto_sync_enabled: true,
    auto_snapshot_enabled: true,
    auto_train_enabled: false,
    auto_deploy_enabled: true,
    default_model_template: 'detection-yolov8-n',
    default_training_profile: 'safe-default',
    preferred_endpoint_mode: 'realtime'
  },
  readiness: {
    task_count: 10,
    annotated_task_count: 9,
    annotation_count: 24,
    draft_task_count: 1,
    eligible_task_count: 9,
    submitted_labeled_task_count: 7,
    submitted_background_task_count: 2,
    invalid_task_count: 0,
    minimum_annotation_count: 10,
    required_classes: ['car'],
    class_distribution: { car: 7 },
    train_ratio: 80,
    val_ratio: 10,
    test_ratio: 10,
    readiness_percent: 90,
    annotations_ready: true,
    latest_snapshot_ready: true,
    latest_valid_snapshot_ready: true,
    readiness_reason: 'Latest valid dataset is ready for model training.'
  },
  latest_snapshot: {
    snapshot_id: 'dss_2',
    dataset_id: 'dst_1',
    label_project_id: 'lpr_1',
    source_label_studio_project_id: 101,
    source_project_title: 'Cars Inspection',
    snapshot_no: 2,
    status: 'ready',
    export_format: 'JSON',
    label_schema_version: 'ls.v1',
    item_count: 9,
    annotation_count: 24,
    eligible_item_count: 9,
    submitted_labeled_item_count: 7,
    submitted_background_item_count: 2,
    draft_item_count: 1,
    invalid_item_count: 0,
    content_checksum: 'sha256:abc',
    validation_status: 'passed',
    created_at: '2026-04-21T10:00:00Z'
  },
  latest_valid_snapshot: {
    snapshot_id: 'dss_2',
    dataset_id: 'dst_1',
    label_project_id: 'lpr_1',
    source_label_studio_project_id: 101,
    source_project_title: 'Cars Inspection',
    snapshot_no: 2,
    status: 'ready',
    export_format: 'JSON',
    label_schema_version: 'ls.v1',
    item_count: 9,
    annotation_count: 24,
    eligible_item_count: 9,
    submitted_labeled_item_count: 7,
    submitted_background_item_count: 2,
    draft_item_count: 1,
    invalid_item_count: 0,
    content_checksum: 'sha256:abc',
    validation_status: 'passed',
    created_at: '2026-04-21T10:00:00Z'
  },
  models: [
    {
      model_family_id: 'mfy_1',
      label_project_id: 'lpr_1',
      display_name: 'Cars Inspection Detector',
      slug: 'cars-inspection-detector',
      endpoint_name: 'cars-inspection-detector',
      description: 'Default detector',
      task_type: 'detection',
      architecture: detectionArchitecture,
      template_key: 'detection-yolov8-n',
      template_version: 1,
      training_profile_key: 'safe-default',
      promotion_policy: {
        criteria: [{ metric_name: 'mAP50', comparison: 'gte', threshold: 0.35, phase: 'evaluation' }],
        allow_model_override: true
      },
      latest_model_version_id: 'mvr_7',
      latest_model_version_no: 7,
      latest_model_status: 'trained',
      latest_training_job_id: 'trn_7',
      created_at: '2026-04-21T10:05:00Z'
    }
  ],
  last_training: {
    training_job_id: 'trn_7',
    model_family_slug: 'cars-inspection-detector',
    dataset_snapshot_id: 'dss_2',
    task_type: 'detection',
    architecture: detectionArchitecture,
    model_template_key: 'detection-yolov8-n',
    model_template_version: 1,
    recipe_name: 'baseline_det',
    recipe_version: '1.1.0',
    queue_name: 'training',
    status: 'completed',
    progress_percent: 100,
    current_epoch: 40,
    current_step: 40,
    total_epochs: 40,
    latest_checkpoint_id: 'chk_7',
    worker_instance_id: 'worker-1',
    gpu_device: 'cuda:0',
    promotion_ready: true,
    created_at: '2026-04-21T10:05:00Z',
    started_at: '2026-04-21T10:06:00Z',
    finished_at: '2026-04-21T10:25:00Z'
  },
  current_endpoints: [
    {
      endpoint_name: 'cars-inspection-detector',
      label_project_id: 'lpr_1',
      model_family_id: 'mfy_1',
      model_family_slug: 'cars-inspection-detector',
      display_name: 'Cars Inspection Detector',
      task_type: 'detection',
      architecture: detectionArchitecture,
      preferred_mode: 'both',
      realtime_available: true,
      batch_available: true,
      realtime_release_id: 'rel_rt_1',
      batch_release_id: 'rel_bt_1',
      current_model_version_id: 'mvr_7',
      current_model_version_no: 7,
      last_deployed_at: '2026-04-21T10:40:00Z',
      api_examples: [
        {
          method: 'GET',
          path: '/models/cars-inspection-detector',
          description: 'Discover the endpoint.'
        }
      ]
    }
  ],
  prediction_backend: {
    enabled: false,
    status: 'not_configured',
    auto_update: true
  },
  next_recommended_action: 'Test inference',
  next_action_detail: 'The endpoint is live.'
};

describe('pipeline presentation helpers', () => {
  it('marks a pipeline as ready when a valid dataset and model exist', () => {
    expect(pipelineReadyForTraining(basePipeline)).toBe(true);
  });

  it('flattens project models with project context', () => {
    const [item] = flattenProjectModels([basePipeline]);
    expect(item.projectTitle).toBe('Cars Inspection');
    expect(item.nextAction).toBe('Test inference');
    expect(item.display_name).toBe('Cars Inspection Detector');
  });

  it('flattens project endpoints and formats availability labels', () => {
    const [item] = flattenProjectEndpoints([basePipeline]);
    expect(item.projectTitle).toBe('Cars Inspection');
    expect(endpointAvailabilityLabel(item)).toBe('Realtime + batch live');
  });
});
