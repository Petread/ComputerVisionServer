from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeServiceBoundary:
    service_name: str
    plane: str
    default_port: int
    responsibilities: tuple[str, ...]
    non_responsibilities: tuple[str, ...]
    public_routes: tuple[str, ...] = ()


RUNTIME_SERVICE_BOUNDARIES: tuple[RuntimeServiceBoundary, ...] = (
    RuntimeServiceBoundary(
        service_name="runtime-admin-api",
        plane="runtime",
        default_port=8090,
        responsibilities=(
            "operator runtime API",
            "channel and pipeline configuration aggregation",
            "runtime status and recent result reads",
        ),
        non_responsibilities=("frame processing", "model deployment mutation", "training"),
        public_routes=("/runtime-api",),
    ),
    RuntimeServiceBoundary(
        service_name="channel-service",
        plane="runtime",
        default_port=8091,
        responsibilities=("channel state", "source metadata", "channel lifecycle"),
        non_responsibilities=("pipeline graph validation", "inference execution"),
    ),
    RuntimeServiceBoundary(
        service_name="pipeline-definition-service",
        plane="runtime",
        default_port=8092,
        responsibilities=("pipeline DSL", "node registry", "pipeline version validation"),
        non_responsibilities=("channel process management", "model training"),
    ),
    RuntimeServiceBoundary(
        service_name="runtime-orchestrator",
        plane="runtime",
        default_port=8093,
        responsibilities=("execution leases", "admission control", "recovery reconciliation"),
        non_responsibilities=("frame acquisition", "trigger HTTP responses"),
    ),
    RuntimeServiceBoundary(
        service_name="realtime-executor",
        plane="runtime",
        default_port=8094,
        responsibilities=("shared frame consumption", "realtime pipeline execution", "overlay emission"),
        non_responsibilities=(
            "camera ownership",
            "video encoding",
            "trigger HTTP endpoint ownership",
            "batch inference",
            "training",
        ),
    ),
    RuntimeServiceBoundary(
        service_name="camera-media",
        plane="runtime",
        default_port=8102,
        responsibilities=(
            "single-owner camera acquisition",
            "shared latest-frame transport",
            "NVENC-only WebRTC source publication",
        ),
        non_responsibilities=("model inference", "pipeline execution", "browser fallback media"),
    ),
    RuntimeServiceBoundary(
        service_name="trigger-executor",
        plane="runtime",
        default_port=8095,
        responsibilities=("HTTP PUT triggers", "snapshot execution", "deterministic responses"),
        non_responsibilities=("continuous RTSP loops", "batch inference", "training"),
        public_routes=("/runtime-trigger",),
    ),
    RuntimeServiceBoundary(
        service_name="overlay-service",
        plane="runtime",
        default_port=8096,
        responsibilities=("overlay payloads", "latest overlay state", "browser event stream"),
        non_responsibilities=("model inference", "source credential management"),
        public_routes=("/runtime-overlay",),
    ),
    RuntimeServiceBoundary(
        service_name="capture-service",
        plane="runtime",
        default_port=8097,
        responsibilities=("Label Studio capture", "ROI materialization", "capture retries"),
        non_responsibilities=("training snapshot export", "realtime inference routing"),
    ),
    RuntimeServiceBoundary(
        service_name="result-service",
        plane="runtime",
        default_port=8098,
        responsibilities=("latest results", "result history", "normalized business outputs"),
        non_responsibilities=("connector delivery", "pipeline graph editing"),
    ),
    RuntimeServiceBoundary(
        service_name="connector-service",
        plane="runtime",
        default_port=8099,
        responsibilities=("output connector delivery", "connector retries", "connector plugins"),
        non_responsibilities=("trigger HTTP response ownership", "Label Studio annotation UI"),
    ),
)


def runtime_service_names() -> tuple[str, ...]:
    return tuple(boundary.service_name for boundary in RUNTIME_SERVICE_BOUNDARIES)


def runtime_service_boundary(service_name: str) -> RuntimeServiceBoundary:
    for boundary in RUNTIME_SERVICE_BOUNDARIES:
        if boundary.service_name == service_name:
            return boundary
    raise KeyError(f"Unknown runtime service: {service_name}")
