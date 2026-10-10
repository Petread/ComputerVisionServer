import type { RuntimePlaces } from '@cv-platform/schemas';
import type { EdgeSettingsCommand, EdgeSettingsStatus } from '@cv-platform/schemas';
import type {
  EdgeSoftwareOverview,
  EdgeFleetCommandSummary,
  EdgeSoftwareRolloutRequest,
  AcceptedJob,
  AnnotationSchemaSummary,
  AnnotationStatistics,
  ApiKeyCreateRequest,
  ApiKeyCreateResponse,
  ApiKeyDetail,
  ApiKeyRevokeResponse,
  ApiKeyScopeSummary,
  ApiKeySummary,
  AuditEventDetail,
  AuditEventSummary,
  AnomalyInferenceResponse,
  ApiError,
  BatchJobCreateRequest,
  BatchJobDetail,
  BatchJobSummary,
  BatchResultResponse,
  ClassificationInferenceResponse,
  CleanupExecutionRequest,
  CleanupExecutionResult,
  CleanupImpactPreview,
  DatasetCreateRequest,
  DatasetSnapshotCreateRequest,
  DatasetSnapshotDetail,
  DatasetSnapshotSummary,
  DatasetSummary,
  DeploymentApprovalRecordSummary,
  DeploymentCommandRequest,
  DeploymentModelServingCommandRequest,
  DeploymentPackageBuildRequest,
  DeploymentPackageDetail,
  DeploymentPackageSummary,
  DeploymentReleaseDetail,
  DeploymentReleaseSummary,
  DeploymentSmokeTestRequest,
  DeploymentTargetSummary,
  DetectionInferenceResponse,
  EdgeDeviceDetail,
  EdgeDeviceListResponse,
  EdgeDeviceReconcileResponse,
  EdgeDeviceRegistrationRequest,
  EdgeDeviceUpdateRequest,
  EdgeDeploymentAttemptListResponse,
  EdgeDeploymentAttemptSummary,
  EdgeDeploymentControllerStatus,
  EdgeDeploymentExecuteRequest,
  EdgeDeploymentPreflightReport,
  EdgeDeploymentPreflightRequest,
  EdgeDeploymentWorkflowResponse,
  EdgeDiagnosticReport,
  EdgeFleetHealthOverview,
  EdgeFleetReconcilerStatus,
  EdgeRollbackRequest,
  EdgeRollbackResponse,
  EdgeResultSyncStatus,
  EdgeViewerLeaseRequest,
  EdgeViewerLeaseSummary,
  EdgeWorkloadReleaseDetail,
  EdgeWorkloadReleaseListResponse,
  LabelProjectMappingRequest,
  LabelProjectProvisionRequest,
  LabelProjectSummary,
  LabelStudioTokenUpdateRequest,
  LabelStudioTokenUpdateResponse,
  ListResponse,
  LoginRequest,
  ModelApprovalRequest,
  ModelCapabilitiesResponse,
  ModelFamilySummary,
  ModelLifecycleCommandRequest,
  ModelTemplateCreateRequest,
  ModelTemplateSummary,
  ModelTemplateUpdateRequest,
  ModelStatusResponse,
  ModelVersionComparison,
  ModelVersionDetail,
  ModelVersionSummary,
  OfflineUpdateApplyRequest,
  OfflineUpdateApplyResult,
  OfflineUpdatePlanResponse,
  OfflineUpdateScheduleRequest,
  OfflineUpdateScheduleResult,
  OperationWorkersResponse,
  PlcWriteAccessStatusResponse,
  PlcWriteAccessUpdateRequest,
  ProjectAutomationPolicy,
  ProjectDeployRequest,
  ProjectModelCreateRequest,
  ProjectModelUpdateRequest,
  ProjectModelSummary,
  ProjectModelWorkspaceDetail,
  ProjectPipelineDetail,
  ProjectPipelineSummary,
  ProjectPredictionBackendSummary,
  ProjectTrainRequest,
  PublicAnomalyResponse,
  PublicClassificationResponse,
  PublicDetectionResponse,
  PublicModelDetailResponse,
  PublicModelListResponse,
  PublicSegmentationResponse,
  RealtimeAnomalyRequest,
  RealtimeClassifyRequest,
  RealtimeDetectRequest,
  RealtimeInferenceRequest,
  RealtimeInferenceResponse,
  RealtimeSegmentRequest,
  RuntimeCaptureHistoryResponse,
  RuntimeCameraCalibrationCalculateRequest,
  RuntimeCameraCalibrationCalculateResponse,
  RuntimeImageViewProfileCalculateRequest,
  RuntimeImageViewProfileCalculateResponse,
  RuntimeStemCurvatureProcessCalibrationCalculateRequest,
  RuntimeStemCurvatureProcessCalibrationCalculateResponse,
  RuntimeStemSurfacePhaseCalibrationCalculateRequest,
  RuntimeStemSurfacePhaseCalibrationCalculateResponse,
  RuntimeStemMinimumPrincipleValidationCalculateRequest,
  RuntimeStemMinimumPrincipleValidationCalculateResponse,
  RuntimeChannelCommandRequest,
  RuntimeChannelCommandResponse,
  RuntimeChannelCreateRequest,
  RuntimeChannelAuthoringContextResponse,
  RuntimeChannelDetail,
  RuntimeChannelListResponse,
  RuntimeChannelStreamListResponse,
  RuntimeChannelStreamControlResponse,
  RuntimeChannelStatusOverview,
  RuntimeChannelSummary,
  RuntimeChannelTestFileUploadResponse,
  RuntimeChannelUpdateRequest,
  RuntimeChannelPipelineInstanceRequest,
  RuntimeManualCaptureHistoryResponse,
  RuntimeManualCaptureRequest,
  RuntimeManualCaptureResponse,
  RuntimeManualCaptureSessionResponse,
  RuntimeManualCaptureTargetListResponse,
  RuntimeManualIntervalCaptureStartRequest,
  RuntimeManualIntervalCaptureStopRequest,
  RuntimeLiveViewResponse,
  RuntimeModelCapabilityListResponse,
  RuntimeMode,
  RuntimeNodeCategory,
  RuntimeNodeTypeCatalogResponse,
  RuntimeConnectorDeliveryHistoryResponse,
  RuntimeOutputConnectorCreateRequest,
  RuntimeOutputConnectorDetail,
  RuntimeOutputConnectorListResponse,
  RuntimeOutputConnectorTypeCatalogResponse,
  RuntimeOutputConnectorUpdateRequest,
  RuntimePipelineDefinitionCreateRequest,
  RuntimePipelineDefinitionDetail,
  RuntimePipelineDefinitionListResponse,
  RuntimePipelineDefinitionSummary,
  RuntimePipelinePreviewRequest,
  RuntimePipelineTemplateInstantiateRequest,
  RuntimePipelineValidationRequest,
  RuntimePipelineValidationResponse,
  RuntimePipelineVersionCreateRequest,
  RuntimePipelineVersionDetail,
  RuntimeResultDetailResponse,
  RuntimeResultListResponse,
  RuntimeStemOrientationReviewCreateRequest,
  RuntimeStemOrientationReviewListResponse,
  RuntimeStemOrientationReviewResponse,
  RuntimeStemShadowEvaluationResponse,
  RuntimeTriggerEndpointDetail,
  RuntimeTriggerEndpointListResponse,
  RuntimeTriggerExecutionRequest,
  RuntimeTriggerExecutionResponse,
  RuntimeSourceType,
  SelfPasswordChangeRequest,
  SegmentationInferenceResponse,
  RollbackCommandRequest,
  RollbackTargetRequest,
  SessionResponse,
  SettingsStatusResponse,
  SupportedModelArchitecture,
  SystemStatusResponse,
  TargetPath,
  TaskType,
  TrainingJobCommandRequest,
  TrainingJobCreateRequest,
  TrainingJobDetail,
  TrainingJobSummary,
  TrainingRunComparison,
  UploadPartReceipt,
  UploadSessionCompleteRequest,
  UploadSessionCreateRequest,
  UploadSessionSummary,
  UserCreateRequest,
  UserDetail,
  UserPasswordChangeRequest,
  UserSummary,
  UserUpdateRequest,
  ValidationReport,
  WebhookEventDetail,
  WebhookEventSummary
} from '@cv-platform/schemas';

export interface ApiClientConfig {
  adminApiUrl: string;
  runtimeAdminApiUrl?: string;
  resultServiceUrl?: string;
  inferenceGatewayRealtimeUrl: string;
  inferenceGatewayBatchUrl: string;
  triggerExecutorUrl?: string;
}

class PlatformApiError extends Error {
  readonly payload: ApiError;

  constructor(payload: ApiError) {
    super(payload.message);
    this.payload = payload;
  }
}

function readCookie(name: string) {
  if (typeof document === 'undefined') {
    return null;
  }
  const prefix = `${name}=`;
  return (
    document.cookie
      .split(';')
      .map((item) => item.trim())
      .find((item) => item.startsWith(prefix))
      ?.slice(prefix.length) ?? null
  );
}

async function request<T>(baseUrl: string, path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase();
  const csrfToken =
    method === 'GET' || method === 'HEAD' || method === 'OPTIONS' ? null : readCookie('cv_csrf');
  const response = await fetch(`${baseUrl}${path}`, {
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
      ...(init?.headers ?? {})
    },
    ...init
  });

  if (!response.ok) {
    let payload: ApiError;
    try {
      payload = (await response.json()) as ApiError;
    } catch {
      payload = {
        code: 'http_error',
        message: `Request failed with status ${response.status}`,
        status_code: response.status,
        metadata: {}
      };
    }
    throw new PlatformApiError(payload);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

async function requestBinary(baseUrl: string, path: string, init?: RequestInit): Promise<Blob> {
  const method = (init?.method ?? 'GET').toUpperCase();
  const csrfToken =
    method === 'GET' || method === 'HEAD' || method === 'OPTIONS' ? null : readCookie('cv_csrf');
  const response = await fetch(`${baseUrl}${path}`, {
    credentials: 'include',
    headers: {
      ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
      ...(init?.headers ?? {})
    },
    ...init
  });

  if (!response.ok) {
    let payload: ApiError;
    try {
      payload = (await response.json()) as ApiError;
    } catch {
      payload = {
        code: 'http_error',
        message: `Request failed with status ${response.status}`,
        status_code: response.status,
        metadata: {}
      };
    }
    throw new PlatformApiError(payload);
  }

  return await response.blob();
}

async function requestForm<T>(baseUrl: string, path: string, body: FormData): Promise<T> {
  const csrfToken = readCookie('cv_csrf');
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {})
    },
    body
  });

  if (!response.ok) {
    let payload: ApiError;
    try {
      payload = (await response.json()) as ApiError;
    } catch {
      payload = {
        code: 'http_error',
        message: `Request failed with status ${response.status}`,
        status_code: response.status,
        metadata: {}
      };
    }
    throw new PlatformApiError(payload);
  }

  return (await response.json()) as T;
}

async function requestImageBytes<T>(baseUrl: string, path: string, image: File): Promise<T> {
  const csrfToken = readCookie('cv_csrf');
  const response = await fetch(`${baseUrl}${path}`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
      'Content-Type': image.type || 'application/octet-stream'
    },
    body: image
  });

  if (!response.ok) {
    let payload: ApiError;
    try {
      payload = (await response.json()) as ApiError;
    } catch {
      payload = {
        code: 'http_error',
        message: `Request failed with status ${response.status}`,
        status_code: response.status,
        metadata: {}
      };
    }
    throw new PlatformApiError(payload);
  }

  return (await response.json()) as T;
}

function queryString(pairs: Array<[string, string]>) {
  return new URLSearchParams(pairs).toString();
}

function liveViewQuery(params?: {
  maxFrameWidth?: number;
  includeFrame?: boolean;
  preferCameraStreamFrame?: boolean;
}) {
  const pairs: Array<[string, string]> = [];
  if (params?.maxFrameWidth) {
    pairs.push(['max_frame_width', String(params.maxFrameWidth)]);
  }
  if (params?.includeFrame !== undefined) {
    pairs.push(['include_frame', String(params.includeFrame)]);
  }
  if (params?.preferCameraStreamFrame !== undefined) {
    pairs.push(['prefer_camera_stream_frame', String(params.preferCameraStreamFrame)]);
  }
  const query = queryString(pairs);
  return query ? `?${query}` : '';
}

export function createPlatformApi(config: ApiClientConfig) {
  const runtimeAdminApiUrl = config.runtimeAdminApiUrl ?? config.adminApiUrl;
  const resultServiceUrl = config.resultServiceUrl ?? runtimeAdminApiUrl;
  const triggerExecutorUrl = config.triggerExecutorUrl ?? runtimeAdminApiUrl;
  return {
    admin: {
      login: (payload: LoginRequest) =>
        request<SessionResponse>(config.adminApiUrl, '/api/v1/auth/login', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      logout: () =>
        request<SessionResponse>(config.adminApiUrl, '/api/v1/auth/logout', {
          method: 'POST'
        }),
      me: () => request<SessionResponse>(config.adminApiUrl, '/api/v1/auth/me'),
      systemStatus: () =>
        request<SystemStatusResponse>(config.adminApiUrl, '/api/v1/system/status'),
      operationWorkers: () =>
        request<OperationWorkersResponse>(config.adminApiUrl, '/api/v1/operations/workers'),
      restartOperationWorker: (serviceName: string) =>
        request<OperationWorkersResponse>(
          config.adminApiUrl,
          `/api/v1/operations/workers/${encodeURIComponent(serviceName)}:restart`,
          {
            method: 'POST'
          }
        ),
      offlineUpdatePlan: () =>
        request<OfflineUpdatePlanResponse>(config.adminApiUrl, '/api/v1/operations/offline-update/plan'),
      markOfflineUpdateCurrent: () =>
        request<OfflineUpdatePlanResponse>(
          config.adminApiUrl,
          '/api/v1/operations/offline-update:mark-current',
          {
            method: 'POST'
          }
        ),
      applyOfflineUpdate: (payload: OfflineUpdateApplyRequest) =>
        request<OfflineUpdateApplyResult>(
          config.adminApiUrl,
          '/api/v1/operations/offline-update:apply',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      scheduleOfflineUpdate: (payload: OfflineUpdateScheduleRequest) =>
        request<OfflineUpdateScheduleResult>(
          config.adminApiUrl,
          '/api/v1/operations/offline-update:schedule',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      auditEvents: (params?: {
        aggregateType?: string;
        aggregateId?: string;
        eventType?: string;
        sourceService?: string;
        limit?: number;
      }) =>
        request<ListResponse<AuditEventSummary>>(
          config.adminApiUrl,
          `/api/v1/audit-events${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    aggregate_type: params.aggregateType,
                    aggregate_id: params.aggregateId,
                    event_type: params.eventType,
                    source_service: params.sourceService,
                    limit: params.limit ? String(params.limit) : undefined
                  }).filter(([, value]) => value !== undefined) as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      auditEventDetail: (eventId: string) =>
        request<AuditEventDetail>(config.adminApiUrl, `/api/v1/audit-events/${eventId}`),
      webhookEvents: () =>
        request<ListResponse<WebhookEventSummary>>(config.adminApiUrl, '/api/v1/webhook-events'),
      webhookEventDetail: (eventId: string) =>
        request<WebhookEventDetail>(config.adminApiUrl, `/api/v1/webhook-events/${eventId}`),
      integrationStatus: () =>
        request<SettingsStatusResponse>(config.adminApiUrl, '/api/v1/settings/integration-status'),
      updateLabelStudioToken: (payload: LabelStudioTokenUpdateRequest) =>
        request<LabelStudioTokenUpdateResponse>(
          config.adminApiUrl,
          '/api/v1/settings/label-studio-token',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      users: (params?: { active?: boolean }) =>
        request<ListResponse<UserSummary>>(
          config.adminApiUrl,
          `/api/v1/users${
            params?.active === undefined
              ? ''
              : `?${new URLSearchParams({ active: String(params.active) }).toString()}`
          }`
        ),
      userDetail: (userId: string) =>
        request<UserDetail>(config.adminApiUrl, `/api/v1/users/${userId}`),
      createUser: (payload: UserCreateRequest) =>
        request<UserDetail>(config.adminApiUrl, '/api/v1/users', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      updateUser: (userId: string, payload: UserUpdateRequest) =>
        request<UserDetail>(config.adminApiUrl, `/api/v1/users/${userId}`, {
          method: 'PUT',
          body: JSON.stringify(payload)
        }),
      deactivateUser: (userId: string) =>
        request<UserDetail>(config.adminApiUrl, `/api/v1/users/${userId}:deactivate`, {
          method: 'POST'
        }),
      reactivateUser: (userId: string) =>
        request<UserDetail>(config.adminApiUrl, `/api/v1/users/${userId}:reactivate`, {
          method: 'POST'
        }),
      changeUserPassword: (userId: string, payload: UserPasswordChangeRequest) =>
        request<UserDetail>(config.adminApiUrl, `/api/v1/users/${userId}:change-password`, {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      changeOwnPassword: (payload: SelfPasswordChangeRequest) =>
        request<SessionResponse>(config.adminApiUrl, '/api/v1/users/me/change-password', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      apiKeyScopes: () =>
        request<ListResponse<ApiKeyScopeSummary>>(config.adminApiUrl, '/api/v1/api-key-scopes'),
      plcWriteAccess: () =>
        request<PlcWriteAccessStatusResponse>(config.adminApiUrl, '/api/v1/plc-write-access'),
      updatePlcWriteAccess: (payload: PlcWriteAccessUpdateRequest) =>
        request<PlcWriteAccessStatusResponse>(config.adminApiUrl, '/api/v1/plc-write-access', {
          method: 'PUT',
          body: JSON.stringify(payload)
        }),
      apiKeys: (params?: {
        state?: 'all' | 'active' | 'revoked' | 'expired';
        sourceType?: 'bootstrap' | 'managed';
        scope?: string;
        search?: string;
      }) =>
        request<ListResponse<ApiKeySummary>>(
          config.adminApiUrl,
          `/api/v1/api-keys${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    state: params.state,
                    source_type: params.sourceType,
                    scope: params.scope,
                    search: params.search
                  }).filter(([, value]) => value !== undefined && value !== '') as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      apiKeyDetail: (apiKeyId: string) =>
        request<ApiKeyDetail>(config.adminApiUrl, `/api/v1/api-keys/${apiKeyId}`),
      createApiKey: (payload: ApiKeyCreateRequest) =>
        request<ApiKeyCreateResponse>(config.adminApiUrl, '/api/v1/api-keys', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      revokeApiKey: (apiKeyId: string) =>
        request<ApiKeyRevokeResponse>(config.adminApiUrl, `/api/v1/api-keys/${apiKeyId}:revoke`, {
          method: 'POST'
        })
    },
    datasets: {
      list: () => request<ListResponse<DatasetSummary>>(config.adminApiUrl, '/api/v1/datasets'),
      create: (payload: DatasetCreateRequest) =>
        request<DatasetSummary>(config.adminApiUrl, '/api/v1/datasets', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      createUploadSession: (datasetId: string, payload: UploadSessionCreateRequest) =>
        request<UploadSessionSummary>(
          config.adminApiUrl,
          `/api/v1/datasets/${datasetId}/upload-sessions`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      uploadSession: (uploadSessionId: string) =>
        request<UploadSessionSummary>(
          config.adminApiUrl,
          `/api/v1/upload-sessions/${uploadSessionId}`
        ),
      uploadPart: async (
        uploadSessionId: string,
        partNo: number,
        fileName: string,
        payload: Blob | ArrayBuffer | Uint8Array,
        contentType: string
      ) => {
        const csrfToken = readCookie('cv_csrf');
        const response = await fetch(
          `${config.adminApiUrl}/api/v1/upload-sessions/${uploadSessionId}/parts/${partNo}`,
          {
            method: 'PUT',
            credentials: 'include',
            headers: {
              ...(csrfToken ? { 'X-CSRF-Token': csrfToken } : {}),
              'Content-Type': contentType,
              'X-Upload-Filename': fileName
            },
            body: toUploadBody(payload, contentType)
          }
        );

        if (!response.ok) {
          let errorPayload: ApiError;
          try {
            errorPayload = (await response.json()) as ApiError;
          } catch {
            errorPayload = {
              code: 'http_error',
              message: `Request failed with status ${response.status}`,
              status_code: response.status,
              metadata: {}
            };
          }
          throw new PlatformApiError(errorPayload);
        }

        return (await response.json()) as UploadPartReceipt;
      },
      completeUploadSession: (uploadSessionId: string, payload: UploadSessionCompleteRequest) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/upload-sessions/${uploadSessionId}:complete`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      projects: () =>
        request<ListResponse<LabelProjectSummary>>(config.adminApiUrl, '/api/v1/label-projects'),
      annotationSchemas: () =>
        request<ListResponse<AnnotationSchemaSummary>>(
          config.adminApiUrl,
          '/api/v1/annotation-schemas'
        ),
      provisionProject: (payload: LabelProjectProvisionRequest) =>
        request<AcceptedJob>(config.adminApiUrl, '/api/v1/label-projects:provision', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      mapProject: (payload: LabelProjectMappingRequest) =>
        request<LabelProjectSummary>(config.adminApiUrl, '/api/v1/label-projects', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      syncProjects: () =>
        request<AcceptedJob>(config.adminApiUrl, '/api/v1/label-projects/sync', {
          method: 'POST'
        }),
      allSnapshots: (datasetId?: string) =>
        request<ListResponse<DatasetSnapshotSummary>>(
          config.adminApiUrl,
          `/api/v1/dataset-snapshots${
            datasetId ? `?${new URLSearchParams({ dataset_id: datasetId }).toString()}` : ''
          }`
        ),
      snapshots: (datasetId: string) =>
        request<ListResponse<DatasetSnapshotSummary>>(
          config.adminApiUrl,
          `/api/v1/datasets/${datasetId}/snapshots`
        ),
      createSnapshot: (payload: DatasetSnapshotCreateRequest) =>
        request<AcceptedJob>(config.adminApiUrl, '/api/v1/dataset-snapshots', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      snapshotDetail: (snapshotId: string) =>
        request<DatasetSnapshotDetail>(config.adminApiUrl, `/api/v1/dataset-snapshots/${snapshotId}`),
      snapshotCleanupPreview: (snapshotId: string) =>
        request<CleanupImpactPreview>(
          config.adminApiUrl,
          `/api/v1/dataset-snapshots/${snapshotId}/cleanup-preview`
        ),
      cleanupSnapshot: (snapshotId: string, payload: CleanupExecutionRequest) =>
        request<CleanupExecutionResult>(
          config.adminApiUrl,
          `/api/v1/dataset-snapshots/${snapshotId}:cleanup`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      snapshotValidation: (snapshotId: string) =>
        request<ValidationReport>(config.adminApiUrl, `/api/v1/dataset-snapshots/${snapshotId}/validation-report`),
      datasetStats: (datasetId: string) =>
        request<AnnotationStatistics>(config.adminApiUrl, `/api/v1/datasets/${datasetId}/annotation-statistics`),
      projectStats: (labelProjectId: string) =>
        request<AnnotationStatistics>(
          config.adminApiUrl,
          `/api/v1/label-projects/${labelProjectId}/annotation-statistics`
        )
    },
    pipelines: {
      refresh: () =>
        request<AcceptedJob>(config.adminApiUrl, '/api/v1/project-pipelines:refresh', {
          method: 'POST'
        }),
      list: () =>
        request<ListResponse<ProjectPipelineSummary>>(
          config.adminApiUrl,
          '/api/v1/project-pipelines'
        ),
      detail: (labelProjectId: string) =>
        request<ProjectPipelineDetail>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}`
        ),
      modelTemplates: (taskType?: TaskType) =>
        request<ListResponse<ModelTemplateSummary>>(
          config.adminApiUrl,
          `/api/v1/model-templates${
            taskType
              ? `?${new URLSearchParams({ task_type: taskType }).toString()}`
              : ''
          }`
        ),
      modelCapabilities: (taskType?: TaskType) =>
        request<ListResponse<SupportedModelArchitecture>>(
          config.adminApiUrl,
          `/api/v1/model-capabilities${
            taskType
              ? `?${new URLSearchParams({ task_type: taskType }).toString()}`
              : ''
          }`
        ),
      createModelTemplate: (payload: ModelTemplateCreateRequest) =>
        request<ModelTemplateSummary>(config.adminApiUrl, '/api/v1/model-templates', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      updateModelTemplate: (templateKey: string, payload: ModelTemplateUpdateRequest) =>
        request<ModelTemplateSummary>(config.adminApiUrl, `/api/v1/model-templates/${templateKey}`, {
          method: 'PUT',
          body: JSON.stringify(payload)
        }),
      sync: (labelProjectId: string) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}:sync`,
          {
            method: 'POST'
          }
        ),
      updatePolicy: (labelProjectId: string, payload: ProjectAutomationPolicy) =>
        request<ProjectAutomationPolicy>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}/automation-policy`,
          {
            method: 'PUT',
            body: JSON.stringify(payload)
          }
        ),
      createModel: (labelProjectId: string, payload: ProjectModelCreateRequest) =>
        request<ProjectModelSummary>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}/models`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      modelWorkspace: (labelProjectId: string, modelFamilyId: string) =>
        request<ProjectModelWorkspaceDetail>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}/models/${modelFamilyId}`
        ),
      updateModel: (
        labelProjectId: string,
        modelFamilyId: string,
        payload: ProjectModelUpdateRequest
      ) =>
        request<ProjectModelSummary>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}/models/${modelFamilyId}`,
          {
            method: 'PATCH',
            body: JSON.stringify(payload)
          }
        ),
      train: (labelProjectId: string, payload?: ProjectTrainRequest) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}:train`,
          {
            method: 'POST',
            body: JSON.stringify(
              payload ?? {
                allow_duplicate_snapshot_training: false
              }
            )
          }
        ),
      retrainLatest: (labelProjectId: string) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}:retrain-latest`,
          {
            method: 'POST'
          }
        ),
      deployLatest: (labelProjectId: string, payload?: ProjectDeployRequest) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}:deploy-latest`,
          {
            method: 'POST',
            body: JSON.stringify(payload ?? {})
          }
        ),
      deployModel: (
        labelProjectId: string,
        modelFamilyId: string,
        payload?: ProjectDeployRequest
      ) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}/models/${modelFamilyId}:deploy`,
          {
            method: 'POST',
            body: JSON.stringify(payload ?? {})
          }
        ),
      linkLabelStudioPredictions: (labelProjectId: string, modelFamilyId?: string) =>
        request<ProjectPredictionBackendSummary>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}:link-labelstudio-predictions${
            modelFamilyId ? `?${new URLSearchParams({ model_family_id: modelFamilyId }).toString()}` : ''
          }`,
          {
            method: 'POST'
          }
        ),
      cleanupPreview: (labelProjectId: string, params?: { deleteEntirePipeline?: boolean }) =>
        request<CleanupImpactPreview>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}/cleanup-preview${
            params?.deleteEntirePipeline
              ? `?${new URLSearchParams({ delete_entire_pipeline: 'true' }).toString()}`
              : ''
          }`
        ),
      cleanup: (labelProjectId: string, payload: CleanupExecutionRequest) =>
        request<CleanupExecutionResult>(
          config.adminApiUrl,
          `/api/v1/project-pipelines/${labelProjectId}:cleanup`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        )
    },
    places: {
      list: () => request<RuntimePlaces>(runtimeAdminApiUrl, '/api/v1/runtime/places'),
      save: (kind: 'areas' | 'facilities', payload: { name: string; area_id?: string }, id?: string) => request<RuntimePlaces>(runtimeAdminApiUrl, '/api/v1/runtime/' + kind + (id ? '/' + encodeURIComponent(id) : ''), { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) }),
      remove: (kind: 'areas' | 'facilities', id: string) => request<RuntimePlaces>(runtimeAdminApiUrl, '/api/v1/runtime/' + kind + '/' + encodeURIComponent(id), { method: 'DELETE' })
    },
    channels: {
      list: (params?: {
        search?: string;
        sourceType?: RuntimeSourceType;
        mode?: RuntimeMode;
        status?: string;
        technicalPlace?: string;
        executionTarget?: 'server' | 'edge_device';
      }) =>
        request<RuntimeChannelListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    search: params.search,
                    source_type: params.sourceType,
                    mode: params.mode,
                    status: params.status,
                    technical_place: params.technicalPlace,
                    execution_target: params.executionTarget
                  }).filter(([, value]) => value !== undefined && value !== '') as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      statusOverview: () =>
        request<RuntimeChannelStatusOverview>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/channels/status'
        ),
      detail: (channelId: string) =>
        request<RuntimeChannelDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}`
        ),
      authoringContext: (channelId: string) =>
        request<RuntimeChannelAuthoringContextResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/authoring-context`
        ),
      pipelineInstance: (channelId: string, payload: RuntimeChannelPipelineInstanceRequest) =>
        request<RuntimePipelineDefinitionDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/pipeline-instance`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      status: (channelId: string) =>
        request<RuntimeChannelSummary>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/status`
        ),
      create: (payload: RuntimeChannelCreateRequest) =>
        request<RuntimeChannelDetail>(runtimeAdminApiUrl, '/api/v1/runtime/channels', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      uploadTestFile: (file: File) => {
        const form = new FormData();
        form.append('file', file);
        return requestForm<RuntimeChannelTestFileUploadResponse>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/channel-test-files',
          form
        );
      },
      update: (channelId: string, payload: RuntimeChannelUpdateRequest) =>
        request<RuntimeChannelDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}`,
          {
            method: 'PUT',
            body: JSON.stringify(payload)
          }
        ),
      delete: (channelId: string, reason?: string) =>
        request<RuntimeChannelSummary>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}${
            reason ? `?${new URLSearchParams({ reason }).toString()}` : ''
          }`,
          {
            method: 'DELETE'
          }
        ),
      start: (channelId: string, payload?: RuntimeChannelCommandRequest) =>
        request<RuntimeChannelCommandResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}:start`,
          {
            method: 'POST',
            body: JSON.stringify(payload ?? {})
          }
        ),
      stop: (channelId: string, payload?: RuntimeChannelCommandRequest) =>
        request<RuntimeChannelCommandResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}:stop`,
          {
            method: 'POST',
            body: JSON.stringify(payload ?? {})
          }
        ),
      restart: (channelId: string, payload?: RuntimeChannelCommandRequest) =>
        request<RuntimeChannelCommandResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}:restart`,
          {
            method: 'POST',
            body: JSON.stringify(payload ?? {})
          }
        ),
      liveViewLatest: (
        channelId: string,
        params?: {
          maxFrameWidth?: number;
          includeFrame?: boolean;
          preferCameraStreamFrame?: boolean;
        }
      ) =>
        request<RuntimeLiveViewResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/live-view/latest${liveViewQuery(params)}`
        ),
      acquireEdgeViewerLease: (channelId: string, payload: EdgeViewerLeaseRequest) =>
        request<EdgeViewerLeaseSummary>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/edge-viewer-leases`,
          { method: 'POST', body: JSON.stringify(payload) }
        ),
      releaseEdgeViewerLease: (channelId: string, leaseId: string) =>
        request<EdgeViewerLeaseSummary>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/edge-viewer-leases/${leaseId}`,
          { method: 'DELETE' }
        ),
      captureCalibrationFrame: (channelId: string, timeoutMs = 8000) =>
        request<RuntimeLiveViewResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/calibration-frame?${new URLSearchParams({
            timeout_ms: String(timeoutMs)
          }).toString()}`,
          { method: 'POST' }
        ),
      calculateCameraCalibration: (
        channelId: string,
        payload: RuntimeCameraCalibrationCalculateRequest
      ) =>
        request<RuntimeCameraCalibrationCalculateResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/camera-calibration/calculate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      calculateStemProcessCalibration: (
        channelId: string,
        payload: RuntimeStemCurvatureProcessCalibrationCalculateRequest
      ) =>
        request<RuntimeStemCurvatureProcessCalibrationCalculateResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/stem-process-calibration/calculate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      calculateImageViewProfile: (
        channelId: string,
        payload: RuntimeImageViewProfileCalculateRequest
      ) =>
        request<RuntimeImageViewProfileCalculateResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/image-view-profile/calculate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      calculateStemSurfacePhaseCalibration: (
        channelId: string,
        payload: RuntimeStemSurfacePhaseCalibrationCalculateRequest
      ) =>
        request<RuntimeStemSurfacePhaseCalibrationCalculateResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/stem-surface-phase-calibration/calculate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      calculateStemMinimumPrincipleValidation: (
        channelId: string,
        payload: RuntimeStemMinimumPrincipleValidationCalculateRequest
      ) =>
        request<RuntimeStemMinimumPrincipleValidationCalculateResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/stem-minimum-principle-validation/calculate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      streams: (channelId: string) =>
        request<RuntimeChannelStreamListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/streams`
        ),
      startStream: (channelId: string) =>
        request<RuntimeChannelStreamControlResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/stream/start`,
          { method: 'POST' }
        ),
      stopStream: (channelId: string) =>
        request<RuntimeChannelStreamControlResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/stream/stop`,
          { method: 'POST' }
        ),
      restartStream: (channelId: string) =>
        request<RuntimeChannelStreamControlResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/stream/restart`,
          { method: 'POST' }
        ),
      overlayEventsUrl: (channelId: string) =>
        `${runtimeAdminApiUrl}/api/v1/runtime/channels/${channelId}/overlay/events`,
      pipelinePreview: (channelId: string, payload: RuntimePipelinePreviewRequest) =>
        request<RuntimeLiveViewResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/pipeline-preview`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      manualCaptureTargets: (channelId: string) =>
        request<RuntimeManualCaptureTargetListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/manual-capture/targets`
        ),
      manualCaptureOnce: (channelId: string, payload: RuntimeManualCaptureRequest) =>
        request<RuntimeManualCaptureResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/manual-capture/single`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      manualCaptureSession: (channelId: string) =>
        request<RuntimeManualCaptureSessionResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/manual-capture/session`
        ),
      manualCaptureHistory: (channelId: string, limit?: number) =>
        request<RuntimeManualCaptureHistoryResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/manual-capture/history${
            limit ? `?${new URLSearchParams({ limit: String(limit) }).toString()}` : ''
          }`
        ),
      captureDeliveryHistory: (channelId: string, limit?: number) =>
        request<RuntimeCaptureHistoryResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/captures/history${
            limit ? `?${new URLSearchParams({ limit: String(limit) }).toString()}` : ''
          }`
        ),
      outputConnectors: (channelId: string) =>
        request<RuntimeOutputConnectorListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/output-connectors`
        ),
      connectorDeliveryHistory: (channelId: string, limit?: number) =>
        request<RuntimeConnectorDeliveryHistoryResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/connector-deliveries${
            limit ? `?${new URLSearchParams({ limit: String(limit) }).toString()}` : ''
          }`
        ),
      startManualCaptureInterval: (
        channelId: string,
        payload: RuntimeManualIntervalCaptureStartRequest
      ) =>
        request<RuntimeManualCaptureSessionResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/manual-capture/interval-session`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      stopManualCaptureInterval: (
        channelId: string,
        payload?: RuntimeManualIntervalCaptureStopRequest
      ) =>
        request<RuntimeManualCaptureSessionResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/channels/${channelId}/manual-capture/interval-session:stop`,
          {
            method: 'POST',
            body: JSON.stringify(payload ?? {})
          }
        )
    },
    edgeDevices: {
      settings: (deviceId: string) => request<EdgeSettingsStatus>(runtimeAdminApiUrl,
        `/api/v1/runtime/edge-devices/${deviceId}/settings`),
      updateSettings: (deviceId: string, payload: EdgeSettingsCommand) => request<EdgeSettingsStatus>(runtimeAdminApiUrl,
        `/api/v1/runtime/edge-devices/${deviceId}/settings`, { method: 'PUT', body: JSON.stringify(payload) }),
      software: () => request<EdgeSoftwareOverview>(runtimeAdminApiUrl, '/api/v1/runtime/edge-software'),
      uploadSoftware: (file: File) => {
        const body = new FormData();
        body.append('file', file);
        return requestForm<EdgeSoftwareOverview>(runtimeAdminApiUrl, '/api/v1/runtime/edge-software/packages', body);
      },
      updateSoftware: (deviceId: string, payload: EdgeSoftwareRolloutRequest) =>
        request<EdgeFleetCommandSummary>(runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices/${deviceId}/software`,
          { method: 'POST', body: JSON.stringify(payload) }),
      fleetHealth: () =>
        request<EdgeFleetHealthOverview>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/edge-fleet/health'
        ),
      preflightDeployment: (payload: EdgeDeploymentPreflightRequest) =>
        request<EdgeDeploymentPreflightReport>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/edge-operator/deployments:preflight',
          { method: 'POST', body: JSON.stringify(payload) }
        ),
      executeDeployment: (payload: EdgeDeploymentExecuteRequest) =>
        request<EdgeDeploymentWorkflowResponse>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/edge-operator/deployments',
          { method: 'POST', body: JSON.stringify(payload) }
        ),
      rollbackDeployment: (edgeDeploymentId: string, payload: EdgeRollbackRequest) =>
        request<EdgeRollbackResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-deployments/${edgeDeploymentId}:rollback`,
          { method: 'POST', body: JSON.stringify(payload) }
        ),
      runDiagnostics: (edgeDeviceId: string, refreshDevice = true) =>
        request<EdgeDiagnosticReport>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices/${edgeDeviceId}/diagnostics`,
          { method: 'POST', body: JSON.stringify({ refresh_device: refreshDevice }) }
        ),
      latestDiagnostics: (edgeDeviceId: string) =>
        request<EdgeDiagnosticReport>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices/${edgeDeviceId}/diagnostics/latest`
        ),
      resultSyncStatus: () =>
        request<EdgeResultSyncStatus>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/edge-result-sync'
        ),
      deploymentControllerStatus: () =>
        request<EdgeDeploymentControllerStatus>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/edge-deployment-controller'
        ),
      workloadReleases: (params?: { channelId?: string; edgeDeviceId?: string }) =>
        request<EdgeWorkloadReleaseListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-workload-releases${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    channel_id: params.channelId,
                    edge_device_id: params.edgeDeviceId
                  }).filter(([, value]) => value) as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      workloadReleaseDetail: (workloadReleaseId: string) =>
        request<EdgeWorkloadReleaseDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-workload-releases/${workloadReleaseId}`
        ),
      deployments: (params?: { channelId?: string; edgeDeviceId?: string }) =>
        request<EdgeDeploymentAttemptListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-deployments${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    channel_id: params.channelId,
                    edge_device_id: params.edgeDeviceId
                  }).filter(([, value]) => value) as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      deploymentDetail: (edgeDeploymentId: string) =>
        request<EdgeDeploymentAttemptSummary>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-deployments/${edgeDeploymentId}`
        ),
      reconcilerStatus: () =>
        request<EdgeFleetReconcilerStatus>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/edge-reconciler'
        ),
      list: (params?: {
        search?: string;
        connectivityState?: string;
        enabled?: boolean;
      }) =>
        request<EdgeDeviceListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    search: params.search,
                    connectivity_state: params.connectivityState,
                    enabled: params.enabled === undefined ? undefined : String(params.enabled)
                  }).filter(([, value]) => value !== undefined && value !== '') as Array<[
                    string,
                    string
                  ]>
                ).toString()}`
              : ''
          }`
        ),
      detail: (edgeDeviceId: string) =>
        request<EdgeDeviceDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices/${edgeDeviceId}`
        ),
      register: (payload: EdgeDeviceRegistrationRequest) =>
        request<EdgeDeviceDetail>(runtimeAdminApiUrl, '/api/v1/runtime/edge-devices', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      update: (edgeDeviceId: string, payload: EdgeDeviceUpdateRequest) =>
        request<EdgeDeviceDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices/${edgeDeviceId}`,
          { method: 'PUT', body: JSON.stringify(payload) }
        ),
      archive: (edgeDeviceId: string) =>
        request<EdgeDeviceDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices/${edgeDeviceId}`,
          { method: 'DELETE' }
        ),
      reconcile: (edgeDeviceId: string, idempotencyKey: string) =>
        request<EdgeDeviceReconcileResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/edge-devices/${edgeDeviceId}:reconcile`,
          {
            method: 'POST',
            body: JSON.stringify({ idempotency_key: idempotencyKey })
          }
        )
    },
    outputConnectors: {
      types: () =>
        request<RuntimeOutputConnectorTypeCatalogResponse>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/output-connectors/types'
        ),
      list: (params?: { channelId?: string; connectorType?: string }) =>
        request<RuntimeOutputConnectorListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/output-connectors${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    channel_id: params.channelId,
                    connector_type: params.connectorType
                  }).filter(([, value]) => value !== undefined && value !== '') as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      create: (payload: RuntimeOutputConnectorCreateRequest) =>
        request<RuntimeOutputConnectorDetail>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/output-connectors',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      update: (outputConnectorId: string, payload: RuntimeOutputConnectorUpdateRequest) =>
        request<RuntimeOutputConnectorDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/output-connectors/${outputConnectorId}`,
          {
            method: 'PUT',
            body: JSON.stringify(payload)
          }
        )
    },
    runtimeResults: {
      artifact: (resultId: string, name: string) =>
        requestBinary(resultServiceUrl, `/api/v1/runtime/results/${encodeURIComponent(resultId)}/artifacts/${encodeURIComponent(name)}`),
      list: (params?: {
        channelId?: string;
        pipelineVersionId?: string;
        mode?: RuntimeMode;
        status?: 'completed' | 'failed' | 'timeout';
        resultType?: string;
        completedFrom?: string;
        completedTo?: string;
        search?: string;
        limit?: number;
        offset?: number;
      }) =>
        request<RuntimeResultListResponse>(
          resultServiceUrl,
          `/api/v1/runtime/results${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    channel_id: params.channelId,
                    pipeline_version_id: params.pipelineVersionId,
                    mode: params.mode,
                    status: params.status,
                    result_type: params.resultType,
                    completed_from: params.completedFrom,
                    completed_to: params.completedTo,
                    search: params.search,
                    limit: params.limit ? String(params.limit) : undefined,
                    offset: params.offset ? String(params.offset) : undefined
                  }).filter(([, value]) => value !== undefined && value !== '') as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      detail: (resultRecordId: string) =>
        request<RuntimeResultDetailResponse>(
          resultServiceUrl,
          `/api/v1/runtime/results/${resultRecordId}`
        ),
      stemOrientationReviews: (resultRecordId: string) =>
        request<RuntimeStemOrientationReviewListResponse>(
          resultServiceUrl,
          `/api/v1/runtime/results/${resultRecordId}/stem-orientation-reviews`
        ),
      appendStemOrientationReview: (
        resultRecordId: string,
        payload: RuntimeStemOrientationReviewCreateRequest
      ) =>
        request<RuntimeStemOrientationReviewResponse>(
          resultServiceUrl,
          `/api/v1/runtime/results/${resultRecordId}/stem-orientation-reviews`,
          { method: 'POST', body: JSON.stringify(payload) }
        ),
      stemShadowEvaluation: (
        pipelineVersionId: string,
        params?: { completedFrom?: string; completedTo?: string; maximumSessions?: number }
      ) =>
        request<RuntimeStemShadowEvaluationResponse>(
          resultServiceUrl,
          `/api/v1/runtime/pipeline-versions/${pipelineVersionId}/stem-shadow-evaluation${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    completed_from: params.completedFrom,
                    completed_to: params.completedTo,
                    maximum_sessions: params.maximumSessions
                      ? String(params.maximumSessions)
                      : undefined
                  }).filter(([, value]) => value !== undefined && value !== '') as Array<[
                    string,
                    string
                  ]>
                ).toString()}`
              : ''
          }`
        ),
      latestForChannel: (channelId: string) =>
        request<RuntimeResultDetailResponse>(
          resultServiceUrl,
          `/api/v1/runtime/channels/${channelId}/results/latest`
        )
    },
    triggerEndpoints: {
      list: (params?: { channelId?: string }) =>
        request<RuntimeTriggerEndpointListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/trigger-endpoints${
            params?.channelId
              ? `?${new URLSearchParams({ channel_id: params.channelId }).toString()}`
              : ''
          }`
        ),
      detail: (triggerEndpointId: string) =>
        request<RuntimeTriggerEndpointDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/trigger-endpoints/${triggerEndpointId}`
        ),
      execute: (
        endpointSlug: string,
        payload?: RuntimeTriggerExecutionRequest,
        machineToken?: string
      ) =>
        request<RuntimeTriggerExecutionResponse>(
          triggerExecutorUrl,
          `/api/v1/runtime/triggers/${endpointSlug}`,
          {
            method: 'PUT',
            headers: machineToken ? { Authorization: `Bearer ${machineToken}` } : undefined,
            body: JSON.stringify(payload ?? { payload: {} })
          }
        )
    },
    runtimePipelines: {
      nodeTypes: (params?: { category?: RuntimeNodeCategory; mode?: RuntimeMode }) =>
        request<RuntimeNodeTypeCatalogResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/node-types${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    category: params.category,
                    mode: params.mode
                  }).filter(([, value]) => value !== undefined) as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      modelCapabilities: (params?: { taskType?: TaskType; targetPath?: TargetPath }) =>
        request<RuntimeModelCapabilityListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/model-capabilities${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    task_type: params.taskType,
                    target_path: params.targetPath
                  }).filter(([, value]) => value !== undefined) as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      list: (params?: {
        includeTemplates?: boolean;
        templatesOnly?: boolean;
        scope?: 'template' | 'channel_instance';
        ownerChannelId?: string;
      }) =>
        request<RuntimePipelineDefinitionListResponse>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/pipeline-definitions${
            params
              ? `?${new URLSearchParams(
                  Object.entries({
                    include_templates:
                      params.includeTemplates === undefined
                        ? undefined
                        : String(params.includeTemplates),
                    templates_only:
                      params.templatesOnly === undefined ? undefined : String(params.templatesOnly),
                    scope: params.scope,
                    owner_channel_id: params.ownerChannelId
                  }).filter(([, value]) => value !== undefined && value !== '') as Array<[string, string]>
                ).toString()}`
              : ''
          }`
        ),
      detail: (pipelineDefinitionId: string) =>
        request<RuntimePipelineDefinitionDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/pipeline-definitions/${pipelineDefinitionId}`
        ),
      create: (payload: RuntimePipelineDefinitionCreateRequest) =>
        request<RuntimePipelineDefinitionDetail>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/pipeline-definitions',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      delete: (pipelineDefinitionId: string, reason?: string) =>
        request<RuntimePipelineDefinitionSummary>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/pipeline-definitions/${pipelineDefinitionId}${
            reason ? `?${new URLSearchParams({ reason }).toString()}` : ''
          }`,
          {
            method: 'DELETE'
          }
        ),
      createVersion: (pipelineDefinitionId: string, payload: RuntimePipelineVersionCreateRequest) =>
        request<RuntimePipelineVersionDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/pipeline-definitions/${pipelineDefinitionId}/versions`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      instantiateTemplate: (templateId: string, payload: RuntimePipelineTemplateInstantiateRequest) =>
        request<RuntimePipelineDefinitionDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/pipeline-templates/${templateId}:instantiate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      validate: (payload: RuntimePipelineValidationRequest) =>
        request<RuntimePipelineValidationResponse>(
          runtimeAdminApiUrl,
          '/api/v1/runtime/pipeline-versions:validate',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      validateVersion: (pipelineVersionId: string) =>
        request<RuntimePipelineVersionDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/pipeline-versions/${pipelineVersionId}:validate`,
          {
            method: 'POST'
          }
        ),
      publishVersion: (pipelineVersionId: string) =>
        request<RuntimePipelineVersionDetail>(
          runtimeAdminApiUrl,
          `/api/v1/runtime/pipeline-versions/${pipelineVersionId}:publish`,
          {
            method: 'POST'
          }
        )
    },
    webhooks: {
      list: () =>
        request<ListResponse<WebhookEventSummary>>(config.adminApiUrl, '/api/v1/webhook-events'),
      detail: (eventId: string) =>
        request<WebhookEventDetail>(config.adminApiUrl, `/api/v1/webhook-events/${eventId}`)
    },
    training: {
      list: () => request<ListResponse<TrainingJobSummary>>(config.adminApiUrl, '/api/v1/training-jobs'),
      detail: (trainingJobId: string) =>
        request<TrainingJobDetail>(config.adminApiUrl, `/api/v1/training-jobs/${trainingJobId}`),
      compare: (jobIds: string[]) =>
        request<TrainingRunComparison>(
          config.adminApiUrl,
          `/api/v1/training-jobs/compare?${queryString(jobIds.map((jobId) => ['job_ids', jobId]))}`
        ),
      create: (payload: TrainingJobCreateRequest) =>
        request<AcceptedJob>(config.adminApiUrl, '/api/v1/training-jobs', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      cancel: (trainingJobId: string, payload: TrainingJobCommandRequest) =>
        request<AcceptedJob>(config.adminApiUrl, `/api/v1/training-jobs/${trainingJobId}:cancel`, {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      resume: (trainingJobId: string, payload: TrainingJobCommandRequest) =>
        request<AcceptedJob>(config.adminApiUrl, `/api/v1/training-jobs/${trainingJobId}:resume`, {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      cleanupPreview: (trainingJobId: string) =>
        request<CleanupImpactPreview>(
          config.adminApiUrl,
          `/api/v1/training-jobs/${trainingJobId}/cleanup-preview`
        ),
      cleanup: (trainingJobId: string, payload: CleanupExecutionRequest) =>
        request<CleanupExecutionResult>(
          config.adminApiUrl,
          `/api/v1/training-jobs/${trainingJobId}:cleanup`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        )
    },
    registry: {
      families: () => request<ListResponse<ModelFamilySummary>>(config.adminApiUrl, '/api/v1/registry/model-families'),
      versions: (familySlug?: string) =>
        request<ListResponse<ModelVersionSummary>>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions${
            familySlug
              ? `?${new URLSearchParams({ family_slug: familySlug }).toString()}`
              : ''
          }`
        ),
      detail: (modelVersionId: string) =>
        request<ModelVersionDetail>(config.adminApiUrl, `/api/v1/registry/model-versions/${modelVersionId}`),
      compare: (modelVersionIds: string[]) =>
        request<ModelVersionComparison>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions/compare?${queryString(
            modelVersionIds.map((modelVersionId) => ['model_version_ids', modelVersionId])
          )}`
        ),
      approve: (modelVersionId: string, payload: ModelApprovalRequest) =>
        request<DeploymentApprovalRecordSummary>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions/${modelVersionId}:approve`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      markRollbackTarget: (modelVersionId: string, payload: RollbackTargetRequest) =>
        request<ModelVersionDetail>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions/${modelVersionId}:mark-rollback-target`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      archive: (modelVersionId: string, payload: ModelLifecycleCommandRequest) =>
        request<ModelVersionDetail>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions/${modelVersionId}:archive`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      reject: (modelVersionId: string, payload: ModelLifecycleCommandRequest) =>
        request<ModelVersionDetail>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions/${modelVersionId}:reject`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      cleanupPreview: (modelVersionId: string) =>
        request<CleanupImpactPreview>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions/${modelVersionId}/cleanup-preview`
        ),
      cleanup: (modelVersionId: string, payload: CleanupExecutionRequest) =>
        request<CleanupExecutionResult>(
          config.adminApiUrl,
          `/api/v1/registry/model-versions/${modelVersionId}:cleanup`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        )
    },
    deployments: {
      packages: () => request<ListResponse<DeploymentPackageSummary>>(config.adminApiUrl, '/api/v1/deployments/packages'),
      packageDetail: (modelVersionId: string) =>
        request<DeploymentPackageDetail>(config.adminApiUrl, `/api/v1/deployments/packages/${modelVersionId}`),
      buildPackage: (payload: DeploymentPackageBuildRequest) =>
        request<AcceptedJob>(config.adminApiUrl, '/api/v1/deployments/packages', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      targets: () =>
        request<ListResponse<DeploymentTargetSummary>>(config.adminApiUrl, '/api/v1/deployments/targets'),
      releases: () =>
        request<ListResponse<DeploymentReleaseSummary>>(config.adminApiUrl, '/api/v1/deployments/releases'),
      releaseDetail: (deploymentReleaseId: string) =>
        request<DeploymentReleaseDetail>(config.adminApiUrl, `/api/v1/deployments/releases/${deploymentReleaseId}`),
      promote: (payload: DeploymentCommandRequest) =>
        request<AcceptedJob>(config.adminApiUrl, '/api/v1/deployments/promotions', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      smokeTest: (deploymentReleaseId: string, payload: DeploymentSmokeTestRequest) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/deployments/releases/${deploymentReleaseId}:smoke-test`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      deactivateModel: (
        deploymentReleaseId: string,
        payload: DeploymentModelServingCommandRequest
      ) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/deployments/releases/${deploymentReleaseId}:deactivate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      activateModel: (
        deploymentReleaseId: string,
        payload: DeploymentModelServingCommandRequest
      ) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/deployments/releases/${deploymentReleaseId}:activate`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      rollback: (targetPath: string, payload: RollbackCommandRequest) =>
        request<AcceptedJob>(
          config.adminApiUrl,
          `/api/v1/deployments/${targetPath}:rollback`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      cleanupPreview: (deploymentReleaseId: string) =>
        request<CleanupImpactPreview>(
          config.adminApiUrl,
          `/api/v1/deployments/releases/${deploymentReleaseId}/cleanup-preview`
        ),
      cleanup: (deploymentReleaseId: string, payload: CleanupExecutionRequest) =>
        request<CleanupExecutionResult>(
          config.adminApiUrl,
          `/api/v1/deployments/releases/${deploymentReleaseId}:cleanup`,
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        )
    },
    inference: {
      realtimeClassifyBytes: (
        image: File,
        params: { deploymentTarget: string; deploymentReleaseId: string; timeoutMs?: number; topK?: number }
      ) =>
        requestImageBytes<ClassificationInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          `/api/v1/realtime/classify-bytes?${queryString([
            ['deployment_target', params.deploymentTarget],
            ['deployment_release_id', params.deploymentReleaseId],
            ...(params.timeoutMs !== undefined ? [['timeout_ms', String(params.timeoutMs)] as [string, string]] : []),
            ...(params.topK !== undefined ? [['top_k', String(params.topK)] as [string, string]] : [])
          ])}`,
          image
        ),
      realtimeDetectBytes: (
        image: File,
        params: { deploymentTarget: string; deploymentReleaseId: string; timeoutMs?: number; maxDetections?: number; scoreThreshold?: number }
      ) =>
        requestImageBytes<DetectionInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          `/api/v1/realtime/detect-bytes?${queryString([
            ['deployment_target', params.deploymentTarget],
            ['deployment_release_id', params.deploymentReleaseId],
            ...(params.timeoutMs !== undefined ? [['timeout_ms', String(params.timeoutMs)] as [string, string]] : []),
            ...(params.maxDetections !== undefined ? [['max_detections', String(params.maxDetections)] as [string, string]] : []),
            ...(params.scoreThreshold !== undefined ? [['score_threshold', String(params.scoreThreshold)] as [string, string]] : [])
          ])}`,
          image
        ),
      realtimeSegmentBytes: (
        image: File,
        params: { deploymentTarget: string; deploymentReleaseId: string; timeoutMs?: number; maxSegments?: number; scoreThreshold?: number }
      ) =>
        requestImageBytes<SegmentationInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          `/api/v1/realtime/segment-bytes?${queryString([
            ['deployment_target', params.deploymentTarget],
            ['deployment_release_id', params.deploymentReleaseId],
            ...(params.timeoutMs !== undefined ? [['timeout_ms', String(params.timeoutMs)] as [string, string]] : []),
            ...(params.maxSegments !== undefined ? [['max_segments', String(params.maxSegments)] as [string, string]] : []),
            ...(params.scoreThreshold !== undefined ? [['score_threshold', String(params.scoreThreshold)] as [string, string]] : [])
          ])}`,
          image
        ),
      realtimeAnomalyBytes: (
        image: File,
        params: { deploymentTarget: string; deploymentReleaseId: string; timeoutMs?: number; threshold?: number; returnMask?: boolean }
      ) =>
        requestImageBytes<AnomalyInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          `/api/v1/realtime/anomaly-bytes?${queryString([
            ['deployment_target', params.deploymentTarget],
            ['deployment_release_id', params.deploymentReleaseId],
            ...(params.timeoutMs !== undefined ? [['timeout_ms', String(params.timeoutMs)] as [string, string]] : []),
            ...(params.threshold !== undefined ? [['threshold', String(params.threshold)] as [string, string]] : []),
            ...(params.returnMask !== undefined ? [['return_mask', String(params.returnMask)] as [string, string]] : [])
          ])}`,
          image
        ),
      classify: (payload: RealtimeClassifyRequest) =>
        request<ClassificationInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/realtime/classify',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      detect: (payload: RealtimeDetectRequest) =>
        request<DetectionInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/realtime/detect',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      segment: (payload: RealtimeSegmentRequest) =>
        request<SegmentationInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/realtime/segment',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      anomaly: (payload: RealtimeAnomalyRequest) =>
        request<AnomalyInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/realtime/anomaly',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      realtimePredict: (payload: RealtimeInferenceRequest) =>
        request<RealtimeInferenceResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/realtime/predict',
          {
            method: 'POST',
            body: JSON.stringify(payload)
          }
        ),
      submitBatch: (payload: BatchJobCreateRequest) =>
        request<BatchJobSummary>(config.adminApiUrl, '/api/v1/batch-jobs', {
          method: 'POST',
          body: JSON.stringify(payload)
        }),
      listBatchJobs: () =>
        request<ListResponse<BatchJobSummary>>(config.adminApiUrl, '/api/v1/batch-jobs'),
      batchStatus: (batchJobId: string) =>
        request<BatchJobDetail>(config.adminApiUrl, `/api/v1/batch-jobs/${batchJobId}`),
      batchResults: (batchJobId: string) =>
        request<BatchResultResponse>(config.adminApiUrl, `/api/v1/batch-jobs/${batchJobId}/results`),
      batchDownload: (batchJobId: string) =>
        requestBinary(config.adminApiUrl, `/api/v1/batch-jobs/${batchJobId}/download`),
      realtimeCapabilities: () =>
        request<ModelCapabilitiesResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/realtime/models/capabilities'
        ),
      realtimeStatus: () =>
        request<ModelStatusResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/realtime/models/status'
        ),
      batchCapabilities: () =>
        request<ModelCapabilitiesResponse>(
          config.inferenceGatewayBatchUrl,
          '/api/v1/batch/models/capabilities'
        ),
      batchStatusSummary: () =>
        request<ModelStatusResponse>(
          config.inferenceGatewayBatchUrl,
          '/api/v1/batch/models/status'
        ),
      publicRealtimeModels: () =>
        request<PublicModelListResponse>(config.inferenceGatewayRealtimeUrl, '/api/v1/models'),
      publicRealtimeModelDetail: (modelId: string) =>
        request<PublicModelDetailResponse>(config.inferenceGatewayRealtimeUrl, `/api/v1/models/${modelId}`),
      publicBatchModels: () =>
        request<PublicModelListResponse>(config.inferenceGatewayBatchUrl, '/api/v1/models'),
      publicBatchModelDetail: (modelId: string) =>
        request<PublicModelDetailResponse>(config.inferenceGatewayBatchUrl, `/api/v1/models/${modelId}`),
      publicClassify: (modelId: string, image: File, params?: { topK?: number; timeoutMs?: number }) => {
        const form = new FormData();
        form.append('model_id', modelId);
        form.append('image', image);
        if (params?.topK !== undefined) {
          form.append('top_k', String(params.topK));
        }
        if (params?.timeoutMs !== undefined) {
          form.append('timeout_ms', String(params.timeoutMs));
        }
        return requestForm<PublicClassificationResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/infer/classify',
          form
        );
      },
      publicDetect: (
        modelId: string,
        image: File,
        params?: {
          threshold?: number;
          iouThreshold?: number;
          maxDetections?: number;
          timeoutMs?: number;
        }
      ) => {
        const form = new FormData();
        form.append('model_id', modelId);
        form.append('image', image);
        if (params?.threshold !== undefined) {
          form.append('threshold', String(params.threshold));
        }
        if (params?.iouThreshold !== undefined) {
          form.append('iou_threshold', String(params.iouThreshold));
        }
        if (params?.maxDetections !== undefined) {
          form.append('max_detections', String(params.maxDetections));
        }
        if (params?.timeoutMs !== undefined) {
          form.append('timeout_ms', String(params.timeoutMs));
        }
        return requestForm<PublicDetectionResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/infer/detect',
          form
        );
      },
      publicSegment: (
        modelId: string,
        image: File,
        params?: {
          threshold?: number;
          maxSegments?: number;
          timeoutMs?: number;
        }
      ) => {
        const form = new FormData();
        form.append('model_id', modelId);
        form.append('image', image);
        if (params?.threshold !== undefined) {
          form.append('threshold', String(params.threshold));
        }
        if (params?.maxSegments !== undefined) {
          form.append('max_segments', String(params.maxSegments));
        }
        if (params?.timeoutMs !== undefined) {
          form.append('timeout_ms', String(params.timeoutMs));
        }
        return requestForm<PublicSegmentationResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/infer/segment',
          form
        );
      },
      publicAnomaly: (
        modelId: string,
        image: File,
        params?: {
          threshold?: number;
          returnMask?: boolean;
          timeoutMs?: number;
        }
      ) => {
        const form = new FormData();
        form.append('model_id', modelId);
        form.append('image', image);
        if (params?.threshold !== undefined) {
          form.append('threshold', String(params.threshold));
        }
        if (params?.returnMask !== undefined) {
          form.append('return_mask', String(params.returnMask));
        }
        if (params?.timeoutMs !== undefined) {
          form.append('timeout_ms', String(params.timeoutMs));
        }
        return requestForm<PublicAnomalyResponse>(
          config.inferenceGatewayRealtimeUrl,
          '/api/v1/infer/anomaly',
          form
        );
      }
    }
  };
}

function toUploadBody(payload: Blob | ArrayBuffer | Uint8Array, contentType: string) {
  if (payload instanceof Blob || payload instanceof ArrayBuffer) {
    return payload;
  }
  const slicedBuffer = payload.buffer.slice(
    payload.byteOffset,
    payload.byteOffset + payload.byteLength
  ) as ArrayBuffer;
  return new Blob([slicedBuffer], { type: contentType });
}

export { PlatformApiError };
