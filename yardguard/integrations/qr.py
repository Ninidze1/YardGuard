"""QR codes for pairing links (phone access, ntfy topic, Telegram deep link).

Uses the optional pure-Python package segno. Without it every function returns None and callers
show the link text with a Copy button instead. QR codes stay black on white in both themes, so
every phone camera can read them.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger("yardguard")

try:
    import segno
    AVAILABLE = True
except Exception:  # pragma: no cover - depends on the installation
    segno = None
    AVAILABLE = False


def _make(text: str):
    if not AVAILABLE or not text:
        return None
    try:
        return segno.make(text, error="m", micro=False)
    except Exception as exc:  # too long, or a segno problem
        log.warning("QR code failed: %s", type(exc).__name__)
        return None


def matrix(text: str) -> list[list[bool]] | None:
    """Rows of modules (True = dark), without the quiet border (the caller adds it)."""
    qr = _make(text)
    if qr is None:
        return None
    return [[bool(v) for v in row] for row in qr.matrix_iter(border=0)]


def to_qimage(text: str, scale: int = 6, dark: str = "#000", light: str = "#fff", border: int = 2):
    """QImage of the code (with a `border` modules wide quiet zone), or None."""
    m = matrix(text)
    if m is None:
        return None
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QColor, QImage, QPainter

    n = len(m) + 2 * border
    scale = max(1, int(scale))
    img = QImage(n * scale, n * scale, QImage.Format_RGB32)
    img.fill(QColor(light))
    p = QPainter(img)
    try:
        color = QColor(dark)
        for y, row in enumerate(m):
            for x, on in enumerate(row):
                if on:
                    p.fillRect(QRect((x + border) * scale, (y + border) * scale, scale, scale), color)
    finally:
        p.end()
    return img


def to_svg(text: str, scale: int = 4, border: int = 2) -> str | None:
    """Standalone SVG text of the code, or None."""
    qr = _make(text)
    if qr is None:
        return None
    buf = io.BytesIO()
    try:
        qr.save(buf, kind="svg", scale=scale, border=border, dark="#000", light="#fff", xmldecl=False)
    except Exception as exc:
        log.warning("QR SVG failed: %s", type(exc).__name__)
        return None
    return buf.getvalue().decode("utf-8")
