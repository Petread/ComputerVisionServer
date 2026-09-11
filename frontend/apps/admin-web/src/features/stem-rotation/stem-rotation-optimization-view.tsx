import { useState } from 'react';
import type {
  RuntimeResultRecordSummary,
  RuntimeStemRotationAngleScoreView,
  RuntimeStemRotationCrossSectionView,
  RuntimeStemRotationDeltaGeometryView,
  RuntimeStemRotationLongitudinalSliceView,
  RuntimeStemRotationPriorityControl,
  RuntimeStemRotationViewModel,
  RuntimeStemRotationViewPoint,
  StemRotationPriorityMode
} from '@cv-platform/schemas';

type Tone = 'neutral' | 'success' | 'warn' | 'danger' | 'info';

export function StemRotationOptimizationView(props: {
  control: RuntimeStemRotationPriorityControl;
  viewModel: RuntimeStemRotationViewModel | null;
  historical?: boolean;
  historicalCompletedAt?: string | null;
  busy: boolean;
  onModeChange: (mode: StemRotationPriorityMode) => void;
  onOptimizationEnableChange: (enabled: boolean) => void;
}) {
  const [criticalPosition, setCriticalPosition] = useState<'current' | 'optimal'>('current');
  const frontendActive = props.control.priority_source === 'FRONTEND';
  const synchronized =
    props.viewModel === null ||
    (props.viewModel.priority_source === props.control.priority_source &&
      props.viewModel.priority_mode === props.control.priority_mode &&
      props.viewModel.priority_revision === props.control.revision &&
      props.viewModel.optimization_enable === props.control.optimization_enable);
  const recommendationVisible = Boolean(
    synchronized && props.viewModel?.result_ready && props.viewModel.result_valid
  );
  const statePresentation = stemRotationStatePresentation(props.viewModel, synchronized);
  const selectedCrossSection =
    criticalPosition === 'optimal'
      ? props.viewModel?.optimal_critical_cross_section
      : props.viewModel?.current_critical_cross_section;

  return (
    <section className="stem-rotation-hmi" aria-labelledby="stem-rotation-hmi-title">
      <header className="stem-rotation-hmi__header">
        <div>
          <p className="stem-rotation-hmi__eyebrow">Geometrische Eindrehoptimierung</p>
          <h2 id="stem-rotation-hmi-title">Stammausrichtung</h2>
        </div>
        <span className={`runtime-chip runtime-chip--${statePresentation.tone}`}>
          {statePresentation.label}
        </span>
      </header>

      {props.historical ? (
        <div className="stem-rotation-hmi__history" role="status">
          <strong>Historisches Ergebnis</strong>
          <span>
            Letzter abgeschlossener Messzyklus
            {props.historicalCompletedAt
              ? ` · ${formatTimestamp(props.historicalCompletedAt)}`
              : ''}
          </span>
        </div>
      ) : null}

      <div className="stem-rotation-hmi__decision-grid">
        <section className="stem-rotation-card stem-rotation-priority" aria-label="Prioritätswahl">
          <div className="stem-rotation-card__heading">
            <div>
              <span>Priorisierte Richtung</span>
              <strong>{priorityLabel(props.control.priority_mode)}</strong>
            </div>
            <span className="runtime-chip runtime-chip--neutral">
              Rev. {props.control.revision}
            </span>
          </div>
          <p className="stem-rotation-priority__source">
            Quelle:{' '}
            <strong>
              {frontendActive ? 'Frontend (Test/Inbetriebnahme)' : 'SPS (nur Anzeige)'}
            </strong>
          </p>
          <label className="stem-rotation-priority__enable">
            <input
              type="checkbox"
              checked={props.control.optimization_enable}
              disabled={props.busy || !frontendActive}
              onChange={(event) =>
                props.onOptimizationEnableChange(event.currentTarget.checked)
              }
            />
            Eindrehoptimierung aktiv
          </label>
          <div
            className="stem-rotation-priority__modes"
            role="group"
            aria-label="Prioritätsmodus"
          >
            {(
              [
                ['VORSCHNITT_KRITISCH', 'Vorschnitt', 'oben–unten · z'],
                ['NACHSCHNITT_KRITISCH', 'Nachschnitt', 'links–rechts · y']
              ] as const
            ).map(([mode, label, axis]) => (
              <button
                key={mode}
                type="button"
                className={props.control.priority_mode === mode ? 'is-active' : undefined}
                aria-pressed={props.control.priority_mode === mode}
                disabled={
                  props.busy || !frontendActive || !props.control.optimization_enable
                }
                onClick={() => props.onModeChange(mode)}
              >
                <strong>{label}</strong>
                <span>{axis}</span>
              </button>
            ))}
          </div>
          {!frontendActive ? (
            <p className="stem-rotation-priority__readonly">
              Die aktive SPS-Quelle sperrt Frontend-Eingaben.
            </p>
          ) : null}
        </section>

        <section
          className={`stem-rotation-card stem-rotation-decision stem-rotation-decision--${statePresentation.tone}`}
          data-testid="stem-rotation-decision"
          data-rotation-direction={props.viewModel?.rotation_direction ?? 'UNAVAILABLE'}
          aria-live="polite"
        >
          <span className="stem-rotation-decision__state">{statePresentation.detail}</span>
          {recommendationVisible && props.viewModel ? (
            props.viewModel.should_rotate && props.viewModel.rotation_delta_deg !== null ? (
              <>
                <div className="stem-rotation-decision__rotation">
                  <span aria-hidden="true">
                    {rotationArrow(props.viewModel)}
                  </span>
                  <strong>{formatSigned(props.viewModel.rotation_delta_deg, 1)}°</strong>
                </div>
                <span className="stem-rotation-decision__direction">
                  {rotationDirectionLabel(props.viewModel)}
                </span>
                <div className="stem-rotation-decision__gain">
                  <strong>{formatSigned(props.viewModel.gain_mm, 1)} mm</strong>
                  <span>Geometriegewinn</span>
                </div>
              </>
            ) : (
              <>
                <strong className="stem-rotation-decision__none">
                  Keine Korrekturdrehung
                </strong>
                <div className="stem-rotation-decision__gain">
                  <strong>{formatSigned(props.viewModel.gain_mm, 1)} mm</strong>
                  <span>Geometriegewinn</span>
                </div>
              </>
            )
          ) : (
            <div className="stem-rotation-decision__unavailable">
              <strong>{statePresentation.operatorMessage}</strong>
              <span>Es wird kein Drehwert als gültige Empfehlung angezeigt.</span>
            </div>
          )}
        </section>
      </div>

      {recommendationVisible && props.viewModel ? (
        <>
          <div className="stem-rotation-hmi__geometry-grid">
            <section className="stem-rotation-card">
              <div className="stem-rotation-card__heading">
                <div>
                  <span>Stamm-Längsansicht</span>
                  <strong>Geometrieverlauf entlang x</strong>
                </div>
              </div>
              <StemLongitudinalPlot
                points={props.viewModel.stem_longitudinal_geometry}
                priorityAxis={props.viewModel.priority_axis}
              />
            </section>

            <section className="stem-rotation-card">
              <div className="stem-rotation-card__heading stem-rotation-card__heading--wrap">
                <div>
                  <span>Kritischer Querschnitt</span>
                  <strong>
                    {criticalPosition === 'current' ? 'Aktuell limitierend' : 'Optimal limitierend'}
                  </strong>
                </div>
                <div className="stem-rotation-position-toggle" role="group" aria-label="Limitierende Position">
                  <button
                    type="button"
                    aria-pressed={criticalPosition === 'current'}
                    className={criticalPosition === 'current' ? 'is-active' : undefined}
                    onClick={() => setCriticalPosition('current')}
                  >
                    aktuell
                  </button>
                  <button
                    type="button"
                    aria-pressed={criticalPosition === 'optimal'}
                    className={criticalPosition === 'optimal' ? 'is-active' : undefined}
                    disabled={!props.viewModel.optimal_critical_cross_section}
                    onClick={() => setCriticalPosition('optimal')}
                  >
                    optimiert
                  </button>
                </div>
              </div>
              <CriticalCrossSectionPlot
                crossSection={selectedCrossSection ?? null}
                priorityAxis={props.viewModel.priority_axis}
                comparisonAngleDeg={props.viewModel.comparison_angle_deg}
              />
            </section>
          </div>

          <section className="stem-rotation-card">
            <div className="stem-rotation-card__heading">
              <div>
                <span>Differenz entlang der Stammlänge</span>
                <strong>Lokaler Geometriegewinn ΔGᵢ</strong>
              </div>
              <span className="stem-rotation-card__hint">
                Backend-Werte · keine UI-Neuberechnung
              </span>
            </div>
            <DeltaGeometryPlot points={props.viewModel.delta_geometry_by_x} />
          </section>
        </>
      ) : (
        <section className="stem-rotation-card stem-rotation-hmi__empty">
          <strong>{statePresentation.label}</strong>
          <p>{statePresentation.operatorMessage}</p>
        </section>
      )}

      <StemRotationDiagnostics viewModel={props.viewModel} synchronized={synchronized} />
    </section>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function stemRotationViewModelFromResult(
  result: RuntimeResultRecordSummary | null | undefined
): RuntimeStemRotationViewModel | null {
  const candidate = result?.derived_preview.view_model;
  if (
    !isRecord(candidate) ||
    candidate.schema_version !== 'stem.rotation-hmi.v1' ||
    !Array.isArray(candidate.stem_longitudinal_geometry) ||
    !Array.isArray(candidate.delta_geometry_by_x) ||
    !isRecord(candidate.diagnostics)
  ) {
    return null;
  }
  return candidate as unknown as RuntimeStemRotationViewModel;
}

function StemLongitudinalPlot(props: {
  points: RuntimeStemRotationLongitudinalSliceView[];
  priorityAxis: RuntimeStemRotationViewModel['priority_axis'];
}) {
  const points = props.points.filter((point) => finite(point.x_mm));
  if (!points.length) return <PlotEmpty message="Keine Scheibengeometrie verfügbar." />;
  const width = 760;
  const height = 250;
  const margin = { left: 58, right: 24, top: 28, bottom: 42 };
  const xValues = points.map((point) => point.x_mm);
  const axisValues = points.flatMap((point) => [
    point.current_outline_negative_mm,
    point.current_outline_positive_mm,
    point.optimal_outline_negative_mm,
    point.optimal_outline_positive_mm
  ]).filter(finiteNumber);
  const xScale = linearScale(Math.min(...xValues), Math.max(...xValues), margin.left, width - margin.right);
  const extent = Math.max(...axisValues.map((value) => Math.abs(value)), 1);
  const axisScale = linearScale(-extent, extent, height - margin.bottom, margin.top);
  const currentTop = pathFrom(points, (point) => xScale(point.x_mm), (point) => axisScale(point.current_outline_positive_mm));
  const currentBottom = pathFrom([...points].reverse(), (point) => xScale(point.x_mm), (point) => axisScale(point.current_outline_negative_mm));
  const currentBody = `${currentTop} ${currentBottom.replace(/^M/, 'L')} Z`;
  const currentCenter = pathFrom(points, (point) => xScale(point.x_mm), (point) => axisScale(point.current_center_axis_mm));
  const optimalCenter = pathFrom(
    points.filter((point) => finite(point.optimal_center_axis_mm)),
    (point) => xScale(point.x_mm),
    (point) => axisScale(point.optimal_center_axis_mm as number)
  );
  const axisLabel = props.priorityAxis === 'VERTICAL_Z' ? 'z [mm]' : 'y [mm]';
  return (
    <div className="stem-rotation-plot">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Schematische Stamm-Längsansicht entlang x mit ${axisLabel}`}>
        <line className="stem-rotation-plot__axis" x1={margin.left} x2={width - margin.right} y1={axisScale(0)} y2={axisScale(0)} />
        <path className="stem-rotation-longitudinal__body" d={currentBody} />
        <path className="stem-rotation-longitudinal__center" d={currentCenter} />
        {optimalCenter ? <path className="stem-rotation-longitudinal__center-optimal" d={optimalCenter} /> : null}
        {points.map((point) => (
          <circle
            key={`slice-${point.x_mm}`}
            className={`stem-rotation-longitudinal__slice${point.in_evaluation_range ? '' : ' stem-rotation-longitudinal__slice--outside'}`}
            cx={xScale(point.x_mm)}
            cy={axisScale(point.current_center_axis_mm)}
            r={2.5 + 4 * point.ovality_ratio}
          >
            <title>{`x=${formatNumber(point.x_mm, 0)} mm · mittlerer Durchmesser ${formatNumber(point.mean_diameter_mm, 1)} mm · Ovalität ${formatNumber(point.ovality_ratio * 100, 1)} % · Qualität ${formatNumber(point.quality * 100, 0)} %`}</title>
          </circle>
        ))}
        {points.filter((point) => point.interpolated).map((point) => (
          <line key={`interpolated-${point.x_mm}`} className="stem-rotation-longitudinal__interpolated" x1={xScale(point.x_mm)} x2={xScale(point.x_mm)} y1={margin.top} y2={height - margin.bottom}>
            <title>{`Interpolierte Scheibe bei x=${formatNumber(point.x_mm, 0)} mm`}</title>
          </line>
        ))}
        {points.filter((point) => point.limiting_current).map((point) => (
          <LimitMarker key={`current-${point.x_mm}`} x={xScale(point.x_mm)} top={margin.top} bottom={height - margin.bottom} label="limitierend aktuell" kind="current" />
        ))}
        {points.filter((point) => point.limiting_optimal).map((point) => (
          <LimitMarker key={`optimal-${point.x_mm}`} x={xScale(point.x_mm)} top={margin.top} bottom={height - margin.bottom} label="limitierend optimiert" kind="optimal" />
        ))}
        <text className="stem-rotation-plot__axis-label" x={(margin.left + width - margin.right) / 2} y={height - 8} textAnchor="middle">x · Stamm-Längsachse [mm]</text>
        <text className="stem-rotation-plot__axis-label" transform={`translate(16 ${(margin.top + height - margin.bottom) / 2}) rotate(-90)`} textAnchor="middle">{axisLabel}</text>
      </svg>
      <div className="stem-rotation-legend" aria-label="Legende Stamm-Längsansicht">
        <span><i className="outline" /> Stammkontur aktuell</span>
        <span><i className="center" /> Mittellinie aktuell</span>
        <span><i className="optimal" /> Mittellinie optimiert</span>
        <span><i className="slice" /> Scheibe · Größe zeigt Ovalität</span>
        <span><i className="limit-current" /> limitierend aktuell</span>
        <span><i className="limit-optimal" /> limitierend optimiert</span>
      </div>
    </div>
  );
}

function CriticalCrossSectionPlot(props: {
  crossSection: RuntimeStemRotationCrossSectionView | null;
  priorityAxis: RuntimeStemRotationViewModel['priority_axis'];
  comparisonAngleDeg?: number | null;
}) {
  const crossSection = props.crossSection;
  if (!crossSection?.current_ellipse.length) {
    return <PlotEmpty message="Kein kritischer Querschnitt verfügbar." />;
  }
  const width = 420;
  const height = 320;
  const margin = 34;
  const allPoints = [
    ...crossSection.current_ellipse,
    ...crossSection.optimal_ellipse,
    crossSection.current_symmetric_chord.start,
    crossSection.current_symmetric_chord.end,
    ...(crossSection.optimal_symmetric_chord
      ? [crossSection.optimal_symmetric_chord.start, crossSection.optimal_symmetric_chord.end]
      : [])
  ];
  const extent = Math.max(
    ...allPoints.flatMap((point) => [Math.abs(point.y_mm), Math.abs(point.z_mm)]),
    1
  ) * 1.12;
  const scale = (Math.min(width, height) - 2 * margin) / (2 * extent);
  const x = (value: number) => width / 2 + value * scale;
  const y = (value: number) => height / 2 - value * scale;
  const currentPath = closedPointPath(crossSection.current_ellipse, x, y);
  const optimalPath = closedPointPath(crossSection.optimal_ellipse, x, y);
  const priorityVertical = props.priorityAxis === 'VERTICAL_Z';
  const comparisonAngleDeg = props.comparisonAngleDeg ?? 0;
  const hasRotation =
    props.comparisonAngleDeg !== null &&
    props.comparisonAngleDeg !== undefined &&
    Math.abs(comparisonAngleDeg) > 1e-9;
  const arrowRadius = 70;
  const arrowPath = comparisonAngleDeg < 0
    ? `M ${width / 2} ${height / 2 - arrowRadius} A ${arrowRadius} ${arrowRadius} 0 0 1 ${width / 2 + arrowRadius} ${height / 2}`
    : `M ${width / 2 + arrowRadius} ${height / 2} A ${arrowRadius} ${arrowRadius} 0 0 0 ${width / 2} ${height / 2 - arrowRadius}`;
  return (
    <div className="stem-rotation-plot stem-rotation-cross-section">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`Kritischer Querschnitt bei x=${formatNumber(crossSection.x_mm, 0)} Millimeter`}>
        <defs>
          <marker id="stem-rotation-arrowhead" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
            <path d="M 0 0 L 7 3.5 L 0 7 Z" />
          </marker>
        </defs>
        <line className={priorityVertical ? 'stem-rotation-cross-section__priority-axis' : 'stem-rotation-plot__axis'} x1={width / 2} x2={width / 2} y1={margin / 2} y2={height - margin / 2} />
        <line className={!priorityVertical ? 'stem-rotation-cross-section__priority-axis' : 'stem-rotation-plot__axis'} x1={margin / 2} x2={width - margin / 2} y1={height / 2} y2={height / 2} />
        <text className="stem-rotation-plot__axis-label" x={width - 12} y={height / 2 - 8} textAnchor="end">y</text>
        <text className="stem-rotation-plot__axis-label" x={width / 2 + 9} y={18}>z</text>
        <path className="stem-rotation-cross-section__current" d={currentPath} data-source="view-model-current-ellipse" />
        {optimalPath ? <path className="stem-rotation-cross-section__optimal" d={optimalPath} data-source="view-model-optimal-ellipse" /> : null}
        <ChordLine chord={crossSection.current_symmetric_chord} x={x} y={y} kind="current" />
        {crossSection.optimal_symmetric_chord ? <ChordLine chord={crossSection.optimal_symmetric_chord} x={x} y={y} kind="optimal" /> : null}
        <circle className="stem-rotation-cross-section__origin" cx={x(0)} cy={y(0)} r="4"><title>Maschinenachse</title></circle>
        <circle className="stem-rotation-cross-section__center-current" cx={x(crossSection.current_center.y_mm)} cy={y(crossSection.current_center.z_mm)} r="4"><title>Mittelpunkt aktuell</title></circle>
        {crossSection.optimal_center ? <rect className="stem-rotation-cross-section__center-optimal" x={x(crossSection.optimal_center.y_mm) - 4} y={y(crossSection.optimal_center.z_mm) - 4} width="8" height="8"><title>Mittelpunkt optimiert</title></rect> : null}
        {hasRotation ? (
          <path className="stem-rotation-cross-section__rotation-arrow" data-canonical-rotation-sign={comparisonAngleDeg < 0 ? 'NEGATIVE' : 'POSITIVE'} data-plot-direction={comparisonAngleDeg < 0 ? 'CLOCKWISE' : 'COUNTER_CLOCKWISE'} d={arrowPath} markerEnd="url(#stem-rotation-arrowhead)" />
        ) : null}
        <text className="stem-rotation-cross-section__position" x={margin} y={height - 12}>{`x = ${formatNumber(crossSection.x_mm, 0)} mm`}</text>
      </svg>
      <div className="stem-rotation-legend" aria-label="Legende Querschnitt">
        <span><i className="ellipse-current" /> aktuell</span>
        <span><i className="ellipse-optimal" /> optimiert</span>
        <span><i className="chord" /> symmetrisch nutzbare Mittelsehne</span>
        <span><i className="axis" /> priorisierte Achse</span>
      </div>
    </div>
  );
}

function DeltaGeometryPlot(props: { points: RuntimeStemRotationDeltaGeometryView[] }) {
  const points = props.points.filter(
    (point) => point.valid && finite(point.x_mm) && finite(point.delta_geometry_mm)
  );
  if (!points.length) return <PlotEmpty message="Keine lokalen Differenzwerte verfügbar." />;
  const width = 920;
  const height = 260;
  const margin = { left: 58, right: 24, top: 26, bottom: 44 };
  const xMin = Math.min(...points.map((point) => point.x_mm));
  const xMax = Math.max(...points.map((point) => point.x_mm));
  const maxAbs = Math.max(...points.map((point) => Math.abs(point.delta_geometry_mm as number)), 1);
  const x = linearScale(xMin, xMax, margin.left, width - margin.right);
  const y = linearScale(-maxAbs, maxAbs, height - margin.bottom, margin.top);
  const zeroY = y(0);
  return (
    <div className="stem-rotation-plot">
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Lokaler Geometriegewinn Delta G entlang der Stamm-Längsachse">
        <line className="stem-rotation-plot__zero" x1={margin.left} x2={width - margin.right} y1={zeroY} y2={zeroY} />
        {points.map((point) => {
          const delta = point.delta_geometry_mm as number;
          return (
            <g
              key={point.x_mm}
              className={point.in_evaluation_range ? undefined : 'stem-rotation-delta__outside'}
              data-delta-mm={delta}
            >
              <line className={delta >= 0 ? 'stem-rotation-delta__positive' : 'stem-rotation-delta__negative'} x1={x(point.x_mm)} x2={x(point.x_mm)} y1={zeroY} y2={y(delta)} />
              <circle className={delta >= 0 ? 'stem-rotation-delta__point-positive' : 'stem-rotation-delta__point-negative'} cx={x(point.x_mm)} cy={y(delta)} r="3">
                <title>{`x=${formatNumber(point.x_mm, 0)} mm: ΔGᵢ=${formatSigned(delta, 2)} mm`}</title>
              </circle>
            </g>
          );
        })}
        {points.filter((point) => point.limiting_current).map((point) => <LimitMarker key={`delta-current-${point.x_mm}`} x={x(point.x_mm)} top={margin.top} bottom={height - margin.bottom} label="limitierend aktuell" kind="current" />)}
        {points.filter((point) => point.limiting_optimal).map((point) => <LimitMarker key={`delta-optimal-${point.x_mm}`} x={x(point.x_mm)} top={margin.top} bottom={height - margin.bottom} label="limitierend optimiert" kind="optimal" />)}
        <text className="stem-rotation-plot__axis-label" x={(margin.left + width - margin.right) / 2} y={height - 8} textAnchor="middle">x [mm]</text>
        <text className="stem-rotation-plot__axis-label" transform={`translate(16 ${(margin.top + height - margin.bottom) / 2}) rotate(-90)`} textAnchor="middle">ΔGᵢ [mm]</text>
      </svg>
      <div className="stem-rotation-legend" aria-label="Legende Differenzplot">
        <span><i className="delta-positive" /> + lokaler Geometriegewinn</span>
        <span><i className="delta-negative" /> − lokale Verringerung</span>
        <span><i className="zero" /> Null-Linie</span>
      </div>
    </div>
  );
}

function StemRotationDiagnostics(props: {
  viewModel: RuntimeStemRotationViewModel | null;
  synchronized: boolean;
}) {
  const viewModel = props.viewModel;
  return (
    <details className="stem-rotation-diagnostics">
      <summary>
        <span>
          <strong>Diagnose</strong>
          <small>Scores, Modellqualität, Winkelkurve und aktive Konfiguration</small>
        </span>
        <span className="runtime-chip runtime-chip--neutral">optional</span>
      </summary>
      <div className="stem-rotation-diagnostics__body">
        {!viewModel ? (
          <p className="subtle-text">Noch keine Diagnosedaten verfügbar.</p>
        ) : (
          <>
            {!props.synchronized ? (
              <div className="inline-alert inline-alert--warning">
                Die Diagnose gehört noch zur vorherigen Prioritätsrevision und wird aktualisiert.
              </div>
            ) : null}
            <dl className="stem-rotation-diagnostics__metrics">
              <DiagnosticMetric label="Score aktuell" value={formatMillimeters(viewModel.diagnostics.score_current_mm)} />
              <DiagnosticMetric label="Score optimal" value={formatMillimeters(viewModel.diagnostics.score_optimal_mm)} />
              <DiagnosticMetric label="Modellqualität" value={`${formatNumber(viewModel.model_quality * 100, 0)} %`} />
              <DiagnosticMetric label="Fitqualität (Mittel)" value={formatPercent(viewModel.diagnostics.mean_fit_quality)} />
              <DiagnosticMetric label="Messqualität (Mittel)" value={formatPercent(viewModel.diagnostics.mean_observation_quality)} />
              <DiagnosticMetric label="Min. Winkelabdeckung" value={formatPercent(viewModel.diagnostics.minimum_phase_coverage_ratio)} />
              <DiagnosticMetric label="Residual Mitte" value={formatMillimeters(viewModel.diagnostics.mean_center_residual_rms_mm)} />
              <DiagnosticMetric label="Residual Breite" value={formatMillimeters(viewModel.diagnostics.mean_width_residual_rms_mm)} />
              <DiagnosticMetric label="Gültige Scheiben" value={`${viewModel.diagnostics.valid_slice_count} / ${viewModel.diagnostics.evaluated_slice_count}`} />
              <DiagnosticMetric label="Limitierend aktuell" value={formatX(viewModel.limiting_x_current_mm)} />
              <DiagnosticMetric label="Limitierend optimal" value={formatX(viewModel.limiting_x_optimal_mm)} />
              <DiagnosticMetric label="Fit-Datensätze" value={String(viewModel.diagnostics.fit_diagnostics.length)} />
              <DiagnosticMetric label="Reason Code" value={viewModel.reason_code} />
            </dl>
            <AngleScorePlot points={viewModel.diagnostics.angle_score_curve} />
            <details className="stem-rotation-diagnostics__config">
              <summary>Aktive Konfiguration</summary>
              <dl>
                {Object.entries(viewModel.diagnostics.active_configuration)
                  .sort(([left], [right]) => left.localeCompare(right))
                  .map(([key, value]) => (
                    <div key={key}>
                      <dt>{key}</dt>
                      <dd>{formatConfigValue(value)}</dd>
                    </div>
                  ))}
              </dl>
            </details>
            <p className="stem-rotation-diagnostics__convention">
              Koordinaten: x längs, y horizontal, z vertikal · Drehung um +x ·{' '}
              {viewModel.positive_rotation ?? 'positive Richtung gemäß konfiguriertem Betrachtungsstandpunkt'}
            </p>
          </>
        )}
      </div>
    </details>
  );
}

function AngleScorePlot(props: { points: RuntimeStemRotationAngleScoreView[] }) {
  const points = props.points.filter((point) => point.valid && finite(point.score_mm));
  if (!points.length) return <PlotEmpty message="Keine Winkel-Score-Kurve verfügbar." compact />;
  const width = 760;
  const height = 220;
  const margin = { left: 58, right: 24, top: 24, bottom: 42 };
  const xMin = Math.min(...points.map((point) => point.angle_deg));
  const xMax = Math.max(...points.map((point) => point.angle_deg));
  const scores = points.map((point) => point.score_mm as number);
  const yMinRaw = Math.min(...scores);
  const yMaxRaw = Math.max(...scores);
  const yPad = Math.max((yMaxRaw - yMinRaw) * 0.1, 0.5);
  const x = linearScale(xMin, xMax, margin.left, width - margin.right);
  const y = linearScale(yMinRaw - yPad, yMaxRaw + yPad, height - margin.bottom, margin.top);
  const curve = pathFrom(points, (point) => x(point.angle_deg), (point) => y(point.score_mm as number));
  return (
    <section className="stem-rotation-angle-score">
      <strong>Winkel → Score</strong>
      <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Diagnosekurve aus Kandidatenwinkel und Stamm-Score">
        <line className="stem-rotation-plot__axis" x1={margin.left} x2={width - margin.right} y1={height - margin.bottom} y2={height - margin.bottom} />
        <line className="stem-rotation-plot__axis" x1={margin.left} x2={margin.left} y1={margin.top} y2={height - margin.bottom} />
        <path className="stem-rotation-angle-score__curve" d={curve} />
        {points.filter((point) => point.current || point.selected).map((point) => (
          <g key={`${point.angle_deg}-${point.current ? 'current' : 'selected'}`}>
            <line className={point.current ? 'stem-rotation-angle-score__current' : 'stem-rotation-angle-score__selected'} x1={x(point.angle_deg)} x2={x(point.angle_deg)} y1={margin.top} y2={height - margin.bottom} />
            <text className="stem-rotation-angle-score__marker-label" x={x(point.angle_deg)} y={margin.top + (point.current ? 12 : 28)} textAnchor="middle">
              {point.current && point.selected
                ? '0° aktuell / Optimum'
                : point.current
                  ? '0° aktuell'
                  : 'Optimum'}
            </text>
          </g>
        ))}
        <text className="stem-rotation-plot__axis-label" x={(margin.left + width - margin.right) / 2} y={height - 7} textAnchor="middle">relative Drehung [°]</text>
        <text className="stem-rotation-plot__axis-label" transform={`translate(16 ${(margin.top + height - margin.bottom) / 2}) rotate(-90)`} textAnchor="middle">Score [mm]</text>
      </svg>
    </section>
  );
}

function ChordLine(props: {
  chord: RuntimeStemRotationCrossSectionView['current_symmetric_chord'];
  x: (value: number) => number;
  y: (value: number) => number;
  kind: 'current' | 'optimal';
}) {
  if (!props.chord.valid) return null;
  return (
    <line
      className={`stem-rotation-cross-section__chord stem-rotation-cross-section__chord--${props.kind}`}
      x1={props.x(props.chord.start.y_mm)}
      x2={props.x(props.chord.end.y_mm)}
      y1={props.y(props.chord.start.z_mm)}
      y2={props.y(props.chord.end.z_mm)}
    >
      <title>{`Symmetrisch nutzbare Geometrie: ${formatNumber(props.chord.span_mm, 1)} mm`}</title>
    </line>
  );
}

function LimitMarker(props: {
  x: number;
  top: number;
  bottom: number;
  label: string;
  kind: 'current' | 'optimal';
}) {
  return (
    <g>
      <line className={`stem-rotation-limit-marker stem-rotation-limit-marker--${props.kind}`} x1={props.x} x2={props.x} y1={props.top} y2={props.bottom} />
      <text className="stem-rotation-limit-marker__label" x={props.x + 4} y={props.top + (props.kind === 'current' ? 12 : 27)}>{props.label}</text>
    </g>
  );
}

function DiagnosticMetric(props: { label: string; value: string }) {
  return <div><dt>{props.label}</dt><dd>{props.value}</dd></div>;
}

function PlotEmpty(props: { message: string; compact?: boolean }) {
  return <div className={`stem-rotation-plot__empty${props.compact ? ' stem-rotation-plot__empty--compact' : ''}`}>{props.message}</div>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function stemRotationStatePresentation(
  viewModel: RuntimeStemRotationViewModel | null,
  synchronized = true
): { label: string; detail: string; operatorMessage: string; tone: Tone } {
  if (!synchronized) {
    return {
      label: 'Wird aktualisiert',
      detail: 'Prioritätswechsel erkannt',
      operatorMessage: 'Das Ergebnis wird für die neue priorisierte Richtung neu berechnet.',
      tone: 'info'
    };
  }
  switch (viewModel?.state ?? 'IDLE') {
    case 'MEASURING':
      return { label: 'Messung läuft', detail: 'MEASURING', operatorMessage: 'Stammgeometrie wird erfasst.', tone: 'info' };
    case 'MODEL_BUILDING':
      return { label: 'Modellaufbau', detail: 'MODEL_BUILDING', operatorMessage: 'Scheibenmodell wird aufgebaut.', tone: 'info' };
    case 'OPTIMIZING':
      return { label: 'Optimierung läuft', detail: 'OPTIMIZING', operatorMessage: 'Drehwinkel werden bewertet.', tone: 'info' };
    case 'RESULT_READY':
    case 'DONE':
    case 'APPLIED':
      return viewModel?.result_valid
        ? { label: 'Ergebnis bereit', detail: 'RESULT_READY', operatorMessage: 'Gültige Empfehlung verfügbar.', tone: 'success' }
        : { label: 'Ergebnis ungültig', detail: 'INVALID', operatorMessage: reasonLabel(viewModel?.reason_code), tone: 'danger' };
    case 'INVALID':
      return { label: 'Messung ungültig', detail: 'INVALID', operatorMessage: reasonLabel(viewModel?.reason_code), tone: 'danger' };
    case 'ERROR':
      return { label: 'Fehler', detail: 'ERROR', operatorMessage: reasonLabel(viewModel?.reason_code), tone: 'danger' };
    default:
      return { label: 'Bereit', detail: 'IDLE', operatorMessage: 'Warten auf einen Messzyklus.', tone: 'neutral' };
  }
}

function priorityLabel(mode: StemRotationPriorityMode) {
  if (mode === 'VORSCHNITT_KRITISCH') return 'Vorschnitt / oben–unten (z)';
  if (mode === 'NACHSCHNITT_KRITISCH') return 'Nachschnitt / links–rechts (y)';
  return 'Keine Priorität';
}

function rotationDirectionLabel(viewModel: RuntimeStemRotationViewModel) {
  const physicalDirection = physicalRotationDirection(viewModel);
  const physicalLabel =
    physicalDirection === 'CLOCKWISE'
      ? 'Uhrzeigersinn'
      : physicalDirection === 'COUNTER_CLOCKWISE'
        ? 'Gegenuhrzeigersinn'
        : null;
  if (viewModel.rotation_direction === 'POSITIVE') {
    return `Positive Drehung um +x${physicalLabel ? ` · ${physicalLabel}` : ''}${viewModel.rotation_reference_view ? ` · Blick: ${viewModel.rotation_reference_view}` : ''}`;
  }
  if (viewModel.rotation_direction === 'NEGATIVE') {
    return `Negative Drehung um +x${physicalLabel ? ` · ${physicalLabel}` : ''}${viewModel.rotation_reference_view ? ` · Blick: ${viewModel.rotation_reference_view}` : ''}`;
  }
  return 'Keine Drehung';
}

function rotationArrow(viewModel: RuntimeStemRotationViewModel) {
  return physicalRotationDirection(viewModel) === 'CLOCKWISE' ? '↻' : '↺';
}

function physicalRotationDirection(
  viewModel: RuntimeStemRotationViewModel
): 'CLOCKWISE' | 'COUNTER_CLOCKWISE' | null {
  const configured = viewModel.positive_rotation?.trim().toUpperCase();
  const positive =
    configured === 'CLOCKWISE' || configured === 'COUNTER_CLOCKWISE'
      ? configured
      : null;
  if (
    !positive ||
    viewModel.rotation_direction === 'NONE' ||
    viewModel.rotation_direction === 'UNAVAILABLE'
  ) {
    return null;
  }
  if (viewModel.rotation_direction === 'POSITIVE') return positive;
  return positive === 'CLOCKWISE' ? 'COUNTER_CLOCKWISE' : 'CLOCKWISE';
}

function reasonLabel(reasonCode?: string | null) {
  const labels: Record<string, string> = {
    OPTIMIZATION_DISABLED: 'Die Eindrehoptimierung ist deaktiviert.',
    INVALID_PRIORITY_MODE: 'Keine gültige priorisierte Richtung gewählt.',
    INSUFFICIENT_MEASUREMENT: 'Messdaten reichen für eine Empfehlung nicht aus.',
    INSUFFICIENT_ANGLE_COVERAGE: 'Die Winkelabdeckung reicht für eine Empfehlung nicht aus.',
    INVALID_STEM_MODEL: 'Das Scheibenmodell ist ungültig.',
    LOW_MODEL_QUALITY: 'Die Modellqualität reicht für eine Empfehlung nicht aus.',
    OPTIMIZER_FAILED: 'Die geometrische Optimierung ist fehlgeschlagen.',
    MECHANICAL_ROTATION_CONSTRAINT_CONFLICT: 'Die mechanischen Drehgrenzen erlauben keine sichere Empfehlung.'
  };
  return labels[reasonCode ?? ''] ?? 'Keine gültige Drehempfehlung verfügbar.';
}

function closedPointPath(
  points: RuntimeStemRotationViewPoint[],
  x: (value: number) => number,
  y: (value: number) => number
) {
  if (!points.length) return '';
  return `${points.map((point, index) => `${index ? 'L' : 'M'} ${x(point.y_mm)} ${y(point.z_mm)}`).join(' ')} Z`;
}

function pathFrom<T>(
  points: T[],
  x: (point: T) => number,
  y: (point: T) => number
) {
  return points.map((point, index) => `${index ? 'L' : 'M'} ${x(point)} ${y(point)}`).join(' ');
}

function linearScale(domainMin: number, domainMax: number, rangeMin: number, rangeMax: number) {
  const span = Math.max(domainMax - domainMin, 1e-9);
  return (value: number) => rangeMin + ((value - domainMin) / span) * (rangeMax - rangeMin);
}

function formatSigned(value: number | null | undefined, digits: number) {
  if (!finite(value)) return '–';
  return `${value >= 0 ? '+' : ''}${value.toFixed(digits)}`;
}

function formatNumber(value: number | null | undefined, digits: number) {
  return finite(value) ? value.toFixed(digits) : '–';
}

function formatMillimeters(value: number | null | undefined) {
  return finite(value) ? `${value.toFixed(2)} mm` : '–';
}

function formatPercent(value: number | null | undefined) {
  return finite(value) ? `${(value * 100).toFixed(0)} %` : '–';
}

function formatX(value: number | null | undefined) {
  return finite(value) ? `x = ${value.toFixed(0)} mm` : '–';
}

function formatTimestamp(value: string) {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString('de-AT') : value;
}

function formatConfigValue(value: unknown) {
  if (value === null || value === undefined) return '–';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function finite(value: number | null | undefined): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function finiteNumber(value: number | null | undefined): value is number {
  return finite(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}
