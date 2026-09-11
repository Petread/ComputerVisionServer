import { Link } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { EmptyState, Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';
import { stringifyClassDistribution } from '../datasets/presentation';

export function LabelingPage() {
  const statusQuery = useQuery({
    queryKey: ['system-status'],
    queryFn: api.admin.systemStatus
  });
  const projectsQuery = useQuery({
    queryKey: ['label-projects'],
    queryFn: api.datasets.projects
  });
  const relatedServices = statusQuery.data?.services.filter((service) =>
    ['webhook-receiver', 'dataset-service'].includes(service.name)
  );

  return (
    <Panel title="Labeling Projects">
      <EmptyState
        title="Label Studio stays visible from the control room"
        body="Project discovery, mapping, webhook audit, annotation statistics, and snapshot requests all stay browser-first. Operators can still jump directly into Label Studio when they need the native labeling experience."
      >
        <div className="status-grid">
          {relatedServices?.map((service) => (
            <article key={service.name} className={`status-card status-card--${service.state}`}>
              <h3>{service.name}</h3>
              <p>{service.detail}</p>
            </article>
          ))}
        </div>
        <div className="detail-grid">
          {projectsQuery.data?.items.slice(0, 3).map((project) => (
            <article key={project.label_project_id} className="status-card">
              <h3>{project.title}</h3>
              <p className="subtle-text">
                {project.annotation_count} annotations across {project.annotated_task_count}/{project.task_count} tasks
              </p>
              <p className="subtle-text">{stringifyClassDistribution(project.class_distribution)}</p>
            </article>
          ))}
        </div>
        <div className="actions-row">
          <Link className="primary-link" to="/datasets/projects">
            Project Mapping
          </Link>
          <Link className="primary-link" to="/datasets/webhooks">
            Webhook Audit
          </Link>
          <Link className="primary-link" to="/datasets/stats">
            Annotation Stats
          </Link>
        </div>
        <a className="primary-link" href="/label-studio/" target="_blank" rel="noreferrer">
          Open Label Studio
        </a>
      </EmptyState>
    </Panel>
  );
}
