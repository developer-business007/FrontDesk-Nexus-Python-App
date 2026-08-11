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
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_HOST_DIR = Path(__file__).resolve().parent
_DEFAULT_SAMPLE = _HOST_DIR / "samples" / "id_card.png"

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

def _build_result(structured: dict[str, Any], image_b64: str, aamva_raw: str = "") -> dict[str, Any]:
    """Build a SDK_DOCUMENT_RESULT dict from structured fields + image base64."""
    from scanner import sync_structured_document_fields_for_extension

    sync_structured_document_fields_for_extension(structured)

    return {
        "type": "SDK_DOCUMENT_RESULT",
        "success": True,
        "sdk_engine": "nscan690gt_twain",
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
        # camelCase aliases (set by sync_structured_document_fields_for_extension)
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
        "image_back_base64": "",
        "front_image_base64": image_b64,
        "back_image_base64": "",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

_TWAIN_SCAN_TIMEOUT_S = 55  # seconds to wait before declaring card pre-inserted / stuck


_WORKER_SCRIPT = _HOST_DIR / "scanner_twain_worker.py"


def scan_document(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    AMBIR nScan 690gt full scan pipeline.

      1. Acquire image via TWAIN subprocess with 55 s timeout.
         • pytwain's modal loop requires the Windows message pump to run on the
           process main thread. A daemon thread does not satisfy this — the scanner
           sends MSG_XFERREADY to the thread that called MSG_ENALEDS, which in a
           background thread has no proper HWND message loop. Running as a
           subprocess gives the TWAIN worker its own main thread.
         • Pre-inserted card = no insertion event. subprocess.TimeoutExpired catches
           this and returns a clear error instead of blocking the host forever.
      2. Decode PDF417/AAMVA barcode with zxingcpp (most US DL backs).
      3. If no barcode, fall back to Windows.Media.Ocr (offline, built-in).
      4. Return SDK_DOCUMENT_RESULT.

    Raises ScannerError on unrecoverable failures (propagated by main.py dispatch).
    """
    from scanner import ScannerError, empty_id_fields, parse_id_fields

    # ── 1. TWAIN scan (subprocess + timeout) ─────────────────────────────────
    try:
        proc = subprocess.run(
            [sys.executable, str(_WORKER_SCRIPT)],
            capture_output=True,
            timeout=_TWAIN_SCAN_TIMEOUT_S,
            text=True,
            cwd=str(_HOST_DIR),
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "nScan690gt: TWAIN timed out after %ds — card pre-inserted or scanner stuck",
            _TWAIN_SCAN_TIMEOUT_S,
        )
        raise ScannerError(
            "No card detected. Remove any card from the scanner, then click "
            "Scan ID and insert the card within 55 seconds."
        )

    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0 or not stdout:
        err = (proc.stderr or "").strip() or "TWAIN worker exited with no output"
        logger.warning("nScan690gt: TWAIN worker failed (rc=%d): %s", proc.returncode, err)
        raise ScannerError(err or "TWAIN scan failed")

    try:
        tw: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise ScannerError(f"TWAIN worker returned invalid data: {exc}") from exc
    if tw.get("type") == "ERROR":
        raise ScannerError(str(tw.get("message") or "nScan 690gt scan failed"))
    image_b64 = (tw.get("image_base64") or "").strip()
    if not image_b64:
        raise ScannerError("nScan 690gt returned no image data")

    logger.info(
        "nScan690gt: TWAIN scan ok — source=%r simulated=%s chars=%d",
        tw.get("source_name"),
        tw.get("simulated"),
        len(image_b64),
    )

    raw_bytes = base64.b64decode(image_b64)

    # ── 2. Try PDF417 barcode (US DL back) ───────────────────────────────────
    structured, aamva_raw = _try_pdf417(raw_bytes)

    # ── 3. Windows.Media.Ocr fallback ────────────────────────────────────────
    if not structured:
        logger.info("nScan690gt: no barcode — falling back to Windows.Media.Ocr")
        raw_text = _try_windows_ocr(raw_bytes)
        if raw_text:
            logger.info("nScan690gt: Windows OCR extracted %d chars", len(raw_text))
            ocr_data = parse_id_fields(raw_text)
        else:
            logger.warning("nScan690gt: Windows OCR returned no text — returning image with empty fields")
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

    # ── 4. Build and return result ────────────────────────────────────────────
    return _build_result(structured, image_b64, aamva_raw)