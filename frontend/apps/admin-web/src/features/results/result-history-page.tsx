import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import type {
  RuntimeMode,
  RuntimePipelineDefinitionSummary,
  RuntimeResultRecordDetail,
  RuntimeResultRecordSummary,
  RuntimeStemShadowEvaluationResponse,
  RuntimeStemOrientationReview,
  RuntimeStemOrientationReviewCreateRequest
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { BarkArtifactViewer, BarkResultView } from '../runtime-pipelines/bark-inspection-workspace';
import { StemCurvaturePhaseDiagnosticGraph } from '../stem-orientation/curvature-phase-diagnostic-graph';

type ResultFilters = {
  search: string;
  channelId: string;
  pipelineVersionId: string;
  mode: RuntimeMode | 'all';
  status: 'completed' | 'failed' | 'timeout' | 'all';
  resultType: string;
  completedFrom: string;
  completedTo: string;
};

const emptyFilters: ResultFilters = {
  search: '',
  channelId: '',
  pipelineVersionId: '',
  mode: 'all',
  status: 'all',
  resultType: '',
  completedFrom: '',
  completedTo: ''
};

const resultTypeOptions = [
  { value: '', label: 'All result types' },
  { value: 'realtime_frame', label: 'Realtime frame' },
  { value: 'trigger_execution', label: 'Trigger execution' },
  { value: 'failed_execution', label: 'Failed execution' },
  { value: 'timeout', label: 'Timeout' },
  { value: 'pipeline_execution', label: 'Pipeline result' },
  { value: 'stem_orientation', label: 'Stem orientation' }
];

export function ResultHistoryPage({fixedChannelId}: {fixedChannelId?: string} = {}) {
  const [filters, setFilters] = useState<ResultFilters>(emptyFilters);
  const [selectedResultId, setSelectedResultId] = useState<string | null>(null);

  const channelsQuery = useQuery({
    queryKey: ['runtime-channels', 'result-history'],
    queryFn: () => api.channels.list(),
    staleTime: 30000
  });

  const pipelineDefinitionsQuery = useQuery({
    queryKey: ['runtime-pipeline-definitions', 'result-history'],
    queryFn: () => api.runtimePipelines.list({ includeTemplates: false }),
    staleTime: 30000
  });

  const resultParams = useMemo(
    () => ({
      channelId: fixedChannelId || filters.channelId || undefined,
      pipelineVersionId: filters.pipelineVersionId || undefined,
      mode: filters.mode === 'all' ? undefined : filters.mode,
      status: filters.status === 'all' ? undefined : filters.status,
      resultType: filters.resultType || undefined,
      completedFrom: toIsoFromLocal(filters.completedFrom),
      completedTo: toIsoFromLocal(filters.completedTo),
      search: filters.search.trim() || undefined,
      limit: 50
    }),
    [filters, fixedChannelId]
  );

  const resultsQuery = useQuery({
    queryKey: ['runtime-results', resultParams],
    queryFn: () => api.runtimeResults.list(resultParams),
    refetchInterval: 5000
  });

  const shadowEvaluationQuery = useQuery({
    queryKey: [
      'stem-shadow-evaluation',
      filters.pipelineVersionId,
      resultParams.completedFrom,
      resultParams.completedTo
    ],
    queryFn: () => api.runtimeResults.stemShadowEvaluation(filters.pipelineVersionId, {
      completedFrom: resultParams.completedFrom,
      completedTo: resultParams.completedTo,
      maximumSessions: 2000
    }),
    enabled: Boolean(filters.pipelineVersionId),
    refetchInterval: 15000
  });

  const items = resultsQuery.data?.items ?? [];
  const selectedId =
    selectedResultId && items.some((item) => item.result_record_id === selectedResultId)
      ? selectedResultId
      : items[0]?.result_record_id ?? null;

  const detailQuery = useQuery({
    queryKey: ['runtime-result-detail', selectedId],
    queryFn: () => api.runtimeResults.detail(selectedId as string),
    enabled: Boolean(selectedId),
    refetchInterval: 5000
  });

  const completedCount = items.filter((item) => item.status === 'completed').length;
  const triggerCount = items.filter((item) => item.mode === 'trigger').length;

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Result History</p>
          <h2>Browse runtime results by channel, pipeline, time, and mode.</h2>
          <p className="subtle-text">
            Trigger responses, realtime persisted results, inference outputs, and calculated fields are kept together for operator review.
          </p>
        </div>
        <div className="training-summary-pills">
          <MetricPill label="Loaded" value={String(items.length)} />
          <MetricPill label="Total Match" value={String(resultsQuery.data?.total ?? 0)} />
          <MetricPill label="Completed" value={String(completedCount)} tone="success" />
          <MetricPill label="Trigger" value={String(triggerCount)} />
        </div>
      </section>

      <ResultFilterPanel
        filters={filters}
        onChange={(next) => {
          setSelectedResultId(null);
          setFilters(next);
        }}
        channels={channelsQuery.data?.items ?? []}
        pipelines={pipelineDefinitionsQuery.data?.items ?? []}
      />

      {filters.pipelineVersionId ? (
        <StemShadowEvaluationPanel
          evaluation={shadowEvaluationQuery.data ?? null}
          loading={shadowEvaluationQuery.isLoading}
          error={shadowEvaluationQuery.error}
        />
      ) : null}

      <div className="training-grid training-grid--details">
        <Panel title="Results">
          {items.length ? (
            <div className="runtime-result-list">
              {items.map((item) => (
                <RuntimeResultCard
                  key={item.result_record_id}
                  result={item}
                  selected={item.result_record_id === selectedId}
                  onSelect={() => setSelectedResultId(item.result_record_id)}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              title={resultsQuery.isLoading ? 'Loading results' : 'No results match'}
              body="Adjust the filters or run a channel pipeline that emits persisted results."
            />
          )}
        </Panel>

        <Panel title="Result Detail">
          <ResultDetailPanel result={detailQuery.data?.item ?? null} loading={detailQuery.isLoading} />
        </Panel>
      </div>
    </div>
  );
}

export function StemShadowEvaluationPanel(props: {
  evaluation: RuntimeStemShadowEvaluationResponse | null;
  loading?: boolean;
  error?: unknown;
}) {
  if (!props.evaluation) {
    return (
      <Panel title="Shadow-Mode Produktionsbewertung">
        <p className={props.error ? 'error-text' : 'subtle-text'}>
          {props.error
            ? errorMessage(props.error)
            : props.loading
              ? 'Shadow-Sessions werden serverseitig ausgewertet ...'
              : 'Keine Shadow-Auswertung verfuegbar.'}
        </p>
      </Panel>
    );
  }
  const evaluation = props.evaluation;
  return (
    <Panel title="Shadow-Mode Produktionsbewertung">
      <div className="detail-grid">
        <MetricPill label="Status" value={humanize(evaluation.evaluation_status)} tone={evaluation.evaluation_status === 'ACTIVE' ? 'success' : 'warn'} />
        <MetricPill label="Sessions" value={String(evaluation.sessions_analyzed)} />
        <MetricPill label="NO_ROTATION" value={formatPercent(evaluation.no_rotation_share)} />
        <MetricPill label="UNCERTAIN" value={formatPercent(evaluation.uncertain_share)} tone={evaluation.uncertain_share > 0.25 ? 'warn' : 'neutral'} />
        <MetricPill label="Decision P95" value={formatMilliseconds(evaluation.decision_latency.p95_ms)} />
        <MetricPill label="Drift" value={humanize(evaluation.drift.status)} tone={evaluation.drift.status === 'DRIFT_SIGNAL' ? 'warn' : 'neutral'} />
      </div>
      {!evaluation.shadow_mode_enabled ? (
        <p className="error-text">Shadow Mode ist in dieser Pipelineversion nicht aktiviert.</p>
      ) : null}
      {evaluation.limit_reached ? (
        <p className="subtle-text">Die Auswertung ist auf die neuesten {evaluation.maximum_sessions} Sessions begrenzt.</p>
      ) : null}
      <div className="button-row">
        <button type="button" onClick={() => downloadText(
          `${evaluation.pipeline_version_id}-shadow-evaluation.json`,
          JSON.stringify(evaluation.report_json, null, 2),
          'application/json'
        )}>JSON-Bericht exportieren</button>
        <button type="button" onClick={() => downloadText(
          `${evaluation.pipeline_version_id}-shadow-evaluation.md`,
          evaluation.report_markdown,
          'text/markdown'
        )}>Markdown-Bericht exportieren</button>
      </div>
      <div className="training-grid training-grid--details">
        <ShadowDistributionTable title="Entscheidungen" items={evaluation.decision_distribution} />
        <ShadowDistributionTable title="Reason Codes" items={evaluation.reason_code_distribution} />
      </div>
      <details className="advanced-section" open>
        <summary>Winkel- und Confidenceverteilung</summary>
        <table className="data-table compact-table">
          <thead><tr><th>Winkelbereich [deg]</th><th>Anzahl</th></tr></thead>
          <tbody>
            {evaluation.angle_histogram.map((bin) => (
              <tr key={`${bin.lower_bound}-${bin.upper_bound}`}>
                <td>{bin.lower_bound} bis {bin.upper_bound}</td>
                <td>{bin.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table className="data-table compact-table">
          <thead><tr><th>Confidencebereich</th><th>Anzahl</th></tr></thead>
          <tbody>
            {evaluation.confidence_histogram.map((bin) => (
              <tr key={`confidence-${bin.lower_bound}-${bin.upper_bound}`}>
                <td>{bin.lower_bound.toFixed(1)} bis {bin.upper_bound.toFixed(1)}</td>
                <td>{bin.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="subtle-text">
          Confidence Median/P90/P95: {formatOptionalNumber(evaluation.confidence_percentiles.median)} / {formatOptionalNumber(evaluation.confidence_percentiles.p90)} / {formatOptionalNumber(evaluation.confidence_percentiles.p95)}
        </p>
      </details>
      <details className="advanced-section" open={evaluation.drift.status === 'DRIFT_SIGNAL'}>
        <summary>Kamera-/Kalibrierungsdrift</summary>
        <p className="subtle-text">
          Baseline/aktuell: {evaluation.drift.baseline_count}/{evaluation.drift.recent_count} Sessions; Confidence-Delta {formatOptionalNumber(evaluation.drift.confidence_delta)}; UNCERTAIN-Delta {formatPercent(evaluation.drift.uncertain_share_delta ?? 0)}
        </p>
        <p>{evaluation.drift.signals.length ? evaluation.drift.signals.join(', ') : 'Keine belegbaren Driftsignale.'}</p>
      </details>
      <details className="advanced-section">
        <summary>Bedienerfeedback ({evaluation.feedback.reviewed_session_count})</summary>
        <ShadowDistributionTable title="Bewertung" items={evaluation.feedback.ratings} />
        <ShadowDistributionTable title="Technische Fehlergruende" items={evaluation.feedback.technical_error_reasons} />
      </details>
    </Panel>
  );
}

function ShadowDistributionTable(props: {
  title: string;
  items: Array<{ key: string; count: number; share: number }>;
}) {
  return (
    <section className="runtime-result-section">
      <h3>{props.title}</h3>
      {props.items.length ? (
        <table className="data-table compact-table">
          <thead><tr><th>Wert</th><th>Anzahl</th><th>Anteil</th></tr></thead>
          <tbody>{props.items.map((item) => (
            <tr key={item.key}><td>{humanize(item.key)}</td><td>{item.count}</td><td>{formatPercent(item.share)}</td></tr>
          ))}</tbody>
        </table>
      ) : <p className="subtle-text">Keine Daten.</p>}
    </section>
  );
}

export function ResultFilterPanel(props: {
  filters: ResultFilters;
  onChange: (filters: ResultFilters) => void;
  channels: Array<{ channel_id: string; name: string; technical_place: string }>;
  pipelines: RuntimePipelineDefinitionSummary[];
}) {
  const pipelineOptions = props.pipelines.flatMap((pipeline) =>
    pipeline.published_version_id
      ? [{ value: pipeline.published_version_id, label: `${pipeline.name} / published` }]
      : []
  );
  return (
    <Panel title="Filter Results">
      <div className="result-filter-grid">
        <label className="filter-field result-filter-grid__search">
          <span>Search</span>
          <input
            value={props.filters.search}
            onChange={(event) => props.onChange({ ...props.filters, search: event.target.value })}
            placeholder="Channel, place, or correlation"
          />
        </label>
        <label className="filter-field">
          <span>Channel</span>
          <select
            value={props.filters.channelId}
            onChange={(event) => props.onChange({ ...props.filters, channelId: event.target.value })}
          >
            <option value="">All channels</option>
            {props.channels.map((channel) => (
              <option key={channel.channel_id} value={channel.channel_id}>
                {channel.name} / {channel.technical_place}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Pipeline</span>
          <select
            value={props.filters.pipelineVersionId}
            onChange={(event) => props.onChange({ ...props.filters, pipelineVersionId: event.target.value })}
          >
            <option value="">All pipelines</option>
            {pipelineOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>Mode</span>
          <select
            value={props.filters.mode}
            onChange={(event) => props.onChange({ ...props.filters, mode: event.target.value as ResultFilters['mode'] })}
          >
            <option value="all">All modes</option>
            <option value="realtime">Realtime</option>
            <option value="trigger">Trigger</option>
            <option value="interval">Interval</option>
          </select>
        </label>
        <label className="filter-field">
          <span>Status</span>
          <select
            value={props.filters.status}
            onChange={(event) => props.onChange({ ...props.filters, status: event.target.value as ResultFilters['status'] })}
          >
            <option value="all">All status</option>
            <option value="completed">Completed</option>
            <option value="failed">Failed</option>
            <option value="timeout">Timeout</option>
          </select>
        </label>
        <label className="filter-field">
          <span>Type</span>
          <select
            value={props.filters.resultType}
            onChange={(event) => props.onChange({ ...props.filters, resultType: event.target.value })}
          >
            {resultTypeOptions.map((option) => (
              <option key={option.value || 'all'} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-field">
          <span>From</span>
          <input
            type="datetime-local"
            value={props.filters.completedFrom}
            onChange={(event) => props.onChange({ ...props.filters, completedFrom: event.target.value })}
          />
        </label>
        <label className="filter-field">
          <span>To</span>
          <input
            type="datetime-local"
            value={props.filters.completedTo}
            onChange={(event) => props.onChange({ ...props.filters, completedTo: event.target.value })}
          />
        </label>
        <button type="button" onClick={() => props.onChange(emptyFilters)}>
          Clear
        </button>
      </div>
    </Panel>
  );
}

export function RuntimeResultCard(props: {
  result: RuntimeResultRecordSummary;
  selected?: boolean;
  onSelect?: () => void;
}) {
  const fields = resultFieldEntries(props.result);
  return (
    <button
      type="button"
      className={`runtime-result-card ${props.selected ? 'runtime-result-card--selected' : ''}`}
      onClick={props.onSelect}
    >
      <span className={`runtime-chip runtime-chip--${statusTone(props.result.status)}`}>
        {humanize(props.result.status)}
      </span>
      <strong>{props.result.channel_name ?? props.result.channel_id}</strong>
      <span className="subtle-text">
        {props.result.technical_place ?? 'No place'} / {modeLabel(props.result.mode)} / {humanize(props.result.result_type)}
      </span>
      <span className="subtle-text">{formatActivity(props.result.completed_at)}</span>
      {fields.length ? (
        <dl className="runtime-result-fields">
          {fields.slice(0, 4).map(([key, value]) => (
            <div key={key}>
              <dt>{humanize(key)}</dt>
              <dd>{valuePreview(value)}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <span className="subtle-text">No final fields</span>
      )}
    </button>
  );
}

export function ResultDetailPanel(props: { result: RuntimeResultRecordDetail | null; loading?: boolean }) {
  if (!props.result) {
    return (
      <EmptyState
        title={props.loading ? 'Loading result' : 'Choose a result'}
        body="Select a result to inspect final fields, calculated values, inference outputs, overlays, and runtime metadata."
      />
    );
  }
  const result = props.result;
  const stemResult = stemOrientationResult(result);
  return (
    <div className="panel-stack">
      <div className="detail-grid">
        <MetricPill label="Status" value={humanize(result.status)} tone={statusTone(result.status)} />
        <MetricPill label="Mode" value={modeLabel(result.mode)} />
        <MetricPill label="Type" value={humanize(result.result_type)} />
        <MetricPill label="Latency" value={formatLatency(result.timing.total_ms)} />
        <MetricPill label="Inference" value={String(result.inference_output_count)} />
        <MetricPill label="Calculated" value={String(result.derived_output_count)} />
      </div>

      <dl className="definition-list">
        <div>
          <dt>Channel</dt>
          <dd>
            {result.channel_name ?? result.channel_id}
            <small>{result.technical_place ?? result.channel_id}</small>
          </dd>
        </div>
        <div>
          <dt>Completed</dt>
          <dd>{formatActivity(result.completed_at)}</dd>
        </div>
        <div>
          <dt>Correlation</dt>
          <dd>{result.correlation_id}</dd>
        </div>
        <div>
          <dt>Pipeline</dt>
          <dd>{result.pipeline_version_id ?? 'Not recorded'}</dd>
        </div>
      </dl>

      <ResultFieldBlock title="Final Response" payload={result.trigger_response} />
      <BarkResultView value={result.trigger_response} />
      <BarkArtifactViewer resultId={result.result_record_id} artifacts={result.runtime_metadata.bark_artifacts} />
      <ResultFieldBlock title="Calculated Values" payload={result.derived_preview} />

      {stemResult ? (
        <>
          <StemOrientationResultSummary result={stemResult} />
          <StemOrientationReviewPanel
            resultRecordId={result.result_record_id}
            technicalOnly={stemResult.schema_version === 'stem.orientation.v3'}
          />
        </>
      ) : null}

      <ResultEntryList title="Derived Output Records" entries={result.derived_outputs} />
      <ResultEntryList title="Normalized Inference Outputs" entries={result.normalized_inference_outputs} />

      {result.overlay_primitives.length ? (
        <details className="advanced-section" open>
          <summary>Overlay primitives</summary>
          <table className="data-table compact-table">
            <thead>
              <tr>
                <th>Type</th>
                <th>Label</th>
                <th>Geometry</th>
              </tr>
            </thead>
            <tbody>
              {result.overlay_primitives.map((primitive, index) => (
                <tr key={`${String(primitive.primitive_type)}-${index}`}>
                  <td>{humanize(String(primitive.primitive_type ?? 'primitive'))}</td>
                  <td>{String(primitive.label ?? 'n/a')}</td>
                  <td>{valuePreview(primitive.geometry)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}

      <details className="advanced-section">
        <summary>Raw inference and runtime metadata</summary>
        <pre className="code-block">
          {JSON.stringify(
            {
              raw_inference_outputs: result.raw_inference_outputs,
              runtime_metadata: result.runtime_metadata,
              execution_result: result.execution_result
            },
            null,
            2
          )}
        </pre>
      </details>
    </div>
  );
}

type StemReviewDraft = {
  proposalPlausibility: RuntimeStemOrientationReviewCreateRequest['proposal_plausibility'];
  directionAssessment: RuntimeStemOrientationReviewCreateRequest['direction_assessment'];
  angleAssessment: RuntimeStemOrientationReviewCreateRequest['angle_assessment'];
  bowRelevance: RuntimeStemOrientationReviewCreateRequest['bow_relevance'];
  finalPosition: RuntimeStemOrientationReviewCreateRequest['final_position'] | '';
  targetFrameStart: string;
  targetFrameEnd: string;
  comment: string;
  specialCase: string;
};

const emptyStemReview: StemReviewDraft = {
  proposalPlausibility: 'unclear',
  directionAssessment: 'unclear',
  angleAssessment: 'not_assessable',
  bowRelevance: 'unclear',
  finalPosition: '',
  targetFrameStart: '',
  targetFrameEnd: '',
  comment: '',
  specialCase: ''
};

export function StemOrientationResultSummary(props: { result: Record<string, unknown> }) {
  const isCurvatureMinimum = props.result.schema_version === 'stem.orientation.v3';
  const recommendation = recordValue(props.result.recommendation);
  const decision = recordValue(props.result.decision);
  const target = recordValue(props.result.target_estimate);
  const versions = recordValue(props.result.versions);
  const direction = String(
    isCurvatureMinimum ? props.result.direction ?? 'UNKNOWN' : recommendation.direction ?? 'manual_required'
  );
  const signedRotation = numberValue(
    isCurvatureMinimum ? props.result.recommended_rotation_deg_signed : recommendation.signed_rotation_deg
  );
  const confidence = numberValue(
    isCurvatureMinimum ? props.result.overall_confidence : props.result.confidence
  ) ?? 0;
  const decisionReady = Boolean(decision.ready ?? props.result.decision_ready);
  const decisionKind = isCurvatureMinimum ? String(props.result.decision ?? 'UNCERTAIN') : null;
  const recommendationText =
    decisionReady && decisionKind !== 'UNCERTAIN' && signedRotation !== null
      ? direction.toLowerCase() === 'none'
        ? `Keine Drehung erforderlich – Konfidenz ${formatConfidence(confidence)}`
        : `Drehe ${formatDegrees(Math.abs(signedRotation))} ${directionLabel(direction)} – Konfidenz ${formatConfidence(confidence)}`
      : 'Keine sichere Winkelentscheidung – Bediener erforderlich';
  const curvatureTrace = traceSamples(props.result.compact_curvature_trace);
  const rotationTrace = traceSamples(props.result.compact_rotation_trace);
  const assets = recordValue(props.result.asset_references);
  const selectedFrames = recordArray(assets.selected_frames);
  const postRotation = recordValueOrNull(props.result.post_rotation_verification);

  return (
    <section className="runtime-result-section">
      <h3>Vor-Einzug-Empfehlung</h3>
      <p className="runtime-callout"><strong>{recommendationText}</strong></p>
      <dl className="definition-list">
        <div>
          <dt>Track / Session</dt>
          <dd>{String(props.result.track_id ?? 'n/a')}<small>{String(props.result.session_id ?? 'n/a')}</small></dd>
        </div>
        <div>
          <dt>Beobachtungsbeginn</dt>
          <dd>{formatActivity(String(isCurvatureMinimum ? props.result.session_started_timestamp ?? '' : props.result.observation_started_at ?? ''))}</dd>
        </div>
        <div>
          <dt>Decision Frame</dt>
          <dd>{valuePreview(isCurvatureMinimum ? props.result.decision_frame_sequence : decision.frame_sequence)}<small>{formatActivity(String(isCurvatureMinimum ? props.result.decision_timestamp ?? '' : decision.timestamp ?? ''))}</small></dd>
        </div>
        <div>
          <dt>{isCurvatureMinimum ? 'Zielphase / aktuelle Phase' : 'Zielkandidat'}</dt>
          <dd>{isCurvatureMinimum ? `${valuePreview(props.result.target_phase_deg)}° / ${valuePreview(props.result.current_phase_deg)}°` : frameInterval(target.frame_start, target.frame_end)}<small>{isCurvatureMinimum ? (props.result.target_observed ? 'minimum observed' : 'minimum fitted') : String(target.candidate_type ?? 'not observed')}</small></dd>
        </div>
        <div>
          <dt>Blickkonvention</dt>
          <dd>{humanize(String(isCurvatureMinimum ? props.result.rotation_reference_view ?? 'not recorded' : recommendation.rotation_viewpoint_convention ?? 'not recorded'))}</dd>
        </div>
        <div>
          <dt>Positive Drehrichtung</dt>
          <dd>{humanize(String(isCurvatureMinimum ? props.result.positive_rotation ?? 'not recorded' : recommendation.positive_rotation_direction ?? 'not recorded'))}</dd>
        </div>
        <div>
          <dt>Methode</dt>
          <dd>{humanize(String(isCurvatureMinimum ? props.result.algorithm ?? 'unknown' : props.result.estimate_method ?? 'unknown'))}</dd>
        </div>
        {isCurvatureMinimum ? <div>
          <dt>Shadow / SPS</dt>
          <dd>{props.result.shadow_mode ? 'Shadow Mode' : 'Produktivmodus'}<small>{props.result.plc_writes_suppressed ? 'SPS-Ausgabe unterdrueckt' : props.result.shadow_evaluation_approved ? 'Shadow fachlich freigegeben' : 'Keine Shadow-Freigabe'}</small></dd>
        </div> : null}
        {isCurvatureMinimum ? <div>
          <dt>Referenzvalidierung</dt>
          <dd>{humanize(String(props.result.minimum_principle_validation_status ?? 'not recorded'))}<small>{String(props.result.minimum_principle_validation_id ?? 'validation not recorded')}</small></dd>
        </div> : null}
        <div>
          <dt>Reason Codes</dt>
          <dd>{valuePreview(props.result.reason_codes)}</dd>
        </div>
        <div>
          <dt>Pipeline / Modell</dt>
          <dd>{String(versions.pipeline_version_id ?? props.result.pipeline_version_id ?? 'n/a')}<small>{String(versions.segmentation_model_version_id ?? 'model not recorded')}</small></dd>
        </div>
        <div>
          <dt>Kalibrierung</dt>
          <dd>{String(isCurvatureMinimum ? props.result.rotation_calibration_id ?? 'not recorded' : versions.rotation_calibration_id ?? 'not recorded')}<small>{isCurvatureMinimum ? `Prozess ${String(props.result.calibration_id ?? 'not recorded')}` : `Intrinsik ${String(versions.intrinsic_calibration_hash ?? 'not recorded')}`}</small></dd>
        </div>
        <div>
          <dt>{isCurvatureMinimum ? 'Krümmung Min / Max / Span' : 'Prozesszonen'}</dt>
          <dd>{isCurvatureMinimum ? `${valuePreview(props.result.curvature_min_mm)} / ${valuePreview(props.result.curvature_max_mm)} / ${valuePreview(props.result.curvature_span_mm)} mm` : String(versions.process_zone_calibration_id ?? 'not recorded')}<small>{isCurvatureMinimum ? `Rotation ${valuePreview(props.result.observed_rotation_span_deg)}°` : `Version ${String(versions.process_zone_calibration_version ?? 'not recorded')}`}</small></dd>
        </div>
        <div>
          <dt>Feature-Version</dt>
          <dd>{String(isCurvatureMinimum ? props.result.curvature_algorithm_version ?? 'not recorded' : versions.feature_schema_version ?? 'not recorded')}<small>{String(isCurvatureMinimum ? props.result.solver_algorithm_version ?? 'algorithm not recorded' : versions.rotation_algorithm_version ?? 'algorithm not recorded')}</small></dd>
        </div>
      </dl>

      {isCurvatureMinimum ? (
        <StemCurvaturePhaseDiagnosticGraph graph={props.result.diagnostic_graph} />
      ) : (
        <StemTraceTable curvature={curvatureTrace} rotation={rotationTrace} />
      )}

      <details className="advanced-section" open>
        <summary>Ausgewählte Frames und Artefaktverweise</summary>
        {selectedFrames.length ? (
          <table className="data-table compact-table">
            <thead><tr><th>Rolle</th><th>Frame</th><th>Zeit</th><th>Asset</th></tr></thead>
            <tbody>
              {selectedFrames.map((frame, index) => (
                <tr key={`${String(frame.role)}-${String(frame.frame_sequence)}-${index}`}>
                  <td>{humanize(String(frame.role ?? 'frame'))}</td>
                  <td>{valuePreview(frame.frame_sequence)}</td>
                  <td>{frame.timestamp ? formatActivity(String(frame.timestamp)) : 'n/a'}</td>
                  <td>{String(frame.asset_ref ?? 'nicht aufgezeichnet')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <p className="subtle-text">Keine ausgewählten Frameverweise aufgezeichnet.</p>}
        <p className="subtle-text">
          {isCurvatureMinimum
            ? `Entzerrtes Video: ${String(assets.rectified_video_ref ?? 'nicht aufgezeichnet')} / Overlayvideo: ${String(assets.overlay_video_ref ?? 'nicht aufgezeichnet')}`
            : `Rohvideo: ${String(assets.raw_video_ref ?? 'nicht aufgezeichnet')} / Diagnosevideo: ${String(assets.diagnostic_video_ref ?? 'nicht aufgezeichnet')}`}
        </p>
        {isCurvatureMinimum ? (
          <p className="subtle-text">
            Beobachtungen: {String(assets.observations_ref ?? 'nicht aufgezeichnet')} / Phase: {String(assets.phase_trace_ref ?? 'nicht aufgezeichnet')} / Fit: {String(assets.fit_ref ?? 'nicht aufgezeichnet')} / Resultat: {String(assets.result_ref ?? 'nicht aufgezeichnet')}
          </p>
        ) : null}
      </details>

      <details className="advanced-section" open={postRotation !== null}>
        <summary>Getrennte Nachkontrolle</summary>
        {postRotation ? <pre className="code-block">{JSON.stringify(postRotation, null, 2)}</pre> : (
          <p className="subtle-text">Keine Nachkontrolle gespeichert; die Vor-Einzug-Empfehlung bleibt unverändert.</p>
        )}
      </details>
    </section>
  );
}

function StemTraceTable(props: {
  curvature: Record<string, unknown>[];
  rotation: Record<string, unknown>[];
}) {
  if (!props.curvature.length && !props.rotation.length) {
    return <p className="subtle-text">Keine kompakte Zeitreihe gespeichert.</p>;
  }
  const rotationsByFrame = new Map(
    props.rotation.map((item) => [String(item.frame_sequence), item])
  );
  return (
    <details className="advanced-section" open>
      <summary>Kompakte Krümmungs- und Rotationsspur</summary>
      <table className="data-table compact-table">
        <thead>
          <tr><th>Frame</th><th>Bogen norm.</th><th>RMS norm.</th><th>Rotation °</th><th>Rot.-Konf.</th><th>Inlier</th></tr>
        </thead>
        <tbody>
          {props.curvature.map((item, index) => {
            const rotation = rotationsByFrame.get(String(item.frame_sequence)) ?? props.rotation[index] ?? {};
            return (
              <tr key={`${String(item.frame_sequence)}-${index}`}>
                <td>{valuePreview(item.frame_sequence)}</td>
                <td>{valuePreview(item.signed_bow_normalized)}</td>
                <td>{valuePreview(item.rms_residual_normalized)}</td>
                <td>{valuePreview(rotation.cumulative_rotation_deg)}</td>
                <td>{valuePreview(rotation.rotation_confidence)}</td>
                <td>{valuePreview(rotation.inlier_ratio)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </details>
  );
}

export function StemOrientationReviewPanel(props: {
  resultRecordId: string;
  technicalOnly?: boolean;
}) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<StemReviewDraft>(emptyStemReview);
  const reviewsQuery = useQuery({
    queryKey: ['stem-orientation-reviews', props.resultRecordId],
    queryFn: () => api.runtimeResults.stemOrientationReviews(props.resultRecordId)
  });
  const latest = reviewsQuery.data?.latest ?? null;
  useEffect(() => {
    if (!latest) {
      setDraft(emptyStemReview);
      return;
    }
    setDraft(reviewToDraft(latest));
  }, [latest]);
  const appendMutation = useMutation({
    mutationFn: (payload: RuntimeStemOrientationReviewCreateRequest) =>
      api.runtimeResults.appendStemOrientationReview(props.resultRecordId, payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['stem-orientation-reviews', props.resultRecordId] });
    }
  });

  return (
    <section className="runtime-result-section">
      <h3>Stammausrichtungs-Schnellreview</h3>
      <p className="subtle-text">
        {props.technicalOnly
          ? 'Technisches Plausibilitaets- und Fehlerursachenreview; keine Ziel-Frame- oder Endlagenannotation.'
          : 'Diagnose und Vorannotation; kein verbindliches Ground-Truth-Label.'}
      </p>
      <form
        className="result-filter-grid"
        onSubmit={(event) => {
          event.preventDefault();
          if (!props.technicalOnly && !draft.finalPosition) return;
          appendMutation.mutate({
            proposal_plausibility: draft.proposalPlausibility,
            direction_assessment: props.technicalOnly ? 'unclear' : draft.directionAssessment,
            angle_assessment: props.technicalOnly ? 'not_assessable' : draft.angleAssessment,
            bow_relevance: props.technicalOnly ? 'unclear' : draft.bowRelevance,
            final_position: props.technicalOnly
              ? 'not_assessed'
              : (draft.finalPosition as RuntimeStemOrientationReviewCreateRequest['final_position']),
            target_frame_start: props.technicalOnly ? null : optionalFrame(draft.targetFrameStart),
            target_frame_end: props.technicalOnly ? null : optionalFrame(draft.targetFrameEnd),
            comment: props.technicalOnly ? '' : draft.comment,
            special_case: draft.specialCase
          });
        }}
      >
        <ReviewSelect label={props.technicalOnly ? 'Bewertung' : 'Vorschlag plausibel'} value={draft.proposalPlausibility} options={[
          ['yes', props.technicalOnly ? 'Plausibel' : 'Ja'],
          ['no', props.technicalOnly ? 'Unplausibel' : 'Nein'],
          ['unclear', props.technicalOnly ? 'Unsicher' : 'Unklar']
        ]} onChange={(value) => setDraft({ ...draft, proposalPlausibility: value as StemReviewDraft['proposalPlausibility'] })} />
        {!props.technicalOnly ? <>
        <ReviewSelect label="Richtung" value={draft.directionAssessment} options={[
          ['correct', 'Korrekt'], ['incorrect', 'Falsch'], ['unclear', 'Unklar']
        ]} onChange={(value) => setDraft({ ...draft, directionAssessment: value as StemReviewDraft['directionAssessment'] })} />
        <ReviewSelect label="Winkel" value={draft.angleAssessment} options={[
          ['too_small', 'Grob zu klein'], ['appropriate', 'Passend'], ['too_large', 'Zu groß'], ['not_assessable', 'Nicht beurteilbar']
        ]} onChange={(value) => setDraft({ ...draft, angleAssessment: value as StemReviewDraft['angleAssessment'] })} />
        <ReviewSelect label="Bogen" value={draft.bowRelevance} options={[
          ['relevant', 'Relevant'], ['nearly_straight', 'Nahezu gerade'], ['unclear', 'Unklar']
        ]} onChange={(value) => setDraft({ ...draft, bowRelevance: value as StemReviewDraft['bowRelevance'] })} />
        </> : null}
        {!props.technicalOnly ? <>
          <ReviewSelect label="Finale Lage" value={draft.finalPosition} options={[
            ['', 'Bitte wählen'], ['good', 'Good'], ['acceptable', 'Acceptable'], ['bad', 'Bad']
          ]} onChange={(value) => setDraft({ ...draft, finalPosition: value as StemReviewDraft['finalPosition'] })} />
          <label className="filter-field"><span>Ziel-Frame von</span><input type="number" min="0" value={draft.targetFrameStart} onChange={(event) => setDraft({ ...draft, targetFrameStart: event.target.value })} /></label>
          <label className="filter-field"><span>Ziel-Frame bis</span><input type="number" min="0" value={draft.targetFrameEnd} onChange={(event) => setDraft({ ...draft, targetFrameEnd: event.target.value })} /></label>
        </> : null}
        <label className="filter-field"><span>{props.technicalOnly ? 'Technischer Fehlergrund (optional)' : 'Sonderfall'}</span><input value={draft.specialCase} maxLength={500} onChange={(event) => setDraft({ ...draft, specialCase: event.target.value })} /></label>
        {!props.technicalOnly ? <label className="filter-field result-filter-grid__search"><span>Kommentar</span><textarea value={draft.comment} maxLength={4000} onChange={(event) => setDraft({ ...draft, comment: event.target.value })} /></label> : null}
        <button type="submit" disabled={(!props.technicalOnly && !draft.finalPosition) || appendMutation.isPending}>Neue Review-Revision speichern</button>
      </form>
      {appendMutation.isError ? <p className="error-text">{errorMessage(appendMutation.error)}</p> : null}
      {reviewsQuery.isLoading ? <p className="subtle-text">Reviews werden geladen …</p> : null}
      {(reviewsQuery.data?.items ?? []).length ? (
        <details className="advanced-section" open>
          <summary>Review-Revisionen ({reviewsQuery.data?.items.length})</summary>
          <table className="data-table compact-table">
            <thead><tr><th>Revision</th><th>Review</th>{!props.technicalOnly ? <th>Ziel</th> : null}<th>Person / Zeit</th></tr></thead>
            <tbody>
              {(reviewsQuery.data?.items ?? []).map((item) => (
                <tr key={item.review_id}>
                  <td>#{item.revision}</td>
                  <td>{props.technicalOnly ? humanize(item.proposal_plausibility) : `${humanize(item.proposal_plausibility)} / ${humanize(item.direction_assessment)} / ${humanize(item.angle_assessment)}`}<small>{item.special_case || item.comment || 'Kein Kommentar'}</small></td>
                  {!props.technicalOnly ? <td>{frameInterval(item.target_frame_start, item.target_frame_end)}</td> : null}
                  <td>{item.reviewer_username}<small>{formatActivity(item.created_at)}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
    </section>
  );
}

function ReviewSelect(props: {
  label: string;
  value: string;
  options: Array<[string, string]>;
  onChange: (value: string) => void;
}) {
  return (
    <label className="filter-field">
      <span>{props.label}</span>
      <select value={props.value} onChange={(event) => props.onChange(event.target.value)}>
        {props.options.map(([value, label]) => <option key={value || 'empty'} value={value}>{label}</option>)}
      </select>
    </label>
  );
}

function ResultFieldBlock(props: { title: string; payload: Record<string, unknown> }) {
  const entries = Object.entries(props.payload);
  if (!entries.length) {
    return null;
  }
  return (
    <section className="runtime-result-section">
      <h3>{props.title}</h3>
      <dl className="runtime-result-fields runtime-result-fields--wide">
        {entries.map(([key, value]) => (
          <div key={key}>
            <dt>{humanize(key)}</dt>
            <dd>{valuePreview(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function ResultEntryList(props: { title: string; entries: Record<string, unknown>[] }) {
  if (!props.entries.length) {
    return null;
  }
  return (
    <details className="advanced-section" open>
      <summary>{props.title}</summary>
      <div className="runtime-result-entry-list">
        {props.entries.map((entry, index) => (
          <article key={`${props.title}-${index}`} className="runtime-result-entry">
            <strong>{humanize(String(entry.node_type ?? entry.payload_type ?? `entry_${index + 1}`))}</strong>
            <span className="subtle-text">{String(entry.node_key ?? entry.output_port ?? 'runtime output')}</span>
            <pre className="code-block">{JSON.stringify(entry.payload ?? entry, null, 2)}</pre>
          </article>
        ))}
      </div>
    </details>
  );
}

function resultFieldEntries(result: RuntimeResultRecordSummary): Array<[string, unknown]> {
  if (result.result_type === 'stem_orientation') {
    const stem = stemOrientationResult(result);
    if (stem) {
      return [
        ['recommended_rotation_deg_signed', stem.recommended_rotation_deg_signed],
        ['recommended_direction', stem.direction ?? stem.recommended_direction],
        ['confidence', stem.overall_confidence ?? stem.confidence],
        ['decision_frame', stem.decision_frame_sequence ?? recordValue(stem.decision_frame).frame_sequence]
      ];
    }
  }
  const responseEntries = Object.entries(result.trigger_response);
  if (responseEntries.length) {
    return responseEntries;
  }
  return Object.entries(result.derived_preview);
}

export function stemOrientationResult(
  result: RuntimeResultRecordSummary | RuntimeResultRecordDetail
): Record<string, unknown> | null {
  const candidates: unknown[] = [result.trigger_response, result.derived_preview];
  if ('derived_outputs' in result) {
    for (const entry of result.derived_outputs) {
      const payload = recordValue(entry.payload);
      candidates.push(payload.result);
    }
  }
  for (const candidate of candidates) {
    const record = recordValueOrNull(candidate);
    if (record?.schema_version === 'stem.orientation.v2' || record?.schema_version === 'stem.orientation.v3') {
      return record;
    }
  }
  return null;
}

function traceSamples(value: unknown): Record<string, unknown>[] {
  return recordArray(recordValue(value).samples);
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => recordValueOrNull(item) !== null)
    : [];
}

function recordValue(value: unknown): Record<string, unknown> {
  return recordValueOrNull(value) ?? {};
}

function recordValueOrNull(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function optionalFrame(value: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 ? parsed : undefined;
}

function reviewToDraft(review: RuntimeStemOrientationReview): StemReviewDraft {
  return {
    proposalPlausibility: review.proposal_plausibility,
    directionAssessment: review.direction_assessment,
    angleAssessment: review.angle_assessment,
    bowRelevance: review.bow_relevance,
    finalPosition: review.final_position,
    targetFrameStart: review.target_frame_start === null || review.target_frame_start === undefined ? '' : String(review.target_frame_start),
    targetFrameEnd: review.target_frame_end === null || review.target_frame_end === undefined ? '' : String(review.target_frame_end),
    comment: review.comment,
    specialCase: review.special_case
  };
}

function frameInterval(start: unknown, end: unknown) {
  const first = numberValue(start);
  const last = numberValue(end);
  if (first === null) return 'n/a';
  return last === null || last === first ? `Frame ${first}` : `Frames ${first}–${last}`;
}

function directionLabel(direction: string) {
  if (direction === 'clockwise_viewed_from_configured_log_end' || direction === 'CLOCKWISE') return 'im Uhrzeigersinn';
  if (direction === 'counter_clockwise_viewed_from_configured_log_end' || direction === 'COUNTER_CLOCKWISE') return 'gegen den Uhrzeigersinn';
  return humanize(direction);
}

function formatDegrees(value: number) {
  return `${new Intl.NumberFormat('de-DE', { maximumFractionDigits: 1 }).format(value)}°`;
}

function formatConfidence(value: number) {
  return new Intl.NumberFormat('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
}

function formatPercent(value: number) {
  return new Intl.NumberFormat('de-DE', { style: 'percent', maximumFractionDigits: 1 }).format(value);
}

function formatMilliseconds(value?: number | null) {
  return value === null || value === undefined ? 'n/a' : `${value.toFixed(1)} ms`;
}

function formatOptionalNumber(value?: number | null) {
  return value === null || value === undefined ? 'n/a' : value.toFixed(3);
}

function downloadText(filename: string, content: string, mimeType: string) {
  const url = URL.createObjectURL(new Blob([content], { type: `${mimeType};charset=utf-8` }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function errorMessage(value: unknown) {
  return value instanceof Error ? value.message : 'Review konnte nicht gespeichert werden.';
}

function toIsoFromLocal(value: string) {
  if (!value) {
    return undefined;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? undefined : parsed.toISOString();
}

function valuePreview(value: unknown) {
  if (value === null || value === undefined || value === '') {
    return 'n/a';
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(3);
  }
  if (typeof value === 'boolean') {
    return value ? 'yes' : 'no';
  }
  if (typeof value === 'string') {
    return value;
  }
  return JSON.stringify(value);
}

function formatLatency(value: unknown) {
  return typeof value === 'number' ? `${value.toFixed(1)} ms` : 'n/a';
}

function formatActivity(value?: string | null) {
  if (!value) {
    return 'No timestamp';
  }
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(new Date(value));
}

function modeLabel(mode: RuntimeMode) {
  const labels: Record<RuntimeMode, string> = {
    realtime: 'Realtime',
    trigger: 'Trigger',
    interval: 'Interval'
  };
  return labels[mode];
}

function statusTone(status: string): 'neutral' | 'success' | 'warn' | 'danger' {
  if (status === 'completed') {
    return 'success';
  }
  if (status === 'timeout') {
    return 'warn';
  }
  if (status === 'failed') {
    return 'danger';
  }
  return 'neutral';
}

function humanize(value: string) {
  return value
    .replaceAll('_', ' ')
    .replaceAll('-', ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}
