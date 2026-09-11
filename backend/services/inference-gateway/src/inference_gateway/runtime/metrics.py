from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from prometheus_client import Counter, Gauge, Histogram, REGISTRY

from cv_contracts import LatencyQuantiles


def _collector(name: str) -> Any | None:
    return getattr(REGISTRY, "_names_to_collectors", {}).get(name)


def _counter(name: str, documentation: str, labelnames: tuple[str, ...]) -> Counter:
    existing = _collector(name)
    if isinstance(existing, Counter):
        return existing
    return Counter(name, documentation, labelnames=labelnames)


def _gauge(name: str, documentation: str, labelnames: tuple[str, ...]) -> Gauge:
    existing = _collector(name)
    if isinstance(existing, Gauge):
        return existing
    return Gauge(name, documentation, labelnames=labelnames)


def _histogram(name: str, documentation: str, labelnames: tuple[str, ...]) -> Histogram:
    existing = _collector(name)
    if isinstance(existing, Histogram):
        return existing
    return Histogram(name, documentation, labelnames=labelnames)


REQUESTS_TOTAL = _counter(
    "cv_inference_requests_total",
    "Total inference requests handled by the gateway runtime.",
    ("service", "target_path", "operation", "result", "deployment_release_id"),
)
REQUEST_DURATION_SECONDS = _histogram(
    "cv_inference_request_duration_seconds",
    "Latency of inference requests handled by the gateway runtime.",
    ("service", "target_path", "operation", "deployment_release_id"),
)
REQUESTS_INFLIGHT = _gauge(
    "cv_inference_requests_inflight",
    "Current inflight inference requests admitted into the runtime.",
    ("service", "target_path"),
)
ADMISSION_REJECTIONS_TOTAL = _counter(
    "cv_inference_admission_rejections_total",
    "Total requests rejected because the runtime was overloaded.",
    ("service", "target_path", "operation"),
)


class WindowedLatencyMetrics:
    def __init__(self, *, window_size: int) -> None:
        self._latencies_ms: deque[float] = deque(maxlen=max(window_size, 1))
        self._timestamps: deque[float] = deque(maxlen=max(window_size, 1))
        self._lock = threading.Lock()

    def record(self, latency_ms: float) -> None:
        now = time.time()
        with self._lock:
            self._latencies_ms.append(max(latency_ms, 0.0))
            self._timestamps.append(now)

    def snapshot(self) -> LatencyQuantiles:
        with self._lock:
            values = list(self._latencies_ms)
            timestamps = list(self._timestamps)
        if not values:
            return LatencyQuantiles()
        values.sort()
        return LatencyQuantiles(
            sample_count=len(values),
            p50_ms=_quantile(values, 0.50),
            p95_ms=_quantile(values, 0.95),
            p99_ms=_quantile(values, 0.99),
            throughput_per_second=_throughput(timestamps),
        )


class RuntimeMetrics:
    def __init__(self, *, service_name: str, target_path: str, window_size: int) -> None:
        self._service_name = service_name
        self._target_path = target_path
        self._window_size = window_size
        self._lock = threading.Lock()
        self._by_release: dict[str, WindowedLatencyMetrics] = {}

    def inflight_changed(self, *, current_inflight: int) -> None:
        REQUESTS_INFLIGHT.labels(self._service_name, self._target_path).set(current_inflight)

    def record_overload(self, *, operation: str) -> None:
        ADMISSION_REJECTIONS_TOTAL.labels(self._service_name, self._target_path, operation).inc()

    def record_success(self, *, operation: str, deployment_release_id: str, total_ms: float) -> None:
        self._window(deployment_release_id).record(total_ms)
        REQUESTS_TOTAL.labels(
            self._service_name,
            self._target_path,
            operation,
            "success",
            deployment_release_id,
        ).inc()
        REQUEST_DURATION_SECONDS.labels(
            self._service_name,
            self._target_path,
            operation,
            deployment_release_id,
        ).observe(max(total_ms, 0.0) / 1000.0)

    def record_failure(
        self,
        *,
        operation: str,
        deployment_release_id: str,
        total_ms: float,
        result: str,
    ) -> None:
        self._window(deployment_release_id).record(total_ms)
        REQUESTS_TOTAL.labels(
            self._service_name,
            self._target_path,
            operation,
            result,
            deployment_release_id,
        ).inc()
        REQUEST_DURATION_SECONDS.labels(
            self._service_name,
            self._target_path,
            operation,
            deployment_release_id,
        ).observe(max(total_ms, 0.0) / 1000.0)

    def snapshot(self, *, deployment_release_id: str) -> LatencyQuantiles:
        return self._window(deployment_release_id).snapshot()

    def _window(self, deployment_release_id: str) -> WindowedLatencyMetrics:
        with self._lock:
            window = self._by_release.get(deployment_release_id)
            if window is None:
                window = WindowedLatencyMetrics(window_size=self._window_size)
                self._by_release[deployment_release_id] = window
            return window


def _quantile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    index = max(min(int(round((len(values) - 1) * quantile)), len(values) - 1), 0)
    return round(values[index], 3)


def _throughput(timestamps: list[float]) -> float:
    if len(timestamps) < 2:
        return float(len(timestamps))
    elapsed = max(timestamps[-1] - timestamps[0], 1e-6)
    return round(len(timestamps) / elapsed, 3)
