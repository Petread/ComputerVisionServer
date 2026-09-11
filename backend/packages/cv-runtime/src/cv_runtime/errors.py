from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RuntimeNodeError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{2,119}$")
    message: str
    node_key: str | None = None
    node_type: str | None = None
    retryable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeNodeExecutionError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        node_key: str | None = None,
        node_type: str | None = None,
        retryable: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.error = RuntimeNodeError(
            code=code,
            message=message,
            node_key=node_key,
            node_type=node_type,
            retryable=retryable,
            metadata=metadata or {},
        )


class RuntimeGraphBuildError(ValueError):
    pass


class RuntimeHandlerLoadError(ValueError):
    pass
