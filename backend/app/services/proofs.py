"""Safe storage of dispute screenshots.

Uploads are decoded with Pillow (so only real images are kept), size/pixel limited, stripped of EXIF
metadata (GPS, device info) by re-encoding, and stored under random names inside ``UPLOAD_DIR``.
"""

from __future__ import annotations

import io
import secrets
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.config import get_settings

MAX_BYTES = 10 * 1024 * 1024
MAX_PIXELS = 50_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS  # decompression-bomb guard
ALLOWED = {"JPEG": ("jpg", "JPEG"), "PNG": ("png", "PNG"), "WEBP": ("webp", "WEBP")}


class ProofError(ValueError):
    """Message is safe to show to the scanner."""


def save_proof(data: bytes, dispute_id: int) -> str:
    """Validate + sanitise ``data`` and return its path relative to ``UPLOAD_DIR``."""
    if not data:
        raise ProofError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ProofError("The image is too large (max 10 MB).")
    try:
        probe = Image.open(io.BytesIO(data))
        fmt = probe.format or ""
        probe.verify()  # structural check
        if fmt not in ALLOWED:
            raise ProofError("Please send a JPEG, PNG or WEBP image.")
        image = Image.open(io.BytesIO(data))
        image.load()
    except ProofError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ProofError("That doesn't look like a valid image.") from exc

    ext, pil_format = ALLOWED[fmt]
    if pil_format == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    out = io.BytesIO()
    image.save(out, pil_format)  # re-encode: drops EXIF / embedded payloads

    relative = f"proofs/{dispute_id}-{secrets.token_hex(8)}.{ext}"
    path = Path(get_settings().upload_dir) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out.getvalue())
    path.chmod(0o640)
    return relative
