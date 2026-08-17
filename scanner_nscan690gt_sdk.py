"""
AMBIR nScan 690gt via NS690gt.DLL (proprietary SI_* API).

Per SDK addendum:
  SI_OpenInterface("nScan690gt")  — case-sensitive
  LoadLibrary("NS690gt.DLL")      — installed to Windows\\System32 by driver

Manual mode: wait for paper after host receives SCAN_DOCUMENT_NSCAN690GT (Scan ID click).
Auto mode:   background poll SI_GetPaperStatus; scan when paper is inserted (AmbirScan Auto Scan).

Duplex: SIP_DUPLEX_ENABLED + SI_ReadImageData(..., pageNumber=0|1) per SpeedbirdTest / MiniScan samples.
"""

from __future__ import annotations

import base64
import ctypes
import logging
import os
import struct
import sys
import threading
import time
from ctypes import byref, c_uint32, c_uint8
from pathlib import Path
from typing import Any

from scanner_ambir_sdk import (
    AmbirSDKError,
    SIR_ALREADY_OPEN,
    SIR_ENDOFDATA,
    SIR_SUCCESS,
    SI_CO_BGR,
    SI_FALSE,
    SI_SCANMODE_RGB,
    SI_TRUE,
    SIP_CHANNEL_ORDER,
    SIP_EOP_DETECT_ENABLED,
    SIP_LINE_WIDTH_IN_BYTES,
    SIP_SCAN_LENGTH_IN_LINES,
    SIP_SCAN_MODE,
    SIP_SCAN_WIDTH_IN_PIXELS,
    SIP_XOFFSET,
    SIP_XRESOLUTION,
    SIP_YOFFSET,
    SIP_YRESOLUTION,
    SICON_RANGE,
    SICON_LIST,
    SICON_SINGLE,
    _ERR_CODE_TIMEOUT,
    _bind,
    _check,
    _get_error_text,
    _get_prop,
    _list_int_values,
    _set_prop_bool,
    _set_prop_list_int,
    _set_prop_single_int,
)

logger = logging.getLogger(__name__)

_LOG_TAG = "[nScan690gt-sdk]"
_HOST_DIR = Path(__file__).resolve().parent

# nScan 690gt addendum
_OPEN_NAME = "nScan690gt"
_DLL_NAMES = ("NS690gt.DLL", "NS690GT.DLL")

# SIPaperStatus — defined here so we do not depend on ambir_sdk exporting them
SI_PS_PAPER_OUT = 0
SI_PS_PAPER_IN = 1

# Property IDs (ScannerAPI.h + 690gt addendum)
SIP_DUPLEX_ENABLED = 21
SIP_PREFEED_ENABLED = 50

_LINES_PER_READ = 16
_scanner_lock = threading.Lock()
_PROBE_CACHE_S = 30.0
_probe_cache: dict[str, Any] | None = None
_probe_cache_at = 0.0


def _resolve_dll_path() -> Path | None:
    env = os.environ.get("FDN_NSCAN690GT_DLL_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_file() else None
    sys_root = Path(os.environ.get("SystemRoot", "C:/Windows"))
    for sub in ("System32", "SysWOW64"):
        for name in _DLL_NAMES:
            p = sys_root / sub / name
            if p.is_file():
                return p
    return None


def _load_dll(dll_path: Path) -> ctypes.CDLL:
    if not dll_path.is_file():
        raise AmbirSDKError(f"NS690gt.DLL not found at: {dll_path}")
    try:
        os.add_dll_directory(str(dll_path.parent.resolve()))
    except (OSError, AttributeError):
        pass
    try:
        return ctypes.CDLL(str(dll_path))
    except OSError as exc:
        raise AmbirSDKError(
            f"Failed to load {dll_path.name}. Install the nScan 690gt driver; "
            f"Python bitness must match the DLL (64-bit). ({exc})"
        ) from exc


def _open_interface(dll: ctypes.CDLL) -> None:
    try:
        dll.SI_CloseInterface()
    except Exception:  # noqa: BLE001
        pass
    rc = dll.SI_OpenInterface(_OPEN_NAME.encode("ascii"))
    if rc not in (SIR_SUCCESS, SIR_ALREADY_OPEN):
        err = _get_error_text(dll)
        raise AmbirSDKError(
            f'SI_OpenInterface("{_OPEN_NAME}") → {rc:#x}' + (f": {err}" if err else ""),
            code=rc,
        )
    logger.info("%s SI_OpenInterface(%r) ok", _LOG_TAG, _OPEN_NAME)


def _close_interface(dll: ctypes.CDLL) -> None:
    try:
        dll.SI_CloseInterface()
    except Exception as exc:  # noqa: BLE001
        logger.debug("%s SI_CloseInterface: %s", _LOG_TAG, exc)


def _paper_status(dll: ctypes.CDLL) -> int:
    paper = c_uint32(0)
    rc = dll.SI_GetPaperStatus(ctypes.addressof(paper))
    _check(dll, rc, "SI_GetPaperStatus")
    return int(paper.value)


def _wait_for_paper(dll: ctypes.CDLL, *, timeout_s: float) -> None:
    logger.info("%s waiting up to %.0fs for card insertion…", _LOG_TAG, timeout_s)
    deadline = time.monotonic() + timeout_s
    while True:
        if _paper_status(dll) == SI_PS_PAPER_IN:
            logger.info("%s paper detected (SI_PS_PAPER_IN)", _LOG_TAG)
            return
        if time.monotonic() >= deadline:
            raise AmbirSDKError(
                f"No card detected within {int(timeout_s)}s. "
                "Remove any card, click Scan ID, then insert the card.",
                code=_ERR_CODE_TIMEOUT,
            )
        time.sleep(0.15)


def _wait_for_paper_out(dll: ctypes.CDLL, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while _paper_status(dll) == SI_PS_PAPER_IN:
        if time.monotonic() >= deadline:
            logger.warning("%s card still in scanner after %.0fs — continuing", _LOG_TAG, timeout_s)
            return
        time.sleep(0.2)


def _configure_id_scan(dll: ctypes.CDLL, *, target_dpi: int = 300) -> tuple[int, int, int, bool]:
    """Configure RGB duplex ID scan. Returns (width, max_lines, bytes_per_line, duplex_on)."""
    chosen_dpi = target_dpi
    x_prop = _get_prop(dll, SIP_XRESOLUTION)
    available_dpi = _list_int_values(x_prop)
    if available_dpi:
        chosen_dpi = target_dpi if target_dpi in available_dpi else min(
            available_dpi, key=lambda x: abs(x - target_dpi)
        )
        logger.info("%s DPI choices=%s → %d", _LOG_TAG, available_dpi, chosen_dpi)

    if x_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, x_prop, chosen_dpi)
    else:
        _set_prop_single_int(dll, SIP_XRESOLUTION, chosen_dpi)

    y_prop = _get_prop(dll, SIP_YRESOLUTION)
    if y_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, y_prop, chosen_dpi)
    else:
        _set_prop_single_int(dll, SIP_YRESOLUTION, chosen_dpi)

    mode_prop = _get_prop(dll, SIP_SCAN_MODE)
    if mode_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, mode_prop, SI_SCANMODE_RGB)
    else:
        _set_prop_single_int(dll, SIP_SCAN_MODE, SI_SCANMODE_RGB)

    _set_prop_single_int(dll, SIP_XOFFSET, 0)
    _set_prop_single_int(dll, SIP_YOFFSET, 0)
    # Duplex / EOP are SI_BOOL. Prefeed (id=50) is often invalid on 690gt — skip it.
    duplex_set = _set_prop_bool(dll, SIP_DUPLEX_ENABLED, True)
    _set_prop_bool(dll, SIP_EOP_DETECT_ENABLED, True)

    w_prop = _get_prop(dll, SIP_SCAN_WIDTH_IN_PIXELS)
    if w_prop.containerType == SICON_RANGE:
        w_prop.range.current.iVal = w_prop.range.maximum.iVal
        dll.SI_SetProperty(byref(w_prop))

    h_prop = _get_prop(dll, SIP_SCAN_LENGTH_IN_LINES)
    max_lines = h_prop.range.maximum.iVal if h_prop.containerType == SICON_RANGE else h_prop.single.current.iVal
    if h_prop.containerType == SICON_RANGE:
        six_inch = min(6 * chosen_dpi, max_lines)
        h_prop.range.current.iVal = six_inch
        dll.SI_SetProperty(byref(h_prop))
        max_lines = six_inch

    co_prop = _get_prop(dll, SIP_CHANNEL_ORDER)
    if co_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, co_prop, SI_CO_BGR)
    else:
        _set_prop_single_int(dll, SIP_CHANNEL_ORDER, SI_CO_BGR)

    w_actual = _get_prop(dll, SIP_SCAN_WIDTH_IN_PIXELS)
    width = w_actual.range.current.iVal if w_actual.containerType == SICON_RANGE else w_actual.single.current.iVal

    lw_prop = _get_prop(dll, SIP_LINE_WIDTH_IN_BYTES)
    bytes_per_line = lw_prop.single.current.iVal

    duplex_on = False
    try:
        dx = _get_prop(dll, SIP_DUPLEX_ENABLED)
        if dx.containerType == SICON_SINGLE:
            duplex_on = (
                dx.single.current.bVal == SI_TRUE
                or dx.single.current.iVal == SI_TRUE
            )
    except AmbirSDKError:
        duplex_on = duplex_set

    if duplex_set and not duplex_on:
        # GetProperty lag — trust successful SetProperty
        duplex_on = True

    logger.info(
        "%s configured %d×%d max lines, %d B/line, duplex=%s @ %d DPI",
        _LOG_TAG, width, max_lines, bytes_per_line, duplex_on, chosen_dpi,
    )
    return width, max_lines, bytes_per_line, duplex_on


def _rows_to_bmp(all_rows: list[bytes], width: int, bytes_per_line: int) -> bytes:
    if not all_rows:
        raise AmbirSDKError("No scan lines captured for BMP assembly.")
    row_stride = (bytes_per_line + 3) & ~3
    pixel_data = b"".join(all_rows)
    pixel_size = len(pixel_data)
    file_size = 14 + 40 + pixel_size
    bfh = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, 54)
    bih = struct.pack(
        "<IiiHHIIiiII",
        40, width, -len(all_rows), 1, 24, 0, pixel_size, 0, 0, 0, 0,
    )
    return bfh + bih + pixel_data


def _read_side(
    dll: ctypes.CDLL,
    *,
    side: int,
    width: int,
    max_lines: int,
    bytes_per_line: int,
) -> bytes:
    row_stride = (bytes_per_line + 3) & ~3
    buf = (c_uint8 * (row_stride * _LINES_PER_READ))()
    all_rows: list[bytes] = []

    while True:
        lines_returned = c_uint32(0)
        to_read = min(_LINES_PER_READ, max(1, max_lines - len(all_rows)))
        rc = dll.SI_ReadImageData(
            ctypes.cast(buf, ctypes.c_void_p),
            c_uint32(to_read),
            c_uint32(side),
            ctypes.addressof(lines_returned),
        )
        n = lines_returned.value
        if n > 0:
            for i in range(n):
                start = i * bytes_per_line
                row = bytes(buf[start : start + bytes_per_line])
                if row_stride > bytes_per_line:
                    row += b"\x00" * (row_stride - bytes_per_line)
                all_rows.append(row)

        if rc == SIR_ENDOFDATA:
            break
        if rc != SIR_SUCCESS:
            err = _get_error_text(dll)
            raise AmbirSDKError(
                f"SI_ReadImageData(side={side}) failed (code={rc:#x})" + (f": {err}" if err else ""),
                code=rc,
            )
        if n == 0:
            time.sleep(0.01)

    logger.info("%s side %d captured %d lines", _LOG_TAG, side, len(all_rows))
    return _rows_to_bmp(all_rows, width, bytes_per_line)


def _scan_duplex(
    dll: ctypes.CDLL,
    *,
    width: int,
    max_lines: int,
    bytes_per_line: int,
    duplex_on: bool,
) -> tuple[bytes, bytes | None]:
    rc = dll.SI_StartScan()
    _check(dll, rc, "SI_StartScan")
    scan_started = True
    try:
        front_bmp = _read_side(
            dll, side=0, width=width, max_lines=max_lines, bytes_per_line=bytes_per_line
        )
        back_bmp: bytes | None = None
        if duplex_on:
            back_bmp = _read_side(
                dll, side=1, width=width, max_lines=max_lines, bytes_per_line=bytes_per_line
            )
        rc = dll.SI_EndScan()
        scan_started = False
        if rc != SIR_SUCCESS:
            logger.warning("%s SI_EndScan returned %#x (non-fatal)", _LOG_TAG, rc)
        return front_bmp, back_bmp
    except Exception:
        if scan_started:
            try:
                dll.SI_EndScan()
            except Exception:  # noqa: BLE001
                pass
        raise


def _run_scan_cycle(
    *,
    wait_for_paper: bool,
    wait_timeout_s: float,
    resolution: int,
) -> dict[str, Any]:
    """Open → (optional wait) → duplex scan → eject → close. Caller must hold _scanner_lock."""
    dll_path = _resolve_dll_path()
    if dll_path is None:
        raise AmbirSDKError(
            "NS690gt.DLL not found. Install the nScan 690gt driver from ambir.com "
            "or set FDN_NSCAN690GT_DLL_PATH."
        )

    dll = _load_dll(dll_path)
    _bind(dll)
    _open_interface(dll)
    logger.info("%s opened %s", _LOG_TAG, dll_path)

    try:
        cal = c_uint32(0)
        rc = dll.SI_IsCalibrated(ctypes.addressof(cal))
        if rc == SIR_SUCCESS and cal.value == SI_FALSE:
            logger.warning(
                "%s scanner not calibrated — run AmbirScan/MiniScan calibration if quality is poor",
                _LOG_TAG,
            )

        if wait_for_paper:
            _wait_for_paper(dll, timeout_s=wait_timeout_s)
        elif _paper_status(dll) != SI_PS_PAPER_IN:
            raise AmbirSDKError("No card in scanner.", code=_ERR_CODE_TIMEOUT)

        width, max_lines, bpl, duplex_on = _configure_id_scan(dll, target_dpi=resolution)
        logger.info("%s SI_StartScan (duplex=%s)…", _LOG_TAG, duplex_on)
        front_bmp, back_bmp = _scan_duplex(
            dll, width=width, max_lines=max_lines, bytes_per_line=bpl, duplex_on=duplex_on
        )

        eject_rc = dll.SI_FeedPaperOut()
        if eject_rc != SIR_SUCCESS:
            logger.warning("%s SI_FeedPaperOut returned %#x", _LOG_TAG, eject_rc)

        removal_s = float(os.environ.get("FDN_NSCAN690GT_REMOVAL_TIMEOUT_S", "15").strip() or "15")
        _wait_for_paper_out(dll, timeout_s=removal_s)

        front_b64 = base64.b64encode(front_bmp).decode("ascii")
        back_b64 = base64.b64encode(back_bmp).decode("ascii") if back_bmp else ""

        logger.info(
            "%s scan complete — front=%d bytes back=%s",
            _LOG_TAG,
            len(front_bmp),
            f"{len(back_bmp)} bytes" if back_bmp else "none",
        )
        return {
            "type": "NSCAN690GT_SCAN_OK",
            "image_front_base64": front_b64,
            "image_back_base64": back_b64,
            "front_image_base64": front_b64,
            "back_image_base64": back_b64,
            "image_base64": front_b64,
            "duplex": duplex_on and bool(back_b64),
            "dll_path": str(dll_path),
            "resolution": resolution,
            "width": width,
        }
    finally:
        _close_interface(dll)


def scan_manual_blocking(*, wait_timeout_s: int | None = None, resolution: int | None = None) -> dict[str, Any]:
    """Manual mode: wait for card after Scan ID click, then duplex scan."""
    wait_s = wait_timeout_s if wait_timeout_s is not None else int(
        os.environ.get("FDN_NSCAN690GT_WAIT_TIMEOUT_S", "55").strip() or "55"
    )
    dpi = resolution if resolution is not None else int(
        os.environ.get("FDN_NSCAN690GT_RESOLUTION", "300").strip() or "300"
    )
    with _scanner_lock:
        return _run_scan_cycle(wait_for_paper=True, wait_timeout_s=float(wait_s), resolution=dpi)


def scan_auto_once(*, resolution: int | None = None) -> dict[str, Any]:
    """
    Auto mode: scan when paper is already in the slot; NO_DOCUMENT if empty.
    Used by the background auto-watch thread (AmbirScan Auto Scan behaviour).
    """
    dpi = resolution if resolution is not None else int(
        os.environ.get("FDN_NSCAN690GT_RESOLUTION", "300").strip() or "300"
    )
    with _scanner_lock:
        return _run_scan_cycle(wait_for_paper=False, wait_timeout_s=0, resolution=dpi)


def scan_manual_safe() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"type": "ERROR", "message": "nScan 690gt SDK is only supported on Windows."}
    try:
        return scan_manual_blocking()
    except AmbirSDKError as exc:
        code = getattr(exc, "code", None)
        msg = str(exc)
        if code == _ERR_CODE_TIMEOUT or "no card" in msg.lower():
            return {"type": "NO_DOCUMENT", "message": msg}
        return {"type": "ERROR", "message": msg}
    except OSError as exc:
        return {"type": "ERROR", "message": str(exc)}


def scan_auto_safe() -> dict[str, Any]:
    if sys.platform != "win32":
        return {"type": "ERROR", "message": "nScan 690gt SDK is only supported on Windows."}
    try:
        return scan_auto_once()
    except AmbirSDKError as exc:
        code = getattr(exc, "code", None)
        msg = str(exc)
        if code == _ERR_CODE_TIMEOUT or "no card" in msg.lower():
            return {"type": "NO_DOCUMENT", "message": msg}
        return {"type": "ERROR", "message": msg}
    except OSError as exc:
        return {"type": "ERROR", "message": str(exc)}


def peek_paper_status() -> int | None:
    """
    Quick paper check without scanning. Returns SI_PS_PAPER_IN / SI_PS_PAPER_OUT,
    or None when the DLL is unavailable.

    Opens a short-lived SI_* session under the shared scanner lock so this never
    races with Manual Scan ID / duplex capture.
    """
    dll_path = _resolve_dll_path()
    if dll_path is None:
        return None
    with _scanner_lock:
        try:
            dll = _load_dll(dll_path)
            _bind(dll)
            _open_interface(dll)
            try:
                status = _paper_status(dll)
                logger.debug("%s peek paper_status=%s", _LOG_TAG, status)
                return status
            finally:
                _close_interface(dll)
        except AmbirSDKError as exc:
            logger.warning("%s peek_paper_status failed: %s", _LOG_TAG, exc)
            return None
        except OSError as exc:
            logger.warning("%s peek_paper_status OS error: %s", _LOG_TAG, exc)
            return None


def wait_for_card_insert(*, poll_s: float = 0.35, stop_event: threading.Event | None = None) -> bool:
    """
    Keep NS690gt.DLL open and poll until paper goes OUT→IN (AmbirScan Auto behaviour).

    Returns True when a rising edge is seen, False if stop_event is set.
    Caller should then call scan_auto_once() (which re-opens under the same lock).
    """
    dll_path = _resolve_dll_path()
    if dll_path is None:
        logger.warning("%s wait_for_card_insert: NS690gt.DLL not found", _LOG_TAG)
        return False

    last = SI_PS_PAPER_OUT
    # Hold the lock for the whole wait so Manual cannot interleave mid-poll.
    # Release periodically so Manual Scan ID can still run if staff switches mode.
    while stop_event is None or not stop_event.is_set():
        with _scanner_lock:
            try:
                dll = _load_dll(dll_path)
                _bind(dll)
                _open_interface(dll)
                try:
                    # Stay open for several polls — closer to MiniScan auto loop.
                    for _ in range(20):
                        if stop_event is not None and stop_event.is_set():
                            return False
                        paper_now = _paper_status(dll)
                        inserted = last == SI_PS_PAPER_OUT and paper_now == SI_PS_PAPER_IN
                        last = paper_now
                        if inserted:
                            logger.info("%s card insert edge detected (paper IN)", _LOG_TAG)
                            return True
                        time.sleep(poll_s)
                finally:
                    _close_interface(dll)
            except AmbirSDKError as exc:
                logger.warning("%s wait_for_card_insert: %s — retry in 2s", _LOG_TAG, exc)
                time.sleep(2.0)
            except OSError as exc:
                logger.warning("%s wait_for_card_insert OS error: %s", _LOG_TAG, exc)
                time.sleep(2.0)
        # Brief unlock window for Manual scan between open sessions
        time.sleep(0.05)
    return False


def probe_nscan690gt_sdk() -> dict[str, Any]:
    """
    Lightweight probe for DEVICE_STATUS.

    Never blocks a live scan: if the scanner lock is held, return the last good
    result (or DLL-present) without calling SI_OpenInterface.
    """
    global _probe_cache, _probe_cache_at

    if sys.platform != "win32":
        return {"available": False, "hw_ok": False, "dll_path": "", "detail": "Windows only."}
    dll_path = _resolve_dll_path()
    if dll_path is None:
        return {
            "available": False,
            "hw_ok": False,
            "dll_path": "",
            "detail": "NS690gt.DLL not found. Install nScan 690gt driver.",
        }

    now = time.monotonic()
    if _probe_cache is not None and (now - _probe_cache_at) < _PROBE_CACHE_S:
        return dict(_probe_cache)

    got = _scanner_lock.acquire(blocking=False)
    if not got:
        if _probe_cache is not None:
            out = dict(_probe_cache)
            out["detail"] = "nScan 690gt busy (scan/poll in progress)."
            return out
        return {
            "available": True,
            "hw_ok": True,
            "dll_path": str(dll_path),
            "detail": "NS690gt.DLL present; hardware busy (scan in progress).",
        }

    try:
        try:
            dll = _load_dll(dll_path)
            _bind(dll)
        except AmbirSDKError as exc:
            result = {"available": False, "hw_ok": False, "dll_path": str(dll_path), "detail": str(exc)}
            _probe_cache = result
            _probe_cache_at = time.monotonic()
            return result

        hw_ok = False
        note = "DLL loaded; hardware status unknown."
        try:
            _open_interface(dll)
            hw_ok = True
            note = f'nScan 690gt responded via SI_OpenInterface("{_OPEN_NAME}").'
            _close_interface(dll)
        except AmbirSDKError as exc:
            note = f"DLL loaded; SI_OpenInterface failed: {exc}"
            try:
                _close_interface(dll)
            except Exception:  # noqa: BLE001
                pass

        result = {
            "available": True,
            "hw_ok": hw_ok,
            "dll_path": str(dll_path),
            "detail": note,
        }
        _probe_cache = result
        _probe_cache_at = time.monotonic()
        return result
    finally:
        _scanner_lock.release()
