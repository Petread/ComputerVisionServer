"""Lossless binary segmentation masks in an explicitly sized image space."""

# ruff: noqa: ANN401
from __future__ import annotations

import base64
import io
from typing import Any

import numpy as np
from cv_contracts.inference import SegmentationMask
from PIL import Image


def encode_mask(mask: Any, *, native_size: tuple[int, int] | None = None) -> SegmentationMask:
    array = np.asarray(mask, dtype=bool)
    if array.ndim != 2:
        raise ValueError("segmentation_mask_requires_two_dimensions")
    height, width = array.shape
    stream = io.BytesIO()
    Image.fromarray(array.astype(np.uint8) * 255).save(stream, format="PNG")
    return SegmentationMask(
        data=base64.b64encode(stream.getvalue()).decode("ascii"),
        width=width,
        height=height,
        native_width=(native_size or (width, height))[0],
        native_height=(native_size or (width, height))[1],
    )


def decode_mask(mask: SegmentationMask | dict[str, Any]) -> np.ndarray:
    mask = SegmentationMask.model_validate(mask)
    raw = base64.b64decode(mask.data, validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        if image.format != "PNG" or image.size != (mask.width, mask.height):
            raise ValueError("segmentation_mask_dimensions_or_format_invalid")
        return np.asarray(image.convert("L")) > 0


def restore_mask(
    mask: SegmentationMask,
    *,
    source_size: tuple[int, int],
    input_size: tuple[int, int],
    padding: tuple[int, int] = (0, 0),
    scale: float = 1,
    letterboxed: bool = False,
) -> SegmentationMask:
    array = decode_mask(mask)
    image = Image.fromarray(array)
    image = image.resize(input_size, Image.Resampling.NEAREST)
    left, top = padding if letterboxed else (0, 0)
    right = (
        min(input_size[0], left + round(source_size[0] * scale)) if letterboxed else input_size[0]
    )
    bottom = (
        min(input_size[1], top + round(source_size[1] * scale)) if letterboxed else input_size[1]
    )
    if right <= left or bottom <= top:
        raise ValueError("segmentation_mask_crop_invalid")
    restored = image.crop((left, top, right, bottom)).resize(source_size, Image.Resampling.NEAREST)
    return encode_mask(
        np.asarray(restored),
        native_size=(
            max(1, round((mask.native_width or mask.width) * (right - left) / input_size[0])),
            max(1, round((mask.native_height or mask.height) * (bottom - top) / input_size[1])),
        ),
    )
