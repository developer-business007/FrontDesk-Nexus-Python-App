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

- **Two-sided ID** (Thales): send ``{"type": "SCAN_DOCUMENT_SDK", "two_sided": true}`` twice
  (once per physical side, any order). First response: ``SDK_DOCUMENT_SIDE_RESULT``;
  second: ``SDK_DOCUMENT_RESULT`` with ``image_front_base64``, ``image_back_base64``, merged
  ``document_data`` (``front_image_base64`` / ``back_image_base64`` are aliases). Single-read
  ``AUTO_SCAN_RESULT`` uses the same ``image_*`` keys with one slot filled from side inference.
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


def _format_inbound_message(msg: dict[str, Any]) -> str:
    """Pretty JSON for logs (stderr); extension payload is usually small."""
    try:
        return json.dumps(msg, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return repr(msg)


def _handle_scan_id(payload: dict[str, Any]) -> dict[str, Any]:
    logger.info("[host] executing SCAN_ID handler (payload keys: %s)", sorted(payload.keys()))
    return scanner.scan_id(simulation_mode=False, sample_path=SAMPLE_ID_PATH)


def _handle_scan_document_sdk(payload: dict[str, Any]) -> dict[str, Any]:
    """Thales QS2000 / MMMReader SDK — structured MRZ / AAMVA; optional two-sided capture."""
    logger.info(
        "[host] executing SCAN_DOCUMENT_SDK handler (Thales MMMReader) two_sided=%s",
        bool(payload.get("two_sided")),
    )
    return scanner.scan_document_thales_sdk(payload)


def _handle_scan_document_ambir(payload: dict[str, Any]) -> dict[str, Any]:
    """AMBIR DocketPORT — feed-scanner image capture + Google Vision OCR."""
    logger.info("[host] executing SCAN_DOCUMENT_AMBIR handler (AMBIR DocketPORT)")
    return scanner.scan_document_ambir_sdk(payload)


def _handle_scan_document_nscan690gt(payload: dict[str, Any]) -> dict[str, Any]:
    """AMBIR nScan 690gt — NS690gt.DLL duplex scan + PDF417/OCR."""
    logger.info(
        "[host] ========== SCAN_DOCUMENT_NSCAN690GT ========== "
        "(nScan 690gt via NS690gt.DLL SI_* API, manual mode)"
    )
    from scanner_nscan690gt import scan_document
    return scan_document(payload)


_nscan690gt_stdout: BinaryIO | None = None
_nscan690gt_watch_stop = threading.Event()
_nscan690gt_watch_thread: threading.Thread | None = None
_nscan690gt_mode = "manual"

_thales_watch_stop = threading.Event()
_thales_watch_thread: threading.Thread | None = None
_active_scanner: str = "twain"


def _start_nscan690gt_auto_watch() -> None:
    """Start (or restart) the nScan 690gt insert-card auto-watch loop."""
    global _nscan690gt_watch_stop, _nscan690gt_watch_thread

    if _nscan690gt_stdout is None:
        logger.warning("[host] nScan690gt auto-watch: stdout not ready")
        return

    _nscan690gt_watch_stop.set()
    if _nscan690gt_watch_thread is not None and _nscan690gt_watch_thread.is_alive():
        _nscan690gt_watch_thread.join(timeout=3.0)

    _nscan690gt_watch_stop = threading.Event()
    _nscan690gt_watch_thread = threading.Thread(
        target=_nscan690gt_auto_watch_thread,
        args=(_nscan690gt_stdout, _nscan690gt_watch_stop),
        name="nscan690gt-auto-watch",
        daemon=True,
    )
    _nscan690gt_watch_thread.start()
    logger.info("[host] nScan690gt auto-watch started (extension Auto mode)")


def _stop_nscan690gt_auto_watch() -> None:
    _nscan690gt_watch_stop.set()
    logger.info("[host] nScan690gt auto-watch stop signaled (extension Manual mode)")


def _start_thales_auto_watch() -> None:
    """Start (or restart) the Thales QS2000 insert-document auto-watch loop."""
    global _thales_watch_stop, _thales_watch_thread

    if _nscan690gt_stdout is None:
        logger.warning("[host] Thales auto-watch: stdout not ready")
        return

    _thales_watch_stop.set()
    if _thales_watch_thread is not None and _thales_watch_thread.is_alive():
        _thales_watch_thread.join(timeout=3.0)

    _thales_watch_stop = threading.Event()
    _thales_watch_thread = threading.Thread(
        target=_thales_auto_watch_thread,
        args=(_nscan690gt_stdout, _thales_watch_stop),
        name="thales-auto-watch",
        daemon=True,
    )
    _thales_watch_thread.start()
    logger.info("[host] Thales auto-watch started (QS2000 selected)")


def _stop_thales_auto_watch() -> None:
    _thales_watch_stop.set()
    logger.info("[host] Thales auto-watch stop signaled")


def _handle_set_active_scanner(payload: dict[str, Any]) -> dict[str, Any]:
    """Extension dropdown: Thales QS2000 vs nScan 690gt — only one auto-watch at a time."""
    global _active_scanner

    scanner = payload.get("scanner")
    if scanner not in ("thales", "twain"):
        return _error('scanner must be "thales" or "twain"')

    logger.info("[host] SET_ACTIVE_SCANNER scanner=%r (was %r)", scanner, _active_scanner)
    _active_scanner = scanner
    if scanner == "thales":
        _stop_nscan690gt_auto_watch()
        _start_thales_auto_watch()
    else:
        _stop_thales_auto_watch()
        if _nscan690gt_mode == "auto":
            _start_nscan690gt_auto_watch()
    return {"type": "ACTIVE_SCANNER", "success": True, "scanner": scanner}


def _handle_set_nscan690gt_scan_mode(payload: dict[str, Any]) -> dict[str, Any]:
    """Extension Auto/Manual toggle for Ambir nScan 690gt."""
    global _nscan690gt_mode

    mode = payload.get("mode")
    if mode not in ("auto", "manual"):
        return _error('mode must be "auto" or "manual"')

    logger.info("[host] SET_NSCAN690GT_SCAN_MODE mode=%r (was %r) active_scanner=%r", mode, _nscan690gt_mode, _active_scanner)
    _nscan690gt_mode = mode
    if _active_scanner != "twain":
        _stop_nscan690gt_auto_watch()
        return {"type": "NSCAN690GT_SCAN_MODE", "success": True, "mode": mode, "ignored": "thales_selected"}
    if mode == "auto":
        _start_nscan690gt_auto_watch()
    else:
        _stop_nscan690gt_auto_watch()
    return {"type": "NSCAN690GT_SCAN_MODE", "success": True, "mode": mode}


def _handle_scan_document_auto(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Auto-routing: try Thales first (richer structured data), fall back to AMBIR.

    The extension can send SCAN_DOCUMENT_AUTO when it does not know which
    scanner is installed; the host picks whichever responds first.
    Pass two_sided=true to forward that flag to the Thales path.
    """
    logger.info("[host] executing SCAN_DOCUMENT_AUTO handler")

    # ── Try Thales ────────────────────────────────────────────────────────────
    try:
        from scanner_thales_sdk import probe_thales_sdk
        thales_probe = probe_thales_sdk()
    except Exception as exc:  # noqa: BLE001
        thales_probe = {"dll_load_ok": False}
        logger.debug("[auto] Thales probe error: %s", exc)

    if thales_probe.get("dll_load_ok"):
        logger.info("[auto] Thales DLL available — attempting Thales scan")
        try:
            return scanner.scan_document_thales_sdk(payload)
        except scanner.ScannerError as exc:
            logger.warning("[auto] Thales scan failed (%s) — falling back to AMBIR", exc)

    # ── Try AMBIR ─────────────────────────────────────────────────────────────
    try:
        from scanner_ambir_sdk import probe_ambir_sdk
        ambir_probe = probe_ambir_sdk()
    except Exception as exc:  # noqa: BLE001
        ambir_probe = {"available": False}
        logger.debug("[auto] AMBIR probe error: %s", exc)

    if ambir_probe.get("available"):
        logger.info("[auto] AMBIR DLL available — attempting AMBIR scan")
        return scanner.scan_document_ambir_sdk(payload)

    raise scanner.ScannerError(
        "SCAN_DOCUMENT_AUTO: no scanner found. "
        "Neither Thales (check config/thales_paths.ini) nor AMBIR "
        "(check config/ambir_paths.ini) is configured and available."
    )


def _handle_device_status(_payload: dict[str, Any]) -> dict[str, Any]:
    """TWAIN source list + Thales / AMBIR / nScan690gt probes (see scanner.get_device_status)."""
    logger.info("[host] executing DEVICE_STATUS")
    status = scanner.get_device_status()
    logger.info(
        "[host] DEVICE_STATUS nscan690gt available=%s hw_ok=%s dll=%r detail=%s",
        status.get("nscan690gt_available"),
        status.get("nscan690gt_hw_ok"),
        status.get("nscan690gt_dll_path"),
        status.get("nscan690gt_detail"),
    )
    return status


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
    # Scanner commands
    "SCAN_DOCUMENT_SDK":        _handle_scan_document_sdk,        # Thales QS2000 (explicit)
    "SCAN_DOCUMENT_AMBIR":      _handle_scan_document_ambir,      # AMBIR DocketPORT (explicit)
    "SCAN_DOCUMENT_NSCAN690GT": _handle_scan_document_nscan690gt, # AMBIR nScan 690gt SDK
    "SET_NSCAN690GT_SCAN_MODE": _handle_set_nscan690gt_scan_mode, # Ambir Auto / Manual
    "SET_ACTIVE_SCANNER":       _handle_set_active_scanner,       # QS2000 vs nScan 690gt
    "SCAN_DOCUMENT_AUTO":       _handle_scan_document_auto,       # auto-detect: Thales → AMBIR
    "DEVICE_STATUS": _handle_device_status,
    "DISPENSE_CASH": _not_implemented("DISPENSE_CASH"),
    # RFID key card encoder — all RFID_* commands share one handler
    "RFID_HANDSHAKE":   _handle_rfid,
    "RFID_MAKE_KEY":    _handle_rfid,
    "RFID_READ_CARD":   _handle_rfid,
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


def _nscan690gt_auto_watch_enabled() -> bool:
    v = os.environ.get("FDN_NSCAN690GT_AUTO_WATCH", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _attach_id_side_images(msg: dict, *, front: str, back: str) -> dict:
    from scanner import normalize_front_image_b64

    out = dict(msg)
    front_b64 = normalize_front_image_b64((front or "").strip())
    back_b64 = (back or "").strip()
    out["image_front_base64"] = front_b64
    out["image_back_base64"] = back_b64
    out["front_image_base64"] = front_b64
    out["back_image_base64"] = back_b64
    out["image_base64"] = front_b64 or back_b64
    return out


def _push_partial_auto_scan(
    stdout: BinaryIO,
    *,
    detected_side: str,
    front_b64: str,
    back_b64: str,
    extra: dict | None = None,
) -> None:
    """Notify extension that one side is ready; the other slot stays empty until the next scan."""
    body: dict = {
        "type": "AUTO_SCAN_RESULT",
        "success": True,
        "session_pending": True,
        "detected_side": detected_side,
        "source": "thales_auto_watch",
        "sdk_engine": "thales_mmmreader",
    }
    if extra:
        body.update(extra)
    _write_message_safe(stdout, _attach_id_side_images(body, front=front_b64, back=back_b64))


def _thales_auto_watch_thread(stdout: BinaryIO, stop: threading.Event) -> None:
    """
    Background loop: wait for a document on the Thales reader, then push AUTO_SCAN_RESULT
    to the extension (no inbound message required). Requires Chrome to have opened the
    native port so this process is running.

    After each successful push a cooldown sleep (FDN_AUTO_WATCH_COOLDOWN_S, default 8 s)
    prevents the card that is still sitting on the reader from triggering a second push.
    If the same document_number is returned again within the cooldown the read is skipped.
    """
    logger.info("[auto-watch] Thales background loop started")
    backoff = 1.0
    _last_doc_number = ""
    _last_push_time = 0.0
    # QS2000 has one camera. Two scans are needed:
    #   face-down scan  → portrait image,  AAMVA absent  (barcode not against glass)
    #   barcode-down scan → barcode image, AAMVA present
    # We accumulate each side independently so the user can scan in EITHER order.
    _face_image: str = ""          # portrait image from the no-AAMVA scan
    _barcode_image: str = ""       # barcode image from the AAMVA scan
    _aamva_payload: dict | None = None  # full payload from the AAMVA scan
    cooldown_s = float(os.environ.get("FDN_AUTO_WATCH_COOLDOWN_S", "8").strip() or "8")

    def _reset_session() -> None:
        nonlocal _face_image, _barcode_image, _aamva_payload, _last_doc_number
        _face_image = ""
        _barcode_image = ""
        _aamva_payload = None
        _last_doc_number = ""

    while not stop.is_set():
        try:
            from scanner_thales_sdk import read_document_safe

            out = read_document_safe()
            if out.get("type") == "NO_DOCUMENT":
                logger.debug("[auto-watch] No document before wait timeout; retrying.")
                logger.debug("[auto-watch] %s", out.get("message"))
                _reset_session()
                time.sleep(1.0)
                continue
            if out.get("type") == "ERROR":
                logger.warning("[auto-watch] %s", out.get("message"))
                time.sleep(min(backoff, 30.0))
                backoff = min(backoff * 1.5, 30.0)
                continue
            backoff = 1.0

            this_scan_image = (out.get("visible_image_base64") or "").strip()

            payload = scanner.auto_scan_document_push_payload(out)
            if payload.get("type") == "ERROR":
                logger.error("[auto-watch] %s", payload.get("message"))
                continue

            doc_num = (payload.get("document_data") or {}).get("document_number", "")
            p_first = (payload.get("first_name") or "").strip()
            p_last = (payload.get("last_name") or "").strip()
            p_dob = (payload.get("date_of_birth") or "").strip()
            has_aamva = bool(doc_num and p_first and p_last and p_dob)

            if has_aamva:
                # Barcode side: save image + payload; wait for face scan if not yet done.
                _barcode_image = this_scan_image
                _aamva_payload = payload
                if not _face_image:
                    logger.info(
                        "[auto-watch] barcode side buffered (doc=%r, %d b64 chars) — "
                        "flip card face-down for portrait scan.",
                        doc_num, len(this_scan_image),
                    )
                    _push_partial_auto_scan(
                        stdout,
                        detected_side="back",
                        front_b64="",
                        back_b64=_barcode_image,
                        extra={
                            "document_data": payload.get("document_data") or {},
                            "document_number": payload.get("document_number", ""),
                        },
                    )
                    time.sleep(1.0)
                    continue
            else:
                # Face/portrait side: save image; wait for barcode scan if not yet done.
                if this_scan_image:
                    _face_image = this_scan_image
                    logger.info(
                        "[auto-watch] face side buffered (%d b64 chars) — "
                        "flip card barcode-down for AAMVA scan.",
                        len(this_scan_image),
                    )
                    if not _aamva_payload:
                        _push_partial_auto_scan(
                            stdout,
                            detected_side="front",
                            front_b64=_face_image,
                            back_b64="",
                        )
                else:
                    logger.info("[auto-watch] incomplete read (no image, no AAMVA) — skipping.")
                if not _aamva_payload:
                    time.sleep(1.0)
                    continue
                # Face arrived AFTER barcode — fall through to push.

            # Both sides are ready: portrait in front slot, barcode in back slot.
            assert _aamva_payload is not None
            front_b64 = (_face_image or "").strip()
            back_b64 = (_barcode_image or "").strip()
            if not front_b64 or not back_b64:
                logger.warning(
                    "[auto-watch] both sides required but missing image (front=%s back=%s) — skipping push.",
                    "yes" if front_b64 else "no",
                    "yes" if back_b64 else "no",
                )
                time.sleep(1.0)
                continue

            doc_num = (_aamva_payload.get("document_data") or {}).get("document_number", "")
            now = time.monotonic()
            if doc_num and doc_num == _last_doc_number and (now - _last_push_time) < cooldown_s:
                logger.info(
                    "[auto-watch] duplicate read of document %r within %.0f s cooldown — skipping push.",
                    doc_num, cooldown_s,
                )
                time.sleep(1.0)
                continue

            complete = _attach_id_side_images(_aamva_payload, front=front_b64, back=back_b64)
            complete["session_pending"] = False

            _last_doc_number = doc_num
            _last_push_time = now
            logger.info(
                "[auto-watch] pushing AUTO_SCAN_RESULT document_number=%r front_image=%s back_image=%s",
                doc_num,
                "yes" if front_b64 else "no",
                "yes" if back_b64 else "no",
            )
            try:
                _write_message_safe(stdout, complete)
            except (BrokenPipeError, OSError, ValueError) as exc:
                logger.error("[auto-watch] failed to push AUTO_SCAN_RESULT: %s", exc)
            _reset_session()
            logger.info("[auto-watch] cooldown %.0f s (remove card now)", cooldown_s)
            time.sleep(cooldown_s)
        except Exception:  # noqa: BLE001
            logger.exception("[auto-watch] unexpected error")
            time.sleep(2.0)
    logger.info("[auto-watch] Thales background loop stopped")


def _nscan690gt_auto_watch_thread(stdout: BinaryIO, stop: threading.Event) -> None:
    """
    AmbirScan Auto Scan behaviour: poll for card insertion, duplex scan, push AUTO_SCAN_RESULT.
    Manual Scan ID still works via SCAN_DOCUMENT_NSCAN690GT (shared scanner lock in SDK module).
    """
    logger.info("[auto-watch] nScan 690gt background loop starting…")
    try:
        from scanner_nscan690gt import scan_document_auto_safe
        from scanner_nscan690gt_sdk import wait_for_card_insert
    except Exception:  # noqa: BLE001
        logger.exception(
            "[auto-watch] nScan690gt import failed — Auto mode cannot run. "
            "Deploy scanner_nscan690gt.py + scanner_nscan690gt_sdk.py + updated scanner_ambir_sdk.py"
        )
        return

    logger.info("[auto-watch] nScan 690gt background loop started (insert card → auto scan)")
    poll_s = float(os.environ.get("FDN_NSCAN690GT_POLL_S", "0.35").strip() or "0.35")
    cooldown_s = float(os.environ.get("FDN_NSCAN690GT_COOLDOWN_S", "8").strip() or "8")
    backoff = 1.0

    while not stop.is_set():
        try:
            inserted = wait_for_card_insert(poll_s=poll_s, stop_event=stop)
            if not inserted or stop.is_set():
                break

            logger.info("[auto-watch] nScan690gt: card inserted — starting auto duplex scan")
            payload = scan_document_auto_safe()
            if payload.get("type") == "NO_DOCUMENT":
                logger.info("[auto-watch] nScan690gt: NO_DOCUMENT after insert edge — continue")
                time.sleep(poll_s)
                continue
            if payload.get("type") == "ERROR":
                logger.warning("[auto-watch] nScan690gt: %s", payload.get("message"))
                time.sleep(min(backoff, 10.0))
                backoff = min(backoff * 1.5, 10.0)
                continue

            backoff = 1.0
            doc_num = (payload.get("document_data") or {}).get("document_number", "")
            n_out = len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
            logger.info(
                "[auto-watch] nScan690gt: pushing AUTO_SCAN_RESULT document_number=%r name=%r "
                "two_sided=%s payload_bytes=%d",
                doc_num,
                payload.get("full_name") or payload.get("fullName"),
                payload.get("two_sided"),
                n_out,
            )
            try:
                _write_message_safe(stdout, payload)
            except (BrokenPipeError, OSError, ValueError) as exc:
                logger.error("[auto-watch] nScan690gt: failed to push AUTO_SCAN_RESULT: %s", exc)
                time.sleep(2.0)
                continue
            logger.info("[auto-watch] nScan690gt: cooldown %.0f s (remove card)", cooldown_s)
            time.sleep(cooldown_s)
        except Exception:  # noqa: BLE001
            logger.exception("[auto-watch] nScan690gt unexpected error")
            time.sleep(2.0)
    logger.info("[auto-watch] nScan690gt background loop stopped")


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
    # pytwain logs every TWAIN protocol call at INFO — only show warnings+
    logging.getLogger('twain').setLevel(logging.WARNING)
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

    global _nscan690gt_stdout, _active_scanner
    _nscan690gt_stdout = stdout

    stop_watch = threading.Event()
    watch_threads: list[threading.Thread] = []
    if _thales_auto_watch_enabled():
        _ini_paths = _HOST_ROOT / "config" / "thales_paths.ini"
        _app_ini = _HOST_ROOT / "config" / "Application.ini"
        if not _ini_paths.is_file():
            logger.warning(
                "Thales auto-watch is ON but %s is missing. Copy config/thales_paths.example.ini "
                "to thales_paths.ini and set sdk_bin + application_ini.",
                _ini_paths,
            )
        if not _app_ini.is_file():
            logger.warning(
                "Thales auto-watch expects %s - copy from Application.ini.example if missing.",
                _app_ini,
            )
        _active_scanner = "thales"
        _start_thales_auto_watch()
        if _thales_watch_thread is not None:
            watch_threads.append(_thales_watch_thread)
        logger.info(
            "FDN_THALES_AUTO_WATCH is on - each successful Thales read pushes "
            "AUTO_SCAN_RESULT to the extension (listen on native port onMessage)."
        )

    if _nscan690gt_auto_watch_enabled():
        logger.info(
            "FDN_NSCAN690GT_AUTO_WATCH is on — starting nScan690gt auto-watch at boot "
            "(extension can override via SET_NSCAN690GT_SCAN_MODE)."
        )
        _start_nscan690gt_auto_watch()
        if _nscan690gt_watch_thread is not None:
            watch_threads.append(_nscan690gt_watch_thread)

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

            logger.info("[host] received message from extension (decoded JSON):\n%s", _format_inbound_message(raw))
            _write_last_inbound(raw)

            try:
                response = dispatch(raw)
            except Exception:  # noqa: BLE001
                logger.exception("Unexpected failure in dispatch")
                response = _error("Unexpected internal error")

            rtype = response.get("type")
            logger.info("[host] sending response to extension: type=%r", rtype)
            if rtype == "SCAN_RESULT":
                logger.info(
                    "[host] SCAN_RESULT summary: image_base64_chars=%d ocr_data=%s",
                    len(response.get("image_base64") or ""),
                    response.get("ocr_data"),
                )
            if rtype == "SDK_DOCUMENT_RESULT":
                logger.info(
                    "[host] SDK_DOCUMENT_RESULT document_number=%r engine=%s two_sided=%s",
                    (response.get("document_data") or {}).get("document_number"),
                    response.get("sdk_engine"),
                    response.get("two_sided"),
                )
            if rtype == "SDK_DOCUMENT_SIDE_RESULT":
                logger.info(
                    "[host] SDK_DOCUMENT_SIDE_RESULT pending=%s detected_side=%r",
                    response.get("session_pending"),
                    response.get("detected_side"),
                )

            try:
                _write_message_safe(stdout, response)
            except ValueError as exc:
                logger.error("Failed to write response (oversize?): %s", exc)
                try:
                    slim: dict[str, Any] = {
                        "type": "ERROR",
                        "success": False,
                        "message": str(exc),
                    }
                    rid = response.get("requestId")
                    if isinstance(rid, str) and rid:
                        slim["requestId"] = rid
                    _write_message_safe(stdout, slim)
                except (OSError, ValueError) as exc2:
                    logger.error("Failed to write slim error response: %s", exc2)
                    return 1
            except OSError as exc:
                logger.error("Failed to write response: %s", exc)
                return 1
    finally:
        stop_watch.set()
        _stop_nscan690gt_auto_watch()
        _stop_thales_auto_watch()


def _wants_native_messaging() -> bool:
    if "--native-messaging" in sys.argv:
        return True
    v = os.environ.get("FDN_NATIVE_MESSAGING", "").strip().lower()
    return v in ("1", "true", "yes", "on")


if __name__ == "__main__":
    raise SystemExit(run() if _wants_native_messaging() else run_terminal_demo())
