from __future__ import annotations

import struct
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cv_contracts import ClassificationLabelScore
from cv_contracts.runtime import RuntimeGraphEdge, RuntimeNodeSpec, RuntimePipelineGraph
from cv_runtime import (
    BUILTIN_NODE_TYPE_BY_KEY,
    ExecutablePipelineGraph,
    RuntimeExecutionContext,
)
from cv_runtime.handlers import NodeExecutionResult
from cv_runtime.payloads import ClassificationPayload, ResultPayload
from cv_runtime.siemens_sps import (
    SiemensSpsConnectionConfig,
    SiemensSpsWriteHandler,
    close_siemens_sps_connection_cache,
    validate_siemens_sps_write_config,
)
from cv_status import RuntimeMode, RuntimeNodeCategory


class ClassifierHandler:
    node_type = "test.classifier"

    def execute(self, _context, _node, _inputs):
        return NodeExecutionResult(
            outputs={
                "result": ClassificationPayload(
                    labels=[ClassificationLabelScore(label="gut", score=0.9, rank=1)]
                )
            }
        )


class MeasurementHandler:
    node_type = "test.measurement"

    def execute(self, _context, _node, _inputs):
        return NodeExecutionResult(
            outputs={"result": ResultPayload(result={"distance_mm": 12.5})}
        )


class FakeSiemensSpsClient:
    instances: list[FakeSiemensSpsClient] = []

    def __init__(self, connection: SiemensSpsConnectionConfig) -> None:
        self.connection = connection
        self.connected = False
        self.closed = False
        self.writes: list[tuple[int, int, bytes]] = []
        self.reads: list[tuple[int, int, int]] = []
        self.memory: dict[tuple[int, int], bytearray] = {}
        FakeSiemensSpsClient.instances.append(self)

    def connect(self) -> None:
        self.connected = True

    def read_db(self, db_number: int, byte_offset: int, size: int) -> bytearray:
        self.reads.append((db_number, byte_offset, size))
        return bytearray(self.memory.get((db_number, byte_offset), bytearray(size)))

    def write_db(self, db_number: int, byte_offset: int, data: bytes | bytearray) -> None:
        self.writes.append((db_number, byte_offset, bytes(data)))
        self.memory[(db_number, byte_offset)] = bytearray(data)

    def close(self) -> None:
        self.closed = True


class FailingFirstWriteClient(FakeSiemensSpsClient):
    remaining_failures = 1

    def write_db(self, db_number: int, byte_offset: int, data: bytes | bytearray) -> None:
        if FailingFirstWriteClient.remaining_failures > 0:
            FailingFirstWriteClient.remaining_failures -= 1
            raise RuntimeError("transient PLC write failure")
        super().write_db(db_number, byte_offset, data)


def _node(config: dict[str, Any]) -> RuntimeNodeSpec:
    return RuntimeNodeSpec(
        node_key="sps_write",
        node_type="output.siemens_sps_write",
        node_category=RuntimeNodeCategory.OUTPUT,
        input_ports=[],
        output_ports=[],
        config=config,
        position={},
    )


def _context(
    *,
    dry_allowed: bool = True,
    mode: RuntimeMode = RuntimeMode.TRIGGER,
) -> RuntimeExecutionContext:
    return RuntimeExecutionContext(
        correlation_id="corr_sps",
        pipeline_version_id="pver_sps",
        mode=mode,
        metadata={"plc_writes_allowed": dry_allowed},
    )


def _base_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "dry_run": False,
        "connection": {
            "name": "PLC A",
            "host": "192.168.1.10",
            "port": 102,
            "rack": 0,
            "slot": 1,
            "connect_timeout_ms": 1000,
            "write_timeout_ms": 1000,
            "reconnect": "on_failure",
            "reuse_policy": "per_execution",
            "connection_ref": None,
        },
        "failure_behavior": "fail_pipeline",
        "realtime_min_interval_ms": 1000,
        "mappings": [],
    }


def _valid_stem_orientation_result(
    *,
    decision: str = "ROTATE",
    expiry: datetime | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "stem.orientation.v3",
        "algorithm": "CURVATURE_PHASE_MINIMUM",
        "algorithm_version": "curvature-phase-minimum.v1",
        "component_versions": {
            "curvature": "stem-horizontal-curvature.v1",
            "phase": "stem-surface-phase.v1",
            "solver": "stem-curvature-phase-solver.v1",
        },
        "session_id": "stem-session-sps-1",
        "track_id": "stem-track-sps-1",
        "decision": decision,
        "recommended_rotation_deg_signed": 20.0 if decision == "ROTATE" else None,
        "direction": "CLOCKWISE" if decision == "ROTATE" else "NONE",
        "rotation_reference_view": "LOOKING_IN_CONVEYOR_DIRECTION",
        "positive_rotation": "CLOCKWISE",
        "target_phase_deg": 40.0,
        "current_phase_deg": 20.0,
        "overall_confidence": 0.9,
        "decision_frame_sequence": 20,
        "result_frozen": True,
        "deadline_met": True,
        "result_expiry": (expiry or datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
        "calibration_id": "process-calibration-1",
        "process_calibration_hash": "process-hash-1",
        "rotation_calibration_id": "rotation-calibration-1",
        "rotation_calibration_hash": "rotation-hash-1",
        "minimum_principle_validation_id": "validation-1",
        "minimum_principle_validation_hash": "validation-hash-1",
        "minimum_principle_validation_status": "VALIDATION_PASSED",
        "shadow_evaluation_approved": True,
        "reason_codes": ["CURVATURE_MINIMUM_CORRECTION_READY"],
    }


def _handler() -> SiemensSpsWriteHandler:
    FakeSiemensSpsClient.instances.clear()
    return SiemensSpsWriteHandler(
        "output.siemens_sps_write",
        client_factory=FakeSiemensSpsClient,
        dependency_available=lambda: True,
    )


def test_enum_mapping_gut_schlecht_writes_int_to_db() -> None:
    config = _base_config()
    config["mappings"] = [
        {
            "key": "class_to_int",
            "enabled": True,
            "source": {"field": "top_label.label"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {
                "mode": "enum",
                "enum_values": [
                    {"source": "gut", "value": 1},
                    {"source": "schlecht", "value": 0},
                ],
            },
        }
    ]
    payload = ClassificationPayload(
        labels=[ClassificationLabelScore(label="gut", score=0.92, rank=1)],
        image_context_id="ctx",
    )

    result = _handler().execute(_context(), _node(config), {"result": payload})

    client = FakeSiemensSpsClient.instances[0]
    assert client.writes == [(101, 0, struct.pack(">h", 1))]
    status = result.outputs["status"]
    assert status.success is True
    assert status.writes[0]["mapped_value"] == 1
    assert status.writes[0]["address"] == "DB101.DBW0"


def test_classification_alias_source_field_writes_int_to_db() -> None:
    config = _base_config()
    config["mappings"] = [
        {
            "key": "class_to_int",
            "enabled": True,
            "source": {"field": "classification.top1.label"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {
                "mode": "enum",
                "enum_values": [
                    {"source": "gut", "value": 1},
                    {"source": "schlecht", "value": 0},
                ],
            },
        }
    ]
    payload = ClassificationPayload(
        labels=[ClassificationLabelScore(label="gut", score=0.92, rank=1)],
        image_context_id="ctx",
    )

    _handler().execute(_context(), _node(config), {"result": payload})

    assert FakeSiemensSpsClient.instances[0].writes == [(101, 0, struct.pack(">h", 1))]


def test_confidence_scaling_writes_percent_int() -> None:
    config = _base_config()
    config["mappings"] = [
        {
            "key": "confidence_percent",
            "enabled": True,
            "source": {"field": "top_label.score"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 10, "data_type": "INT"},
            "conversion": {
                "mode": "scale",
                "multiplier": 100,
                "rounding": "nearest",
                "clamp_min": 0,
                "clamp_max": 100,
            },
        }
    ]
    payload = ClassificationPayload(
        labels=[ClassificationLabelScore(label="gut", score=0.876, rank=1)],
        image_context_id="ctx",
    )

    _handler().execute(_context(), _node(config), {"result": payload})

    assert FakeSiemensSpsClient.instances[0].writes == [(101, 10, struct.pack(">h", 88))]


def test_measurement_distance_alias_writes_real() -> None:
    config = _base_config()
    config["mappings"] = [
        {
            "key": "distance_real",
            "enabled": True,
            "source": {"field": "measurement.distance_mm"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 20, "data_type": "REAL"},
            "conversion": {"mode": "direct"},
        }
    ]
    payload = ResultPayload(result={"distance_mm": 12.5})

    _handler().execute(_context(), _node(config), {"result": payload})

    assert FakeSiemensSpsClient.instances[0].writes == [(101, 20, struct.pack(">f", 12.5))]


def test_bool_write_preserves_other_bits() -> None:
    config = _base_config()
    config["mappings"] = [
        {
            "key": "quality_pass",
            "enabled": True,
            "source": {"field": "result"},
            "target": {
                "area": "DB",
                "db_number": 101,
                "byte_offset": 8,
                "bit_offset": 2,
                "data_type": "BOOL",
            },
            "conversion": {"mode": "direct"},
        }
    ]
    handler = _handler()
    client = FakeSiemensSpsClient(SiemensSpsConnectionConfig(host="192.168.1.10"))
    client.memory[(101, 8)] = bytearray([0b00001000])
    FakeSiemensSpsClient.instances.clear()
    handler = SiemensSpsWriteHandler(
        "output.siemens_sps_write",
        client_factory=lambda _connection: client,
        dependency_available=lambda: True,
    )

    from cv_runtime.payloads import ResultPayload

    handler.execute(
        _context(),
        _node(config),
        {"result": ResultPayload(result={"result": True}, decision=True)},
    )

    assert client.reads == [(101, 8, 1)]
    assert client.writes == [(101, 8, bytes([0b00001100]))]


def test_dry_run_does_not_connect_or_write() -> None:
    config = _base_config()
    config["dry_run"] = True
    config["mappings"] = [
        {
            "key": "class_to_int",
            "enabled": True,
            "source": {"field": "top_label.label"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {
                "mode": "enum",
                "enum_values": [{"source": "gut", "value": 1}],
            },
        }
    ]
    payload = ClassificationPayload(
        labels=[ClassificationLabelScore(label="gut", score=0.92, rank=1)]
    )

    result = _handler().execute(_context(), _node(config), {"result": payload})

    assert FakeSiemensSpsClient.instances == []
    assert result.outputs["status"].status == "dry_run"
    assert result.outputs["status"].writes[0]["write_attempted"] is False


def test_stem_orientation_dry_run_emits_gated_single_main_command() -> None:
    config = _base_config()
    config.update({"dry_run": True, "stem_orientation_command_enabled": True})
    config["mappings"] = [
        {
            "key": "stem_angle",
            "enabled": True,
            "source": {"field": "stem_orientation_command.signed_target_angle_deg"},
            "target": {
                "area": "DB",
                "db_number": 101,
                "byte_offset": 20,
                "data_type": "REAL",
            },
            "conversion": {"mode": "direct"},
        }
    ]
    context = _context()
    payload = ResultPayload(result=_valid_stem_orientation_result())
    handler = _handler()

    first = handler.execute(context, _node(config), {"result": payload})
    second = handler.execute(context, _node(config), {"result": payload})

    first_status = first.outputs["status"]
    assert first_status.status == "dry_run"
    assert first_status.stem_orientation_gate["ready"] is True
    command = first_status.stem_orientation_command
    assert command["schema_version"] == "stem.orientation.sps-dry-run.v1"
    assert command["session_id"] == "stem-session-sps-1"
    assert command["track_id"] == "stem-track-sps-1"
    assert command["signed_target_angle_deg"] == 20.0
    assert command["ready"] is True
    assert command["issue_main_command"] is True
    assert first_status.writes[0]["mapped_value"] == 20.0
    assert first_status.writes[0]["write_attempted"] is False
    assert FakeSiemensSpsClient.instances == []
    second_status = second.outputs["status"]
    assert second_status.stem_orientation_command["issue_main_command"] is False
    assert second_status.writes[0]["reason"] == "main_command_already_announced"


@pytest.mark.parametrize(
    ("orientation", "reason_code"),
    [
        (
            _valid_stem_orientation_result(decision="UNCERTAIN"),
            "STEM_SPS_RESULT_UNCERTAIN",
        ),
        (
            _valid_stem_orientation_result(
                expiry=datetime.now(UTC) - timedelta(seconds=1)
            ),
            "STEM_SPS_RESULT_EXPIRED",
        ),
    ],
)
def test_stem_orientation_gate_fails_closed(
    orientation: dict[str, Any],
    reason_code: str,
) -> None:
    config = _base_config()
    config.update({"dry_run": True, "stem_orientation_command_enabled": True})
    config["mappings"] = [
        {
            "key": "stem_angle",
            "enabled": True,
            "source": {"field": "stem_orientation_command.signed_target_angle_deg"},
            "target": {
                "area": "DB",
                "db_number": 101,
                "byte_offset": 20,
                "data_type": "REAL",
            },
            "conversion": {"mode": "direct"},
        }
    ]

    result = _handler().execute(
        _context(),
        _node(config),
        {"result": ResultPayload(result=orientation)},
    )

    status = result.outputs["status"]
    assert status.status == "skipped"
    assert status.stem_orientation_gate["ready"] is False
    assert reason_code in status.stem_orientation_gate["reason_codes"]
    assert status.writes[0]["write_attempted"] is False
    assert FakeSiemensSpsClient.instances == []


def test_closed_phase_control_uses_phase_only_and_requests_stop_in_tolerance() -> None:
    config = _base_config()
    config.update(
        {
            "dry_run": True,
            "stem_orientation_command_enabled": True,
            "closed_phase_control_enabled": True,
            "sps_rotation_limits_confirmed": True,
            "phase_control_stop_tolerance_deg": 2.0,
        }
    )
    config["mappings"] = [
        {
            "key": "stop_rotation",
            "enabled": True,
            "source": {"field": "phase_control.stop_requested"},
            "target": {
                "area": "DB",
                "db_number": 101,
                "byte_offset": 30,
                "bit_offset": 0,
                "data_type": "BOOL",
            },
            "conversion": {"mode": "direct"},
        }
    ]
    orientation = _valid_stem_orientation_result()
    orientation["_sps_phase_runtime"] = {
        "track_id": "stem-track-sps-1",
        "current_phase_deg": 39.0,
        "phase_confidence": 0.9,
        "curvature_frozen": True,
        "result_frozen": True,
    }

    result = _handler().execute(
        _context(),
        _node(config),
        {"result": ResultPayload(result=orientation)},
    )

    status = result.outputs["status"]
    assert status.phase_control["available"] is True
    assert status.phase_control["stop_requested"] is True
    assert status.phase_control["curvature_recomputed"] is False
    assert status.phase_control["vision_is_safety_control"] is False
    assert status.writes[0]["mapped_value"] is True


def test_closed_phase_control_requires_confirmed_plc_limits() -> None:
    config = _base_config()
    config.update(
        {
            "stem_orientation_command_enabled": True,
            "closed_phase_control_enabled": True,
            "sps_rotation_limits_confirmed": False,
        }
    )
    config["mappings"] = [
        {
            "key": "stop_rotation",
            "enabled": True,
            "source": {"field": "phase_control.stop_requested"},
            "target": {
                "area": "DB",
                "db_number": 101,
                "byte_offset": 30,
                "bit_offset": 0,
                "data_type": "BOOL",
            },
            "conversion": {"mode": "direct"},
        }
    ]

    issues = validate_siemens_sps_write_config(
        config,
        known_fields={"phase_control.stop_requested"},
        dependency_available=True,
    )

    assert any(
        issue.code == "siemens_sps_phase_control_limits_unconfirmed"
        and issue.severity == "error"
        for issue in issues
    )


def test_stem_sps_features_are_registered_fail_closed_by_default() -> None:
    definition = BUILTIN_NODE_TYPE_BY_KEY["output.siemens_sps_write"]

    assert definition.default_config["enabled"] is False
    assert definition.default_config["dry_run"] is True
    assert definition.default_config["stem_orientation_command_enabled"] is False
    assert definition.default_config["closed_phase_control_enabled"] is False
    assert definition.default_config["sps_rotation_limits_confirmed"] is False


def test_reconnect_closes_failed_per_runtime_client_before_reusing_cache() -> None:
    config = _base_config()
    config["failure_behavior"] = "warn_continue"
    config["connection"]["reuse_policy"] = "per_runtime"
    config["mappings"] = [
        {
            "key": "first_write",
            "enabled": True,
            "source": {"field": "result"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {"mode": "direct"},
        },
        {
            "key": "second_write",
            "enabled": True,
            "source": {"field": "result"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 2, "data_type": "INT"},
            "conversion": {"mode": "direct"},
        },
    ]
    context = _context()
    FakeSiemensSpsClient.instances.clear()
    FailingFirstWriteClient.remaining_failures = 1
    handler = SiemensSpsWriteHandler(
        "output.siemens_sps_write",
        client_factory=FailingFirstWriteClient,
        dependency_available=lambda: True,
    )

    result = handler.execute(
        context,
        _node(config),
        {"result": ResultPayload(result={"result": 7})},
    )

    assert result.outputs["status"].status == "failed"
    assert len(FakeSiemensSpsClient.instances) == 2
    assert FakeSiemensSpsClient.instances[0].closed is True
    assert FakeSiemensSpsClient.instances[1].closed is False
    assert FakeSiemensSpsClient.instances[1].writes == [(101, 2, struct.pack(">h", 7))]

    assert close_siemens_sps_connection_cache(context.state) == 1
    assert FakeSiemensSpsClient.instances[1].closed is True


def test_reconnect_closes_failed_per_execution_client_before_retrying_next_mapping() -> None:
    config = _base_config()
    config["failure_behavior"] = "warn_continue"
    config["mappings"] = [
        {
            "key": "first_write",
            "enabled": True,
            "source": {"field": "result"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {"mode": "direct"},
        },
        {
            "key": "second_write",
            "enabled": True,
            "source": {"field": "result"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 2, "data_type": "INT"},
            "conversion": {"mode": "direct"},
        },
    ]
    FakeSiemensSpsClient.instances.clear()
    FailingFirstWriteClient.remaining_failures = 1
    handler = SiemensSpsWriteHandler(
        "output.siemens_sps_write",
        client_factory=FailingFirstWriteClient,
        dependency_available=lambda: True,
    )

    handler.execute(_context(), _node(config), {"result": ResultPayload(result={"result": 7})})

    assert len(FakeSiemensSpsClient.instances) == 2
    assert all(client.closed for client in FakeSiemensSpsClient.instances)
    assert FakeSiemensSpsClient.instances[1].writes == [(101, 2, struct.pack(">h", 7))]


def test_unknown_enum_value_fails_without_default() -> None:
    config = _base_config()
    config["mappings"] = [
        {
            "key": "class_to_int",
            "enabled": True,
            "source": {"field": "top_label.label"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {
                "mode": "enum",
                "enum_values": [{"source": "gut", "value": 1}],
            },
        }
    ]
    payload = ClassificationPayload(
        labels=[ClassificationLabelScore(label="unbekannt", score=0.92, rank=1)]
    )

    with pytest.raises(Exception) as exc:
        _handler().execute(_context(), _node(config), {"result": payload})

    assert "Write to Siemens SPS failed" in str(exc.value)
    assert FakeSiemensSpsClient.instances == []


def test_unknown_enum_value_can_skip_mapping_without_write() -> None:
    config = _base_config()
    config["failure_behavior"] = "skip_mapping"
    config["mappings"] = [
        {
            "key": "class_to_int",
            "enabled": True,
            "source": {"field": "top_label.label"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {
                "mode": "enum",
                "enum_values": [{"source": "gut", "value": 1}],
            },
        }
    ]
    payload = ClassificationPayload(
        labels=[ClassificationLabelScore(label="unbekannt", score=0.92, rank=1)]
    )

    result = _handler().execute(_context(), _node(config), {"result": payload})

    assert FakeSiemensSpsClient.instances == []
    status = result.outputs["status"]
    assert status.status == "skipped"
    assert status.writes[0]["reason"] == "conversion_skipped"
    assert status.writes[0]["write_attempted"] is False


def test_validation_catches_bool_bit_offset_and_int_range() -> None:
    config = _base_config()
    config["mappings"] = [
        {
            "key": "bad_bool",
            "enabled": True,
            "source": {"field": "result"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 8, "data_type": "BOOL"},
            "conversion": {"mode": "direct"},
        },
        {
            "key": "bad_enum",
            "enabled": True,
            "source": {"field": "top_label.label"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {
                "mode": "enum",
                "enum_values": [{"source": "gut", "value": 50000}],
            },
        },
    ]

    issues = validate_siemens_sps_write_config(
        config,
        known_fields={"result", "top_label.label"},
        mode=RuntimeMode.TRIGGER,
        dependency_available=True,
    )

    assert {issue.code for issue in issues if issue.severity == "error"} >= {
        "siemens_sps_bool_bit_offset_missing",
        "siemens_sps_mapping_value_invalid",
    }


def test_realtime_validation_requires_throttle_for_active_writes() -> None:
    config = _base_config()
    config["realtime_min_interval_ms"] = 100
    config["mappings"] = [
        {
            "key": "class_to_int",
            "enabled": True,
            "source": {"field": "top_label.label"},
            "target": {"area": "DB", "db_number": 101, "byte_offset": 0, "data_type": "INT"},
            "conversion": {"mode": "enum", "enum_values": [{"source": "gut", "value": 1}]},
        }
    ]

    issues = validate_siemens_sps_write_config(
        config,
        known_fields={"top_label.label"},
        mode=RuntimeMode.REALTIME,
        dependency_available=True,
    )

    assert any(issue.code == "siemens_sps_realtime_throttle_required" for issue in issues)


def test_pipeline_execution_emits_plc_write_outputs() -> None:
    source = RuntimeNodeSpec(
        node_key="classifier",
        node_type="test.classifier",
        node_category=RuntimeNodeCategory.ANALYSIS,
        input_ports=[],
        output_ports=[],
        config={},
        position={},
    )
    sink = _node(
        {
            **_base_config(),
            "mappings": [
                {
                    "key": "class_to_int",
                    "enabled": True,
                    "source": {"field": "classification.top1.label"},
                    "target": {
                        "area": "DB",
                        "db_number": 101,
                        "byte_offset": 0,
                        "data_type": "INT",
                    },
                    "conversion": {
                        "mode": "enum",
                        "enum_values": [{"source": "gut", "value": 1}],
                    },
                },
                {
                    "key": "confidence_percent",
                    "enabled": True,
                    "source": {"field": "classification.top1.score"},
                    "target": {
                        "area": "DB",
                        "db_number": 101,
                        "byte_offset": 10,
                        "data_type": "INT",
                    },
                    "conversion": {
                        "mode": "scale",
                        "multiplier": 100,
                        "rounding": "nearest",
                        "clamp_min": 0,
                        "clamp_max": 100,
                    },
                },
            ],
        }
    )

    graph = RuntimePipelineGraph(
        nodes=[source, sink],
        edges=[
            RuntimeGraphEdge(
                from_node_key="classifier",
                from_port="result",
                to_node_key="sps_write",
                to_port="result",
            )
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="pver_sps",
        graph=graph,
        handler_refs={
            "test.classifier": f"{__name__}:classifier_handler",
            "output.siemens_sps_write": f"{__name__}:sps_handler",
        },
    )

    FakeSiemensSpsClient.instances.clear()
    result = executable.execute(_context())

    assert result.status == "completed"
    assert result.plc_write_outputs[0]["payload"]["writes"][0]["mapped_value"] == 1
    assert result.plc_write_outputs[0]["payload"]["writes"][1]["mapped_value"] == 90
    assert FakeSiemensSpsClient.instances[0].writes == [
        (101, 0, struct.pack(">h", 1)),
        (101, 10, struct.pack(">h", 90)),
    ]


def test_pipeline_execution_with_measurement_distance_writes_real() -> None:
    source = RuntimeNodeSpec(
        node_key="measurement",
        node_type="test.measurement",
        node_category=RuntimeNodeCategory.ANALYSIS,
        input_ports=[],
        output_ports=[],
        config={},
        position={},
    )
    sink = _node(
        {
            **_base_config(),
            "mappings": [
                {
                    "key": "distance_real",
                    "enabled": True,
                    "source": {"field": "measurement.distance_mm"},
                    "target": {
                        "area": "DB",
                        "db_number": 101,
                        "byte_offset": 20,
                        "data_type": "REAL",
                    },
                    "conversion": {"mode": "direct"},
                }
            ],
        }
    )
    graph = RuntimePipelineGraph(
        nodes=[source, sink],
        edges=[
            RuntimeGraphEdge(
                from_node_key="measurement",
                from_port="result",
                to_node_key="sps_write",
                to_port="result",
            )
        ],
    )
    executable = ExecutablePipelineGraph.build(
        pipeline_version_id="pver_sps_measurement",
        graph=graph,
        handler_refs={
            "test.measurement": f"{__name__}:measurement_handler",
            "output.siemens_sps_write": f"{__name__}:sps_handler",
        },
    )

    FakeSiemensSpsClient.instances.clear()
    result = executable.execute(_context())

    assert result.status == "completed"
    assert FakeSiemensSpsClient.instances[0].writes == [
        (101, 20, struct.pack(">f", 12.5))
    ]


classifier_handler = ClassifierHandler()
measurement_handler = MeasurementHandler()
sps_handler = _handler()
