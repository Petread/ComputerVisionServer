from __future__ import annotations

import importlib.util
import math
import os
import re
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from cv_contracts.runtime import RuntimeNodeSpec
from cv_status import RuntimeMode
from cv_utils import utc_now_iso
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cv_runtime.context import NodeInputs, RuntimeExecutionContext
from cv_runtime.errors import RuntimeNodeExecutionError
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import (
    ResultPayload,
    SiemensSpsWriteStatusPayload,
    StemCurvaturePhaseSolutionPayload,
)

SUPPORTED_SIEMENS_SPS_DATA_TYPES = {
    "BOOL",
    "BYTE",
    "WORD",
    "DWORD",
    "INT",
    "DINT",
    "REAL",
}
SIEMENS_SPS_WRITE_ALLOW_ENV = "YBB_ALLOW_PLC_WRITES"
SIEMENS_SPS_CONNECTION_CACHE_KEY = "_siemens_sps_connections"
SIEMENS_SPS_CONNECTION_CACHE_LOCK_KEY = "_siemens_sps_connections_lock"
SKIP_WRITE = object()
_LOCK_TYPE = type(threading.Lock())

SiemensDataType = Literal["BOOL", "BYTE", "WORD", "DWORD", "INT", "DINT", "REAL"]
FailureBehavior = Literal["fail_pipeline", "warn_continue", "skip_mapping"]
DefaultBehavior = Literal["fail", "skip", "write_default"]


class SiemensSpsConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SiemensSpsConnectionConfig(SiemensSpsConfigModel):
    name: str = Field(default="Siemens SPS", max_length=160)
    host: str = Field(default="", max_length=255)
    port: int = Field(default=102, ge=1, le=65535)
    rack: int = Field(default=0, ge=0, le=7)
    slot: int = Field(default=1, ge=0, le=31)
    connect_timeout_ms: int = Field(default=1000, ge=50, le=30000)
    write_timeout_ms: int = Field(default=1000, ge=50, le=30000)
    reconnect: Literal["disabled", "on_failure"] = "on_failure"
    reuse_policy: Literal["per_execution", "per_runtime"] = "per_execution"
    connection_ref: str | None = None

    def cache_key(self) -> str:
        return f"{self.host}:{self.port}:rack{self.rack}:slot{self.slot}"


class SiemensSpsSourceSelector(SiemensSpsConfigModel):
    field: str = Field(default="", max_length=240)
    extractor: Literal[
        "nested_result_path",
        "top1_label",
        "top1_score",
        "numeric_field",
        "boolean_field",
        "string_field",
    ] = "nested_result_path"
    advanced_path: str = Field(default="", max_length=240)
    allow_advanced_path: bool = False

    def path(self) -> str:
        if self.extractor == "top1_label":
            return "top_label.label"
        if self.extractor == "top1_score":
            return "top_label.score"
        if self.allow_advanced_path and self.advanced_path.strip():
            return self.advanced_path.strip()
        return self.field.strip()


class SiemensSpsTargetAddress(SiemensSpsConfigModel):
    area: Literal["DB"] = "DB"
    db_number: int = Field(default=1, ge=1, le=65535)
    byte_offset: int = Field(default=0, ge=0, le=65535)
    bit_offset: int | None = Field(default=None, ge=0, le=7)
    data_type: SiemensDataType = "INT"


class SiemensSpsEnumMappingEntry(SiemensSpsConfigModel):
    source: str = Field(min_length=1, max_length=240)
    value: bool | int | float | str


class SiemensSpsConversionConfig(SiemensSpsConfigModel):
    mode: Literal["direct", "enum", "boolean", "scale", "threshold"] = "direct"
    enum_values: list[SiemensSpsEnumMappingEntry] = Field(default_factory=list)
    true_value: bool | int | float | str | None = None
    false_value: bool | int | float | str | None = None
    multiplier: float = 1.0
    addend: float = 0.0
    rounding: Literal["none", "nearest", "floor", "ceil"] = "none"
    clamp_min: float | None = None
    clamp_max: float | None = None
    operator: Literal[">=", ">", "<=", "<", "==", "!="] = ">="
    threshold: float = 0.5
    default_behavior: DefaultBehavior = "fail"
    default_value: bool | int | float | str | None = None


class SiemensSpsWriteCondition(SiemensSpsConfigModel):
    mode: Literal["always", "when_field"] = "always"
    field: str = Field(default="", max_length=240)
    operator: Literal["truthy", "falsy", "==", "!=", ">=", ">", "<=", "<"] = "truthy"
    value: bool | int | float | str | None = None


class SiemensSpsMappingRow(SiemensSpsConfigModel):
    key: str = Field(default="", max_length=80)
    label: str = Field(default="", max_length=160)
    enabled: bool = True
    source: SiemensSpsSourceSelector = Field(default_factory=SiemensSpsSourceSelector)
    source_field: str | None = Field(default=None, max_length=240)
    target: SiemensSpsTargetAddress = Field(default_factory=SiemensSpsTargetAddress)
    conversion: SiemensSpsConversionConfig = Field(default_factory=SiemensSpsConversionConfig)
    condition: SiemensSpsWriteCondition = Field(default_factory=SiemensSpsWriteCondition)
    failure_behavior: Literal["inherit", "fail_pipeline", "warn_continue", "skip_mapping"] = (
        "inherit"
    )

    @model_validator(mode="after")
    def normalize_source_field(self) -> SiemensSpsMappingRow:
        if self.source_field and not self.source.field:
            self.source.field = self.source_field
        return self

    def display_key(self, index: int) -> str:
        return self.key.strip() or self.label.strip() or f"mapping_{index + 1}"


class SiemensSpsWriteConfig(SiemensSpsConfigModel):
    enabled: bool = False
    dry_run: bool = True
    connection: SiemensSpsConnectionConfig = Field(default_factory=SiemensSpsConnectionConfig)
    mappings: list[SiemensSpsMappingRow] = Field(default_factory=list)
    failure_behavior: FailureBehavior = "fail_pipeline"
    realtime_min_interval_ms: int = Field(default=1000, ge=100, le=60000)
    stem_orientation_command_enabled: bool = False
    stem_orientation_minimum_confidence: float = Field(default=0.55, ge=0, le=1)
    stem_orientation_maximum_abs_angle_deg: float = Field(default=90.0, gt=0, le=90)
    closed_phase_control_enabled: bool = False
    phase_control_stop_tolerance_deg: float = Field(default=2.0, ge=0, le=30)
    sps_rotation_limits_confirmed: bool = False
    sps_maximum_rotation_deg: float = Field(default=90.0, gt=0, le=180)
    sps_maximum_rotation_time_ms: int = Field(default=5000, ge=100, le=120000)


class SiemensSpsClient(Protocol):
    def connect(self) -> None:
        ...

    def read_db(self, db_number: int, byte_offset: int, size: int) -> bytearray:
        ...

    def write_db(self, db_number: int, byte_offset: int, data: bytes | bytearray) -> None:
        ...

    def close(self) -> None:
        ...


SiemensSpsClientFactory = Callable[[SiemensSpsConnectionConfig], SiemensSpsClient]


@dataclass(frozen=True)
class SiemensSpsValidationIssue:
    severity: Literal["error", "warning"]
    code: str
    message: str
    field: str | None = None
    metadata: dict[str, Any] | None = None


class SiemensSpsConfigError(ValueError):
    pass


class Snap7SiemensSpsClient:
    def __init__(self, connection: SiemensSpsConnectionConfig) -> None:
        self.connection = connection
        self._client: Any | None = None

    def connect(self) -> None:
        try:
            import snap7  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeNodeExecutionError(
                code="siemens_sps_dependency_missing",
                message=(
                    "python-snap7 is not installed in the runtime environment. "
                    "Install the offline runtime dependency bundle before enabling PLC writes."
                ),
                retryable=False,
            ) from exc
        client = snap7.client.Client()
        _apply_snap7_timeouts(client, self.connection)
        client.connect(
            self.connection.host,
            self.connection.rack,
            self.connection.slot,
            self.connection.port,
        )
        self._client = client

    def read_db(self, db_number: int, byte_offset: int, size: int) -> bytearray:
        if self._client is None:
            self.connect()
        assert self._client is not None
        return bytearray(self._client.db_read(db_number, byte_offset, size))

    def write_db(self, db_number: int, byte_offset: int, data: bytes | bytearray) -> None:
        if self._client is None:
            self.connect()
        assert self._client is not None
        self._client.db_write(db_number, byte_offset, bytearray(data))

    def close(self) -> None:
        if self._client is None:
            return
        try:
            disconnect = getattr(self._client, "disconnect", None)
            if callable(disconnect):
                disconnect()
        finally:
            self._client = None


class SiemensSpsWriteHandler:
    def __init__(
        self,
        node_type: str,
        *,
        client_factory: SiemensSpsClientFactory | None = None,
        dependency_available: Callable[[], bool] | None = None,
    ) -> None:
        self.node_type = node_type
        self._client_factory = client_factory or Snap7SiemensSpsClient
        self._dependency_available = dependency_available or snap7_dependency_available

    def execute(
        self,
        context: RuntimeExecutionContext,
        node: RuntimeNodeSpec,
        inputs: NodeInputs,
    ) -> NodeExecutionResult:
        config = parse_siemens_sps_write_config(node.config or {})
        dependency_available = self._dependency_available()
        issues = validate_siemens_sps_write_config(
            config,
            mode=context.mode,
            dependency_available=dependency_available,
        )
        errors = [item for item in issues if item.severity == "error"]
        if errors:
            raise RuntimeNodeExecutionError(
                code=errors[0].code,
                message=errors[0].message,
                retryable=False,
                metadata={
                    "node_key": node.node_key,
                    "validation_errors": [
                        {
                            "code": item.code,
                            "message": item.message,
                            "field": item.field,
                            "metadata": item.metadata or {},
                        }
                        for item in errors
                    ],
                },
            )
        result = _normalized_result_payload(inputs.get("result"))
        result, stem_command, stem_gate, phase_control = _stem_orientation_command_gate(
            context,
            node,
            config,
            result,
        )
        dry_run_reason = _dry_run_reason(context, config)
        dry_run = bool(config.dry_run or dry_run_reason)
        target = _target_metadata(config.connection)
        started = time.perf_counter()
        write_attempted = False
        writes: list[dict[str, Any]] = []
        if not config.enabled:
            writes = [
                _write_status(
                    mapping=row,
                    index=index,
                    target=target,
                    status="skipped",
                    success=False,
                    dry_run=True,
                    reason="node_disabled",
                    started_perf=started,
                )
                for index, row in enumerate(config.mappings)
            ]
            return _status_result(
                config,
                target,
                writes,
                status="disabled",
                stem_command=stem_command,
                stem_gate=stem_gate,
                phase_control=phase_control,
            )
        if config.stem_orientation_command_enabled and not stem_gate.get("ready", False):
            blocked_reason = str(
                next(iter(stem_gate.get("reason_codes") or []), "stem_orientation_gate_blocked")
            ).lower()
            writes = [
                _write_status(
                    mapping=row,
                    index=index,
                    target=target,
                    status="skipped",
                    success=False,
                    dry_run=True,
                    reason=blocked_reason,
                    started_perf=started,
                )
                for index, row in enumerate(config.mappings)
                if row.enabled
            ]
            return _status_result(
                config,
                target,
                writes,
                status="skipped",
                stem_command=stem_command,
                stem_gate=stem_gate,
                phase_control=phase_control,
            )
        throttle_status = _realtime_throttle_status(context, node, config, dry_run=dry_run)
        if throttle_status:
            writes = [
                _write_status(
                    mapping=row,
                    index=index,
                    target=target,
                    status="skipped",
                    success=False,
                    dry_run=dry_run,
                    reason=throttle_status,
                    started_perf=started,
                )
                for index, row in enumerate(config.mappings)
                if row.enabled
            ]
            return _status_result(
                config,
                target,
                writes,
                status="skipped",
                stem_command=stem_command,
                stem_gate=stem_gate,
                phase_control=phase_control,
            )

        client: SiemensSpsClient | None = None
        client_lock: threading.Lock | None = None
        try:
            for index, row in enumerate(config.mappings):
                row_started = time.perf_counter()
                if not row.enabled:
                    writes.append(
                        _write_status(
                            mapping=row,
                            index=index,
                            target=target,
                            status="skipped",
                            success=False,
                            dry_run=dry_run,
                            reason="mapping_disabled",
                            started_perf=row_started,
                        )
                    )
                    continue
                if (
                    config.stem_orientation_command_enabled
                    and not stem_command.get("issue_main_command", False)
                    and not row.source.path().startswith("phase_control.")
                ):
                    writes.append(
                        _write_status(
                            mapping=row,
                            index=index,
                            target=target,
                            status="skipped",
                            success=False,
                            dry_run=dry_run,
                            reason="main_command_already_announced",
                            started_perf=row_started,
                        )
                    )
                    continue
                if (
                    row.source.path().startswith("phase_control.")
                    and not phase_control.get("available", False)
                ):
                    writes.append(
                        _write_status(
                            mapping=row,
                            index=index,
                            target=target,
                            status="skipped",
                            success=False,
                            dry_run=dry_run,
                            reason="phase_control_unavailable",
                            started_perf=row_started,
                        )
                    )
                    continue
                if not _condition_matches(row.condition, result):
                    writes.append(
                        _write_status(
                            mapping=row,
                            index=index,
                            target=target,
                            status="skipped",
                            success=False,
                            dry_run=dry_run,
                            reason="condition_false",
                            started_perf=row_started,
                        )
                    )
                    continue
                source_value: Any = None
                mapped_value: Any = None
                try:
                    source_value = _resolve_source_value(result, row.source.path())
                    mapped_value = convert_siemens_sps_value(source_value, row)
                    if mapped_value is SKIP_WRITE:
                        writes.append(
                            _write_status(
                                mapping=row,
                                index=index,
                                target=target,
                                status="skipped",
                                success=False,
                                dry_run=dry_run,
                                reason="fallback_skip",
                                source_value=source_value,
                                started_perf=row_started,
                            )
                        )
                        continue
                    plc_value = coerce_siemens_sps_value(mapped_value, row.target.data_type)
                except SiemensSpsConfigError as exc:
                    behavior = _configured_failure_behavior(row, config)
                    status = _write_status(
                        mapping=row,
                        index=index,
                        target=target,
                        status="skipped" if behavior == "skip_mapping" else "failed",
                        success=False,
                        dry_run=dry_run,
                        reason=(
                            "conversion_skipped"
                            if behavior == "skip_mapping"
                            else "conversion_failed"
                        ),
                        source_value=source_value,
                        error=str(exc),
                        started_perf=row_started,
                    )
                    writes.append(status)
                    _maybe_fail_pipeline(node, row, config, writes, str(exc))
                    continue
                if dry_run:
                    writes.append(
                        _write_status(
                            mapping=row,
                            index=index,
                            target=target,
                            status="dry_run",
                            success=True,
                            dry_run=True,
                            reason=dry_run_reason or "dry_run",
                            source_value=source_value,
                            mapped_value=plc_value,
                            started_perf=row_started,
                        )
                    )
                    continue
                try:
                    if client is None:
                        client, client_lock = _client_for_context(
                            context,
                            config,
                            self._client_factory,
                        )
                    write_attempted = True
                    if client_lock is None:
                        _write_to_plc(client, row.target, plc_value)
                    else:
                        with client_lock:
                            _write_to_plc(client, row.target, plc_value)
                    writes.append(
                        _write_status(
                            mapping=row,
                            index=index,
                            target=target,
                            status="success",
                            success=True,
                            dry_run=False,
                            reason=None,
                            source_value=source_value,
                            mapped_value=plc_value,
                            started_perf=row_started,
                        )
                    )
                except Exception as exc:
                    if client is not None and config.connection.reconnect == "on_failure":
                        _discard_client_after_failure(context, config, client)
                        client = None
                        client_lock = None
                    error = _safe_error(exc)
                    writes.append(
                        _write_status(
                            mapping=row,
                            index=index,
                            target=target,
                            status="failed",
                            success=False,
                            dry_run=False,
                            reason="write_failed",
                            source_value=source_value,
                            mapped_value=plc_value,
                            error=error,
                            started_perf=row_started,
                        )
                    )
                    _maybe_fail_pipeline(node, row, config, writes, error)
        finally:
            if (
                client is not None
                and config.connection.reuse_policy == "per_execution"
            ):
                client.close()
        if write_attempted:
            _record_realtime_write(context, node)
        if not writes:
            status = "skipped"
        elif any(item.get("status") == "failed" for item in writes):
            status = "failed"
        elif dry_run:
            status = "dry_run"
        elif any(item.get("status") == "success" for item in writes):
            status = "success"
        else:
            status = "skipped"
        if (
            status in {"dry_run", "success"}
            and stem_command.get("issue_main_command") is True
            and any(item.get("status") in {"dry_run", "success"} for item in writes)
        ):
            _record_stem_command_announced(context, node, stem_command)
        return _status_result(
            config,
            target,
            writes,
            status=status,
            stem_command=stem_command,
            stem_gate=stem_gate,
            phase_control=phase_control,
        )


def parse_siemens_sps_write_config(config: dict[str, Any]) -> SiemensSpsWriteConfig:
    try:
        return SiemensSpsWriteConfig.model_validate(config)
    except ValidationError as exc:
        raise RuntimeNodeExecutionError(
            code="siemens_sps_config_invalid",
            message="Write to Siemens SPS configuration is invalid.",
            retryable=False,
            metadata={"errors": exc.errors(include_url=False)},
        ) from exc


def validate_siemens_sps_write_config(
    config: SiemensSpsWriteConfig | dict[str, Any],
    *,
    known_fields: set[str] | None = None,
    mode: RuntimeMode | None = None,
    dependency_available: bool | None = None,
) -> list[SiemensSpsValidationIssue]:
    try:
        parsed = (
            config
            if isinstance(config, SiemensSpsWriteConfig)
            else SiemensSpsWriteConfig.model_validate(config)
        )
    except ValidationError as exc:
        return [
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_config_invalid",
                "Write to Siemens SPS configuration is invalid.",
                metadata={"errors": exc.errors(include_url=False)},
            )
        ]
    issues: list[SiemensSpsValidationIssue] = []
    if not parsed.enabled:
        return issues
    if parsed.closed_phase_control_enabled and not parsed.stem_orientation_command_enabled:
        issues.append(
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_phase_control_requires_stem_command",
                "Closed stem phase control requires the stem-orientation command gate.",
                field="closed_phase_control_enabled",
            )
        )
    if parsed.closed_phase_control_enabled and not parsed.sps_rotation_limits_confirmed:
        issues.append(
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_phase_control_limits_unconfirmed",
                "Closed stem phase control requires confirmed PLC-side angle and time limits.",
                field="sps_rotation_limits_confirmed",
            )
        )
    if dependency_available is False and not parsed.dry_run:
        issues.append(
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_dependency_missing",
                "python-snap7 must be installed before PLC writes can be enabled.",
                field="connection",
            )
        )
    connection = parsed.connection
    if not _valid_host(connection.host):
        issues.append(
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_host_invalid",
                "PLC host must be a valid IP address or hostname.",
                field="connection.host",
            )
        )
    if not parsed.mappings or not any(row.enabled for row in parsed.mappings):
        issues.append(
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_mapping_missing",
                "At least one enabled PLC write mapping is required.",
                field="mappings",
            )
        )
    if (
        mode == RuntimeMode.REALTIME
        and not parsed.dry_run
        and parsed.realtime_min_interval_ms < 1000
    ):
        issues.append(
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_realtime_throttle_required",
                "Realtime PLC writes require a throttle interval of at least 1000 ms.",
                field="realtime_min_interval_ms",
            )
        )
    if parsed.enabled and not parsed.dry_run:
        issues.append(
            SiemensSpsValidationIssue(
                "warning",
                "siemens_sps_side_effect_enabled",
                "This pipeline writes to a Siemens PLC when executed.",
                field="enabled",
            )
        )
    known = known_fields or set()
    for index, row in enumerate(parsed.mappings):
        if not row.enabled:
            continue
        prefix = f"mappings[{index}]"
        source_path = row.source.path()
        if not source_path:
            issues.append(
                SiemensSpsValidationIssue(
                    "error",
                    "siemens_sps_source_missing",
                    "Each enabled PLC mapping must select a source result field.",
                    field=f"{prefix}.source.field",
                )
            )
        elif (
            known
            and not _known_field_matches(source_path, known)
            and not row.source.allow_advanced_path
        ):
            issues.append(
                SiemensSpsValidationIssue(
                    "error",
                    "siemens_sps_source_not_produced",
                    f"PLC source field {source_path} is not produced by a reachable upstream node.",
                    field=f"{prefix}.source.field",
                    metadata={"source_field": source_path, "known_fields": sorted(known)},
                )
            )
        target = row.target
        if target.data_type not in SUPPORTED_SIEMENS_SPS_DATA_TYPES:
            issues.append(
                SiemensSpsValidationIssue(
                    "error",
                    "siemens_sps_datatype_unsupported",
                    "Unsupported Siemens SPS data type.",
                    field=f"{prefix}.target.data_type",
                )
            )
        if target.data_type == "BOOL" and target.bit_offset is None:
            issues.append(
                SiemensSpsValidationIssue(
                    "error",
                    "siemens_sps_bool_bit_offset_missing",
                    "BOOL targets require a bit offset from 0 to 7.",
                    field=f"{prefix}.target.bit_offset",
                )
            )
        if target.data_type != "BOOL" and target.bit_offset is not None:
            issues.append(
                SiemensSpsValidationIssue(
                    "error",
                    "siemens_sps_bit_offset_invalid",
                    "Bit offset is only valid for BOOL targets.",
                    field=f"{prefix}.target.bit_offset",
                )
            )
        issues.extend(_validate_conversion(row, prefix))
    return issues


def snap7_dependency_available() -> bool:
    return importlib.util.find_spec("snap7") is not None


def close_siemens_sps_connection_cache(state: dict[str, Any]) -> int:
    cache = state.pop(SIEMENS_SPS_CONNECTION_CACHE_KEY, None)
    state.pop(SIEMENS_SPS_CONNECTION_CACHE_LOCK_KEY, None)
    if not isinstance(cache, dict):
        return 0
    closed = 0
    for entry in list(cache.values()):
        if _close_cached_client_entry(entry):
            closed += 1
    cache.clear()
    return closed


def convert_siemens_sps_value(source_value: Any, row: SiemensSpsMappingRow) -> Any:
    conversion = row.conversion
    if source_value is None:
        return _fallback_value(conversion, "Source result field is missing.")
    if conversion.mode == "direct":
        return source_value
    if conversion.mode == "enum":
        for item in conversion.enum_values:
            if str(source_value) == item.source:
                return item.value
        return _fallback_value(
            conversion,
            f"Source value {source_value!r} is not mapped for enum conversion.",
        )
    if conversion.mode == "boolean":
        boolean = _coerce_bool(source_value)
        if boolean is None:
            return _fallback_value(conversion, "Source value is not boolean-compatible.")
        if (
            row.target.data_type == "BOOL"
            and conversion.true_value is None
            and conversion.false_value is None
        ):
            return boolean
        return conversion.true_value if boolean else conversion.false_value
    if conversion.mode == "scale":
        number = _coerce_number(source_value)
        if number is None:
            return _fallback_value(conversion, "Source value is not numeric.")
        value = number * conversion.multiplier + conversion.addend
        if conversion.clamp_min is not None:
            value = max(value, conversion.clamp_min)
        if conversion.clamp_max is not None:
            value = min(value, conversion.clamp_max)
        if conversion.rounding == "nearest":
            value = round(value)
        elif conversion.rounding == "floor":
            value = math.floor(value)
        elif conversion.rounding == "ceil":
            value = math.ceil(value)
        return value
    if conversion.mode == "threshold":
        number = _coerce_number(source_value)
        if number is None:
            return _fallback_value(conversion, "Source value is not numeric.")
        matched = _compare_number(number, conversion.operator, conversion.threshold)
        if conversion.true_value is None and conversion.false_value is None:
            return matched
        return conversion.true_value if matched else conversion.false_value
    raise SiemensSpsConfigError(f"Unsupported conversion mode {conversion.mode}.")


def coerce_siemens_sps_value(value: Any, data_type: SiemensDataType) -> bool | int | float:
    if data_type == "BOOL":
        boolean = _coerce_bool(value)
        if boolean is None:
            raise SiemensSpsConfigError("Value cannot be converted to BOOL.")
        return boolean
    if data_type in {"BYTE", "WORD", "DWORD", "INT", "DINT"}:
        if isinstance(value, bool):
            raise SiemensSpsConfigError(
                "Boolean values require boolean mapping before numeric writes."
            )
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SiemensSpsConfigError(
                f"Value {value!r} cannot be converted to {data_type}."
            ) from exc
        minimum, maximum = _integer_range(data_type)
        if number < minimum or number > maximum:
            raise SiemensSpsConfigError(
                f"Value {number} is outside {data_type} range {minimum}..{maximum}."
            )
        return number
    if data_type == "REAL":
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise SiemensSpsConfigError(f"Value {value!r} cannot be converted to REAL.") from exc
        if not math.isfinite(number):
            raise SiemensSpsConfigError("REAL value must be finite.")
        return number
    raise SiemensSpsConfigError(f"Unsupported Siemens data type {data_type}.")


def encode_siemens_sps_value(value: bool | int | float, data_type: SiemensDataType) -> bytes:
    if data_type == "BYTE":
        return struct.pack(">B", int(value))
    if data_type == "WORD":
        return struct.pack(">H", int(value))
    if data_type == "DWORD":
        return struct.pack(">I", int(value))
    if data_type == "INT":
        return struct.pack(">h", int(value))
    if data_type == "DINT":
        return struct.pack(">i", int(value))
    if data_type == "REAL":
        return struct.pack(">f", float(value))
    raise SiemensSpsConfigError(f"{data_type} is not encoded as a standalone byte buffer.")


def address_label(target: SiemensSpsTargetAddress) -> str:
    if target.data_type == "BOOL":
        bit = 0 if target.bit_offset is None else target.bit_offset
        return f"DB{target.db_number}.DBX{target.byte_offset}.{bit}"
    if target.data_type in {"BYTE"}:
        prefix = "DBB"
    elif target.data_type in {"INT", "WORD"}:
        prefix = "DBW"
    else:
        prefix = "DBD"
    return f"DB{target.db_number}.{prefix}{target.byte_offset}"


def _normalized_result_payload(payload: object) -> ResultPayload:
    if isinstance(payload, StemCurvaturePhaseSolutionPayload):
        orientation = payload.orientation_result
        if orientation is not None:
            result = orientation.model_dump(mode="json")
            result["_sps_phase_runtime"] = {
                "track_id": payload.track_id,
                "current_phase_deg": payload.current_phase_deg,
                "phase_confidence": payload.phase_confidence,
                "session_state": payload.session_state,
                "curvature_frozen": payload.curvature_frozen,
                "result_frozen": payload.result_frozen,
            }
            return ResultPayload(result=result, decision=True)
    from cv_runtime.nodes.builtin import _result_from_payload

    return _result_from_payload(payload)


def _stem_orientation_command_gate(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    config: SiemensSpsWriteConfig,
    result: ResultPayload,
) -> tuple[ResultPayload, dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not config.stem_orientation_command_enabled:
        return result, {}, {}, {}
    values = dict(result.result)
    phase_runtime = values.pop("_sps_phase_runtime", {})
    reasons: list[str] = []
    if values.get("schema_version") != "stem.orientation.v3":
        reasons.append("STEM_SPS_V3_RESULT_REQUIRED")
    if values.get("algorithm") != "CURVATURE_PHASE_MINIMUM":
        reasons.append("STEM_SPS_ALGORITHM_NOT_ALLOWED")
    if values.get("decision") != "ROTATE":
        reasons.append(
            "STEM_SPS_RESULT_UNCERTAIN"
            if values.get("decision") == "UNCERTAIN"
            else "STEM_SPS_NO_ROTATION_COMMAND"
        )
    session_id = str(values.get("session_id") or "")
    track_id = str(values.get("track_id") or "")
    if not session_id or not track_id:
        reasons.append("STEM_SPS_SESSION_OR_TRACK_MISSING")
    if _stem_track_changed(context, node, session_id=session_id, track_id=track_id):
        reasons.append("STEM_SPS_TRACK_CHANGED")
    if values.get("minimum_principle_validation_status") != "VALIDATION_PASSED":
        reasons.append("STEM_SPS_REFERENCE_VALIDATION_NOT_PASSED")
    if not values.get("minimum_principle_validation_id") or not values.get(
        "minimum_principle_validation_hash"
    ):
        reasons.append("STEM_SPS_REFERENCE_VALIDATION_IDENTITY_MISSING")
    if values.get("shadow_evaluation_approved") is not True:
        reasons.append("STEM_SPS_SHADOW_EVALUATION_NOT_APPROVED")
    calibration_fields = (
        "calibration_id",
        "process_calibration_hash",
        "rotation_calibration_id",
        "rotation_calibration_hash",
    )
    if any(not values.get(field) for field in calibration_fields):
        reasons.append("STEM_SPS_CALIBRATION_INVALID")
    confidence = _coerce_number(values.get("overall_confidence"))
    if confidence is None or confidence < config.stem_orientation_minimum_confidence:
        reasons.append("STEM_SPS_CONFIDENCE_TOO_LOW")
    angle = _coerce_number(values.get("recommended_rotation_deg_signed"))
    if (
        angle is None
        or angle == 0
        or abs(angle) > config.stem_orientation_maximum_abs_angle_deg
    ):
        reasons.append("STEM_SPS_ANGLE_OUTSIDE_PROCESS_LIMIT")
    positive_rotation = values.get("positive_rotation")
    expected_direction = (
        positive_rotation
        if angle is not None and angle > 0
        else "COUNTER_CLOCKWISE"
        if positive_rotation == "CLOCKWISE"
        else "CLOCKWISE"
        if positive_rotation == "COUNTER_CLOCKWISE"
        else None
    )
    if (
        positive_rotation not in {"CLOCKWISE", "COUNTER_CLOCKWISE"}
        or not values.get("rotation_reference_view")
        or values.get("direction") != expected_direction
    ):
        reasons.append("STEM_SPS_DIRECTION_CONVENTION_INVALID")
    if values.get("result_frozen") is not True or values.get("deadline_met") is not True:
        reasons.append("STEM_SPS_RESULT_NOT_FINAL_OR_DEADLINE_MISSED")
    expiry = _parse_utc_timestamp(values.get("result_expiry"))
    if expiry is None or datetime.now(UTC) >= expiry:
        reasons.append("STEM_SPS_RESULT_EXPIRED")
    result_reason_codes = [str(item) for item in values.get("reason_codes") or []]
    invalid_reason_tokens = (
        "CALIBRATION_INVALID",
        "CALIBRATION_MISMATCH",
        "RUNTIME_ERROR",
        "PIPELINE_FAILED",
    )
    if context.metadata.get("runtime_error") or any(
        token in reason
        for reason in result_reason_codes
        for token in invalid_reason_tokens
    ):
        reasons.append("STEM_SPS_RUNTIME_OR_CALIBRATION_ERROR")

    command_id = (
        f"{session_id}:{values.get('decision_frame_sequence')}"
        if session_id
        else "invalid"
    )
    ready = not reasons
    issue_main_command = ready and not _stem_command_already_announced(
        context, node, command_id
    )
    command = {
        "schema_version": "stem.orientation.sps-dry-run.v1",
        "command_id": command_id,
        "session_id": session_id or None,
        "track_id": track_id or None,
        "signed_target_angle_deg": angle,
        "direction": values.get("direction"),
        "target_phase_deg": values.get("target_phase_deg"),
        "current_phase_deg": values.get("current_phase_deg"),
        "overall_confidence": confidence,
        "valid_until": values.get("result_expiry"),
        "calibration": {
            "process_id": values.get("calibration_id"),
            "process_hash": values.get("process_calibration_hash"),
            "rotation_id": values.get("rotation_calibration_id"),
            "rotation_hash": values.get("rotation_calibration_hash"),
            "validation_id": values.get("minimum_principle_validation_id"),
            "validation_hash": values.get("minimum_principle_validation_hash"),
        },
        "algorithm": {
            "name": values.get("algorithm"),
            "version": values.get("algorithm_version"),
            "components": values.get("component_versions") or {},
        },
        "reason_codes": result_reason_codes,
        "ready": ready,
        "issue_main_command": issue_main_command,
    }
    gate = {
        "schema_version": "stem.orientation.sps-gate.v1",
        "ready": ready,
        "reason_codes": list(dict.fromkeys(reasons)),
        "checked_at": utc_now_iso(),
        "dry_run": bool(config.dry_run or _dry_run_reason(context, config)),
        "vision_is_safety_control": False,
    }
    phase_control = _phase_control_status(
        config,
        values=values,
        phase_runtime=phase_runtime if isinstance(phase_runtime, dict) else {},
        gate_ready=ready,
    )
    augmented = ResultPayload(
        result={
            **values,
            "stem_orientation_command": command,
            "stem_orientation_gate": gate,
            "phase_control": phase_control,
        },
        decision=ready,
    )
    return augmented, command, gate, phase_control


def _phase_control_status(
    config: SiemensSpsWriteConfig,
    *,
    values: dict[str, Any],
    phase_runtime: dict[str, Any],
    gate_ready: bool,
) -> dict[str, Any]:
    reasons: list[str] = []
    if not config.closed_phase_control_enabled:
        reasons.append("PHASE_CONTROL_DISABLED")
    if not config.sps_rotation_limits_confirmed:
        reasons.append("PHASE_CONTROL_SPS_LIMITS_UNCONFIRMED")
    if not gate_ready:
        reasons.append("PHASE_CONTROL_MAIN_COMMAND_GATE_BLOCKED")
    if phase_runtime.get("track_id") != values.get("track_id"):
        reasons.append("PHASE_CONTROL_TRACK_MISMATCH")
    if phase_runtime.get("curvature_frozen") is not True:
        reasons.append("PHASE_CONTROL_CURVATURE_NOT_FROZEN")
    current_phase = _coerce_number(phase_runtime.get("current_phase_deg"))
    target_phase = _coerce_number(values.get("target_phase_deg"))
    phase_confidence = _coerce_number(phase_runtime.get("phase_confidence"))
    if current_phase is None or target_phase is None:
        reasons.append("PHASE_CONTROL_PHASE_MISSING")
    if (
        phase_confidence is None
        or phase_confidence < config.stem_orientation_minimum_confidence
    ):
        reasons.append("PHASE_CONTROL_CONFIDENCE_TOO_LOW")
    error = (
        _wrap_to_minus_90_plus_90(target_phase - current_phase)
        if target_phase is not None and current_phase is not None
        else None
    )
    available = not reasons
    return {
        "schema_version": "stem.orientation.phase-control.v1",
        "enabled": config.closed_phase_control_enabled,
        "available": available,
        "current_phase_deg": current_phase,
        "target_phase_deg": target_phase,
        "phase_error_deg_signed": error,
        "phase_confidence": phase_confidence,
        "stop_tolerance_deg": config.phase_control_stop_tolerance_deg,
        "stop_requested": bool(
            available
            and error is not None
            and abs(error) <= config.phase_control_stop_tolerance_deg
        ),
        "curvature_recomputed": False,
        "sps_limits_confirmed": config.sps_rotation_limits_confirmed,
        "sps_maximum_rotation_deg": config.sps_maximum_rotation_deg,
        "sps_maximum_rotation_time_ms": config.sps_maximum_rotation_time_ms,
        "vision_is_safety_control": False,
        "reason_codes": reasons,
    }


def _stem_command_state(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
) -> dict[str, Any]:
    state = context.state.setdefault("_siemens_stem_orientation_commands", {})
    if not isinstance(state, dict):
        state = {}
        context.state["_siemens_stem_orientation_commands"] = state
    value = state.setdefault(node.node_key, {})
    if not isinstance(value, dict):
        value = {}
        state[node.node_key] = value
    return value


def _stem_track_changed(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    *,
    session_id: str,
    track_id: str,
) -> bool:
    if not session_id or not track_id:
        return False
    state = _stem_command_state(context, node)
    if state.get("session_id") == session_id:
        return bool(state.get("track_id") and state.get("track_id") != track_id)
    state["session_id"] = session_id
    state["track_id"] = track_id
    return False


def _stem_command_already_announced(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    command_id: str,
) -> bool:
    return _stem_command_state(context, node).get("last_command_id") == command_id


def _record_stem_command_announced(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    command: dict[str, Any],
) -> None:
    command_id = command.get("command_id")
    if isinstance(command_id, str) and command_id:
        _stem_command_state(context, node)["last_command_id"] = command_id


def _parse_utc_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _wrap_to_minus_90_plus_90(value: float) -> float:
    return ((value + 90.0) % 180.0) - 90.0


def _resolve_source_value(result: ResultPayload, field: str) -> Any:
    from cv_runtime.nodes.builtin import _lookup_result_output_value

    alias = _canonical_source_path(field)
    value = _lookup_result_output_value(result, alias)
    if value is None and alias == "top1.label":
        return _lookup_result_output_value(result, "top_label.label")
    if value is None and alias == "top1.score":
        return _lookup_result_output_value(result, "top_label.score")
    return value


def _condition_matches(condition: SiemensSpsWriteCondition, result: ResultPayload) -> bool:
    if condition.mode == "always":
        return True
    value = _resolve_source_value(result, condition.field)
    if condition.operator == "truthy":
        return bool(value)
    if condition.operator == "falsy":
        return not bool(value)
    expected = condition.value
    if condition.operator == "==":
        return value == expected
    if condition.operator == "!=":
        return value != expected
    left = _coerce_number(value)
    right = _coerce_number(expected)
    if left is None or right is None:
        return False
    return _compare_number(left, condition.operator, right)


def _write_to_plc(
    client: SiemensSpsClient,
    target: SiemensSpsTargetAddress,
    plc_value: bool | int | float,
) -> None:
    if target.data_type == "BOOL":
        if target.bit_offset is None:
            raise SiemensSpsConfigError("BOOL targets require bit offset.")
        current = client.read_db(target.db_number, target.byte_offset, 1)
        mask = 1 << target.bit_offset
        if bool(plc_value):
            current[0] = current[0] | mask
        else:
            current[0] = current[0] & ~mask
        client.write_db(target.db_number, target.byte_offset, current)
        return
    client.write_db(
        target.db_number,
        target.byte_offset,
        encode_siemens_sps_value(plc_value, target.data_type),
    )


def _client_for_context(
    context: RuntimeExecutionContext,
    config: SiemensSpsWriteConfig,
    client_factory: SiemensSpsClientFactory,
) -> tuple[SiemensSpsClient, threading.Lock | None]:
    if config.connection.reuse_policy == "per_execution":
        return _new_connected_client(config.connection, client_factory), None
    cache = context.state.setdefault(SIEMENS_SPS_CONNECTION_CACHE_KEY, {})
    if not isinstance(cache, dict):
        cache = {}
        context.state[SIEMENS_SPS_CONNECTION_CACHE_KEY] = cache
    cache_key = config.connection.cache_key()
    cache_lock = _connection_cache_lock(context.state)
    with cache_lock:
        entry = cache.get(cache_key)
        if not _valid_cached_client_entry(entry):
            if entry is not None:
                _close_cached_client_entry(entry)
            client = _new_connected_client(config.connection, client_factory)
            entry = {"client": client, "lock": threading.Lock()}
            cache[cache_key] = entry
    return entry["client"], entry["lock"]


def _new_connected_client(
    connection: SiemensSpsConnectionConfig,
    client_factory: SiemensSpsClientFactory,
) -> SiemensSpsClient:
    client = client_factory(connection)
    try:
        client.connect()
    except Exception:
        _close_client_quietly(client)
        raise
    return client


def _discard_client_after_failure(
    context: RuntimeExecutionContext,
    config: SiemensSpsWriteConfig,
    client: SiemensSpsClient,
) -> None:
    if config.connection.reuse_policy == "per_runtime":
        _drop_cached_client(context, config.connection.cache_key())
        return
    _close_client_quietly(client)


def _drop_cached_client(context: RuntimeExecutionContext, cache_key: str) -> None:
    cache = context.state.get(SIEMENS_SPS_CONNECTION_CACHE_KEY)
    if not isinstance(cache, dict):
        return
    cache_lock = _connection_cache_lock(context.state)
    with cache_lock:
        entry = cache.pop(cache_key, None)
    _close_cached_client_entry(entry)


def _connection_cache_lock(state: dict[str, Any]) -> threading.Lock:
    lock = state.get(SIEMENS_SPS_CONNECTION_CACHE_LOCK_KEY)
    if isinstance(lock, _LOCK_TYPE):
        return lock
    lock = threading.Lock()
    state[SIEMENS_SPS_CONNECTION_CACHE_LOCK_KEY] = lock
    return lock


def _valid_cached_client_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    return entry.get("client") is not None and isinstance(entry.get("lock"), _LOCK_TYPE)


def _close_cached_client_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    return _close_client_quietly(entry.get("client"))


def _close_client_quietly(client: object) -> bool:
    close = getattr(client, "close", None)
    if not callable(close):
        return False
    try:
        close()
    except Exception:
        return False
    return True


def _status_result(
    config: SiemensSpsWriteConfig,
    target: dict[str, Any],
    writes: list[dict[str, Any]],
    *,
    status: Literal["disabled", "dry_run", "success", "failed", "skipped"],
    stem_command: dict[str, Any] | None = None,
    stem_gate: dict[str, Any] | None = None,
    phase_control: dict[str, Any] | None = None,
) -> NodeExecutionResult:
    return NodeExecutionResult(
        outputs={
            "status": SiemensSpsWriteStatusPayload(
                target=target,
                writes=writes,
                enabled=config.enabled,
                dry_run=config.dry_run or status == "dry_run",
                write_attempted=any(item.get("write_attempted") for item in writes),
                success=status != "failed"
                and not any(item.get("status") == "failed" for item in writes),
                status=status,
                last_error=next(
                    (str(item.get("error")) for item in reversed(writes) if item.get("error")),
                    None,
                ),
                stem_orientation_command=stem_command or {},
                stem_orientation_gate=stem_gate or {},
                phase_control=phase_control or {},
            )
        }
    )


def _write_status(
    *,
    mapping: SiemensSpsMappingRow,
    index: int,
    target: dict[str, Any],
    status: Literal["skipped", "dry_run", "success", "failed"],
    success: bool,
    dry_run: bool,
    reason: str | None,
    started_perf: float,
    source_value: Any = None,
    mapped_value: Any = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "mapping_key": mapping.display_key(index),
        "mapping_label": mapping.label or mapping.display_key(index),
        "source_field": mapping.source.path(),
        "source_value": source_value,
        "mapped_value": mapped_value,
        "target": target,
        "address": address_label(mapping.target),
        "data_type": mapping.target.data_type,
        "timestamp": utc_now_iso(),
        "duration_ms": round((time.perf_counter() - started_perf) * 1000, 3),
        "status": status,
        "success": success,
        "dry_run": dry_run,
        "write_attempted": status == "success" and not dry_run,
        "reason": reason,
        "error": error,
    }


def _maybe_fail_pipeline(
    node: RuntimeNodeSpec,
    row: SiemensSpsMappingRow,
    config: SiemensSpsWriteConfig,
    writes: list[dict[str, Any]],
    error: str,
) -> None:
    behavior = _configured_failure_behavior(row, config)
    if behavior == "fail_pipeline":
        raise RuntimeNodeExecutionError(
            code="siemens_sps_write_failed",
            message="Write to Siemens SPS failed.",
            retryable=True,
            metadata={"node_key": node.node_key, "writes": writes, "error": error},
        )


def _configured_failure_behavior(
    row: SiemensSpsMappingRow,
    config: SiemensSpsWriteConfig,
) -> FailureBehavior:
    return row.failure_behavior if row.failure_behavior != "inherit" else config.failure_behavior


def _dry_run_reason(context: RuntimeExecutionContext, config: SiemensSpsWriteConfig) -> str | None:
    if context.metadata.get("authoring_preview"):
        return "authoring_preview"
    if config.dry_run:
        return "dry_run"
    metadata_allowed = context.metadata.get("plc_writes_allowed")
    if metadata_allowed is not None and not bool(metadata_allowed):
        return "environment_blocked"
    if metadata_allowed is None and not _env_bool(SIEMENS_SPS_WRITE_ALLOW_ENV, default=False):
        return "environment_blocked"
    return None


def _realtime_throttle_status(
    context: RuntimeExecutionContext,
    node: RuntimeNodeSpec,
    config: SiemensSpsWriteConfig,
    *,
    dry_run: bool,
) -> str | None:
    if dry_run or context.mode != RuntimeMode.REALTIME:
        return None
    now = time.monotonic()
    state = context.state.setdefault("_siemens_sps_write_throttle", {})
    last = state.get(node.node_key)
    if isinstance(last, int | float):
        elapsed_ms = (now - float(last)) * 1000
        if elapsed_ms < config.realtime_min_interval_ms:
            return "realtime_throttled"
    return None


def _record_realtime_write(context: RuntimeExecutionContext, node: RuntimeNodeSpec) -> None:
    if context.mode != RuntimeMode.REALTIME:
        return
    state = context.state.setdefault("_siemens_sps_write_throttle", {})
    state[node.node_key] = time.monotonic()


def _target_metadata(connection: SiemensSpsConnectionConfig) -> dict[str, Any]:
    return {
        "name": connection.name,
        "host": connection.host,
        "port": connection.port,
        "rack": connection.rack,
        "slot": connection.slot,
    }


def _fallback_value(conversion: SiemensSpsConversionConfig, message: str) -> Any:
    if conversion.default_behavior == "write_default":
        return conversion.default_value
    if conversion.default_behavior == "skip":
        return SKIP_WRITE
    raise SiemensSpsConfigError(message)


def _validate_conversion(
    row: SiemensSpsMappingRow,
    prefix: str,
) -> list[SiemensSpsValidationIssue]:
    issues: list[SiemensSpsValidationIssue] = []
    conversion = row.conversion
    if conversion.mode == "enum":
        if not conversion.enum_values:
            issues.append(
                SiemensSpsValidationIssue(
                    "error",
                    "siemens_sps_enum_mapping_missing",
                    "Enum conversion requires at least one source-to-value mapping.",
                    field=f"{prefix}.conversion.enum_values",
                )
            )
        for enum_index, item in enumerate(conversion.enum_values):
            issues.extend(
                _static_value_issues(
                    item.value,
                    row.target.data_type,
                    f"{prefix}.conversion.enum_values[{enum_index}].value",
                )
            )
    if conversion.mode == "boolean":
        if row.target.data_type != "BOOL":
            if conversion.true_value is None or conversion.false_value is None:
                issues.append(
                    SiemensSpsValidationIssue(
                        "error",
                        "siemens_sps_boolean_mapping_values_missing",
                        "Boolean conversion to numeric targets requires true and false PLC values.",
                        field=f"{prefix}.conversion",
                    )
                )
            else:
                issues.extend(
                    _static_value_issues(
                        conversion.true_value,
                        row.target.data_type,
                        f"{prefix}.conversion.true_value",
                    )
                )
                issues.extend(
                    _static_value_issues(
                        conversion.false_value,
                        row.target.data_type,
                        f"{prefix}.conversion.false_value",
                    )
                )
    if conversion.mode == "scale":
        if conversion.clamp_min is not None and conversion.clamp_max is not None:
            if conversion.clamp_min > conversion.clamp_max:
                issues.append(
                    SiemensSpsValidationIssue(
                        "error",
                        "siemens_sps_scale_clamp_invalid",
                        "Scale clamp minimum must not be greater than clamp maximum.",
                        field=f"{prefix}.conversion.clamp_min",
                    )
                )
            if row.target.data_type in {"BYTE", "WORD", "DWORD", "INT", "DINT"}:
                issues.extend(
                    _static_value_issues(
                        conversion.clamp_min,
                        row.target.data_type,
                        f"{prefix}.conversion.clamp_min",
                    )
                )
                issues.extend(
                    _static_value_issues(
                        conversion.clamp_max,
                        row.target.data_type,
                        f"{prefix}.conversion.clamp_max",
                    )
                )
    if conversion.mode == "threshold":
        true_value = True if conversion.true_value is None else conversion.true_value
        false_value = False if conversion.false_value is None else conversion.false_value
        issues.extend(
            _static_value_issues(
                true_value,
                row.target.data_type,
                f"{prefix}.conversion.true_value",
            )
        )
        issues.extend(
            _static_value_issues(
                false_value, row.target.data_type, f"{prefix}.conversion.false_value"
            )
        )
    if conversion.default_behavior == "write_default":
        issues.extend(
            _static_value_issues(
                conversion.default_value,
                row.target.data_type,
                f"{prefix}.conversion.default_value",
            )
        )
    return issues


def _static_value_issues(
    value: Any,
    data_type: SiemensDataType,
    field: str,
) -> list[SiemensSpsValidationIssue]:
    try:
        coerce_siemens_sps_value(value, data_type)
    except SiemensSpsConfigError as exc:
        return [
            SiemensSpsValidationIssue(
                "error",
                "siemens_sps_mapping_value_invalid",
                str(exc),
                field=field,
            )
        ]
    return []


def _known_field_matches(source_path: str, known_fields: set[str]) -> bool:
    canonical = _canonical_source_path(source_path)
    base = canonical.split(".", 1)[0]
    return source_path in known_fields or canonical in known_fields or base in known_fields


def _canonical_source_path(field: str) -> str:
    return {
        "classification.top1.label": "top_label.label",
        "classification.top1.score": "top_label.score",
        "classification.top1.class": "top_label.label",
        "classification.top1.confidence": "top_label.score",
        "anomaly.score": "anomaly_score",
        "anomaly.decision": "is_anomalous",
        "detection.count": "count",
        "detections.count": "count",
        "quality.decision": "result",
        "quality.pass": "result",
        "measurement.distance_mm": "distance_mm",
        "measurements.distance_mm": "distance_mm",
    }.get(field, field)


def _valid_host(value: str) -> bool:
    candidate = value.strip()
    if not candidate:
        return False
    if len(candidate) > 255:
        return False
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", candidate):
        return all(0 <= int(part) <= 255 for part in candidate.split("."))
    return bool(
        re.fullmatch(
            r"(?=.{1,255}$)([a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
            r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?",
            candidate,
        )
    )


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on", "gut", "pass", "ok"}:
            return True
        if normalized in {"false", "0", "no", "off", "schlecht", "fail", "ng"}:
            return False
    return None


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _integer_range(data_type: str) -> tuple[int, int]:
    ranges = {
        "BYTE": (0, 255),
        "WORD": (0, 65535),
        "DWORD": (0, 4294967295),
        "INT": (-32768, 32767),
        "DINT": (-2147483648, 2147483647),
    }
    return ranges[data_type]


def _compare_number(left: float, operator: str, right: float) -> bool:
    if operator == ">=":
        return left >= right
    if operator == ">":
        return left > right
    if operator == "<=":
        return left <= right
    if operator == "<":
        return left < right
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    return False


def _apply_snap7_timeouts(client: Any, connection: SiemensSpsConnectionConfig) -> None:
    try:
        import snap7  # type: ignore[import-not-found]

        parameter = getattr(getattr(snap7, "types", None), "Parameter", None)
        set_param = getattr(client, "set_param", None)
        if parameter is None or not callable(set_param):
            return
        for name, value in {
            "PingTimeout": connection.connect_timeout_ms,
            "RecvTimeout": connection.write_timeout_ms,
            "SendTimeout": connection.write_timeout_ms,
        }.items():
            enum_value = getattr(parameter, name, None)
            if enum_value is not None:
                set_param(enum_value, int(value))
    except Exception:
        return


def _safe_error(exc: Exception) -> str:
    return str(exc)[:500] or exc.__class__.__name__


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


output_siemens_sps_write = SiemensSpsWriteHandler("output.siemens_sps_write")
