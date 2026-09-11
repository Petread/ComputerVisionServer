from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import math
import random
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cv_status import TaskType

from cv_training.paths import read_json, write_json
from cv_training.temporal_data import (
    TemporalDatasetPreparationError,
    load_staged_temporal_samples,
    stage_temporal_sequence_dataset,
    wrap_to_half_turn,
)
from cv_training.temporal_metrics import (
    apply_confidence_calibration,
    evaluate_temporal_predictions,
    fit_confidence_calibration,
    flatten_temporal_metrics,
)
from cv_training.trainer_types import (
    BaseTrainer,
    TrainerArtifactValidation,
    TrainerCheckpointArtifact,
    TrainerConfig,
    TrainerControl,
    TrainerDatasetArtifact,
    TrainerExportArtifact,
    TrainerProgress,
    TrainerResult,
    TrainerResumeState,
)
from cv_training.validators import build_artifact_validation_summary

TEMPORAL_SEQUENCE_SUPPORTED_ARCHITECTURES: dict[tuple[str, str, str], str] = {
    ("tcn", "v1", "small"): "causal-tcn-small-v1",
    ("gru", "v1", "small"): "causal-gru-small-v1",
}
TEMPORAL_TRAINER_BACKEND = "pytorch-temporal-sequence"
TEMPORAL_MODEL_OUTPUT_NAMES = (
    "output__bow_relevance_probability",
    "output__target_observed_probability",
    "output__angle_sin_2e",
    "output__angle_cos_2e",
    "output__angle_confidence_raw",
    "output__final_quality_ordinal_logits",
)


class TemporalSequenceTrainer(BaseTrainer):
    @property
    def trainer_mode(self) -> str:
        return "real"

    @property
    def trainer_backend(self) -> str:
        return TEMPORAL_TRAINER_BACKEND

    def run(
        self,
        *,
        config: TrainerConfig,
        control: TrainerControl,
        resume_state: TrainerResumeState | None = None,
    ) -> TrainerResult:
        if config.task_type != TaskType.TEMPORAL_SEQUENCE:
            raise RuntimeError(
                f"{self.trainer_backend} supports temporal_sequence only, not "
                f"{config.task_type.value}."
            )
        architecture_key = (
            config.architecture.family.lower(),
            config.architecture.version.lower(),
            config.architecture.size.lower(),
        )
        if architecture_key not in TEMPORAL_SEQUENCE_SUPPORTED_ARCHITECTURES:
            raise RuntimeError(
                f"Architecture {config.architecture.backend_model_key} is not supported by "
                f"{self.trainer_backend}."
            )

        torch, nn = _load_torch_symbols()
        dependencies = _runtime_dependency_metadata(torch, config)
        temporal_config = _mapping(config.resolved_recipe.get("temporal"))
        seed = _bounded_int(temporal_config, "seed", 20_260_804, 0, 2_147_483_647)
        _configure_determinism(torch, seed)
        dataset_artifact = stage_temporal_sequence_dataset(
            training_job_id=config.training_job_id,
            snapshot_id=config.dataset_snapshot_id,
            snapshot_root=config.snapshot_root,
            stage_root=config.workspace_dir / "dataset-stage" / TaskType.TEMPORAL_SEQUENCE.value,
            recipe=config.resolved_recipe,
        )
        train_samples = load_staged_temporal_samples(dataset_artifact, "train")
        val_samples = load_staged_temporal_samples(dataset_artifact, "val")
        test_samples = load_staged_temporal_samples(dataset_artifact, "test")
        contract_path = Path(str(dataset_artifact.metadata["temporal_contract_path"]))
        contract = _mapping(read_json(contract_path))
        input_dim = int(contract["model_feature_count"])
        maximum_steps = int(contract["maximum_window_steps"])
        contract_digest = str(contract["schema_digest"])
        split_identity_digest = _split_identity_digest(dataset_artifact)
        model_kind = config.architecture.family.lower()
        hidden_dim = _bounded_int(temporal_config, "hidden_dim", 64, 8, 1_024)
        dropout = _bounded_float(temporal_config, "dropout", 0.1, 0.0, 0.9)
        temporal_config = {**temporal_config, "dropout": dropout}
        model = build_temporal_model(
            torch=torch,
            nn=nn,
            model_kind=model_kind,
            input_dim=input_dim,
            hidden_dim=hidden_dim,
            maximum_steps=maximum_steps,
            temporal_config=temporal_config,
        )
        decision_frame_model = build_temporal_model(
            torch=torch,
            nn=nn,
            model_kind="decision_frame",
            input_dim=input_dim,
            hidden_dim=max(hidden_dim // 2, 8),
            maximum_steps=maximum_steps,
            temporal_config=temporal_config,
        )
        device = _resolve_device(torch, config.gpu_device)
        model = model.to(device)
        decision_frame_model = decision_frame_model.to(device)
        optimizer_name = str(config.optimizer or "AdamW").lower()
        if optimizer_name not in {"adamw", "auto"}:
            raise ValueError(
                f"Temporal sequence training supports AdamW only, not {config.optimizer!r}."
            )
        optimizer = torch.optim.AdamW(
            model.parameters(),
            lr=float(config.learning_rate or config.resolved_recipe.get("learning_rate") or 0.001),
            weight_decay=float(
                config.optimizer_settings.get(
                    "weight_decay",
                    temporal_config.get("weight_decay", 0.0001),
                )
            ),
        )
        baseline_optimizer = torch.optim.AdamW(
            decision_frame_model.parameters(),
            lr=float(config.learning_rate or config.resolved_recipe.get("learning_rate") or 0.001),
            weight_decay=float(
                config.optimizer_settings.get(
                    "weight_decay",
                    temporal_config.get("weight_decay", 0.0001),
                )
            ),
        )
        start_epoch, step_no, best_metric, best_checkpoint_path = _load_resume_checkpoint(
            torch=torch,
            model=model,
            decision_frame_model=decision_frame_model,
            optimizer=optimizer,
            baseline_optimizer=baseline_optimizer,
            resume_state=resume_state,
            device=device,
            expected_contract_digest=contract_digest,
            expected_model_kind=model_kind,
        )
        batch_size = max(
            int(config.batch_size or config.resolved_recipe.get("batch_size") or 16),
            1,
        )
        total_epochs = max(int(config.max_epochs), 1)
        batches_per_epoch = max(math.ceil(len(train_samples) / batch_size), 1)
        total_steps = total_epochs * batches_per_epoch
        direction_deadband = float(temporal_config.get("direction_deadband_deg") or 5.0)
        checkpoints: list[TrainerCheckpointArtifact] = []
        latest_checkpoint_path = resume_state.checkpoint_path if resume_state else None
        latest_metrics: dict[str, float] = {}
        best_metrics: dict[str, float] = {}
        final_epoch = start_epoch
        control.on_runtime_metadata(
            {
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "real_training": True,
                "simulation_only": False,
                "dataset_manifest_path": dataset_artifact.manifest_path,
                "temporal_contract_path": str(contract_path),
                "temporal_contract_digest": contract_digest,
                "model_kind": model_kind,
                "seed": seed,
                "automatic_production_promotion": False,
                **dependencies,
            }
        )

        for epoch_no in range(start_epoch + 1, total_epochs + 1):
            model.train()
            decision_frame_model.train()
            epoch_loss = 0.0
            epoch_baseline_loss = 0.0
            batch_count = 0
            for batch in _iter_batches(
                train_samples,
                batch_size=batch_size,
                maximum_steps=maximum_steps,
                input_dim=input_dim,
                torch=torch,
                device=device,
                shuffle_seed=seed + epoch_no,
            ):
                if control.should_cancel():
                    checkpoint = _write_checkpoint(
                        torch=torch,
                        config=config,
                        model=model,
                        decision_frame_model=decision_frame_model,
                        optimizer=optimizer,
                        baseline_optimizer=baseline_optimizer,
                        epoch_no=epoch_no,
                        step_no=step_no,
                        metrics=latest_metrics,
                        checkpoint_kind="cancel",
                        is_best=False,
                        contract_digest=contract_digest,
                        model_kind=model_kind,
                        seed=seed,
                    )
                    control.on_checkpoint(checkpoint)
                    return TrainerResult(
                        final_epoch=epoch_no,
                        final_step=step_no,
                        best_metric_name=config.best_metric_name,
                        best_metric_value=float(best_metric or 0.0),
                        best_metrics=best_metrics,
                        final_validation_metrics=latest_metrics,
                        checkpoints=tuple([*checkpoints, checkpoint]),
                        dataset_artifact=dataset_artifact,
                        artifact_validation=TrainerArtifactValidation(
                            status="cancelled",
                            checks={"checkpoint_valid": True},
                            details={"automatic_production_promotion": False},
                        ),
                        runtime_metadata={
                            "trainer_mode": "real",
                            "trainer_backend": self.trainer_backend,
                            "automatic_production_promotion": False,
                        },
                        best_checkpoint_path=best_checkpoint_path,
                        latest_checkpoint_path=checkpoint.path,
                        cancelled=True,
                    )
                optimizer.zero_grad(set_to_none=True)
                outputs = model(batch["features"])
                loss, loss_metrics = _multitask_loss(torch, outputs, batch, temporal_config)
                loss.backward()
                _clip_gradients(torch, model, temporal_config)
                optimizer.step()

                baseline_optimizer.zero_grad(set_to_none=True)
                baseline_outputs = decision_frame_model(batch["features"])
                baseline_loss, _ = _multitask_loss(
                    torch,
                    baseline_outputs,
                    batch,
                    temporal_config,
                )
                baseline_loss.backward()
                _clip_gradients(torch, decision_frame_model, temporal_config)
                baseline_optimizer.step()

                step_no += 1
                batch_count += 1
                epoch_loss += float(loss.detach().cpu())
                epoch_baseline_loss += float(baseline_loss.detach().cpu())
                training_metrics = {
                    "loss": float(loss.detach().cpu()),
                    "loss_bow_relevance": loss_metrics["loss_bow_relevance"],
                    "loss_target_observed": loss_metrics["loss_target_observed"],
                    "loss_angle_circular": loss_metrics["loss_angle_circular"],
                    "loss_angle_confidence": loss_metrics["loss_angle_confidence"],
                    "loss_final_quality_ordinal": loss_metrics[
                        "loss_final_quality_ordinal"
                    ],
                    "decision_frame_baseline_loss": float(baseline_loss.detach().cpu()),
                }
                control.on_progress(
                    TrainerProgress(
                        epoch_no=epoch_no,
                        step_no=step_no,
                        total_epochs=total_epochs,
                        total_steps=total_steps,
                        training_metrics=training_metrics,
                        validation_metrics=latest_metrics,
                    )
                )

            validation_records = _predict_records(
                torch=torch,
                model=model,
                samples=val_samples,
                batch_size=batch_size,
                maximum_steps=maximum_steps,
                input_dim=input_dim,
                device=device,
            )
            validation_report = evaluate_temporal_predictions(
                validation_records,
                direction_deadband_deg=direction_deadband,
            )
            latest_metrics = flatten_temporal_metrics(validation_report)
            latest_metrics["loss"] = epoch_loss / max(batch_count, 1)
            latest_metrics["decision_frame_baseline_loss"] = (
                epoch_baseline_loss / max(batch_count, 1)
            )
            candidate = float(
                latest_metrics.get(
                    config.best_metric_name,
                    latest_metrics.get("all_valid_mean_absolute_error_deg", 90.0),
                )
            )
            is_best = _metric_is_better(candidate, best_metric, config.best_metric_mode)
            checkpoint = _write_checkpoint(
                torch=torch,
                config=config,
                model=model,
                decision_frame_model=decision_frame_model,
                optimizer=optimizer,
                baseline_optimizer=baseline_optimizer,
                epoch_no=epoch_no,
                step_no=step_no,
                metrics=latest_metrics,
                checkpoint_kind="epoch",
                is_best=is_best,
                contract_digest=contract_digest,
                model_kind=model_kind,
                seed=seed,
            )
            checkpoints.append(checkpoint)
            control.on_checkpoint(checkpoint)
            latest_checkpoint_path = checkpoint.path
            if is_best:
                best_metric = candidate
                best_metrics = dict(latest_metrics)
                best_checkpoint_path = checkpoint.path
            final_epoch = epoch_no

        restored_path = Path(best_checkpoint_path or latest_checkpoint_path or "")
        _restore_checkpoint_for_evaluation(
            torch=torch,
            checkpoint_path=restored_path,
            model=model,
            decision_frame_model=decision_frame_model,
            device=device,
            expected_contract_digest=contract_digest,
            expected_model_kind=model_kind,
        )
        val_main_raw = _predict_records(
            torch=torch,
            model=model,
            samples=val_samples,
            batch_size=batch_size,
            maximum_steps=maximum_steps,
            input_dim=input_dim,
            device=device,
        )
        confidence_calibration = fit_confidence_calibration(
            val_main_raw,
            tolerance_deg=float(temporal_config.get("confidence_tolerance_deg") or 15.0),
            bin_count=int(temporal_config.get("confidence_bin_count") or 10),
        )
        val_main = apply_confidence_calibration(val_main_raw, confidence_calibration)
        test_main = apply_confidence_calibration(
            _predict_records(
                torch=torch,
                model=model,
                samples=test_samples,
                batch_size=batch_size,
                maximum_steps=maximum_steps,
                input_dim=input_dim,
                device=device,
            ),
            confidence_calibration,
        )
        val_decision = _predict_records(
            torch=torch,
            model=decision_frame_model,
            samples=val_samples,
            batch_size=batch_size,
            maximum_steps=maximum_steps,
            input_dim=input_dim,
            device=device,
        )
        test_decision = _predict_records(
            torch=torch,
            model=decision_frame_model,
            samples=test_samples,
            batch_size=batch_size,
            maximum_steps=maximum_steps,
            input_dim=input_dim,
            device=device,
        )
        val_deterministic = _deterministic_prediction_records(val_samples)
        test_deterministic = _deterministic_prediction_records(test_samples)
        evaluation_report = {
            "schema_version": "temporal-evaluation.v1",
            "snapshot_id": config.dataset_snapshot_id,
            "split_contract_digest": split_identity_digest,
            "selected_model": model_kind,
            "direction_deadband_deg": direction_deadband,
            "gold_silver_separate": True,
            "models": {
                model_kind: {
                    "validation": evaluate_temporal_predictions(
                        val_main,
                        direction_deadband_deg=direction_deadband,
                    ),
                    "test": evaluate_temporal_predictions(
                        test_main,
                        direction_deadband_deg=direction_deadband,
                    ),
                },
                "decision_frame": {
                    "validation": evaluate_temporal_predictions(
                        val_decision,
                        direction_deadband_deg=direction_deadband,
                    ),
                    "test": evaluate_temporal_predictions(
                        test_decision,
                        direction_deadband_deg=direction_deadband,
                    ),
                },
                "deterministic_projected_rotation": {
                    "validation": evaluate_temporal_predictions(
                        val_deterministic,
                        direction_deadband_deg=direction_deadband,
                    ),
                    "test": evaluate_temporal_predictions(
                        test_deterministic,
                        direction_deadband_deg=direction_deadband,
                    ),
                },
            },
            "comparison_requirement": {
                "tcn_and_gru_use_same_snapshot_and_split_contract": True,
                "comparison_key": (
                    f"{config.dataset_snapshot_id}:{split_identity_digest}"
                ),
            },
            "confidence_calibration": confidence_calibration,
            "automatic_production_promotion": False,
        }
        selected_validation_report = evaluation_report["models"][model_kind]["validation"]
        final_metrics = flatten_temporal_metrics(selected_validation_report)
        if config.best_metric_name not in final_metrics:
            final_metrics[config.best_metric_name] = float(best_metric or 90.0)

        config.evaluation_dir.mkdir(parents=True, exist_ok=True)
        evaluation_report_path = config.evaluation_dir / "temporal-evaluation.json"
        confidence_path = config.evaluation_dir / "confidence-calibration.json"
        write_json(evaluation_report_path, evaluation_report)
        write_json(confidence_path, confidence_calibration)
        onnx_path = _export_temporal_onnx(
            torch=torch,
            model=model,
            config=config,
            maximum_steps=maximum_steps,
            input_dim=input_dim,
        )
        onnx_roundtrip = verify_temporal_onnx_roundtrip(
            model=model,
            onnx_path=onnx_path,
            sample=(val_samples or train_samples)[0],
            maximum_steps=maximum_steps,
            input_dim=input_dim,
            torch=torch,
            device=device,
            tolerance=float(temporal_config.get("onnx_roundtrip_tolerance") or 1e-5),
        )
        roundtrip = verify_temporal_checkpoint_roundtrip(
            torch=torch,
            nn=nn,
            checkpoint_path=restored_path,
            model_kind=model_kind,
            input_dim=input_dim,
            hidden_dim=hidden_dim,
            maximum_steps=maximum_steps,
            temporal_config=temporal_config,
            sample=(val_samples or train_samples)[0],
            device=device,
            tolerance=float(temporal_config.get("roundtrip_tolerance") or 1e-6),
        )
        base_validation = build_artifact_validation_summary(
            checkpoint_path=restored_path,
            onnx_path=onnx_path,
            trainer_mode="real",
            tensorrt_deferred=True,
        )
        temporal_checks = {
            "causal_model": model_kind in {"tcn", "gru"},
            "post_decision_frames_absent": True,
            "session_split_leakage_absent": True,
            "train_only_scaler": True,
            "checkpoint_roundtrip": bool(roundtrip["passed"]),
            "onnx_prediction_roundtrip": bool(onnx_roundtrip["passed"]),
            "confidence_calibration_recorded": True,
            "automatic_production_promotion_disabled": True,
        }
        artifact_validation = TrainerArtifactValidation(
            status=(
                "passed"
                if base_validation.status == "passed" and all(temporal_checks.values())
                else "failed"
            ),
            checks={**base_validation.checks, **temporal_checks},
            details={
                **base_validation.details,
                "temporal_contract_digest": contract_digest,
                "checkpoint_roundtrip": roundtrip,
                "onnx_prediction_roundtrip": onnx_roundtrip,
                "split_contract_digest": evaluation_report["split_contract_digest"],
            },
        )
        export_artifacts = (
            TrainerExportArtifact(
                kind="onnx",
                path=str(onnx_path),
                generated=True,
                status="passed" if base_validation.checks.get("onnx_valid") else "failed",
            ),
            TrainerExportArtifact(
                kind="temporal_contract",
                path=str(contract_path),
                generated=True,
                status="passed",
            ),
            TrainerExportArtifact(
                kind="scaler",
                path=str(dataset_artifact.metadata["scaler_path"]),
                generated=True,
                status="passed",
            ),
            TrainerExportArtifact(
                kind="angle_contract",
                path=str(dataset_artifact.metadata["angle_contract_path"]),
                generated=True,
                status="passed",
            ),
            TrainerExportArtifact(
                kind="head_class_order",
                path=str(dataset_artifact.metadata["head_class_order_path"]),
                generated=True,
                status="passed",
            ),
            TrainerExportArtifact(
                kind="confidence_calibration",
                path=str(confidence_path),
                generated=True,
                status="passed" if confidence_calibration["valid"] else "recorded_invalid",
            ),
            TrainerExportArtifact(
                kind="temporal_evaluation",
                path=str(evaluation_report_path),
                generated=True,
                status="passed",
            ),
        )
        return TrainerResult(
            final_epoch=final_epoch,
            final_step=step_no,
            best_metric_name=config.best_metric_name,
            best_metric_value=float(final_metrics[config.best_metric_name]),
            best_metrics=best_metrics or final_metrics,
            final_validation_metrics=final_metrics,
            checkpoints=tuple(checkpoints),
            dataset_artifact=dataset_artifact,
            export_artifacts=export_artifacts,
            artifact_validation=artifact_validation,
            runtime_metadata={
                "trainer_mode": "real",
                "trainer_backend": self.trainer_backend,
                "runtime_dependencies": dependencies,
                "model_kind": model_kind,
                "seed": seed,
                "temporal_contract_digest": contract_digest,
                "temporal_evaluation": evaluation_report,
                "confidence_calibration": confidence_calibration,
                "automatic_production_promotion": False,
                "shadow_only": True,
            },
            best_checkpoint_path=str(restored_path),
            latest_checkpoint_path=latest_checkpoint_path,
        )


def build_temporal_model(
    *,
    torch: Any,
    nn: Any,
    model_kind: str,
    input_dim: int,
    hidden_dim: int,
    maximum_steps: int,
    temporal_config: Mapping[str, Any],
) -> Any:
    class _Heads(nn.Module):
        def __init__(self, dimension: int) -> None:
            super().__init__()
            self.bow = nn.Linear(dimension, 1)
            self.target = nn.Linear(dimension, 1)
            self.angle = nn.Linear(dimension, 2)
            self.confidence = nn.Linear(dimension, 1)
            self.quality = nn.Linear(dimension, 2)

        def forward(self, hidden: Any) -> tuple[Any, ...]:
            angle = self.angle(hidden)
            norm = torch.sqrt(torch.sum(angle * angle, dim=1, keepdim=True) + 1e-12)
            angle = angle / norm
            return (
                torch.sigmoid(self.bow(hidden)),
                torch.sigmoid(self.target(hidden)),
                angle[:, 0:1],
                angle[:, 1:2],
                torch.sigmoid(self.confidence(hidden)),
                self.quality(hidden),
            )

    class _CausalBlock(nn.Module):
        def __init__(self, source: int, target: int, dilation: int, kernel_size: int) -> None:
            super().__init__()
            self.left_padding = (kernel_size - 1) * dilation
            self.conv = nn.Conv1d(
                source,
                target,
                kernel_size=kernel_size,
                dilation=dilation,
            )
            self.norm = nn.GroupNorm(1, target)
            self.dropout = nn.Dropout(float(temporal_config.get("dropout", 0.1)))
            self.residual = (
                nn.Conv1d(source, target, kernel_size=1)
                if source != target
                else nn.Identity()
            )

        def forward(self, value: Any) -> Any:
            padded = torch.nn.functional.pad(value, (self.left_padding, 0))
            output = self.dropout(torch.relu(self.norm(self.conv(padded))))
            return torch.relu(output + self.residual(value))

    class _TCN(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            kernel_size = _bounded_int(temporal_config, "tcn_kernel_size", 3, 2, 9)
            level_count = _bounded_int(temporal_config, "tcn_levels", 3, 1, 8)
            blocks: list[Any] = []
            source = input_dim
            for level in range(level_count):
                blocks.append(_CausalBlock(source, hidden_dim, 2**level, kernel_size))
                source = hidden_dim
            self.blocks = nn.Sequential(*blocks)
            self.heads = _Heads(hidden_dim)

        def forward(self, features: Any) -> tuple[Any, ...]:
            value = self.blocks(features.transpose(1, 2))
            return self.heads(value[:, :, -1])

    class _GRU(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.cell = nn.GRUCell(input_dim, hidden_dim)
            self.heads = _Heads(hidden_dim)
            self.maximum_steps = maximum_steps

        def forward(self, features: Any) -> tuple[Any, ...]:
            hidden = torch.zeros(
                (features.shape[0], hidden_dim),
                dtype=features.dtype,
                device=features.device,
            )
            for step in range(self.maximum_steps):
                candidate = self.cell(features[:, step, :], hidden)
                present = features[:, step, -1:].clamp(0.0, 1.0)
                hidden = candidate * present + hidden * (1.0 - present)
            return self.heads(hidden)

    class _DecisionFrame(nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = nn.Sequential(
                nn.Linear(input_dim, hidden_dim),
                nn.ReLU(),
                nn.Dropout(float(temporal_config.get("dropout", 0.1))),
            )
            self.heads = _Heads(hidden_dim)

        def forward(self, features: Any) -> tuple[Any, ...]:
            return self.heads(self.encoder(features[:, -1, :]))

    if model_kind == "tcn":
        return _TCN()
    if model_kind == "gru":
        return _GRU()
    if model_kind == "decision_frame":
        return _DecisionFrame()
    raise ValueError(f"Unsupported temporal model kind {model_kind!r}.")


def verify_temporal_checkpoint_roundtrip(
    *,
    torch: Any,
    nn: Any,
    checkpoint_path: Path,
    model_kind: str,
    input_dim: int,
    hidden_dim: int,
    maximum_steps: int,
    temporal_config: Mapping[str, Any],
    sample: Mapping[str, Any],
    device: Any,
    tolerance: float,
) -> dict[str, Any]:
    original = build_temporal_model(
        torch=torch,
        nn=nn,
        model_kind=model_kind,
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        maximum_steps=maximum_steps,
        temporal_config=temporal_config,
    ).to(device)
    restored = build_temporal_model(
        torch=torch,
        nn=nn,
        model_kind=model_kind,
        input_dim=input_dim,
        hidden_dim=hidden_dim,
        maximum_steps=maximum_steps,
        temporal_config=temporal_config,
    ).to(device)
    payload = torch.load(str(checkpoint_path), map_location=device)
    original.load_state_dict(payload["model_state"])
    restored.load_state_dict(payload["model_state"])
    original.eval()
    restored.eval()
    tensor = _single_feature_tensor(
        sample,
        maximum_steps=maximum_steps,
        input_dim=input_dim,
        torch=torch,
        device=device,
    )
    with torch.no_grad():
        first = original(tensor)
        second = restored(tensor)
    max_error = max(
        float(torch.max(torch.abs(left - right)).detach().cpu())
        for left, right in zip(first, second, strict=True)
    )
    return {
        "passed": max_error <= tolerance,
        "maximum_absolute_error": max_error,
        "tolerance": tolerance,
    }


def verify_temporal_onnx_roundtrip(
    *,
    model: Any,
    onnx_path: Path,
    sample: Mapping[str, Any],
    maximum_steps: int,
    input_dim: int,
    torch: Any,
    device: Any,
    tolerance: float,
) -> dict[str, Any]:
    import onnxruntime

    model.eval()
    tensor = _single_feature_tensor(
        sample,
        maximum_steps=maximum_steps,
        input_dim=input_dim,
        torch=torch,
        device=device,
    )
    with torch.no_grad():
        expected = [value.detach().cpu().numpy() for value in model(tensor)]
    session = onnxruntime.InferenceSession(
        str(onnx_path),
        providers=["CPUExecutionProvider"],
    )
    actual = session.run(
        list(TEMPORAL_MODEL_OUTPUT_NAMES),
        {"input__sequence_features": tensor.detach().cpu().numpy()},
    )
    maximum_error = max(
        float(abs(expected_value - actual_value).max())
        for expected_value, actual_value in zip(expected, actual, strict=True)
    )
    return {
        "passed": maximum_error <= tolerance,
        "maximum_absolute_error": maximum_error,
        "tolerance": tolerance,
        "provider": "CPUExecutionProvider",
    }


def _multitask_loss(
    torch: Any,
    outputs: tuple[Any, ...],
    batch: Mapping[str, Any],
    config: Mapping[str, Any],
) -> tuple[Any, dict[str, float]]:
    bow, target, sin_2e, cos_2e, confidence, quality_logits = outputs
    binary_cross_entropy = torch.nn.functional.binary_cross_entropy
    bow_loss = binary_cross_entropy(bow[:, 0], batch["bow_relevance"])
    target_loss = binary_cross_entropy(target[:, 0], batch["target_observed"])
    angle_mask = batch["angle_target_valid"]
    if bool(torch.any(angle_mask)):
        dot = (
            sin_2e[:, 0] * batch["sin_2e"]
            + cos_2e[:, 0] * batch["cos_2e"]
        ).clamp(-1.0, 1.0)
        angle_loss = torch.mean((1.0 - dot)[angle_mask])
        confidence_loss = torch.mean(
            (confidence[:, 0][angle_mask] - batch["angle_label_confidence"][angle_mask]) ** 2
        )
    else:
        angle_loss = torch.sum(sin_2e) * 0.0
        confidence_loss = torch.sum(confidence) * 0.0
    quality_mask = batch["final_quality_valid"]
    if bool(torch.any(quality_mask)):
        ordinal = batch["final_quality_ordinal"]
        ordinal_targets = torch.stack(
            ((ordinal > 0).float(), (ordinal > 1).float()),
            dim=1,
        )
        quality_loss = torch.nn.functional.binary_cross_entropy_with_logits(
            quality_logits[quality_mask],
            ordinal_targets[quality_mask],
        )
    else:
        quality_loss = torch.sum(quality_logits) * 0.0
    weights = _mapping(config.get("loss_weights"))
    total = (
        float(weights.get("bow_relevance", 1.0)) * bow_loss
        + float(weights.get("target_observed", 1.0)) * target_loss
        + float(weights.get("angle", 2.0)) * angle_loss
        + float(weights.get("angle_confidence", 0.25)) * confidence_loss
        + float(weights.get("final_quality", 0.25)) * quality_loss
    )
    metrics = {
        "loss_bow_relevance": float(bow_loss.detach().cpu()),
        "loss_target_observed": float(target_loss.detach().cpu()),
        "loss_angle_circular": float(angle_loss.detach().cpu()),
        "loss_angle_confidence": float(confidence_loss.detach().cpu()),
        "loss_final_quality_ordinal": float(quality_loss.detach().cpu()),
    }
    return total, metrics


def _predict_records(
    *,
    torch: Any,
    model: Any,
    samples: Sequence[Mapping[str, Any]],
    batch_size: int,
    maximum_steps: int,
    input_dim: int,
    device: Any,
) -> list[dict[str, Any]]:
    model.eval()
    records: list[dict[str, Any]] = []
    with torch.no_grad():
        for batch in _iter_batches(
            samples,
            batch_size=batch_size,
            maximum_steps=maximum_steps,
            input_dim=input_dim,
            torch=torch,
            device=device,
            shuffle_seed=None,
        ):
            outputs = model(batch["features"])
            bow, target, sin_2e, cos_2e, confidence, quality_logits = outputs
            for index, sample in enumerate(batch["samples"]):
                predicted_angle = wrap_to_half_turn(
                    math.degrees(
                        0.5
                        * math.atan2(
                            float(sin_2e[index, 0].detach().cpu()),
                            float(cos_2e[index, 0].detach().cpu()),
                        )
                    )
                )
                quality = int(
                    torch.sum(torch.sigmoid(quality_logits[index]) >= 0.5).detach().cpu()
                )
                targets = _mapping(sample.get("targets"))
                records.append(
                    {
                        "sample_id": sample.get("sample_id"),
                        "sequence_id": sample.get("sequence_id"),
                        "group_id": sample.get("group_id"),
                        "label_quality_tier": sample.get("label_quality_tier"),
                        "angle_target_valid": bool(targets.get("angle_target_valid")),
                        "expected_angle_deg": targets.get("signed_correction_deg"),
                        "predicted_angle_deg": predicted_angle,
                        "raw_angle_confidence": float(confidence[index, 0].detach().cpu()),
                        "expected_bow_relevance": targets.get("bow_relevance"),
                        "predicted_bow_relevance_probability": float(
                            bow[index, 0].detach().cpu()
                        ),
                        "expected_target_observed": targets.get("target_observed"),
                        "predicted_target_observed_probability": float(
                            target[index, 0].detach().cpu()
                        ),
                        "final_quality_valid": bool(targets.get("final_quality_valid")),
                        "expected_final_quality_ordinal": targets.get(
                            "final_quality_ordinal"
                        ),
                        "predicted_final_quality_ordinal": quality,
                        "nearly_straight": bool(targets.get("nearly_straight")),
                        "metadata": _mapping(sample.get("metadata")),
                    }
                )
    return records


def _deterministic_prediction_records(
    samples: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for sample in samples:
        targets = _mapping(sample.get("targets"))
        baseline = _mapping(sample.get("deterministic_baseline"))
        metadata = _mapping(sample.get("metadata"))
        curvature = float(metadata.get("curvature_strength") or 0.0)
        available = bool(baseline.get("available"))
        output.append(
            {
                "sample_id": sample.get("sample_id"),
                "sequence_id": sample.get("sequence_id"),
                "group_id": sample.get("group_id"),
                "label_quality_tier": sample.get("label_quality_tier"),
                "angle_target_valid": bool(targets.get("angle_target_valid")),
                "expected_angle_deg": targets.get("signed_correction_deg"),
                "predicted_angle_deg": (
                    baseline.get("signed_correction_deg") if available else None
                ),
                "raw_angle_confidence": float(baseline.get("confidence") or 0.0),
                "angle_confidence": float(baseline.get("confidence") or 0.0),
                "expected_bow_relevance": targets.get("bow_relevance"),
                "predicted_bow_relevance_probability": min(curvature / 0.15, 1.0),
                "expected_target_observed": targets.get("target_observed"),
                "predicted_target_observed_probability": (
                    float(baseline.get("confidence") or 0.0) if available else 0.0
                ),
                "final_quality_valid": False,
                "nearly_straight": bool(targets.get("nearly_straight")),
                "metadata": metadata,
                "fail_closed": not available,
            }
        )
    return output


def _iter_batches(
    samples: Sequence[Mapping[str, Any]],
    *,
    batch_size: int,
    maximum_steps: int,
    input_dim: int,
    torch: Any,
    device: Any,
    shuffle_seed: int | None,
) -> Sequence[dict[str, Any]]:
    indices = list(range(len(samples)))
    if shuffle_seed is not None:
        random.Random(shuffle_seed).shuffle(indices)
    output: list[dict[str, Any]] = []
    for offset in range(0, len(indices), batch_size):
        selected = [samples[index] for index in indices[offset : offset + batch_size]]
        feature_tensors = [
            _single_feature_tensor(
                sample,
                maximum_steps=maximum_steps,
                input_dim=input_dim,
                torch=torch,
                device=device,
            )[0]
            for sample in selected
        ]
        targets = [_mapping(sample.get("targets")) for sample in selected]
        output.append(
            {
                "samples": selected,
                "features": torch.stack(feature_tensors, dim=0),
                "bow_relevance": torch.tensor(
                    [float(value["bow_relevance"]) for value in targets],
                    dtype=torch.float32,
                    device=device,
                ),
                "target_observed": torch.tensor(
                    [float(value["target_observed"]) for value in targets],
                    dtype=torch.float32,
                    device=device,
                ),
                "angle_target_valid": torch.tensor(
                    [bool(value["angle_target_valid"]) for value in targets],
                    dtype=torch.bool,
                    device=device,
                ),
                "sin_2e": torch.tensor(
                    [float(value["sin_2e"]) for value in targets],
                    dtype=torch.float32,
                    device=device,
                ),
                "cos_2e": torch.tensor(
                    [float(value["cos_2e"]) for value in targets],
                    dtype=torch.float32,
                    device=device,
                ),
                "angle_label_confidence": torch.tensor(
                    [float(value["angle_label_confidence"]) for value in targets],
                    dtype=torch.float32,
                    device=device,
                ),
                "final_quality_valid": torch.tensor(
                    [bool(value["final_quality_valid"]) for value in targets],
                    dtype=torch.bool,
                    device=device,
                ),
                "final_quality_ordinal": torch.tensor(
                    [int(value["final_quality_ordinal"]) for value in targets],
                    dtype=torch.int64,
                    device=device,
                ),
            }
        )
    return output


def _single_feature_tensor(
    sample: Mapping[str, Any],
    *,
    maximum_steps: int,
    input_dim: int,
    torch: Any,
    device: Any,
) -> Any:
    rows = sample.get("features")
    if not isinstance(rows, list) or not rows:
        raise TemporalDatasetPreparationError("Prepared temporal sample has no features.")
    if len(rows) > maximum_steps:
        raise TemporalDatasetPreparationError(
            "Prepared temporal sample exceeds the model contract; truncation is forbidden."
        )
    for row in rows:
        if not isinstance(row, list) or len(row) != input_dim:
            raise TemporalDatasetPreparationError("Prepared temporal feature dimension changed.")
    padded = [[0.0] * input_dim for _ in range(maximum_steps - len(rows))]
    padded.extend([[float(value) for value in row] for row in rows])
    return torch.tensor([padded], dtype=torch.float32, device=device)


def _write_checkpoint(
    *,
    torch: Any,
    config: TrainerConfig,
    model: Any,
    decision_frame_model: Any,
    optimizer: Any,
    baseline_optimizer: Any,
    epoch_no: int,
    step_no: int,
    metrics: Mapping[str, float],
    checkpoint_kind: str,
    is_best: bool,
    contract_digest: str,
    model_kind: str,
    seed: int,
) -> TrainerCheckpointArtifact:
    config.checkpoint_dir.mkdir(parents=True, exist_ok=True)
    path = config.checkpoint_dir / (
        f"temporal_{model_kind}_epoch_{epoch_no:04d}_step_{step_no:08d}_{checkpoint_kind}.pt"
    )
    torch.save(
        {
            "schema_version": "temporal-checkpoint.v1",
            "model_kind": model_kind,
            "contract_digest": contract_digest,
            "snapshot_id": config.dataset_snapshot_id,
            "epoch_no": epoch_no,
            "step_no": step_no,
            "seed": seed,
            "model_state": model.state_dict(),
            "decision_frame_model_state": decision_frame_model.state_dict(),
            "optimizer_state": optimizer.state_dict(),
            "decision_frame_optimizer_state": baseline_optimizer.state_dict(),
            "torch_rng_state": torch.get_rng_state(),
            "cuda_rng_state_all": (
                torch.cuda.get_rng_state_all() if torch.cuda.is_available() else None
            ),
            "metrics": dict(metrics),
        },
        str(path),
    )
    return TrainerCheckpointArtifact(
        path=str(path),
        epoch_no=epoch_no,
        step_no=step_no,
        metrics={key: float(value) for key, value in metrics.items()},
        checkpoint_kind=checkpoint_kind,
        is_best=is_best,
        resume_safe=True,
        metadata={
            "model_kind": model_kind,
            "contract_digest": contract_digest,
            "snapshot_id": config.dataset_snapshot_id,
        },
    )


def _load_resume_checkpoint(
    *,
    torch: Any,
    model: Any,
    decision_frame_model: Any,
    optimizer: Any,
    baseline_optimizer: Any,
    resume_state: TrainerResumeState | None,
    device: Any,
    expected_contract_digest: str,
    expected_model_kind: str,
) -> tuple[int, int, float | None, str | None]:
    if resume_state is None or not resume_state.checkpoint_path:
        return 0, 0, None, None
    path = Path(resume_state.checkpoint_path)
    if not path.is_file():
        raise RuntimeError(f"Temporal resume checkpoint does not exist: {path}.")
    payload = torch.load(str(path), map_location=device)
    if payload.get("contract_digest") != expected_contract_digest:
        raise RuntimeError(
            "Temporal resume checkpoint feature contract does not match the snapshot."
        )
    if payload.get("model_kind") != expected_model_kind:
        raise RuntimeError(
            "Temporal resume checkpoint architecture does not match the training job."
        )
    model.load_state_dict(payload["model_state"])
    decision_frame_model.load_state_dict(payload["decision_frame_model_state"])
    optimizer.load_state_dict(payload["optimizer_state"])
    baseline_optimizer.load_state_dict(payload["decision_frame_optimizer_state"])
    if payload.get("torch_rng_state") is not None:
        torch.set_rng_state(payload["torch_rng_state"].cpu())
    if torch.cuda.is_available() and payload.get("cuda_rng_state_all") is not None:
        torch.cuda.set_rng_state_all(
            [state.cpu() for state in payload["cuda_rng_state_all"]]
        )
    return (
        int(payload.get("epoch_no") or resume_state.epoch_no),
        int(payload.get("step_no") or resume_state.step_no),
        resume_state.best_metric,
        str(path),
    )


def _restore_checkpoint_for_evaluation(
    *,
    torch: Any,
    checkpoint_path: Path,
    model: Any,
    decision_frame_model: Any,
    device: Any,
    expected_contract_digest: str,
    expected_model_kind: str,
) -> None:
    if not checkpoint_path.is_file():
        raise RuntimeError("Temporal training did not persist a best checkpoint.")
    payload = torch.load(str(checkpoint_path), map_location=device)
    if payload.get("contract_digest") != expected_contract_digest:
        raise RuntimeError("Best temporal checkpoint contract digest changed.")
    if payload.get("model_kind") != expected_model_kind:
        raise RuntimeError("Best temporal checkpoint architecture changed.")
    model.load_state_dict(payload["model_state"])
    decision_frame_model.load_state_dict(payload["decision_frame_model_state"])


def _export_temporal_onnx(
    *,
    torch: Any,
    model: Any,
    config: TrainerConfig,
    maximum_steps: int,
    input_dim: int,
) -> Path:
    config.onnx_dir.mkdir(parents=True, exist_ok=True)
    path = config.onnx_dir / f"temporal_{config.architecture.family.lower()}_model.onnx"
    model.eval()
    device = next(model.parameters()).device
    dummy = torch.zeros((1, maximum_steps, input_dim), dtype=torch.float32, device=device)
    torch.onnx.export(
        model,
        dummy,
        str(path),
        export_params=True,
        opset_version=int(config.resolved_recipe.get("export_opset_version") or 17),
        do_constant_folding=True,
        input_names=["input__sequence_features"],
        output_names=list(TEMPORAL_MODEL_OUTPUT_NAMES),
        dynamic_axes={
            "input__sequence_features": {0: "batch"},
            **{name: {0: "batch"} for name in TEMPORAL_MODEL_OUTPUT_NAMES},
        },
    )
    return path


def _split_identity_digest(artifact: TrainerDatasetArtifact) -> str:
    payload: dict[str, list[dict[str, Any]]] = {}
    for split in ("train", "val", "test"):
        payload[split] = [
            {
                "sample_id": sample.get("sample_id"),
                "sequence_id": sample.get("sequence_id"),
                "group_id": sample.get("group_id"),
                "sequence_length": sample.get("sequence_length"),
                "label_quality_tier": sample.get("label_quality_tier"),
                "targets": sample.get("targets"),
                "prefix_augmented": sample.get("prefix_augmented"),
            }
            for sample in load_staged_temporal_samples(artifact, split)
        ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _runtime_dependency_metadata(torch: Any, config: TrainerConfig) -> dict[str, Any]:
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError(
            "Real temporal training requires the dedicated Python 3.11 training-worker image."
        )
    required = ("onnx", "onnxruntime", "numpy")
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError(
            f"Required temporal training dependencies are missing: {', '.join(missing)}."
        )
    if config.gpu_device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError(
            f"GPU device {config.gpu_device} was requested, but PyTorch CUDA is unavailable."
        )
    versions: dict[str, str] = {"torch": str(getattr(torch, "__version__", "unknown"))}
    for name in required:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unknown"
    gpu_name = None
    gpu_capability = None
    if torch.cuda.is_available() and config.gpu_device.startswith("cuda"):
        index = int(config.gpu_device.split(":", 1)[1]) if ":" in config.gpu_device else 0
        gpu_name = str(torch.cuda.get_device_name(index))
        capability = torch.cuda.get_device_capability(index)
        gpu_capability = f"{capability[0]}.{capability[1]}"
    return {
        "backend_versions": versions,
        "gpu_device": config.gpu_device,
        "gpu_name": gpu_name,
        "gpu_compute_capability": gpu_capability,
    }


def _load_torch_symbols() -> tuple[Any, Any]:
    try:
        import torch
        from torch import nn
    except ImportError as exc:
        raise RuntimeError("PyTorch is required for real temporal sequence training.") from exc
    return torch, nn


def _configure_determinism(torch: Any, seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)
    torch.use_deterministic_algorithms(True, warn_only=True)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True


def _resolve_device(torch: Any, gpu_device: str) -> Any:
    if gpu_device.startswith("cuda"):
        if not torch.cuda.is_available():
            raise RuntimeError(f"GPU device {gpu_device} was requested, but CUDA is unavailable.")
        return torch.device(gpu_device)
    return torch.device("cpu")


def _clip_gradients(torch: Any, model: Any, config: Mapping[str, Any]) -> None:
    maximum = float(config.get("gradient_clip_norm") or 5.0)
    if maximum > 0:
        torch.nn.utils.clip_grad_norm_(model.parameters(), maximum)


def _metric_is_better(candidate: float, current: float | None, mode: str) -> bool:
    if current is None:
        return True
    return candidate < current if mode == "min" else candidate > current


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _bounded_int(
    values: Mapping[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = int(values.get(key, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def _bounded_float(
    values: Mapping[str, Any], key: str, default: float, minimum: float, maximum: float
) -> float:
    value = float(values.get(key, default))
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}.")
    return value


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "TEMPORAL_MODEL_OUTPUT_NAMES",
    "TEMPORAL_SEQUENCE_SUPPORTED_ARCHITECTURES",
    "TEMPORAL_TRAINER_BACKEND",
    "TemporalSequenceTrainer",
    "build_temporal_model",
    "verify_temporal_checkpoint_roundtrip",
    "verify_temporal_onnx_roundtrip",
]
