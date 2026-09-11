import { useEffect, useMemo, useState } from 'react';
import { Link } from '@tanstack/react-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Search } from '../../components/workspace-ui';
import { LabelStudioProjectLink } from './label-studio-link';
import type { ProjectPipelineSummary, TaskType } from '@cv-platform/schemas';
import { api } from '../../app/api';
import { describeError } from '../../app/errors';
import { AppIcon, type AppIconName } from '../../components/app-icon';
import { useNotifications } from '../../components/notification-provider';
import { taskTypeLabel, taskTypeOptions } from './modeling';
import {
  endpointAvailabilityLabel,
  snapshotLabel,
  trainingTone
} from './presentation';

type PipelineTaskFilter = 'all' | TaskType;
type PipelineStatusTone = 'neutral' | 'success' | 'warn' | 'danger';

const taskFilterOptions: Array<{ value: PipelineTaskFilter; label: string }> = [
  { value: 'all', label: 'Alle Typen' },
  ...taskTypeOptions.map((taskType) => ({ value: taskType, label: taskTypeLabel(taskType) }))
];

export function PipelineListPage() {
  const queryClient = useQueryClient();
  const { notify } = useNotifications();
  const [queuedInitialRefresh, setQueuedInitialRefresh] = useState(false);
  const [taskFilter, setTaskFilter] = useState<PipelineTaskFilter>('all');
  const [searchTerm, setSearchTerm] = useState('');

  const pipelinesQuery = useQuery({
    queryKey: ['project-pipelines'],
    queryFn: api.pipelines.list,
    refetchInterval: 10000
  });

  const refreshMutation = useMutation({
    mutationFn: api.pipelines.refresh,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['project-pipelines'] });
      for (const delayMs of [2000, 5000]) {
        window.setTimeout(() => {
          void queryClient.invalidateQueries({ queryKey: ['project-pipelines'] });
        }, delayMs);
      }
    },
    onError: (error) => {
      notify({ ...describeError(error, 'Background project sync failed'), tone: 'error' });
    }
  });

  useEffect(() => {
    if (!queuedInitialRefresh) {
      setQueuedInitialRefresh(true);
      refreshMutation.mutate();
    }
  }, [queuedInitialRefresh, refreshMutation]);

  const pipelineItems = pipelinesQuery.data?.items;
  const pipelines = useMemo(() => pipelineItems ?? [], [pipelineItems]);
  const filteredPipelines = useMemo(() => {
    const query = searchTerm.trim().toLowerCase();
    return pipelines.filter((pipeline) => {
      const matchesType = taskFilter === 'all' || pipeline.task_type === taskFilter;
      const matchesSearch = !query || pipelineSearchText(pipeline).includes(query);
      return matchesType && matchesSearch;
    });
  }, [pipelines, searchTerm, taskFilter]);
  const pipelineError = pipelinesQuery.isError
    ? describeError(pipelinesQuery.error, 'Pipeline list could not be loaded')
    : null;

  return (
    <div className="ws-list-workspace workspace-project-list">
      <div className="ws-toolbar"><Search value={searchTerm} onChange={setSearchTerm} placeholder="Projekt, Dataset, Modell, Deployment …"/><select aria-label="Projekttyp" value={taskFilter} onChange={event => setTaskFilter(event.target.value as PipelineTaskFilter)}>{taskFilterOptions.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select><span className="ws-spacer"/><button onClick={() => refreshMutation.mutate()} disabled={refreshMutation.isPending}>{refreshMutation.isPending ? 'Synchronisiert …' : 'Synchronisieren'}</button><a className="button-link" href="/label-studio/" target="_blank" rel="noreferrer">Label Studio öffnen ↗</a></div><div className="ws-scroll">
        {pipelineError ? (
          <div className="inline-alert inline-alert--danger">
            <strong>{pipelineError.title}</strong>
            {pipelineError.description ? <p className="subtle-text">{pipelineError.description}</p> : null}
          </div>
        ) : !pipelines.length ? (
          <p className="subtle-text">
            No Label Studio projects are visible yet. A background sync is queued automatically when
            this page opens, and Label Studio webhook activity will continue to refresh pipeline state.
          </p>
        ) : (
          <>
            {!filteredPipelines.length ? (
              <p className="subtle-text">No pipeline workspace matches the current filters.</p>
            ) : (
              <table className="ws-table ws-project-table workspace-project-table"><thead><tr><th>Projekt</th><th>Typ</th><th>Status</th><th>Dataset</th><th>Modelle</th><th>Deployment</th><th>Label Studio</th></tr></thead><tbody>
                {filteredPipelines.map(pipeline => { const status = pipelineStatusSummary(pipeline); return <tr key={pipeline.label_project_id}>
                  <td><Link to="/pipelines/$labelProjectId" params={{labelProjectId: pipeline.label_project_id}}>{pipeline.title}</Link><small>{pipeline.next_recommended_action}</small></td>
                  <td>{taskTypeLabel(pipeline.task_type)}</td><td><span className={`traffic-status traffic-status--${status.tone}`}><AppIcon name={status.icon}/>{status.label}</span></td>
                  <td title={pipeline.readiness.readiness_reason}>{pipeline.readiness.readiness_percent.toFixed(0)}% bereit · {pipeline.readiness.eligible_task_count} Bilder</td><td>{pipeline.models.length}</td><td>{pipeline.current_endpoints.filter(e => e.realtime_available || e.batch_available).map(e => e.endpoint_name).join(', ') || '—'}</td>
                  <td><LabelStudioProjectLink projectId={pipeline.label_studio_project_id}/></td>
                </tr>; })}
              </tbody></table>
            )}
          </>
        )}
      </div><footer className="ws-statusline"><span>{filteredPipelines.length} von {pipelines.length} Projekten</span><span>Label Studio öffnet das zugeordnete Projekt direkt</span></footer>
    </div>
  );
}

function pipelineSearchText(pipeline: ProjectPipelineSummary) {
  const endpointText = pipeline.current_endpoints
    .map((endpoint) =>
      [
        endpoint.endpoint_name,
        endpoint.display_name,
        endpoint.model_family_slug,
        endpointAvailabilityLabel(endpoint)
      ].join(' ')
    )
    .join(' ');
  return [
    pipeline.title,
    pipeline.status,
    pipeline.label_project_id,
    pipeline.label_studio_project_id,
    pipeline.dataset_id,
    pipeline.dataset_name,
    pipeline.training_pipeline_slug,
    taskTypeLabel(pipeline.task_type),
    pipeline.next_recommended_action,
    pipeline.next_action_detail,
    pipeline.readiness.readiness_reason,
    snapshotLabel(pipeline.latest_snapshot),
    pipeline.latest_snapshot?.status,
    snapshotLabel(pipeline.latest_valid_snapshot),
    pipeline.latest_valid_snapshot?.status,
    pipeline.last_training?.training_job_id,
    pipeline.last_training?.model_family_slug,
    pipeline.last_training?.status,
    endpointText
  ]
    .filter((value) => value !== undefined && value !== null)
    .join(' ')
    .toLowerCase();
}

function pipelineStatusSummary(pipeline: ProjectPipelineSummary): {
  label: string;
  tone: PipelineStatusTone;
  icon: AppIconName;
} {
  const liveEndpoint = pipeline.current_endpoints.some(
    (endpoint) => endpoint.realtime_available || endpoint.batch_available
  );
  const latestSnapshotFailed = pipeline.latest_snapshot?.status === 'failed';
  const lastTrainingFailed = trainingTone(pipeline.last_training?.status) === 'danger';

  if (latestSnapshotFailed || lastTrainingFailed) {
    return { label: 'Needs attention', tone: 'danger', icon: 'alert' };
  }
  if (liveEndpoint) {
    return { label: 'Live', tone: 'success', icon: 'check' };
  }
  if (pipeline.latest_valid_snapshot) {
    return { label: 'Ready', tone: 'success', icon: 'check' };
  }
  if (pipeline.readiness.task_count > 0) {
    return { label: 'In progress', tone: 'warn', icon: 'activity' };
  }
  return { label: 'Waiting', tone: 'neutral', icon: 'pipeline' };
}
