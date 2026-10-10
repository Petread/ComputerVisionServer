import { useEffect, useRef, useState, type PointerEvent } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type {
  BarkMeasurementResult, BarkSideMapping, RuntimeLiveFrame, RuntimePipelineGraph, StemBarkJob
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { PipelineTrainingCapture } from './pipeline-training-capture';

type Box = { x: number; y: number; width: number; height: number };
type Json = Record<string, unknown>;
const mappingLabels = {
  left_first: 'Links = erste Scheibe · Rechts = letzte Scheibe',
  left_last: 'Links = letzte Scheibe · Rechts = erste Scheibe'
};
const obj = (value: unknown): Json => value && typeof value === 'object' ? value as Json : {};
const reasonLabels: Record<string, string> = {
  registration_outline_ambiguous: 'Die Kontur ist zu kreis- oder ellipsenförmig für eine eindeutige Kalibrierung.',
  registration_unstable_or_ambiguous: 'Mehrere Ausrichtungen sind möglich oder die Kalibrierung ist instabil.',
  registration_contour_mismatch: 'Bildkontur und CSV-Scheibe stimmen nicht ausreichend überein.',
  registration_coverage_insufficient: 'Die Konturen stimmen nicht über den gesamten Umfang überein.',
  registration_failed: 'Keine stabile Zuordnung von Bild und Messscheibe gefunden.',
  registration_singular: 'Die berechnete Ausrichtung ist geometrisch ungültig.',
  endface_clipped: 'Die Stirnfläche wird vom Bildrand abgeschnitten. Bildbereich vergrößern.',
  exactly_one_endface_with_full_mask_required: 'Es wird genau eine vollständig segmentierte Schnittfläche benötigt.',
  endface_outline_disconnected: 'Die erkannte Schnittfläche besteht aus getrennten Bereichen.',
  insufficient_native_image_resolution: 'Die ursprüngliche Bild- oder Maskenauflösung reicht nicht aus.',
  center_not_inside_wood: 'Der Schwerpunkt der Messscheibe liegt nicht sicher innerhalb des Holzes.',
  outer_boundary_ambiguous_or_clipped: 'Der Strahl trifft den Außenrand mehrfach oder der Rand fehlt.',
  wood_missing: 'Auf diesem Strahl wurde kein Holz erkannt.',
  multiple_wood_bark_crossings: 'Mehrere Übergänge zwischen Holz und Rinde machen den Strahl uneindeutig.',
  boundary_unknown_or_occluded: 'Der Übergang ist unklar oder verdeckt.',
  bark_not_confirmed: 'Der Zwischenbereich wurde nicht vollständig als Rinde erkannt.',
  insufficient_valid_rays: 'Zu wenige radiale Messungen sind gültig.',
  excessive_invalid_angular_gap: 'Die zusammenhängende Lücke ungültiger Messungen ist zu groß.',
  radial_mean_registration_unstable: 'Der Mittelwert reagiert zu stark auf kleine Änderungen der Kalibrierung.',
  lossless_semantic_masks_required: 'Das Modell liefert keine vollständigen Klassenmasken.',
  semantic_labels_must_be_distinct: 'Holz, Rinde, Hintergrund und unklar benötigen unterschiedliche Modellklassen.'
};
const reasonLabel = (code: string | null | undefined) => code ? reasonLabels[code] ?? code : 'nicht verfügbar';

export function effectiveBarkMapping(configured: unknown, override: unknown): BarkSideMapping | null {
  const value = override || configured;
  return value === 'left_first' || value === 'left_last' ? value : null;
}

export function barkResultFromExecution(value: unknown): BarkMeasurementResult | null {
  const data = obj(value);
  const candidates = [data, obj(data.final_response), ...Object.values(obj(data.node_outputs)).flatMap(
    outputs => Object.values(obj(outputs)).map(output => obj(output).result)
  )];
  return candidates.find(candidate => obj(candidate).schema_version === 'stem.bark-measurement.v1'
    && obj(candidate).left && obj(candidate).right) as BarkMeasurementResult | undefined ?? null;
}

export function barkChartRuns(samples: BarkMeasurementResult['left']['samples']): number[][][] {
  const runs: number[][][] = [];
  let current: number[][] = [];
  for (const sample of samples) {
    if (sample.valid && sample.thickness_mm !== null && Number.isFinite(sample.thickness_mm)) {
      current.push([sample.angle_deg, sample.thickness_mm]);
    } else if (current.length) { runs.push(current); current = []; }
  }
  if (current.length) runs.push(current);
  return runs;
}

export function BarkResultView({ value }: { value: unknown }) {
  const result = barkResultFromExecution(value);
  if (!result) return null;
  const number = (v: number | null | undefined) => v == null ? 'nicht verfügbar' : `${v.toFixed(2)} mm`;
  return <section aria-label="Rindenmessung Ergebnis">
    <h3>Stamm {result.workpiece_id} · Mittelwert {number(result.mean_mm)}</h3>
    <p>{result.metrology_label}</p>
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 20 }}>
      {(['left', 'right'] as const).map(side => {
        const item = result[side];
        const maximum = Math.max(1, ...item.samples.map(s => s.thickness_mm ?? 0));
        return <section key={side}>
          <h4>{side === 'left' ? 'Links' : 'Rechts'} · {item.reference.slice_role === 'first' ? 'erste' : 'letzte'} Scheibe · Position {item.reference.slice.position_mm} mm</h4>
          <p>Mittelwert: <strong>{number(item.mean_mm)}</strong> · gültiger Umfang: {(item.valid_fraction * 100).toFixed(1)} %</p>
          {item.min_mm != null && <p>Min: {number(item.min_mm)} · Max: {number(item.max_mm)}</p>}
          {!!item.reason_codes.length && <p role="status">Keine gültige Messung: {item.reason_codes.map(reasonLabel).join(' ')}</p>}
          <svg viewBox="0 0 410 170" role="img" aria-label={`Rindenstärke ${side === 'left' ? 'links' : 'rechts'} über Umfangswinkel`} style={{ width: '100%' }}>
            <path d="M35 10 V140 H395" fill="none" stroke="currentColor" />
            <text x="2" y="15" fontSize="10">{maximum.toFixed(1)}</text>
            <text x="3" y="35" fontSize="10">mm</text>
            <text x="30" y="158" fontSize="11">0°</text><text x="205" y="158" fontSize="11">180°</text><text x="368" y="158" fontSize="11">360°</text>
            {barkChartRuns(item.samples).map((run, index) => <polyline key={index}
              points={run.map(([angle, thickness]) => `${35 + angle},${140 - thickness / maximum * 120}`).join(' ')}
              fill="none" stroke="#d97706" strokeWidth="2" />)}
          </svg>
          <details><summary>Einzelmessungen und Qualität</summary>
            <p>CSV-Zeile {item.reference.slice.csv_line} · Zuordnung aus {item.reference.mapping_source === 'job' ? 'Auftrag' : 'Pipeline'}</p>
            <p>Konturfehler {number(item.registration_error_mm)} · Stabilität {number(item.registration_stability_mm)} · ursprüngliche Pixelgröße {number(item.effective_source_mm_per_pixel)}</p>
            <div style={{ maxHeight: 240, overflow: 'auto' }}><table><thead><tr><th>Winkel</th><th>Stärke</th><th>Status</th></tr></thead><tbody>
              {item.samples.map(s => <tr key={s.angle_deg}><td>{s.angle_deg.toFixed(1)}°</td><td>{number(s.thickness_mm)}</td><td>{s.valid ? 'gültig' : reasonLabel(s.reason)}</td></tr>)}
            </tbody></table></div>
          </details>
        </section>;
      })}
    </div>
  </section>;
}

export function BarkArtifactViewer({ resultId, artifacts }: { resultId: string; artifacts: unknown }) {
  const items = Array.isArray(artifacts) ? artifacts.map(obj).filter(a => typeof a.name === 'string') : [];
  const [selected, setSelected] = useState('');
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  useEffect(() => { setSelected(''); setError(''); }, [resultId]);
  useEffect(() => {
    let cancelled = false;
    let objectUrl = '';
    setUrl('');
    if (selected) api.runtimeResults.artifact(resultId, selected).then(blob => {
      if (cancelled) return;
      objectUrl = URL.createObjectURL(blob); setUrl(objectUrl);
    }).catch(() => { if (!cancelled) setError('Artefakt konnte nicht geladen werden.'); });
    return () => { cancelled = true; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [resultId, selected]);
  if (!items.length) return null;
  const item = items.find(a => a.name === selected);
  return <details><summary>Gespeicherte Bilder und Messdateien</summary>
    <select aria-label="Gespeichertes Artefakt" value={selected} onChange={e => { setSelected(e.target.value); setError(''); }}>
      <option value="">Artefakt auswählen</option>{items.map(a => <option key={String(a.name)} value={String(a.name)}>{String(a.name)}</option>)}
    </select>
    {error && <p role="alert">{error}</p>}
    {url && <><p><a href={url} download={selected}>Datei herunterladen</a></p>
      {String(item?.mime_type).startsWith('image/') && <img src={url} alt={selected} style={{ maxWidth: '100%', maxHeight: 500 }} />}</>}
  </details>;
}

export function BarkInspectionWorkspace(props: {
  graph: RuntimePipelineGraph; channelId?: string; definitionId: string; versionId: string | null; dirty: boolean;
  onGraphChange: (graph: RuntimePipelineGraph) => void;
  onPreviewFrame: (frame: RuntimeLiveFrame) => void;
}) {
  const split = props.graph.nodes.find(n => n.node_type === 'preprocessing.endface_pair_split');
  const [photo, setPhoto] = useState<RuntimeLiveFrame | null>(null);
  const [csv, setCsv] = useState<{ text: string; name: string } | null>(null);
  const [override, setOverride] = useState('');
  const [regionSide, setRegionSide] = useState<'left' | 'right'>('left');
  const [execution, setExecution] = useState<Json | null>(null);
  const [error, setError] = useState('');
  const [endpointSlug, setEndpointSlug] = useState('');
  const [trainingStepKey, setTrainingStepKey] = useState('');
  const pointerStart = useRef<[number, number] | null>(null);
  const requestGeneration = useRef(0);
  const endpoints = useQuery({ queryKey: ['bark-trigger-endpoints', props.channelId, props.definitionId, props.versionId],
    queryFn: () => api.triggerEndpoints.list({ channelId: props.channelId }), enabled: !!props.channelId });
  const matchingEndpoints = (endpoints.data?.items ?? []).filter(e => e.enabled && e.pipeline_definition_id === props.definitionId
    && e.pipeline_version_id === props.versionId);
  const endpoint = matchingEndpoints.find(e => e.slug === endpointSlug) ?? (matchingEndpoints.length === 1 ? matchingEndpoints[0] : undefined);
  const configuredMapping = effectiveBarkMapping(split?.config.side_mapping, null);
  const mapping = effectiveBarkMapping(configuredMapping, override);
  useEffect(() => { setExecution(null); requestGeneration.current += 1; }, [photo, csv, override, props.graph]);
  const run = useMutation({
    mutationFn: async (preview: boolean | string) => {
      if (!photo || !csv || !mapping || !props.channelId) throw new Error('Foto, CSV, Seitenzuordnung und Kanal werden benötigt.');
      const generation = requestGeneration.current;
      const job: StemBarkJob = { schema_version: 'stem.bark-job.v1', csv_text: csv.text, csv_filename: csv.name,
        side_mapping: override ? mapping : null };
      if (preview) {
        const response = await api.channels.pipelinePreview(props.channelId, { mode: 'trigger', graph: props.graph,
          preview_frame: photo, bark_job: job, include_capture_outputs: false,
          selected_node_key: typeof preview === 'string' ? preview : null });
        return { generation, execution: response.execution_result ?? {}, preview: true };
      }
      if (!endpoint || props.dirty) throw new Error('Die konfigurierte Pipeline zuerst speichern und veröffentlichen.');
      const response = await api.triggerEndpoints.execute(endpoint.slug, {
        image_base64: photo.image_base64, mime_type: photo.mime_type, timeout_ms: endpoint.timeout_ms,
        payload: { bark_job: job }
      });
      return { generation, execution: response.execution_result, preview: false };
    },
    onSuccess: response => { if (response.generation === requestGeneration.current) setExecution(response.execution); },
    onError: e => setError(describeError(e, 'Rindenmessung fehlgeschlagen').description ?? String(e))
  });
  function changeConfig(patch: Json) {
    if (!split) return;
    props.onGraphChange({ ...props.graph, nodes: props.graph.nodes.map(n => n.node_key === split.node_key ? { ...n, config: { ...n.config, ...patch } } : n) });
  }
  async function loadPhoto(file?: File) {
    if (!file) return;
    try {
      if (!['image/png', 'image/jpeg'].includes(file.type) || file.size > 5 * 1024 * 1024) throw new Error('JPEG oder PNG mit höchstens 5 MB auswählen.');
      const data = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = reject; reader.readAsDataURL(file);
      });
      const image = new Image(); image.src = data; await image.decode();
      const frame: RuntimeLiveFrame = { image_base64: data.split(',')[1], mime_type: file.type,
        width: image.naturalWidth, height: image.naturalHeight, sequence: 0, image_context: {}, metadata: {} };
      setPhoto(frame); props.onPreviewFrame(frame); setError('');
    } catch (e) { setPhoto(null); setError(String(e)); }
  }
  async function loadCsv(file?: File) {
    if (!file) return;
    try {
      if (file.size > 1_000_000) throw new Error('CSV-Datei darf höchstens 1 MB groß sein.');
      const text = await file.text();
      if (!text.replace(/^\uFEFF/, '').startsWith('HFA_KRUEMMUNG_V1')) throw new Error('CSV muss das Format HFA_KRUEMMUNG_V1 verwenden.');
      setCsv({ text, name: file.name }); setError('');
    } catch (e) { setCsv(null); setError(String(e)); }
  }
  function point(event: PointerEvent<SVGSVGElement>): [number, number] {
    const bounds = event.currentTarget.getBoundingClientRect();
    return [Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width)), Math.max(0, Math.min(1, (event.clientY - bounds.top) / bounds.height))];
  }
  function finishRegion(event: PointerEvent<SVGSVGElement>) {
    const start = pointerStart.current; pointerStart.current = null;
    if (!start) return;
    const end = point(event);
    const region: Box = { x: Math.min(start[0], end[0]), y: Math.min(start[1], end[1]),
      width: Math.abs(end[0] - start[0]), height: Math.abs(end[1] - start[1]) };
    if (region.width > 0.01 && region.height > 0.01) changeConfig({ [`${regionSide}_region`]: [{ ...region, label: regionSide }] });
  }
  const frames = Object.entries(obj(execution?.node_outputs)).flatMap(([node, outputs]) => Object.entries(obj(outputs)).flatMap(([port, p]) => {
    const f = obj(p); return f.payload_type === 'frame' && typeof f.image_base64 === 'string' ? [{ name: `${node} · ${port}`, frame: f }] : [];
  }));
  const summary = obj(execution?.final_response);
  const csvRows = csv?.text.trim().split(/\r?\n/) ?? [];
  return <div className="panel-stack" style={{ maxHeight: '78vh', overflow: 'auto' }}>
    <fieldset disabled={run.isPending} style={{ border: 0, padding: 0 }}>
      <div className="actions-row">
        <label>Foto mit beiden Stirnflächen <input type="file" accept="image/png,image/jpeg" onChange={e => void loadPhoto(e.target.files?.[0])} /></label>
        <label>Messdatei <input type="file" accept=".csv,text/csv" onChange={e => void loadCsv(e.target.files?.[0])} /></label>
      </div>
      {csv && <p>{csv.name} · Stamm-ID {csvRows[2]?.split(';')[0]} · {Math.max(0, csvRows.length - 6)} Scheiben · erste Position {Number(csvRows[6]?.split(';')[0]) * 10} mm · letzte Position {Number(csvRows.at(-1)?.split(';')[0]) * 10} mm</p>}
      <label>Zuordnung in der Pipeline <select value={configuredMapping ?? ''} onChange={e => changeConfig({ side_mapping: e.target.value })}>
        <option value="">Zuordnung ausdrücklich auswählen</option>{Object.entries(mappingLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <label>Abweichende Zuordnung nur für diesen Auftrag <select value={override} onChange={e => setOverride(e.target.value)}>
        <option value="">Gespeicherte Pipeline-Zuordnung verwenden</option>{Object.entries(mappingLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select></label>
      <p><strong>{mapping ? mappingLabels[mapping] : 'Ohne Zuordnung kann keine Messung ausgeführt werden.'}</strong></p>
      {photo && <details open><summary>Bildbereiche und Ausrichtung</summary>
        <label>Rechteck zeichnen für <select value={regionSide} onChange={e => setRegionSide(e.target.value as 'left' | 'right')}><option value="left">Links</option><option value="right">Rechts</option></select></label>
        <svg viewBox="0 0 1000 1000" preserveAspectRatio="none" style={{ width: '100%', aspectRatio: `${photo.width}/${photo.height}`, touchAction: 'none', maxHeight: 500 }}
          role="img" aria-label="Bildbereiche links und rechts einstellen"
          onPointerDown={e => { pointerStart.current = point(e); e.currentTarget.setPointerCapture(e.pointerId); }} onPointerUp={finishRegion} onPointerCancel={() => { pointerStart.current = null; }}>
          <image href={`data:${photo.mime_type};base64,${photo.image_base64}`} width="1000" height="1000" preserveAspectRatio="none" />
          {(['left', 'right'] as const).map(side => {
            const box = (split?.config[`${side}_region`] as Box[] | undefined)?.[0];
            return box && <rect key={side} x={box.x * 1000} y={box.y * 1000} width={box.width * 1000} height={box.height * 1000} fill="none" stroke={side === 'left' ? '#22c55e' : '#60a5fa'} strokeWidth="4" />;
          })}
        </svg>
        {(['left', 'right'] as const).map(side => <div key={side} className="actions-row"><span>{side === 'left' ? 'Links' : 'Rechts'}</span>
          <label>Drehung <select value={String(split?.config[`${side}_rotation`] ?? '0')} onChange={e => changeConfig({ [`${side}_rotation`]: e.target.value })}>{[0, 90, 180, 270].map(v => <option key={v} value={String(v)}>{v}°</option>)}</select></label>
          <label><input type="checkbox" checked={Boolean(split?.config[`${side}_mirror`])} onChange={e => changeConfig({ [`${side}_mirror`]: e.target.checked })} /> Spiegeln</label>
        </div>)}
      </details>}
      {!props.channelId && <p>Die Vorlage in einem Kanal öffnen, um einen Auftrag auszuführen.</p>}
      {matchingEndpoints.length > 1 && <label>Veröffentlichte Pipeline <select value={endpointSlug} onChange={e => setEndpointSlug(e.target.value)}><option value="">Auswählen</option>{matchingEndpoints.map(e => <option key={e.slug} value={e.slug}>{e.name}</option>)}</select></label>}
      <div className="actions-row">
        <button disabled={!photo || !csv || !mapping || !props.channelId} onClick={() => { setError(''); run.mutate(true); }}>Entwurf prüfen</button>
        <button disabled={!photo || !csv || !mapping || !endpoint || props.dirty} onClick={() => { setError(''); run.mutate(false); }}>Auftrag ausführen und speichern</button>
      </div>
      <details><summary>Trainingsbilder vor einem nachfolgenden Modell vorbereiten</summary>
        <p>Nur bis zur gewählten Stufe rechnen. Für die aufgeteilten Bilder wird noch kein Segmentierungsmodell benötigt; für die Ausrichtung nur Modell 1.</p>
        <label>Vorbereitungsstufe <select value={trainingStepKey || split?.node_key || ''} onChange={event => setTrainingStepKey(event.target.value)}>
          {split && <option value={split.node_key}>Beide Stirnflächen aufteilen · Training Modell 1</option>}
          {props.graph.nodes.filter(node => node.node_type === 'preprocessing.endface_metric_rectify').map(node => <option key={node.node_key} value={node.node_key}>Metrisch ausrichten · {node.node_key} · Training Modell 2</option>)}
        </select></label>
        <button type="button" disabled={!photo || !csv || !mapping || !props.channelId}
          onClick={() => { setError(''); run.mutate(trainingStepKey || split!.node_key); }}>Bis zur Trainingsstufe vorbereiten</button>
      </details>
      {props.dirty && <p>Entwurf geändert. Für gespeicherte Aufträge die Pipeline zuerst speichern und veröffentlichen.</p>}
    </fieldset>
    {run.isPending && <p role="status">Stirnflächen werden ausgewertet …</p>}
    {error && <p role="alert">{error}</p>}
    {Array.isArray(execution?.errors) && execution.errors.map((e, i) => <p role="alert" key={i}>{String(obj(e).message ?? obj(e).code)}</p>)}
    <BarkResultView value={execution} />
    <PipelineTrainingCapture channelId={props.channelId} graph={props.graph} execution={execution}
      pipelineVersionId={execution?.pipeline_version_id === 'preview' ? null : props.versionId} />
    {typeof summary.result_record_id === 'string' && <BarkArtifactViewer resultId={summary.result_record_id} artifacts={summary.artifacts} />}
    {!!frames.length && <details><summary>Zwischenbilder und Messdiagnose</summary><div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: 12 }}>
      {frames.map(({ name, frame }) => <figure key={name}><img src={`data:${frame.mime_type};base64,${frame.image_base64}`} alt={name} style={{ width: '100%' }} /><figcaption>{name}</figcaption></figure>)}
    </div></details>}
  </div>;
}
