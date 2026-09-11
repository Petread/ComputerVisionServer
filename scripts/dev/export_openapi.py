from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

for path in [
    ROOT / "backend" / "packages" / "cv-core" / "src",
    ROOT / "backend" / "packages" / "cv-contracts" / "src",
    ROOT / "backend" / "packages" / "cv-deploy" / "src",
    ROOT / "backend" / "packages" / "cv-edge" / "src",
    ROOT / "backend" / "packages" / "cv-runtime" / "src",
    ROOT / "backend" / "packages" / "cv-training" / "src",
    ROOT / "backend" / "packages" / "cv-datasets" / "src",
    ROOT / "backend" / "packages" / "cv-db" / "src",
    ROOT / "backend" / "packages" / "cv-events" / "src",
    ROOT / "backend" / "packages" / "cv-observability" / "src",
    ROOT / "backend" / "packages" / "cv-status" / "src",
    ROOT / "backend" / "packages" / "cv-utils" / "src",
    ROOT / "backend" / "services" / "admin-api" / "src",
    ROOT / "backend" / "services" / "dataset-service" / "src",
    ROOT / "backend" / "services" / "training-orchestrator" / "src",
    ROOT / "backend" / "services" / "model-registry-service" / "src",
    ROOT / "backend" / "services" / "deployment-controller" / "src",
    ROOT / "backend" / "services" / "webhook-receiver" / "src",
    ROOT / "backend" / "services" / "inference-gateway" / "src",
    ROOT / "backend" / "services" / "runtime-admin-api" / "src",
    ROOT / "backend" / "services" / "channel-service" / "src",
    ROOT / "backend" / "services" / "pipeline-definition-service" / "src",
    ROOT / "backend" / "services" / "runtime-orchestrator" / "src",
    ROOT / "backend" / "services" / "realtime-executor" / "src",
    ROOT / "backend" / "services" / "trigger-executor" / "src",
    ROOT / "backend" / "services" / "overlay-service" / "src",
    ROOT / "backend" / "services" / "capture-service" / "src",
    ROOT / "backend" / "services" / "result-service" / "src",
    ROOT / "backend" / "services" / "connector-service" / "src",
    ROOT / "backend" / "services" / "edge-agent" / "src",
]:
    sys.path.insert(0, str(path))

APPS = {
    "admin-api.v1.yaml": ("admin_api.main", "app"),
    "dataset-service.v1.yaml": ("dataset_service.main", "app"),
    "training-orchestrator.v1.yaml": ("training_orchestrator.main", "app"),
    "model-registry-service.v1.yaml": ("model_registry_service.main", "app"),
    "deployment-controller.v1.yaml": ("deployment_controller.main", "app"),
    "webhook-receiver.v1.yaml": ("webhook_receiver.main", "app"),
    "inference-realtime.v1.yaml": ("inference_gateway.main_realtime", "app"),
    "inference-batch.v1.yaml": ("inference_gateway.main_batch", "app"),
    "runtime-admin-api.v1.yaml": ("runtime_admin_api.main", "app"),
    "channel-service.v1.yaml": ("channel_service.main", "app"),
    "pipeline-definition-service.v1.yaml": ("pipeline_definition_service.main", "app"),
    "runtime-orchestrator.v1.yaml": ("runtime_orchestrator.main", "app"),
    "realtime-executor.v1.yaml": ("realtime_executor.main", "app"),
    "trigger-executor.v1.yaml": ("trigger_executor.main", "app"),
    "overlay-service.v1.yaml": ("overlay_service.main", "app"),
    "capture-service.v1.yaml": ("capture_service.main", "app"),
    "result-service.v1.yaml": ("result_service.main", "app"),
    "connector-service.v1.yaml": ("connector_service.main", "app"),
    "edge-agent.v1.yaml": ("edge_agent.app", "app"),
    "edge-plant.v1.yaml": ("edge_agent.plant_openapi", "app"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Export FastAPI OpenAPI contracts.")
    parser.add_argument(
        "filenames",
        nargs="*",
        choices=sorted(APPS),
        help="Optional contract filenames to export; defaults to every service.",
    )
    args = parser.parse_args()
    output_dir = ROOT / "contracts" / "openapi"
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = args.filenames or list(APPS)
    for filename in selected:
        module_name, attribute_name = APPS[filename]
        module = importlib.import_module(module_name)
        app = getattr(module, attribute_name)
        path = output_dir / filename
        path.write_text(yaml.safe_dump(app.openapi(), sort_keys=False), encoding="utf-8")
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
