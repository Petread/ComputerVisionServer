import { describe, expect, it } from 'vitest';
import type { RuntimePipelineGraph } from '@cv-platform/schemas';
import { pipelineTrainingImages } from './pipeline-training-capture';

const graph = { nodes: [
  { node_key: 'split_faces', node_type: 'preprocessing.endface_pair_split' },
  { node_key: 'left_metric', node_type: 'preprocessing.endface_metric_rectify' },
  { node_key: 'right_metric', node_type: 'preprocessing.endface_metric_rectify' },
  { node_key: 'left_bark', node_type: 'analysis.bark_radial_thickness' }
], edges: [] } as unknown as RuntimePipelineGraph;
const frame = (id: string, side = 'left') => ({ payload_type: 'frame', image_base64: `pixels-${id}`,
  mime_type: 'image/png', width: 50, height: 40, image_context: { context_id: id, width: 50, height: 40 },
  metadata: { endface_reference: { side }, registration_hash: id } });
const metric = (id: string, side = 'left') => ({ frame: frame(id, side),
  overlay: frame('overlay'), isolated: frame('isolated'),
  eligibility: { result: { eligible: true, image_context_id: id } } });

describe('pipeline stage training capture', () => {
  it('offers split and rectified images separately without training on overlays', () => {
    const left = metric('left-rectified');
    const images = pipelineTrainingImages(graph, { node_outputs: {
      split_faces: { left_frame: frame('left-split'), right_frame: frame('right-split', 'right') },
      left_metric: left, right_metric: metric('right-rectified', 'right'), left_bark: { overlay: frame('rays') }
    } });
    expect(images).toHaveLength(4);
    expect(images.every(image => image.allowed)).toBe(true);
    expect(images[0].label).toContain('Modell 1');
    expect(images[2].label).toContain('Modell 2');
    expect(images[3].label).toContain('Rechts');
    expect(images[2].frame.image_base64).toBe(left.frame.image_base64);
    expect(images[2].frame.image_context).toBe(left.frame.image_context);
    expect(images[2].frame.metadata).toBe(left.frame.metadata);
    expect(images[2].requireEligibility).toBe(true);
  });
  it.each([null, { eligible: false, image_context_id: 'rectified' },
    { eligible: true, image_context_id: 'another-frame' }])('blocks invalid or mismatched rectification eligibility %j', eligibility => {
    const images = pipelineTrainingImages(graph, { node_outputs: {
      left_metric: { frame: frame('rectified'), eligibility: { result: eligibility } }
    } });
    expect(images[0].allowed).toBe(false);
  });
  it('supports preparing model 1 data before any inference model is available', () => {
    const images = pipelineTrainingImages(graph, { node_outputs: {
      split_faces: { left_frame: frame('left'), right_frame: frame('right', 'right') }
    } });
    expect(images).toHaveLength(2);
    expect(images.every(image => image.allowed && !image.requireEligibility)).toBe(true);
  });
});
