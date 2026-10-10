import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type { EdgeDeviceDetail, EdgeTimingSummary } from '@cv-platform/schemas';
import { api } from '../../app/api';
import './orchestration.css';

const stageLabels: Record<string, string> = {
  acquisition: 'Kamerabild lesen (inkl. Warten)',
  pipeline: 'Pipeline gesamt',
  result_persistence: 'Ergebnis lokal speichern',
  preview: 'Vorschau bereitstellen'
};

export function edgePerformanceIsFresh(device: EdgeDeviceDetail, now = Date.now()) {
  const observed = Date.parse(device.last_seen_at ?? '');
  return device.enabled && device.connectivity_state === 'online' &&
    Number.isFinite(observed) && now - observed >= -5000 && now - observed < 45_000 &&
    device.health?.boot_id === device.health?.runtime?.boot_id &&
    (!device.current_boot_id || device.current_boot_id === device.health?.boot_id);
}

export function EdgePerformancePanel({ device, channelId, channelNames = {}, now = Date.now() }: {
  device: EdgeDeviceDetail;
  channelId?: string;
  channelNames?: Record<string, string>;
  now?: number;
}) {
  const fresh = edgePerformanceIsFresh(device, now);
  const processes = (device.health?.runtime?.processes ?? []).filter(
    process => (!channelId || process.channel_id === channelId) && process.state !== 'stopped'
  );
  return <Panel title="Edge-Leistung">
    <p className="subtle-text">Messwerte vom Gerät, unabhängig von der Vorschau. FPS über die letzten 10 Sekunden;
      Laufzeiten aus bis zu 256 Messungen je Schritt in diesem Zeitraum.</p>
    <p className="subtle-text">{fresh ? 'Letzte Geräteabfrage' : 'Kein aktueller Gerätestatus'}: {device.last_seen_at ? new Date(device.last_seen_at).toLocaleString('de-DE') : '–'}.
      {' '}Der automatische Geräteabgleich aktualisiert die Werte normalerweise alle 15 Sekunden.</p>
    {processes.length ? <div className="edge-performance-list">{processes.map(process => {
      const metrics = process.metrics;
      const current = fresh && process.state === 'running' && Boolean(metrics);
      const pipeline = metrics?.stages.find(stage => stage.key === 'pipeline');
      return <section key={process.deployment_id} className="edge-performance-channel">
        <div className="edge-release-toolbar"><h3>{process.channel_id ? <Link to="/channels/$channelId" params={{ channelId: process.channel_id }}>{channelNames[process.channel_id] ?? process.channel_id}</Link> : process.deployment_id}</h3>
          <span>{process.state === 'running' ? 'Läuft' : process.state === 'starting' ? 'Startet' : process.state === 'backoff' ? 'Neustart wird vorbereitet' : 'Fehler'} · {process.restart_count} Neustarts</span></div>
        {process.last_error && <p className="inline-alert inline-alert--error">{process.last_error}</p>}
        {metrics ? <>
          <div className="edge-metrics">
            <div><small>Verarbeitet</small><strong>{current ? `${metrics.processed_fps.toFixed(1)} FPS` : '–'}</strong><small>{metrics.processed_frames.toLocaleString('de-DE')} Frames seit Kanalstart</small></div>
            <div><small>Von der Kamera geliefert</small><strong>{current ? `${metrics.source_fps.toFixed(1)} FPS` : '–'}</strong><small>{metrics.acquired_frames.toLocaleString('de-DE')} gelesene Frames</small></div>
            <div><small>Pipeline · P95</small><strong>{current && pipeline ? `${pipeline.p95_ms.toFixed(1)} ms` : '–'}</strong><small>{metrics.sampled_out_frames} durch Sampling ausgelassen</small></div>
            <div><small>Ausführungsfehler</small><strong>{metrics.execution_errors}</strong><small>{current ? 'Aktuelle Messung' : 'Letzter bekannter Stand'}</small></div>
          </div>
          {current ? <details className="edge-performance-details"><summary>Verarbeitungszeiten je Schritt</summary>
            <TimingTable items={metrics.stages} label="Verarbeitungsphasen" />
            <TimingTable items={metrics.nodes} label="Pipeline-Schritte" />
            <small>Zeiten können sich überlappen. Übersprungene Schritte sind gekennzeichnet; Kamerazeit enthält das Warten auf ein Bild.</small>
          </details> : <p className="subtle-text">FPS und Laufzeiten werden erst mit einem aktuellen, laufenden Kanal angezeigt.</p>}
        </> : <EmptyState title="Noch keine Leistungsmessung" body="Nach dem Edge-Runtime-Update und Kanalstart erscheinen hier FPS und Verarbeitungszeiten." />}
        <details className="edge-performance-details"><summary>TensorRT-Engines</summary>
          {process.engines?.length ? <div className="table-scroll"><table><thead><tr><th>Modellschritt</th><th>Letzte Vorbereitung</th><th>Dauer</th><th>Größe</th></tr></thead><tbody>
            {process.engines.map(engine => <tr key={engine.node_key}><td>{engine.node_key}</td><td>{engine.cache_hit ? 'Aus persistentem Cache geladen' : engine.reason === 'invalid_cache' ? 'Neu gebaut: Cache beschädigt' : 'Neu gebaut: passender Cache fehlt'}</td><td>{engine.duration_ms.toFixed(1)} ms</td><td>{(engine.engine_size_bytes / 1024 ** 2).toFixed(1)} MiB</td></tr>)}
          </tbody></table></div> : <p className="subtle-text">Keine Engine-Meldung verfügbar. Der Kanal startet noch, verwendet kein TensorRT-Modell oder benötigt das Edge-Runtime-Update.</p>}
          <p className="subtle-text">Passende Engines werden bei Neustarts wiederverwendet. Änderungen an Modell, Eingabeform, Präzision oder Plattform können einen Neubau erfordern.</p>
        </details>
      </section>;
    })}</div> : <EmptyState title="Kein aktiver Edge-Kanal gemeldet" body="Einen Kanal starten und den Gerätestatus aktualisieren. Ältere Edge-Versionen liefern noch keine Leistungsmessung." />}
  </Panel>;
}

function TimingTable({ items, label }: { items: EdgeTimingSummary[]; label: string }) {
  return <div className="table-scroll"><table aria-label={label}><thead><tr><th>{label}</th><th>Mittel</th><th>P95</th><th>Maximum</th><th>Zuletzt</th><th>Messungen</th></tr></thead><tbody>
    {items.map(item => <tr key={item.key}><td>{stageLabels[item.key] ?? item.key}{item.node_type && <small className="edge-performance-node-type">{item.node_type}</small>}{item.status === 'skipped' && <small>Übersprungen</small>}{item.status === 'failed' && <small>Fehlgeschlagen</small>}</td><td>{item.mean_ms.toFixed(2)} ms</td><td>{item.p95_ms.toFixed(2)} ms</td><td>{item.max_ms.toFixed(2)} ms</td><td>{item.last_ms.toFixed(2)} ms</td><td>{item.sample_count}</td></tr>)}
    {!items.length && <tr><td colSpan={6}>Keine Messung im aktuellen Zeitraum.</td></tr>}
  </tbody></table></div>;
}

export function ChannelEdgePerformance({ edgeDeviceId, channelId }: { edgeDeviceId: string; channelId: string }) {
  const device = useQuery({ queryKey: ['edge-device', edgeDeviceId], queryFn: () => api.edgeDevices.detail(edgeDeviceId), refetchInterval: 5000 });
  if (device.isError) return <Panel title="Edge-Leistung"><p className="inline-alert inline-alert--error">Der Edge-Status konnte nicht geladen werden.</p></Panel>;
  return device.data ? <EdgePerformancePanel device={device.data} channelId={channelId} /> : null;
}
