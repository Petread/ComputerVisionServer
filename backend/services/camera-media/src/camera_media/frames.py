from __future__ import annotations

import numpy as np


def encoder_frame(frame: object) -> np.ndarray:
    """Normalize a native-size camera frame to contiguous 8-bit BGR."""
    value = np.asarray(frame)
    if value.ndim == 2:
        value = np.repeat(value[:, :, np.newaxis], 3, axis=2)
    elif value.ndim == 3 and value.shape[2] == 1:
        value = np.repeat(value, 3, axis=2)
    elif value.ndim == 3 and value.shape[2] == 4:
        value = value[:, :, :3]
    if value.ndim != 3 or value.shape[2] != 3:
        raise ValueError("Camera frames must be convertible to 8-bit BGR for NVENC.")
    if value.dtype != np.uint8:
        raise ValueError("Camera frames must use an 8-bit pixel format for NVENC.")
    return np.ascontiguousarray(value)
