from __future__ import annotations

import numpy as np


def encoder_frame(frame: object) -> np.ndarray:
    """Normalize a camera frame to contiguous, NVENC-compatible 8-bit BGR.

    The direct camera adapter tries to request an even ROI.  Some GenICam
    cameras nevertheless deliver an odd effective dimension due to their ROI
    increment or offset constraints.  Trim only the trailing row/column as a
    final encoder-boundary safeguard instead of rejecting an otherwise valid
    live camera frame.
    """
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
    height, width = value.shape[:2]
    even_height = height - (height % 2)
    even_width = width - (width % 2)
    if even_height <= 0 or even_width <= 0:
        raise ValueError("Camera frames must have positive NVENC-compatible dimensions.")
    value = value[:even_height, :even_width]
    return np.ascontiguousarray(value)
