"""Small, bounded transient caches and the existing Result Output history projection."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

TRIGGER_REPLAY_SECONDS = 300
TRIGGER_CACHE_MAX_BYTES = 64 * 1024**2
TRIGGER_CACHE_MAX_ITEMS = 10_000
ACK_CACHE_MAX_ITEMS = 4096

class TransientCache[T]:
    """Caller holds its state lock. Limits include all channels, not each channel."""

    def __init__(
        self,
        *,
        max_bytes: int = 16 * 1024**2,
        max_items: int = 128,
        ttl_seconds: float = TRIGGER_REPLAY_SECONDS,
    ) -> None:
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.ttl_seconds = ttl_seconds
        self._items: OrderedDict[str, tuple[float, int, T]] = OrderedDict()
        self._bytes = 0

    def _discard(self, key: str) -> None:
        previous = self._items.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]

    def _expire(self) -> None:
        now = time.monotonic()
        for key, (expires, _, _) in list(self._items.items()):
            if expires <= now:
                self._discard(key)

    def get(self, key: str) -> T | None:
        self._expire()
        item = self._items.get(key)
        return item[2] if item is not None else None

    def put(self, key: str, value: T, *, size_bytes: int) -> None:
        self._expire()
        self._discard(key)
        if size_bytes > self.max_bytes:
            return
        while self._items and (
            len(self._items) >= self.max_items or self._bytes + size_bytes > self.max_bytes
        ):
            self._discard(next(iter(self._items)))
        self._items[key] = (time.monotonic() + self.ttl_seconds, size_bytes, value)
        self._bytes += size_bytes


def history_payload(execution: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ANN401
    """Transfer only history destinations, never the full per-frame execution report."""
    entries = execution.get("persisted_results")
    if not isinstance(entries, list) or not entries:
        return None
    entries = _without_images(entries)
    fields: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload") or {}
        result = payload.get("result") if isinstance(payload, dict) else None
        if isinstance(result, dict):
            fields.update(result)
        # The older E1 persisted-result node declares its output explicitly as well.
        elif entry.get("kind") == "edge_result" and isinstance(entry.get("value"), dict):
            fields.update(entry["value"])
    if not fields:
        return None
    return {
        "schema_version": execution.get("schema_version", "runtime.execution.result.v1"),
        "status": execution.get("status", "completed"),
        "pipeline_version_id": execution.get("pipeline_version_id"),
        "persisted_results": entries,
        "final_response": fields,
        "runtime_metadata": {"edge_history_projection": True},
    }


def _without_images(value: Any) -> Any:  # noqa: ANN401
    if isinstance(value, dict):
        return {
            key: _without_images(item)
            for key, item in value.items()
            if key not in {"image_base64", "data_uri"}
        }
    if isinstance(value, list):
        return [_without_images(item) for item in value]
    return value
