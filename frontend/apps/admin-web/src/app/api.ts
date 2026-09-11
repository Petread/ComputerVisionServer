import { createPlatformApi } from '@cv-platform/api-client';

const defaultAdminApiUrl = import.meta.env.DEV ? 'http://localhost:8080' : '';
const defaultRuntimeAdminApiUrl = import.meta.env.DEV ? 'http://localhost:8090' : '/runtime-api';
const defaultResultServiceUrl = import.meta.env.DEV ? 'http://localhost:8098' : '/runtime-results';
const defaultInferenceGatewayRealtimeUrl = import.meta.env.DEV
  ? 'http://localhost:8087'
  : '/inference-rt';
const defaultInferenceGatewayBatchUrl = import.meta.env.DEV
  ? 'http://localhost:8088'
  : '/inference-batch';
const defaultTriggerExecutorUrl = import.meta.env.DEV
  ? 'http://localhost:8095'
  : '/runtime-trigger';

export const apiBaseUrls = {
  adminApiUrl: import.meta.env.VITE_ADMIN_API_URL ?? defaultAdminApiUrl,
  runtimeAdminApiUrl: import.meta.env.VITE_RUNTIME_ADMIN_API_URL ?? defaultRuntimeAdminApiUrl,
  resultServiceUrl: import.meta.env.VITE_RESULT_SERVICE_URL ?? defaultResultServiceUrl,
  inferenceGatewayRealtimeUrl:
    import.meta.env.VITE_INFERENCE_GATEWAY_RT_URL ?? defaultInferenceGatewayRealtimeUrl,
  inferenceGatewayBatchUrl:
    import.meta.env.VITE_INFERENCE_GATEWAY_BATCH_URL ?? defaultInferenceGatewayBatchUrl,
  triggerExecutorUrl: import.meta.env.VITE_TRIGGER_EXECUTOR_URL ?? defaultTriggerExecutorUrl
};

export const api = createPlatformApi(apiBaseUrls);
