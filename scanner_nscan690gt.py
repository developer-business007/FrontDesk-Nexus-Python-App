"""
AMBIR nScan 690gt scan module.

Pipeline:
  1. TWAIN scan via scanner_twain.scan_image(preferred_substring="690gt")
  2. zxingcpp PDF417/AAMVA barcode reading  (offline, no external API)
  3. Windows.Media.Ocr fallback             (built-in Windows 10/11, offline)
  4. Returns SDK_DOCUMENT_RESULT identical in shape to scan_document_ambir_sdk / scan_document_thales_sdk

Required on hotel PC:
  pip install zxingcpp pillow
  pip install winrt-Windows.Media.Ocr winrt-Windows.Graphics.Imaging winrt-Windows.Storage.Streams
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_HOST_DIR = Path(__file__).resolve().parent
_DEFAULT_SAMPLE = _HOST_DIR / "samples" / "id_card.png"
_LOG_TAG = "[nScan690gt]"


def _log_step(msg: str, *args: Any) -> None:
    """Always-visible milestone lines in native-host.log (prefix for easy grepping)."""
    logger.info("%s " + msg, _LOG_TAG, *args)

# Matches AAMVA three-letter field codes followed by value text.
_AAMVA_FIELD_RE = re.compile(r'([A-Z]{3})([^\r\n]*)')


# ─────────────────────────────────────────────────────────────────────────────
# AAMVA PDF417 parser
# ─────────────────────────────────────────────────────────────────────────────

def _aamva_date(mmddyyyy: str) -> str:
    """Convert MMDDYYYY → ISO date (YYYY-MM-DD). Returns original string on bad input."""
    s = (mmddyyyy or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[4:]}-{s[:2]}-{s[2:4]}"
    return s


def _parse_aamva(raw: str) -> dict[str, Any]:
    """
    Parse AAMVA PDF417 raw text into a structured snake_case dict.

    Supports US AAMVA DL/ID PDF417 barcode (version 1–9+).
    Key field codes used:
      DAQ = DL number   DCS = last name    DAC = first name  DAD = middle name
      DBB = DOB         DBA = expiry       DBD = issue date
      DAG = street      DAI = city         DAJ = state       DAK = postal code
      DBC = sex (1=M / 2=F)
    """
    fields: dict[str, str] = {}
    for m in _AAMVA_FIELD_RE.finditer(raw):
        code, value = m.group(1), m.group(2).strip()
        if code not in fields:  # first occurrence wins
            fields[code] = value

    first = fields.get("DAC", "").strip()
    last = fields.get("DCS", "").strip()
    middle = fields.get("DAD", "").strip()

    # Fallback: some jurisdictions encode "LAST$FIRST$MIDDLE" in DAA
    if not (first or last):
        daa = fields.get("DAA", "")
        parts = [p.strip() for p in re.split(r"[$,]", daa) if p.strip()]
        if len(parts) >= 2:
            last, first = parts[0], parts[1]
            middle = parts[2] if len(parts) >= 3 else ""
        elif len(parts) == 1:
            last = parts[0]

    full_name = " ".join(x for x in (first, middle, last) if x)
    dob = _aamva_date(fields.get("DBB", ""))
    expiry = _aamva_date(fields.get("DBA", ""))
    issue = _aamva_date(fields.get("DBD", ""))
    dl_num = fields.get("DAQ", "").strip()
    street = fields.get("DAG", "").strip()
    city = fields.get("DAI", "").strip()
    state_code = fields.get("DAJ", "").strip()
    postal = fields.get("DAK", "").strip()[:5]
    sex_code = fields.get("DBC", "").strip()
    sex = "M" if sex_code == "1" else ("F" if sex_code == "2" else "")

    city_state_zip = " ".join(x for x in (city, state_code, postal) if x)
    if city_state_zip and state_code:
        city_state_zip = f"{city}, {state_code} {postal}".strip(", ")
    address = f"{street}, {city_state_zip}".strip(", ") if city_state_zip else street

    barcode_data: dict[str, Any] = {"source": "pdf417_aamva"}
    for k in ("DAQ", "DCS", "DAC", "DAD", "DBB", "DBA", "DBD", "DAG", "DAI", "DAJ", "DAK", "DBC"):
        if k in fields:
            barcode_data[k] = fields[k]

    return {
        "first_name": first,
        "middle_name": middle,
        "last_name": last,
        "full_name": full_name,
        "document_number": dl_num,
        "document_type": "Driver License",
        "date_of_birth": dob,
        "expiry_date": expiry,
        "issue_date": issue,
        "address": address,
        "street_address": street,
        "city": city,
        "state": state_code,
        "postal_code": postal,
        "gender": sex,
        "nationality": "",
        "mrz_raw": "",
        "barcode_data": barcode_data,
    }


# ─────────────────────────────────────────────────────────────────────────────
# zxingcpp PDF417 reader
# ─────────────────────────────────────────────────────────────────────────────

def _try_pdf417(raw_bytes: bytes) -> tuple[dict[str, Any] | None, str]:
    """
    Attempt PDF417 barcode decode on the scanned image.
    Returns (structured_dict, aamva_raw_text) on success, or (None, "") on failure.
    """
    try:
        import zxingcpp
        from PIL import Image
    except ImportError as exc:
        logger.warning("nScan690gt: zxingcpp or Pillow not installed — skipping PDF417: %s", exc)
        return None, ""

    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("L")
        results = zxingcpp.read_barcodes(img)
        for r in results:
            text = r.text or ""
            is_pdf417 = getattr(r, "format", None) == getattr(zxingcpp, "BarcodeFormat", type(None)).PDF417  # type: ignore[attr-defined]
            is_aamva = text.startswith("@") or "ANSI " in text or "DAQ" in text or "DCS" in text
            if is_pdf417 and is_aamva:
                structured = _parse_aamva(text)
                if structured.get("document_number") and (structured.get("last_name") or structured.get("first_name")):
                    logger.info(
                        "nScan690gt: PDF417/AAMVA decoded — doc=%r name=%r dob=%r",
                        structured.get("document_number"),
                        structured.get("full_name"),
                        structured.get("date_of_birth"),
                    )
                    return structured, text
        logger.info("nScan690gt: no AAMVA PDF417 barcode found in image")
        return None, ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("nScan690gt: zxingcpp decode failed: %s", exc)
        return None, ""


# ─────────────────────────────────────────────────────────────────────────────
# Windows.Media.Ocr fallback
# ─────────────────────────────────────────────────────────────────────────────

async def _ocr_async(image_bytes: bytes) -> str:
    """Run Windows.Media.Ocr on raw image bytes. Async because WinRT APIs are async."""
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("Windows OCR engine unavailable for this user's language profile")

    stream = InMemoryRandomAccessStream()
    try:
        writer = DataWriter(stream)
        writer.write_bytes(bytearray(image_bytes))
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)

        decoder = await BitmapDecoder.create_async(stream)
        bmp = await decoder.get_software_bitmap_async()
        result = await engine.recognize_async(bmp)
        return (result.text or "") if result else ""
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def _try_windows_ocr(raw_bytes: bytes) -> str:
    """
    Run Windows.Media.Ocr synchronously.
    Returns extracted text or "" on any error (missing package, unsupported OS, etc.).
    """
    try:
        return asyncio.run(_ocr_async(raw_bytes))
    except RuntimeError as exc:
        msg = str(exc)
        if "running event loop" in msg.lower() or "This event loop is already running" in msg:
            try:
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(_ocr_async(raw_bytes))
                finally:
                    loop.close()
            except Exception as exc2:  # noqa: BLE001
                logger.warning("nScan690gt: Windows OCR (new loop) failed: %s", exc2)
                return ""
        logger.warning("nScan690gt: Windows OCR runtime error: %s", exc)
        return ""
    except ImportError as exc:
        logger.warning("nScan690gt: winrt packages not installed — OCR unavailable: %s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("nScan690gt: Windows OCR failed: %s", exc)
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# Result builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_result(
    structured: dict[str, Any],
    image_b64: str,
    aamva_raw: str = "",
    *,
    back_image_b64: str = "",
    sdk_engine: str = "nscan690gt_si",
) -> dict[str, Any]:
    """Build a SDK_DOCUMENT_RESULT dict from structured fields + image base64."""
    from scanner import sync_structured_document_fields_for_extension

    sync_structured_document_fields_for_extension(structured)
    back = (back_image_b64 or "").strip()

    return {
        "type": "SDK_DOCUMENT_RESULT",
        "success": True,
        "sdk_engine": sdk_engine,
        "vision_ocr_fallback": not bool(aamva_raw),
        "document_data": structured,
        "aamva_raw": aamva_raw,
        "codeline_raw": "",
        "codeline_data_raw": "",
        # flat snake_case fields
        "first_name": structured.get("first_name", ""),
        "middle_name": structured.get("middle_name", ""),
        "last_name": structured.get("last_name", ""),
        "full_name": structured.get("full_name", ""),
        "document_number": structured.get("document_number", ""),
        "document_type": structured.get("document_type", ""),
        "date_of_birth": structured.get("date_of_birth", ""),
        "expiry_date": structured.get("expiry_date", ""),
        "issue_date": structured.get("issue_date", ""),
        "address": structured.get("address", ""),
        "street_address": structured.get("street_address", ""),
        "city": structured.get("city", ""),
        "state": structured.get("state", ""),
        "postal_code": structured.get("postal_code", ""),
        "gender": structured.get("gender", ""),
        "nationality": "",
        "mrz_raw": "",
        "barcode_data": structured.get("barcode_data", {}),
        # camelCase aliases
        "fullName": structured.get("fullName", ""),
        "firstName": structured.get("firstName", ""),
        "middleName": structured.get("middleName", ""),
        "lastName": structured.get("lastName", ""),
        "dateOfBirth": structured.get("dateOfBirth", ""),
        "idNumber": structured.get("idNumber", ""),
        "idType": structured.get("idType", ""),
        "issueDate": structured.get("issueDate", ""),
        "expiryDate": structured.get("expiryDate", ""),
        "streetAddress": structured.get("streetAddress", ""),
        "postalCode": structured.get("postalCode", ""),
        "sex": structured.get("sex", ""),
        # image slots
        "image_base64": image_b64,
        "image_front_base64": image_b64,
        "image_back_base64": back,
        "front_image_base64": image_b64,
        "back_image_base64": back,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

_TWAIN_SCAN_TIMEOUT_S = 55  # seconds to wait before declaring card pre-inserted / stuck
_WORKER_HEARTBEAT_S = 5


_WORKER_SCRIPT = _HOST_DIR / "scanner_twain_worker.py"


def _resolve_host_log_path() -> Path | None:
    """Same file the native host uses (env override or logs/native-host.log)."""
    if os.environ.get("FDN_NO_LOG_FILE", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    explicit = os.environ.get("FDN_LOG_FILE", "").strip()
    if explicit:
        return Path(explicit)
    return _HOST_DIR / "logs" / "native-host.log"


def _run_twain_worker(*, timeout_s: int) -> subprocess.CompletedProcess[str]:
    """
    Spawn the TWAIN worker and log heartbeats while it waits for MSG_XFERREADY.
    Worker stderr is mirrored into native-host.log as [nScan690gt][worker] lines.
    """
    log_path = _resolve_host_log_path()
    env = os.environ.copy()
    if log_path is not None:
        env["FDN_NSCAN690GT_LOG_FILE"] = str(log_path.resolve())

    _log_step(
        "step1 START TWAIN worker — timeout=%ds path=%s python=%s",
        timeout_s,
        _WORKER_SCRIPT,
        sys.executable,
    )
    _log_step(
        "hint: blue LED ON usually means IDLE (SIP_LED_INDICATOR1_AUTO). "
        "Motor/feed starts only after insert event → MSG_XFERREADY. "
        "Remove card first, click Scan ID, then insert."
    )

    proc = subprocess.Popen(
        [sys.executable, str(_WORKER_SCRIPT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(_HOST_DIR),
        env=env,
    )
    started = time.monotonic()
    deadline = started + timeout_s
    next_beat = started + _WORKER_HEARTBEAT_S

    while True:
        rc = proc.poll()
        now = time.monotonic()
        if rc is not None:
            break
        if now >= deadline:
            _log_step(
                "TIMEOUT after %ds — killing worker pid=%s (no MSG_XFERREADY / card not detected)",
                timeout_s,
                proc.pid,
            )
            try:
                proc.kill()
            except OSError:
                pass
            try:
                _out, err = proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                err = ""
            if err and err.strip():
                for line in err.strip().splitlines():
                    logger.warning("%s [worker stderr] %s", _LOG_TAG, line)
            raise subprocess.TimeoutExpired(proc.args, timeout_s)

        if now >= next_beat:
            elapsed = int(now - started)
            remaining = max(0, int(deadline - now))
            _log_step(
                "WAITING for card/XFERREADY — elapsed=%ds remaining=%ds pid=%s",
                elapsed,
                remaining,
                proc.pid,
            )
            next_beat = now + _WORKER_HEARTBEAT_S
        time.sleep(0.25)

    stdout, stderr = proc.communicate()
    elapsed = int(time.monotonic() - started)
    _log_step("TWAIN worker exited — rc=%s elapsed=%ds", proc.returncode, elapsed)
    if stderr and stderr.strip():
        for line in stderr.strip().splitlines():
            logger.info("%s [worker] %s", _LOG_TAG, line)
    return subprocess.CompletedProcess(
        args=proc.args,
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout or "",
        stderr=stderr or "",
    )


def scan_document(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    AMBIR nScan 690gt full scan pipeline (official SI_* API).

      1. Load NS690gt.DLL → SI_OpenInterface("nScan690gt")
      2. Wait for paper (SI_GetPaperStatus) → configure → SI_StartScan (starts feed)
      3. Read front [+ back if duplex] → SI_FeedPaperOut
      4. Decode PDF417/AAMVA (prefer back image), else Windows.Media.Ocr
      5. Return SDK_DOCUMENT_RESULT

    Set FDN_NSCAN690GT_FORCE_TWAIN=1 to use the legacy TWAIN path (not recommended).

    Raises ScannerError on unrecoverable failures (propagated by main.py dispatch).
    """
    from scanner import ScannerError, empty_id_fields, parse_id_fields

    force_twain = os.environ.get("FDN_NSCAN690GT_FORCE_TWAIN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )
    if force_twain:
        return _scan_document_twain(payload)

    _log_step("========== SCAN START (engine=NS690gt.DLL / SI_*) ==========")
    if payload:
        _log_step("payload keys: %s", sorted(payload.keys()))

    from scanner_ambir_sdk import scan_document_safe

    _log_step("step1 open NS690gt.DLL + wait for card + SI_StartScan…")
    out = scan_document_safe(force_model="nScan690gt", duplex=True)

    if out.get("type") == "NO_DOCUMENT":
        raise ScannerError(
            str(out.get("message") or "No card detected. Insert the ID after clicking Scan ID.")
        )
    if out.get("type") == "ERROR":
        raise ScannerError(str(out.get("message") or "nScan 690gt SI scan failed"))

    image_b64 = (out.get("image_base64") or "").strip()
    back_b64 = (out.get("image_back_base64") or "").strip()
    if not image_b64:
        raise ScannerError("nScan 690gt returned no image data")

    _log_step(
        "step1 OK — model=%r dll=%s duplex=%s front_chars=%d back_chars=%d",
        out.get("model"),
        out.get("dll_path"),
        out.get("duplex"),
        len(image_b64),
        len(back_b64),
    )

    # Prefer barcode on back (PDF417), then front
    structured = None
    aamva_raw = ""
    for label, b64 in (("back", back_b64), ("front", image_b64)):
        if not b64:
            continue
        _log_step("step2 PDF417/AAMVA on %s image…", label)
        structured, aamva_raw = _try_pdf417(base64.b64decode(b64))
        if structured:
            _log_step(
                "step2 OK barcode on %s — doc=%r name=%r",
                label,
                structured.get("document_number"),
                structured.get("full_name"),
            )
            break

    if not structured:
        _log_step("step3 Windows.Media.Ocr on front…")
        raw_text = _try_windows_ocr(base64.b64decode(image_b64))
        if raw_text:
            _log_step("step3 OK OCR — %d chars", len(raw_text))
            ocr_data = parse_id_fields(raw_text)
        else:
            logger.warning("%s step3 OCR returned no text — empty fields + images", _LOG_TAG)
            ocr_data = empty_id_fields()
        structured = {
            "first_name": ocr_data.get("firstName", ""),
            "middle_name": ocr_data.get("middleName", ""),
            "last_name": ocr_data.get("lastName", ""),
            "full_name": ocr_data.get("fullName", ""),
            "document_number": ocr_data.get("idNumber", ""),
            "document_type": ocr_data.get("idType", ""),
            "date_of_birth": ocr_data.get("dateOfBirth", ""),
            "expiry_date": ocr_data.get("expiryDate", ""),
            "issue_date": ocr_data.get("issueDate", ""),
            "address": ocr_data.get("address", ""),
            "street_address": "",
            "city": "",
            "state": "",
            "postal_code": "",
            "gender": ocr_data.get("sex", ""),
            "nationality": "",
            "mrz_raw": "",
            "barcode_data": {"source": "windows_ocr"},
        }

    result = _build_result(
        structured,
        image_b64,
        aamva_raw,
        back_image_b64=back_b64,
        sdk_engine="nscan690gt_si",
    )
    _log_step(
        "========== SCAN DONE — engine=%s name=%r id=%r duplex_back=%s ==========",
        result.get("sdk_engine"),
        result.get("full_name") or result.get("fullName"),
        result.get("document_number") or result.get("idNumber"),
        bool(back_b64),
    )
    return result


def _scan_document_twain(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Legacy TWAIN path (EnableDS wait). Kept for emergency fallback only."""
    from scanner import ScannerError, empty_id_fields, parse_id_fields

    _log_step("========== SCAN START (engine=TWAIN FALLBACK) ==========")
    try:
        proc = _run_twain_worker(timeout_s=_TWAIN_SCAN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise ScannerError(
            "No card detected (TWAIN). Prefer NS690gt.DLL path — unset FDN_NSCAN690GT_FORCE_TWAIN."
        )

    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0 or not stdout:
        err = (proc.stderr or "").strip() or "TWAIN worker exited with no output"
        raise ScannerError(err or "TWAIN scan failed")

    tw: dict[str, Any] = json.loads(stdout)
    if tw.get("type") == "ERROR":
        raise ScannerError(str(tw.get("message") or "nScan 690gt TWAIN scan failed"))
    image_b64 = (tw.get("image_base64") or "").strip()
    if not image_b64:
        raise ScannerError("nScan 690gt returned no image data")

    structured, aamva_raw = _try_pdf417(base64.b64decode(image_b64))
    if not structured:
        raw_text = _try_windows_ocr(base64.b64decode(image_b64))
        ocr_data = parse_id_fields(raw_text) if raw_text else empty_id_fields()
        structured = {
            "first_name": ocr_data.get("firstName", ""),
            "middle_name": ocr_data.get("middleName", ""),
            "last_name": ocr_data.get("lastName", ""),
            "full_name": ocr_data.get("fullName", ""),
            "document_number": ocr_data.get("idNumber", ""),
            "document_type": ocr_data.get("idType", ""),
            "date_of_birth": ocr_data.get("dateOfBirth", ""),
            "expiry_date": ocr_data.get("expiryDate", ""),
            "issue_date": ocr_data.get("issueDate", ""),
            "address": ocr_data.get("address", ""),
            "street_address": "",
            "city": "",
            "state": "",
            "postal_code": "",
            "gender": ocr_data.get("sex", ""),
            "nationality": "",
            "mrz_raw": "",
            "barcode_data": {"source": "windows_ocr"},
        }
    return _build_result(
        structured, image_b64, aamva_raw, sdk_engine="nscan690gt_twain",
    )