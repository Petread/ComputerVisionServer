import { createRootRoute, createRoute, createRouter, Navigate, Outlet, useParams } from '@tanstack/react-router';
import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import { AccessPage } from '../features/access/access-page';
import { AuthShell } from '../components/auth-shell';
import { AuditLogPage } from '../features/audit/audit-log-page';
import { LoginPage } from '../features/auth/login-page';
import { BatchPage } from '../features/batch/batch-page';
import { ChannelDetailPage } from '../features/channels/channel-detail-page';
import { ChannelListPage } from '../features/channels/channel-list-page';
import { AnnotationStatisticsPage } from '../features/datasets/annotation-statistics-page';
import { DatasetsPage } from '../features/datasets/datasets-page';
import { ProjectMappingPage } from '../features/datasets/project-mapping-page';
import { WebhookAuditPage } from '../features/datasets/webhook-audit-page';
import { DeploymentsPage } from '../features/deployments/deployments-page';
import { EdgeDeviceDetailPage, EdgeDeviceListPage } from '../features/edge-devices/edge-device-pages';
import { HealthPage } from '../features/health/health-page';
import { PlacesPage } from '../features/channels/places';
import { InferencePage } from '../features/inference/inference-page';
import { ModelsPage } from '../features/models/models-page';
import { ModelTemplatesPage } from '../features/model-templates/model-templates-page';
import { OperationsPage } from '../features/operations/operations-page';
import { WorkersUpdatesPage } from '../features/operations/workers-updates-page';
import { ProjectWorkspacePage } from '../features/pipelines/project-workspace-page';
import { PipelineListPage } from '../features/pipelines/pipeline-list-page';
import { RuntimePipelineDesignerPage } from '../features/runtime-pipelines/runtime-pipeline-designer-page';
import { ResultHistoryPage } from '../features/results/result-history-page';
import { SettingsPage } from '../features/settings/settings-page';
import { SnapshotDetailsPage } from '../features/snapshots/snapshot-details-page';
import { ValidationReportPage } from '../features/snapshots/validation-report-page';
import { TrainingPage } from '../features/training/training-page';

const rootRoute = createRootRoute({
  component: () => <Outlet />
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: LoginPage
});

const authenticatedRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: 'authenticated',
  component: AuthShell
});

const overviewRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/',
  component: DefaultWorkspace
});

function DefaultWorkspace() {
  const session = useQuery({queryKey: ['session'], queryFn: api.admin.me});
  return <Navigate to={session.data?.user?.role === 'label_manager' ? '/pipelines' : '/channels'}/>;
}

const pipelinesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/pipelines',
  component: PipelineListPage
});

const pipelineDetailRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/pipelines/$labelProjectId',
  validateSearch: (search: Record<string, unknown>): {
    modelFamilyId?: string;
    modelView?: 'status' | 'metrics' | 'parameters' | 'deployment';
  } => ({
    modelFamilyId: typeof search.modelFamilyId === 'string' ? search.modelFamilyId : undefined,
    modelView:
      search.modelView === 'status'
      || search.modelView === 'metrics'
      || search.modelView === 'parameters'
      || search.modelView === 'deployment'
        ? search.modelView
        : undefined
  }),
  component: ProjectWorkspacePage
});

const modelsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/models',
  component: () => <Navigate to="/pipelines" />
});

const endpointsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/endpoints',
  component: () => <Navigate to="/pipelines" />
});

const placesRoute = createRoute({ getParentRoute: () => authenticatedRoute, path: '/operations/places', component: PlacesPage });

const channelsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/channels',
  component: ChannelListPage
});

const edgeDevicesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/edge-devices',
  component: EdgeDeviceListPage
});

const edgeDeviceDetailRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/edge-devices/$edgeDeviceId',
  component: EdgeDeviceDetailPage
});

const runtimePipelinesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/runtime-pipelines',
  component: RuntimePipelineDesignerPage
});

const resultsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/results',
  component: ResultHistoryPage
});

const channelDetailRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/channels/$channelId',
  component: ChannelDetailPage
});

const endpointDetailRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/endpoints/$modelId',
  component: LegacyEndpointRedirect
});

function LegacyEndpointRedirect() {
  const { modelId } = useParams({ from: '/authenticated/endpoints/$modelId' });
  const pipelinesQuery = useQuery({
    queryKey: ['project-pipelines'],
    queryFn: api.pipelines.list
  });
  if (!pipelinesQuery.data) {
    return <div className="screen-center">Projekt und Modell werden gesucht...</div>;
  }
  const match = pipelinesQuery.data.items
    .flatMap((pipeline) =>
      pipeline.models.map((model) => ({ pipeline, model }))
    )
    .find(
      ({ pipeline, model }) =>
        model.model_family_id === modelId ||
        model.slug === modelId ||
        model.endpoint_name === modelId ||
        pipeline.current_endpoints.some(
          (endpoint) => endpoint.model_family_id === model.model_family_id && endpoint.endpoint_name === modelId
        )
    );
  if (!match) {
    return <Navigate to="/pipelines" />;
  }
  return (
    <Navigate
      to="/pipelines/$labelProjectId"
      params={{ labelProjectId: match.pipeline.label_project_id }}
      search={{ modelFamilyId: match.model.model_family_id, modelView: 'deployment' }}
    />
  );
}

const operationsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations',
  component: OperationsPage
});

const accessRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/access',
  component: AccessPage
});

const snapshotsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/snapshots',
  component: DatasetsPage
});

const snapshotDetailsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/snapshots/$snapshotId',
  component: SnapshotDetailsPage
});

const validationReportRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/snapshots/$snapshotId/validation',
  component: ValidationReportPage
});

const annotationStatisticsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/annotation-statistics',
  component: AnnotationStatisticsPage
});

const projectMappingRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/project-mapping',
  component: ProjectMappingPage
});

const webhookAuditRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/webhooks',
  component: WebhookAuditPage
});

const trainingRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/training',
  component: TrainingPage
});

const registryRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/registry',
  component: ModelsPage
});

const modelTemplatesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/model-templates',
  validateSearch: (search: Record<string, unknown>): {
    templateKey?: string;
    returnToLabelProjectId?: string;
  } => ({
    templateKey: typeof search.templateKey === 'string' ? search.templateKey : undefined,
    returnToLabelProjectId:
      typeof search.returnToLabelProjectId === 'string' ? search.returnToLabelProjectId : undefined
  }),
  component: ModelTemplatesPage
});

const deploymentsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/deployments',
  component: DeploymentsPage
});

const inferenceRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/inference',
  component: InferencePage
});

const batchRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/batch',
  component: BatchPage
});

const auditRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/audit',
  component: AuditLogPage
});

const settingsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/settings',
  component: SettingsPage
});

const healthRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/health',
  component: HealthPage
});

const workersUpdatesRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/operations/workers-updates',
  component: WorkersUpdatesPage
});

const datasetsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/datasets',
  component: () => <Navigate to="/operations/snapshots" />
});

const legacyProjectsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/projects',
  component: () => <Navigate to="/pipelines" />
});

const legacyProjectMappingRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/datasets/projects',
  component: () => <Navigate to="/pipelines" />
});

const legacyAnnotationStatisticsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/datasets/stats',
  component: () => <Navigate to="/operations/annotation-statistics" />
});

const legacyWebhookAuditRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/datasets/webhooks',
  component: () => <Navigate to="/operations/webhooks" />
});

const legacySnapshotDetailsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/datasets/snapshots/$snapshotId',
  component: SnapshotDetailsPage
});

const legacyValidationReportRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/datasets/snapshots/$snapshotId/validation',
  component: ValidationReportPage
});

const legacySnapshotsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/snapshots',
  component: () => <Navigate to="/operations/snapshots" />
});

const labelingRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/labeling',
  component: () => <Navigate to="/pipelines" />
});

const legacyTrainingRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/training',
  component: () => <Navigate to="/operations/training" />
});

const legacyDeploymentsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/deployments',
  component: () => <Navigate to="/operations/deployments" />
});

const legacyInferenceRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/inference',
  component: () => <Navigate to="/operations/inference" />
});

const legacyBatchRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/batch',
  component: () => <Navigate to="/operations/batch" />
});

const legacyAuditRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/audit',
  component: () => <Navigate to="/operations/audit" />
});

const legacySettingsRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/settings',
  component: () => <Navigate to="/operations/settings" />
});

const legacyHealthRoute = createRoute({
  getParentRoute: () => authenticatedRoute,
  path: '/health',
  component: () => <Navigate to="/operations/health" />
});

const routeTree = rootRoute.addChildren([
  loginRoute,
  authenticatedRoute.addChildren([
    overviewRoute,
    pipelinesRoute,
    pipelineDetailRoute,
    modelsRoute,
    endpointsRoute,
    channelsRoute,
    placesRoute,
    edgeDevicesRoute,
    edgeDeviceDetailRoute,
    runtimePipelinesRoute,
    resultsRoute,
    channelDetailRoute,
    endpointDetailRoute,
    operationsRoute,
    accessRoute,
    snapshotsRoute,
    snapshotDetailsRoute,
    validationReportRoute,
    annotationStatisticsRoute,
    projectMappingRoute,
    webhookAuditRoute,
    trainingRoute,
    registryRoute,
    modelTemplatesRoute,
    deploymentsRoute,
    inferenceRoute,
    batchRoute,
    auditRoute,
    settingsRoute,
    healthRoute,
    workersUpdatesRoute,
    datasetsRoute,
    legacyProjectsRoute,
    legacyProjectMappingRoute,
    legacyAnnotationStatisticsRoute,
    legacyWebhookAuditRoute,
    legacySnapshotDetailsRoute,
    legacyValidationReportRoute,
    legacySnapshotsRoute,
    labelingRoute,
    legacyTrainingRoute,
    legacyDeploymentsRoute,
    legacyInferenceRoute,
    legacyBatchRoute,
    legacyAuditRoute,
    legacySettingsRoute,
    legacyHealthRoute
  ])
]);

const routerBasePath = (import.meta.env.VITE_FRONTEND_BASE_PATH || import.meta.env.BASE_URL || '/')
  .replace(/\/+$/, '') || '/';

export const router = createRouter({
  routeTree,
  basepath: routerBasePath
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}
