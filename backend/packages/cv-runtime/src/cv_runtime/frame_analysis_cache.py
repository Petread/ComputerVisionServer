from __future__ import annotations

import threading
from collections.abc import Callable
from functools import lru_cache
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    import numpy as np

T = TypeVar("T")


class FrameAnalysisCache:
    """Small execution-local cache; retain owners to prevent object-id reuse.

    Factories run outside the lock, including in parallel measurement branches.
    Only immutable results belong here. Nothing is persisted in runtime state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[int, object], tuple[object, Any]] = {}

    def get(self, owner: object, key: object, factory: Callable[[], T]) -> T:
        cache_key = (id(owner), key)
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is not None:
                return entry[1]
        value = factory()
        with self._lock:
            if len(self._entries) >= 32:
                self._entries.pop(next(iter(self._entries)))
            return self._entries.setdefault(cache_key, (owner, value))[1]


@lru_cache(maxsize=8)
def immutable_roi_mask(
    version: str, width: int, height: int, vertices: tuple[tuple[int, int], ...]
) -> np.ndarray:
    """Bound raster caching by calibration version AND the actual projected polygon."""
    import cv2
    import numpy as np

    result = np.zeros((height, width), dtype=np.uint8)
    cv2.fillPoly(result, [np.asarray(vertices, dtype=np.int32)], 255)
    result.flags.writeable = False
    return result
