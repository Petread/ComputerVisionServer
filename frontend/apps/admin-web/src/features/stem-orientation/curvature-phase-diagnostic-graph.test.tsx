import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  diagnosticGraphFromOverlay,
  stemOrientationResultFromOverlay,
  StemCurvaturePhaseDiagnosticGraph,
  StemCurvaturePhaseRuntimePanel
} from './curvature-phase-diagnostic-graph';

describe('StemCurvaturePhaseDiagnosticGraph', () => {
  it('renders accepted and ignored live points before a fit exists', () => {
    const graph = {
      schema_version: 'stem.curvature-phase-diagnostic.v1',
      observed_phase_span_deg: 12,
      points: [
        {
          frame_id: '7',
          cumulative_phase_deg: 12,
          curvature_rms_mm: 6.4,
          fit_weight: 0.72,
          status: 'weighted',
          reason_codes: []
        },
        {
          frame_id: '8',
          cumulative_phase_deg: 14,
          curvature_rms_mm: 7.1,
          fit_weight: 0,
          status: 'ignored',
          reason_codes: ['SOLVER_PHASE_CONFIDENCE_LOW']
        }
      ],
      fit_curve: [],
      current_phase_deg: 14,
      reason_codes: ['INSUFFICIENT_OBSERVATIONS']
    };
    const extracted = diagnosticGraphFromOverlay([
      {
        layer: 'stem_curvature_phase_diagnostic',
        metadata: { diagnostic_graph: graph }
      }
    ]);
    const html = renderToStaticMarkup(
      <StemCurvaturePhaseDiagnosticGraph graph={extracted} compact />
    );

    expect(extracted).toBe(graph);
    expect(html).toContain('1 gewichtet · 1 ignoriert');
    expect(html).toContain('INSUFFICIENT_OBSERVATIONS');
    expect(html).toContain('SOLVER_PHASE_CONFIDENCE_LOW');
    expect(html).not.toContain('Noch keine auswertbaren');
  });

  it('keeps a compact result area visible before graph observations exist', () => {
    const overlay = [
      {
        layer: 'stem_surface_phase',
        metadata: {}
      },
      {
        layer: 'stem_curvature_phase_diagnostic',
        metadata: { diameter_mm: 318.4, recommended_rotation_deg_signed: 27.5 }
      }
    ];

    expect(stemOrientationResultFromOverlay(overlay)).toEqual({
      diameterMm: 318.4,
      targetRotationDeg: 27.5
    });

    const html = renderToStaticMarkup(
      <StemCurvaturePhaseRuntimePanel overlay={overlay} compact />
    );

    expect(html).toContain('Stammdurchmesser [mm]');
    expect(html).toContain('Solldrehung auf Krümmungsminimum [°]');
    expect(html).toContain('318');
    expect(html).toContain('+27.5');
    expect(html).toContain('stem-diagnostic-graph--compact');
    expect(html).toContain('Noch keine auswertbaren');
  });

  it('shows stable placeholders when no result overlay is available', () => {
    const html = renderToStaticMarkup(
      <StemCurvaturePhaseRuntimePanel overlay={[]} compact />
    );

    expect(html).toContain('stem-runtime-result');
    expect(html).toContain('Stammdurchmesser [mm]');
    expect(html).toContain('Solldrehung auf Krümmungsminimum [°]');
    expect(html.match(/>–</g)).toHaveLength(2);
  });
});
