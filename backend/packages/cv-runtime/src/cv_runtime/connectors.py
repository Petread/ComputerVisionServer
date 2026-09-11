from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from cv_contracts.runtime import (
    RuntimeConnectorDeliveryStatus,
    RuntimeOutputConnectorDeliveryPolicy,
    RuntimeOutputConnectorTypeSummary,
)
from pydantic import BaseModel, ConfigDict, Field


ConnectorStatus: TypeAlias = RuntimeConnectorDeliveryStatus

_SECRET_KEY_HINTS = ("password", "passwd", "secret", "token", "apikey", "api_key", "auth")


class RuntimeConnectorConfigError(ValueError):
    """Raised when an output connector configuration is not safe or valid."""


class RuntimeConnectorDeliveryError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "runtime_connector_delivery_failed",
        retryable: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.metadata = metadata or {}


class OverlayOutputConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publish_live_view: bool = True
    layer_prefix: str = Field(default="", max_length=64)


class HttpTriggerResponseOutputConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_response_delivery: bool = True


class PersistedResultOutputConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_class: str = Field(default="standard", min_length=3, max_length=64)


class InternalEventOutputConnectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_namespace: str = Field(default="runtime.output", min_length=3, max_length=120)


@dataclass(frozen=True)
class OutputConnectorTypeDefinition:
    connector_type: str
    display_name: str
    description: str
    config_model: type[BaseModel]
    default_config: dict[str, Any]
    default_delivery_policy: RuntimeOutputConnectorDeliveryPolicy
    supports_secrets: bool = False
    retryable: bool = True

    def validate_config(
        self,
        config: dict[str, Any] | None,
        *,
        secret_refs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        raw_config = dict(config or {})
        _reject_inline_secret_values(raw_config)
        _validate_secret_refs(secret_refs or {}, supports_secrets=self.supports_secrets)
        try:
            return self.config_model.model_validate(raw_config).model_dump(mode="json")
        except ValueError as exc:
            raise RuntimeConnectorConfigError(str(exc)) from exc

    def to_api(self) -> RuntimeOutputConnectorTypeSummary:
        return RuntimeOutputConnectorTypeSummary(
            connector_type=self.connector_type,
            display_name=self.display_name,
            description=self.description,
            config_schema=self.config_model.model_json_schema(),
            default_config=dict(self.default_config),
            default_delivery_policy=self.default_delivery_policy,
            supports_secrets=self.supports_secrets,
            retryable=self.retryable,
        )


@dataclass(frozen=True)
class ConnectorDeliveryContext:
    output_connector_id: str
    connector_type: str
    channel_id: str
    result_record_id: str
    request_payload: dict[str, Any]
    config: dict[str, Any]
    secret_refs: dict[str, str]


@dataclass(frozen=True)
class ConnectorDeliveryOutcome:
    status: ConnectorStatus
    response_payload: dict[str, Any]


class RuntimeOutputConnector(Protocol):
    connector_type: str

    def deliver(self, context: ConnectorDeliveryContext) -> ConnectorDeliveryOutcome:
        ...


DEFAULT_DELIVERY_POLICY = RuntimeOutputConnectorDeliveryPolicy()


BUILTIN_OUTPUT_CONNECTOR_TYPES: dict[str, OutputConnectorTypeDefinition] = {
    "output.overlay": OutputConnectorTypeDefinition(
        connector_type="output.overlay",
        display_name="Overlay Output",
        description="Publishes runtime overlay primitives to the channel live-view state.",
        config_model=OverlayOutputConnectorConfig,
        default_config=OverlayOutputConnectorConfig().model_dump(mode="json"),
        default_delivery_policy=DEFAULT_DELIVERY_POLICY,
        supports_secrets=False,
        retryable=True,
    ),
    "output.http_trigger_response": OutputConnectorTypeDefinition(
        connector_type="output.http_trigger_response",
        display_name="HTTP Trigger Response",
        description="Records the final trigger response emitted by a trigger pipeline.",
        config_model=HttpTriggerResponseOutputConnectorConfig,
        default_config=HttpTriggerResponseOutputConnectorConfig().model_dump(mode="json"),
        default_delivery_policy=RuntimeOutputConnectorDeliveryPolicy(max_attempts=1),
        supports_secrets=False,
        retryable=False,
    ),
    "output.persisted_result": OutputConnectorTypeDefinition(
        connector_type="output.persisted_result",
        display_name="Persisted Result",
        description="Confirms that structured runtime results were stored durably.",
        config_model=PersistedResultOutputConnectorConfig,
        default_config=PersistedResultOutputConnectorConfig().model_dump(mode="json"),
        default_delivery_policy=RuntimeOutputConnectorDeliveryPolicy(max_attempts=1),
        supports_secrets=False,
        retryable=False,
    ),
    "output.internal_event": OutputConnectorTypeDefinition(
        connector_type="output.internal_event",
        display_name="Internal Event",
        description="Writes internal runtime events to the platform audit/event stream.",
        config_model=InternalEventOutputConnectorConfig,
        default_config=InternalEventOutputConnectorConfig().model_dump(mode="json"),
        default_delivery_policy=DEFAULT_DELIVERY_POLICY,
        supports_secrets=False,
        retryable=True,
    ),
}


def builtin_output_connector_types() -> list[OutputConnectorTypeDefinition]:
    return list(BUILTIN_OUTPUT_CONNECTOR_TYPES.values())


def output_connector_type_catalog() -> list[RuntimeOutputConnectorTypeSummary]:
    return [definition.to_api() for definition in builtin_output_connector_types()]


def get_output_connector_type(connector_type: str) -> OutputConnectorTypeDefinition:
    definition = BUILTIN_OUTPUT_CONNECTOR_TYPES.get(connector_type)
    if definition is None:
        raise RuntimeConnectorConfigError(f"Unsupported output connector type: {connector_type}.")
    return definition


def validate_output_connector_config(
    connector_type: str,
    config: dict[str, Any] | None,
    *,
    secret_refs: dict[str, str] | None = None,
) -> dict[str, Any]:
    definition = get_output_connector_type(connector_type)
    return definition.validate_config(config, secret_refs=secret_refs)


def normalize_connector_delivery_policy(
    policy: RuntimeOutputConnectorDeliveryPolicy | dict[str, Any] | None,
) -> RuntimeOutputConnectorDeliveryPolicy:
    if isinstance(policy, RuntimeOutputConnectorDeliveryPolicy):
        return policy
    return RuntimeOutputConnectorDeliveryPolicy.model_validate(policy or {})


def connector_display_name(connector_type: str) -> str:
    try:
        return get_output_connector_type(connector_type).display_name
    except RuntimeConnectorConfigError:
        return connector_type.replace(".", " ").replace("_", " ").title()


def _validate_secret_refs(secret_refs: dict[str, str], *, supports_secrets: bool) -> None:
    if secret_refs and not supports_secrets:
        raise RuntimeConnectorConfigError("This connector type does not accept secret references.")
    for key, value in secret_refs.items():
        if not key or not value:
            raise RuntimeConnectorConfigError("Connector secret references must include a key and reference.")
        if any(char.isspace() for char in key):
            raise RuntimeConnectorConfigError("Connector secret reference keys must not contain whitespace.")


def _reject_inline_secret_values(value: object, *, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key).casefold().replace("-", "_")
            if any(hint in key_text for hint in _SECRET_KEY_HINTS):
                raise RuntimeConnectorConfigError(
                    f"Connector {path}.{key} must use secret_refs instead of inline secrets."
                )
            _reject_inline_secret_values(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_inline_secret_values(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.casefold()
        if "://" in lowered and "@" in lowered.split("://", 1)[1]:
            raise RuntimeConnectorConfigError(
                f"Connector {path} must not contain inline URL credentials."
            )
