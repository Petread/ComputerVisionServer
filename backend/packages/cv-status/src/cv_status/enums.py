from enum import StrEnum


class EnvironmentName(StrEnum):
    DEV = "dev"
    PROD = "prod"


class ServiceName(StrEnum):
    ADMIN_API = "admin-api"
    WEBHOOK_RECEIVER = "webhook-receiver"
    DATASET_SERVICE = "dataset-service"
    TRAINING_ORCHESTRATOR = "training-orchestrator"
    MODEL_REGISTRY_SERVICE = "model-registry-service"
    INFERENCE_GATEWAY_RT = "inference-gateway-rt"
    INFERENCE_GATEWAY_BATCH = "inference-gateway-batch"
    DEPLOYMENT_CONTROLLER = "deployment-controller"
    RUNTIME_ADMIN_API = "runtime-admin-api"
    CHANNEL_SERVICE = "channel-service"
    PIPELINE_DEFINITION_SERVICE = "pipeline-definition-service"
    RUNTIME_ORCHESTRATOR = "runtime-orchestrator"
    REALTIME_EXECUTOR = "realtime-executor"
    TRIGGER_EXECUTOR = "trigger-executor"
    OVERLAY_SERVICE = "overlay-service"
    CAPTURE_SERVICE = "capture-service"
    RESULT_SERVICE = "result-service"
    CONNECTOR_SERVICE = "connector-service"
    CONTROL_WORKER = "control-worker"
    TRAINING_WORKER = "training-worker"
    BATCH_WORKER = "batch-worker"
    MAINTENANCE_WORKER = "maintenance-worker"


class QueueName(StrEnum):
    WEBHOOK = "webhook"
    DATASET = "dataset"
    TRAINING = "training"
    DEPLOY = "deploy"
    BATCH = "batch"
    RUNTIME_CONTROL = "runtime-control"
    RUNTIME_CAPTURE = "runtime-capture"
    RUNTIME_CONNECTOR = "runtime-connector"
    MAINTENANCE = "maintenance"


class HealthState(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class UserRole(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    ML_ENGINEER = "ml_engineer"
    OPERATOR = "operator"
    LABEL_MANAGER = "label_manager"
    VIEWER = "viewer"
    INFERENCE_CLIENT = "inference_client"


class TaskType(StrEnum):
    CLASSIFICATION = "classification"
    DETECTION = "detection"
    ORIENTED_DETECTION = "oriented_detection"
    SEGMENTATION = "segmentation"
    ANOMALY = "anomaly"
    TEMPORAL_SEQUENCE = "temporal_sequence"


class DatasetStatus(StrEnum):
    DRAFT = "draft"
    UPLOADING = "uploading"
    READY = "ready"
    SNAPSHOTTING = "snapshotting"
    SNAPSHOTTED = "snapshotted"
    FAILED = "failed"


class LabelProjectStatus(StrEnum):
    DISCOVERED = "discovered"
    MAPPED = "mapped"
    ACTIVE = "active"
    NEEDS_ATTENTION = "needs_attention"
    ARCHIVED = "archived"


class SnapshotStatus(StrEnum):
    REQUESTED = "requested"
    EXPORTING = "exporting"
    MATERIALIZING = "materializing"
    VALIDATING = "validating"
    READY = "ready"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class UploadSessionStatus(StrEnum):
    OPEN = "open"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


class TrainingJobStatus(StrEnum):
    QUEUED = "queued"
    WAITING_FOR_GPU = "waiting_for_gpu"
    PREPARING_DATA = "preparing_data"
    VALIDATING_SNAPSHOT = "validating_snapshot"
    INITIALIZING = "initializing"
    TRAINING = "training"
    CHECKPOINTING = "checkpointing"
    EVALUATING = "evaluating"
    EXPORTING = "exporting"
    REGISTERING = "registering"
    RESUMING = "resuming"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK_IF_NEEDED = "rolled_back_if_needed"


class ModelLifecycleStatus(StrEnum):
    DRAFT = "draft"
    TRAINED = "trained"
    VALIDATED = "validated"
    STAGED = "staged"
    PRODUCTION = "production"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class PromotionStatus(StrEnum):
    STAGING_REQUESTED = "staging_requested"
    STAGED = "staged"
    PRODUCTION_REQUESTED = "production_requested"
    PRODUCTION = "production"
    REJECTED = "rejected"
    RETIRED = "retired"


class DeploymentApprovalScope(StrEnum):
    STAGING = "staging"
    PRODUCTION = "production"


class DeploymentApprovalStatus(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class DeploymentPackageStatus(StrEnum):
    MISSING = "missing"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class DeploymentReleaseStatus(StrEnum):
    REQUESTED = "requested"
    PACKAGING = "packaging"
    REPOSITORY_SYNC = "repository_sync"
    LOADING = "loading"
    SMOKE_TESTING = "smoke_testing"
    ACTIVATED = "activated"
    DEACTIVATED = "deactivated"
    ACTIVATION_FAILED = "activation_failed"
    ROLLBACK_REQUESTED = "rollback_requested"
    ROLLED_BACK = "rolled_back"
    RETIRED = "retired"


class BatchJobStatus(StrEnum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


class BatchChunkStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ValidationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class WebhookProcessingStatus(StrEnum):
    ACCEPTED = "accepted"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"


class TargetPath(StrEnum):
    REALTIME = "realtime"
    BATCH = "batch"


class EndpointMode(StrEnum):
    REALTIME = "realtime"
    BATCH = "batch"
    BOTH = "both"


class RuntimeMode(StrEnum):
    REALTIME = "realtime"
    TRIGGER = "trigger"
    INTERVAL = "interval"


class RuntimeSourceType(StrEnum):
    RTSP = "rtsp"
    USB_OPENCV = "usb_opencv"
    ARAVIS_GIGE = "aravis_gige"
    FLIR_BLACKFLY = "flir_blackfly"
    BASLER_PYLON = "basler_pylon"
    SNAPSHOT = "snapshot"
    TRIGGER_CAPTURE = "trigger_capture"
    FILE_TEST = "file_test"


class RuntimeNodeCategory(StrEnum):
    TRIGGER = "trigger"
    SOURCE = "source"
    PREPROCESSING = "preprocessing"
    COMPUTER_VISION = "computer_vision"
    INFERENCE = "inference"
    ANALYSIS = "analysis"
    CAPTURE = "capture"
    OUTPUT = "output"


class RuntimeChannelDesiredState(StrEnum):
    DRAFT = "draft"
    STOPPED = "stopped"
    START_REQUESTED = "start_requested"
    RUNNING = "running"
    STOP_REQUESTED = "stop_requested"
    DISABLED = "disabled"


class RuntimeObservedState(StrEnum):
    UNASSIGNED = "unassigned"
    STARTING = "starting"
    VALIDATING = "validating"
    LOADING_PIPELINE = "loading_pipeline"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    RECOVERING = "recovering"


class RuntimeExecutionStatus(StrEnum):
    REQUESTED = "requested"
    ADMITTED = "admitted"
    VALIDATING = "validating"
    LOADING_PIPELINE = "loading_pipeline"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    RECOVERED = "recovered"


class RuntimeControlCommand(StrEnum):
    START = "start"
    STOP = "stop"
    RESTART = "restart"
    RECONCILE = "reconcile"


class RuntimeControlCommandStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RuntimePipelineStatus(StrEnum):
    DRAFT = "draft"
    VALIDATED = "validated"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"


class RuntimeResourceStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    FAILED = "failed"
