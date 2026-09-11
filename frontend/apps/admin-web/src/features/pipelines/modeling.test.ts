import { describe, expect, it } from 'vitest';
import type {
  DeploymentReleaseSummary,
  ModelTemplateSummary,
  ProjectReadinessSummary,
  SupportedModelArchitecture
} from '@cv-platform/schemas';
import {
  datasetAwareTrainingRecommendation,
  deploymentModelsForVersions,
  metricChartDomain,
  resolveTrainingProgress,
  splitRatioLabel,
  weightDecayFromDefaults
} from './modeling';

describe('metric chart scaling', () => {
  it('recalculates a tighter domain from the currently visible values', () => {
    const allSeries = metricChartDomain([0.15, 0.2, 42, 85]);
    const oneVisibleSeries = metricChartDomain([0.15, 0.2]);

    expect(oneVisibleSeries.range).toBeLessThan(allSeries.range);
    expect(oneVisibleSeries.min).toBeLessThan(0.15);
    expect(oneVisibleSeries.max).toBeGreaterThan(0.2);
  });

  it('keeps a useful domain for a flat metric series', () => {
    const domain = metricChartDomain([0.75, 0.75]);

    expect(domain.min).toBeLessThan(0.75);
    expect(domain.max).toBeGreaterThan(0.75);
    expect(domain.range).toBeGreaterThan(0);
  });
});

const baseArchitecture = {
  task_type: 'classification' as const,
  family: 'resnet',
  version: 'v1',
  size: '18',
  display_name: 'ResNet-18',
  internal_key: 'classification:resnet:v1:18'
};

function deploymentRelease(
  patch: Partial<DeploymentReleaseSummary> & Pick<DeploymentReleaseSummary, 'deployment_release_id' | 'model_version_id'>
): DeploymentReleaseSummary {
  return {
    target_path: 'realtime',
    architecture: baseArchitecture,
    triton_model_name: 'rt__example_v1',
    triton_model_version: 1,
    status: 'deactivated',
    serving_enabled: false,
    ...patch
  };
}

describe('model deployment selection', () => {
  it('keeps only deployments belonging to the selected model family', () => {
    const result = deploymentModelsForVersions(
      [
        deploymentRelease({ deployment_release_id: 'rel_selected', model_version_id: 'mvr_selected' }),
        deploymentRelease({ deployment_release_id: 'rel_other', model_version_id: 'mvr_other' })
      ],
      [{ model_version_id: 'mvr_selected' }]
    );

    expect(result.map((release) => release.deployment_release_id)).toEqual(['rel_selected']);
  });

  it('shows one entry per Triton model and prefers the currently serving release', () => {
    const result = deploymentModelsForVersions(
      [
        deploymentRelease({
          deployment_release_id: 'rel_newer_deactivated',
          model_version_id: 'mvr_2',
          serving_enabled: true,
          serving_release_id: 'rel_serving',
          triton_model_version: 2
        }),
        deploymentRelease({
          deployment_release_id: 'rel_serving',
          model_version_id: 'mvr_1',
          status: 'activated',
          serving_enabled: true,
          serving_release_id: 'rel_serving',
          triton_model_version: 1
        }),
        deploymentRelease({
          deployment_release_id: 'rel_batch',
          model_version_id: 'mvr_2',
          target_path: 'batch',
          triton_model_name: 'bt__example_v1'
        })
      ],
      [{ model_version_id: 'mvr_1' }, { model_version_id: 'mvr_2' }]
    );

    expect(result.map((release) => release.deployment_release_id)).toEqual([
      'rel_serving',
      'rel_batch'
    ]);
  });
});

const classificationTemplate: ModelTemplateSummary = {
  template_key: 'classification-resnet-18',
  label: 'ResNet-18 Classifier',
  description: 'Transfer-learning friendly classification preset.',
  task_type: 'classification',
  architecture: baseArchitecture,
  display_name_suffix: 'Classifier',
  default_training_profile: 'safe-default',
  defaults: {
    training: {
      epochs: 30,
      learning_rate: 0.0008,
      image_size: 224,
      batch_size: 32,
      optimizer: 'adamw',
      optimizer_settings: { weight_decay: 0.0005 },
      checkpoint_interval_steps: 20
    },
    promotion: null
  },
  default_promotion_policy: {
    criteria: [{ metric_name: 'accuracy_top1', comparison: 'gte', threshold: 0.9, phase: 'evaluation' }],
    allow_model_override: true
  },
  template_version: 1,
  recommended: true,
  active: true
};

const detectionArchitecture = {
  task_type: 'detection' as const,
  family: 'yolo',
  version: 'v8',
  size: 's',
  display_name: 'YOLOv8 Small',
  internal_key: 'detection:yolo:v8:s'
};

const detectionTemplate: ModelTemplateSummary = {
  template_key: 'detection-yolov8-s',
  label: 'YOLOv8 Small Detector',
  description: 'Quality-focused detection preset.',
  task_type: 'detection',
  architecture: detectionArchitecture,
  display_name_suffix: 'Detector',
  default_training_profile: 'quality-guarded',
  defaults: {
    training: {
      epochs: 55,
      learning_rate: 0.01,
      image_size: 768,
      batch_size: 12,
      optimizer: 'sgd',
      optimizer_settings: { momentum: 0.937, weight_decay: 0.0005 },
      checkpoint_interval_steps: 30
    },
    promotion: null
  },
  default_promotion_policy: {
    criteria: [{ metric_name: 'map_50_95', comparison: 'gte', threshold: 0.55, phase: 'evaluation' }],
    allow_model_override: true
  },
  template_version: 1,
  recommended: false,
  active: true
};

const capability: SupportedModelArchitecture = {
  architecture: baseArchitecture,
  supported: true,
  trainer_backend: 'torchvision-classification',
  concrete_architecture: 'resnet18',
  export_support: true,
  deployment_support: true,
  inference_support: true,
  transfer_learning_policy: 'auto_pretrained_le_2000',
  status: 'supported',
  status_flags: [],
  capability_metadata: {}
};

const detectionCapability: SupportedModelArchitecture = {
  architecture: detectionArchitecture,
  supported: true,
  trainer_backend: 'openmmlab-mmyolo',
  concrete_architecture: 'yolov8_s_syncbn_fast_8xb16-500e_coco',
  export_support: true,
  deployment_support: true,
  inference_support: true,
  transfer_learning_policy: 'auto_pretrained_le_2000',
  status: 'supported',
  status_flags: [],
  capability_metadata: {}
};

function readiness(patch: Partial<ProjectReadinessSummary>): ProjectReadinessSummary {
  return {
    task_count: 40,
    annotated_task_count: 40,
    annotation_count: 40,
    draft_task_count: 0,
    eligible_task_count: 40,
    submitted_labeled_task_count: 40,
    submitted_background_task_count: 0,
    invalid_task_count: 0,
    minimum_annotation_count: 25,
    required_classes: ['ok', 'nok'],
    class_distribution: { ok: 30, nok: 10 },
    train_ratio: 80,
    val_ratio: 10,
    test_ratio: 10,
    readiness_percent: 100,
    annotations_ready: true,
    latest_snapshot_ready: true,
    latest_valid_snapshot_ready: true,
    readiness_reason: 'Latest valid dataset is ready for model training.',
    ...patch
  };
}

describe('dataset-aware training recommendations', () => {
  it('keeps very small datasets conservative and increases validation share', () => {
    const recommendation = datasetAwareTrainingRecommendation({
      taskType: 'classification',
      readiness: readiness({ eligible_task_count: 40 }),
      template: classificationTemplate,
      capability
    });

    expect(recommendation?.initializationLabel).toBe('Transfer learning');
    expect(recommendation?.recommendedDefaults.learning_rate).toBeLessThan(
      classificationTemplate.defaults.training.learning_rate
    );
    expect(recommendation?.recommendedDefaults.batch_size).toBe(16);
    expect(recommendation?.recommendedDefaults.optimizer_settings.weight_decay).toBe(0.001);
    expect(recommendation?.split.val_ratio).toBeGreaterThanOrEqual(20);
    expect(recommendation?.split.test_ratio).toBe(0);
  });

  it('marks large datasets as scratch candidates after the auto-pretraining threshold', () => {
    const recommendation = datasetAwareTrainingRecommendation({
      taskType: 'classification',
      readiness: readiness({
        task_count: 3000,
        eligible_task_count: 3000,
        submitted_labeled_task_count: 3000,
        class_distribution: { ok: 1500, nok: 1500 }
      }),
      template: classificationTemplate,
      capability
    });

    expect(recommendation?.initializationLabel).toBe('Scratch candidate');
    expect(recommendation?.recommendedDefaults.epochs).toBeGreaterThan(
      classificationTemplate.defaults.training.epochs
    );
    expect(recommendation?.recommendedDefaults.learning_rate).toBeGreaterThan(
      classificationTemplate.defaults.training.learning_rate
    );
    expect(splitRatioLabel(recommendation!.split)).toBe('80/10/10');
  });

  it('warns when the weakest class is too thin for trustworthy validation', () => {
    const recommendation = datasetAwareTrainingRecommendation({
      taskType: 'classification',
      readiness: readiness({ class_distribution: { ok: 36, nok: 4 } }),
      template: classificationTemplate,
      capability
    });

    expect(recommendation?.warnings.join(' ')).toContain('nok');
    expect(recommendation?.classBalanceStrategy).toBe('weighted_loss');
  });

  it('recommends a balanced sampler when imbalance is strong but minority support is usable', () => {
    const recommendation = datasetAwareTrainingRecommendation({
      taskType: 'classification',
      readiness: readiness({ class_distribution: { ok: 160, nok: 20 } }),
      template: classificationTemplate,
      capability
    });

    expect(recommendation?.classBalanceStrategy).toBe('balanced_sampler');
  });

  it('keeps YOLO SGD learning-rate recommendations on the trainer lr0 baseline', () => {
    const recommendation = datasetAwareTrainingRecommendation({
      taskType: 'detection',
      readiness: readiness({
        task_count: 183,
        eligible_task_count: 183,
        submitted_labeled_task_count: 183,
        class_distribution: { Ast: 26, Brett: 157 }
      }),
      template: detectionTemplate,
      capability: detectionCapability
    });

    expect(recommendation?.initializationLabel).toBe('Transfer learning');
    expect(recommendation?.recommendedDefaults.learning_rate).toBe(0.01);
    expect(recommendation?.notes.join(' ')).toContain('YOLO SGD lr0 baseline');
  });

  it('raises stale YOLO SGD recommendation defaults back to the lr0 baseline', () => {
    const staleTemplate: ModelTemplateSummary = {
      ...detectionTemplate,
      defaults: {
        ...detectionTemplate.defaults,
        training: {
          ...detectionTemplate.defaults.training,
          learning_rate: 0.001
        }
      }
    };

    const recommendation = datasetAwareTrainingRecommendation({
      taskType: 'detection',
      readiness: readiness({ eligible_task_count: 183, submitted_labeled_task_count: 183 }),
      template: staleTemplate,
      capability: detectionCapability
    });

    expect(recommendation?.recommendedDefaults.learning_rate).toBe(0.01);
  });

  it('reads weight decay from optimizer settings only when it is numeric', () => {
    expect(weightDecayFromDefaults(classificationTemplate.defaults.training)).toBe(0.0005);
    expect(
      weightDecayFromDefaults({
        ...classificationTemplate.defaults.training,
        optimizer_settings: { weight_decay: '0.0005' }
      })
    ).toBeNull();
  });
});

describe('training progress projection', () => {
  it('uses the latest YOLO metric epoch when the worker percentage trails behind', () => {
    expect(
      resolveTrainingProgress({
        currentEpoch: 7,
        totalEpochs: 50,
        progressPercent: 10,
        latestMetricEpoch: 8
      })
    ).toEqual({ currentEpoch: 8, totalEpochs: 50, progressPercent: 16 });
  });

  it('clamps malformed progress values to the valid progressbar range', () => {
    expect(
      resolveTrainingProgress({
        currentEpoch: 120,
        totalEpochs: 100,
        progressPercent: 140
      })
    ).toEqual({ currentEpoch: 100, totalEpochs: 100, progressPercent: 100 });
  });
});
