from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from cv_contracts.runtime import RuntimeLogicalModelRef
from cv_status import RuntimeMode, TargetPath, TaskType
from cv_utils import utc_now_iso

from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.inference_client import RuntimeInferenceClient
from cv_runtime.payloads import (
    AnyRuntimePayload,
    FramePayload,
    SynchronizedFrameSetPayload,
    TriggerPayload,
)


@dataclass(frozen=True)
class RuntimeModelBinding:
    deployment_release_id: str
    deployment_target: str
    target_path: TargetPath
    model_family_slug: str
    model_version_id: str
    task_type: TaskType
    contract_major: int
    display_name: str | None = None
    capability: dict[str, Any] = field(default_factory=dict)


ModelResolver = Callable[[RuntimeLogicalModelRef], RuntimeModelBinding]


@dataclass
class RuntimeExecutionContext:
    correlation_id: str
    pipeline_version_id: str
    mode: RuntimeMode
    channel_id: str | None = None
    runtime_execution_id: str | None = None
    trigger_endpoint_id: str | None = None
    source_config: dict[str, Any] = field(default_factory=dict)
    input_frame: FramePayload | None = None
    input_frames: dict[str, FramePayload] = field(default_factory=dict)
    synchronized_frame_set: SynchronizedFrameSetPayload | None = None
    trigger: TriggerPayload | None = None
    inference_client: RuntimeInferenceClient | None = None
    model_resolver: ModelResolver | None = None
    state: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def now_iso(self) -> str:
        return utc_now_iso()

    def resolve_model(self, model_ref: RuntimeLogicalModelRef) -> RuntimeModelBinding:
        if self.model_resolver is not None:
            try:
                return self.model_resolver(model_ref)
            except RuntimeNodeExecutionError:
                raise
            except Exception as exc:
                raise RuntimeNodeExecutionError(
                    code="model_binding_unavailable",
                    message="The runtime could not resolve a deployed model for this node.",
                    retryable=False,
                    metadata={
                        "detail": str(exc),
                        "model_family_slug": model_ref.model_family_slug,
                        "target_path": model_ref.target_path.value,
                        "contract_major": model_ref.contract_major,
                        "selection_policy": model_ref.selection_policy,
                        "required_task_type": model_ref.required_task_type.value,
                        "deployment_release_id": model_ref.deployment_release_id,
                    },
                ) from exc
        if model_ref.deployment_release_id:
            return RuntimeModelBinding(
                deployment_release_id=model_ref.deployment_release_id,
                deployment_target=model_ref.target_path.value,
                target_path=model_ref.target_path,
                model_family_slug=model_ref.model_family_slug,
                model_version_id=model_ref.deployment_release_id,
                task_type=model_ref.required_task_type,
                contract_major=model_ref.contract_major,
            )
        raise RuntimeNodeExecutionError(
            code="model_resolver_missing",
            message="A model resolver is required for non-explicit runtime model references.",
        )


NodeInputs = dict[str, AnyRuntimePayload]
