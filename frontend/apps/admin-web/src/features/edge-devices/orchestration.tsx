import { useMemo, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import type { EdgeDeviceSummary, EdgeSoftwareDevice, EdgeSoftwareRolloutRequest, EdgeSoftwareStatus } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import { edgeStatusLabel, edgeStatusTone } from './presentation';
import './orchestration.css';

export const softwarePhaseLabels: Record<EdgeSoftwareStatus['phase'], string> = {
  idle: 'Bereit', queued: 'Eingeplant', transferring: 'Paket wird übertragen',
  verifying: 'Signatur und Plattform prüfen', installing: 'Runtime vorbereiten',
  activating: 'Runtime wechseln', checking: 'Start und Funktion prüfen',
  succeeded: 'Update abgeschlossen', failed: 'Update fehlgeschlagen', rolled_back: 'Vorherige Runtime aktiv'
};

export function softwareBusy(device?: EdgeSoftwareDevice): boolean {
  return !!device?.commands.some(command => command.status === 'queued' || command.status === 'running');
}

export function softwareUpdateBlockers(device: EdgeDeviceSummary, state: EdgeSoftwareDevice | undefined, packageId: string): string[] {
  const reasons: string[] = [];
  if (!device.enabled) reasons.push('Verwaltung deaktiviert.');
  if (!['online', 'degraded'].includes(device.connectivity_state)) reasons.push('Gerät zuerst verbinden und Status abrufen.');
  if (softwareBusy(state)) reasons.push('Ein Software-Auftrag läuft bereits.');
  const compatibility = state?.releases.find(item => item.package_sha256 === packageId);
  reasons.push(...(compatibility?.reasons ?? ['Passendes Runtime-Paket auswählen.']));
  return reasons;
}

function shortDigest(value?: string | null) { return value ? value.slice(0, 12) : 'Unbekannt'; }
function platformName(value?: string | null) {
  return ({ jetson_orin_nano: 'Jetson Orin Nano', jetson_agx_orin: 'Jetson AGX Orin', windows_11: 'Windows 11', simulated: 'Simulation' } as Record<string, string>)[value ?? ''] ?? 'Noch nicht erfasst';
}
function dateLabel(value?: string | null) { return value ? new Date(value).toLocaleString('de-AT') : 'Noch kein Kontakt'; }

export function EdgeOrchestration({ devices, detail = false, onReconcile }: { devices: EdgeDeviceSummary[]; detail?: boolean; onReconcile?: (id: string) => void }) {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all');
  const [selected, setSelected] = useState<string[]>([]);
  const [packageId, setPackageId] = useState('');
  const software = useQuery({ queryKey: ['edge-software'], queryFn: () => api.edgeDevices.software(), refetchInterval: 5000 });
  const channels = useQuery({ queryKey: ['runtime-channels', 'fleet'], queryFn: () => api.channels.list({ executionTarget: 'edge_device' }), refetchInterval: 10000 });
  const upload = useMutation({
    mutationFn: (file: File) => api.edgeDevices.uploadSoftware(file),
    onSuccess: data => {
      queryClient.setQueryData(['edge-software'], data);
      notify({ title: 'Runtime-Paket geprüft und importiert', tone: 'success' });
    },
    onError: error => notify({ ...describeError(error, 'Runtime-Paket konnte nicht importiert werden'), tone: 'error' })
  });
  const rollout = useMutation({
    mutationFn: async ({ ids, action }: { ids: string[]; action: 'update' | 'rollback' }) => {
      const outcomes = await Promise.allSettled(ids.map(id => {
        const requestId = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const payload: EdgeSoftwareRolloutRequest = { action, idempotency_key: `software-${requestId}`, package_sha256: action === 'update' ? packageId : undefined };
        return api.edgeDevices.updateSoftware(id, payload);
      }));
      return { successful: outcomes.filter(item => item.status === 'fulfilled').length, failed: outcomes.filter(item => item.status === 'rejected') as PromiseRejectedResult[] };
    },
    onSuccess: ({ successful, failed }) => {
      queryClient.invalidateQueries({ queryKey: ['edge-software'] });
      setSelected([]);
      notify({ title: `${successful} Software-Aufträge eingeplant`,
        description: failed.length ? `${failed.length} abgelehnt: ${describeError(failed[0].reason, 'Fehler').description ?? 'Gerätestatus prüfen'}` : 'Fortschritt und Ergebnis erscheinen direkt beim Gerät.',
        tone: failed.length ? 'error' : 'success' });
    }
  });
  const statuses = new Map(software.data?.devices.map(item => [item.edge_device_id, item]) ?? []);
  const selectedPackage = software.data?.releases.find(item => item.package_sha256 === packageId);
  const visible = useMemo(() => devices.filter(device => {
    const state = software.data?.devices.find(item => item.edge_device_id === device.edge_device_id);
    const matches = `${device.name} ${device.technical_place ?? ''} ${device.address.management_ip}`.toLocaleLowerCase().includes(search.toLocaleLowerCase());
    return matches && (filter === 'all' || (filter === 'updates' ? state?.requires_update : filter === 'busy' ? softwareBusy(state) : device.connectivity_state === filter));
  }), [devices, software.data, search, filter]);
  const reasonsFor = (id: string) => statuses.get(id)?.releases.find(item => item.package_sha256 === packageId)?.reasons ?? ['Passendes Runtime-Paket auswählen.'];
  const canUpdate = (device: EdgeDeviceSummary) => !softwareUpdateBlockers(device, statuses.get(device.edge_device_id), packageId).length;
  const eligible = devices.filter(device => selected.includes(device.edge_device_id) && canUpdate(device));

  return <div className="edge-orchestration">
    {!detail && <div className="edge-metrics" aria-label="Fleet-Übersicht">
      <div><span>Registrierte Geräte</span><strong>{devices.length}</strong><small>{devices.filter(d => d.enabled).length} zur Verwaltung aktiviert</small></div>
      <div><span>Erreichbar</span><strong>{devices.filter(d => ['online', 'degraded'].includes(d.connectivity_state)).length}</strong><small>{devices.filter(d => d.connectivity_state === 'offline').length} offline</small></div>
      <div><span>Runtime abgleichen</span><strong>{software.isPending ? '…' : devices.filter(d => statuses.get(d.edge_device_id)?.requires_update).length}</strong><small>Abweichung vom zentralen Codestand</small></div>
      <div><span>Zugewiesene Kanäle</span><strong>{devices.reduce((count, d) => count + d.assigned_channel_count, 0)}</strong><small>{devices.filter(d => softwareBusy(statuses.get(d.edge_device_id))).length} Software-Aufträge aktiv</small></div>
    </div>}

    <Panel title="Runtime-Releases und Updates">
      <div className="edge-release-toolbar">
        <div><p className="subtle-text">Signiertes Paket importieren, passende Geräte auswählen und zentral aktualisieren. Die Kanalverarbeitung pausiert während des Runtime-Wechsels.</p>
          <small>Zentraler Codestand <code title={software.data?.server_runtime_digest}>{shortDigest(software.data?.server_runtime_digest)}</code></small></div>
        <label className="edge-upload">{upload.isPending ? 'Paket wird geprüft …' : 'Runtime-Paket importieren'}<input aria-label="Signiertes Runtime-Paket importieren" type="file" accept=".zip" disabled={upload.isPending} onChange={event => { const file = event.target.files?.[0]; if (file) upload.mutate(file); event.target.value = ''; }} /></label>
      </div>
      {software.isError && <p role="alert" className="inline-alert inline-alert--warn">{describeError(software.error, 'Softwareverwaltung nicht erreichbar').description ?? 'Softwareverwaltung nicht erreichbar.'} <button onClick={() => software.refetch()}>Erneut laden</button></p>}
      <div className="edge-release-selection">
        <label><span>Freigegebenes Runtime-Paket</span><select value={packageId} onChange={event => setPackageId(event.target.value)}><option value="">Paket auswählen</option>{software.data?.releases.map(item => <option key={item.package_sha256} value={item.package_sha256}>{item.release.release_id} · {platformName(item.release.platform)} · {(item.size_bytes / 1024 ** 2).toFixed(0)} MB</option>)}</select></label>
        {selectedPackage && <div className="edge-package-facts"><span>Python {selectedPackage.release.python_version}</span><span>CUDA {selectedPackage.release.cuda_version ?? '–'}</span><span>TensorRT {selectedPackage.release.tensorrt_version ?? '–'}</span><span>{Object.keys(selectedPackage.release.node_contract_versions).length} Node-Verträge</span></div>}
      </div>
      {!software.isPending && !software.isError && !software.data?.releases.length && <p className="subtle-text">Noch kein Runtime-Paket importiert. Geräteinstallation und Pipeline-Deployments bleiben separat verfügbar.</p>}
    </Panel>

    <Panel title={detail ? 'Softwarestand dieses Geräts' : 'Geräte und Orchestrierung'}>
      {!detail && <div className="edge-filters">
        <input aria-label="Geräte suchen" placeholder="Name, Standort oder IP-Adresse suchen" value={search} onChange={event => setSearch(event.target.value)} />
        <select aria-label="Geräte filtern" value={filter} onChange={event => setFilter(event.target.value)}><option value="all">Alle Geräte</option><option value="online">Online</option><option value="degraded">Eingeschränkt</option><option value="offline">Offline</option><option value="stale">Status veraltet</option><option value="unknown">Unbekannt</option><option value="updates">Runtime abweichend</option><option value="busy">Update läuft</option></select>
        <span>{visible.length} Geräte</span>
      </div>}
      {selected.length > 0 && <div className="edge-bulk"><span>{selected.length} ausgewählt · {eligible.length} für dieses Paket bereit</span><button className="button--primary" disabled={!eligible.length || rollout.isPending} onClick={() => rollout.mutate({ ids: eligible.map(d => d.edge_device_id), action: 'update' })}>Ausgewählte Geräte aktualisieren</button><button onClick={() => setSelected([])}>Auswahl aufheben</button></div>}
      <div className="table-scroll"><table className="edge-fleet-table"><thead><tr><th><span className="sr-only">Auswahl</span></th><th>Gerät / Standort</th><th>Verbindung</th><th>Runtime</th><th>Kanäle</th><th>Software-Auftrag</th><th>Aktionen</th></tr></thead><tbody>
        {visible.map(device => {
          const status = statuses.get(device.edge_device_id);
          const command = status?.commands[0];
          const busy = softwareBusy(status);
          const phase = command?.software.phase ?? (command?.status === 'queued' ? 'queued' : 'idle');
          const blocked = reasonsFor(device.edge_device_id);
          const assigned = channels.data?.items.filter(channel => channel.placement.edge_device_id === device.edge_device_id) ?? [];
          return <tr key={device.edge_device_id}>
            <td><input type="checkbox" aria-label={`${device.name} auswählen`} checked={selected.includes(device.edge_device_id)} disabled={busy} onChange={event => setSelected(current => event.target.checked ? [...current, device.edge_device_id] : current.filter(id => id !== device.edge_device_id))} /></td>
            <td><Link to="/edge-devices/$edgeDeviceId" params={{ edgeDeviceId: device.edge_device_id }}><strong>{device.name}</strong></Link><small>{device.technical_place || 'Standort nicht hinterlegt'}</small><code>{device.address.management_ip}</code><small>{platformName(status?.platform)}</small></td>
            <td><span className={`status-pill status-pill--${edgeStatusTone(device.connectivity_state)}`}>{edgeStatusLabel(device.connectivity_state)}</span><small>{dateLabel(device.last_seen_at)}</small>{!device.enabled && <small>Verwaltung deaktiviert</small>}</td>
            <td><span className={`edge-software-badge ${status?.requires_update ? 'edge-software-badge--warn' : ''}`}>{!status?.runtime_code_digest ? 'Nicht erfasst' : status.requires_update ? 'Abweichender Codestand' : 'Aktuell'}</span><code title={status?.runtime_code_digest ?? ''}>{shortDigest(status?.runtime_code_digest)}</code><small>{status?.node_count ?? '–'} verfügbare Nodes</small>{status?.update_protocol === 0 && <small>Launcher einmalig installieren</small>}</td>
            <td><strong>{device.assigned_channel_count}</strong>{assigned.slice(0, 3).map(channel => <small key={channel.channel_id}>{channel.name}</small>)}{assigned.length > 3 && <small>+ {assigned.length - 3} weitere</small>}</td>
            <td><span role={busy ? 'status' : undefined}>{command?.status === 'failed' ? 'Fehlgeschlagen' : softwarePhaseLabels[phase]}</span>{busy && <progress value={phase === "transferring" ? command?.software.transfer_bytes : undefined} max={command?.software.transfer_total_bytes || 1} aria-label="Runtime-Update läuft" />}<small>{command ? dateLabel(command.created_at) : 'Kein Software-Auftrag'}</small>{command?.software.active_release && <small>Aktiv: {shortDigest(command.software.active_release)}</small>}{command?.last_error && <small className="edge-error">{command.last_error}</small>}</td>
            <td><div className="edge-row-actions">{onReconcile && <button disabled={busy} onClick={() => onReconcile(device.edge_device_id)}>Status abrufen</button>}<button disabled={!canUpdate(device) || rollout.isPending} onClick={() => rollout.mutate({ ids: [device.edge_device_id], action: 'update' })}>Runtime aktualisieren</button><button disabled={busy || !device.enabled || !['online', 'degraded'].includes(device.connectivity_state) || !command?.software.previous_release || rollout.isPending} onClick={() => rollout.mutate({ ids: [device.edge_device_id], action: 'rollback' })}>Software-Rollback</button><Link to="/edge-devices/$edgeDeviceId" params={{ edgeDeviceId: device.edge_device_id }}>Kanäle & Diagnose →</Link></div>{packageId && blocked.map(reason => <small className="edge-compatibility-reason" key={reason}>{reason}</small>)}</td>
          </tr>;
        })}
      </tbody></table></div>
      {!visible.length && <p className="edge-empty">{devices.length ? 'Keine Geräte passen zur Auswahl.' : 'Registrieren Sie ein Edge-Gerät, um Kanäle und Runtime zentral zu verwalten.'}</p>}
    </Panel>
    {detail && software.data?.devices.find(d => d.edge_device_id === devices[0]?.edge_device_id)?.commands.length ? <Panel title="Software-Verlauf"><ol className="edge-history">{software.data.devices.find(d => d.edge_device_id === devices[0]?.edge_device_id)?.commands.map(command => <li key={command.command_id}><strong>{command.action === 'rollback' ? 'Software-Rollback' : 'Runtime-Update'}</strong><span>{softwarePhaseLabels[command.software.phase ?? 'queued']}</span><small>{dateLabel(command.created_at)}</small>{command.last_error && <p className="edge-error">{command.last_error}</p>}</li>)}</ol></Panel> : null}
  </div>;
}
