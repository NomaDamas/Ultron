"""Bounded image intake pipeline for the multimodal (VLM) path.

Enforces hard caps, sniffs the MIME type from magic bytes, validates dimensions and
pixel count, and strips EXIF/metadata by re-encoding from a fresh pixel buffer (no
``info``/``exif`` carried over). Raw bytes and the provider data URL live only inside a
request-scoped ``RawImagePayload``; durable/read metadata is limited to MIME, byte
length, dimensions, pixel count, and a sha256 fingerprint prefix.

Rejections raise ``ImageRejected`` with a safe, generic message (no filename, EXIF,
data URL, raw bytes, or provider payload).
"""

from __future__ import annotations

import base64
import hashlib
import io

from ultron.model_provider import ImagePart, RawImagePayload

MAX_IMAGE_COUNT = 1
MAX_BYTES = 4 * 1024 * 1024  # 4 MiB before decode/base64
MAX_DIMENSION = 4096
MAX_PIXELS = 16 * 1024 * 1024  # 16 megapixels
ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp")

_SAVE_FORMAT = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


class ImageRejected(ValueError):
    """Raised with a safe, generic message when an image fails validation."""


def _sniff_mime(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def validate_image(raw: bytes) -> ImagePart:
    """Validate, sanitize, and wrap a single image into an ``ImagePart``.

    Raises ``ImageRejected`` (safe message) on any violation.
    """
    if not raw:
        raise ImageRejected("image validation failed")
    if len(raw) > MAX_BYTES:
        raise ImageRejected("image too large")
    mime = _sniff_mime(raw)
    if mime is None or mime not in ALLOWED_MIME:
        raise ImageRejected("unsupported image type")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImageRejected("image validation failed") from None

    try:
        with Image.open(io.BytesIO(raw)) as probe:
            probe.verify()  # detect truncation/corruption
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            width, height = img.size
            if width <= 0 or height <= 0:
                raise ImageRejected("image validation failed")
            if width > MAX_DIMENSION or height > MAX_DIMENSION:
                raise ImageRejected("image dimensions too large")
            if width * height > MAX_PIXELS:
                raise ImageRejected("image dimensions too large")
            mode = img.mode
            pixels = img.tobytes()
            # Rebuild from raw pixels so no EXIF/ICC/text metadata survives.
            clean = Image.frombytes(mode, (width, height), pixels)
            save_format = _SAVE_FORMAT[mime]
            if save_format == "JPEG" and clean.mode not in ("RGB", "L"):
                clean = clean.convert("RGB")
            buffer = io.BytesIO()
            clean.save(buffer, format=save_format)
            sanitized = buffer.getvalue()
    except ImageRejected:
        raise
    except Exception:
        raise ImageRejected("image validation failed") from None

    fingerprint = hashlib.sha256(sanitized).hexdigest()[:16]
    data_url = f"data:{mime};base64,{base64.b64encode(sanitized).decode('ascii')}"
    return ImagePart(
        mime_type=mime,
        byte_length=len(sanitized),
        width=width,
        height=height,
        pixel_count=width * height,
        fingerprint=fingerprint,
        _raw=RawImagePayload(sanitized, data_url),
    )
