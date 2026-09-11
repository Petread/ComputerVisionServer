import { useQuery } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import type { RuntimeOperationalSummary } from '@cv-platform/schemas';
import { api } from '../../app/api';

export function HealthPage() {
  const statusQuery = useQuery({
    queryKey: ['system-status'],
    queryFn: api.admin.systemStatus,
    refetchInterval: 10000
  });

  if (!statusQuery.data) {
    return (
      <Panel title="Operational Health">
        <EmptyState
          title="Loading operational health"
          body="The control plane is gathering service readiness, dependency posture, worker heartbeats, disk growth, and GPU telemetry."
        />
      </Panel>
    );
  }

  const data = statusQuery.data;
  const criticalAlerts = data.alerts.filter((alert) => alert.severity === 'critical').length;
  const warningAlerts = data.alerts.filter((alert) => alert.severity === 'warning').length;
  const hottestDisk = [...data.disks].sort((left, right) => right.used_percent - left.used_percent)[0];
  const busiestGpu = [...data.gpus].sort(
    (left, right) => (right.memory_used_percent ?? 0) - (left.memory_used_percent ?? 0)
  )[0];
  const runtimeAttention = data.runtime.degraded_channels + data.runtime.failed_channels + data.runtime.stale_runtime_instances;

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Operational Health</p>
          <h2>See readiness, dependency posture, worker liveness, storage pressure, GPU headroom, and concrete recovery hints in one place.</h2>
          <p className="subtle-text">
            This page is meant for real operations work on the single-node platform: startup checks,
            restart triage, batch backlog review, rollout verification, and crash recovery follow-through.
          </p>
        </div>
        <div className="training-summary-pills">
          <Metric label="Critical Alerts" value={String(criticalAlerts)} />
          <Metric label="Warnings" value={String(warningAlerts)} />
          <Metric label="Workers" value={String(data.workers.length)} />
          <Metric label="Runtime Attention" value={String(runtimeAttention)} />
          <Metric
            label="Hottest Disk"
            value={hottestDisk ? `${hottestDisk.label} ${hottestDisk.used_percent.toFixed(1)}%` : 'n/a'}
          />
          <Metric
            label="Busiest GPU"
            value={
              busiestGpu?.memory_used_percent != null
                ? `${busiestGpu.name ?? `GPU ${busiestGpu.device_index}`} ${busiestGpu.memory_used_percent.toFixed(1)}%`
                : 'n/a'
            }
          />
        </div>
      </section>

      <Panel title="Alert-Worthy States">
        {data.alerts.length ? (
          <div className="status-grid">
            {data.alerts.map((alert) => (
              <article
                key={alert.code}
                className={`status-card status-card--${alert.severity === 'critical' ? 'down' : alert.severity === 'warning' ? 'degraded' : 'ok'}`}
              >
                <div className="status-card__header">
                  <h3>{alert.title}</h3>
                  <span className={`badge badge--${alert.severity === 'critical' ? 'danger' : alert.severity === 'warning' ? 'warn' : 'success'}`}>
                    {alert.severity}
                  </span>
                </div>
                <p>{alert.detail}</p>
                <p className="subtle-text">{alert.remediation_hint ?? alert.source}</p>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            title="No active alerts"
            body="The control plane does not currently see service outages, stale workers, queue pressure, disk pressure, or GPU memory contention."
          />
        )}
      </Panel>

      <RuntimePlanePanel runtime={data.runtime} />

      <div className="page-grid">
        <Panel title="Service Readiness">
          <table className="data-table">
            <thead>
              <tr>
                <th>Service</th>
                <th>State</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {data.services.map((service) => (
                <tr key={service.name}>
                  <td>{service.name}</td>
                  <td>
                    <span className={`badge badge--${tone(service.state)}`}>{service.state}</span>
                  </td>
                  <td>{service.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Dependency Aggregation">
          <table className="data-table">
            <thead>
              <tr>
                <th>Dependency</th>
                <th>Category</th>
                <th>State</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {data.dependencies.map((dependency) => (
                <tr key={dependency.name}>
                  <td>{dependency.name}</td>
                  <td>{dependency.category}</td>
                  <td>
                    <span className={`badge badge--${tone(dependency.state)}`}>{dependency.state}</span>
                  </td>
                  <td>{dependency.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="Worker Heartbeats">
          <table className="data-table">
            <thead>
              <tr>
                <th>Worker</th>
                <th>Instance</th>
                <th>State</th>
                <th>Status</th>
                <th>Last Heartbeat</th>
              </tr>
            </thead>
            <tbody>
              {data.workers.map((worker) => (
                <tr key={`${worker.service_name}-${worker.instance_id}`}>
                  <td>{worker.service_name}</td>
                  <td>{worker.instance_id}</td>
                  <td>
                    <span className={`badge badge--${tone(worker.state)}`}>{worker.state}</span>
                  </td>
                  <td>{worker.status}</td>
                  <td>{worker.last_heartbeat_at ?? 'n/a'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="Queue Pressure">
          <table className="data-table">
            <thead>
              <tr>
                <th>Queue</th>
                <th>Depth</th>
                <th>Oldest Age</th>
              </tr>
            </thead>
            <tbody>
              {data.queues.map((queue) => (
                <tr key={queue.queue_name}>
                  <td>{queue.queue_name}</td>
                  <td>{queue.depth}</td>
                  <td>{queue.oldest_job_age_seconds}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>

      <div className="training-grid training-grid--details">
        <Panel title="Disk Usage">
          <table className="data-table">
            <thead>
              <tr>
                <th>Path</th>
                <th>State</th>
                <th>Used</th>
                <th>Free</th>
              </tr>
            </thead>
            <tbody>
              {data.disks.map((disk) => (
                <tr key={disk.key}>
                  <td>
                    <strong>{disk.label}</strong>
                    <div className="subtle-text">{disk.path}</div>
                  </td>
                  <td>
                    <span className={`badge badge--${tone(disk.state)}`}>{disk.state}</span>
                  </td>
                  <td>{disk.used_percent.toFixed(1)}% - {formatBytes(disk.used_bytes)}</td>
                  <td>{formatBytes(disk.free_bytes)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>

        <Panel title="GPU Visibility">
          <table className="data-table">
            <thead>
              <tr>
                <th>GPU</th>
                <th>State</th>
                <th>Utilization</th>
                <th>Memory</th>
                <th>Temp</th>
              </tr>
            </thead>
            <tbody>
              {data.gpus.map((gpu) => (
                <tr key={`${gpu.device_index}-${gpu.name ?? 'gpu'}`}>
                  <td>{gpu.name ?? `GPU ${gpu.device_index}`}</td>
                  <td>
                    <span className={`badge badge--${tone(gpu.state)}`}>{gpu.state}</span>
                  </td>
                  <td>{gpu.utilization_percent ?? 'n/a'}%</td>
                  <td>
                    {gpu.memory_used_mb ?? 'n/a'} / {gpu.memory_total_mb ?? 'n/a'} MB
                  </td>
                  <td>{gpu.temperature_c ?? 'n/a'} C</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      </div>
    </div>
  );
}

export function RuntimePlanePanel(props: { runtime: RuntimeOperationalSummary }) {
  const { runtime } = props;
  return (
    <Panel title="Runtime Plane">
      <div className="detail-grid">
        <RuntimeMetric label="Channels" value={String(runtime.total_channels)} state={runtime.state} />
        <RuntimeMetric label="Running" value={String(runtime.running_channels)} state="ok" />
        <RuntimeMetric
          label="Stale"
          value={String(runtime.stale_runtime_instances)}
          state={runtime.stale_runtime_instances ? 'degraded' : 'ok'}
        />
        <RuntimeMetric
          label="Realtime FPS"
          value={formatOptionalNumber(runtime.performance.realtime_fps, 1)}
          state="ok"
        />
        <RuntimeMetric
          label="Realtime p95"
          value={formatOptionalMs(runtime.performance.realtime_latency_p95_ms)}
          state={
            runtime.performance.realtime_latency_p95_ms &&
            runtime.performance.realtime_latency_p95_ms > 250
              ? 'degraded'
              : 'ok'
          }
        />
        <RuntimeMetric
          label="Trigger p95"
          value={formatOptionalMs(runtime.performance.trigger_latency_p95_ms)}
          state="ok"
        />
      </div>
      {runtime.recovery_hint ? (
        <div className="inline-alert inline-alert--warn">{runtime.recovery_hint}</div>
      ) : null}
      {runtime.attention.length ? (
        <div className="status-grid status-grid--compact">
          {runtime.attention.slice(0, 6).map((item) => (
            <article key={item.channel_id} className={`status-card status-card--${item.health_state}`}>
              <div className="status-card__header">
                <h3>{item.name}</h3>
                <span className={`badge badge--${tone(item.health_state)}`}>
                  {item.observed_state.replaceAll('_', ' ')}
                </span>
              </div>
              <p>{item.health_text}</p>
              <p className="subtle-text">{item.recovery_hint ?? item.technical_place}</p>
            </article>
          ))}
        </div>
      ) : (
        <p className="subtle-text">Runtime channels have no active recovery alerts.</p>
      )}
    </Panel>
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

function RuntimeMetric(props: { label: string; value: string; state: 'ok' | 'degraded' | 'down' }) {
  return (
    <div className={`status-card status-card--${props.state}`}>
      <span className={`badge badge--${tone(props.state)}`}>{props.label}</span>
      <strong>{props.value}</strong>
    </div>
  );
}

function tone(state: 'ok' | 'degraded' | 'down') {
  if (state === 'ok') {
    return 'success';
  }
  if (state === 'degraded') {
    return 'warn';
  }
  return 'danger';
}

function formatOptionalNumber(value?: number | null, digits = 0) {
  return value === null || value === undefined ? 'n/a' : value.toFixed(digits);
}

function formatOptionalMs(value?: number | null) {
  return value === null || value === undefined ? 'n/a' : `${value.toFixed(0)} ms`;
}

function formatBytes(value: number) {
  if (!value) {
    return '0 B';
  }
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let current = value;
  let unitIndex = 0;
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024;
    unitIndex += 1;
  }
  return `${current.toFixed(current >= 10 ? 0 : 1)} ${units[unitIndex]}`;
}
