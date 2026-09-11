from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from cv_status import TaskType

from cv_training.catalog import trainer_framework_for_task


def load_training_recipe(
    task_type: TaskType | str | None = None,
    recipe_name: str | None = None,
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    if task_type is None:
        raise ValueError("A task type is required to load a training recipe.")
    if recipe_name is None:
        raise ValueError("A recipe name is required to load a training recipe.")
    resolved_task_type = task_type if isinstance(task_type, TaskType) else TaskType(str(task_type))
    resolved_workspace_root = workspace_root or Path.cwd()
    task_dir = _recipe_task_dir(resolved_task_type)
    recipe_path = (
        resolved_workspace_root
        / "ml"
        / "configs"
        / "task-types"
        / task_dir
        / f"{recipe_name}.yaml"
    )
    searched = [str(recipe_path)]
    if recipe_path.exists():
        raw_recipe = recipe_path.read_text(encoding="utf-8")
        recipe_source = str(recipe_path)
    else:
        resource_path = (
            resources.files("cv_training")
            / "resources"
            / "task-types"
            / task_dir
            / f"{recipe_name}.yaml"
        )
        searched.append(f"package://cv_training/resources/task-types/{task_dir}/{recipe_name}.yaml")
        if not resource_path.is_file():
            raise ValueError(
                f"Training recipe {recipe_name!r} was not found for task type {resolved_task_type.value}. "
                f"Searched: {', '.join(searched)}."
            )
        raw_recipe = resource_path.read_text(encoding="utf-8")
        recipe_source = searched[-1]
    payload = yaml.safe_load(raw_recipe)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Training recipe {recipe_name!r} for task type {resolved_task_type.value} did not contain a valid mapping."
        )
    payload.setdefault("recipe_name", recipe_name)
    payload.setdefault("task_type", resolved_task_type.value)
    payload["_recipe_source"] = recipe_source
    payload.setdefault("framework", trainer_framework_for_task(resolved_task_type))
    return payload


def merge_recipe_overrides(recipe: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(recipe)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_recipe_overrides(merged[key], value)
        else:
            merged[key] = value
    return merged


def _recipe_task_dir(task_type: TaskType) -> str:
    if task_type == TaskType.CLASSIFICATION:
        return "classification"
    if task_type == TaskType.DETECTION:
        return "detection"
    if task_type == TaskType.ORIENTED_DETECTION:
        return "oriented_detection"
    if task_type == TaskType.SEGMENTATION:
        return "segmentation"
    if task_type == TaskType.ANOMALY:
        return "anomaly"
    if task_type == TaskType.TEMPORAL_SEQUENCE:
        return "temporal_sequence"
    raise ValueError(f"Unsupported task type {task_type.value}.")
