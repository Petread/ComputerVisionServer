import { useEffect, useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { RuntimeResultRecordDetail, RuntimeResultRecordSummary } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { Empty, Icon, Search } from '../../components/workspace-ui';
import { StemOrientationResultSummary, StemOrientationReviewPanel, stemOrientationResult } from '../results/result-history-page';

const pageSize = 30;
type Status = 'all' | 'completed' | 'failed' | 'timeout';
const statusNames = { completed: 'Abgeschlossen', failed: 'Fehler', timeout: 'Zeitüberschreitung' };
const typeNames: Record<string, string> = {realtime_frame: 'Live-Ausführung', trigger_execution: 'Trigger-Ausführung', failed_execution: 'Fehlgeschlagen', pipeline_execution: 'Pipeline-Ergebnis', stem_orientation: 'Stammausrichtung', stem_rotation_optimization: 'Stammoptimierung', timeout: 'Zeitüberschreitung'};
const time = (value: string) => new Date(value).toLocaleString('de-AT', {day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit'});

export function ChannelResultsWorkspace({channelId, historyEnabled, onConfigure}: {channelId: string; historyEnabled: boolean; onConfigure: () => void}) {
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<Status>('all');
  const [period, setPeriod] = useState('all');
  const [from, setFrom] = useState<string>();
  const [offset, setOffset] = useState(0);
  const [refresh, setRefresh] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    if (search.trim() === query) return;
    const timer = window.setTimeout(() => {setQuery(search.trim()); setOffset(0); setSelected(null);}, 300);
    return () => window.clearTimeout(timer);
  }, [search, query]);
  const params = useMemo(() => ({channelId, status: status === 'all' ? undefined : status, completedFrom: from, search: query || undefined, offset, limit: pageSize}), [channelId, status, from, query, offset]);
  const results = useQuery({queryKey:['runtime-results', 'channel-workspace', params], queryFn:() => api.runtimeResults.list(params), refetchInterval: refresh && offset === 0 && !selected ? 5000 : false});
  const items = results.data?.items ?? [];
  const selectedId = selected ?? items[0]?.result_record_id;
  const detail = useQuery({queryKey:['runtime-result-detail', selectedId], queryFn:() => api.runtimeResults.detail(selectedId!), enabled:Boolean(selectedId), staleTime:30000});
  const selectPage = (next: number) => {setOffset(next); setSelected(null);};
  const total = results.data?.total ?? 0;
  const selectedDetail = detail.data?.item.channel_id === channelId ? detail.data.item : null;
  return <div className="channel-results-workspace">
    <div className="ws-toolbar"><Search value={search} onChange={setSearch} placeholder="In Kanalergebnissen suchen …"/>
      <select aria-label="Ergebnisstatus" value={status} onChange={event => {setStatus(event.target.value as Status); selectPage(0);}}><option value="all">Alle Zustände</option>{Object.entries(statusNames).map(([value,label]) => <option key={value} value={value}>{label}</option>)}</select>
      <select aria-label="Ergebniszeitraum" value={period} onChange={event => {const value=event.target.value; setPeriod(value); setFrom(value==='all' ? undefined : new Date(Date.now()-Number(value)*3600000).toISOString()); selectPage(0);}}><option value="all">Gesamter Verlauf</option><option value="24">Letzte 24 Stunden</option><option value="168">Letzte 7 Tage</option></select>
      <span className="ws-spacer"/><button aria-pressed={refresh} onClick={() => setRefresh(value => !value)}><Icon name={refresh ? 'pause' : 'play'} size={14}/>{refresh ? 'Automatisch' : 'Pausiert'}</button><button onClick={() => {setOffset(0); setSelected(null); if(offset===0) void results.refetch();}} disabled={results.isFetching}><Icon name="refresh" size={14}/>Neueste</button>
    </div>
    {!historyEnabled && <div className="channel-results-notice"><span>Die laufende Ergebnisspeicherung ist deaktiviert. Bereits gespeicherte Ergebnisse bleiben hier sichtbar.</span><button onClick={onConfigure}>Speicherung konfigurieren</button></div>}
    {results.isError && <div className="channel-results-notice" role="alert">Ergebnisse konnten nicht aktualisiert werden. Vorhandene Einträge können veraltet sein.<button onClick={() => void results.refetch()}>Erneut laden</button></div>}
    <div className="channel-results-body">
      <section className="channel-result-timeline" aria-label="Gespeicherte Kanalergebnisse"><div className="ws-inspector-title">Ausführungen <small>{selected ? 'Auswahl fixiert' : offset ? 'Ältere Ergebnisse' : 'Neueste zuerst'}</small></div><div className="ws-scroll">
        {items.map(item => <button key={item.result_record_id} className={`channel-result-entry ${selectedId === item.result_record_id ? 'is-selected' : ''}`} aria-pressed={selectedId === item.result_record_id} onClick={() => setSelected(item.result_record_id)}><time dateTime={item.completed_at}>{time(item.completed_at)}</time><ResultStatus status={item.status}/><strong>{typeNames[item.result_type] ?? item.result_type}</strong><small>{resultSummary(item)}</small></button>)}
        {!items.length && !results.isError && <Empty title={results.isPending ? 'Ergebnisse werden geladen …' : 'Keine gespeicherten Ergebnisse'}>{query || status !== 'all' || period !== 'all' ? 'Suche oder Zeitraum anpassen.' : 'Hier erscheinen gespeicherte Ausführungen dieses Kanals. Die Ausgabe in die Ergebnishistorie wird in der Pipeline konfiguriert.'}</Empty>}
      </div><footer className="ws-statusline"><button aria-label="Neuere Ergebnisse" disabled={offset===0 || results.isFetching} onClick={() => selectPage(Math.max(0,offset-pageSize))}>←</button><span>{total ? `${offset+1}–${offset+items.length} von ${total}` : '0 Ergebnisse'}</span><button aria-label="Ältere Ergebnisse" disabled={!results.data || offset+items.length>=total || results.isFetching} onClick={() => selectPage(offset+pageSize)}>→</button></footer></section>
      <section className="channel-result-inspection ws-scroll" aria-label="Ausgewähltes Kanalergebnis">
        {detail.isError ? <div className="channel-results-notice" role="alert">Ergebnisdetails konnten nicht geladen werden.<button onClick={() => void detail.refetch()}>Erneut laden</button></div> : <ChannelResultInspection key={selectedId} result={selectedDetail} loading={Boolean(selectedId) && detail.isPending}/>}
      </section>
    </div>
    <footer className="ws-statusline"><span>Gespeicherte Ausführungen dieses Kanals</span><span>{selected ? 'Ausgewähltes Ergebnis bleibt beim Prüfen fixiert' : refresh && offset===0 ? 'Aktualisierung alle 5 Sekunden' : 'Automatische Aktualisierung pausiert'}</span></footer>
  </div>;
}

function ResultStatus({status}: {status: RuntimeResultRecordSummary['status']}) {
  return <span className={`ws-badge ws-badge--${status==='completed' ? 'ok' : 'bad'}`}><i/>{statusNames[status]}</span>;
}

export function resultSummary(result: RuntimeResultRecordSummary) {
  // Domain decisions use their existing, confidence-aware renderer in the detail pane.
  if (stemOrientationResult(result)) return 'Fachliche Auswertung verfügbar';
  const fields = Object.entries(result.trigger_response).length ? result.trigger_response : result.derived_preview;
  return Object.entries(fields).slice(0,3).map(([key,value]) => `${key}: ${typeof value === 'object' && value !== null ? 'Details' : String(value ?? '—')}`).join(' · ') || `${result.inference_output_count} Modellausgaben · ${result.derived_output_count} berechnete Ausgaben`;
}

export function ChannelResultInspection({result,loading}: {result: RuntimeResultRecordDetail | null; loading?: boolean}) {
  if (!result) return <Empty title={loading ? 'Ergebnis wird geladen …' : 'Ausführung auswählen'}>Zeitpunkt links auswählen, um Messwerte und Ausgaben zu prüfen.</Empty>;
  const stem = stemOrientationResult(result);
  return <div className="channel-result-content"><header><div><small>{typeNames[result.result_type] ?? result.result_type}</small><h2>{time(result.completed_at)}</h2></div><ResultStatus status={result.status}/></header>
    <dl className="channel-result-context"><div><dt>Ausführung</dt><dd>{result.mode==='trigger' ? 'Trigger' : 'Live'}</dd></div><div><dt>Laufzeit</dt><dd>{typeof result.timing.total_ms==='number' ? `${result.timing.total_ms.toLocaleString('de-AT')} ms` : 'Nicht erfasst'}</dd></div><div><dt>Pipeline-Version</dt><dd>{result.pipeline_version_id ?? 'Nicht erfasst'}</dd></div></dl>
    {result.status!=='completed' && <p role="status" className="channel-results-notice">{result.status==='timeout' ? 'Die Ausführung hat das Zeitlimit überschritten.' : 'Die Ausführung ist fehlgeschlagen.'} Vorhandene Ausgaben können unvollständig sein. Fehlerursachen stehen in den Ausführungsdaten.</p>}
    {stem ? <><StemOrientationResultSummary result={stem}/><details className="channel-result-section"><summary>Fachliche Bewertung</summary><StemOrientationReviewPanel resultRecordId={result.result_record_id} technicalOnly={stem.schema_version==='stem.orientation.v3'}/></details></> : <>
      <ResultValues title="Ergebniswerte" values={result.trigger_response}/><ResultValues title="Berechnete Werte" values={result.derived_preview}/>
      {!Object.keys(result.trigger_response).length && !Object.keys(result.derived_preview).length && <p className="ws-help">Diese Ausführung enthält keine zusammengefassten Messwerte. Weitere Ausgaben stehen unten bereit.</p>}
    </>}
    <details className="channel-result-section"><summary>Modellausgaben · {result.normalized_inference_outputs.length}</summary><pre className="ws-code">{JSON.stringify(result.normalized_inference_outputs,null,2)}</pre></details>
    <details className="channel-result-section"><summary>Berechnete Ausgaben · {result.derived_outputs.length}</summary><pre className="ws-code">{JSON.stringify(result.derived_outputs,null,2)}</pre></details>
    <details className="channel-result-section"><summary>Ausführungsdaten & Rohdaten</summary><dl className="channel-result-context"><div><dt>Korrelation</dt><dd>{result.correlation_id}</dd></div><div><dt>Ergebnis-ID</dt><dd>{result.result_record_id}</dd></div></dl><pre className="ws-code">{JSON.stringify({result:result.result,trigger_response:result.trigger_response,derived_preview:result.derived_preview,overlay_primitives:result.overlay_primitives,raw_inference_outputs:result.raw_inference_outputs,runtime_metadata:result.runtime_metadata,execution_result:result.execution_result,timing:result.timing,model_routes:result.model_routes},null,2)}</pre></details>
  </div>;
}
function ResultValues({title,values}: {title:string; values: Record<string,unknown>}) {
  if (!Object.keys(values).length) return null;
  return <section className="channel-result-section"><h3>{title}</h3><dl className="channel-result-values">{Object.entries(values).map(([key,value]) => <div key={key}><dt>{key.replaceAll('_',' ')}</dt><dd>{value!==null && typeof value==='object' ? <details><summary>{Array.isArray(value) ? `${value.length} Einträge` : 'Struktur anzeigen'}</summary><pre className="ws-code">{JSON.stringify(value,null,2)}</pre></details> : String(value ?? '—')}</dd></div>)}</dl></section>;
}
