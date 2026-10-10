import { Tabs, Modal } from '../../components/workspace-ui';
import { useState } from 'react';
import { EdgeSettingsPanel } from './settings';
import { useNavigate, useParams } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type {
  EdgeDeploymentPreflightReport,
  EdgeDeviceRegistrationRequest,
  EdgeDeviceSummary,
  EdgeDeviceUpdateRequest,
  RuntimeChannelSummary
} from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';
import { EdgeOrchestration } from './orchestration';
import { EdgePerformancePanel } from './performance';
import { edgeDiagnosticTone, edgeStatusLabel } from './presentation';

const initialForm = {
  name: '',
  technicalPlace: '',
  edgeDeviceIp: '',
  orchestrationServerIp: '',
  allowedCidr: '',
  controlPort: '9443',
  plantApiPort: '9444',
  rtspPort: '8554'
};

export function EdgeDeviceListPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState(initialForm);
  const devicesQuery = useQuery({
    queryKey: ['edge-devices'],
    queryFn: () =>
      api.edgeDevices.list(),
    refetchInterval: 10_000
  });
  const resultSyncQuery = useQuery({
    queryKey: ['edge-result-sync'],
    queryFn: () => api.edgeDevices.resultSyncStatus(),
    refetchInterval: 5000
  });
  const fleetHealthQuery = useQuery({
    queryKey: ['edge-fleet-health'],
    queryFn: () => api.edgeDevices.fleetHealth(),
    refetchInterval: 15_000
  });
  const registerMutation = useMutation({
    mutationFn: (payload: EdgeDeviceRegistrationRequest) => api.edgeDevices.register(payload),
    onSuccess: (device) => {
      setForm(initialForm);
      setFormOpen(false);
      queryClient.invalidateQueries({ queryKey: ['edge-devices'] });
      notify({ title: 'Edge-Gerät registriert', description: device.name, tone: 'success' });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Edge-Gerät konnte nicht registriert werden'), tone: 'error' })
  });
  const reconcileMutation = useMutation({
    mutationFn: (deviceId: string) =>
      api.edgeDevices.reconcile(deviceId, reconciliationKey(deviceId)),
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['edge-devices'] });
      notify({
        title: response.command.status === 'succeeded' ? 'Gerätestatus aktualisiert' : 'Gerät nicht erreichbar',
        description: response.command.last_error ?? response.device.name,
        tone: response.command.status === 'succeeded' ? 'success' : 'error'
      });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Geräteabgleich fehlgeschlagen'), tone: 'error' })
  });

  const devices = devicesQuery.data?.items ?? [];
  return (
    <div className="panel-stack panel-stack--wide">
      <section className="channel-page-header">
        <div>
          <p className="training-hero__eyebrow">Fleet</p>
          <h1>Edge-Orchestrierung</h1>
          <p className="subtle-text">
            Geräte über ihre feste IPv4-Adresse verwalten und ihren zuletzt bekannten Zustand prüfen.
          </p>
        </div>
        <button
          type="button"
          className={formOpen ? undefined : 'button--primary'}
          onClick={() => setFormOpen((current) => !current)}
        >
          {formOpen ? 'Abbrechen' : 'Gerät registrieren'}
        </button>
      </section>

      {formOpen ? (
        <Panel title="Edge-Gerät registrieren">
          <form
            className="channel-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              registerMutation.mutate(buildRegistration(form));
            }}
          >
            <label>
              <span>Name</span>
              <input required value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} />
            </label>
            <label>
              <span>Technischer Ort</span>
              <input value={form.technicalPlace} onChange={(event) => setForm({ ...form, technicalPlace: event.target.value })} />
            </label>
            <label>
              <span>Edge-Geräte-IP (feste IPv4)</span>
              <input required placeholder="192.168.50.21" value={form.edgeDeviceIp} onChange={(event) => setForm({ ...form, edgeDeviceIp: event.target.value })} />
              <small className="subtle-text">IP-Adresse dieses Edge-Geräts im Anlagen-VLAN.</small>
            </label>
            <label>
              <span>Orchestrierungsserver-IP (feste IPv4)</span>
              <input required placeholder="192.168.50.10" value={form.orchestrationServerIp} onChange={(event) => setForm({ ...form, orchestrationServerIp: event.target.value })} />
              <small className="subtle-text">IP-Adresse des zentralen Servers, der dieses Gerät verwalten darf.</small>
            </label>
            <label>
              <span>Erlaubtes Edge-Zielnetz (CIDR)</span>
              <input required placeholder="192.168.50.21/32" value={form.allowedCidr} onChange={(event) => setForm({ ...form, allowedCidr: event.target.value })} />
              <small className="subtle-text">Muss die Edge-Geräte-IP enthalten. Bei einem einzelnen Gerät kann dessen IP mit /32 eingetragen werden; die Server-IP darf in einem anderen Netz liegen.</small>
            </label>
            <label>
              <span>Edge-Control-Port (Server → Edge)</span>
              <input type="number" min="1" max="65535" value={form.controlPort} onChange={(event) => setForm({ ...form, controlPort: event.target.value })} />
            </label>
            <label>
              <span>HTTP-Trigger-Port am Edge (SPS → Edge)</span>
              <input type="number" min="1" max="65535" value={form.plantApiPort} onChange={(event) => setForm({ ...form, plantApiPort: event.target.value })} />
              <small className="subtle-text">Zielport des LHTTP-Aufrufs auf dem Edge-Gerät; keine SPS-Zieladresse.</small>
            </label>
            <label>
              <span>RTSP-Port</span>
              <input type="number" min="1" max="65535" value={form.rtspPort} onChange={(event) => setForm({ ...form, rtspPort: event.target.value })} />
            </label>
            <div className="channel-form-grid__wide channel-card__actions">
              <button type="submit" className="button--primary" disabled={registerMutation.isPending}>
                Registrieren
              </button>
            </div>
          </form>
        </Panel>
      ) : null}

      {devicesQuery.isError ? <p role="alert" className="inline-alert inline-alert--warn">Ger?te konnten nicht geladen werden. <button onClick={() => devicesQuery.refetch()}>Erneut laden</button></p> : null}
      {devicesQuery.isLoading ? <p role="status">Ger?te werden geladen ?</p> : <EdgeOrchestration devices={devices} onReconcile={id => reconcileMutation.mutate(id)} />}
      <details>
        <summary>Ergebnis?bertragung und Betriebsdiagnose</summary>
        <Panel title="Ergebnis- und Live-Synchronisation">
          {resultSyncQuery.isError ? <p role="alert">Synchronisationsstatus nicht verf?gbar.</p> : <dl className="summary-list">
            <div><dt>Worker</dt><dd>{resultSyncQuery.data ? resultSyncQuery.data.running ? 'Aktiv' : 'Gestoppt' : 'Wird geladen ?'}</dd></div>
            <div><dt>Persistierte Ergebnisse</dt><dd>{resultSyncQuery.data?.persisted_results ?? '?'}</dd></div>
            <div><dt>Best?tigte Ergebnisse</dt><dd>{resultSyncQuery.data?.acknowledged_results ?? '?'}</dd></div>
          </dl>}
          {resultSyncQuery.data?.last_error ? <p role="alert">{resultSyncQuery.data.last_error}</p> : null}
        </Panel>
        <Panel title="Fleet-Warnungen">
          {fleetHealthQuery.isError ? <p role="alert">Fleet-Diagnose nicht verf?gbar.</p> : fleetHealthQuery.data?.alerts.length ? <ul>{fleetHealthQuery.data.alerts.map(alert => <li key={alert.alert_id}><strong>{alert.title}</strong> ? {alert.detail}</li>)}</ul> : <p>{fleetHealthQuery.isPending ? 'Diagnose wird geladen ?' : 'Keine aktiven Fleet-Warnungen.'}</p>}
        </Panel>
      </details>
    </div>
  );
}

export function EdgeDeviceDetailPage() {
  const [workspaceTab, setWorkspaceTab] = useState('Betrieb');
  const { edgeDeviceId } = useParams({ from: '/authenticated/edge-devices/$edgeDeviceId' });
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [editForm, setEditForm] = useState<typeof initialForm | null>(null);
  const [selectedChannelId, setSelectedChannelId] = useState('');
  const [precision, setPrecision] = useState<'fp32' | 'fp16' | 'int8'>('fp16');
  const [preflight, setPreflight] = useState<EdgeDeploymentPreflightReport | null>(null);
  const deviceQuery = useQuery({
    queryKey: ['edge-device', edgeDeviceId],
    queryFn: () => api.edgeDevices.detail(edgeDeviceId),
    refetchInterval: 10_000
  });
  const releasesQuery = useQuery({
    queryKey: ['edge-workload-releases', edgeDeviceId],
    queryFn: () => api.edgeDevices.workloadReleases({ edgeDeviceId }),
    refetchInterval: 10_000
  });
  const deploymentsQuery = useQuery({
    queryKey: ['edge-deployments', edgeDeviceId],
    queryFn: () => api.edgeDevices.deployments({ edgeDeviceId }),
    refetchInterval: 5_000
  });
  const resultSyncQuery = useQuery({
    queryKey: ['edge-result-sync'],
    queryFn: () => api.edgeDevices.resultSyncStatus(),
    refetchInterval: 5000
  });
  const channelsQuery = useQuery({
    queryKey: ['runtime-channels', 'edge-device', edgeDeviceId],
    queryFn: () => api.channels.list({ executionTarget: 'edge_device' }),
    refetchInterval: 10_000
  });
  const diagnosticQuery = useQuery({
    queryKey: ['edge-diagnostics', edgeDeviceId],
    queryFn: () => api.edgeDevices.latestDiagnostics(edgeDeviceId),
    refetchInterval: 15_000
  });
  const reconcileMutation = useMutation({
    mutationFn: () => api.edgeDevices.reconcile(edgeDeviceId, reconciliationKey(edgeDeviceId)),
    onSuccess: (response) => {
      queryClient.setQueryData(['edge-device', edgeDeviceId], response.device);
      queryClient.invalidateQueries({ queryKey: ['edge-devices'] });
      notify({
        title: response.command.status === 'succeeded' ? 'Status aktualisiert' : 'Gerät offline',
        description: response.command.last_error ?? response.device.name,
        tone: response.command.status === 'succeeded' ? 'success' : 'error'
      });
    },
    onError: (error) => notify({ ...describeError(error, 'Statusabfrage fehlgeschlagen'), tone: 'error' })
  });
  const updateMutation = useMutation({
    mutationFn: (payload: EdgeDeviceUpdateRequest) => api.edgeDevices.update(edgeDeviceId, payload),
    onSuccess: (device) => {
      setEditForm(null);
      queryClient.setQueryData(['edge-device', edgeDeviceId], device);
      queryClient.invalidateQueries({ queryKey: ['edge-devices'] });
      notify({ title: 'Edge-Gerät aktualisiert', description: device.name, tone: 'success' });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Edge-Gerät konnte nicht aktualisiert werden'), tone: 'error' })
  });
  const archiveMutation = useMutation({
    mutationFn: () => api.edgeDevices.archive(edgeDeviceId),
    onSuccess: async (archived) => {
      queryClient.removeQueries({ queryKey: ['edge-device', edgeDeviceId] });
      queryClient.invalidateQueries({ queryKey: ['edge-devices'] });
      notify({ title: 'Edge-Gerät archiviert', description: archived.name, tone: 'success' });
      await navigate({ to: '/edge-devices' });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Edge-Gerät konnte nicht archiviert werden'), tone: 'error' })
  });
  const preflightMutation = useMutation({
    mutationFn: (payload: { channelId: string; precision: 'fp32' | 'fp16' | 'int8' }) =>
      api.edgeDevices.preflightDeployment({
        channel_id: payload.channelId,
        precision: payload.precision
      }),
    onSuccess: (report) => {
      setPreflight(report);
      notify({
        title: report.ready ? 'Preflight erfolgreich' : 'Preflight blockiert',
        description: `${report.artifact_count} Artefakte, ${formatBytes(report.total_artifact_bytes)}`,
        tone: report.ready ? 'success' : 'error'
      });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Deployment-Preflight fehlgeschlagen'), tone: 'error' })
  });
  const deployMutation = useMutation({
    mutationFn: (report: EdgeDeploymentPreflightReport) =>
      api.edgeDevices.executeDeployment({
        channel_id: report.channel_id,
        workload_release_id: report.workload_release_id ?? '',
        idempotency_key: operatorKey('deploy', report.channel_id),
        reason: 'Edge Deployment Wizard'
      }),
    onSuccess: (response) => {
      setPreflight(null);
      queryClient.invalidateQueries({ queryKey: ['edge-deployments', edgeDeviceId] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      notify({
        title: 'Deployment angenommen',
        description: `${response.command} als Kommando ${response.command_id}`,
        tone: 'success'
      });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Deployment konnte nicht gestartet werden'), tone: 'error' })
  });
  const diagnosticMutation = useMutation({
    mutationFn: () => api.edgeDevices.runDiagnostics(edgeDeviceId, true),
    onSuccess: (report) => {
      queryClient.setQueryData(['edge-diagnostics', edgeDeviceId], report);
      queryClient.invalidateQueries({ queryKey: ['edge-fleet-health'] });
      queryClient.invalidateQueries({ queryKey: ['edge-device', edgeDeviceId] });
      notify({
        title: 'Diagnose abgeschlossen',
        description: `Gesamtstatus: ${report.status}`,
        tone: report.status === 'critical' ? 'error' : 'success'
      });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Gerätediagnose fehlgeschlagen'), tone: 'error' })
  });
  const rollbackMutation = useMutation({
    mutationFn: (deploymentId: string) =>
      api.edgeDevices.rollbackDeployment(deploymentId, {
        idempotency_key: operatorKey('rollback', deploymentId),
        reason: 'Manual operator rollback'
      }),
    onSuccess: (response) => {
      queryClient.invalidateQueries({ queryKey: ['edge-deployments', edgeDeviceId] });
      queryClient.invalidateQueries({ queryKey: ['runtime-channels'] });
      queryClient.invalidateQueries({ queryKey: ['edge-device', edgeDeviceId] });
      notify({
        title: 'Rollback abgeschlossen',
        description: `Deployment ${response.restored_deployment.edge_deployment_id} wiederhergestellt`,
        tone: 'success'
      });
    },
    onError: (error) =>
      notify({ ...describeError(error, 'Rollback fehlgeschlagen'), tone: 'error' })
  });
  const device = deviceQuery.data;
  if (!device) {
    return <div className="screen-center">Edge-Gerät wird geladen...</div>;
  }
  const capability = device.latest_capability_snapshot?.capabilities;
  const runtime = device.health?.runtime;
  const assignedChannels = (channelsQuery.data?.items ?? []).filter(
    (channel: RuntimeChannelSummary) => channel.placement.edge_device_id === edgeDeviceId
  );
  const diagnostic = diagnosticQuery.data;
  return (
    <div className="panel-stack panel-stack--wide edge-workspace">
      <section className="channel-page-header">
        <div>
          <p className="training-hero__eyebrow">{device.address.management_ip}</p>
          <h1>{device.name}</h1>
          <span className="traffic-status traffic-status--success">Edge-Ziel · feste IP</span>
          <p className="subtle-text">{device.technical_place || 'Kein technischer Ort hinterlegt'}</p>
        </div>
        <div className="channel-card__actions">
          <button type="button" className="button--primary" disabled={reconcileMutation.isPending || !device.enabled} onClick={() => reconcileMutation.mutate()}>
            Status abrufen
          </button>
          <button
            type="button"
            onClick={() => { setWorkspaceTab('Betrieb'); setEditForm(editForm ? null : editFormFromDevice(device)); }}
          >
            {editForm ? 'Bearbeiten abbrechen' : 'Bearbeiten'}
          </button>
          <button type="button" disabled={updateMutation.isPending} onClick={() => updateMutation.mutate({ enabled: !device.enabled })}>
            {device.enabled ? 'Deaktivieren' : 'Aktivieren'}
          </button>
          <button
            type="button"
            disabled={archiveMutation.isPending || device.assigned_channel_count > 0}
            onClick={() => {
              if (window.confirm(`Edge-Gerät "${device.name}" wirklich archivieren?`)) {
                archiveMutation.mutate();
              }
            }}
          >
            Archivieren
          </button>
        </div>
      </section>
      <Tabs tabs={['Betrieb', 'Deployments', 'Diagnose', 'Einstellungen']} value={workspaceTab} onChange={setWorkspaceTab}/>
      {workspaceTab === 'Einstellungen' ? <EdgeSettingsPanel deviceId={edgeDeviceId} enabled={device.enabled} /> : null}
      <div className="workspace-tab-body" hidden={workspaceTab !== 'Betrieb'}>
      <EdgePerformancePanel device={device} channelNames={Object.fromEntries(assignedChannels.map(channel => [channel.channel_id, channel.name]))} />
      <EdgeOrchestration devices={[device]} detail onReconcile={() => reconcileMutation.mutate()} />
      {editForm ? (
        <Modal title="Edge-Gerät bearbeiten" onClose={() => setEditForm(null)} wide><Panel title="Verbindung">
          <form
            className="channel-form-grid"
            onSubmit={(event) => {
              event.preventDefault();
              updateMutation.mutate(buildUpdate(editForm));
            }}
          >
            <label>
              <span>Name</span>
              <input required value={editForm.name} onChange={(event) => setEditForm({ ...editForm, name: event.target.value })} />
            </label>
            <label>
              <span>Technischer Ort</span>
              <input value={editForm.technicalPlace} onChange={(event) => setEditForm({ ...editForm, technicalPlace: event.target.value })} />
            </label>
            <label>
              <span>Edge-Geräte-IP (unveränderlich)</span>
              <input disabled value={editForm.edgeDeviceIp} />
              <small className="subtle-text">Feste IP-Adresse dieses Edge-Geräts im Anlagen-VLAN.</small>
            </label>
            <label>
              <span>Orchestrierungsserver-IP (feste IPv4)</span>
              <input required value={editForm.orchestrationServerIp} onChange={(event) => setEditForm({ ...editForm, orchestrationServerIp: event.target.value })} />
              <small className="subtle-text">Zentraler Server, der das Edge-Gerät verwaltet und Kanäle ausrollt.</small>
            </label>
            <label>
              <span>Erlaubtes Edge-Zielnetz (CIDR)</span>
              <input required value={editForm.allowedCidr} onChange={(event) => setEditForm({ ...editForm, allowedCidr: event.target.value })} />
              <small className="subtle-text">Muss die Edge-Geräte-IP enthalten. Die Server-IP darf über ein Gateway in einem anderen Netz liegen.</small>
            </label>
            <label>
              <span>Edge-Control-Port (Server → Edge)</span>
              <input type="number" min="1" max="65535" value={editForm.controlPort} onChange={(event) => setEditForm({ ...editForm, controlPort: event.target.value })} />
            </label>
            <label>
              <span>HTTP-Trigger-Port am Edge (SPS → Edge)</span>
              <input type="number" min="1" max="65535" value={editForm.plantApiPort} onChange={(event) => setEditForm({ ...editForm, plantApiPort: event.target.value })} />
              <small className="subtle-text">Zielport des SPS-LHTTP-Aufrufs auf dem Edge-Gerät.</small>
            </label>
            <label>
              <span>RTSP-Port</span>
              <input type="number" min="1" max="65535" value={editForm.rtspPort} onChange={(event) => setEditForm({ ...editForm, rtspPort: event.target.value })} />
            </label>
            <div className="channel-form-grid__wide channel-card__actions">
              <button type="submit" className="button--primary" disabled={updateMutation.isPending}>
                Änderungen speichern
              </button>
            </div>
          </form>
        </Panel></Modal>
      ) : null}
      {device.assigned_channel_count > 0 ? (
        <p className="inline-alert inline-alert--warn">
          Das Gerät kann erst archiviert werden, wenn alle Kanäle einem anderen Ziel zugewiesen sind.
        </p>
      ) : null}
      <Panel title="Fleet-Status">
        <dl className="summary-list">
          <div><dt>Erreichbarkeit</dt><dd>{edgeStatusLabel(device.connectivity_state)}</dd></div>
          <div><dt>Gemeldete Gesundheit</dt><dd>{device.reported_health_state ?? 'Unbekannt'}</dd></div>
          <div><dt>Boot-ID</dt><dd>{device.current_boot_id ?? 'Noch nicht gemeldet'}</dd></div>
          <div><dt>Identitätsgeneration</dt><dd>{device.identity_generation}</dd></div>
          <div><dt>Edge-Geräte-IP</dt><dd>{device.address.management_ip}</dd></div>
          <div><dt>Erlaubtes Edge-Zielnetz (CIDR)</dt><dd>{device.allowed_cidr}</dd></div>
          <div><dt>Orchestrierungsserver-IP</dt><dd>{device.server_ip}</dd></div>
          <div><dt>SPS-Trigger-URL</dt><dd>http://{device.address.management_ip}:{device.address.plant_api_port}/edge-plant/v1/channels/&lt;kanal-id&gt;/trigger</dd></div>
          <div><dt>Zugewiesene Kanäle</dt><dd>{device.assigned_channel_count}</dd></div>
          <div><dt>Letzter Kontakt</dt><dd>{formatTimestamp(device.last_seen_at)}</dd></div>
        </dl>
        {device.last_connectivity_error ? <p className="inline-alert inline-alert--error">{device.last_connectivity_error}</p> : null}
      </Panel>
      </div><div className="workspace-tab-body" hidden={workspaceTab !== 'Diagnose'}>
      <Panel title="Diagnose und Kanal-SLOs">
        <div className="channel-card__actions">
          <button
            type="button"
            className="button--primary"
            disabled={diagnosticMutation.isPending}
            onClick={() => diagnosticMutation.mutate()}
          >
            Diagnose aktualisieren
          </button>
          {diagnostic ? <span className={`traffic-status traffic-status--${edgeDiagnosticTone(diagnostic.status)}`}>{diagnostic.status}</span> : null}
        </div>
        {diagnostic ? (
          <>
            <p className="subtle-text">Stand {formatTimestamp(diagnostic.generated_at)} · Bericht <code>{diagnostic.diagnostic_report_id}</code></p>
            <div className="table-scroll"><table><thead><tr><th>Prüfung</th><th>Ergebnis</th><th>Details</th><th>Maßnahme</th></tr></thead><tbody>
              {diagnostic.checks.map((check) => <tr key={check.key}>
                <td>{check.key}</td><td>{check.outcome}</td><td>{check.message}</td><td>{check.remediation_hint ?? '-'}</td>
              </tr>)}
            </tbody></table></div>
            {diagnostic.channels.length ? <div className="table-scroll"><table><thead><tr><th>Kanal</th><th>Status</th><th>P95</th><th>SLO</th><th>Result-Lag</th></tr></thead><tbody>
              {diagnostic.channels.map((channel) => <tr key={channel.channel_id}>
                <td>{channel.channel_name}</td><td>{channel.status}</td><td>{channel.latency_p95_ms == null ? '-' : `${channel.latency_p95_ms.toFixed(1)} ms`}</td><td>{channel.latency_slo_ms.toFixed(0)} ms</td><td>{channel.result_lag}</td>
              </tr>)}
            </tbody></table></div> : null}
          </>
        ) : <EmptyState title="Noch kein Diagnosebericht" body="Starten Sie eine Diagnose, um Netzwerk, Runtime, Speicher, Ergebnis-Replay und Kanal-SLOs zu prüfen." />}
      </Panel>
      <Panel title="Capabilities">
        {capability ? (
          <dl className="summary-list">
            <div><dt>Plattform</dt><dd>{capability.platform}</dd></div>
            <div><dt>Architektur</dt><dd>{capability.architecture}</dd></div>
            <div><dt>JetPack / L4T</dt><dd>{capability.jetpack_version ?? '-'} / {capability.l4t_version ?? '-'}</dd></div>
            <div><dt>CUDA / TensorRT</dt><dd>{capability.cuda_version ?? '-'} / {capability.tensorrt_version ?? '-'}</dd></div>
            <div><dt>GPU</dt><dd>{capability.gpu_model ?? '-'}</dd></div>
            <div><dt>Runtime</dt><dd>{capability.runtime_version}</dd></div>
          </dl>
        ) : <EmptyState title="Noch keine Capabilities" body="Führen Sie die erste Statusabfrage aus." />}
      </Panel>
      <Panel title="Edge Runtime M2">
        {runtime ? (
          <>
            <dl className="summary-list">
              <div><dt>Supervisor</dt><dd>{runtime.supervisor_state}</dd></div>
              <div><dt>Aktiver / vorheriger Slot</dt><dd>{runtime.active_slot ?? '-'} / {runtime.previous_slot ?? '-'}</dd></div>
              <div><dt>Persistenz</dt><dd>SQLite {runtime.database_journal_mode.toUpperCase()}</dd></div>
              <div><dt>Outbox</dt><dd>{runtime.storage.outbox_items} / {runtime.storage.outbox_max_items} Ergebnisse</dd></div>
              <div><dt>Artefakte</dt><dd>{formatBytes(runtime.storage.artifact_bytes)} / {formatBytes(runtime.storage.artifact_quota_bytes)}</dd></div>
              <div><dt>Freier Speicher</dt><dd>{formatBytes(runtime.storage.disk_free_bytes)}</dd></div>
            </dl>
            {runtime.storage.pressure ? <p className="inline-alert inline-alert--warn">Eine Edge-Speichergrenze ist erreicht.</p> : null}
            <div className="table-scroll"><table><thead><tr><th>Slot</th><th>Status</th><th>Deployment</th></tr></thead><tbody>
              {runtime.slots.map((slot) => <tr key={slot.name}><td>{slot.name}</td><td>{slot.state}</td><td>{slot.deployment_id ?? '-'}</td></tr>)}
            </tbody></table></div>
          </>
        ) : <EmptyState title="Noch kein M2-Runtime-Status" body="Der Status erscheint nach der nächsten automatischen oder manuellen Reconciliation." />}
      </Panel>
      </div><div className="workspace-tab-body" hidden={workspaceTab !== 'Deployments'}>      <Panel title="Deployment-Assistent">
        {assignedChannels.length ? (
          <div className="panel-stack">
            <div className="channel-form-grid">
              <label>
                <span>Kanal auf diesem Gerät</span>
                <select
                  value={selectedChannelId}
                  onChange={(event) => {
                    setSelectedChannelId(event.target.value);
                    setPreflight(null);
                  }}
                >
                  <option value="">Kanal auswählen</option>
                  {assignedChannels.map((channel) => (
                    <option key={channel.channel_id} value={channel.channel_id}>
                      {channel.name} · {channel.status}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>TensorRT-Präzision</span>
                <select
                  value={precision}
                  onChange={(event) => {
                    setPrecision(event.target.value as 'fp32' | 'fp16' | 'int8');
                    setPreflight(null);
                  }}
                >
                  <option value="fp16">FP16</option>
                  <option value="int8">INT8</option>
                  <option value="fp32">FP32</option>
                </select>
              </label>
            </div>
            <div className="channel-card__actions">
              <button
                type="button"
                disabled={!selectedChannelId || preflightMutation.isPending}
                onClick={() => preflightMutation.mutate({ channelId: selectedChannelId, precision })}
              >
                Preflight ausführen
              </button>
              <button
                type="button"
                className="button--primary"
                disabled={!preflight?.ready || !preflight.workload_release_id || deployMutation.isPending}
                onClick={() => preflight && deployMutation.mutate(preflight)}
              >
                Geprüftes Release ausrollen
              </button>
            </div>
            {preflight ? (
              <>
                {preflight.checks.some(check => check.key === 'admission.runtime.code' && check.outcome === 'fail') && (
                  <div className="inline-alert inline-alert--warn" role="alert">
                    <strong>Edge-Runtime zuerst aktualisieren.</strong>
                    <p>Der Codestand des Geräts unterscheidet sich von der Zentrale. Im Bereich Betrieb das passende signierte Runtime-Paket importieren und aktualisieren. Danach den Gerätestatus abrufen und diesen Preflight erneut ausführen.</p>
                    <button type="button" onClick={() => setWorkspaceTab('Betrieb')}>Zum Runtime-Update</button>
                  </div>
                )}
                <p className={preflight.ready ? 'inline-alert inline-alert--success' : 'inline-alert inline-alert--danger'}>
                  {preflight.ready ? 'Deployment ist freigegeben.' : 'Deployment ist durch fehlgeschlagene Prüfungen blockiert.'}
                  {' '}{preflight.artifact_count} Artefakte ({formatBytes(preflight.total_artifact_bytes)}).
                </p>
                <div className="table-scroll"><table><thead><tr><th>Prüfung</th><th>Ergebnis</th><th>Details</th><th>Maßnahme</th></tr></thead><tbody>
                  {preflight.checks.map((check) => <tr key={check.key}>
                    <td>{check.key}</td><td>{check.outcome}</td><td>{check.message}</td><td>{check.remediation_hint ?? '-'}</td>
                  </tr>)}
                </tbody></table></div>
              </>
            ) : <p className="subtle-text">Preflight erzeugt ein unveränderliches Release und prüft Platzierung, Capabilities, Erreichbarkeit und Workflow-Konflikte.</p>}
          </div>
        ) : <EmptyState title="Keine zugewiesenen Kanäle" body="Weisen Sie in der Kanalverwaltung zuerst einen Kanal diesem Edge-Gerät zu." />}
      </Panel>

      <Panel title="Workload Releases und Deployments M3/M5">
        {(deploymentsQuery.data?.items.length ?? 0) > 0 ? (
          <div className="table-scroll"><table><thead><tr><th>Deployment</th><th>Release</th><th>Status</th><th>Fehler</th><th>Fortschritt</th><th>Zeit</th><th>Aktion</th></tr></thead><tbody>
            {deploymentsQuery.data?.items.map((deployment) => <tr key={deployment.edge_deployment_id}>
              <td><code>{deployment.edge_deployment_id}</code></td>
              <td><code>{deployment.workload_release_id}</code></td>
              <td>{deployment.status}{deployment.remote_status ? ` / ${deployment.remote_status}` : ''}</td>
              <td>{deployment.last_error ? <span className="edge-error">{deployment.last_error}</span> : '-'}</td>
              <td>{String(deployment.progress.artifacts_uploaded ?? 0)} / {String(deployment.progress.artifacts_total ?? 0)} Artefakte</td>
              <td>{formatTimestamp(deployment.updated_at)}</td>
              <td>
                {deployment.status === 'running' && deployment.previous_edge_deployment_id ? (
                  <button
                    type="button"
                    disabled={rollbackMutation.isPending}
                    onClick={() => {
                      if (window.confirm(`Deployment ${deployment.edge_deployment_id} zurückrollen und den vorherigen A/B-Slot wiederherstellen?`)) {
                        rollbackMutation.mutate(deployment.edge_deployment_id);
                      }
                    }}
                  >
                    Rollback
                  </button>
                ) : '-'}
              </td>
            </tr>)}
          </tbody></table></div>
        ) : <EmptyState title="Noch keine M3-Deployments" body="Der Start eines zugewiesenen Edge-Kanals erzeugt und aktiviert das erste Release." />}
        {(releasesQuery.data?.items.length ?? 0) > 0 ? (
          <div className="table-scroll"><table><thead><tr><th>Release</th><th>Admission</th><th>Artefakte</th><th>Größe</th></tr></thead><tbody>
            {releasesQuery.data?.items.map((release) => <tr key={release.workload_release_id}>
              <td><code>{release.workload_release_id}</code></td><td>{release.status}</td><td>{release.artifact_count}</td><td>{formatBytes(release.total_artifact_bytes)}</td>
            </tr>)}
          </tbody></table></div>
        ) : null}
      </Panel>
      </div><div className="workspace-tab-body" hidden={workspaceTab !== 'Diagnose'}><Panel title="Letzte Fleet-Kommandos">
        {device.recent_commands.length ? (
          <div className="table-scroll"><table><thead><tr><th>Typ</th><th>Status</th><th>Versuche</th><th>Zeit</th></tr></thead><tbody>
            {device.recent_commands.map((command) => <tr key={command.command_id}><td>{command.command_type}</td><td>{command.status}</td><td>{command.attempt_count}</td><td>{formatTimestamp(command.finished_at ?? command.created_at)}</td></tr>)}
          </tbody></table></div>
        ) : <EmptyState title="Keine Kommandos" body="Für dieses Gerät wurde noch kein Fleet-Kommando ausgeführt." />}
      </Panel>
      <Panel title="Result-Replay-Cursor">
        {(() => {
          const sync = resultSyncQuery.data?.devices.find((item) => item.edge_device_id === edgeDeviceId);
          return sync ? (
            <p className={sync.last_error ? 'inline-alert inline-alert--warn' : 'subtle-text'}>
              Sync: {sync.persisted_results} persistiert, {sync.acknowledged_results} bestÃ¤tigt,
              {' '}{sync.preview_frames} Livebilder{sync.last_error ? ` â€“ ${sync.last_error}` : ''}
            </p>
          ) : null;
        })()}
        {device.result_cursors.length ? device.result_cursors.map((cursor) => (
          <p key={cursor.boot_id}><code>{cursor.boot_id}</code>: persistiert {cursor.last_persisted_sequence}, bestätigt {cursor.last_acknowledged_sequence}</p>
        )) : <p className="subtle-text">Noch keine Ergebnis-Sequenz synchronisiert.</p>}
      </Panel>
      </div>
    </div>
  );
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let scaled = value;
  let unit = -1;
  do {
    scaled /= 1024;
    unit += 1;
  } while (scaled >= 1024 && unit < units.length - 1);
  return `${scaled.toFixed(1)} ${units[unit]}`;
}

function buildRegistration(form: typeof initialForm): EdgeDeviceRegistrationRequest {
  return {
    name: form.name.trim(),
    technical_place: form.technicalPlace.trim() || null,
    address: {
      management_ip: form.edgeDeviceIp.trim(),
      control_port: Number(form.controlPort),
      plant_api_port: Number(form.plantApiPort),
      rtsp_port: Number(form.rtspPort)
    },
    allowed_cidr: form.allowedCidr.trim(),
    server_ip: form.orchestrationServerIp.trim(),
    enabled: true
  };
}

function buildUpdate(form: typeof initialForm): EdgeDeviceUpdateRequest {
  return {
    name: form.name.trim(),
    technical_place: form.technicalPlace.trim() || null,
    server_ip: form.orchestrationServerIp.trim(),
    allowed_cidr: form.allowedCidr.trim(),
    control_port: Number(form.controlPort),
    plant_api_port: Number(form.plantApiPort),
    rtsp_port: Number(form.rtspPort)
  };
}

function editFormFromDevice(device: EdgeDeviceSummary): typeof initialForm {
  return {
    name: device.name,
    technicalPlace: device.technical_place ?? '',
    edgeDeviceIp: device.address.management_ip,
    orchestrationServerIp: device.server_ip,
    allowedCidr: device.allowed_cidr,
    controlPort: String(device.address.control_port),
    plantApiPort: String(device.address.plant_api_port),
    rtspPort: String(device.address.rtsp_port)
  };
}

function reconciliationKey(deviceId: string): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : String(Date.now());
  return `fleet-reconcile-${deviceId}-${random}`;
}

function operatorKey(action: string, targetId: string): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : String(Date.now());
  return `m5-${action}-${targetId}-${random}`;
}

function formatTimestamp(value?: string | null): string {
  return value ? new Date(value).toLocaleString() : 'Noch nie';
}
