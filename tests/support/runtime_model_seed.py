from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from cv_db import DeploymentRelease, DeploymentTarget, ModelFamily, ModelPromotion, ModelVersion
from cv_status import DeploymentReleaseStatus, PromotionStatus, TargetPath, TaskType
from sqlalchemy.orm import Session


def seed_deployed_runtime_model(
    session: Session,
    *,
    release_id: str = "rel_detection",
    model_family_id: str = "mf_defect_detector",
    model_family_slug: str = "defect-detector",
    model_family_display_name: str = "Defect Detector",
    model_version_id: str = "mvr_detection",
    version_no: int = 1,
    task_type: TaskType = TaskType.DETECTION,
    target_path: TargetPath = TargetPath.REALTIME,
    active_release: bool = True,
    staged: bool = False,
    serving_manifest: dict[str, Any] | None = None,
) -> None:
    """Seed deployment-table truth for runtime model capability tests."""
    if session.get(ModelFamily, model_family_id) is None:
        session.add(
            ModelFamily(
                id=model_family_id,
                slug=model_family_slug,
                display_name=model_family_display_name,
                task_type=task_type.value,
                architecture_family="yolo",
                architecture_version="v11",
                architecture_size="m",
                architecture_display_name="YOLOv11 Medium",
                contract_major=1,
                input_schema_ref="schema://runtime/frame.v1",
                output_schema_ref="schema://runtime/detections.v1",
                active=True,
            )
        )
    if session.get(ModelVersion, model_version_id) is None:
        session.add(
            ModelVersion(
                id=model_version_id,
                model_family_id=model_family_id,
                version_no=version_no,
                status="production" if active_release else "staged",
                architecture_family="yolo",
                architecture_version="v11",
                architecture_size="m",
                architecture_display_name="YOLOv11 Medium",
                serving_manifest=serving_manifest
                or {
                    "class_labels": ["ok", "defect"],
                    "confidence_threshold_default": 0.35,
                    "compatible_targets": [target_path.value],
                },
            )
        )
    if session.get(DeploymentRelease, release_id) is None:
        target_prefix = "rt" if target_path == TargetPath.REALTIME else "bt"
        triton_model_name = f"{target_prefix}__{model_family_slug.replace('-', '_')}_v1"
        session.add(
            DeploymentRelease(
                id=release_id,
                target_path=target_path.value,
                model_family_id=model_family_id,
                model_version_id=model_version_id,
                triton_model_name=triton_model_name,
                triton_model_version=version_no,
                status=DeploymentReleaseStatus.ACTIVATED.value,
                activated_at=datetime.now(tz=UTC),
                smoke_report={"passed": True},
                compatibility_report={"compatible": True},
            )
        )
    if active_release:
        target = session.get(DeploymentTarget, target_path.value)
        if target is None:
            session.add(
                DeploymentTarget(
                    target_path=target_path.value,
                    active_release_id=release_id,
                    updated_at=datetime.now(tz=UTC),
                )
            )
        else:
            target.active_release_id = release_id
            target.updated_at = datetime.now(tz=UTC)
    if staged:
        promotion_id = f"mpr_{release_id}"
        if session.get(ModelPromotion, promotion_id) is None:
            session.add(
                ModelPromotion(
                    id=promotion_id,
                    model_version_id=model_version_id,
                    target_path=target_path.value,
                    status=PromotionStatus.STAGED.value,
                    deployment_release_id=release_id,
                )
            )
    session.flush()
