"""Small helpers for the Native Messaging host."""

from __future__ import annotations

import base64
from pathlib import Path


def file_to_base64(path: Path) -> str:
    """Read a binary file and return a standard base64 ASCII string."""
    data = path.read_bytes()
    return bytes_to_base64(data)


def bytes_to_base64(data: bytes) -> str:
    """Encode raw image bytes as a standard base64 ASCII string."""
    return base64.b64encode(data).decode("ascii")
