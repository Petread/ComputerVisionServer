import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type {
  HealthState,
  OfflineUpdateBundleSummary,
  OfflineUpdatePlanResponse,
  OperationWorkerSummary
} from '@cv-platform/schemas';
import { api } from '../../app/api';

export function WorkersUpdatesPage() {
  const queryClient = useQueryClient();
  const [scheduledFor, setScheduledFor] = useState(() => defaultScheduleValue());

  const statusQuery = useQuery({
    queryKey: ['system-status'],
    queryFn: api.admin.systemStatus,
    refetchInterval: 10000
  });
  const workersQuery = useQuery({
    queryKey: ['operation-workers'],
    queryFn: api.admin.operationWorkers,
    refetchInterval: 5000
  });
  const updatePlanQuery = useQuery({
    queryKey: ['offline-update-plan'],
    queryFn: api.admin.offlineUpdatePlan,
    refetchInterval: 5000
  });

  const restartMutation = useMutation({
    mutationFn: api.admin.restartOperationWorker,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['operation-workers'] });
      void queryClient.invalidateQueries({ queryKey: ['system-status'] });
    }
  });
  const applyMutation = useMutation({
    mutationFn: api.admin.applyOfflineUpdate,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['offline-update-plan'] });
      void queryClient.invalidateQueries({ queryKey: ['operation-workers'] });
      void queryClient.invalidateQueries({ queryKey: ['system-status'] });
    }
  });
  const scheduleMutation = useMutation({
    mutationFn: api.admin.scheduleOfflineUpdate,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['offline-update-plan'] });
    }
  });

  const workers = workersQuery.data?.items ?? [];
  const status = statusQuery.data;
  const plan = updatePlanQuery.data;
  const selectedBundle = plan?.selected_bundle ?? null;
  const canApply =
    Boolean(plan?.enabled) &&
    Boolean(plan?.apply_available) &&
    Boolean(plan?.apply_token) &&
    Boolean(selectedBundle) &&
    !plan?.blocked &&
    !applyMutation.isPending;
  const canSchedule = canApply && Boolean(scheduledFor) && !scheduleMutation.isPending;
  const workerCounts = useMemo(() => summarizeWorkers(workers), [workers]);

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Operations / Workers & Updates</p>
          <h2>Prozesse prüfen, Fehler sehen und Offline-Updates einspielen.</h2>
          <p className="subtle-text">
            Die Ansicht zeigt nur aktuell verwaltete Dienste. Update-ZIP-Dateien werden aus dem
            Incoming-Ordner erkannt.
          </p>
        </div>
        <div className="training-summary-pills">
          <Metric label="Prozesse" value={String(workers.length)} />
          <Metric label="Laufen" value={String(workerCounts.ok)} />
          <Metric label="Warnung" value={String(workerCounts.degraded)} />
          <Metric label="Fehler" value={String(workerCounts.down)} />
          <Metric label="Queue Items" value={String(status?.queues.reduce((sum, item) => sum + item.depth, 0) ?? 0)} />
          <Metric label="Updates" value={String(plan?.incoming_bundles.length ?? 0)} />
        </div>
      </section>

      <Panel title="Worker und Dienste">
        {!workersQuery.data ? (
          <EmptyState title="Lade Prozesse" body="Der aktuelle Systemd-Status wird gelesen." />
        ) : (
          <div className="training-grid training-grid--details">
            <ManagedWorkersTable
              workers={workers}
              restartingService={restartMutation.variables}
              restartPending={restartMutation.isPending}
              onRestart={(serviceName) => restartMutation.mutate(serviceName)}
            />

            <table className="data-table">
              <thead>
                <tr>
                  <th>Queue</th>
                  <th>Offen</th>
                  <th>Ältester Job</th>
                </tr>
              </thead>
              <tbody>
                {(status?.queues ?? []).map((queue) => (
                  <tr key={queue.queue_name}>
                    <td>{queue.queue_name}</td>
                    <td>{queue.depth}</td>
                    <td>{queue.oldest_job_age_seconds}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {restartMutation.error ? (
          <div className="inline-alert inline-alert--danger">{String(restartMutation.error)}</div>
        ) : null}
      </Panel>

      <Panel title="Offline Update">
        {!plan ? (
          <EmptyState title="Lade Incoming-Ordner" body="Die ZIP-Dateien im Update-Eingang werden geprüft." />
        ) : (
          <div className="panel-stack">
            <UpdatePosture plan={plan} />

            {plan.warnings.length ? (
              <div className="inline-alert inline-alert--warn">{plan.warnings.join(' ')}</div>
            ) : null}
            {plan.blockers.length ? (
              <div className="inline-alert inline-alert--danger">{plan.blockers.join(' ')}</div>
            ) : null}
            {applyMutation.data ? (
              <div className={`inline-alert inline-alert--${resultTone(applyMutation.data.status)}`}>
                {applyMutation.data.message}
              </div>
            ) : null}
            {scheduleMutation.data ? (
              <div className={`inline-alert inline-alert--${resultTone(scheduleMutation.data.status)}`}>
                {scheduleMutation.data.message}
              </div>
            ) : null}
            {applyMutation.error ? (
              <div className="inline-alert inline-alert--danger">{String(applyMutation.error)}</div>
            ) : null}
            {scheduleMutation.error ? (
              <div className="inline-alert inline-alert--danger">{String(scheduleMutation.error)}</div>
            ) : null}

            <IncomingBundlesTable bundles={plan.incoming_bundles} selectedBundle={selectedBundle} />

            <div className="actions-row">
              <button
                type="button"
                className="button--primary"
                disabled={!canApply}
                onClick={() =>
                  plan.apply_token &&
                  selectedBundle &&
                  applyMutation.mutate({
                    apply_token: plan.apply_token,
                    service_names: selectedBundle.affected_services,
                    reason: 'operator-approved-offline-update'
                  })
                }
              >
                Sofort einspielen
              </button>
              <label className="filter-field">
                <span>Geplanter Start</span>
                <input
                  type="datetime-local"
                  value={scheduledFor}
                  onChange={(event) => setScheduledFor(event.target.value)}
                />
              </label>
              <button
                type="button"
                disabled={!canSchedule}
                onClick={() =>
                  plan.apply_token &&
                  scheduleMutation.mutate({
                    apply_token: plan.apply_token,
                    scheduled_for: new Date(scheduledFor).toISOString(),
                    reason: 'operator-scheduled-offline-update'
                  })
                }
              >
                Update planen
              </button>
            </div>
          </div>
        )}
      </Panel>
    </div>
  );
}

function ManagedWorkersTable(props: {
  workers: OperationWorkerSummary[];
  restartingService?: string;
  restartPending: boolean;
  onRestart: (serviceName: string) => void;
}) {
  if (!props.workers.length) {
    return <EmptyState title="Keine verwalteten Prozesse" body="Es wurden keine Systemd-Dienste gefunden." />;
  }
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Prozess</th>
          <th>Zustand</th>
          <th>Detail / Fehler</th>
          <th>PID</th>
          <th>Aktion</th>
        </tr>
      </thead>
      <tbody>
        {props.workers.map((worker) => (
          <tr key={worker.service_name}>
            <td>
              <strong>{worker.display_name}</strong>
              <div className="subtle-text">{worker.unit_name}</div>
            </td>
            <td>
              <span className={`badge badge--${tone(worker.state)}`}>{stateLabel(worker.state)}</span>
              <div className="subtle-text">{worker.status}</div>
            </td>
            <td>
              {worker.last_error ?? worker.detail}
              {worker.restart_count ? <div className="subtle-text">Restarts: {worker.restart_count}</div> : null}
            </td>
            <td>{worker.main_pid ?? 'n/a'}</td>
            <td>
              <button
                type="button"
                disabled={
                  !worker.restartable ||
                  (props.restartPending && props.restartingService === worker.service_name)
                }
                onClick={() => props.onRestart(worker.service_name)}
              >
                Restart
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function UpdatePosture(props: { plan: OfflineUpdatePlanResponse }) {
  const { plan } = props;
  const selectedBundle = plan.selected_bundle;
  return (
    <div className="detail-grid">
      <Metric label="Incoming" value={plan.incoming_dir ?? 'nicht gefunden'} />
      <Metric label="Erkannt" value={String(plan.incoming_bundles.length)} />
      <Metric label="Nächstes Update" value={selectedBundle?.file_name ?? 'kein ZIP'} />
      <Metric label="Version" value={selectedBundle?.app_version ?? 'n/a'} />
      <Metric label="Typ" value={selectedBundle?.bundle_type ?? 'n/a'} />
      <Metric label="Status" value={plan.blocked ? 'blockiert' : selectedBundle ? 'bereit' : 'wartet'} />
    </div>
  );
}

function IncomingBundlesTable(props: {
  bundles: OfflineUpdateBundleSummary[];
  selectedBundle: OfflineUpdateBundleSummary | null;
}) {
  if (!props.bundles.length) {
    return (
      <EmptyState
        title="Kein Update im Incoming-Ordner"
        body="Sobald ein Windows-Update-ZIP dort liegt, wird es hier automatisch angezeigt."
      />
    );
  }
  return (
    <table className="data-table">
      <thead>
        <tr>
          <th>Bundle</th>
          <th>Version</th>
          <th>Geändert</th>
          <th>Größe</th>
          <th>Betroffene Prozesse</th>
        </tr>
      </thead>
      <tbody>
        {props.bundles.map((bundle) => (
          <tr key={bundle.apply_token}>
            <td>
              <strong>{bundle.file_name}</strong>
              {props.selectedBundle?.apply_token === bundle.apply_token ? (
                <div className="subtle-text">wird als nächstes eingespielt</div>
              ) : null}
            </td>
            <td>{bundle.app_version ?? bundle.bundle_type}</td>
            <td>{formatDate(bundle.modified_at)}</td>
            <td>{formatBytes(bundle.size_bytes)}</td>
            <td>{shortList(bundle.affected_services)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Metric(props: { label: string; value: string }) {
  return (
    <div className="metric-pill">
      <span>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

function summarizeWorkers(workers: OperationWorkerSummary[]) {
  return workers.reduce(
    (summary, worker) => ({
      ok: summary.ok + (worker.state === 'ok' ? 1 : 0),
      degraded: summary.degraded + (worker.state === 'degraded' ? 1 : 0),
      down: summary.down + (worker.state === 'down' ? 1 : 0)
    }),
    { ok: 0, degraded: 0, down: 0 }
  );
}

function tone(state: HealthState) {
  if (state === 'ok') {
    return 'success';
  }
  if (state === 'degraded') {
    return 'warn';
  }
  return 'danger';
}

function stateLabel(state: HealthState) {
  if (state === 'ok') {
    return 'läuft';
  }
  if (state === 'degraded') {
    return 'warnung';
  }
  return 'fehler';
}

function resultTone(status: string) {
  return status === 'blocked' || status === 'failed' ? 'danger' : 'success';
}

function formatBytes(value: number) {
  if (value < 1024) {
    return `${value} B`;
  }
  if (value < 1024 * 1024) {
    return `${(value / 1024).toFixed(1)} KB`;
  }
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('de-AT', {
    dateStyle: 'short',
    timeStyle: 'short'
  }).format(new Date(value));
}

function shortList(items: string[]) {
  if (!items.length) {
    return 'keine';
  }
  if (items.length <= 4) {
    return items.join(', ');
  }
  return `${items.slice(0, 4).join(', ')} +${items.length - 4}`;
}

function defaultScheduleValue() {
  const date = new Date();
  date.setHours(date.getHours() + 1, 0, 0, 0);
  const offset = date.getTimezoneOffset();
  const local = new Date(date.getTime() - offset * 60_000);
  return local.toISOString().slice(0, 16);
}
