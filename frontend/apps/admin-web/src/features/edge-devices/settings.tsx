import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import type { EdgeSettingsCommand, EdgeSettingsStatus } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { useNotifications } from '../../components/notification-provider';

const ipKey = 'YBB_EDGE_PLANT_ALLOWED_IPS';
const fields = [
  ['YBB_EDGE_PLANT_TRIGGER_TIMEOUT_MS', 'Trigger-Zeitlimit (ms)', 100, 120000, 'Maximale Wartezeit für eine Trigger-Antwort.'],
  ['YBB_EDGE_PLANT_MAX_CONCURRENT_TRIGGERS', 'Gleichzeitige Trigger', 1, 32, 'Begrenzt gleichzeitig laufende Trigger-Anfragen.'],
  ['YBB_EDGE_PLANT_RATE_LIMIT_PER_SECOND', 'Trigger pro Sekunde und Client', 1, 1000, 'Weitere Anfragen werden mit HTTP 429 abgewiesen.'],
  ['YBB_EDGE_PLANT_MAX_REQUEST_BYTES', 'Maximale Trigger-Anfrage (Bytes)', 1024, 1048576, 'Obergrenze für den HTTP-Anfrageinhalt.'],
  ['YBB_EDGE_PREVIEW_JPEG_QUALITY', 'Vorschauqualität (%)', 10, 95, 'Höhere Qualität benötigt mehr Bandbreite.'],
  ['YBB_EDGE_PREVIEW_MAX_BYTES', 'Maximales Vorschaubild (Bytes)', 65536, 8388608, 'Obergrenze für ein übertragenes Vorschaubild.'],
  ['YBB_EDGE_ENGINE_BUILD_TIMEOUT_SECONDS', 'TensorRT-Bauzeitlimit (Sekunden)', 60, 7200, 'Gilt beim erstmaligen Aufbau einer fehlenden Engine. Bereits gecachte Engines werden weiterverwendet.']
] as const;

const phases: Record<EdgeSettingsStatus['phase'], string> = {
  idle: 'Aktueller Gerätestand', queued: 'Änderung wartet auf das Gerät', applying: 'Firewall und Einstellungen werden übernommen · Neustart läuft',
  succeeded: 'Vom neu gestarteten Gerät bestätigt', failed: 'Änderung fehlgeschlagen'
};

export function EdgeSettingsForm({ status, saving, onSave, onCancel }: {
  status: EdgeSettingsStatus; saving: boolean; onSave: (values: Record<string, string>) => void; onCancel: () => void;
}) {
  const [values, setValues] = useState(status.values);
  const [error, setError] = useState('');
  return <form className="panel-stack" onSubmit={event => {
    event.preventDefault();
    const ips = (values[ipKey] ?? '').split(/[\s,;]+/).filter(Boolean);
    if (ips.some(ip => !/^(?:\d{1,3}\.){3}\d{1,3}$/.test(ip) || ip.split('.').some(part => Number(part) > 255 || (part.length > 1 && part.startsWith('0'))))) {
      setError('Bitte einzelne IPv4-Adressen eingeben, ohne CIDR, Port oder Hostnamen.'); return;
    }
    setError(''); onSave({ ...values, [ipKey]: [...new Set(ips)].join(',') });
  }}>
    <label><span>Erlaubte HTTP-Trigger-Clients</span>
      <textarea rows={4} value={values[ipKey] ?? ''} placeholder={'192.168.50.40\n192.168.50.41'} disabled={saving}
        onChange={event => setValues({ ...values, [ipKey]: event.target.value })} />
      <small className="subtle-text">IP-Adressen der SPS oder anderer Geräte, die diesen Edge per HTTP triggern dürfen. Eine Adresse pro Zeile oder mit Komma getrennt. Eine leere Liste sperrt alle entfernten Trigger-Clients. Lokale Diagnose bleibt möglich.</small>
    </label>
    <details><summary>Weitere Betriebseinstellungen</summary>
      <div className="channel-form-grid">{fields.map(([key, label, min, max, help]) => <label key={key}>
        <span>{label}</span><input required type="number" min={min} max={max} step="1" value={values[key] ?? ''} disabled={saving}
          onChange={event => setValues({ ...values, [key]: event.target.value })} />
        <small className="subtle-text">{help}</small>
      </label>)}</div>
    </details>
    <p className="inline-alert inline-alert--warn">Beim Übernehmen startet der Edge-Agent neu. Laufende Kanäle und Trigger werden kurz unterbrochen und anschließend wiederhergestellt. Firewall und Zugriffskontrolle werden gemeinsam aktualisiert.</p>
    {error ? <p role="alert" className="inline-alert inline-alert--error">{error}</p> : null}
    <div className="channel-card__actions"><button type="submit" className="button--primary" disabled={saving}>{saving ? 'Wird übermittelt …' : 'Speichern und am Edge anwenden'}</button>
      <button type="button" onClick={onCancel}>Abbrechen</button></div>
  </form>;
}

export function EdgeSettingsPanel({ deviceId, enabled }: { deviceId: string; enabled: boolean }) {
  const [editing, setEditing] = useState<EdgeSettingsStatus | null>(null);
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const queryKey = ['edge-settings', deviceId];
  const query = useQuery({ queryKey, queryFn: () => api.edgeDevices.settings(deviceId), enabled, refetchInterval: 5000, retry: false });
  const mutation = useMutation({
    mutationFn: (command: EdgeSettingsCommand) => api.edgeDevices.updateSettings(deviceId, command),
    onSuccess: result => {
      setEditing(null); queryClient.setQueryData(queryKey, result);
      notify({ title: 'Einstellungen an das Edge-Gerät übermittelt', description: 'Die Bestätigung nach dem Neustart erscheint hier.', tone: 'success' });
    },
    onError: error => notify({ ...describeError(error, 'Einstellungen konnten nicht übermittelt werden'), tone: 'error' })
  });
  const status = query.data;
  const busy = status?.phase === 'queued' || status?.phase === 'applying';
  const editable = !!status?.supported && status.writable && !query.isError && !busy && enabled;
  return <Panel title="Geräteeinstellungen">
    <p className="subtle-text">Diese Werte werden direkt vom Edge gelesen und dort dauerhaft gespeichert. Sie gelten für alle Kanäle dieses Geräts und bleiben bei Software-Updates erhalten.</p>
    {!enabled ? <p>Das Gerät ist deaktiviert. Zum Lesen und Ändern zuerst aktivieren.</p> : null}
    {query.isPending && enabled ? <p>Einstellungen werden vom Gerät gelesen …</p> : null}
    {query.isError ? <p role="alert" className="inline-alert inline-alert--warn">Das Gerät antwortet derzeit nicht. Während eines Neustarts wird die Verbindung automatisch erneut geprüft. Angezeigte Werte sind der letzte gelesene Stand.</p> : null}
    {status?.message ? <p className="inline-alert inline-alert--warn">{status.message}</p> : null}
    {status?.supported ? <>
      <p role="status">{phases[status.phase]}</p>
      {status.last_error ? <p role="alert" className="inline-alert inline-alert--error">{status.last_error}</p> : null}
      {editing ? <EdgeSettingsForm status={editing} saving={mutation.isPending || !editable} onCancel={() => setEditing(null)} onSave={values => mutation.mutate({
        values, expected_revision: editing.revision!,
        request_id: globalThis.crypto?.randomUUID?.() ?? `settings-${Date.now()}-${Math.random().toString(36).slice(2)}`
      })} /> : <>
        <dl className="summary-list"><div><dt>Erlaubte HTTP-Trigger-Clients</dt><dd>{status.values[ipKey]?.split(',').join(', ') || 'Keine entfernten Clients freigegeben'}</dd></div></dl>
        <details><summary>Betriebseinstellungen und Umgebungsvariablen anzeigen</summary>
          <div className="table-scroll"><table><thead><tr><th>Einstellung</th><th>Wert</th><th>Umgebungsvariable</th></tr></thead><tbody>
            <tr><td>Erlaubte HTTP-Trigger-Clients</td><td>{status.values[ipKey] || 'Leer'}</td><td><code>{ipKey}</code></td></tr>
            {fields.map(([key, label]) => <tr key={key}><td>{label}</td><td>{status.values[key]}</td><td><code>{key}</code></td></tr>)}
          </tbody></table></div>
        </details>
        <button type="button" className="button--primary" disabled={!editable} onClick={() => setEditing(status)}>Einstellungen bearbeiten</button>
      </>}
    </> : null}
    <p className="subtle-text">Die Trigger-Client-Liste ist unabhängig vom erlaubten Edge-Zielnetz in der Geräteverwaltung. Verbindungsadressen, Ports und Signierschlüssel bleiben Teil der lokalen Installation.</p>
  </Panel>;
}
