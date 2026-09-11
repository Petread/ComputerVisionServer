import { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Panel } from '@cv-platform/ui-kit';
import { api } from '../../app/api';
import { formatDate, stringifyClassDistribution } from './presentation';

export function AnnotationStatisticsPage() {
  const datasetsQuery = useQuery({
    queryKey: ['datasets'],
    queryFn: api.datasets.list
  });
  const projectsQuery = useQuery({
    queryKey: ['label-projects'],
    queryFn: api.datasets.projects
  });

  const [datasetId, setDatasetId] = useState('');
  const [labelProjectId, setLabelProjectId] = useState('');

  useEffect(() => {
    if (!datasetId && datasetsQuery.data?.items[0]) {
      setDatasetId(datasetsQuery.data.items[0].dataset_id);
    }
  }, [datasetId, datasetsQuery.data]);

  useEffect(() => {
    if (!labelProjectId && projectsQuery.data?.items[0]) {
      setLabelProjectId(projectsQuery.data.items[0].label_project_id);
    }
  }, [labelProjectId, projectsQuery.data]);

  const datasetStatsQuery = useQuery({
    queryKey: ['dataset-stats', datasetId],
    queryFn: () => api.datasets.datasetStats(datasetId),
    enabled: Boolean(datasetId)
  });

  const projectStatsQuery = useQuery({
    queryKey: ['project-stats', labelProjectId],
    queryFn: () => api.datasets.projectStats(labelProjectId),
    enabled: Boolean(labelProjectId)
  });

  return (
    <div className="panel-stack">
      <Panel title="Dataset Annotation Statistics">
        <div className="inline-form">
          <select value={datasetId} onChange={(event) => setDatasetId(event.target.value)}>
            {datasetsQuery.data?.items.map((dataset) => (
              <option key={dataset.dataset_id} value={dataset.dataset_id}>
                {dataset.name}
              </option>
            ))}
          </select>
        </div>
        {datasetStatsQuery.data ? (
          <div className="detail-grid">
            <article className="status-card">
              <h3>Coverage</h3>
              <p className="subtle-text">
                {datasetStatsQuery.data.annotated_task_count}/{datasetStatsQuery.data.task_count} tasks annotated
              </p>
              <strong>{datasetStatsQuery.data.annotation_count} annotations</strong>
            </article>
            <article className="status-card">
              <h3>Eligible Finalized</h3>
              <p className="subtle-text">Submitted items usable for training and export</p>
              <strong>{datasetStatsQuery.data.eligible_task_count}</strong>
            </article>
            <article className="status-card">
              <h3>Snapshots</h3>
              <p className="subtle-text">Immutable snapshots created from this dataset</p>
              <strong>{datasetStatsQuery.data.snapshot_count}</strong>
            </article>
            <article className="status-card">
              <h3>Class Distribution</h3>
              <p className="subtle-text">{stringifyClassDistribution(datasetStatsQuery.data.class_distribution)}</p>
            </article>
            <article className="status-card">
              <h3>Last Activity</h3>
              <p className="subtle-text">{formatDate(datasetStatsQuery.data.last_activity_at)}</p>
            </article>
          </div>
        ) : null}
      </Panel>

      <Panel title="Project Statistics">
        <div className="inline-form">
          <select value={labelProjectId} onChange={(event) => setLabelProjectId(event.target.value)}>
            {projectsQuery.data?.items.map((project) => (
              <option key={project.label_project_id} value={project.label_project_id}>
                {project.title}
              </option>
            ))}
          </select>
        </div>
        {projectStatsQuery.data ? (
          <div className="meta-grid">
            <div>
              <span className="subtle-text">Tasks</span>
              <strong>{projectStatsQuery.data.task_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Annotated Tasks</span>
              <strong>{projectStatsQuery.data.annotated_task_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Annotations</span>
              <strong>{projectStatsQuery.data.annotation_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Unlabeled Tasks</span>
              <strong>{projectStatsQuery.data.unlabeled_task_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Draft / Unsubmitted</span>
              <strong>{projectStatsQuery.data.draft_task_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Background Images</span>
              <strong>{projectStatsQuery.data.submitted_background_task_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Invalid / Excluded</span>
              <strong>{projectStatsQuery.data.invalid_task_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Snapshots</span>
              <strong>{projectStatsQuery.data.snapshot_count}</strong>
            </div>
            <div>
              <span className="subtle-text">Last Activity</span>
              <strong>{formatDate(projectStatsQuery.data.last_activity_at)}</strong>
            </div>
          </div>
        ) : null}
        {projectStatsQuery.data ? (
          <pre className="code-block">
            {JSON.stringify(projectStatsQuery.data.class_distribution, null, 2)}
          </pre>
        ) : null}
      </Panel>
    </div>
  );
}
