import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';
import { architectureLabel, taskTypeLabel } from '../pipelines/modeling';
import { endpointAvailabilityLabel, flattenProjectEndpoints, formatDate } from '../pipelines/presentation';

export function EndpointsPage() {
  const pipelinesQuery = useQuery({
    queryKey: ['project-pipelines'],
    queryFn: api.pipelines.list,
    refetchInterval: 10000
  });

  const pipelines = pipelinesQuery.data?.items ?? [];
  const endpoints = flattenProjectEndpoints(pipelines);
  const liveCount = endpoints.filter((endpoint) => endpoint.realtime_available || endpoint.batch_available).length;
  const realtimeCount = endpoints.filter((endpoint) => endpoint.realtime_available).length;
  const batchCount = endpoints.filter((endpoint) => endpoint.batch_available).length;

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Inference Endpoints</p>
          <h2>Show exactly what another application needs to call after deployment.</h2>
          <p className="subtle-text">
            Endpoint names stay logical and stable. The page keeps internal release IDs, Triton names,
            and packaging details behind the scenes while still exposing the task type, availability,
            route examples, and currently deployed version.
          </p>
        </div>
        <div className="training-summary-pills">
          <MetricPill label="Endpoints" value={String(endpoints.length)} />
          <MetricPill label="Live" value={String(liveCount)} tone={liveCount ? 'success' : undefined} />
          <MetricPill label="Realtime" value={String(realtimeCount)} />
          <MetricPill label="Batch" value={String(batchCount)} />
        </div>
      </section>

      <Panel title="Endpoint Catalog">
        {!endpoints.length ? (
          <EmptyState
            title="No endpoint definitions yet"
            body="Create a model from a project pipeline first. Endpoint names are generated automatically from the project and preset."
          />
        ) : (
          <div className="pipeline-grid">
            {endpoints.map((endpoint) => (
              <article key={`${endpoint.projectTitle}-${endpoint.endpoint_name}`} className="pipeline-card">
                <div className="status-card__header">
                  <div>
                    <p className="training-hero__eyebrow">{endpoint.projectTitle}</p>
                    <h3>{endpoint.display_name}</h3>
                    <p className="subtle-text">{endpoint.endpoint_name}</p>
                  </div>
                  <span className={`badge badge--${endpoint.realtime_available || endpoint.batch_available ? 'success' : 'neutral'}`}>
                    {endpointAvailabilityLabel(endpoint)}
                  </span>
                </div>

                <div className="detail-grid">
                  <div className="metric-pill">
                    <span>Task type</span>
                    <strong>{taskTypeLabel(endpoint.task_type)}</strong>
                    <small className="subtle-text">
                      {architectureLabel(endpoint.architecture)} • preferred mode: {endpoint.preferred_mode}
                    </small>
                  </div>
                  <div className="metric-pill">
                    <span>Current version</span>
                    <strong>{endpoint.current_model_version_no ? `v${endpoint.current_model_version_no}` : 'Not deployed'}</strong>
                    <small className="subtle-text">Last deployment {formatDate(endpoint.last_deployed_at)}</small>
                  </div>
                  <div className="metric-pill">
                    <span>Realtime</span>
                    <strong>{endpoint.realtime_available ? 'Available' : 'Unavailable'}</strong>
                    <small className="subtle-text">{endpoint.realtime_available ? 'Ready for live calls' : 'No realtime target'}</small>
                  </div>
                  <div className="metric-pill">
                    <span>Batch</span>
                    <strong>{endpoint.batch_available ? 'Available' : 'Unavailable'}</strong>
                    <small className="subtle-text">{endpoint.batch_available ? 'Ready for jobs' : 'No batch target'}</small>
                  </div>
                </div>

                <details className="advanced-section">
                  <summary>Release identifiers</summary>
                  <p className="subtle-text">
                    Realtime: {endpoint.realtime_release_id ?? 'none'} / Batch:{' '}
                    {endpoint.batch_release_id ?? 'none'}
                  </p>
                </details>

                <div className="endpoint-route-list">
                  {endpoint.api_examples.map((example) => (
                    <code key={`${endpoint.endpoint_name}-${example.method}-${example.path}`} className="endpoint-route">
                      {example.method} {example.path}
                    </code>
                  ))}
                </div>

                <div className="pipeline-card__footer">
                  <Link to="/endpoints/$modelId" params={{ modelId: endpoint.endpoint_name }} className="primary-link">
                    Open endpoint details
                  </Link>
                  <Link to="/pipelines/$labelProjectId" params={{ labelProjectId: endpoint.label_project_id }}>
                    Open pipeline
                  </Link>
                </div>
              </article>
            ))}
          </div>
        )}
      </Panel>
    </div>
  );
}
