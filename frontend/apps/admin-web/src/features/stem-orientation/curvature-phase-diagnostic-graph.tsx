type DiagnosticGraphRecord = Record<string, unknown>;

type DiagnosticPoint = {
  frameId: string;
  phase: number;
  curvature: number;
  weight: number;
  status: 'weighted' | 'downweighted' | 'ignored';
  reasons: string[];
};

type CurvePoint = { phase: number; curvature: number };

export type StemOrientationLiveResult = {
  diameterMm: number | null;
  targetRotationDeg: number | null;
};

// eslint-disable-next-line react-refresh/only-export-components
export function diagnosticGraphFromOverlay(
  overlay: Array<{ layer?: string; metadata?: Record<string, unknown> }> | null | undefined
): DiagnosticGraphRecord | null {
  for (const primitive of overlay ?? []) {
    if (primitive.layer !== 'stem_curvature_phase_diagnostic') continue;
    const graph = primitive.metadata?.diagnostic_graph;
    if (isRecord(graph) && graph.schema_version === 'stem.curvature-phase-diagnostic.v1') {
      return graph;
    }
  }
  return null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function stemOrientationResultFromOverlay(
  overlay: Array<{ layer?: string; metadata?: Record<string, unknown> }> | null | undefined
): StemOrientationLiveResult {
  let diameterMm: number | null = null;
  let targetRotationDeg: number | null = null;
  for (const primitive of overlay ?? []) {
    if (primitive.layer === 'stem_surface_phase') {
      diameterMm = finiteNumber(primitive.metadata?.diameter_mm) ?? diameterMm;
    }
    if (primitive.layer === 'stem_curvature_phase_diagnostic') {
      diameterMm = finiteNumber(primitive.metadata?.diameter_mm) ?? diameterMm;
      targetRotationDeg =
        finiteNumber(primitive.metadata?.recommended_rotation_deg_signed) ??
        finiteNumber(primitive.metadata?.signed_correction_deg) ??
        targetRotationDeg;
    }
  }
  return { diameterMm, targetRotationDeg };
}

export function StemCurvaturePhaseRuntimePanel(props: {
  overlay: Array<{ layer?: string; metadata?: Record<string, unknown> }> | null | undefined;
  title?: string;
  compact?: boolean;
}) {
  const result = stemOrientationResultFromOverlay(props.overlay);
  return (
    <section className="stem-runtime-result" data-testid="stem-runtime-result">
      <div className="stem-runtime-result__metrics" aria-label="Stammausrichtung Ergebnis">
        <div>
          <span>Stammdurchmesser [mm]</span>
          <strong>{formatMetric(result.diameterMm, 0)}</strong>
        </div>
        <div>
          <span>Solldrehung auf Krümmungsminimum [°]</span>
          <strong>{formatSignedMetric(result.targetRotationDeg, 1)}</strong>
        </div>
      </div>
      <StemCurvaturePhaseDiagnosticGraph
        graph={diagnosticGraphFromOverlay(props.overlay)}
        title={props.title}
        compact={props.compact}
      />
    </section>
  );
}

export function StemCurvaturePhaseDiagnosticGraph(props: {
  graph: unknown;
  title?: string;
  compact?: boolean;
}) {
  const graph = isRecord(props.graph) ? props.graph : {};
  const points = diagnosticPoints(graph.points);
  const curve = curvePoints(graph.fit_curve);
  const minimumPhase = finiteNumber(graph.minimum_phase_deg);
  const maximumPhase = finiteNumber(graph.maximum_phase_deg);
  const currentPhase = finiteNumber(graph.current_phase_deg);
  const decisionPhase = finiteNumber(graph.decision_phase_deg);
  const minimumCurvature = finiteNumber(graph.minimum_curvature_mm);
  const maximumCurvature = finiteNumber(graph.maximum_curvature_mm);
  const phases = [
    ...points.map((point) => point.phase),
    ...curve.map((point) => point.phase),
    minimumPhase,
    maximumPhase,
    currentPhase,
    decisionPhase
  ].filter((value): value is number => value !== null);
  const curvatures = [
    ...points.map((point) => point.curvature),
    ...curve.map((point) => point.curvature),
    minimumCurvature,
    maximumCurvature
  ].filter((value): value is number => value !== null);
  const compactClass = props.compact ? ' stem-diagnostic-graph--compact' : '';
  if (!phases.length || !curvatures.length) {
    return (
      <section className={`stem-diagnostic-graph stem-diagnostic-graph--empty${compactClass}`} data-testid="stem-curvature-phase-graph">
        <div className="stem-diagnostic-graph__header">
          <div>
            <strong>{props.title ?? 'Krümmung über Rollphase'}</strong>
            <span>0 gewichtet · 0 ignoriert</span>
          </div>
          <span>–° beobachtet</span>
        </div>
        <div className="stem-diagnostic-graph__empty-plot">
          Noch keine auswertbaren Krümmungs-/Phasenpunkte.
        </div>
      </section>
    );
  }

  const width = 760;
  const height = props.compact ? 190 : 280;
  const margin = { left: 64, right: 22, top: 26, bottom: 48 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const xMinRaw = Math.min(...phases);
  const xMaxRaw = Math.max(...phases);
  const xPad = Math.max((xMaxRaw - xMinRaw) * 0.04, 2);
  const xMin = xMinRaw - xPad;
  const xMax = xMaxRaw + xPad;
  const yMinRaw = Math.min(0, ...curvatures);
  const yMaxRaw = Math.max(...curvatures);
  const yPad = Math.max((yMaxRaw - yMinRaw) * 0.1, 0.25);
  const yMin = Math.max(0, yMinRaw - yPad);
  const yMax = yMaxRaw + yPad;
  const x = (value: number) => margin.left + ((value - xMin) / Math.max(xMax - xMin, 1e-9)) * plotWidth;
  const y = (value: number) => margin.top + (1 - (value - yMin) / Math.max(yMax - yMin, 1e-9)) * plotHeight;
  const curvePath = curve.map((point, index) => `${index ? 'L' : 'M'} ${x(point.phase)} ${y(point.curvature)}`).join(' ');
  const xTicks = ticks(xMin, xMax, 5);
  const yTicks = ticks(yMin, yMax, 5);
  const reasonCodes = stringArray(graph.reason_codes);

  return (
    <section className={`stem-diagnostic-graph${compactClass}`} data-testid="stem-curvature-phase-graph">
      <div className="stem-diagnostic-graph__header">
        <div>
          <strong>{props.title ?? 'Krümmung über Rollphase'}</strong>
          <span>{points.filter((point) => point.status !== 'ignored').length} gewichtet · {points.filter((point) => point.status === 'ignored').length} ignoriert</span>
        </div>
        <span>{formatNumber(finiteNumber(graph.observed_phase_span_deg), 1)}° beobachtet</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Horizontale Krümmung über kumulativer Rollphase">
        {yTicks.map((tick) => (
          <g key={`y-${tick}`}>
            <line className="stem-diagnostic-graph__grid" x1={margin.left} x2={width - margin.right} y1={y(tick)} y2={y(tick)} />
            <text className="stem-diagnostic-graph__tick" x={margin.left - 9} y={y(tick) + 4} textAnchor="end">{tick.toFixed(1)}</text>
          </g>
        ))}
        {xTicks.map((tick) => (
          <g key={`x-${tick}`}>
            <line className="stem-diagnostic-graph__grid" x1={x(tick)} x2={x(tick)} y1={margin.top} y2={height - margin.bottom} />
            <text className="stem-diagnostic-graph__tick" x={x(tick)} y={height - margin.bottom + 20} textAnchor="middle">{tick.toFixed(0)}°</text>
          </g>
        ))}
        <line className="stem-diagnostic-graph__axis" x1={margin.left} x2={margin.left} y1={margin.top} y2={height - margin.bottom} />
        <line className="stem-diagnostic-graph__axis" x1={margin.left} x2={width - margin.right} y1={height - margin.bottom} y2={height - margin.bottom} />
        {curvePath ? <path className="stem-diagnostic-graph__fit" d={curvePath} /> : null}
        {points.map((point, index) => {
          const className = `stem-diagnostic-graph__point stem-diagnostic-graph__point--${point.status}`;
          const radius = point.status === 'ignored' ? 4 : 4 + 2 * point.weight;
          return (
            <g key={`${point.frameId}-${index}`}>
              <circle className={className} cx={x(point.phase)} cy={y(point.curvature)} r={radius}>
                <title>{`Frame ${point.frameId}: ${point.phase.toFixed(1)}°, ${point.curvature.toFixed(2)} mm, Gewicht ${point.weight.toFixed(2)}${point.reasons.length ? `, ${point.reasons.join(', ')}` : ''}`}</title>
              </circle>
              {point.status === 'ignored' ? <path className="stem-diagnostic-graph__ignored-mark" d={`M ${x(point.phase) - 4} ${y(point.curvature) - 4} L ${x(point.phase) + 4} ${y(point.curvature) + 4} M ${x(point.phase) + 4} ${y(point.curvature) - 4} L ${x(point.phase) - 4} ${y(point.curvature) + 4}`} /> : null}
            </g>
          );
        })}
        <PhaseMarker phase={minimumPhase} x={x} top={margin.top} bottom={height - margin.bottom} label="Minimum" className="minimum" />
        <PhaseMarker phase={maximumPhase} x={x} top={margin.top} bottom={height - margin.bottom} label="Maximum" className="maximum" />
        <PhaseMarker phase={currentPhase} x={x} top={margin.top} bottom={height - margin.bottom} label="Aktuell" className="current" />
        <PhaseMarker phase={decisionPhase} x={x} top={margin.top} bottom={height - margin.bottom} label="Decision" className="decision" />
        <text className="stem-diagnostic-graph__axis-label" x={margin.left + plotWidth / 2} y={height - 7} textAnchor="middle">kumulative Rollphase in Grad</text>
        <text className="stem-diagnostic-graph__axis-label" transform={`translate(16 ${margin.top + plotHeight / 2}) rotate(-90)`} textAnchor="middle">horizontale Krümmung in mm</text>
      </svg>
      <div className="stem-diagnostic-graph__legend" aria-label="Legende">
        <span><i className="weighted" /> gewichtet</span>
        <span><i className="downweighted" /> robust abgewichtet</span>
        <span><i className="ignored" /> unberücksichtigt</span>
        <span><i className="fit" /> Fit</span>
      </div>
      {reasonCodes.length ? <p className="stem-diagnostic-graph__reasons">Reason Codes: {reasonCodes.join(', ')}</p> : null}
    </section>
  );
}

function PhaseMarker(props: {
  phase: number | null;
  x: (value: number) => number;
  top: number;
  bottom: number;
  label: string;
  className: string;
}) {
  if (props.phase === null) return null;
  const markerX = props.x(props.phase);
  return (
    <g className={`stem-diagnostic-graph__marker stem-diagnostic-graph__marker--${props.className}`}>
      <line x1={markerX} x2={markerX} y1={props.top} y2={props.bottom} />
      <text x={markerX + 4} y={props.top + 13}>{props.label}</text>
    </g>
  );
}

function diagnosticPoints(value: unknown): DiagnosticPoint[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item)) return [];
    const phase = finiteNumber(item.cumulative_phase_deg);
    const curvature = finiteNumber(item.curvature_rms_mm);
    if (phase === null || curvature === null) return [];
    const rawStatus = String(item.status ?? 'ignored');
    const status: DiagnosticPoint['status'] = rawStatus === 'weighted' || rawStatus === 'downweighted' ? rawStatus : 'ignored';
    return [{
      frameId: String(item.frame_id ?? item.frame_sequence ?? 'n/a'),
      phase,
      curvature,
      weight: Math.max(0, Math.min(finiteNumber(item.fit_weight) ?? 0, 1)),
      status,
      reasons: stringArray(item.reason_codes)
    }];
  });
}

function curvePoints(value: unknown): CurvePoint[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!isRecord(item)) return [];
    const phase = finiteNumber(item.cumulative_phase_deg);
    const curvature = finiteNumber(item.fitted_curvature_mm);
    return phase === null || curvature === null ? [] : [{ phase, curvature }];
  });
}

function ticks(minimum: number, maximum: number, count: number) {
  const span = maximum - minimum;
  return Array.from({ length: count }, (_, index) => minimum + (span * index) / Math.max(count - 1, 1));
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function formatNumber(value: number | null, digits: number) {
  return value === null ? 'n/a' : value.toFixed(digits);
}

function formatMetric(value: number | null, digits: number) {
  return value === null ? '–' : value.toFixed(digits);
}

function formatSignedMetric(value: number | null, digits: number) {
  if (value === null) return '–';
  const prefix = value > 0 ? '+' : '';
  return `${prefix}${value.toFixed(digits)}`;
}
