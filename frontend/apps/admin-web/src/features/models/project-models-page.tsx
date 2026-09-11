import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { EmptyState, MetricPill, Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';
import { architectureLabel, taskTypeLabel } from '../pipelines/modeling';
import { endpointAvailabilityLabel, flattenProjectEndpoints, flattenProjectModels, formatDate } from '../pipelines/presentation';

export function ProjectModelsPage() {
  const pipelinesQuery = useQuery({
    queryKey: ['project-pipelines'],
    queryFn: api.pipelines.list,
    refetchInterval: 10000
  });

  const pipelines = pipelinesQuery.data?.items ?? [];
  const models = flattenProjectModels(pipelines);
  const endpoints = flattenProjectEndpoints(pipelines);
  const liveEndpointByFamilyId = new Map(
    endpoints
      .filter((endpoint) => endpoint.realtime_available || endpoint.batch_available)
      .map((endpoint) => [endpoint.model_family_id, endpoint])
  );
  const trainedCount = models.filter((model) => model.latest_model_version_no).length;

  return (
    <div className="panel-stack">
      <section className="training-hero">
        <div>
          <p className="training-hero__eyebrow">Models</p>
          <h2>See project models in operator language: preset, training state, and live endpoint.</h2>
          <p className="subtle-text">
            Operators should not chase internal registry IDs through multiple admin pages. This view
            keeps each model tied to its project pipeline and shows whether it has reached a callable
            endpoint yet.
          </p>
        </div>
        <div className="training-summary-pills">
          <MetricPill label="Project models" value={String(models.length)} />
          <MetricPill label="Trained" value={String(trainedCount)} tone="success" />
          <MetricPill
            label="Live endpoints"
            value={String(liveEndpointByFamilyId.size)}
            tone={liveEndpointByFamilyId.size ? 'success' : undefined}
          />
          <MetricPill label="Pipelines" value={String(pipelines.length)} />
        </div>
      </section>

      <Panel title="Model Workspace">
        {!models.length ? (
          <EmptyState
            title="No models created yet"
            body="Create the first model from a project pipeline and the platform will generate the technical identifiers automatically."
          />
        ) : (
          <div className="pipeline-grid">
            {models.map((model) => {
              const endpoint = liveEndpointByFamilyId.get(model.model_family_id);
              return (
                <article key={model.model_family_id} className="pipeline-card">
                  <div className="status-card__header">
                    <div>
                      <p className="training-hero__eyebrow">{model.projectTitle}</p>
                      <h3>{model.display_name}</h3>
                    </div>
                    <span className={`badge badge--${endpoint ? 'success' : 'neutral'}`}>
                      {endpoint ? 'Live endpoint' : model.latest_model_status ?? 'Draft'}
                    </span>
                  </div>

                  <div className="detail-grid">
                    <div className="metric-pill">
                      <span>Preset</span>
                      <strong>{model.template_key ?? 'custom'}</strong>
                      <small className="subtle-text">
                        {architectureLabel(model.architecture)} / {model.training_profile_key ?? 'default profile'}
                      </small>
                    </div>
                    <div className="metric-pill">
                      <span>Latest version</span>
                      <strong>{model.latest_model_version_no ? `v${model.latest_model_version_no}` : 'Not trained'}</strong>
                      <small className="subtle-text">
                        {taskTypeLabel(model.task_type)} / {model.latest_model_status ?? 'Awaiting first run'}
                      </small>
                    </div>
                    <div className="metric-pill">
                      <span>Endpoint</span>
                      <strong>{endpoint?.endpoint_name ?? model.endpoint_name}</strong>
                      <small className="subtle-text">
                        {endpoint ? endpointAvailabilityLabel(endpoint) : 'Not deployed yet'}
                      </small>
                    </div>
                    <div className="metric-pill">
                      <span>Created</span>
                      <strong>{formatDate(model.created_at)}</strong>
                      <small className="subtle-text">{model.nextAction}</small>
                    </div>
                  </div>

                  <div className="pipeline-card__footer">
                    {model.label_project_id ? (
                      <Link
                        to="/pipelines/$labelProjectId"
                        params={{ labelProjectId: model.label_project_id }}
                        className="primary-link"
                      >
                        Open pipeline
                      </Link>
                    ) : (
                      <span className="subtle-text">No linked project pipeline.</span>
                    )}
                    {endpoint ? (
                      <Link to="/endpoints/$modelId" params={{ modelId: endpoint.endpoint_name }}>
                        Open endpoint
                      </Link>
                    ) : (
                      <span className="subtle-text">Deploy after training passes the quality gates.</span>
                    )}
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
