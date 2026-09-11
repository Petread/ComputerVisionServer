export type TrainActionState = {
  kind: 'train' | 'create-model' | 'wait-for-dataset';
  label: string;
  detail: string;
  title: string;
};

export function getTrainActionState(input: {
  hasValidSnapshot: boolean;
  hasSelectedModel: boolean;
}): TrainActionState {
  if (!input.hasValidSnapshot) {
    return {
      kind: 'wait-for-dataset',
      label: 'Train new model',
      title: 'Waiting for a valid dataset',
      detail: 'Training starts once the latest valid dataset snapshot is ready.'
    };
  }

  if (!input.hasSelectedModel) {
    return {
      kind: 'create-model',
      label: 'Create model & train',
      title: 'Create the first model preset',
      detail: 'Choose one preset once. The app will generate internal names automatically and start training immediately afterwards.'
    };
  }

  return {
    kind: 'train',
    label: 'Train new model',
    title: 'Ready to train',
    detail: 'The selected model will train against the latest valid dataset snapshot in the background.'
  };
}
