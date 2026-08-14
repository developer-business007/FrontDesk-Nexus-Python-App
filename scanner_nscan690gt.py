"""
AMBIR nScan 690gt scan module.

Pipeline (NS690gt.DLL SI_* API):
  1. Manual: SI_GetPaperStatus wait → SI_StartScan duplex → front + back BMP
  2. Auto:   background paper poll → scan on insert (see main.py auto-watch)
  3. zxingcpp PDF417/AAMVA on back (then front), Windows.Media.Ocr fallback
  4. Returns SDK_DOCUMENT_RESULT / AUTO_SCAN_RESULT for the extension
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_HOST_DIR = Path(__file__).resolve().parent
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
    *,
    front_b64: str,
    back_b64: str = "",
    aamva_raw: str = "",
    scan_mode: str = "manual",
) -> dict[str, Any]:
    """Build SDK_DOCUMENT_RESULT with separate front/back image slots."""
    from scanner import sync_structured_document_fields_for_extension

    sync_structured_document_fields_for_extension(structured)
    front = (front_b64 or "").strip()
    back = (back_b64 or "").strip()

    return {
        "type": "SDK_DOCUMENT_RESULT",
        "success": True,
        "sdk_engine": "nscan690gt_sdk",
        "scan_mode": scan_mode,
        "two_sided": bool(front and back),
        "vision_ocr_fallback": not bool(aamva_raw),
        "document_data": structured,
        "aamva_raw": aamva_raw,
        "codeline_raw": "",
        "codeline_data_raw": "",
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
        "image_base64": front or back,
        "image_front_base64": front,
        "image_back_base64": back,
        "front_image_base64": front,
        "back_image_base64": back,
    }


def _decode_from_images(front_b64: str, back_b64: str) -> tuple[dict[str, Any], str]:
    """PDF417 on back then front; Windows OCR fallback on available sides."""
    from scanner import empty_id_fields, parse_id_fields

    front_bytes = base64.b64decode(front_b64) if front_b64 else b""
    back_bytes = base64.b64decode(back_b64) if back_b64 else b""

    structured, aamva_raw = None, ""
    for label, raw in (("back", back_bytes), ("front", front_bytes)):
        if not raw:
            continue
        _log_step("PDF417/AAMVA decode on %s…", label)
        structured, aamva_raw = _try_pdf417(raw)
        if structured:
            _log_step("PDF417 OK on %s — doc=%r", label, structured.get("document_number"))
            break

    if structured:
        return structured, aamva_raw

    _log_step("no barcode — Windows.Media.Ocr fallback")
    combined_text = ""
    for label, raw in (("back", back_bytes), ("front", front_bytes)):
        if not raw:
            continue
        text = _try_windows_ocr(raw)
        if text:
            _log_step("OCR %s — %d chars", label, len(text))
            combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    if combined_text:
        ocr_data = parse_id_fields(combined_text)
    else:
        logger.warning("%s OCR returned no text — empty fields + images only", _LOG_TAG)
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
    return structured, ""


def _sdk_ok_to_document_result(sdk_ok: dict[str, Any], *, scan_mode: str) -> dict[str, Any]:
    front_b64 = (sdk_ok.get("image_front_base64") or sdk_ok.get("front_image_base64") or "").strip()
    back_b64 = (sdk_ok.get("image_back_base64") or sdk_ok.get("back_image_base64") or "").strip()
    if not front_b64 and not back_b64:
        raise ValueError("nScan 690gt returned no image data")
    structured, aamva_raw = _decode_from_images(front_b64, back_b64)
    return _build_result(
        structured,
        front_b64=front_b64,
        back_b64=back_b64,
        aamva_raw=aamva_raw,
        scan_mode=scan_mode,
    )


def build_auto_scan_payload(sdk_ok: dict[str, Any]) -> dict[str, Any]:
    """AUTO_SCAN_RESULT for extension auto-watch (insert card → scan)."""
    doc = _sdk_ok_to_document_result(sdk_ok, scan_mode="auto")
    doc["type"] = "AUTO_SCAN_RESULT"
    doc["source"] = "nscan690gt_auto_watch"
    doc["session_pending"] = False
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# Public entry points
# ─────────────────────────────────────────────────────────────────────────────


def scan_document(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Manual mode: wait for card after Scan ID click, duplex scan via NS690gt.DLL,
    decode PDF417/OCR, return SDK_DOCUMENT_RESULT.
    """
    from scanner import ScannerError
    from scanner_nscan690gt_sdk import scan_manual_safe

    _log_step("========== MANUAL SCAN START (NS690gt.DLL SI_* API) ==========")
    if payload:
        _log_step("payload keys: %s", sorted(payload.keys()))

    out = scan_manual_safe()
    if out.get("type") == "NO_DOCUMENT":
        raise ScannerError(str(out.get("message") or "No card detected."))
    if out.get("type") != "NSCAN690GT_SCAN_OK":
        raise ScannerError(str(out.get("message") or "nScan 690gt scan failed"))

    _log_step(
        "hardware OK — front=%d chars back=%d chars duplex=%s",
        len(out.get("image_front_base64") or ""),
        len(out.get("image_back_base64") or ""),
        out.get("duplex"),
    )

    result = _sdk_ok_to_document_result(out, scan_mode="manual")
    _log_step(
        "========== MANUAL SCAN DONE — name=%r id=%r two_sided=%s ==========",
        result.get("full_name") or result.get("fullName"),
        result.get("document_number") or result.get("idNumber"),
        result.get("two_sided"),
    )
    return result


def scan_document_auto_safe() -> dict[str, Any]:
    """
    Auto mode: scan when paper is in slot; NO_DOCUMENT if idle.
    Used by main.py nScan690gt auto-watch thread.
    """
    from scanner_nscan690gt_sdk import scan_auto_safe

    out = scan_auto_safe()
    if out.get("type") == "NO_DOCUMENT":
        return out
    if out.get("type") == "ERROR":
        return out
    if out.get("type") != "NSCAN690GT_SCAN_OK":
        return {"type": "ERROR", "message": "Unexpected nScan690gt auto scan response"}
    try:
        return build_auto_scan_payload(out)
    except Exception as exc:  # noqa: BLE001
        return {"type": "ERROR", "message": str(exc)}
