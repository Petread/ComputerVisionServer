from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from cv_contracts import (
    DeploymentApprovalRecordSummary,
    DeploymentPackageDetail,
    DeploymentPackageSummary,
    DeploymentReleaseDetail,
    DeploymentTargetSummary,
    ModelFamilySummary,
    ModelLineageSummary,
    ModelPromotionSummary,
    ModelVersionComparison,
    ModelVersionComparisonField,
    ModelVersionComparisonMetric,
    ModelVersionDetail,
    ModelVersionSummary,
    PromotionCriteriaPolicy,
)
from cv_contracts.modeling import training_defaults_from_persisted
from cv_core import generate_prefixed_id
from cv_status import (
    BatchJobStatus,
    DeploymentApprovalScope,
    DeploymentApprovalStatus,
    DeploymentPackageStatus,
    DeploymentReleaseStatus,
    ModelLifecycleStatus,
    PromotionStatus,
    RuntimeExecutionStatus,
    TargetPath,
    TaskType,
)
from cv_utils import utc_now, utc_now_iso
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cv_db.model_catalog import architecture_summary
from cv_db.models import (
    BatchJob,
    DeploymentApprovalRecord,
    DeploymentRelease,
    DeploymentTarget,
    ModelFamily,
    ModelPromotion,
    ModelVersion,
    RuntimeExecution,
)


def get_model_family_record(session: Session, model_family_id: str) -> ModelFamily | None:
    return session.get(ModelFamily, model_family_id)


def get_model_family_by_slug(session: Session, slug: str) -> ModelFamily | None:
    return session.scalar(
        select(ModelFamily).where(ModelFamily.slug == slug, ModelFamily.deleted_at.is_(None))
    )


def get_model_version_record(session: Session, model_version_id: str) -> ModelVersion | None:
    return session.get(ModelVersion, model_version_id)


def _trainer_provenance(row: ModelVersion) -> tuple[str | None, str | None, dict[str, Any]]:
    immutable_metadata = row.immutable_metadata or {}
    export_manifest = row.export_manifest or {}
    evaluation_summary = row.evaluation_summary or {}
    registry_tags = row.registry_tags or {}
    trainer_mode = (
        immutable_metadata.get("trainer_mode")
        or export_manifest.get("trainer_mode")
        or evaluation_summary.get("trainer_mode")
        or registry_tags.get("cv_platform.trainer_mode")
    )
    trainer_backend = (
        immutable_metadata.get("trainer_backend")
        or export_manifest.get("trainer_backend")
        or evaluation_summary.get("trainer_backend")
        or registry_tags.get("cv_platform.trainer_backend")
    )
    artifact_validation = (
        immutable_metadata.get("artifact_validation")
        or export_manifest.get("artifact_validation")
        or {}
    )
    return (
        str(trainer_mode) if trainer_mode is not None else None,
        str(trainer_backend) if trainer_backend is not None else None,
        artifact_validation if isinstance(artifact_validation, dict) else {},
    )


def _deployment_policy_errors(
    row: ModelVersion,
    *,
    approval_scope: str,
    allow_simulation_deployment: bool,
    allow_quality_gate_bypass: bool = False,
) -> list[str]:
    trainer_mode, _trainer_backend, artifact_validation = _trainer_provenance(row)
    export_manifest = row.export_manifest or {}
    checks = artifact_validation.get("checks") or {}
    evaluation_summary = row.evaluation_summary or {}
    temporal = export_manifest.get("temporal") or {}
    temporal_model = str(export_manifest.get("task_type") or "") == TaskType.TEMPORAL_SEQUENCE.value
    errors: list[str] = []
    if trainer_mode == "simulation" and not allow_simulation_deployment:
        errors.append(
            "Simulation-trained model versions cannot be approved or promoted for deployment."
        )
    if temporal_model:
        required_temporal_checks = (
            "causal_model",
            "post_decision_frames_absent",
            "session_split_leakage_absent",
            "train_only_scaler",
            "checkpoint_roundtrip",
            "onnx_prediction_roundtrip",
            "confidence_calibration_recorded",
            "automatic_production_promotion_disabled",
        )
        failed_invariants = [
            name for name in required_temporal_checks if checks.get(name) is not True
        ]
        if failed_invariants:
            errors.append(
                "Temporal deployment invariants are missing or failed: "
                + ", ".join(failed_invariants)
                + "."
            )
        required_artifacts = {
            "temporal_contract",
            "scaler",
            "angle_contract",
            "head_class_order",
            "confidence_calibration",
            "temporal_evaluation",
        }
        temporal_artifacts = temporal.get("artifacts") if isinstance(temporal, dict) else None
        if (
            not isinstance(temporal, dict)
            or not bool(temporal.get("runtime_contract_implemented"))
            or not isinstance(temporal_artifacts, dict)
            or not required_artifacts.issubset(temporal_artifacts)
        ):
            errors.append("Temporal runtime contract artifacts are incomplete.")
        if approval_scope == DeploymentApprovalScope.PRODUCTION.value and bool(
            temporal.get("shadow_only", True)
        ):
            errors.append(
                "Shadow-only temporal angle models cannot be approved or promoted to production."
            )
        promotion_results = evaluation_summary.get("promotion_results") or []
        temporal_staging_ready = bool(promotion_results) and all(
            isinstance(item, dict) and item.get("passed") is True for item in promotion_results
        )
        if (
            approval_scope == DeploymentApprovalScope.STAGING.value
            and not allow_quality_gate_bypass
            and not temporal_staging_ready
        ):
            errors.append("Temporal model did not pass its configured shadow-staging criteria.")
        if (
            approval_scope == DeploymentApprovalScope.PRODUCTION.value
            and not allow_quality_gate_bypass
            and not bool(evaluation_summary.get("promotion_ready"))
        ):
            errors.append(
                "Temporal model did not pass its production promotion criteria."
            )
    elif not allow_quality_gate_bypass and not bool(evaluation_summary.get("promotion_ready")):
        errors.append("Model version did not pass its resolved deployment promotion criteria.")
    if trainer_mode == "real":
        if not export_manifest:
            errors.append("A real deployment requires a validated export manifest.")
        if not row.onnx_uri and not (export_manifest.get("onnx") or {}).get("uri"):
            errors.append("A real deployment requires a validated ONNX export.")
        if not artifact_validation:
            errors.append("Artifact validation metadata is missing for this model version.")
        elif not bool(checks.get("checkpoint_valid")) or not bool(checks.get("onnx_valid")):
            errors.append(
                "Artifact validation has not confirmed both the framework checkpoint "
                "and ONNX export."
            )
    return errors


def get_model_promotion_record(
    session: Session,
    *,
    model_version_id: str,
    target_path: str,
) -> ModelPromotion | None:
    return session.scalar(
        select(ModelPromotion).where(
            ModelPromotion.model_version_id == model_version_id,
            ModelPromotion.target_path == target_path,
        )
    )


def list_model_families(session: Session) -> list[ModelFamilySummary]:
    rows = session.scalars(
        select(ModelFamily)
        .where(ModelFamily.deleted_at.is_(None))
        .order_by(ModelFamily.slug.asc())
    ).all()
    versions = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    ).all()
    versions_by_family: dict[str, list[ModelVersion]] = {}
    for row in versions:
        versions_by_family.setdefault(row.model_family_id, []).append(row)
    return [
        ModelFamilySummary(
            model_family_id=row.id,
            slug=row.slug,
            task_type=TaskType(row.task_type),
            architecture=architecture_summary(
                task_type=TaskType(row.task_type),
                family=row.architecture_family,
                version=row.architecture_version,
                size=row.architecture_size,
            ),
            template_key=row.template_key,
            template_version=row.template_version,
            promotion_policy=_promotion_policy_from_row(row.promotion_policy),
            contract_major=row.contract_major,
            version_count=len(versions_by_family.get(row.id, [])),
            latest_model_version_id=(
                versions_by_family[row.id][0].id if versions_by_family.get(row.id) else None
            ),
            latest_status=(
                ModelLifecycleStatus(versions_by_family[row.id][0].status)
                if versions_by_family.get(row.id)
                else None
            ),
        )
        for row in rows
    ]


def list_model_versions(
    session: Session,
    *,
    family_slug: str | None = None,
) -> list[ModelVersionSummary]:
    family_stmt = select(ModelFamily).where(ModelFamily.deleted_at.is_(None))
    if family_slug:
        family_stmt = family_stmt.where(ModelFamily.slug == family_slug)
    families = session.scalars(family_stmt).all()
    family_lookup = {row.id: row for row in families}
    if not family_lookup:
        return []
    versions = session.scalars(
        select(ModelVersion)
        .where(
            ModelVersion.model_family_id.in_(family_lookup.keys()),
            ModelVersion.deleted_at.is_(None),
        )
        .order_by(ModelVersion.created_at.desc())
    ).all()
    promotions = session.scalars(
        select(ModelPromotion).where(ModelPromotion.model_version_id.in_([row.id for row in versions]))
    ).all()
    approvals = session.scalars(
        select(DeploymentApprovalRecord).where(
            DeploymentApprovalRecord.model_version_id.in_([row.id for row in versions])
        )
    ).all()
    promotions_by_version = _promotions_by_version(promotions)
    approvals_by_version = _approvals_by_version(approvals)
    return [
        _model_version_summary(
            row,
            family=family_lookup[row.model_family_id],
            promotions=promotions_by_version.get(row.id, []),
            approvals=approvals_by_version.get(row.id, []),
        )
        for row in versions
    ]


def get_model_version_detail(session: Session, model_version_id: str) -> ModelVersionDetail | None:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        return None
    family = get_model_family_record(session, row.model_family_id)
    if family is None:
        return None
    promotions = session.scalars(
        select(ModelPromotion)
        .where(ModelPromotion.model_version_id == model_version_id)
        .order_by(ModelPromotion.created_at.asc())
    ).all()
    approvals = session.scalars(
        select(DeploymentApprovalRecord)
        .where(DeploymentApprovalRecord.model_version_id == model_version_id)
        .order_by(DeploymentApprovalRecord.decided_at.desc())
    ).all()
    summary = _model_version_summary(row, family=family, promotions=promotions, approvals=approvals)
    _trainer_mode, _trainer_backend, artifact_validation = _trainer_provenance(row)
    return ModelVersionDetail(
        **summary.model_dump(),
        metrics_summary={key: float(value) for key, value in (row.metrics_summary or {}).items()},
        evaluation_summary=row.evaluation_summary or {},
        artifact_manifest_uri=row.artifact_manifest_uri,
        artifact_manifest=row.artifact_manifest or {},
        export_manifest_uri=row.export_manifest_uri,
        export_manifest=row.export_manifest or {},
        mlflow_registered_model=row.mlflow_registered_model,
        mlflow_model_version=row.mlflow_model_version,
        lineage=ModelLineageSummary(
            dataset_snapshot_id=row.dataset_snapshot_id,
            code_revision=row.code_revision,
            training_job_id=row.training_job_id,
            mlflow_run_id=row.mlflow_run_id,
            dvc_references=row.dvc_references or {},
            reproducibility_metadata=row.reproducibility_metadata or {},
        ),
        immutable_metadata=row.immutable_metadata or {},
        immutable_metadata_sha256=row.immutable_metadata_sha256,
        artifact_validation=artifact_validation,
        promotions=[_promotion_summary(item) for item in promotions],
        approvals=[_approval_summary(item) for item in approvals],
        archived_at=_iso(row.archived_at),
        archived_by=row.archived_by,
        archive_reason=row.archive_reason,
        rejected_at=_iso(row.rejected_at),
        rejected_by=row.rejected_by,
        rejection_reason=row.rejection_reason,
    )


def compare_model_versions(
    session: Session,
    *,
    model_version_ids: list[str],
) -> ModelVersionComparison:
    unique_ids = list(dict.fromkeys(model_version_ids))
    versions = [get_model_version_detail(session, model_version_id) for model_version_id in unique_ids]
    items = [item for item in versions if item is not None]
    metric_names = sorted({name for item in items for name in item.metrics_summary.keys()})
    metrics = [
        ModelVersionComparisonMetric(
            metric_name=metric_name,
            values={item.model_version_id: item.metrics_summary.get(metric_name) for item in items},
        )
        for metric_name in metric_names
    ]
    lineage_fields = [
        ModelVersionComparisonField(
            field_name=field_name,
            values={item.model_version_id: getattr(item.lineage, field_name) for item in items},
        )
        for field_name in ("dataset_snapshot_id", "training_job_id", "code_revision", "mlflow_run_id")
    ]
    return ModelVersionComparison(
        versions=[ModelVersionSummary.model_validate(item.model_dump()) for item in items],
        metrics=metrics,
        lineage_fields=lineage_fields,
    )


def record_deployment_approval(
    session: Session,
    *,
    model_version_id: str,
    target_path: str,
    approval_scope: str,
    status: str,
    decided_by: str | None,
    notes: str | None,
    promotion_id: str | None = None,
    deployment_release_id: str | None = None,
    allow_simulation_deployment: bool = False,
    allow_quality_gate_bypass: bool = False,
) -> DeploymentApprovalRecord:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    policy_errors = _deployment_policy_errors(
        row,
        approval_scope=approval_scope,
        allow_simulation_deployment=allow_simulation_deployment,
        allow_quality_gate_bypass=allow_quality_gate_bypass,
    )
    if policy_errors:
        raise ValueError(" ".join(policy_errors))
    approval = DeploymentApprovalRecord(
        id=generate_prefixed_id("apr"),
        model_version_id=model_version_id,
        target_path=target_path,
        approval_scope=approval_scope,
        status=status,
        decided_by=decided_by,
        decided_at=utc_now(),
        notes=notes,
        promotion_id=promotion_id,
        deployment_release_id=deployment_release_id,
    )
    session.add(approval)
    if status == DeploymentApprovalStatus.APPROVED.value and row.status not in {
        ModelLifecycleStatus.STAGED.value,
        ModelLifecycleStatus.PRODUCTION.value,
        ModelLifecycleStatus.ARCHIVED.value,
        ModelLifecycleStatus.REJECTED.value,
    }:
        row.status = ModelLifecycleStatus.VALIDATED.value
        row.validated_at = approval.decided_at
        row.validated_by = decided_by
    session.flush()
    return approval


def archive_model_version(
    session: Session,
    *,
    model_version_id: str,
    archived_by: str | None,
    reason: str | None,
) -> ModelVersion:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    if row.status == ModelLifecycleStatus.PRODUCTION.value:
        raise ValueError("A production model version cannot be archived until it is no longer active.")
    row.status = ModelLifecycleStatus.ARCHIVED.value
    row.archived_at = utc_now()
    row.archived_by = archived_by
    row.archive_reason = reason
    for promotion in session.scalars(
        select(ModelPromotion).where(ModelPromotion.model_version_id == model_version_id)
    ).all():
        if promotion.status != PromotionStatus.PRODUCTION.value:
            promotion.status = PromotionStatus.RETIRED.value
            promotion.retired_at = utc_now()
    session.flush()
    return row


def reject_model_version(
    session: Session,
    *,
    model_version_id: str,
    rejected_by: str | None,
    reason: str | None,
) -> ModelVersion:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    if row.status == ModelLifecycleStatus.PRODUCTION.value:
        raise ValueError("A production model version cannot be rejected directly.")
    row.status = ModelLifecycleStatus.REJECTED.value
    row.rejected_at = utc_now()
    row.rejected_by = rejected_by
    row.rejection_reason = reason
    for promotion in session.scalars(
        select(ModelPromotion).where(ModelPromotion.model_version_id == model_version_id)
    ).all():
        if promotion.status != PromotionStatus.PRODUCTION.value:
            promotion.status = PromotionStatus.REJECTED.value
            promotion.notes = reason
    session.flush()
    return row


def mark_rollback_candidate(
    session: Session,
    *,
    model_version_id: str,
    target_path: str,
    enabled: bool,
    marked_by: str | None,
    notes: str | None,
) -> ModelPromotion:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    promotion = get_model_promotion_record(
        session,
        model_version_id=model_version_id,
        target_path=target_path,
    )
    if promotion is None:
        promotion = ModelPromotion(
            id=generate_prefixed_id("prm"),
            model_version_id=model_version_id,
            target_path=target_path,
            requested_by=marked_by,
            requested_environment="prod",
            status=(
                PromotionStatus.PRODUCTION.value
                if row.status == ModelLifecycleStatus.PRODUCTION.value
                else PromotionStatus.STAGED.value
            ),
        )
        session.add(promotion)
    promotion.rollback_candidate = enabled
    promotion.rollback_marked_by = marked_by
    promotion.rollback_marked_at = utc_now() if enabled else None
    if notes:
        promotion.notes = notes
    session.flush()
    return promotion


def create_promotion_request(
    session: Session,
    *,
    model_version_id: str,
    target_path: str,
    requested_by: str | None,
    requested_state: Literal["staged", "production"],
    allow_simulation_deployment: bool = False,
    allow_quality_gate_bypass: bool = False,
) -> str:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    if row.status in {ModelLifecycleStatus.ARCHIVED.value, ModelLifecycleStatus.REJECTED.value}:
        raise ValueError("Archived or rejected model versions cannot be deployed.")
    policy_errors = _deployment_policy_errors(
        row,
        approval_scope=(
            DeploymentApprovalScope.STAGING.value
            if requested_state == "staged"
            else DeploymentApprovalScope.PRODUCTION.value
        ),
        allow_simulation_deployment=allow_simulation_deployment,
        allow_quality_gate_bypass=allow_quality_gate_bypass,
    )
    if policy_errors:
        raise ValueError(" ".join(policy_errors))
    _require_approval(
        session,
        model_version_id=model_version_id,
        target_path=target_path,
        approval_scope=(
            DeploymentApprovalScope.STAGING.value
            if requested_state == "staged"
            else DeploymentApprovalScope.PRODUCTION.value
        ),
    )
    promotion = get_model_promotion_record(
        session,
        model_version_id=model_version_id,
        target_path=target_path,
    )
    if promotion is None:
        promotion = ModelPromotion(
            id=generate_prefixed_id("prm"),
            model_version_id=model_version_id,
            target_path=target_path,
            requested_by=requested_by,
            requested_environment="prod",
        )
        session.add(promotion)
    promotion.requested_by = requested_by
    promotion.status = (
        PromotionStatus.STAGING_REQUESTED.value
        if requested_state == "staged"
        else PromotionStatus.PRODUCTION_REQUESTED.value
    )
    session.flush()
    return promotion.id


def update_model_serving_package(
    session: Session,
    *,
    model_version_id: str,
    status: DeploymentPackageStatus,
    built_at: Any | None = None,
    serving_manifest_uri: str | None = None,
    serving_manifest: dict[str, Any] | None = None,
    error: str | None = None,
) -> ModelVersion:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        raise ValueError(f"Model version {model_version_id} does not exist.")
    row.serving_package_status = status.value
    row.serving_package_error = error
    if built_at is not None:
        row.serving_package_built_at = built_at
    if serving_manifest_uri is not None:
        row.serving_manifest_uri = serving_manifest_uri
    if serving_manifest is not None:
        row.serving_manifest = serving_manifest
    session.flush()
    return row


def list_deployment_packages(session: Session) -> list[DeploymentPackageSummary]:
    versions = session.scalars(
        select(ModelVersion)
        .where(ModelVersion.deleted_at.is_(None))
        .order_by(ModelVersion.created_at.desc())
    ).all()
    families = {
        row.id: row
        for row in session.scalars(select(ModelFamily).where(ModelFamily.deleted_at.is_(None))).all()
    }
    items: list[DeploymentPackageSummary] = []
    for row in versions:
        family = families.get(row.model_family_id)
        if family is None:
            continue
        manifest = row.serving_manifest or {}
        compatible_targets = [
            TargetPath(item) if isinstance(item, str) else item
            for item in manifest.get("compatible_targets", [])
        ]
        items.append(
            DeploymentPackageSummary(
                model_version_id=row.id,
                model_family_id=row.model_family_id,
                model_family_slug=family.slug,
                version_no=row.version_no,
                task_type=TaskType(family.task_type),
                architecture=architecture_summary(
                    task_type=TaskType(row.task_type if hasattr(row, "task_type") else family.task_type),
                    family=row.architecture_family,
                    version=row.architecture_version,
                    size=row.architecture_size,
                ),
                status=DeploymentPackageStatus(row.serving_package_status),
                built_at=_iso(row.serving_package_built_at),
                serving_manifest_uri=row.serving_manifest_uri,
                compatible_targets=compatible_targets,
                error=row.serving_package_error,
            )
        )
    return items


def get_deployment_package_detail(
    session: Session,
    *,
    model_version_id: str,
) -> DeploymentPackageDetail | None:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        return None
    family = get_model_family_record(session, row.model_family_id)
    if family is None:
        return None
    serving_manifest = row.serving_manifest or _read_json_if_exists(row.serving_manifest_uri)
    export_manifest = row.export_manifest or _read_json_if_exists(row.export_manifest_uri)
    artifact_manifest = row.artifact_manifest or _read_json_if_exists(row.artifact_manifest_uri)
    return DeploymentPackageDetail(
        model_version_id=row.id,
        model_family_id=row.model_family_id,
        model_family_slug=family.slug,
        version_no=row.version_no,
        task_type=TaskType(family.task_type),
        architecture=architecture_summary(
            task_type=TaskType(row.task_type if hasattr(row, "task_type") else family.task_type),
            family=row.architecture_family,
            version=row.architecture_version,
            size=row.architecture_size,
        ),
        status=DeploymentPackageStatus(row.serving_package_status),
        built_at=_iso(row.serving_package_built_at),
        serving_manifest_uri=row.serving_manifest_uri,
        compatible_targets=[
            TargetPath(item) if isinstance(item, str) else item
            for item in serving_manifest.get("compatible_targets", [])
        ],
        error=row.serving_package_error,
        export_manifest_uri=row.export_manifest_uri,
        artifact_manifest_uri=row.artifact_manifest_uri,
        serving_manifest=serving_manifest,
        export_manifest=export_manifest,
        artifact_manifest=artifact_manifest,
    )


def create_deployment_release_for_promotion(
    session: Session,
    *,
    promotion_id: str,
) -> DeploymentRelease:
    promotion = session.get(ModelPromotion, promotion_id)
    if promotion is None:
        raise ValueError(f"Promotion {promotion_id} does not exist.")
    if promotion.deployment_release_id:
        existing = session.get(DeploymentRelease, promotion.deployment_release_id)
        if existing is not None:
            return existing
    model_version = get_model_version_record(session, promotion.model_version_id)
    if model_version is None:
        raise ValueError("Promotion references an unknown model version.")
    family = get_model_family_record(session, model_version.model_family_id)
    if family is None:
        raise ValueError("Model family referenced by the model version does not exist.")

    current_release = get_active_release_for_target(session, target_path=promotion.target_path)
    next_triton_version = (
        session.scalar(
            select(func.max(DeploymentRelease.triton_model_version)).where(
                DeploymentRelease.target_path == promotion.target_path,
                DeploymentRelease.model_family_id == model_version.model_family_id,
            )
        )
        or 0
    ) + 1
    release = DeploymentRelease(
        id=generate_prefixed_id("rel"),
        target_path=promotion.target_path,
        model_family_id=model_version.model_family_id,
        model_version_id=model_version.id,
        triton_model_name=_triton_model_name(
            target_path=TargetPath(promotion.target_path),
            family_slug=family.slug,
            contract_major=family.contract_major,
        ),
        triton_model_version=next_triton_version,
        status=DeploymentReleaseStatus.REQUESTED.value,
        artifact_manifest_uri=(
            model_version.serving_manifest_uri
            or model_version.export_manifest_uri
            or model_version.artifact_manifest_uri
        ),
        rollback_source_release_id=current_release.id if current_release else None,
    )
    session.add(release)
    session.flush()
    promotion.deployment_release_id = release.id
    session.flush()
    return release


def update_deployment_release_status(
    session: Session,
    *,
    deployment_release_id: str,
    status: DeploymentReleaseStatus,
    compatibility_report: dict[str, Any] | None = None,
    smoke_report_uri: str | None = None,
    smoke_report: dict[str, Any] | None = None,
) -> DeploymentRelease:
    release = session.get(DeploymentRelease, deployment_release_id)
    if release is None:
        raise ValueError(f"Deployment release {deployment_release_id} does not exist.")
    release.status = status.value
    if compatibility_report is not None:
        release.compatibility_report = compatibility_report
    if smoke_report_uri is not None:
        release.smoke_report_uri = smoke_report_uri
    if smoke_report is not None:
        release.smoke_report = smoke_report
    if status == DeploymentReleaseStatus.ACTIVATED:
        release.activated_at = utc_now()
    if status == DeploymentReleaseStatus.RETIRED:
        release.retired_at = utc_now()
    session.flush()
    _refresh_runtime_model_capabilities(session)
    return release


def mark_promotion_staged(
    session: Session,
    *,
    promotion_id: str,
) -> ModelPromotion:
    promotion = session.get(ModelPromotion, promotion_id)
    if promotion is None:
        raise ValueError(f"Promotion {promotion_id} does not exist.")
    release = session.get(DeploymentRelease, promotion.deployment_release_id)
    if release is None:
        raise ValueError("The promotion has no deployment release to activate.")
    release.status = DeploymentReleaseStatus.ACTIVATED.value
    release.activated_at = utc_now()
    promotion.status = PromotionStatus.STAGED.value
    promotion.validated_at = utc_now()
    session.flush()
    _recalculate_model_status(session, promotion.model_version_id)
    _refresh_runtime_model_capabilities(session)
    return promotion


def promote_staged_release_to_production(
    session: Session,
    *,
    promotion_id: str,
) -> DeploymentTarget:
    promotion = session.get(ModelPromotion, promotion_id)
    if promotion is None:
        raise ValueError(f"Promotion {promotion_id} does not exist.")
    if promotion.deployment_release_id is None:
        raise ValueError("The selected promotion has no staged deployment release.")
    release = session.get(DeploymentRelease, promotion.deployment_release_id)
    if release is None:
        raise ValueError("The staged deployment release no longer exists.")
    if promotion.status not in {
        PromotionStatus.STAGED.value,
        PromotionStatus.PRODUCTION_REQUESTED.value,
    }:
        raise ValueError("Only staged releases can be promoted to production.")
    smoke = release.smoke_report or {}
    if not (smoke.get("passed") or (
        smoke.get("runtime_validation_pending") is True and smoke.get("package_verified") is True
    )):
        raise ValueError("Only releases with a passing smoke test can be promoted to production.")
    if release.compatibility_report and not bool(release.compatibility_report.get("compatible", True)):
        raise ValueError("Only compatibility-verified releases can be promoted to production.")

    target = session.get(DeploymentTarget, promotion.target_path)
    if target is None:
        target = DeploymentTarget(
            target_path=promotion.target_path,
            active_release_id=None,
            previous_release_id=None,
            updated_at=utc_now(),
        )
        session.add(target)

    previous_active_release_id = target.active_release_id
    explicit_rollback = _pick_explicit_rollback_release(
        session,
        target_path=promotion.target_path,
        exclude_model_version_id=promotion.model_version_id,
    )
    # Runtime bindings follow production within a family. Promoting another
    # family changes the target default without demoting unrelated models.
    previous_family_promotions = session.scalars(
        select(ModelPromotion)
        .join(ModelVersion, ModelVersion.id == ModelPromotion.model_version_id)
        .where(
            ModelVersion.model_family_id == release.model_family_id,
            ModelPromotion.target_path == promotion.target_path,
            ModelPromotion.status == PromotionStatus.PRODUCTION.value,
            ModelPromotion.id != promotion.id,
        )
    ).all()
    for previous_promotion in previous_family_promotions:
        previous_promotion.status = PromotionStatus.STAGED.value
        previous_promotion.rollback_candidate = True
        previous_promotion.rollback_marked_at = previous_promotion.rollback_marked_at or utc_now()
    target.previous_release_id = explicit_rollback or previous_active_release_id
    target.active_release_id = release.id
    target.updated_at = utc_now()
    promotion.status = PromotionStatus.PRODUCTION.value
    release.status = DeploymentReleaseStatus.ACTIVATED.value
    release.activated_at = utc_now()
    session.flush()
    _recalculate_model_status(session, promotion.model_version_id)
    for previous_promotion in previous_family_promotions:
        _recalculate_model_status(session, previous_promotion.model_version_id)
    if previous_active_release_id:
        previous_release = session.get(DeploymentRelease, previous_active_release_id)
        if previous_release is not None:
            _recalculate_model_status(session, previous_release.model_version_id)
    _refresh_runtime_model_capabilities(session)
    return target


def request_rollback_for_target(session: Session, *, target_path: str) -> str:
    target = session.get(DeploymentTarget, target_path)
    if target is None or target.active_release_id is None or target.previous_release_id is None:
        raise ValueError(f"Target {target_path} does not have a rollback candidate.")
    active_release = session.get(DeploymentRelease, target.active_release_id)
    if active_release is None:
        raise ValueError("The active deployment release no longer exists.")
    active_release.status = DeploymentReleaseStatus.ROLLBACK_REQUESTED.value
    session.flush()
    return active_release.id


def rollback_target_to_previous_release(session: Session, *, target_path: str) -> DeploymentTarget:
    target = session.get(DeploymentTarget, target_path)
    if target is None or target.active_release_id is None or target.previous_release_id is None:
        raise ValueError(f"Target {target_path} does not have a previous release to roll back to.")
    failed_release_id = target.active_release_id
    rollback_release_id = target.previous_release_id
    failed_release = session.get(DeploymentRelease, failed_release_id)
    rollback_release = session.get(DeploymentRelease, rollback_release_id)
    if failed_release is None or rollback_release is None:
        raise ValueError("Rollback could not resolve both active and previous releases.")

    failed_release.status = DeploymentReleaseStatus.ROLLED_BACK.value
    rollback_release.status = DeploymentReleaseStatus.ACTIVATED.value
    rollback_release.activated_at = utc_now()
    target.active_release_id = rollback_release_id
    target.previous_release_id = failed_release_id
    target.updated_at = utc_now()

    failed_promotion = _promotion_by_release_id(session, failed_release_id)
    if failed_promotion is not None:
        failed_promotion.status = PromotionStatus.STAGED.value
    rollback_promotion = _promotion_by_release_id(session, rollback_release_id)
    if rollback_promotion is not None:
        rollback_promotion.status = PromotionStatus.PRODUCTION.value
        rollback_promotion.rollback_candidate = False
    session.flush()

    if failed_promotion is not None:
        _recalculate_model_status(session, failed_promotion.model_version_id)
    if rollback_promotion is not None:
        _recalculate_model_status(session, rollback_promotion.model_version_id)
    _refresh_runtime_model_capabilities(session)
    return target


def get_active_release_for_target(session: Session, *, target_path: str) -> DeploymentRelease | None:
    target = session.get(DeploymentTarget, target_path)
    if target is None or target.active_release_id is None:
        return None
    return session.get(DeploymentRelease, target.active_release_id)


_ACTIVE_RUNTIME_EXECUTION_STATUSES = {
    RuntimeExecutionStatus.REQUESTED.value,
    RuntimeExecutionStatus.ADMITTED.value,
    RuntimeExecutionStatus.VALIDATING.value,
    RuntimeExecutionStatus.LOADING_PIPELINE.value,
    RuntimeExecutionStatus.STARTING.value,
    RuntimeExecutionStatus.RUNNING.value,
    RuntimeExecutionStatus.DRAINING.value,
    RuntimeExecutionStatus.FAILED_RETRYABLE.value,
}

_ACTIVE_BATCH_JOB_STATUSES = {
    BatchJobStatus.ACCEPTED.value,
    BatchJobStatus.QUEUED.value,
    BatchJobStatus.RUNNING.value,
}

_TRANSITIONING_DEPLOYMENT_RELEASE_STATUSES = {
    DeploymentReleaseStatus.REQUESTED.value,
    DeploymentReleaseStatus.PACKAGING.value,
    DeploymentReleaseStatus.REPOSITORY_SYNC.value,
    DeploymentReleaseStatus.LOADING.value,
    DeploymentReleaseStatus.SMOKE_TESTING.value,
    DeploymentReleaseStatus.ROLLBACK_REQUESTED.value,
}


def validate_deployment_model_serving_command(
    session: Session,
    *,
    deployment_release_id: str,
    enable: bool,
) -> DeploymentRelease:
    release = session.get(DeploymentRelease, deployment_release_id)
    if release is None or release.deleted_at is not None:
        raise ValueError(f"Deployment release {deployment_release_id} does not exist.")
    if release.retired_at is not None or release.status == DeploymentReleaseStatus.RETIRED.value:
        raise ValueError("A retired deployment release cannot change serving state.")

    siblings = _deployment_model_release_group(session, release=release)
    activated = [
        item
        for item in siblings
        if item.status == DeploymentReleaseStatus.ACTIVATED.value
        and item.retired_at is None
    ]
    transitioning_siblings = [
        item
        for item in siblings
        if item.id != release.id
        and item.status in _TRANSITIONING_DEPLOYMENT_RELEASE_STATUSES
    ]
    if transitioning_siblings:
        raise ValueError(
            "The Triton model has another deployment release in progress: "
            f"{', '.join(sorted(item.id for item in transitioning_siblings))}."
        )
    if enable:
        if activated:
            if any(item.id == release.id for item in activated):
                return release
            raise ValueError(
                "This Triton model is already enabled through deployment release "
                f"{activated[-1].id}. Deactivate it before selecting another release."
            )
        if release.status not in {
            DeploymentReleaseStatus.DEACTIVATED.value,
            DeploymentReleaseStatus.LOADING.value,
        }:
            raise ValueError("Only a deactivated deployment release can be activated again.")
        version = session.get(ModelVersion, release.model_version_id)
        if version is None or version.deleted_at is not None:
            raise ValueError("The model version for this deployment release is unavailable.")
        if version.status in {
            ModelLifecycleStatus.ARCHIVED.value,
            ModelLifecycleStatus.REJECTED.value,
        }:
            raise ValueError("Archived or rejected model versions cannot be activated.")
        return release

    if release.status in _TRANSITIONING_DEPLOYMENT_RELEASE_STATUSES:
        raise ValueError(
            "The selected deployment release is still changing state and cannot be "
            "deactivated yet."
        )
    if not activated:
        return release

    sibling_ids = {item.id for item in siblings}
    from cv_db.runtime_model_store import demanded_release_ids
    if sibling_ids.intersection(demanded_release_ids(session)):
        raise ValueError("The model is still required by a started or draining channel.")
    target_refs = session.scalars(select(DeploymentTarget)).all()
    active_targets = sorted(
        item.target_path for item in target_refs if item.active_release_id in sibling_ids
    )
    if active_targets:
        raise ValueError(
            "The Triton model is still the active production release for target(s): "
            f"{', '.join(active_targets)}. Promote or roll back to another model first."
        )
    rollback_targets = sorted(
        item.target_path for item in target_refs if item.previous_release_id in sibling_ids
    )
    if rollback_targets:
        raise ValueError(
            "The Triton model is still reserved as rollback release for target(s): "
            f"{', '.join(rollback_targets)}. Replace the rollback candidate first."
        )

    runtime_channels = sorted(
        {
            execution.channel_id
            for execution in session.scalars(
                select(RuntimeExecution).where(
                    RuntimeExecution.status.in_(_ACTIVE_RUNTIME_EXECUTION_STATUSES)
                )
            ).all()
            if _runtime_plan_uses_any_release(execution.runtime_plan, sibling_ids)
        }
    )
    if runtime_channels:
        raise ValueError(
            "The Triton model is still bound to running runtime channel(s): "
            f"{', '.join(runtime_channels)}. Stop those channels first."
        )

    active_batch_jobs = session.scalars(
        select(BatchJob).where(
            BatchJob.deployment_release_id.in_(sibling_ids),
            BatchJob.status.in_(_ACTIVE_BATCH_JOB_STATUSES),
        )
    ).all()
    if active_batch_jobs:
        raise ValueError(
            "The Triton model is still used by active batch job(s): "
            f"{', '.join(sorted(item.id for item in active_batch_jobs))}."
        )
    return release


def deactivate_deployment_model_releases(
    session: Session,
    *,
    deployment_release_id: str,
) -> tuple[DeploymentRelease, list[str]]:
    release = validate_deployment_model_serving_command(
        session,
        deployment_release_id=deployment_release_id,
        enable=False,
    )
    changed_ids: list[str] = []
    for item in _deployment_model_release_group(session, release=release):
        if item.status != DeploymentReleaseStatus.ACTIVATED.value:
            continue
        item.status = DeploymentReleaseStatus.DEACTIVATED.value
        changed_ids.append(item.id)
    session.flush()
    _refresh_runtime_model_capabilities(session)
    return release, changed_ids


def activate_deployment_model_release(
    session: Session,
    *,
    deployment_release_id: str,
) -> DeploymentRelease:
    release = validate_deployment_model_serving_command(
        session,
        deployment_release_id=deployment_release_id,
        enable=True,
    )
    release.status = DeploymentReleaseStatus.ACTIVATED.value
    release.activated_at = utc_now()
    session.flush()
    _refresh_runtime_model_capabilities(session)
    return release


def _deployment_model_release_group(
    session: Session,
    *,
    release: DeploymentRelease,
) -> list[DeploymentRelease]:
    return session.scalars(
        select(DeploymentRelease)
        .where(
            DeploymentRelease.target_path == release.target_path,
            DeploymentRelease.triton_model_name == release.triton_model_name,
            DeploymentRelease.deleted_at.is_(None),
        )
        .order_by(
            DeploymentRelease.triton_model_version.asc(),
            DeploymentRelease.created_at.asc(),
        )
    ).all()


def _runtime_plan_uses_any_release(
    runtime_plan: dict[str, Any] | None,
    deployment_release_ids: set[str],
) -> bool:
    binding_signature = dict((runtime_plan or {}).get("binding_signature") or {})
    bindings = binding_signature.get("model_bindings") or []
    return any(
        isinstance(item, dict)
        and str(item.get("deployment_release_id") or "") in deployment_release_ids
        for item in bindings
    )


def list_deployment_releases(session: Session) -> list:
    from cv_contracts import DeploymentReleaseSummary

    rows = session.scalars(
        select(DeploymentRelease)
        .where(DeploymentRelease.deleted_at.is_(None))
        .order_by(DeploymentRelease.created_at.desc())
    ).all()
    versions = {
        row.id: row
        for row in session.scalars(
            select(ModelVersion).where(ModelVersion.deleted_at.is_(None))
        ).all()
    }
    model_family_ids = {row.model_family_id for row in rows}
    families = {
        row.id: row
        for row in session.scalars(
            select(ModelFamily).where(ModelFamily.id.in_(model_family_ids))
        ).all()
    }
    serving_release_ids = _serving_release_ids_by_model(rows)
    return [
        DeploymentReleaseSummary(
            deployment_release_id=row.id,
            target_path=row.target_path,
            model_version_id=row.model_version_id,
            architecture=_deployment_release_architecture(
                version=versions.get(row.model_version_id),
                family=families.get(row.model_family_id),
            ),
            triton_model_name=row.triton_model_name,
            triton_model_version=row.triton_model_version,
            status=DeploymentReleaseStatus(row.status),
            serving_enabled=(row.target_path, row.triton_model_name) in serving_release_ids,
            serving_release_id=serving_release_ids.get((row.target_path, row.triton_model_name)),
            activated_at=_iso(row.activated_at),
            rollback_source_release_id=row.rollback_source_release_id,
        )
        for row in rows
    ]


def get_deployment_release_detail(
    session: Session,
    *,
    deployment_release_id: str,
) -> DeploymentReleaseDetail | None:
    row = session.get(DeploymentRelease, deployment_release_id)
    if row is None:
        return None
    version = get_model_version_record(session, row.model_version_id)
    family = get_model_family_record(session, row.model_family_id)
    serving_release_ids = _serving_release_ids_by_model(
        _deployment_model_release_group(session, release=row)
    )
    return DeploymentReleaseDetail(
        deployment_release_id=row.id,
        target_path=row.target_path,
        model_version_id=row.model_version_id,
        architecture=_deployment_release_architecture(version=version, family=family),
        triton_model_name=row.triton_model_name,
        triton_model_version=row.triton_model_version,
        status=DeploymentReleaseStatus(row.status),
        serving_enabled=(row.target_path, row.triton_model_name) in serving_release_ids,
        serving_release_id=serving_release_ids.get((row.target_path, row.triton_model_name)),
        activated_at=_iso(row.activated_at),
        rollback_source_release_id=row.rollback_source_release_id,
        artifact_manifest_uri=row.artifact_manifest_uri,
        artifact_manifest=_read_json_if_exists(row.artifact_manifest_uri),
        smoke_report_uri=row.smoke_report_uri,
        smoke_report=row.smoke_report or _read_json_if_exists(row.smoke_report_uri),
        compatibility_report=row.compatibility_report or {},
    )


def _deployment_release_architecture(
    *,
    version: ModelVersion | None,
    family: ModelFamily | None,
):
    architecture_source = version or family
    return architecture_summary(
        task_type=TaskType(family.task_type) if family is not None else TaskType.CLASSIFICATION,
        family=architecture_source.architecture_family if architecture_source is not None else "yolo",
        version=architecture_source.architecture_version if architecture_source is not None else "v8",
        size=architecture_source.architecture_size if architecture_source is not None else "n",
    )


def _serving_release_ids_by_model(
    rows: list[DeploymentRelease],
) -> dict[tuple[str, str], str]:
    serving: dict[tuple[str, str], DeploymentRelease] = {}
    for row in rows:
        if (
            row.status != DeploymentReleaseStatus.ACTIVATED.value
            or row.retired_at is not None
            or row.deleted_at is not None
        ):
            continue
        key = (row.target_path, row.triton_model_name)
        current = serving.get(key)
        if current is None or (
            int(row.triton_model_version or 0), str(row.activated_at or ""), row.id
        ) > (
            int(current.triton_model_version or 0),
            str(current.activated_at or ""),
            current.id,
        ):
            serving[key] = row
    return {key: row.id for key, row in serving.items()}


def list_deployment_targets(session: Session) -> list[DeploymentTargetSummary]:
    releases = {
        row.id: row for row in session.scalars(select(DeploymentRelease)).all()
    }
    items: list[DeploymentTargetSummary] = []
    for row in session.scalars(select(DeploymentTarget).order_by(DeploymentTarget.target_path.asc())).all():
        active_release = releases.get(row.active_release_id) if row.active_release_id else None
        previous_release = releases.get(row.previous_release_id) if row.previous_release_id else None
        items.append(
            DeploymentTargetSummary(
                target_path=TargetPath(row.target_path),
                active_release_id=row.active_release_id,
                previous_release_id=row.previous_release_id,
                active_model_version_id=active_release.model_version_id if active_release else None,
                previous_model_version_id=previous_release.model_version_id if previous_release else None,
                active_release_status=(
                    DeploymentReleaseStatus(active_release.status) if active_release else None
                ),
                updated_at=_iso(row.updated_at),
            )
        )
    return items


def _model_version_summary(
    row: ModelVersion,
    *,
    family: ModelFamily,
    promotions: list[ModelPromotion],
    approvals: list[DeploymentApprovalRecord],
) -> ModelVersionSummary:
    trainer_mode, trainer_backend, _artifact_validation = _trainer_provenance(row)
    primary_metric_name, primary_metric_value = _primary_metric(row)
    target_statuses = {item.target_path: item.status for item in promotions}
    rollback_candidate_targets = [
        TargetPath(item.target_path) for item in promotions if item.rollback_candidate
    ]
    deployable_for = sorted(
        {
            item.target_path
            for item in approvals
            if item.status == DeploymentApprovalStatus.APPROVED.value
        }
    )
    return ModelVersionSummary(
        model_version_id=row.id,
        model_family_id=row.model_family_id,
        model_family_slug=family.slug,
        version_no=row.version_no,
        status=ModelLifecycleStatus(row.status),
        architecture=architecture_summary(
            task_type=TaskType(family.task_type),
            family=row.architecture_family,
            version=row.architecture_version,
            size=row.architecture_size,
        ),
        model_template_key=row.model_template_key,
        model_template_version=row.model_template_version,
        resolved_training_defaults=training_defaults_from_persisted(
            row.resolved_training_defaults or {}
        ),
        promotion_policy=_promotion_policy_from_row(row.promotion_policy),
        training_job_id=row.training_job_id,
        dataset_snapshot_id=row.dataset_snapshot_id,
        code_revision=row.code_revision,
        primary_metric_name=primary_metric_name,
        primary_metric_value=primary_metric_value,
        trainer_mode=trainer_mode,
        trainer_backend=trainer_backend,
        alias_names=row.alias_names or [],
        registry_tags=row.registry_tags or {},
        target_statuses=target_statuses,
        rollback_candidate_targets=rollback_candidate_targets,
        deployable_for=[TargetPath(item) for item in deployable_for],
        created_at=_iso(row.created_at) or utc_now_iso(),
    )


def _promotions_by_version(items: list[ModelPromotion]) -> dict[str, list[ModelPromotion]]:
    grouped: dict[str, list[ModelPromotion]] = {}
    for item in items:
        grouped.setdefault(item.model_version_id, []).append(item)
    return grouped


def _approvals_by_version(items: list[DeploymentApprovalRecord]) -> dict[str, list[DeploymentApprovalRecord]]:
    grouped: dict[str, list[DeploymentApprovalRecord]] = {}
    for item in items:
        grouped.setdefault(item.model_version_id, []).append(item)
    return grouped


def _promotion_summary(row: ModelPromotion) -> ModelPromotionSummary:
    return ModelPromotionSummary(
        promotion_id=row.id,
        target_path=TargetPath(row.target_path),
        status=PromotionStatus(row.status),
        requested_by=row.requested_by,
        deployment_release_id=row.deployment_release_id,
        rollback_candidate=row.rollback_candidate,
        rollback_marked_at=_iso(row.rollback_marked_at),
        validated_at=_iso(row.validated_at),
        notes=row.notes,
    )


def _approval_summary(row: DeploymentApprovalRecord) -> DeploymentApprovalRecordSummary:
    return DeploymentApprovalRecordSummary(
        approval_record_id=row.id,
        model_version_id=row.model_version_id,
        target_path=TargetPath(row.target_path),
        approval_scope=DeploymentApprovalScope(row.approval_scope),
        status=DeploymentApprovalStatus(row.status),
        decided_by=row.decided_by,
        decided_at=_iso(row.decided_at) or utc_now_iso(),
        notes=row.notes,
        promotion_id=row.promotion_id,
        deployment_release_id=row.deployment_release_id,
    )


def _primary_metric(row: ModelVersion) -> tuple[str | None, float | None]:
    evaluation = row.evaluation_summary or {}
    primary_name = evaluation.get("primary_metric_name")
    if primary_name and primary_name in (row.metrics_summary or {}):
        return str(primary_name), float(row.metrics_summary[primary_name])
    metrics = row.metrics_summary or {}
    if primary_name and evaluation.get("primary_metric_value") is not None:
        return str(primary_name), float(evaluation["primary_metric_value"])
    if not metrics:
        return None, None
    metric_name = sorted(metrics.keys())[0]
    return metric_name, float(metrics[metric_name])


def _promotion_policy_from_row(payload: dict[str, Any] | None) -> PromotionCriteriaPolicy | None:
    if not payload:
        return None
    return PromotionCriteriaPolicy.model_validate(payload)


def _require_approval(
    session: Session,
    *,
    model_version_id: str,
    target_path: str,
    approval_scope: str,
) -> None:
    approved = session.scalar(
        select(DeploymentApprovalRecord)
        .where(
            DeploymentApprovalRecord.model_version_id == model_version_id,
            DeploymentApprovalRecord.target_path == target_path,
            DeploymentApprovalRecord.approval_scope == approval_scope,
            DeploymentApprovalRecord.status == DeploymentApprovalStatus.APPROVED.value,
        )
        .order_by(DeploymentApprovalRecord.decided_at.desc())
    )
    if approved is None:
        raise ValueError(
            f"Model version {model_version_id} does not have an approved {approval_scope} record for {target_path}."
        )


def _pick_explicit_rollback_release(
    session: Session,
    *,
    target_path: str,
    exclude_model_version_id: str,
) -> str | None:
    promotion = session.scalar(
        select(ModelPromotion)
        .where(
            ModelPromotion.target_path == target_path,
            ModelPromotion.rollback_candidate.is_(True),
            ModelPromotion.model_version_id != exclude_model_version_id,
            ModelPromotion.deployment_release_id.is_not(None),
        )
        .order_by(ModelPromotion.rollback_marked_at.desc())
    )
    return promotion.deployment_release_id if promotion and promotion.deployment_release_id else None


def _promotion_by_release_id(session: Session, deployment_release_id: str) -> ModelPromotion | None:
    return session.scalar(
        select(ModelPromotion).where(ModelPromotion.deployment_release_id == deployment_release_id)
    )


def _recalculate_model_status(session: Session, model_version_id: str) -> ModelVersion | None:
    row = get_model_version_record(session, model_version_id)
    if row is None:
        return None
    if row.status in {ModelLifecycleStatus.ARCHIVED.value, ModelLifecycleStatus.REJECTED.value}:
        return row
    promotions = session.scalars(
        select(ModelPromotion).where(ModelPromotion.model_version_id == model_version_id)
    ).all()
    if any(item.status == PromotionStatus.PRODUCTION.value for item in promotions):
        row.status = ModelLifecycleStatus.PRODUCTION.value
    elif any(item.status == PromotionStatus.STAGED.value for item in promotions):
        row.status = ModelLifecycleStatus.STAGED.value
    elif row.validated_at is not None:
        row.status = ModelLifecycleStatus.VALIDATED.value
    elif row.training_job_id or row.immutable_metadata_sha256:
        row.status = ModelLifecycleStatus.TRAINED.value
    else:
        row.status = ModelLifecycleStatus.DRAFT.value
    session.flush()
    return row


def _triton_model_name(*, target_path: TargetPath, family_slug: str, contract_major: int) -> str:
    prefix = "rt" if target_path == TargetPath.REALTIME else "bt"
    safe_slug = re.sub(r"[^a-z0-9]+", "_", family_slug.lower()).strip("_")
    return f"{prefix}__{safe_slug}_v{contract_major}"


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    return str(value)


def _refresh_runtime_model_capabilities(session: Session) -> None:
    from cv_db.runtime_execution_store import refresh_runtime_model_capability_cache

    refresh_runtime_model_capability_cache(session)


def _read_json_if_exists(path_str: str | None) -> dict[str, Any]:
    if not path_str:
        return {}
    path = Path(path_str)
    if not path.exists():
        return {}
    payload = path.read_text(encoding="utf-8")
    parsed = json.loads(payload)
    return parsed if isinstance(parsed, dict) else {}
