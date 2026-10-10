import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import type { RuntimeResultRecordDetail } from '@cv-platform/schemas';
import { ChannelResultInspection, resultSummary } from './channel-results-workspace';

const result: RuntimeResultRecordDetail = {
  result_record_id: 'result-1', channel_id: 'channel-1', correlation_id: 'correlation-1',
  completed_at: '2026-09-13T10:00:00Z', mode: 'realtime', status: 'completed',
  result_type: 'realtime_frame', schema_version: 'runtime.execution.result.v1',
  result: {}, trigger_response: {}, derived_preview: {}, timing: {}, model_routes: [],
  inference_output_count: 0, derived_output_count: 0, overlay_primitive_count: 0,
  raw_inference_outputs: [], normalized_inference_outputs: [], derived_outputs: [],
  overlay_primitives: [], runtime_metadata: {}, execution_result: {}
};

describe('ChannelResultInspection', () => {
  it('keeps zero, false and absent measurements distinct and retains nested values', () => {
    const html = renderToStaticMarkup(<ChannelResultInspection result={{...result, trigger_response: {count: 0, accepted: false, length: null, nested: {quality: 'unknown'}}}}/>);
    expect(html).toContain('<dd>0</dd>');
    expect(html).toContain('<dd>false</dd>');
    expect(html).toContain('<dd>—</dd>');
    expect(html).toContain('Struktur anzeigen');
    expect(html).toContain('unknown');
  });
  it('identifies incomplete failed outputs without inventing a duration or measurements', () => {
    const html = renderToStaticMarkup(<ChannelResultInspection result={{...result, status: 'failed', execution_result: {error: 'Missing camera frame'}}}/>);
    expect(html).toContain('unvollständig');
    expect(html).toContain('keine zusammengefassten Messwerte');
    expect(html).toContain('Nicht erfasst');
    expect(html).not.toContain('0 ms');
    expect(html).toContain('Missing camera frame');
  });
  it('does not present a raw stem correction as an unqualified list summary', () => {
    expect(resultSummary({...result, trigger_response: {schema_version: 'stem.orientation.v3', signed_correction_deg: 42, confidence: 0.01}})).toBe('Fachliche Auswertung verfügbar');
  });
  it('shows a selection prompt instead of empty result data', () => {
    const html = renderToStaticMarkup(<ChannelResultInspection result={null}/>);
    expect(html).toContain('Ausführung auswählen');
    expect(html).not.toContain('Abgeschlossen');
  });
});
