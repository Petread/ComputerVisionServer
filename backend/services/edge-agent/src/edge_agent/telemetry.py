"""Bounded, in-memory performance counters for the existing Edge health response."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from cv_contracts.edge import EdgeRuntimeMetrics, EdgeTimingSummary

TimingSamples = dict[str, deque[tuple[float, float, str | None, str | None]]]


class EdgePerformanceCounters:
    WINDOW_SECONDS = 10.0
    SAMPLE_LIMIT = 256

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        # FPS counts use 100 ms buckets independently of the bounded timing samples.
        self._rates: deque[list[float]] = deque(maxlen=102)
        self._stages: TimingSamples = {}
        self._nodes: TimingSamples = {}
        self.acquired = self.processed = self.sampled_out = self.errors = 0
        self.last_frame_at: str | None = None
        self.last_execution_at: str | None = None

    def acquired_frame(self, captured_at: str, *, sampled_out: bool = False) -> None:
        with self._lock:
            self.acquired += 1
            self.sampled_out += int(sampled_out)
            self.last_frame_at = captured_at
            self._rate(self._clock(), acquired=1)

    def execution(self, result: dict[str, Any], *, elapsed_ms: float) -> None:
        now = self._clock()
        with self._lock:
            failed = result.get("status") == "failed"
            self.errors += int(failed)
            self.processed += int(not failed)
            self.last_execution_at = datetime.now(UTC).isoformat()
            self._rate(now, processed=int(not failed))
            timing = result.get("timing")
            timing = timing if isinstance(timing, dict) else {}
            self._sample(self._stages, "pipeline", now, elapsed_ms)
            nodes = timing.get("node_timings_ms", timing.get("nodes", []))
            for node in nodes if isinstance(nodes, list) else []:
                if not isinstance(node, dict) or not node.get("node_key"):
                    continue
                self._sample(
                    self._nodes,
                    str(node["node_key"]),
                    now,
                    node.get("total_ms", node.get("duration_ms")),
                    node_type=node.get("node_type"),
                    status=node.get("status"),
                )

    def execution_error(self) -> None:
        with self._lock:
            self.errors += 1

    def stage(self, key: str, elapsed_ms: float) -> None:
        with self._lock:
            self._sample(self._stages, key, self._clock(), elapsed_ms)

    def snapshot(self) -> EdgeRuntimeMetrics:
        now = self._clock()
        with self._lock:
            cutoff = now - self.WINDOW_SECONDS
            while self._rates and self._rates[0][0] <= cutoff:
                self._rates.popleft()
            elapsed = min(max(now - self._started, 0.0), self.WINDOW_SECONDS)
            return EdgeRuntimeMetrics(
                observed_at=datetime.now(UTC).isoformat(),
                window_seconds=elapsed,
                acquired_frames=self.acquired,
                processed_frames=self.processed,
                sampled_out_frames=self.sampled_out,
                execution_errors=self.errors,
                source_fps=round(sum(row[1] for row in self._rates) / elapsed, 2) if elapsed else 0,
                processed_fps=round(sum(row[2] for row in self._rates) / elapsed, 2)
                if elapsed
                else 0,
                last_frame_at=self.last_frame_at,
                last_execution_at=self.last_execution_at,
                stages=self._summaries(self._stages, cutoff),
                nodes=self._summaries(self._nodes, cutoff),
            )

    def _rate(self, now: float, *, acquired: int = 0, processed: int = 0) -> None:
        bucket = math.floor(now * 10) / 10
        if self._rates and self._rates[-1][0] == bucket:
            self._rates[-1][1] += acquired
            self._rates[-1][2] += processed
        else:
            self._rates.append([bucket, acquired, processed])

    def _sample(
        self,
        target: TimingSamples,
        key: str,
        now: float,
        value: object,
        *,
        node_type: str | None = None,
        status: str | None = None,
    ) -> None:
        try:
            duration = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if not math.isfinite(duration) or duration < 0:
            return
        if key not in target:
            if len(target) >= (256 if target is self._nodes else 16):
                return
            target[key] = deque(maxlen=self.SAMPLE_LIMIT)
        target[key].append((now, duration, node_type, status))

    @staticmethod
    def _summaries(samples: TimingSamples, cutoff: float) -> list[EdgeTimingSummary]:
        summaries = []
        for key, values in samples.items():
            while values and values[0][0] <= cutoff:
                values.popleft()
            if not values:
                continue
            ordered = sorted(item[1] for item in values)
            summaries.append(
                EdgeTimingSummary(
                    key=key,
                    node_type=values[-1][2],
                    status=values[-1][3],
                    sample_count=len(ordered),
                    mean_ms=round(sum(ordered) / len(ordered), 3),
                    p95_ms=round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
                    max_ms=round(ordered[-1], 3),
                    last_ms=round(values[-1][1], 3),
                )
            )
        return summaries
