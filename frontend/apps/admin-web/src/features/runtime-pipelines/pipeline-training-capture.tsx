import { useEffect, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import type { RuntimeLiveFrame, RuntimePipelineGraph } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';

type Json = Record<string, unknown>;
const record = (value: unknown): Json => value && typeof value === 'object' ? value as Json : {};

export function pipelineTrainingImages(graph: RuntimePipelineGraph, execution: unknown) {
  const outputs = record(record(execution).node_outputs);
  return graph.nodes.flatMap(node => Object.entries(record(outputs[node.node_key])).flatMap(([port, value]) => {
    const payload = record(value);
    if (payload.payload_type !== 'frame' || typeof payload.image_base64 !== 'string') return [];
    const metric = node.node_type === 'preprocessing.endface_metric_rectify';
    if (metric && port !== 'frame') return []; // Diagnostic overlays are not model inputs.
    if (node.node_type === 'analysis.bark_radial_thickness') return [];
    const eligibilityValue = record(record(outputs[node.node_key]).eligibility);
    const eligibility = eligibilityValue.result ? record(eligibilityValue.result) : null;
    const context = record(payload.image_context);
    const allowed = !!context.context_id && (!metric || !!eligibility)
      && (!eligibility || eligibility.eligible === true && eligibility.image_context_id === context.context_id);
    const reference = record(record(payload.metadata).endface_reference);
    const side = reference.side === 'left' ? 'Links' : reference.side === 'right' ? 'Rechts' : '';
    const stage = metric ? 'Metrisch ausgerichtet · Modell 2'
      : node.node_type === 'preprocessing.endface_pair_split' ? 'Aufgeteilte Stirnfläche · Modell 1'
        : node.node_key;
    return [{ key: `${node.node_key}.${port}`, nodeKey: node.node_key, port,
      label: `${side ? `${side} · ` : ''}${stage}${side ? '' : ` · ${port}`}`,
      frame: { image_base64: payload.image_base64, mime_type: String(payload.mime_type ?? 'image/png'),
        width: Number(payload.width), height: Number(payload.height), sequence: 0,
        source_timestamp: typeof payload.timestamp === 'string' ? payload.timestamp : null,
        image_context: context, metadata: record(payload.metadata) } as RuntimeLiveFrame,
      eligibility, requireEligibility: metric || !!eligibility, allowed }];
  }));
}

export function PipelineTrainingCapture(props: {
  channelId?: string; graph: RuntimePipelineGraph; execution: unknown; pipelineVersionId?: string | null;
}) {
  const images = pipelineTrainingImages(props.graph, props.execution);
  const [selection, setSelection] = useState('');
  const [targetsByImage, setTargetsByImage] = useState<Record<string, string>>({});
  const [status, setStatus] = useState('');
  const targets = useQuery({ queryKey: ['manual-capture-targets', props.channelId],
    queryFn: () => api.channels.manualCaptureTargets(props.channelId!), enabled: !!props.channelId && images.length > 0 });
  const selected = images.find(image => image.key === selection) ?? images.find(image => image.allowed) ?? images[0];
  const targetId = selected ? targetsByImage[selected.key] ?? '' : '';
  useEffect(() => { setStatus(''); }, [props.execution, selection]);
  const capture = useMutation({
    mutationFn: async () => {
      if (!props.channelId || !selected?.allowed || !targetId) throw new Error('Bild und Trainingsprojekt auswählen.');
      const response = await api.channels.manualCaptureOnce(props.channelId, {
        capture_target_id: targetId, capture_source: 'PIPELINE_IMAGE_OUTPUT', request_predictions: false,
        require_eligibility: selected.requireEligibility,
        pipeline_image: { node_key: selected.nodeKey, output_port: selected.port,
          pipeline_version_id: props.pipelineVersionId ?? null, frame: selected.frame, eligibility: selected.eligibility }
      });
      return { response, label: selected.label };
    },
    onSuccess: ({ response, label }) => setStatus(response.result.status === 'queued'
      ? `${label}: Aufnahme eingereiht. Zustellung unter Aufnahmen / Verlauf verfolgen.`
      : `${label}: ${response.result.status}`),
    onError: error => setStatus(describeError(error, 'Aufnahme fehlgeschlagen').description ?? String(error))
  });
  if (!images.length || !props.channelId) return null;
  return <section aria-label="Trainingsbilder aus Pipeline-Stufen">
    <h3>Trainingsbild für Label Studio aufnehmen</h3>
    <p>Gesendet wird genau das hier gezeigte Zwischenbild. Das Trainingsprojekt pro Ansicht auswählen.</p>
    <fieldset disabled={capture.isPending} style={{ border: 0, padding: 0 }}>
      <label>Pipeline-Stufe <select value={selected?.key ?? ''} onChange={event => setSelection(event.target.value)}>
        {images.map(image => <option key={image.key} value={image.key}>{image.label}{image.allowed ? '' : ' · nicht freigegeben'}</option>)}
      </select></label>
      {selected && <img src={`data:${selected.frame.mime_type};base64,${selected.frame.image_base64}`}
        alt={selected.label} style={{ display: 'block', maxWidth: '100%', maxHeight: 320 }} />}
      {!selected?.allowed && <p role="status">Diese Ansicht ist nicht für die Trainingsaufnahme freigegeben. Zuerst die Ausrichtung erfolgreich prüfen.</p>}
      <label>Label-Studio-Projekt <select value={targetId} onChange={event => selected && setTargetsByImage({ ...targetsByImage, [selected.key]: event.target.value })}>
        <option value="">Trainingsprojekt auswählen</option>
        {(targets.data?.items ?? []).filter(target => target.selectable).map(target => <option key={target.capture_target_id} value={target.capture_target_id}>{target.display_name}</option>)}
      </select></label>
      {targets.isError && <p role="alert">Trainingsprojekte konnten nicht geladen werden.</p>}
      <button type="button" disabled={!selected?.allowed || !targetId || targets.isPending}
        onClick={() => capture.mutate()}>{capture.isPending ? 'Aufnahme wird eingereiht …' : 'Dieses Bild für Label Studio aufnehmen'}</button>
    </fieldset>
    {status && <p role="status">{status}</p>}
  </section>;
}
