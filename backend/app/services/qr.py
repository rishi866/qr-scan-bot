"""QR-code PNG helper (deposit addresses, 2FA provisioning)."""

from __future__ import annotations

import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def qr_png(data: str, box_size: int = 8, border: int = 3) -> bytes:
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()
