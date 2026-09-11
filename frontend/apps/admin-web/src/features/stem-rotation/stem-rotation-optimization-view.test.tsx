import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import type {
  RuntimeResultRecordSummary,
  RuntimeStemRotationPriorityControl,
  RuntimeStemRotationViewModel
} from '@cv-platform/schemas';
import {
  StemRotationOptimizationView,
  stemRotationStatePresentation,
  stemRotationViewModelFromResult
} from './stem-rotation-optimization-view';

const control: RuntimeStemRotationPriorityControl = {
  schema_version: 'runtime.stem-rotation-priority-control.v1',
  priority_source: 'FRONTEND',
  priority_mode: 'VORSCHNITT_KRITISCH',
  optimization_enable: true,
  revision: 7
};

const viewModel: RuntimeStemRotationViewModel = {
  schema_version: 'stem.rotation-hmi.v1',
  coordinate_system: 'MACHINE_X_Y_Z_RIGHT_HANDED_V1',
  rotation_axis: 'POSITIVE_MACHINE_X',
  rotation_reference_view: 'BLICK_ENTGEGEN_X',
  positive_rotation: 'RECHTE_HAND_UM_POSITIVE_X',
  timestamp: '2026-08-21T08:00:00Z',
  state: 'RESULT_READY',
  result_ready: true,
  result_valid: true,
  priority_source: 'FRONTEND',
  priority_mode: 'VORSCHNITT_KRITISCH',
  priority_axis: 'VERTICAL_Z',
  priority_revision: 7,
  optimization_enable: true,
  should_rotate: true,
  rotation_delta_deg: 18,
  rotation_direction: 'POSITIVE',
  gain_mm: 34,
  model_quality: 0.91,
  stem_id: 'stem-42',
  session_id: 'cycle-42',
  model_hash: 'sha256:test',
  reason_code: 'OK',
  reason_codes: [],
  limiting_x_current_mm: 100,
  limiting_x_optimal_mm: 150,
  comparison_angle_deg: 18,
  stem_longitudinal_geometry: [
    {
      x_mm: 100,
      in_evaluation_range: true,
      current_center_axis_mm: 6,
      current_outline_negative_mm: -90,
      current_outline_positive_mm: 102,
      optimal_center_axis_mm: 2,
      optimal_outline_negative_mm: -100,
      optimal_outline_positive_mm: 104,
      mean_diameter_mm: 198,
      ovality_ratio: 0.12,
      quality: 0.9,
      interpolated: false,
      limiting_current: true,
      limiting_optimal: false
    },
    {
      x_mm: 150,
      in_evaluation_range: true,
      current_center_axis_mm: 4,
      current_outline_negative_mm: -98,
      current_outline_positive_mm: 106,
      optimal_center_axis_mm: 0,
      optimal_outline_negative_mm: -104,
      optimal_outline_positive_mm: 104,
      mean_diameter_mm: 205,
      ovality_ratio: 0.08,
      quality: 0.88,
      interpolated: false,
      limiting_current: false,
      limiting_optimal: true
    }
  ],
  current_critical_cross_section: crossSection(100),
  optimal_critical_cross_section: crossSection(150),
  delta_geometry_by_x: [
    {
      x_mm: 100,
      in_evaluation_range: true,
      current_geometry_mm: 180,
      optimal_geometry_mm: 214,
      delta_geometry_mm: 34,
      valid: true,
      reason_code: 'OK',
      limiting_current: true,
      limiting_optimal: false
    },
    {
      x_mm: 150,
      in_evaluation_range: true,
      current_geometry_mm: 202,
      optimal_geometry_mm: 198,
      delta_geometry_mm: -4,
      valid: true,
      reason_code: 'OK',
      limiting_current: false,
      limiting_optimal: true
    }
  ],
  diagnostics: {
    score_current_mm: 180,
    score_optimal_mm: 214,
    optimal_candidate_angle_deg: 18,
    plateau_width_deg: 1.5,
    score_tolerance_mm: 0.2,
    valid_slice_count: 2,
    evaluated_slice_count: 2,
    mean_fit_quality: 0.89,
    mean_observation_quality: 0.93,
    mean_center_residual_rms_mm: 0.5,
    mean_width_residual_rms_mm: 0.8,
    minimum_phase_coverage_ratio: 0.82,
    fit_diagnostics: [{ x_mm: 100, center_residual_mm: 0.5 }],
    active_configuration: { coarse_angle_step_deg: 1, min_gain_mm: 2 },
    angle_score_curve: [
      { angle_deg: 0, score_mm: 180, valid: true, current: true, selected: false },
      { angle_deg: 18, score_mm: 214, valid: true, current: false, selected: true }
    ]
  }
};

function crossSection(xMm: number) {
  return {
    x_mm: xMm,
    current_center: { y_mm: 4, z_mm: 6 },
    optimal_center: { y_mm: 1, z_mm: 2 },
    current_ellipse: [
      { y_mm: 100, z_mm: 6 },
      { y_mm: 4, z_mm: 90 },
      { y_mm: -92, z_mm: 6 },
      { y_mm: 4, z_mm: -78 }
    ],
    optimal_ellipse: [
      { y_mm: 95, z_mm: 2 },
      { y_mm: 1, z_mm: 104 },
      { y_mm: -93, z_mm: 2 },
      { y_mm: 1, z_mm: -100 }
    ],
    current_symmetric_chord: {
      start: { y_mm: 0, z_mm: -84 },
      end: { y_mm: 0, z_mm: 84 },
      span_mm: 168,
      valid: true
    },
    optimal_symmetric_chord: {
      start: { y_mm: 0, z_mm: -102 },
      end: { y_mm: 0, z_mm: 102 },
      span_mm: 204,
      valid: true
    },
    current_phi_deg: 5,
    optimal_phi_deg: 23,
    a_mm: 100,
    b_mm: 84,
    quality: 0.9
  };
}

function render(
  model: RuntimeStemRotationViewModel | null = viewModel,
  nextControl = control
) {
  return renderToStaticMarkup(
    <StemRotationOptimizationView
      control={nextControl}
      viewModel={model}
      busy={false}
      onModeChange={vi.fn()}
      onOptimizationEnableChange={vi.fn()}
    />
  );
}

describe('StemRotationOptimizationView', () => {
  it('offers both editable priority modes for the frontend source', () => {
    const html = render();
    expect(html).toContain('Quelle: <strong>Frontend (Test/Inbetriebnahme)</strong>');
    expect(html).toContain('Vorschnitt');
    expect(html).toContain('Nachschnitt');
    expect(html).toContain('oben–unten · z');
    expect(html).toContain('aria-pressed="true"');
  });

  it('shows the vertical z and horizontal y priority labels', () => {
    expect(render()).toContain('Vorschnitt / oben–unten (z)');
    const horizontalControl = {
      ...control,
      priority_mode: 'NACHSCHNITT_KRITISCH' as const
    };
    const horizontalModel = {
      ...viewModel,
      priority_mode: 'NACHSCHNITT_KRITISCH' as const,
      priority_axis: 'HORIZONTAL_Y' as const
    };
    expect(render(horizontalModel, horizontalControl)).toContain(
      'Nachschnitt / links–rechts (y)'
    );
  });

  it('renders positive and negative signed directions according to +x', () => {
    const positive = render({ ...viewModel, positive_rotation: 'CLOCKWISE' });
    expect(positive).toContain('+18.0°');
    expect(positive).toContain('Positive Drehung um +x');
    expect(positive).toContain('Uhrzeigersinn');
    expect(positive).toContain('↻');
    expect(positive).toContain('data-canonical-rotation-sign="POSITIVE"');
    expect(positive).toContain('data-plot-direction="COUNTER_CLOCKWISE"');
    expect(positive).toContain('data-rotation-direction="POSITIVE"');
    const negative = render({
      ...viewModel,
      rotation_delta_deg: -12,
      comparison_angle_deg: -12,
      rotation_direction: 'NEGATIVE',
      positive_rotation: 'CLOCKWISE'
    });
    expect(negative).toContain('-12.0°');
    expect(negative).toContain('Negative Drehung um +x');
    expect(negative).toContain('Gegenuhrzeigersinn');
    expect(negative).toContain('↺');
    expect(negative).toContain('data-canonical-rotation-sign="NEGATIVE"');
    expect(negative).toContain('data-plot-direction="CLOCKWISE"');
    expect(negative).toContain('data-rotation-direction="NEGATIVE"');
  });

  it('states that no correction is needed for a low-gain result', () => {
    const html = render({
      ...viewModel,
      should_rotate: false,
      rotation_delta_deg: 0,
      comparison_angle_deg: 0,
      rotation_direction: 'NONE',
      gain_mm: 0,
      reason_code: 'LOW_GAIN'
    });
    expect(html).toContain('Keine Korrekturdrehung');
    expect(html).toContain('+0.0 mm');
    expect(html).not.toContain('data-canonical-rotation-sign');
  });

  it('never presents an invalid model as a valid rotation recommendation', () => {
    const html = render({
      ...viewModel,
      state: 'INVALID',
      result_ready: false,
      result_valid: false,
      should_rotate: false,
      rotation_delta_deg: null,
      rotation_direction: 'UNAVAILABLE',
      reason_code: 'INVALID_STEM_MODEL'
    });
    expect(html).toContain('Das Scheibenmodell ist ungültig.');
    expect(html).toContain('kein Drehwert als gültige Empfehlung');
    expect(html).not.toContain('+18.0°');
  });

  it('uses backend-provided current and optimal ellipse contours', () => {
    const html = render();
    expect(html).toContain('data-source="view-model-current-ellipse"');
    expect(html).toContain('data-source="view-model-optimal-ellipse"');
    expect(html).toContain('Symmetrisch nutzbare Geometrie: 168.0 mm');
  });

  it('marks both limiting positions and transfers DeltaG values unchanged', () => {
    const html = render();
    expect(html).toContain('limitierend aktuell');
    expect(html).toContain('limitierend optimiert');
    expect(html).toContain('data-delta-mm="34"');
    expect(html).toContain('data-delta-mm="-4"');
    expect(html).toContain('Backend-Werte · keine UI-Neuberechnung');
  });

  it('keeps absolute scores in diagnostics instead of the decision tile', () => {
    const html = render();
    const decision = html.match(
      /<section[^>]*data-testid="stem-rotation-decision"[\s\S]*?<\/section>/
    )?.[0];
    expect(decision).toBeDefined();
    expect(decision).not.toContain('Score aktuell');
    expect(decision).not.toContain('180.00 mm');
    expect(html).toContain('Score aktuell');
    expect(html).toContain('Score optimal');
    expect(html).toContain('Fitqualität (Mittel)');
    expect(html).toContain('Messqualität (Mittel)');
    expect(html).toContain('Winkel → Score');
  });

  it.each([
    ['IDLE', 'Bereit'],
    ['MEASURING', 'Messung läuft'],
    ['MODEL_BUILDING', 'Modellaufbau'],
    ['OPTIMIZING', 'Optimierung läuft'],
    ['RESULT_READY', 'Ergebnis bereit'],
    ['INVALID', 'Messung ungültig'],
    ['ERROR', 'Fehler']
  ] as const)('maps %s to an operator-readable state', (state, label) => {
    const presentation = stemRotationStatePresentation(
      { ...viewModel, state, result_valid: state === 'RESULT_READY' },
      true
    );
    expect(presentation.label).toBe(label);
  });

  it('hides the old recommendation while a new priority revision is pending', () => {
    const html = render(viewModel, { ...control, revision: 8 });
    expect(html).toContain('Prioritätswechsel erkannt');
    expect(html).not.toContain('+18.0°');
  });

  it('reads a validated backend view model from a historical result preview', () => {
    const result = {
      derived_preview: { view_model: viewModel }
    } as unknown as RuntimeResultRecordSummary;
    expect(stemRotationViewModelFromResult(result)).toEqual(viewModel);
    expect(stemRotationViewModelFromResult(null)).toBeNull();
  });
});
