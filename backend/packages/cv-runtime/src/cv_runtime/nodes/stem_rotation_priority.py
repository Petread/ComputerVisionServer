from __future__ import annotations

from collections.abc import Mapping

from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.stem_rotation_priority import resolve_stem_rotation_priority


class StemRotationPriorityAdapterHandler:
    node_type = "analysis.stem_rotation_priority_adapter"

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        raw = context.metadata.get("stem_rotation_priority_control")
        control = raw if isinstance(raw, Mapping) else None
        configured_source = str((node.config or {}).get("priority_source") or "FRONTEND")
        priority = resolve_stem_rotation_priority(
            control,
            default_source=configured_source,
        )
        if inputs.get("frame") is None:
            priority = priority.model_copy(
                update={"valid": False, "reason_code": "CONTROL_TICK_MISSING"}
            )
        return NodeExecutionResult(outputs={"priority": priority})


analysis_stem_rotation_priority_adapter = StemRotationPriorityAdapterHandler()
