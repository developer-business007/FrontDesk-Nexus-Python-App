"""Auto-correct ID scan image orientation using Google Vision text layout."""

from __future__ import annotations

import base64
import io
import logging
import math
import os
import statistics
from typing import Any

logger = logging.getLogger(__name__)


def auto_orientation_enabled() -> bool:
    v = os.environ.get("FDN_AUTO_CORRECT_IMAGE_ORIENTATION", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def rotate_b64_clockwise(b64: str, degrees_cw: int) -> str:
    deg = int(degrees_cw) % 360
    if deg == 0 or not b64:
        return b64
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow not installed (pip install Pillow)") from exc

    raw = base64.b64decode(b64, validate=True)
    img = Image.open(io.BytesIO(raw))
    # PIL rotates counter-clockwise for positive angles.
    rotated = img.rotate(-deg, expand=True)
    buf = io.BytesIO()
    fmt = (img.format or "JPEG").upper()
    if fmt not in ("JPEG", "JPG", "PNG"):
        fmt = "JPEG"
    save_fmt = "JPEG" if fmt in ("JPEG", "JPG") else "PNG"
    rotated.save(buf, format=save_fmt, quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _collect_baseline_angles_deg(full_text_annotation: Any) -> list[float]:
    angles: list[float] = []
    if not full_text_annotation:
        return angles
    for page in full_text_annotation.pages:
        for block in page.blocks:
            for para in block.paragraphs:
                for word in para.words:
                    verts = word.bounding_box.vertices
                    if len(verts) < 2:
                        continue
                    dx = float(verts[1].x - verts[0].x)
                    dy = float(verts[1].y - verts[0].y)
                    if dx == 0 and dy == 0:
                        continue
                    angles.append(math.degrees(math.atan2(dy, dx)))
    return angles


def _clockwise_rotation_to_upright(angles_deg: list[float]) -> int:
    """Map detected text baselines to a clockwise correction (0, 90, 180, 270)."""
    if not angles_deg:
        return 0
    med = statistics.median(angles_deg)
    while med > 180:
        med -= 360
    while med <= -180:
        med += 360
    if -45 <= med <= 45:
        return 0
    if 45 < med <= 135:
        return 270
    if med > 135 or med <= -135:
        return 180
    return 90


def _face_upside_down_rotation(image_bytes: bytes) -> int:
    """
    When OCR layout is inconclusive, use face roll.
    Returns 180 if the dominant face appears upside-down, else 0.
    """
    from scanner import ScannerError, _get_vision_client

    try:
        from google.cloud import vision as _vision
    except ImportError:
        return 0

    image = _vision.Image(content=image_bytes)
    client = _get_vision_client()
    resp = client.face_detection(image=image)
    if resp.error.message:
        return 0

    upside = 0
    upright = 0
    for face in resp.face_annotations:
        roll = float(face.roll_angle or 0)
        # roll_angle: head tilt; near ±180° ⇒ upside-down portrait on flat scanner.
        if abs(roll) > 135:
            upside += 1
        elif abs(roll) < 45:
            upright += 1
    if upside > upright:
        return 180
    return 0


def detect_clockwise_rotation_to_upright(image_bytes: bytes) -> int:
    """Return degrees clockwise to rotate so document text reads normally."""
    from scanner import ScannerError, _get_vision_client

    try:
        from google.cloud import vision as _vision
    except ImportError as exc:
        raise ScannerError("google-cloud-vision not installed") from exc

    image = _vision.Image(content=image_bytes)
    client = _get_vision_client()
    doc_resp = client.document_text_detection(image=image)
    if doc_resp.error.message:
        raise ScannerError(f"Vision API error: {doc_resp.error.message}")

    angles = _collect_baseline_angles_deg(doc_resp.full_text_annotation)
    if angles:
        return _clockwise_rotation_to_upright(angles)
    return _face_upside_down_rotation(image_bytes)


def correct_id_front_image_b64(b64: str) -> str:
    """Rotate portrait/front ID image upright when Vision detects inverted/sideways text."""
    trimmed = (b64 or "").strip()
    if not trimmed or not auto_orientation_enabled():
        return trimmed
    try:
        raw = base64.b64decode(trimmed, validate=True)
        rot = detect_clockwise_rotation_to_upright(raw)
        if rot:
            logger.info("[auto-orient] rotating front image %d° clockwise", rot)
            return rotate_b64_clockwise(trimmed, rot)
    except Exception as exc:  # noqa: BLE001 — never block scan on orient failure
        logger.warning("[auto-orient] skipped: %s", exc)
    return trimmed
