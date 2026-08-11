"""
FrontDesk Nexus — Chrome Native Messaging host entry point.

- **Default** (``python main.py``): terminal demo — loads ``samples/id_card.png``,
  base64-encodes it, logs steps + base64 to stderr, then exits.

- **Chrome Native Messaging**: start with ``--native-messaging``. There is no visible
  console. While in this mode, logs are **always** appended to
  ``logs/native-host.log`` under this project (unless ``FDN_NO_LOG_FILE=1``).
  Override path with ``FDN_LOG_FILE``. Tail the file in PowerShell:
  ``Get-Content .\\logs\\native-host.log -Wait``

- **Thales auto-watch** (optional): set ``FDN_THALES_AUTO_WATCH=1`` so a background
  thread waits for documents and **pushes** ``AUTO_SCAN_RESULT`` to the extension when
  a read completes—no ``SCAN_DOCUMENT_SDK`` message required. The extension must still
  **open the native port** (e.g. on load) and listen on ``port.onMessage``.

- **Two-pass DL scan** (optional): set ``FDN_TWAIN_AUTO_WATCH=1`` for US driver's
  licences that require two physical scans (front then back). First scan pushes
  ``SCAN_FRONT_RESULT`` with the front image immediately; after the clerk flips the
  card the second scan pushes ``AUTO_SCAN_RESULT`` with both images and AAMVA/MRZ
  data—no Google Vision used. Mutually exclusive with ``FDN_THALES_AUTO_WATCH``
  (Thales takes priority if both are set).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

import ctypes
import ctypes.wintypes

import messaging
import rfid_encoder
import scanner
from rfid_encoder import RfidEncoderError
from scanner import ScannerError

logger = logging.getLogger(__name__)

_HOST_ROOT = Path(__file__).resolve().parent

# Native Messaging stdout must be written by one thread at a time (auto-watch + reply path).
_stdout_lock = threading.Lock()


def _write_message_safe(stdout: BinaryIO, message: dict[str, Any]) -> None:
    with _stdout_lock:
        messaging.write_message(stdout, message)

# Small text file Chrome-launched runs touch immediately — proves the OS started this
# process even if logging or stdin fails later. Open: logs/host-launched.txt
_LAUNCH_SENTINEL = _HOST_ROOT / "logs" / "host-launched.txt"


def _write_launch_sentinel() -> None:
    """Write a short proof-of-life file so you can tell Chrome started this host."""
    try:
        _LAUNCH_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f"native_host_started_utc={datetime.now(timezone.utc).isoformat()}\n"
            f"pid={os.getpid()}\n"
            f"argv={sys.argv}\n"
        )
        _LAUNCH_SENTINEL.write_text(body, encoding="utf-8")
    except OSError:
        pass


_LAST_INBOUND = _HOST_ROOT / "logs" / "last-inbound.json"


def _write_last_inbound(msg: dict[str, Any]) -> None:
    """Overwrite with the last JSON object read from the extension (easy to open in IDE)."""
    try:
        _LAST_INBOUND.parent.mkdir(parents=True, exist_ok=True)
        _LAST_INBOUND.write_text(
            json.dumps(msg, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
# RFID encoder credentials — set these env vars or pass values from your settings config.
# FDN_RFID_HOTEL_ID  : 4-digit hotel ID  (e.g. "0042")
# FDN_RFID_AUTH_CODE : 8-digit authorization code provided by Elox/hotel setup
RFID_HOTEL_ID = os.environ.get("FDN_RFID_HOTEL_ID", "0000").strip()
RFID_AUTH_CODE = os.environ.get("FDN_RFID_AUTH_CODE", "00000000").strip()

_SAMPLE_ENV = os.environ.get("FDN_SAMPLE_ID_PATH", "").strip()
if _SAMPLE_ENV:
    _p = Path(_SAMPLE_ENV)
    SAMPLE_ID_PATH = _p if _p.is_absolute() else (_HOST_ROOT / _p)
else:
    SAMPLE_ID_PATH = None


def _error(message: str) -> dict[str, Any]:
    return {"type": "ERROR", "message": message}


def _find_window_by_exe(exe_name: str) -> int:
    """Return HWND of the first visible main window for the given exe name, or 0."""
    target = exe_name.lower()
    if not target.endswith(".exe"):
        target += ".exe"

    found = [0]
    _EnumWindowsProc = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM
    )

    def _cb(hwnd: int, _: int) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):  # type: ignore[attr-defined]
            return True
        pid = ctypes.wintypes.DWORD(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))  # type: ignore[attr-defined]
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # type: ignore[attr-defined]
        if h:
            buf = ctypes.create_unicode_buffer(512)
            sz = ctypes.wintypes.DWORD(512)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(sz)):  # type: ignore[attr-defined]
                path = buf.value.lower()
                if path.endswith("\\" + target) or path == target:
                    found[0] = hwnd
                    ctypes.windll.kernel32.CloseHandle(h)  # type: ignore[attr-defined]
                    return False
            ctypes.windll.kernel32.CloseHandle(h)  # type: ignore[attr-defined]
        return True

    cb = _EnumWindowsProc(_cb)
    ctypes.windll.user32.EnumWindows(cb, 0)  # type: ignore[attr-defined]
    return found[0]


def _handle_window_control(payload: dict[str, Any]) -> dict[str, Any]:
    action = payload.get("action", "")
    exe_name = str(payload.get("processName", "pdocSigner.exe"))
    SW_MINIMIZE = 6
    SW_RESTORE = 9
    try:
        hwnd = _find_window_by_exe(exe_name)
        if not hwnd:
            return {"type": "WINDOW_CONTROL_RESULT", "ok": True, "reason": "not_found"}
        cmd = SW_MINIMIZE if action == "minimize" else SW_RESTORE
        ctypes.windll.user32.ShowWindow(hwnd, cmd)  # type: ignore[attr-defined]
        logger.info("[host] WINDOW_CONTROL action=%r exe=%r hwnd=%r", action, exe_name, hwnd)
        return {"type": "WINDOW_CONTROL_RESULT", "ok": True}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[host] WINDOW_CONTROL error: %s", exc)
        return {"type": "WINDOW_CONTROL_RESULT", "ok": False, "reason": str(exc)}


def _format_inbound_message(msg: dict[str, Any]) -> str:
    """Pretty JSON for logs (stderr); extension payload is usually small."""
    try:
        return json.dumps(msg, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return repr(msg)


def _handle_scan_id(payload: dict[str, Any]) -> dict[str, Any]:
    logger.info("[host] executing SCAN_ID handler (payload keys: %s)", sorted(payload.keys()))
    return scanner.scan_id(simulation_mode=False, sample_path=SAMPLE_ID_PATH)


def _handle_scan_document_sdk(_payload: dict[str, Any]) -> dict[str, Any]:
    """Thales QS2000 / MMMReader SDK — structured MRZ / AAMVA (no Google Vision)."""
    logger.info("[host] executing SCAN_DOCUMENT_SDK handler (Thales MMMReader)")
    return scanner.scan_document_thales_sdk()


def _handle_rfid(payload: dict[str, Any]) -> dict[str, Any]:
    """Route any RFID_* command to rfid_encoder."""
    return rfid_encoder.handle_rfid_command(payload, RFID_HOTEL_ID, RFID_AUTH_CODE)


def _not_implemented(command: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def _handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return _error(f'Command "{command}" is not implemented yet')

    return _handler


CommandHandler = Callable[[dict[str, Any]], dict[str, Any]]

_COMMAND_HANDLERS: dict[str, CommandHandler] = {
    "SCAN_ID": _handle_scan_id,
    "SCAN_DOCUMENT_SDK": _handle_scan_document_sdk,
    "DISPENSE_CASH": _not_implemented("DISPENSE_CASH"),
    "WINDOW_CONTROL": _handle_window_control,
    # RFID key card encoder — all RFID_* commands share one handler
    "RFID_HANDSHAKE": _handle_rfid,
    "RFID_MAKE_KEY": _handle_rfid,
    "RFID_READ_CARD": _handle_rfid,
    "RFID_MAKE_LOST_KEY": _handle_rfid,
}


def dispatch(message: dict[str, Any]) -> dict[str, Any]:
    """Route a validated JSON object to the proper handler."""
    msg_type = message.get("type")
    if not isinstance(msg_type, str) or not msg_type:
        return _error('Missing or invalid "type" field')

    handler = _COMMAND_HANDLERS.get(msg_type)
    if handler is None:
        return _error(f'Unknown command type: {msg_type!r}')

    _is_rfid_ping = msg_type == "RFID_HANDSHAKE"
    if not _is_rfid_ping:
        logger.info(
            "[host] dispatch - command=%r -> handler=%s",
            msg_type,
            getattr(handler, "__name__", type(handler).__name__),
        )

    try:
        response = handler(message)
    except (ScannerError, RfidEncoderError) as exc:
        logger.warning("Module error: %s", exc)
        response = _error(str(exc))
    except Exception as exc:  # noqa: BLE001 — boundary: never crash the host loop
        logger.exception("Unhandled error while handling %s", msg_type)
        response = _error(f"Internal error: {exc}")

    # Forward requestId so the extension can resolve pending request-response promises.
    req_id = message.get("requestId")
    if req_id and isinstance(req_id, str):
        response = {**response, "requestId": req_id}

    return response


def _resolve_log_file_path(*, native_host: bool) -> Path | None:
    """File used for host logs. Chrome has no terminal; file is how you inspect output."""
    if os.environ.get("FDN_NO_LOG_FILE", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    explicit = os.environ.get("FDN_LOG_FILE", "").strip()
    if explicit:
        return Path(explicit)
    if native_host:
        return _HOST_ROOT / "logs" / "native-host.log"
    return None


def _thales_auto_watch_enabled() -> bool:
    v = os.environ.get("FDN_THALES_AUTO_WATCH", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _twain_auto_watch_enabled() -> bool:
    v = os.environ.get("FDN_TWAIN_AUTO_WATCH", "").strip().lower()
    return v in ("1", "true", "yes", "on")


_REMOVAL_FALLBACK_S = 1.5  # sleep seconds when WaitForDocumentRemoval is unavailable
_MAX_BACK_ERRORS = 3      # reset to front pass after this many consecutive back-pass SDK errors


def _thales_auto_watch_thread(stdout: BinaryIO, stop: threading.Event) -> None:
    """
    Background loop: wait for a document on the Thales reader, then push AUTO_SCAN_RESULT
    to the extension (no inbound message required). Requires Chrome to have opened the
    native port so this process is running.

    After each successful scan the loop waits for the document to be physically removed
    before calling WaitForDocument again. This prevents the same ID from being re-scanned
    continuously if the clerk leaves it on the reader.
    """
    logger.info("[auto-watch] Thales background loop started")
    backoff = 1.0
    while not stop.is_set():
        try:
            from scanner_thales_sdk import read_document_safe, wait_for_document_removal_safe

            out = read_document_safe()
            if out.get("type") == "ERROR":
                logger.warning("[auto-watch] %s", out.get("message"))
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 1.5, 30.0)
                continue
            backoff = 1.0
            payload = scanner.auto_scan_document_push_payload(out)
            if payload.get("type") == "ERROR":
                logger.error("[auto-watch] %s", payload.get("message"))
                continue
            logger.info(
                "[auto-watch] pushing AUTO_SCAN_RESULT document_number=%r",
                (payload.get("document_data") or {}).get("document_number"),
            )
            _write_message_safe(stdout, payload)

            # Wait for the document to be removed before starting the next scan cycle.
            # Prevents re-scanning the same ID if it stays on the reader.
            logger.info("[auto-watch] waiting for document removal before next scan...")
            removal = wait_for_document_removal_safe()
            if removal.get("type") == "REMOVAL_ERROR":
                logger.warning(
                    "[auto-watch] WaitForDocumentRemoval unavailable (%s) — "
                    "using %.0fs fallback delay",
                    removal.get("message", ""),
                    _REMOVAL_FALLBACK_S,
                )
                time.sleep(_REMOVAL_FALLBACK_S)
            else:
                logger.info("[auto-watch] document removed — ready for next scan")
        except Exception:  # noqa: BLE001
            logger.exception("[auto-watch] unexpected error")
            time.sleep(2.0)
    logger.info("[auto-watch] Thales background loop stopped")


def _scan_has_barcode(out: dict) -> bool:
    """True when the scan contains AAMVA/PDF417 data — present only on the back of a US DL/ID."""
    return bool(out.get("pdf417_raw") or out.get("aamva_raw"))


def _twain_auto_watch_thread(stdout: BinaryIO, stop: threading.Event) -> None:
    """
    Two-pass DL scan background loop (no Google Vision).

    Pass 1 — front: capture front image and push SCAN_FRONT_RESULT immediately.
    Pass 2 — back: capture back image + AAMVA/MRZ data, push AUTO_SCAN_RESULT with both.

    Side detection: if the back of a US DL/ID is placed first (barcode detected on scan 1),
    the back image is cached and AUTO_SCAN_RESULT is pushed once the front arrives on scan 2.

    Foreign IDs (no PDF417/AAMVA barcode on back): treated as front-first by scan order.
    If the SDK returns consecutive errors on the back pass, state resets to front after
    _MAX_BACK_ERRORS so the clerk can retry without restarting the host.

    State resets to ``front`` after each full cycle or after _MAX_BACK_ERRORS consecutive errors.
    """
    logger.info("[two-pass] Two-pass DL scan background loop started")
    backoff = 1.0
    state = "front"
    cached_front_b64: str | None = None
    cached_back_out: dict | None = None  # full SDK result when back scanned first
    back_error_count = 0

    while not stop.is_set():
        try:
            from scanner_thales_sdk import read_document_safe, wait_for_document_removal_safe

            out = read_document_safe()
            if out.get("type") == "ERROR":
                logger.warning("[two-pass] SDK error (state=%s): %s", state, out.get("message"))
                if state == "back":
                    back_error_count += 1
                    if back_error_count >= _MAX_BACK_ERRORS:
                        logger.warning(
                            "[two-pass] %d consecutive back-pass errors — resetting to front",
                            back_error_count,
                        )
                        state = "front"
                        cached_front_b64 = None
                        cached_back_out = None
                        back_error_count = 0
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 1.5, 30.0)
                continue
            backoff = 1.0
            back_error_count = 0

            img_b64 = out.get("visible_image_base64") or out.get("image_base64") or ""
            is_back = _scan_has_barcode(out)

            if state == "front":
                if not img_b64:
                    logger.warning("[two-pass] first scan returned no image — skipping")
                elif is_back:
                    # Back side placed first — cache SDK result for AAMVA parsing, wait for front
                    cached_back_out = out
                    logger.info(
                        "[two-pass] back side detected on first scan (chars=%d) — waiting for front",
                        len(img_b64),
                    )
                    state = "back"
                else:
                    # Normal order: front side first
                    cached_front_b64 = img_b64
                    cached_back_out = None
                    _write_message_safe(stdout, {
                        "type": "SCAN_FRONT_RESULT",
                        "image_front_base64": img_b64,
                    })
                    logger.info(
                        "[two-pass] SCAN_FRONT_RESULT pushed (chars=%d) — flip card for back",
                        len(img_b64),
                    )
                    state = "back"

            else:  # state == "back"
                if cached_back_out is not None:
                    # Back-first flow: this second scan should be the front
                    if is_back:
                        # Same side scanned again — update cached back, keep waiting for front
                        cached_back_out = out
                        logger.warning("[two-pass] back side scanned again — recaching, still waiting for front")
                    else:
                        # Front arrived — assemble both sides and push
                        front_b64 = img_b64
                        back_b64 = cached_back_out.get("visible_image_base64") or cached_back_out.get("image_base64") or ""
                        payload = scanner.auto_scan_document_push_payload(cached_back_out)
                        if payload.get("type") == "ERROR":
                            payload = {
                                "type": "AUTO_SCAN_RESULT",
                                "success": True,
                                "source": "two_pass_twain",
                                "image_front_base64": front_b64,
                                "image_back_base64": back_b64,
                            }
                            logger.warning("[two-pass] back-first: no barcode data parsed — pushing images only")
                        else:
                            payload["image_front_base64"] = front_b64
                            payload["image_back_base64"] = back_b64
                            payload.pop("image_base64", None)
                            logger.info(
                                "[two-pass] AUTO_SCAN_RESULT pushed (back-first) — document_number=%r",
                                (payload.get("document_data") or {}).get("document_number"),
                            )
                        _write_message_safe(stdout, payload)
                        cached_front_b64 = None
                        cached_back_out = None
                        state = "front"
                else:
                    # Normal flow: this second scan is the back
                    back_b64 = img_b64
                    payload = scanner.auto_scan_document_push_payload(out)
                    if payload.get("type") == "ERROR":
                        payload = {
                            "type": "AUTO_SCAN_RESULT",
                            "success": True,
                            "source": "two_pass_twain",
                            "image_front_base64": cached_front_b64 or "",
                            "image_back_base64": back_b64,
                        }
                        logger.warning("[two-pass] back pass: no barcode data parsed — pushing images only")
                    else:
                        payload["image_front_base64"] = cached_front_b64 or ""
                        payload["image_back_base64"] = back_b64
                        payload.pop("image_base64", None)
                        logger.info(
                            "[two-pass] AUTO_SCAN_RESULT pushed — document_number=%r",
                            (payload.get("document_data") or {}).get("document_number"),
                        )
                    _write_message_safe(stdout, payload)
                    cached_front_b64 = None
                    cached_back_out = None
                    state = "front"

            # Wait for removal before the next scan
            logger.info("[two-pass] waiting for document removal (next state: %s)...", state)
            removal = wait_for_document_removal_safe()
            if removal.get("type") == "REMOVAL_ERROR":
                logger.warning(
                    "[two-pass] WaitForDocumentRemoval unavailable (%s) — %.1fs fallback",
                    removal.get("message", ""),
                    _REMOVAL_FALLBACK_S,
                )
                time.sleep(_REMOVAL_FALLBACK_S)
            else:
                logger.info("[two-pass] document removed — ready (state=%s)", state)

        except Exception:  # noqa: BLE001
            logger.exception("[two-pass] unexpected error")
            time.sleep(2.0)

    logger.info("[two-pass] Two-pass DL scan background loop stopped")


def _configure_logging(*, native_host: bool) -> None:
    level = os.environ.get("FDN_LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr, force=True)

    path = _resolve_log_file_path(native_host=native_host)
    if path is None:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8", mode="a")
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Could not open log file %s (%s); stderr only",
            path,
            exc,
        )
        return

    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(fmt))
    root = logging.getLogger()
    root.addHandler(fh)
    lg = logging.getLogger(__name__)
    lg.info("Logging to file: %s (pid=%s)", path.resolve(), os.getpid())
    fh.flush()


def run_terminal_demo() -> int:
    """
    Dev convenience: ``python main.py`` in a normal console loads ``samples/id_card.png``
    (or ``FDN_SAMPLE_ID_PATH``), encodes to base64, logs a short preview, then exits.
    """
    _configure_logging(native_host=False)
    logger.info("Running in terminal demo mode (not waiting for Chrome messages).")
    try:
        result = scanner.scan_id(simulation_mode=True, sample_path=SAMPLE_ID_PATH)
    except ScannerError as exc:
        logger.error("[demo] scan failed: %s", exc)
        return 1

    b64 = result.get("image_base64", "")
    path = SAMPLE_ID_PATH if SAMPLE_ID_PATH is not None else scanner.DEFAULT_SAMPLE_PATH
    logger.info("[demo] sample image path: %s", path)
    logger.info("[demo] image_base64 length (characters): %d", len(b64))
    logger.info("[demo] ocr_data (camelCase): %s", result.get("ocr_data"))
    logger.info("[demo] terminal run finished successfully.")
    return 0


def run() -> int:
    _write_launch_sentinel()
    _configure_logging(native_host=True)
    logger.info("FrontDesk Nexus Native Messaging host starting")
    logger.info("Proof file written: %s (if this exists, Chrome started Python)", _LAUNCH_SENTINEL)

    stdin, stdout = messaging.stdin_stdout_streams()

    stop_watch = threading.Event()
    watch_thread: threading.Thread | None = None
    if _thales_auto_watch_enabled():
        watch_thread = threading.Thread(
            target=_thales_auto_watch_thread,
            args=(stdout, stop_watch),
            name="thales-auto-watch",
            daemon=True,
        )
        watch_thread.start()
        logger.info(
            "FDN_THALES_AUTO_WATCH is on — each successful Thales read pushes "
            "AUTO_SCAN_RESULT to the extension (listen on native port onMessage)."
        )
    elif _twain_auto_watch_enabled():
        watch_thread = threading.Thread(
            target=_twain_auto_watch_thread,
            args=(stdout, stop_watch),
            name="two-pass-watch",
            daemon=True,
        )
        watch_thread.start()
        logger.info(
            "FDN_TWAIN_AUTO_WATCH is on — two-pass DL scan: "
            "front image pushed on first scan, back image + AAMVA data pushed after card flip."
        )

    try:
        while True:
            try:
                raw = messaging.read_message(stdin)
            except ValueError as exc:
                logger.warning("Protocol/parse error: %s", exc)
                try:
                    _write_message_safe(stdout, _error(str(exc)))
                except OSError:
                    logger.exception("Failed to write error response after parse failure")
                return 1
            except OSError as exc:
                logger.error("I/O error while reading: %s", exc)
                return 1

            if raw is None:
                logger.info("[host] stdin closed - exiting native messaging loop")
                return 0

            if raw.get("type") != "RFID_HANDSHAKE":
                logger.info("[host] received message from extension (decoded JSON):\n%s", _format_inbound_message(raw))
            _write_last_inbound(raw)

            try:
                response = dispatch(raw)
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected failure in dispatch")
                response = _error("Unexpected internal error")

            rtype = response.get("type")
            if rtype != "RFID_HANDSHAKE_RESULT":
                logger.info("[host] sending response to extension: type=%r", rtype)
            if rtype == "SCAN_RESULT":
                logger.info(
                    "[host] SCAN_RESULT summary: image_base64_chars=%d ocr_data=%s",
                    len(response.get("image_base64") or ""),
                    response.get("ocr_data"),
                )
            if rtype == "SDK_DOCUMENT_RESULT":
                logger.info(
                    "[host] SDK_DOCUMENT_RESULT document_number=%r engine=%s",
                    (response.get("document_data") or {}).get("document_number"),
                    response.get("sdk_engine"),
                )
            try:
                _write_message_safe(stdout, response)
            except OSError as exc:
                logger.error("Failed to write response: %s", exc)
                return 1
    finally:
        stop_watch.set()


def _wants_native_messaging() -> bool:
    if "--native-messaging" in sys.argv:
        return True
    v = os.environ.get("FDN_NATIVE_MESSAGING", "").strip().lower()
    return v in ("1", "true", "yes", "on")


if __name__ == "__main__":
    raise SystemExit(run() if _wants_native_messaging() else run_terminal_demo())
