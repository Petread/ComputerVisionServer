import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { BarkResultView, barkChartRuns, barkResultFromExecution, effectiveBarkMapping } from './bark-inspection-workspace';

describe('paired bark inspection', () => {
  it('requires an explicit mapping and applies a job override without changing the default', () => {
    expect(effectiveBarkMapping('', '')).toBeNull();
    expect(effectiveBarkMapping('left_first', '')).toBe('left_first');
    expect(effectiveBarkMapping('left_first', 'left_last')).toBe('left_last');
    expect(effectiveBarkMapping('left_first', '')).toBe('left_first');
    expect(effectiveBarkMapping('left_first', 'unexpected')).toBeNull();
  });
  it('plots true zero and breaks the line at unavailable measurements', () => {
    expect(barkChartRuns([
      { angle_deg: 0, valid: true, thickness_mm: 0 },
      { angle_deg: 1, valid: true, thickness_mm: 2 },
      { angle_deg: 2, valid: false, thickness_mm: null },
      { angle_deg: 3, valid: true, thickness_mm: 5 }
    ])).toEqual([[[0, 0], [1, 2]], [[3, 5]]]);
  });
  it('finds a result independent of configured node names and displays side provenance', () => {
    const side = { reference: { side: 'left', slice_role: 'last', mapping_source: 'job',
      slice: { csv_line: 30, position_mm: 5000 } }, valid: true, mean_mm: 0,
      valid_fraction: 1, samples: [], reason_codes: [] };
    const result = { schema_version: 'stem.bark-measurement.v1', workpiece_id: '11', mean_mm: null,
      metrology_label: 'noch nicht messtechnisch freigegeben', left: side,
      right: { ...side, valid: false, mean_mm: null, reference: { ...side.reference, slice_role: 'first' },
        reason_codes: ['registration_outline_ambiguous'] } };
    const execution = { node_outputs: { custom_pair: { result: { result } } } };
    expect(barkResultFromExecution(execution)).toBe(result);
    const html = renderToStaticMarkup(<BarkResultView value={execution} />);
    expect(html).toContain('letzte');
    expect(html).toContain('5000');
    expect(html).toContain('0.00 mm');
    expect(html).toContain('eindeutige Kalibrierung');
    expect(html).toContain('nicht verfügbar');
  });
});
