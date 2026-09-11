from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from cv_contracts.runtime import RuntimeNodeSpec

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.payloads import AnyRuntimePayload


@dataclass
class NodeExecutionResult:
    outputs: dict[str, AnyRuntimePayload] = field(default_factory=dict)


class RuntimeNodeHandler(Protocol):
    node_type: str

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        ...


def assert_handler_compatible(handler: object, *, expected_node_type: str) -> RuntimeNodeHandler:
    node_type = getattr(handler, "node_type", None)
    execute = getattr(handler, "execute", None)
    if node_type != expected_node_type:
        raise TypeError(
            f"Handler declares node_type {node_type!r}, expected {expected_node_type!r}."
        )
    if not callable(execute):
        raise TypeError("Runtime node handler must expose an execute(context, node, inputs) method.")
    return handler  # type: ignore[return-value]
