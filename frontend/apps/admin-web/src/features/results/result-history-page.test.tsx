import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import {
  ResultDetailPanel,
  ResultFilterPanel,
  RuntimeResultCard,
  StemOrientationResultSummary,
  StemOrientationReviewPanel,
  StemShadowEvaluationPanel
} from './result-history-page';

const summary = {
  result_record_id: 'rrc_1',
  channel_id: 'chn_press',
  channel_name: 'Press inspection',
  technical_place: 'press-line-1',
  pipeline_version_id: 'plv_press',
  runtime_execution_id: 'rex_1',
  correlation_id: 'corr_1',
  source_timestamp: '2026-05-02T08:59:59Z',
  completed_at: '2026-05-02T09:00:00Z',
  mode: 'realtime' as const,
  status: 'completed' as const,
  result_type: 'realtime_frame',
  schema_version: 'runtime.execution.result.v1',
  result: {},
  trigger_response: { count: 2, result: true },
  derived_preview: { count: 2 },
  inference_output_count: 1,
  derived_output_count: 1,
  overlay_primitive_count: 1,
  timing: { total_ms: 18.4 },
  model_routes: [{ deployment_release_id: 'rel_1' }]
};

describe('RuntimeResultCard', () => {
  it('renders compact operator result fields', () => {
    const html = renderToStaticMarkup(<RuntimeResultCard result={summary} selected onSelect={vi.fn()} />);

    expect(html).toContain('Press inspection');
    expect(html).toContain('Realtime Frame');
    expect(html).toContain('Count');
    expect(html).toContain('2');
  });
});

describe('ResultDetailPanel', () => {
  it('shows typed inference, calculated, overlay, and metadata sections', () => {
    const html = renderToStaticMarkup(
      <ResultDetailPanel
        result={{
          ...summary,
          raw_inference_outputs: [{ node_key: 'detect', gateway_response: { route: 'rel_1' } }],
          normalized_inference_outputs: [{ node_key: 'detect', payload: { payload_type: 'detections' } }],
          derived_outputs: [{ node_key: 'count', payload: { payload_type: 'measurements' } }],
          overlay_primitives: [{ primitive_type: 'measurement', label: 'count', geometry: {} }],
          runtime_metadata: { executor_service: 'realtime-executor' },
          execution_result: { final_response: { count: 2 } }
        }}
      />
    );

    expect(html).toContain('Final Response');
    expect(html).toContain('Calculated Values');
    expect(html).toContain('Normalized Inference Outputs');
    expect(html).toContain('Overlay primitives');
    expect(html).toContain('realtime-executor');
  });
});

describe('ResultFilterPanel', () => {
  it('renders channel, pipeline, mode, status, type, and time filters', () => {
    const html = renderToStaticMarkup(
      <ResultFilterPanel
        filters={{
          search: '',
          channelId: '',
          pipelineVersionId: '',
          mode: 'all',
          status: 'all',
          resultType: '',
          completedFrom: '',
          completedTo: ''
        }}
        onChange={vi.fn()}
        channels={[{ channel_id: 'chn_press', name: 'Press inspection', technical_place: 'press-line-1' }]}
        pipelines={[
          {
            pipeline_definition_id: 'pdef_1',
            slug: 'press-pipeline',
            name: 'Press pipeline',
            status: 'published',
            latest_version_id: 'plv_press',
            published_version_id: 'plv_press',
            template: false,
            scope: 'template',
            version_count: 1
          }
        ]}
      />
    );

    expect(html).toContain('Press inspection / press-line-1');
    expect(html).toContain('Press pipeline / published');
    expect(html).toContain('All modes');
    expect(html).toContain('All status');
    expect(html).toContain('All result types');
  });
});

describe('Stem orientation result review', () => {
  it('renders the frozen pre-entry recommendation and separate post-check state', () => {
    const html = renderToStaticMarkup(
      <StemOrientationResultSummary result={{
        schema_version: 'stem.orientation.v2',
        session_id: 'session-7',
        track_id: 'track-4',
        decision_ready: true,
        confidence: 0.84,
        reason_codes: ['projected_minimum_bracketed'],
        observation_started_at: '2026-08-03T09:00:00Z',
        decision: { ready: true, frame_sequence: 152, timestamp: '2026-08-03T09:00:02Z' },
        recommendation: {
          signed_rotation_deg: -43,
          direction: 'counter_clockwise_viewed_from_configured_log_end',
          rotation_viewpoint_convention: 'viewed_from_configured_log_end_a',
          positive_rotation_direction: 'clockwise_viewed_from_configured_log_end'
        },
        target_estimate: { frame_start: 103, frame_end: 108, candidate_type: 'bracketed_minimum' },
        compact_curvature_trace: { samples: [{ frame_sequence: 103, signed_bow_normalized: 0.2, rms_residual_normalized: 0.1 }] },
        compact_rotation_trace: { samples: [{ frame_sequence: 103, cumulative_rotation_deg: 40, rotation_confidence: 0.9, inlier_ratio: 0.8 }] },
        asset_references: { status: 'not_recorded', selected_frames: [{ role: 'decision', frame_sequence: 152, asset_ref: null }] },
        post_rotation_verification: null,
        versions: {
          pipeline_version_id: 'plv_stem',
          segmentation_model_version_id: 'mdl_stem',
          intrinsic_calibration_hash: 'intrinsic-hash',
          rotation_calibration_id: 'rotcal-1',
          process_zone_calibration_id: 'zones-1',
          process_zone_calibration_version: 'v3',
          feature_schema_version: 'stem.geometry.v1',
          rotation_algorithm_version: 'stem.surface-rotation.lk-affine.v1'
        }
      }} />
    );

    expect(html).toContain('Drehe 43° gegen den Uhrzeigersinn');
    expect(html).toContain('Konfidenz 0,84');
    expect(html).toContain('projected_minimum_bracketed');
    expect(html).toContain('rotcal-1');
    expect(html).toContain('zones-1');
    expect(html).toContain('Kompakte Krümmungs- und Rotationsspur');
    expect(html).toContain('die Vor-Einzug-Empfehlung bleibt unverändert');
  });

  it('renders the curvature-minimum v3 angle and calibrated phase evidence', () => {
    const html = renderToStaticMarkup(
      <StemOrientationResultSummary result={{
        schema_version: 'stem.orientation.v3',
        algorithm: 'CURVATURE_PHASE_MINIMUM',
        shadow_mode: true,
        plc_writes_suppressed: true,
        shadow_evaluation_approved: false,
        minimum_principle_validation_id: 'reference-validation-v1',
        minimum_principle_validation_status: 'VALIDATION_PASSED',
        session_id: 'session-v3',
        track_id: 'track-v3',
        decision_ready: true,
        decision: 'ROTATE',
        recommended_rotation_deg_signed: -34.5,
        direction: 'COUNTER_CLOCKWISE',
        overall_confidence: 0.88,
        decision_frame_sequence: 211,
        decision_timestamp: '2026-08-04T10:00:01Z',
        session_started_timestamp: '2026-08-04T09:59:58Z',
        target_phase_deg: 110,
        current_phase_deg: 144.5,
        target_observed: true,
        rotation_reference_view: 'LOOKING_IN_CONVEYOR_DIRECTION',
        positive_rotation: 'CLOCKWISE',
        curvature_min_mm: 2.1,
        curvature_max_mm: 8.8,
        curvature_span_mm: 6.7,
        observed_rotation_span_deg: 120,
        calibration_id: 'process-v1',
        rotation_calibration_id: 'phase-v1',
        curvature_algorithm_version: 'curvature-v1',
        solver_algorithm_version: 'solver-v1',
        reason_codes: ['CURVATURE_MINIMUM_CORRECTION_READY'],
        diagnostic_graph: {
          schema_version: 'stem.curvature-phase-diagnostic.v1',
          observed_phase_span_deg: 120,
          points: [
            { frame_id: '201', cumulative_phase_deg: 20, curvature_rms_mm: 8, fit_weight: 0.8, status: 'weighted', reason_codes: [] },
            { frame_id: '202', cumulative_phase_deg: 50, curvature_rms_mm: 5, fit_weight: 0.2, status: 'downweighted', reason_codes: ['ROBUST_OUTLIER'] },
            { frame_id: '203', cumulative_phase_deg: 80, curvature_rms_mm: 4, fit_weight: 0, status: 'ignored', reason_codes: ['LOW_CONFIDENCE'] }
          ],
          fit_curve: [
            { cumulative_phase_deg: 0, fitted_curvature_mm: 8 },
            { cumulative_phase_deg: 90, fitted_curvature_mm: 2 },
            { cumulative_phase_deg: 180, fitted_curvature_mm: 8 }
          ],
          minimum_phase_deg: 90,
          minimum_curvature_mm: 2,
          maximum_phase_deg: 0,
          maximum_curvature_mm: 8,
          current_phase_deg: 144.5,
          decision_phase_deg: 144.5,
          reason_codes: ['CURVATURE_MINIMUM_CORRECTION_READY']
        }
      }} />
    );

    expect(html).toContain('Drehe 34,5° gegen den Uhrzeigersinn');
    expect(html).toContain('Konfidenz 0,88');
    expect(html).toContain('Zielphase / aktuelle Phase');
    expect(html).toContain('CURVATURE_MINIMUM_CORRECTION_READY');
    expect(html).toContain('process-v1');
    expect(html).toContain('Shadow Mode');
    expect(html).toContain('SPS-Ausgabe unterdrueckt');
    expect(html).toContain('VALIDATION PASSED');
    expect(html).toContain('reference-validation-v1');
    expect(html).toContain('Krümmung über Rollphase');
    expect(html).toContain('2 gewichtet · 1 ignoriert');
    expect(html).toContain('robust abgewichtet');
  });

  it('keeps the quick review inside the existing result detail workflow', () => {
    const queryClient = new QueryClient();
    const html = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <StemOrientationReviewPanel resultRecordId="rrc_stem" />
      </QueryClientProvider>
    );

    expect(html).toContain('Stammausrichtungs-Schnellreview');
    expect(html).toContain('Vorschlag plausibel');
    expect(html).toContain('Finale Lage');
    expect(html).toContain('Neue Review-Revision speichern');
  });

  it('limits v3 review to technical plausibility and error causes', () => {
    const queryClient = new QueryClient();
    const html = renderToStaticMarkup(
      <QueryClientProvider client={queryClient}>
        <StemOrientationReviewPanel resultRecordId="rrc_stem_v3" technicalOnly />
      </QueryClientProvider>
    );

    expect(html).toContain('Technisches Plausibilitaets- und Fehlerursachenreview');
    expect(html).toContain('Plausibel');
    expect(html).toContain('Unplausibel');
    expect(html).toContain('Unsicher');
    expect(html).not.toContain('<span>Ziel-Frame');
    expect(html).not.toContain('Finale Lage');
  });
});

describe('Stem shadow evaluation', () => {
  it('renders bounded production metrics, drift, feedback, and report exports', () => {
    const html = renderToStaticMarkup(
      <StemShadowEvaluationPanel
        evaluation={{
          schema_version: 'runtime.stem-shadow-evaluation.v1',
          pipeline_version_id: 'plv_shadow_1',
          shadow_mode_enabled: true,
          evaluation_status: 'ACTIVE',
          sessions_available: 500,
          sessions_analyzed: 500,
          excluded_session_count: 0,
          maximum_sessions: 2000,
          limit_reached: false,
          completed_from: '2026-08-01T08:00:00Z',
          completed_to: '2026-08-05T08:00:00Z',
          decision_distribution: [
            { key: 'ROTATE', count: 350, share: 0.7 },
            { key: 'NO_ROTATION', count: 100, share: 0.2 },
            { key: 'UNCERTAIN', count: 50, share: 0.1 }
          ],
          no_rotation_share: 0.2,
          uncertain_share: 0.1,
          deadline_miss_share: 0.02,
          angle_histogram: [{ lower_bound: -30, upper_bound: 0, count: 120 }],
          angle_missing_count: 150,
          confidence_histogram: [{ lower_bound: 0.8, upper_bound: 0.9, count: 240 }],
          confidence_percentiles: { median: 0.84, p90: 0.93, p95: 0.96 },
          reason_code_distribution: [
            { key: 'CURVATURE_MINIMUM_CORRECTION_READY', count: 350, share: 0.7 }
          ],
          decision_latency: {
            measured_count: 500,
            missing_count: 0,
            minimum_ms: 180,
            median_ms: 420,
            p90_ms: 650,
            p95_ms: 720,
            maximum_ms: 910
          },
          drift: {
            status: 'NO_DRIFT_DETECTED',
            baseline_count: 50,
            recent_count: 50,
            baseline_mean_confidence: 0.85,
            recent_mean_confidence: 0.84,
            confidence_delta: -0.01,
            baseline_uncertain_share: 0.1,
            recent_uncertain_share: 0.12,
            uncertain_share_delta: 0.02,
            process_calibration_hashes: [],
            rotation_calibration_hashes: [],
            process_calibration_change_count: 0,
            rotation_calibration_change_count: 0,
            drift_reason_codes: [],
            signals: []
          },
          feedback: {
            reviewed_session_count: 42,
            ratings: [{ key: 'plausible', count: 36, share: 36 / 42 }],
            technical_error_reasons: [
              { key: 'surface_feature_loss', count: 4, share: 4 / 42 }
            ]
          },
          report_json: { schema_version: 'stem.shadow-evaluation-report.v1' },
          report_markdown: '# Stem Shadow Mode evaluation\n',
          meta: { request_id: 'req_shadow', generated_at: '2026-08-05T08:00:01Z' }
        }}
      />
    );

    expect(html).toContain('Shadow-Mode Produktionsbewertung');
    expect(html).toContain('500');
    expect(html).toContain('Decision P95');
    expect(html).toContain('720.0 ms');
    expect(html).toContain('Kamera-/Kalibrierungsdrift');
    expect(html).toContain('Surface Feature Loss');
    expect(html).toContain('JSON-Bericht exportieren');
    expect(html).toContain('Markdown-Bericht exportieren');
  });
});
