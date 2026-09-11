from __future__ import annotations

from typing import Any

_BITS_PER_PIXEL = {
    "mono8": 8,
    "bayerrg8": 8,
    "bayerbg8": 8,
    "bayergb8": 8,
    "bayergr8": 8,
    "yuv422packed": 16,
    "rgb8": 24,
    "bgr8": 24,
}


def validate_nvenc_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("Stream dimensions must be positive.")
    if width % 2 or height % 2:
        raise ValueError(
            f"NVENC requires even stream dimensions; received {width}x{height}. "
            "Configure an even camera ROI."
        )


def validate_basler_link_budget(config: dict[str, Any], *, required: bool = False) -> float | None:
    try:
        width = int(config.get("width") or 0)
        height = int(config.get("height") or 0)
        fps = float(config.get("fps") or 0)
        link_speed = float(config.get("link_speed_mbps") or 0)
    except (TypeError, ValueError) as exc:
        if required:
            raise ValueError("Basler stream profile contains invalid numeric values.") from exc
        return None
    bpp = _BITS_PER_PIXEL.get(str(config.get("pixel_format") or "").strip().casefold())
    if not all((width, height, fps, link_speed, bpp)):
        if required:
            raise ValueError(
                "Basler stream start requires width, height, fps, link_speed_mbps, and a "
                "supported pixel_format so the camera link budget can be verified."
            )
        return None
    validate_nvenc_dimensions(width, height)
    payload_mbps = width * height * fps * int(bpp) / 1_000_000
    if payload_mbps > link_speed * 0.8:
        raise ValueError(
            "Basler Pylon image payload exceeds 80% of the configured link_speed_mbps "
            f"({payload_mbps:.0f} Mbit/s payload on {link_speed:.0f} Mbit/s). "
            "Use Bayer/Mono8, a smaller ROI, or a lower camera FPS."
        )
    return payload_mbps


def automatic_stream_bitrate_kbps(
    width: int,
    height: int,
    fps: float,
    *,
    bits_per_pixel: float = 0.12,
    minimum: int = 2_000,
    maximum: int = 20_000,
) -> int:
    calculated = int(round(width * height * max(fps, 1.0) * bits_per_pixel / 1000))
    return min(max(calculated, minimum), maximum)
