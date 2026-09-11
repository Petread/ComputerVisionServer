import { describe, expect, it } from 'vitest';
import { getTrainActionState } from './workflow';

describe('pipeline workflow helpers', () => {
  it('asks for a model when a dataset is ready but no model exists', () => {
    expect(
      getTrainActionState({
        hasValidSnapshot: true,
        hasSelectedModel: false
      })
    ).toMatchObject({
      kind: 'create-model',
      label: 'Create model & train'
    });
  });

  it('waits for a dataset before training', () => {
    expect(
      getTrainActionState({
        hasValidSnapshot: false,
        hasSelectedModel: false
      })
    ).toMatchObject({
      kind: 'wait-for-dataset',
      label: 'Train new model'
    });
  });

  it('allows training when both model and dataset are ready', () => {
    expect(
      getTrainActionState({
        hasValidSnapshot: true,
        hasSelectedModel: true
      })
    ).toMatchObject({
      kind: 'train',
      label: 'Train new model'
    });
  });
});
