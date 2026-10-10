"""Validate an explicitly selected pipeline snapshot for the manual capture queue."""

from __future__ import annotations

import base64
import hashlib
import io

from cv_contracts.runtime import RuntimePipelineCaptureImage
from cv_runtime.payloads import FramePayload, RuntimeImageContext
from cv_utils import inspect_image_bytes
from PIL import Image


def selected_pipeline_capture_frame(
    image: RuntimePipelineCaptureImage,
    *,
    require_eligibility: bool,
    max_image_bytes: int,
) -> FramePayload:
    encoded = image.frame.image_base64
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("The selected pipeline snapshot does not contain image pixels.")
    if len(encoded) > ((max_image_bytes + 2) // 3) * 4:
        raise ValueError("Selected pipeline image exceeds the capture size limit.")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > max_image_bytes:
        raise ValueError("Selected pipeline image exceeds the capture size limit.")
    info = inspect_image_bytes(raw)
    if not info.width or not info.height or info.width * info.height > 16_000_000:
        raise ValueError("Selected pipeline image dimensions are invalid or too large.")
    if (image.frame.width, image.frame.height) != (info.width, info.height):
        raise ValueError("Selected pipeline image dimensions do not match its pixels.")
    if info.mime_type != image.frame.mime_type:
        raise ValueError("Selected pipeline image MIME type does not match its pixels.")
    try:
        with Image.open(io.BytesIO(raw)) as decoded:
            decoded.verify()
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValueError("Selected pipeline image is damaged or cannot be decoded.") from exc
    image_context = RuntimeImageContext.model_validate(image.frame.image_context)
    if (image_context.width, image_context.height) != (info.width, info.height):
        raise ValueError("Selected image context dimensions do not match the snapshot.")
    eligibility = image.eligibility
    if require_eligibility and eligibility is None:
        raise ValueError("This pipeline image requires a positive capture eligibility result.")
    if eligibility is not None and (
        eligibility.get("eligible") is not True
        or eligibility.get("image_context_id") != image_context.context_id
    ):
        raise ValueError("The selected pipeline image is not eligible for training capture.")
    return FramePayload(
        image_base64=encoded,
        mime_type=info.mime_type,
        width=info.width,
        height=info.height,
        timestamp=image.frame.source_timestamp,
        image_context=image_context,
        metadata={
            **image.frame.metadata,
            "capture_source_node_key": image.node_key,
            "capture_source_output_port": image.output_port,
            "capture_pipeline_version_id": image.pipeline_version_id,
            "capture_snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "capture_snapshot_origin": "operator_selected_pipeline_image",
            "frame_eligibility": eligibility or {},
        },
    )
