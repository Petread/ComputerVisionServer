import type {
  ClassBalanceStrategy,
  DeploymentReleaseSummary,
  ModelArchitectureSummary,
  ModelVersionSummary,
  ModelTemplateDefaults,
  ModelTemplateSummary,
  PromotionCriteriaPolicy,
  ProjectReadinessSummary,
  SupportedModelArchitecture,
  TaskType,
  TrainingDefaults
} from '@cv-platform/schemas';

export function deploymentModelsForVersions(
  releases: readonly DeploymentReleaseSummary[],
  versions: readonly Pick<ModelVersionSummary, 'model_version_id'>[]
) {
  const modelVersionIds = new Set(versions.map((version) => version.model_version_id));
  const deploymentModels = new Map<string, DeploymentReleaseSummary>();

  for (const release of releases) {
    if (!modelVersionIds.has(release.model_version_id)) continue;
    const key = `${release.target_path}:${release.triton_model_name}`;
    const current = deploymentModels.get(key);
    const isServingRelease =
      release.serving_enabled
      && (!release.serving_release_id || release.serving_release_id === release.deployment_release_id);
    const currentIsServingRelease = current
      ? current.serving_enabled
        && (!current.serving_release_id || current.serving_release_id === current.deployment_release_id)
      : false;
    if (!current || (!currentIsServingRelease && isServingRelease)) {
      deploymentModels.set(key, release);
    }
  }

  return [...deploymentModels.values()];
}

export const taskTypeOptions: TaskType[] = [
  'classification',
  'detection',
  'oriented_detection',
  'segmentation',
  'anomaly'
];

export type SplitRecommendation = {
  train_ratio: number;
  val_ratio: number;
  test_ratio: number;
};

export type DatasetAwareTrainingRecommendation = {
  profileLabel: string;
  datasetLabel: string;
  datasetDetail: string;
  modelLabel: string;
  modelDetail: string;
  initializationLabel: string;
  initializationDetail: string;
  recommendedDefaults: TrainingDefaults;
  split: SplitRecommendation;
  currentSplit: SplitRecommendation;
  splitDetail: string;
  classBalanceStrategy: ClassBalanceStrategy;
  classBalanceLabel: string;
  classBalanceDetail: string;
  notes: string[];
  warnings: string[];
};

export function taskTypeLabel(taskType: TaskType) {
  if (taskType === 'classification') {
    return 'Classification';
  }
  if (taskType === 'detection') {
    return 'Detection';
  }
  if (taskType === 'oriented_detection') {
    return 'Oriented Detection';
  }
  if (taskType === 'segmentation') {
    return 'Segmentation';
  }
  if (taskType === 'anomaly') {
    return 'Anomaly';
  }
  return 'Temporal Sequence';
}

export function defaultTemplateForTask(taskType: TaskType) {
  if (taskType === 'detection') {
    return 'detection-yolov8-n';
  }
  if (taskType === 'oriented_detection') {
    return 'oriented_detection-yolov26-n';
  }
  if (taskType === 'segmentation') {
    return 'segmentation-deeplab-v3-resnet50';
  }
  if (taskType === 'anomaly') {
    return 'anomaly-patchcore-wide-resnet50-2';
  }
  return 'classification-resnet-18';
}

export function architectureLabel(architecture?: ModelArchitectureSummary | null) {
  if (!architecture) {
    return 'Not configured';
  }
  return architecture.display_name;
}

export function architectureSecondaryLabel(architecture?: ModelArchitectureSummary | null) {
  if (!architecture) {
    return 'No architecture metadata';
  }
  return `${architecture.family.toUpperCase()} ${architecture.version.toUpperCase()} / ${architecture.size.toUpperCase()}`;
}

export function architectureKey(input: {
  family: string;
  version: string;
  size: string;
}) {
  return `${input.family.toLowerCase()}:${input.version.toLowerCase()}:${input.size.toLowerCase()}`;
}

export function architectureKeyFromSummary(architecture: ModelArchitectureSummary) {
  return architectureKey(architecture);
}

export function templateDefaultsSummary(defaults?: ModelTemplateDefaults | null) {
  return trainingDefaultsSummary(defaults?.training);
}

export function trainingDefaultsSummary(defaults?: TrainingDefaults | null) {
  if (!defaults) {
    return 'No defaults configured';
  }
  const parts = [
    `${defaults.epochs} epochs`,
    `lr ${defaults.learning_rate}`,
    defaults.image_size ? `${defaults.image_size}px` : null,
    defaults.batch_size ? `batch ${defaults.batch_size}` : null,
    defaults.optimizer ? defaults.optimizer : null
  ].filter(Boolean);
  return parts.join(' / ');
}

export function weightDecayFromDefaults(defaults?: TrainingDefaults | null) {
  const raw = defaults?.optimizer_settings?.weight_decay;
  return typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
}

export function splitRatioLabel(split: SplitRecommendation) {
  return `${split.train_ratio}/${split.val_ratio}/${split.test_ratio}`;
}

export function formatTrainingNumber(value: number) {
  return Number(value.toPrecision(3)).toString();
}

export function resolveTrainingProgress(input: {
  currentEpoch: number;
  totalEpochs: number;
  progressPercent: number;
  latestMetricEpoch?: number | null;
}) {
  const totalEpochs = Math.max(0, Math.floor(finiteNumber(input.totalEpochs)));
  const observedEpoch = Math.max(
    0,
    Math.floor(finiteNumber(input.currentEpoch)),
    Math.floor(finiteNumber(input.latestMetricEpoch ?? 0))
  );
  const currentEpoch = totalEpochs ? Math.min(observedEpoch, totalEpochs) : observedEpoch;
  const epochPercent = totalEpochs ? (currentEpoch / totalEpochs) * 100 : 0;
  const progressPercent = Math.min(
    100,
    Math.max(0, finiteNumber(input.progressPercent), epochPercent)
  );
  return { currentEpoch, totalEpochs, progressPercent };
}

export function metricChartDomain(values: number[]) {
  const finiteValues = values.filter(Number.isFinite);
  if (!finiteValues.length) {
    return { min: 0, max: 1, range: 1 };
  }
  const rawMin = Math.min(...finiteValues);
  const rawMax = Math.max(...finiteValues);
  const rawRange = rawMax - rawMin;
  const reference = Math.max(Math.abs(rawMin), Math.abs(rawMax), 0.000001);
  const padding = (rawRange || reference) * 0.08;
  const min = rawMin - padding;
  const max = rawMax + padding;
  return { min, max, range: max - min };
}

function finiteNumber(value: number) {
  return Number.isFinite(value) ? value : 0;
}

export function datasetAwareTrainingRecommendation(input: {
  taskType: TaskType;
  readiness: ProjectReadinessSummary;
  template?: ModelTemplateSummary | null;
  capability?: SupportedModelArchitecture | null;
}): DatasetAwareTrainingRecommendation | null {
  if (!input.template) {
    return null;
  }

  const readiness = input.readiness;
  const template = input.template;
  const defaults = template.defaults.training;
  const eligibleCount = Math.max(
    readiness.eligible_task_count,
    readiness.submitted_labeled_task_count + readiness.submitted_background_task_count,
    0
  );
  const classEntries = Object.entries(readiness.class_distribution ?? {}).filter(
    ([, count]) => Number(count) > 0
  );
  const classCount = Math.max(classEntries.length, readiness.required_classes?.length ?? 0);
  const lowestClass = classEntries.reduce<{ label: string; count: number } | null>((lowest, [label, count]) => {
    const numericCount = Number(count);
    if (!Number.isFinite(numericCount)) {
      return lowest;
    }
    if (!lowest || numericCount < lowest.count) {
      return { label, count: numericCount };
    }
    return lowest;
  }, null);
  const scratchMinItems = scratchThresholdFromPolicy(input.capability?.transfer_learning_policy);
  const isAnomaly = input.taskType === 'anomaly';
  const useTransfer = isAnomaly || eligibleCount <= scratchMinItems;
  const datasetBand = datasetBandForCount(eligibleCount);
  const currentSplit = {
    train_ratio: readiness.train_ratio,
    val_ratio: readiness.val_ratio,
    test_ratio: readiness.test_ratio
  };
  const split = recommendedSplit({
    eligibleCount,
    classCount,
    taskType: input.taskType
  });
  const recommendedDefaults = recommendedDefaultsForDataset({
    defaults,
    eligibleCount,
    taskType: input.taskType,
    useTransfer
  });
  const backgroundRatio = eligibleCount
    ? readiness.submitted_background_task_count / eligibleCount
    : 0;
  const classBalance = recommendedClassBalanceStrategy({
    taskType: input.taskType,
    classDistribution: readiness.class_distribution ?? {}
  });
  const notes = [
    learningRateRecommendationNote({
      defaults,
      eligibleCount,
      taskType: input.taskType,
      useTransfer,
      scratchMinItems
    }),
    splitRatioLabel(currentSplit) === splitRatioLabel(split)
      ? 'Current project split already matches the recommendation.'
      : `Current project split is ${splitRatioLabel(currentSplit)}; use ${splitRatioLabel(split)} for the next snapshot if validation is too thin.`,
    classEntries.length
      ? `Class balance is visible for ${classEntries.length} classes.`
      : 'Class balance is not available yet, so the split recommendation is based on dataset size.',
    classBalance.detail
  ];
  const warnings: string[] = [];

  if (eligibleCount < Math.max(readiness.minimum_annotation_count, 25)) {
    warnings.push('Dataset is still very small; treat metrics as directional until more finalized samples are available.');
  }
  if (lowestClass && lowestClass.count < 5) {
    warnings.push(`Class "${lowestClass.label}" has only ${lowestClass.count} finalized samples; collect more before trusting validation metrics.`);
  }
  if (readiness.invalid_task_count > 0) {
    warnings.push(`${readiness.invalid_task_count} invalid item(s) should be cleaned before the next training snapshot.`);
  }
  if (
    (input.taskType === 'detection' ||
      input.taskType === 'oriented_detection' ||
      input.taskType === 'segmentation') &&
    backgroundRatio > 0.35
  ) {
    warnings.push('Background-only images are high; keep them, but make sure object examples still dominate each class.');
  }
  if (isLargeArchitecture(template.architecture) && eligibleCount > 0 && eligibleCount < 500) {
    warnings.push('Selected model is relatively large for this dataset; a smaller preset is usually the safer first baseline.');
  }
  if (classBalance.warning) {
    warnings.push(classBalance.warning);
  }

  return {
    profileLabel: datasetBand.profileLabel,
    datasetLabel: datasetBand.label,
    datasetDetail: `${eligibleCount} eligible item${eligibleCount === 1 ? '' : 's'}${
      classCount ? ` across ${classCount} class${classCount === 1 ? '' : 'es'}` : ''
    }.`,
    modelLabel: template.label,
    modelDetail: isLargeArchitecture(template.architecture) && eligibleCount < 500
      ? 'Prefer this only after a smaller baseline is stable.'
      : 'This template fits the current task and backend capability.',
    initializationLabel: isAnomaly ? 'Pretrained feature backbone' : useTransfer ? 'Transfer learning' : 'Scratch candidate',
    initializationDetail: isAnomaly
      ? 'PatchCore uses a pretrained backbone and calibrates anomaly scores from the dataset.'
      : useTransfer
        ? `Auto policy uses pretrained weights at or below ${scratchMinItems} items.`
        : `Auto policy switches to scratch above ${scratchMinItems} items.`,
    recommendedDefaults,
    split,
    currentSplit,
    splitDetail: `Suggested train/validation/test split: ${splitRatioLabel(split)}.`,
    classBalanceStrategy: classBalance.strategy,
    classBalanceLabel: classBalance.label,
    classBalanceDetail: classBalance.detail,
    notes,
    warnings
  };
}

export function promotionPolicySummary(policy?: PromotionCriteriaPolicy | null) {
  if (!policy?.criteria.length) {
    return 'No promotion criteria configured';
  }
  return policy.criteria
    .map((criterion) => `${criterion.metric_name} ${criterion.comparison} ${criterion.threshold}`)
    .join(' / ');
}

export function templateOptionLabel(template: ModelTemplateSummary) {
  return `${template.label} / ${architectureLabel(template.architecture)}`;
}

export function compatibleTemplatesForSelection(input: {
  templates: ModelTemplateSummary[];
  taskType: TaskType;
  family: string;
  version: string;
  size: string;
}) {
  const key = architectureKey({
    family: input.family,
    version: input.version,
    size: input.size
  });
  return input.templates.filter(
    (template) =>
      template.active &&
      template.task_type === input.taskType &&
      architectureKeyFromSummary(template.architecture) === key
  );
}

export function supportedArchitecturesForTask(
  architectures: SupportedModelArchitecture[],
  taskType: TaskType
) {
  return architectures.filter((item) => item.architecture.task_type === taskType && item.supported);
}

export function recommendedTemplateForSelection(input: {
  templates: ModelTemplateSummary[];
  taskType: TaskType;
  family: string;
  version: string;
  size: string;
}) {
  const compatible = compatibleTemplatesForSelection(input);
  return compatible.find((template) => template.recommended) ?? compatible[0] ?? null;
}

function scratchThresholdFromPolicy(policy?: string | null) {
  const match = /le_(\d+)/.exec(policy ?? '');
  return match ? Number(match[1]) : 2000;
}

function datasetBandForCount(count: number) {
  if (count < 50) {
    return {
      label: 'Very small dataset',
      profileLabel: 'Conservative small-data profile'
    };
  }
  if (count < 200) {
    return {
      label: 'Small dataset',
      profileLabel: 'Transfer-learning profile'
    };
  }
  if (count < 2000) {
    return {
      label: 'Growing dataset',
      profileLabel: 'Balanced training profile'
    };
  }
  return {
    label: 'Large dataset',
    profileLabel: 'High-data training profile'
  };
}

function recommendedDefaultsForDataset(input: {
  defaults: TrainingDefaults;
  eligibleCount: number;
  taskType: TaskType;
  useTransfer: boolean;
}): TrainingDefaults {
  const { defaults, eligibleCount, taskType, useTransfer } = input;
  if (taskType === 'anomaly') {
    return defaults;
  }

  const epochMultiplier = epochMultiplierForDataset({ eligibleCount, useTransfer });
  const weightDecay = recommendedWeightDecay(defaults, eligibleCount);
  const optimizerSettings = {
    ...defaults.optimizer_settings,
    ...(weightDecay === null ? {} : { weight_decay: weightDecay })
  };

  return {
    ...defaults,
    epochs: Math.max(1, Math.round(defaults.epochs * epochMultiplier)),
    learning_rate: recommendedLearningRate({ defaults, eligibleCount, taskType, useTransfer }),
    batch_size: recommendedBatchSize(defaults.batch_size, eligibleCount, taskType),
    optimizer_settings: optimizerSettings
  };
}

function recommendedLearningRate(input: {
  defaults: TrainingDefaults;
  eligibleCount: number;
  taskType: TaskType;
  useTransfer: boolean;
}) {
  if (usesYoloStyleSgdDefaults(input.defaults, input.taskType)) {
    return roundLearningRate(Math.max(input.defaults.learning_rate, 0.01));
  }
  const lrMultiplier = learningRateMultiplier({
    eligibleCount: input.eligibleCount,
    useTransfer: input.useTransfer
  });
  return roundLearningRate(input.defaults.learning_rate * lrMultiplier);
}

function learningRateRecommendationNote(input: {
  defaults: TrainingDefaults;
  eligibleCount: number;
  taskType: TaskType;
  useTransfer: boolean;
  scratchMinItems: number;
}) {
  if (input.taskType === 'anomaly') {
    return 'Anomaly training keeps the template learning-rate defaults for threshold calibration.';
  }
  if (usesYoloStyleSgdDefaults(input.defaults, input.taskType)) {
    return 'Learning rate follows the YOLO SGD lr0 baseline instead of small-data scaling, because the trainer schedule is tuned around that optimizer default.';
  }
  return input.useTransfer
    ? `Learning rate is kept conservative because this profile uses pretrained initialization through ${input.scratchMinItems} items.`
    : `Dataset size is above ${input.scratchMinItems} items, so scratch training can be tested after a transfer-learning baseline.`;
}

function usesYoloStyleSgdDefaults(defaults: TrainingDefaults, taskType: TaskType) {
  return (
    (taskType === 'detection' || taskType === 'oriented_detection' || taskType === 'segmentation') &&
    String(defaults.optimizer ?? '').toLowerCase() === 'sgd'
  );
}

function learningRateMultiplier(input: { eligibleCount: number; useTransfer: boolean }) {
  if (!input.useTransfer) {
    return 1.5;
  }
  if (input.eligibleCount < 50) {
    return 0.5;
  }
  if (input.eligibleCount < 200) {
    return 0.65;
  }
  if (input.eligibleCount < 1000) {
    return 0.85;
  }
  return 1;
}

function epochMultiplierForDataset(input: { eligibleCount: number; useTransfer: boolean }) {
  if (!input.useTransfer) {
    return 1.5;
  }
  if (input.eligibleCount < 50) {
    return 0.75;
  }
  if (input.eligibleCount < 200) {
    return 1;
  }
  if (input.eligibleCount < 1000) {
    return 1.15;
  }
  return 1.25;
}

function recommendedBatchSize(
  batchSize: number | null | undefined,
  eligibleCount: number,
  taskType: TaskType
) {
  if (!batchSize) {
    return batchSize ?? null;
  }
  if (eligibleCount <= 0) {
    return batchSize;
  }
  const smallDataCap =
    taskType === 'segmentation'
      ? 4
      : taskType === 'detection' || taskType === 'oriented_detection'
        ? 8
        : 16;
  const cappedForData = eligibleCount < 100 ? Math.min(batchSize, smallDataCap) : batchSize;
  return Math.max(1, Math.min(cappedForData, eligibleCount));
}

function recommendedWeightDecay(defaults: TrainingDefaults, eligibleCount: number) {
  const weightDecay = weightDecayFromDefaults(defaults);
  if (weightDecay === null) {
    return null;
  }
  if (eligibleCount > 0 && eligibleCount < 200) {
    return Number(Math.min(weightDecay * 2, 0.001).toPrecision(3));
  }
  return weightDecay;
}

function recommendedSplit(input: {
  eligibleCount: number;
  classCount: number;
  taskType: TaskType;
}): SplitRecommendation {
  let split: SplitRecommendation;
  if (input.taskType === 'anomaly') {
    split = input.eligibleCount < 100
      ? { train_ratio: 70, val_ratio: 30, test_ratio: 0 }
      : { train_ratio: 70, val_ratio: 20, test_ratio: 10 };
  } else if (input.eligibleCount < 50) {
    split = { train_ratio: 80, val_ratio: 20, test_ratio: 0 };
  } else if (input.eligibleCount < 150) {
    split = { train_ratio: 70, val_ratio: 20, test_ratio: 10 };
  } else if (input.eligibleCount < 500) {
    split = { train_ratio: 75, val_ratio: 15, test_ratio: 10 };
  } else {
    split = { train_ratio: 80, val_ratio: 10, test_ratio: 10 };
  }

  if (input.eligibleCount > 0 && input.classCount > 0) {
    const targetValidationCount = Math.max(10, input.classCount * 5);
    const minimumValidationRatio = Math.ceil((targetValidationCount / input.eligibleCount) * 100);
    if (minimumValidationRatio > split.val_ratio) {
      const nextVal = Math.min(30, minimumValidationRatio);
      const nextTest = split.test_ratio;
      split = {
        train_ratio: Math.max(100 - nextVal - nextTest, 50),
        val_ratio: nextVal,
        test_ratio: nextTest
      };
      const total = split.train_ratio + split.val_ratio + split.test_ratio;
      if (total !== 100) {
        split = { ...split, test_ratio: Math.max(0, split.test_ratio + (100 - total)) };
      }
    }
  }

  return split;
}

function recommendedClassBalanceStrategy(input: {
  taskType: TaskType;
  classDistribution: Record<string, number>;
}): {
  strategy: ClassBalanceStrategy;
  label: string;
  detail: string;
  warning?: string;
} {
  const counts = Object.values(input.classDistribution)
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value > 0);
  if (input.taskType !== 'classification' || counts.length < 2) {
    return {
      strategy: 'none',
      label: 'No class balancing',
      detail: 'Class balancing is only recommended when multiple classification classes are imbalanced.'
    };
  }
  const minimum = Math.min(...counts);
  const maximum = Math.max(...counts);
  const ratio = maximum / Math.max(minimum, 1);
  if (ratio < 2) {
    return {
      strategy: 'none',
      label: 'No class balancing',
      detail: `Class distribution is acceptable; strongest/weakest class ratio is ${ratio.toFixed(1)}:1.`
    };
  }
  if (ratio >= 5 && minimum >= 10) {
    return {
      strategy: 'balanced_sampler',
      label: 'Balanced sampler',
      detail: `Strong imbalance detected (${ratio.toFixed(1)}:1); sampling can rebalance batches because the weakest class has ${minimum} samples.`,
      warning: `Class imbalance is strong (${ratio.toFixed(1)}:1); apply balanced sampling or collect more minority samples.`
    };
  }
  return {
    strategy: 'weighted_loss',
    label: 'Weighted loss',
    detail: `Imbalance detected (${ratio.toFixed(1)}:1); weighted loss is safer while the weakest class has ${minimum} samples.`,
    warning: `Class imbalance is visible (${ratio.toFixed(1)}:1); weighted loss is recommended for the next classification run.`
  };
}

function roundLearningRate(value: number) {
  return Number(value.toPrecision(3));
}

function isLargeArchitecture(architecture: ModelArchitectureSummary) {
  const size = architecture.size.toLowerCase();
  return ['m', 'l', 'x', '50', 'resnet50', 'large', 'wide_resnet50_2'].includes(size);
}
