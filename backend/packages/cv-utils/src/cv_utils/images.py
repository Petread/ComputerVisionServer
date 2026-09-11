from __future__ import annotations

from dataclasses import dataclass

from cv_utils.errors import PlatformError


@dataclass(frozen=True)
class ImageInspection:
    mime_type: str
    extension: str
    width: int | None
    height: int | None


def inspect_image_bytes(image_bytes: bytes) -> ImageInspection:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n") and len(image_bytes) >= 24:
        width = int.from_bytes(image_bytes[16:20], byteorder="big")
        height = int.from_bytes(image_bytes[20:24], byteorder="big")
        return ImageInspection("image/png", ".png", width, height)

    if image_bytes[:2] == b"\xff\xd8":
        width, height = _jpeg_size(image_bytes)
        return ImageInspection("image/jpeg", ".jpg", width, height)

    if image_bytes[:2] == b"BM" and len(image_bytes) >= 26:
        width = int.from_bytes(image_bytes[18:22], byteorder="little", signed=True)
        height = abs(int.from_bytes(image_bytes[22:26], byteorder="little", signed=True))
        return ImageInspection("image/bmp", ".bmp", width, height)

    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        width, height = _webp_size(image_bytes)
        return ImageInspection("image/webp", ".webp", width, height)

    raise PlatformError(
        code="unsupported_image_type",
        message="The uploaded file is not a supported image format.",
        detail="Supported upload formats are PNG, JPEG, BMP, and WEBP.",
        status_code=415,
    )


def _jpeg_size(image_bytes: bytes) -> tuple[int | None, int | None]:
    index = 2
    while index + 8 < len(image_bytes):
        if image_bytes[index] != 0xFF:
            index += 1
            continue
        marker = image_bytes[index + 1]
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        if marker == 0xDA:
            break
        segment_length = int.from_bytes(image_bytes[index + 2 : index + 4], byteorder="big")
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(image_bytes[index + 5 : index + 7], byteorder="big")
            width = int.from_bytes(image_bytes[index + 7 : index + 9], byteorder="big")
            return width, height
        index += 2 + max(segment_length, 2)
    return None, None


def _webp_size(image_bytes: bytes) -> tuple[int | None, int | None]:
    chunk_header = image_bytes[12:16]
    if chunk_header == b"VP8X" and len(image_bytes) >= 30:
        width = int.from_bytes(image_bytes[24:27] + b"\x00", byteorder="little") + 1
        height = int.from_bytes(image_bytes[27:30] + b"\x00", byteorder="little") + 1
        return width, height
    return None, None
