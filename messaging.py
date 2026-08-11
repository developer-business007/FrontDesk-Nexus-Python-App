"""Chrome Native Messaging framing: 4-byte little-endian length + JSON payload."""

from __future__ import annotations

import json
import struct
import sys
from typing import Any, BinaryIO, Optional


def read_message(stream: BinaryIO) -> Optional[dict[str, Any]]:
    """
    Read one length-prefixed JSON object from a binary stream.

    Returns None on clean EOF before any length bytes.
    Raises ValueError for framing/protocol errors.
    """
    len_bytes = stream.read(4)
    if len(len_bytes) == 0:
        return None
    if len(len_bytes) != 4:
        raise ValueError("Unexpected EOF while reading message length")

    (length,) = struct.unpack("<I", len_bytes)
    if length > 1024 * 1024 * 64:  # 64 MiB guard
        raise ValueError(f"Message length {length} exceeds maximum allowed size")

    payload = stream.read(length)
    if len(payload) != length:
        raise ValueError("Unexpected EOF while reading message body")

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Message body is not valid UTF-8") from exc

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise ValueError("JSON message must be an object")

    return obj


def write_message(stream: BinaryIO, message: dict[str, Any]) -> None:
    """Write one JSON object with Chrome Native Messaging framing."""
    body = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = struct.pack("<I", len(body))
    stream.write(header)
    stream.write(body)
    stream.flush()


def stdin_stdout_streams() -> tuple[BinaryIO, BinaryIO]:
    """Return binary stdin/stdout suitable for Native Messaging on Windows and POSIX."""
    return sys.stdin.buffer, sys.stdout.buffer
