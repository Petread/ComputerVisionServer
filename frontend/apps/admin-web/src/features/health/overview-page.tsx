import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';

export function OverviewPage() {
  const statusQuery = useQuery({
    queryKey: ['system-status'],
    queryFn: api.admin.systemStatus,
    refetchInterval: 10000
  });
  const deploymentTargetsQuery = useQuery({
    queryKey: ['deployment-targets'],
    queryFn: api.deployments.targets,
    refetchInterval: 10000
  });
  const trainingJobsQuery = useQuery({
    queryKey: ['training-jobs'],
    queryFn: api.training.list,
    refetchInterval: 10000
  });
  const pipelinesQuery = useQuery({
    queryKey: ['project-pipelines'],
    queryFn: api.pipelines.list,
    refetchInterval: 10000
  });
  const batchJobsQuery = useQuery({
    queryKey: ['batch-jobs'],
    queryFn: api.inference.listBatchJobs,
    refetchInterval: 10000
  });

  const activeTrainingJobs =
    trainingJobsQuery.data?.items.filter((job) => !['completed', 'failed', 'cancelled'].includes(job.status))
      .length ?? 0;
  const activeBatchJobs =
    batchJobsQuery.data?.items.filter((job) => !['completed', 'failed', 'canceled'].includes(job.status))
      .length ?? 0;
  const activeAlerts = statusQuery.data?.alerts ?? [];
  const runtimeAttention = statusQuery.data
    ? statusQuery.data.runtime.degraded_channels +
      statusQuery.data.runtime.failed_channels +
      statusQuery.data.runtime.stale_runtime_instances
    : 0;
  const readyPipelines =
    pipelinesQuery.data?.items.filter((pipeline) => Boolean(pipeline.latest_valid_snapshot)).length ?? 0;
  const liveEndpoints =
    pipelinesQuery.data?.items.filter((pipeline) =>
      pipeline.current_endpoints.some((endpoint) => endpoint.realtime_available || endpoint.batch_available)
    ).length ?? 0;

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Overview / System Status</p>
          <h2>Start with live health, active work, and the next operator shortcut.</h2>
          <p className="subtle-text">
            The default view keeps only the signals needed for daily operation. Queue internals and
            service details stay available below when an investigation needs them.
          </p>
        </div>
        <div className="training-summary-pills">
          <Metric label="Training Active" value={String(activeTrainingJobs)} />
          <Metric label="Batch Active" value={String(activeBatchJobs)} />
          <Metric label="Trainingsbereite Projekte" value={String(readyPipelines)} />
          <Metric label="Aktive Deployments" value={String(liveEndpoints)} />
          <Metric label="Runtime Attention" value={String(runtimeAttention)} />
          <Metric label="Alerts" value={String(activeAlerts.length)} />
        </div>
      </section>

      <div className="page-grid">
        <Panel title="Platform Status">
          <div className="status-grid">
            {statusQuery.data?.services.map((service) => (
              <article key={service.name} className={`status-card status-card--${service.state}`}>
                <div className="status-card__header">
                  <h3>{service.name}</h3>
                  <span className={`badge badge--${service.state === 'ok' ? 'success' : service.state === 'down' ? 'danger' : 'warn'}`}>
                    {service.state}
                  </span>
                </div>
                <details className="details-panel">
                  <summary>Service detail</summary>
                  <p>{service.detail}</p>
                </details>
              </article>
            ))}
          </div>
        </Panel>

        <Panel title="Operational Details">
          <details className="advanced-section">
            <summary>Queue depth</summary>
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>Queue</th>
                  <th>Depth</th>
                  <th>Oldest Job Age</th>
                </tr>
              </thead>
              <tbody>
                {statusQuery.data?.queues.map((queue) => (
                  <tr key={queue.queue_name}>
                    <td>{queue.queue_name}</td>
                    <td>{queue.depth}</td>
                    <td>{queue.oldest_job_age_seconds}s</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
          <details className="advanced-section" open>
            <summary>Runtime plane</summary>
            <div className="detail-grid">
              <Metric label="Channels" value={String(statusQuery.data?.runtime.total_channels ?? 0)} />
              <Metric label="Running" value={String(statusQuery.data?.runtime.running_channels ?? 0)} />
              <Metric label="Stale" value={String(statusQuery.data?.runtime.stale_runtime_instances ?? 0)} />
              <Metric
                label="Realtime FPS"
                value={
                  statusQuery.data?.runtime.performance.realtime_fps !== null &&
                  statusQuery.data?.runtime.performance.realtime_fps !== undefined
                    ? statusQuery.data.runtime.performance.realtime_fps.toFixed(1)
                    : 'n/a'
                }
              />
            </div>
          </details>
        </Panel>
      </div>

      <Panel title="Top Alerts">
        {activeAlerts.length ? (
          <div className="status-grid">
            {activeAlerts.slice(0, 6).map((alert) => (
              <article key={alert.code} className={`status-card status-card--${alert.severity === 'critical' ? 'down' : 'degraded'}`}>
                <h3>{alert.title}</h3>
                <p>{alert.detail}</p>
                <p className="subtle-text">{alert.remediation_hint ?? alert.source}</p>
              </article>
            ))}
          </div>
        ) : (
          <p className="subtle-text">
            No operational alerts are active. Use the Operational Health page for detailed worker,
            dependency, disk, and GPU visibility.
          </p>
        )}
      </Panel>

      <div className="training-grid training-grid--details">
        <Panel title="Active Deployment Targets">
          <table className="data-table compact-table">
            <thead>
              <tr>
                <th>Target</th>
                <th>Active Model</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {deploymentTargetsQuery.data?.items.map((target) => (
                <tr key={target.target_path}>
                  <td>{target.target_path}</td>
                  <td>{target.active_model_version_id ?? 'none'}</td>
                  <td>{target.active_release_status ?? 'n/a'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <details className="advanced-section">
            <summary>Release identifiers</summary>
            <table className="data-table compact-table">
              <thead>
                <tr>
                  <th>Target</th>
                  <th>Active Release</th>
                  <th>Previous Release</th>
                </tr>
              </thead>
              <tbody>
                {deploymentTargetsQuery.data?.items.map((target) => (
                  <tr key={`${target.target_path}-release`}>
                    <td>{target.target_path}</td>
                    <td>{target.active_release_id ?? 'none'}</td>
                    <td>{target.previous_release_id ?? 'none'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </details>
        </Panel>

        <Panel title="Operator Shortcuts">
          <div className="detail-grid">
            <Shortcut
              to="/pipelines"
              title="Projekte & KI-Modelle öffnen"
              body="Datasets synchronisieren, Modelle trainieren und Deployments verwalten."
            />
            <Shortcut
              to="/channels"
              title="Channels öffnen"
              body="Kameras, Quellen, Live-Status und Laufzeitbetrieb verwalten."
            />
            <Shortcut
              to="/runtime-pipelines"
              title="Runtime Pipelines öffnen"
              body="Live- und Trigger-Abläufe für die Inferenz konfigurieren."
            />
            <Shortcut
              to="/operations/health"
              title="Review Operational Health"
              body="Inspect alerts, worker heartbeats, dependency health, disk growth, and GPU pressure."
            />
            <Shortcut
              to="/operations"
              title="Open Advanced Operations"
              body="Use the admin pages for manual snapshot controls, registry internals, deployment control, audit, and settings."
            />
          </div>
        </Panel>
      </div>
    </div>
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

function Shortcut(props: { to: string; title: string; body: string }) {
  return (
    <Link to={props.to} className="status-card">
      <h3>{props.title}</h3>
      <p className="subtle-text">{props.body}</p>
    </Link>
  );
}
