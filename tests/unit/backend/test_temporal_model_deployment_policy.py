from __future__ import annotations

from cv_db.models import ModelVersion
from cv_db.registry_store import _deployment_policy_errors
from cv_status import DeploymentApprovalScope


def _temporal_model_version() -> ModelVersion:
    checks = {
        "checkpoint_valid": True,
        "onnx_valid": True,
        "causal_model": True,
        "post_decision_frames_absent": True,
        "session_split_leakage_absent": True,
        "train_only_scaler": True,
        "checkpoint_roundtrip": True,
        "onnx_prediction_roundtrip": True,
        "confidence_calibration_recorded": True,
        "automatic_production_promotion_disabled": True,
    }
    artifacts = {
        name: {"uri": f"/artifacts/{name}.json", "sha256": "a" * 64}
        for name in (
            "temporal_contract",
            "scaler",
            "angle_contract",
            "head_class_order",
            "confidence_calibration",
            "temporal_evaluation",
        )
    }
    return ModelVersion(
        id="mvr-temporal-shadow",
        model_family_id="mfy-temporal",
        version_no=1,
        architecture_family="tcn",
        architecture_version="v1",
        architecture_size="small",
        architecture_display_name="Causal TCN Small",
        onnx_uri="/artifacts/model.onnx",
        evaluation_summary={
            "promotion_ready": False,
            "promotion_results": [{"metric_name": "silver/p95", "passed": True}],
        },
        export_manifest={
            "task_type": "temporal_sequence",
            "trainer_mode": "real",
            "artifact_validation": {"status": "passed", "checks": checks},
            "temporal": {
                "shadow_only": True,
                "runtime_contract_implemented": True,
                "artifacts": artifacts,
            },
            "onnx": {"uri": "/artifacts/model.onnx"},
        },
    )


def test_temporal_model_can_enter_shadow_staging_after_temporal_gates() -> None:
    errors = _deployment_policy_errors(
        _temporal_model_version(),
        approval_scope=DeploymentApprovalScope.STAGING.value,
        allow_simulation_deployment=False,
    )

    assert errors == []


def test_temporal_shadow_model_cannot_enter_production_even_with_bypass() -> None:
    errors = _deployment_policy_errors(
        _temporal_model_version(),
        approval_scope=DeploymentApprovalScope.PRODUCTION.value,
        allow_simulation_deployment=False,
        allow_quality_gate_bypass=True,
    )

    assert any("Shadow-only temporal angle models" in error for error in errors)


def test_temporal_causal_invariants_cannot_be_bypassed() -> None:
    row = _temporal_model_version()
    row.export_manifest["artifact_validation"]["checks"]["post_decision_frames_absent"] = False

    errors = _deployment_policy_errors(
        row,
        approval_scope=DeploymentApprovalScope.STAGING.value,
        allow_simulation_deployment=False,
        allow_quality_gate_bypass=True,
    )

    assert any("post_decision_frames_absent" in error for error in errors)
