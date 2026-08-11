"""ID scan: sample image + Google Cloud Vision OCR + camelCase fields for the extension."""

from __future__ import annotations

import base64
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from utils import file_to_base64

logger = logging.getLogger(__name__)

_HOST_DIR = Path(__file__).resolve().parent

# Default sample image; override with FDN_SAMPLE_ID_PATH.
DEFAULT_SAMPLE_PATH = _HOST_DIR / "samples" / "id_card.png"

_vision_client: Any | None = None

# Optional default key filenames (project root). Prefer env; do not commit keys to git.
_CRED_ENV_KEYS = ("FDN_GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_APPLICATION_CREDENTIALS")
_CRED_CANDIDATE_FILES = (
    "gcp-vision-credentials.json",
    "google-vision-key.json",
    "service-account.json",
)


def empty_id_fields() -> dict[str, str]:
    return {
        "fullName": "",
        "dateOfBirth": "",
        "idNumber": "",
        "idType": "",
        "issueDate": "",
        "expiryDate": "",
        "address": "",
    }


class ScannerError(Exception):
    """Domain error for scanner operations (mapped to ERROR responses)."""


def _temp_image_path_from_base64(b64: str) -> Path:
    """Decode image base64 to a temp file with a sensible extension for Vision API."""
    raw = base64.b64decode(b64, validate=True)
    if raw.startswith(b"BM"):
        suffix = ".bmp"
    elif raw.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif len(raw) >= 3 and raw[:3] == b"\xff\xd8\xff":
        suffix = ".jpg"
    else:
        suffix = ".bin"
    fd, name = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    path = Path(name)
    path.write_bytes(raw)
    return path


def _resolve_credentials_path() -> Path | None:
    """Service account JSON: env first, then known filenames / id-scan-project-*.json in project folder."""
    for key in _CRED_ENV_KEYS:
        raw = os.environ.get(key, "").strip()
        if not raw:
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = _HOST_DIR / p
        if p.is_file():
            return p
        logger.warning("Credentials path from %s does not exist: %s", key, p)

    for name in _CRED_CANDIDATE_FILES:
        p = _HOST_DIR / name
        if p.is_file():
            logger.info("Using credentials file (discovered): %s", p.name)
            return p

    discovered = sorted(_HOST_DIR.glob("id-scan-project-*.json"))
    if len(discovered) > 1:
        logger.warning(
            "Multiple id-scan-project-*.json files found; using %s (set %s to pick one).",
            discovered[0].name,
            _CRED_ENV_KEYS[0],
        )
    for p in discovered:
        if p.is_file():
            logger.info("Using credentials file (discovered): %s", p.name)
            return p

    return None


def _get_vision_client() -> Any:
    global _vision_client
    if _vision_client is not None:
        return _vision_client

    try:
        from google.cloud import vision as _vision  # noqa: PLC0415
        from google.oauth2 import service_account as _sa  # noqa: PLC0415
    except ImportError as exc:
        raise ScannerError(
            "google-cloud-vision not installed. "
            "Run: pip install google-cloud-vision google-auth"
        ) from exc

    key_path = _resolve_credentials_path()
    if key_path is None:
        logger.error(
            "No Google Cloud credentials found. Set %s to your service account JSON path "
            "or place a key file in the project folder (e.g. gcp-vision-credentials.json or id-scan-project-*.json).",
            " or ".join(_CRED_ENV_KEYS),
        )
        raise ScannerError("OCR failed")

    creds = _sa.Credentials.from_service_account_file(str(key_path))
    _vision_client = _vision.ImageAnnotatorClient(credentials=creds)
    return _vision_client


def extract_text_with_google_vision(image_path: Path) -> str:
    """
    Run Vision on the image: prefer ``document_text_detection`` (better for ID layouts),
    then fall back to ``text_detection``.
    """
    try:
        from google.api_core import exceptions as google_exceptions  # noqa: PLC0415
        from google.cloud import vision as _vision  # noqa: PLC0415
    except ImportError as exc:
        raise ScannerError("google-cloud-vision not installed") from exc

    image_path = Path(image_path)
    if not image_path.is_file():
        logger.error("Vision: image not found: %s", image_path)
        raise ScannerError("OCR failed")

    content = image_path.read_bytes()
    image = _vision.Image(content=content)

    try:
        client = _get_vision_client()
        doc_resp = client.document_text_detection(image=image)
    except ScannerError:
        raise
    except (google_exceptions.GoogleAPIError, OSError) as exc:
        logger.exception("Google Vision API request failed: %s", exc)
        raise ScannerError("OCR failed") from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected Vision error: %s", exc)
        raise ScannerError("OCR failed") from exc

    if doc_resp.error.message:
        logger.error("Vision API error: %s", doc_resp.error.message)
        raise ScannerError("OCR failed")

    fta = doc_resp.full_text_annotation
    if fta and fta.text:
        raw = fta.text.strip()
        if raw:
            return raw

    try:
        from google.api_core import exceptions as google_exceptions  # noqa: PLC0415
        response = client.text_detection(image=image)
    except (google_exceptions.GoogleAPIError, OSError) as exc:
        logger.exception("Vision text_detection fallback failed: %s", exc)
        raise ScannerError("OCR failed") from exc

    if response.error.message:
        logger.error("Vision text_detection error: %s", response.error.message)
        raise ScannerError("OCR failed")

    texts = response.text_annotations
    if not texts:
        logger.warning("Vision returned no text for %s", image_path)
        return ""

    return (texts[0].description or "").strip()


_DATE_RE = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b",
    re.I,
)
# Polish ID document number (e.g. DIF376208); many EU IDs: letters + digits.
_DOC_NUMBER_RE = re.compile(
    r"\b[A-Z]{2,4}\d{6,9}\b|\b\d{9,12}\b",
    re.I,
)
# Do NOT use \bID\b — it matches inside "IDENTITY" and captures "ENTITY".
_ID_NUMBER_FALSE_POSITIVE = frozenset(
    {
        "entity",
        "identity",
        "number",
        "document",
        "license",
        "licence",
        "passport",
        "national",
        "republic",
        "country",
    }
)

_NAME_LINE_SKIP = re.compile(
    r"(?i)^(rzeczpospolita|republic|poland|polska|polish|european|union|identity\s*card|"
    r"dow[oó]d|osobisty|card|passport|driver|driving|licen[sc]e|licence|state|id\s*$|"
    r"nationality|obywatelstwo|sex|p[ełl]e[cć]|nazwisko|imiona|surname|given|names|data|date|"
    r"termin|numer|seria|document|number|expiry|expires|issued|valid|birth|urodzenia|wa[zż]no)",
)


def _parse_date_candidates(text: str) -> list[str]:
    """Collect date-like substrings in order of appearance."""
    return _DATE_RE.findall(text)


def _is_label_line(line: str) -> bool:
    """Heuristic: line is a field label (e.g. 'NAZWISKO / SURNAME') not a value."""
    s = line.strip()
    if not s:
        return True
    if "/" in s and len(s) < 80 and re.search(
        r"(?i)(name|sur|given|birth|date|number|numer|expiry|issued|valid|termin|data|urodz)",
        s,
    ):
        return True
    if _NAME_LINE_SKIP.match(s):
        return True
    return False


def _value_after_label(lines: list[str], label_regex: re.Pattern[str]) -> str:
    """Find a line matching label_regex; return value from same line or next 1–3 lines."""
    for i, line in enumerate(lines):
        if not label_regex.search(line):
            continue
        # Same line after label: colon or long dash
        same = re.split(r"[:：]\s*", line, maxsplit=1)
        if len(same) == 2 and same[1].strip():
            v = same[1].strip()
            if _DATE_RE.search(v) or (len(v) >= 2 and not _is_label_line(v)):
                return v.split()[0] if _DATE_RE.search(v) and len(v.split()) > 1 else v
        for j in range(i + 1, min(i + 4, len(lines))):
            cand = lines[j].strip()
            if not cand or _is_label_line(cand):
                continue
            return cand
    return ""


def _date_near_label(text: str, label_pattern: str) -> str:
    """Find date (DD.MM.YYYY or ISO) on same line or shortly after a label."""
    m = re.search(label_pattern, text, re.I | re.MULTILINE | re.DOTALL)
    if not m:
        return ""
    chunk = text[m.end() : m.end() + 120]
    dm = _DATE_RE.search(chunk)
    if dm:
        return dm.group(0)
    # Date on next line only
    for line in chunk.splitlines():
        line = line.strip()
        dm = _DATE_RE.search(line)
        if dm:
            return dm.group(0)
    return ""


def parse_id_fields(raw_text: str) -> dict[str, str]:
    """
    Parse OCR text into extension camelCase fields.
    Uses label-driven extraction (EN + PL) and strict document-number patterns.
    """
    out = empty_id_fields()
    text = raw_text.strip()
    if not text:
        return out

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    upper_blob = text.upper()

    # --- idType (avoid bare \\bID\\b) ---
    if re.search(r"\bDL\b|DRIVER\s*LIC|DRIVING\s*LIC", upper_blob):
        out["idType"] = "Driver License"
    elif "PASSPORT" in upper_blob:
        out["idType"] = "Passport"
    elif re.search(r"DOW[OÓ]D\s+OSOBISTY|IDENTITY\s*CARD", text, re.I):
        out["idType"] = "National ID Card"
    elif re.search(r"STATE\s*ID|IDENTIFICATION\s*CARD|IDENTITY\s*CARD", upper_blob):
        out["idType"] = "ID Card"

    # --- idNumber: structured labels first, then strict [A-Z]{2,4}\\d{6,9} ---
    doc_label = re.compile(
        r"(?i)(?:SERIA\s+I\s+NUMER|DOCUMENT\s*NUMBER|NUMER\s*DOKUMENTU|NUMER\s+DO|DOCUMENT\s*NO)",
    )
    for i, line in enumerate(lines):
        if doc_label.search(line):
            for j in range(i, min(i + 5, len(lines))):
                for cand in _DOC_NUMBER_RE.findall(lines[j]):
                    if cand.lower() not in _ID_NUMBER_FALSE_POSITIVE and len(cand) >= 6:
                        out["idNumber"] = cand.upper()
                        break
                if out["idNumber"]:
                    break
            break
    if not out["idNumber"]:
        for m in _DOC_NUMBER_RE.finditer(text):
            cand = m.group(0)
            if cand.lower() in _ID_NUMBER_FALSE_POSITIVE:
                continue
            if len(cand) >= 6:
                out["idNumber"] = cand.upper()
                break

    # --- Dates (labels: EN + PL) ---
    dob = _date_near_label(
        text,
        r"(?:DATA\s+URODZENIA|DATE\s*OF\s*BIRTH|DOB|BIRTH|URODZENIA)\s*[:\s/]*",
    )
    if dob:
        out["dateOfBirth"] = dob
    elif not out["dateOfBirth"]:
        out["dateOfBirth"] = _date_near_label(text, r"(?:DOB|D\.?O\.?B\.?)\s*[:\s]*")

    if not out["dateOfBirth"]:
        dates = _parse_date_candidates(text)
        if dates:
            out["dateOfBirth"] = dates[0]

    iss = _date_near_label(
        text,
        r"(?:DATA\s+WYDANIA|DATE\s*ISSUED|ISSUED|ISSUED\s*ON|ISSUE\s*DATE)\s*[:\s]*",
    )
    if iss:
        out["issueDate"] = iss

    # Do not use bare \\bEXP\\b — it matches inside unrelated words. Prefer full labels.
    exp = _date_near_label(
        text,
        r"(?:TERMIN\s+WA[ZŻ]NO[ŚS]CI|EXPIRY\s*DATE|DATE\s*OF\s*EXPIRY|EXPIRES\s*ON|VALID\s*THRU|VALID\s*UNTIL)\s*[:\s/]*",
    )
    if exp:
        out["expiryDate"] = exp

    dates = _parse_date_candidates(text)
    dob_val = out.get("dateOfBirth", "")

    if not out["issueDate"] and len(dates) >= 3:
        out["issueDate"] = dates[1]

    # OCR reading order can place DOB text in the window after EXPIRY — never keep expiry == DOB.
    if out.get("expiryDate") and dob_val and out["expiryDate"] == dob_val and len(dates) >= 2:
        out["expiryDate"] = ""
    if not out["expiryDate"] and len(dates) >= 2:
        for d in reversed(dates):
            if d != dob_val:
                out["expiryDate"] = d
                break
    elif not out["expiryDate"] and len(dates) == 1 and not dob_val:
        out["expiryDate"] = dates[0]

    # --- fullName: surname + given names (PL/EN labels), never country header ---
    surname_re = re.compile(r"(?i)^(NAZWISKO/|SURNAME|NAZWISKO)\s*")
    given_re = re.compile(r"(?i)^(IMIONA/|GIVEN\s*NAMES?|IMIONA|FORENAMES?)\s*")

    surname = ""
    given = ""
    for i, line in enumerate(lines):
        if surname_re.search(line):
            v = _value_after_label(lines[i:], surname_re)
            if v and not _NAME_LINE_SKIP.match(v) and len(v) > 1:
                surname = re.sub(r"\s+", " ", v.strip())
        if given_re.search(line):
            v = _value_after_label(lines[i:], given_re)
            if v and not _NAME_LINE_SKIP.match(v):
                given = re.sub(r"\s+", " ", v.strip())

    if given and surname:
        out["fullName"] = f"{given} {surname}".strip()
    elif surname:
        out["fullName"] = surname
    elif given:
        out["fullName"] = given

    if not out["fullName"]:
        for i, line in enumerate(lines):
            if re.match(
                r"(?i)^(name|full\s*name|cardholder|holder)\b",
                line,
            ) and i + 1 < len(lines):
                cand = lines[i + 1]
                if not _NAME_LINE_SKIP.match(cand):
                    out["fullName"] = cand.strip()
                    break

    if not out["fullName"]:
        for ln in lines:
            if _NAME_LINE_SKIP.match(ln) or len(ln) < 2:
                continue
            if re.fullmatch(r"[\d\s/.\-]+", ln):
                continue
            if 1 <= len(ln.split()) <= 6 and not re.fullmatch(r"^[A-Z]{3}\d{6,}$", ln):
                out["fullName"] = ln
                break

    # --- address ---
    addr_m = re.search(
        r"\b\d{1,6}\s+[A-Za-z0-9#.\'\s\-]+(?:Street|St\.?|Avenue|Ave\.?|Road|Rd\.?|"
        r"Drive|Dr\.?|Lane|Ln\.?|Blvd\.?|Court|Ct\.?|ul\.?)\b[^,\n]*"
        r"(?:,?\s*[A-Za-z0-9\s]+,?\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?)?",
        text,
        re.I,
    )
    if addr_m:
        out["address"] = " ".join(addr_m.group(0).split())
    else:
        for line in lines:
            low = line.lower()
            if re.match(r"\d+\s+\S", line) and ("," in line or "st" in low or "ave" in low or "rd" in low):
                out["address"] = line
                break

    return out


def _log_ocr_raw(raw_text: str) -> None:
    bar = "=" * 52
    logger.info("%s\n================ OCR RAW TEXT (Vision) ================\n%s\n%s", bar, raw_text or "(empty)", bar)


def _log_parsed_fields(data: dict[str, str]) -> None:
    bar = "=" * 44
    lines = "\n".join(f"  {k}: {v!r}" for k, v in data.items())
    logger.info("%s\nPARSED ID FIELDS (camelCase)\n%s\n%s", bar, lines, bar)


def _log_base64_full(b64: str) -> None:
    """Optional: log base64 in chunks (truncate with FDN_LOG_BASE64_MAX_CHARS)."""
    bar = "=" * 56
    max_total = int(os.environ.get("FDN_LOG_BASE64_MAX_CHARS", "0").strip() or "0")
    logger.info("%s", bar)
    logger.info("STEP: Image as Base64 (host output)")
    logger.info("%s", bar)
    logger.info("base64_character_count=%d", len(b64))
    if not b64:
        logger.info("(empty base64 string)")
        logger.info("%s", bar)
        return
    if max_total > 0 and len(b64) > max_total:
        logger.warning(
            "Truncating base64 log to first %d chars (set FDN_LOG_BASE64_MAX_CHARS=0 for full dump).",
            max_total,
        )
        payload = b64[:max_total]
    else:
        payload = b64
    chunk = int(os.environ.get("FDN_LOG_BASE64_CHUNK", "4096").strip() or "4096")
    for i in range(0, len(payload), chunk):
        logger.info("base64|%06d|%s", i, payload[i : i + chunk])
    if max_total > 0 and len(b64) > max_total:
        logger.info("... (%d characters omitted from log)", len(b64) - max_total)
    logger.info("%s", bar)


def scan_id(*, simulation_mode: bool, sample_path: Path | None = None) -> dict[str, Any]:
    """
    Load ID image, run Google Vision OCR, parse camelCase fields, return ``SCAN_RESULT``.

    When ``simulation_mode`` is False, acquires via TWAIN (``scanner_twain``), with
    simulation fallback when no device is available, then runs the same OCR pipeline.
    """
    path: Path
    image_b64: str

    if not simulation_mode:
        from scanner_twain import scan_image as twain_scan_image

        preferred = os.environ.get("FDN_TWAIN_PREFERRED", "QS2000").strip() or "QS2000"
        tw = twain_scan_image(
            preferred_substring=preferred,
            sample_path=sample_path or DEFAULT_SAMPLE_PATH,
        )
        if tw.get("type") == "ERROR":
            raise ScannerError(str(tw.get("message") or "Scanner not found or failed"))
        image_b64 = tw.get("image_base64") or ""
        if not image_b64:
            raise ScannerError("Scanner not found or failed")

        logger.info(
            "[scan] TWAIN source=%r simulated=%s (chars base64=%d)",
            tw.get("source_name"),
            tw.get("simulated"),
            len(image_b64),
        )

        path = _temp_image_path_from_base64(image_b64)
        try:
            logger.info("[scan] step 1/5 - image path for OCR: %s", path)
            size = path.stat().st_size
            logger.info("[scan] step 2/5 - image bytes on disk (%d)", size)
            logger.info("[scan] step 3/5 - Google Cloud Vision text_detection")
            raw_text = extract_text_with_google_vision(path)
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    else:
        path = sample_path or DEFAULT_SAMPLE_PATH
        logger.info("[scan] step 1/5 - resolve image path: %s", path)

        if not path.is_file():
            logger.error("[scan] step 1/5 - FAILED: file not found")
            raise ScannerError(f"Sample ID image not found: {path}")

        size = path.stat().st_size
        logger.info("[scan] step 2/5 - read image from disk (%d bytes)", size)

        logger.info("[scan] step 3/5 - Google Cloud Vision text_detection")
        raw_text = extract_text_with_google_vision(path)

    _log_ocr_raw(raw_text)

    logger.info("[scan] step 4/5 - parse ID fields from OCR text")
    ocr_data = parse_id_fields(raw_text)
    _log_parsed_fields(ocr_data)

    if simulation_mode:
        image_b64 = file_to_base64(path)

    logger.info("[scan] step 5/5 - base64 encode complete (length %d chars)", len(image_b64))

    result: dict[str, Any] = {
        "type": "SCAN_RESULT",
        "success": True,
        "image_base64": image_b64,
        "ocr_data": ocr_data,
    }
    for key, value in ocr_data.items():
        result[key] = value

    _log_base64_full(image_b64)

    return result


def scan_document_thales_sdk() -> dict[str, Any]:
    """
    Blocking Thales MMMReader session: wait for document, read, return structured fields.

    Does **not** use Google Cloud Vision. Use :func:`scan_id` when you need Vision + TWAIN/sample flow.

    Returns ``SDK_DOCUMENT_RESULT`` with ``document_data`` matching the Thales-oriented JSON shape
    (``first_name``, ``last_name``, ``document_number``, …).
    """
    from scanner_thales_sdk import read_document_safe

    out = read_document_safe()
    if out.get("type") == "ERROR":
        raise ScannerError(str(out.get("message") or "Thales SDK error"))

    structured = out.get("structured") or {}
    return {
        "type": "SDK_DOCUMENT_RESULT",
        "success": True,
        "sdk_engine": "thales_mmmreader",
        "document_data": structured,
        "codeline_raw": out.get("codeline_raw", ""),
        "codeline_data_raw": out.get("codeline_data_raw", ""),
        "aamva_raw": out.get("aamva_raw", ""),
        # Flatten common fields for clients that expect top-level keys:
        "first_name": structured.get("first_name", ""),
        "last_name": structured.get("last_name", ""),
        "document_number": structured.get("document_number", ""),
        "date_of_birth": structured.get("date_of_birth", ""),
        "gender": structured.get("gender", ""),
        "nationality": structured.get("nationality", ""),
        "expiry_date": structured.get("expiry_date", ""),
        "mrz_raw": structured.get("mrz_raw", ""),
        "barcode_data": structured.get("barcode_data", {}),
        "image_base64": out.get("visible_image_base64") or "",
    }


def auto_scan_document_push_payload(sdk_ok: dict[str, Any]) -> dict[str, Any]:
    """
    Build a message pushed to the extension when Thales auto-watch completes a read.

    ``sdk_ok`` must be the dict from :func:`scanner_thales_sdk.read_document_safe` with
    ``type == "SDK_DOCUMENT_OK"``.
    """
    if sdk_ok.get("type") != "SDK_DOCUMENT_OK":
        return {
            "type": "ERROR",
            "message": "auto_scan_document_push_payload: expected SDK_DOCUMENT_OK",
        }
    structured = sdk_ok.get("structured") or {}
    img_b64 = sdk_ok.get("visible_image_base64") or ""
    base = {
        "type": "AUTO_SCAN_RESULT",
        "success": True,
        "source": "thales_auto_watch",
        "sdk_engine": "thales_mmmreader",
        "document_data": structured,
        "codeline_raw": sdk_ok.get("codeline_raw", ""),
        "codeline_data_raw": sdk_ok.get("codeline_data_raw", ""),
        "aamva_raw": sdk_ok.get("aamva_raw", ""),
        "image_base64": img_b64,
        "first_name": structured.get("first_name", ""),
        "last_name": structured.get("last_name", ""),
        "document_number": structured.get("document_number", ""),
        "date_of_birth": structured.get("date_of_birth", ""),
        "gender": structured.get("gender", ""),
        "nationality": structured.get("nationality", ""),
        "expiry_date": structured.get("expiry_date", ""),
        "mrz_raw": structured.get("mrz_raw", ""),
        "barcode_data": structured.get("barcode_data", {}),
    }
    return base
