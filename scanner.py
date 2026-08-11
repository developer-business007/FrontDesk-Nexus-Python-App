"""ID scan: sample image + Google Cloud Vision OCR + camelCase fields for the extension."""

from __future__ import annotations

import base64
import copy
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any

from utils import file_to_base64

logger = logging.getLogger(__name__)

_HOST_DIR = Path(__file__).resolve().parent

# Two-sided Thales flow: first scan stored here until the second completes (see ``scan_document_thales_sdk``).
_two_sided_lock = threading.Lock()
_two_sided_buffer: dict[str, Any] | None = None

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
        "firstName": "",
        "middleName": "",
        "lastName": "",
        "dateOfBirth": "",
        "idNumber": "",
        "idType": "",
        "issueDate": "",
        "expiryDate": "",
        "address": "",
        "sex": "",
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

    # Lazy import — only needed when Vision OCR is actually called.
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
        from google.api_core import exceptions as google_exceptions  # noqa: PLC0415  # already imported above but re-bind for clarity
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
    if re.search(r"\bDL\b|DRIVER\s*LIC|DRIVING\s*LIC|\bDLN\b", upper_blob) or re.search(
        r"\bNEVADA\b.*\bDRIVER\b|\bDRIVER\b.*\bNEVADA\b",
        upper_blob,
    ):
        out["idType"] = "Driver License"
    elif "PASSPORT" in upper_blob:
        out["idType"] = "Passport"
    elif re.search(r"DOW[OÓ]D\s+OSOBISTY|IDENTITY\s*CARD", text, re.I):
        out["idType"] = "National ID Card"
    elif re.search(r"STATE\s*ID|IDENTIFICATION\s*CARD|IDENTITY\s*CARD", upper_blob):
        out["idType"] = "ID Card"
    elif re.search(r"\bNEVADA\b", upper_blob) and re.search(
        r"\bDRIVER\b|\bLICENSE\b|\bDL\b",
        upper_blob,
    ):
        out["idType"] = "Driver License"

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

    # --- US DMV / REAL ID field numbers (1=last, 2=first/middle, 4d=DL#) — common on state IDs ---
    if not out["idNumber"]:
        m4d = re.search(r"(?i)(?:^|\n)\s*4\s*d\s*[.)]?\s*[:\s]*(\d{8,12})\b", text)
        if m4d:
            out["idNumber"] = m4d.group(1)
    if not out["idNumber"]:
        mdl = re.search(
            r"(?i)(?:^|\n)\s*(?:DL|LIC(?:ENSE)?)\s*#?\s*[:\s]*(\d{8,12})\b",
            text,
        )
        if mdl:
            out["idNumber"] = mdl.group(1)

    m_last = re.search(
        r"(?is)(?:^|\n)\s*1\s*[.)]?\s*(?:\n|\r\n)+\s*([A-Z][A-Za-z'\-\s]{1,50})",
        text,
    )
    if m_last and not out.get("lastName"):
        out["lastName"] = re.sub(r"\s+", " ", m_last.group(1).strip())

    m_first = re.search(
        r"(?is)(?:^|\n)\s*2\s*[.)]?\s*(?:\n|\r\n)+\s*([A-Z][A-Za-z'\-\s]{1,50})",
        text,
    )
    if m_first and not out.get("firstName"):
        out["firstName"] = re.sub(r"\s+", " ", m_first.group(1).strip())

    if (out.get("firstName") or out.get("lastName")) and not out["fullName"]:
        out["fullName"] = f"{out.get('firstName', '')} {out.get('lastName', '')}".strip()

    # --- AAMVA-style numbered fields on US licenses (common on REAL ID layouts) ---
    if not out["dateOfBirth"]:
        m3 = re.search(
            r"(?is)(?:^|\n)\s*3\s*[.)]?\s*(?:\n|\r\n)+\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})",
            text,
        )
        if m3:
            out["dateOfBirth"] = m3.group(1)
    if not out["issueDate"]:
        m4a = re.search(
            r"(?is)(?:^|\n)\s*4\s*a\s*[.)]?\s*(?:\n|\r\n)+\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})",
            text,
        )
        if m4a:
            out["issueDate"] = m4a.group(1)
    if not out["expiryDate"]:
        m4b = re.search(
            r"(?is)(?:^|\n)\s*4\s*b\s*[.)]?\s*(?:\n|\r\n)+\s*(\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4})",
            text,
        )
        if m4b:
            out["expiryDate"] = m4b.group(1)

    m15 = re.search(r"(?is)(?:^|\n)\s*15\s*[.)]?\s*[:\s]*\b([MFU])\b", text)
    if m15:
        out["sex"] = m15.group(1).upper()

    if not out["address"]:
        m8 = re.search(r"(?is)(?:^|\n)\s*8\s*[.)]?\s*(?:\n|\r\n)+\s*([0-9][^\n]{6,200})", text)
        if m8:
            line1 = re.sub(r"\s+", " ", m8.group(1).strip())
            tail = text[m8.end() : m8.end() + 250]
            mcity = re.search(
                r"(?i)(?m)^\s*([A-Za-z][A-Za-z\s]{2,40},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?)\s*$",
                tail,
            )
            out["address"] = f"{line1}, {mcity.group(1).strip()}" if mcity else line1

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

    if not out["address"]:
        m_ct = re.search(
            r"\b\d{2,5}\s+[A-Za-z0-9#.'\s\-]+(?:CT|ST|AVE|RD|DR|LN|BLVD|WAY|COURT)\b[^,\n]*"
            r"\s*,?\s*(?:\n\s*)?[A-Za-z\s]{2,40},\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?",
            text,
            re.I | re.S,
        )
        if m_ct:
            out["address"] = " ".join(m_ct.group(0).split())

    # --- last resort: 10-digit US DL numbers (e.g. Nevada) ---
    if not out["idNumber"]:
        for m in re.finditer(r"\b(\d{10})\b", text):
            n = m.group(1)
            if n.startswith("0000000"):
                continue
            out["idNumber"] = n
            break

    return out


def _merge_vision_ocr_into_thales_structured(
    structured: dict[str, Any],
    ocr: dict[str, str],
    *,
    prefer_vision: bool = True,
) -> None:
    """Merge Vision ``parse_id_fields`` output into Thales ``structured`` (snake_case)."""
    if not ocr:
        return

    def _empty(k: str) -> bool:
        return not (str(structured.get(k) or "").strip())

    def _set(key: str, *, from_ocr: str) -> None:
        v = (from_ocr or "").strip()
        if not v:
            return
        if prefer_vision:
            structured[key] = v
        elif _empty(key):
            structured[key] = v

    _set("document_number", from_ocr=ocr.get("idNumber", ""))
    _set("first_name", from_ocr=ocr.get("firstName", ""))
    _set("middle_name", from_ocr=ocr.get("middleName", ""))
    _set("last_name", from_ocr=ocr.get("lastName", ""))

    has_name_pair = (ocr.get("firstName") or "").strip() and (ocr.get("lastName") or "").strip()
    if (
        (ocr.get("fullName") or "").strip()
        and not has_name_pair
        and (prefer_vision or _empty("first_name") or _empty("last_name"))
    ):
        fn_parts = ocr["fullName"].strip().split()
        if len(fn_parts) >= 2:
            if prefer_vision or _empty("last_name"):
                structured["last_name"] = fn_parts[-1]
            if prefer_vision or _empty("first_name"):
                structured["first_name"] = " ".join(fn_parts[:-1])
        elif len(fn_parts) == 1 and (prefer_vision or (_empty("first_name") and _empty("last_name"))):
            structured["first_name"] = fn_parts[0]

    _set("date_of_birth", from_ocr=ocr.get("dateOfBirth", ""))
    _set("expiry_date", from_ocr=ocr.get("expiryDate", ""))
    _set("issue_date", from_ocr=ocr.get("issueDate", ""))
    _set("address", from_ocr=ocr.get("address", ""))
    _set("document_type", from_ocr=ocr.get("idType", ""))

    sx = (ocr.get("sex") or "").strip().upper()[:1]
    if sx in ("M", "F", "U"):
        _set("gender", from_ocr=sx)

    fn = (structured.get("first_name") or "").strip()
    mid = (structured.get("middle_name") or "").strip()
    ln = (structured.get("last_name") or "").strip()
    if fn or mid or ln:
        structured["full_name"] = " ".join(x for x in (fn, mid, ln) if x).strip()
    elif (ocr.get("fullName") or "").strip():
        structured["full_name"] = ocr["fullName"].strip()

    bd = structured.get("barcode_data")
    if not isinstance(bd, dict):
        bd = {}
        structured["barcode_data"] = bd
    bd["vision_ocr_fallback"] = True


def sync_structured_document_fields_for_extension(structured: dict[str, Any]) -> None:
    """
    Ensure ``document_data`` includes **both** snake_case and camelCase keys so Chrome
    extensions can bind ``fullName`` / ``idNumber`` / … without a separate mapping layer.
    """
    fn = (structured.get("first_name") or "").strip()
    mid = (structured.get("middle_name") or "").strip()
    ln = (structured.get("last_name") or "").strip()
    full = (structured.get("full_name") or "").strip() or " ".join(x for x in (fn, mid, ln) if x).strip()
    structured["fullName"] = full
    structured["firstName"] = fn
    structured["middleName"] = mid
    structured["lastName"] = ln
    structured["dateOfBirth"] = (structured.get("date_of_birth") or "").strip()
    structured["idNumber"] = (structured.get("document_number") or "").strip()
    structured["idType"] = (structured.get("document_type") or "").strip()
    structured["issueDate"] = (structured.get("issue_date") or "").strip()
    structured["expiryDate"] = (structured.get("expiry_date") or "").strip()
    structured["address"] = (structured.get("address") or "").strip()
    structured["streetAddress"] = (structured.get("street_address") or "").strip()
    structured["city"] = (structured.get("city") or "").strip()
    structured["state"] = (structured.get("state") or "").strip()
    structured["postalCode"] = (structured.get("postal_code") or "").strip()
    g = (structured.get("gender") or "").strip()
    structured["sex"] = g


def _sdk_ok_has_barcode_id_data(sdk_ok: dict[str, Any]) -> bool:
    """True when PDF417/AAMVA produced usable fields — Vision must not overwrite these."""
    st = sdk_ok.get("structured")
    if not isinstance(st, dict):
        return False
    bd = st.get("barcode_data")
    if isinstance(bd, dict) and any(
        (bd.get(k) or "").strip() for k in ("DAQ", "DCS", "DAC", "DBB", "DBA", "DAG", "DAL")
    ):
        return True
    raw = (sdk_ok.get("aamva_raw") or "").strip()
    return len(raw) >= 32


def _sdk_ok_barcode_complete(sdk_ok: dict[str, Any]) -> bool:
    """True when AAMVA-derived identity + dates look complete (skip Vision API if not ``vision_always``)."""
    if not _sdk_ok_has_barcode_id_data(sdk_ok):
        return False
    st = sdk_ok.get("structured")
    if not isinstance(st, dict):
        return False
    return bool(
        (st.get("document_number") or "").strip()
        and (st.get("last_name") or "").strip()
        and (st.get("first_name") or "").strip()
        and (st.get("date_of_birth") or "").strip()
        and (st.get("expiry_date") or "").strip()
    )


def enrich_thales_sdk_ok_with_vision(sdk_ok: dict[str, Any]) -> None:
    """
    Run Google Cloud Vision on Thales' visible snapshot and merge printed-field OCR into
    ``structured`` so the extension receives **complete** ID data (US DL front, etc.).

    - ``FDN_THALES_VISION_OCR_FALLBACK=0`` — disable Vision entirely (SDK-only text).
    - ``FDN_THALES_VISION_OCR_MODE=barcode_first`` (default) — PDF417/AAMVA (back) wins; Vision only fills
      gaps or supplies data when no barcode. ``prefer_vision`` forces Vision to overwrite. ``fill_empty_only``
      never overwrites existing structured fields.
    - ``FDN_THALES_VISION_ALWAYS=1`` (default) — run Vision when image exists (respecting barcode_first).
      ``0`` skips Vision when barcode data looks **complete** (saves API calls).
    """
    if sdk_ok.get("type") != "SDK_DOCUMENT_OK":
        return
    if os.environ.get("FDN_THALES_VISION_OCR_FALLBACK", "1").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        st = sdk_ok.get("structured")
        if not isinstance(st, dict):
            st = {}
            sdk_ok["structured"] = st
        sync_structured_document_fields_for_extension(st)
        return

    structured = sdk_ok.get("structured")
    if not isinstance(structured, dict):
        structured = {}
        sdk_ok["structured"] = structured

    img = (sdk_ok.get("visible_image_base64") or "").strip()
    if not img:
        sync_structured_document_fields_for_extension(structured)
        return

    mode = os.environ.get("FDN_THALES_VISION_OCR_MODE", "barcode_first").strip().lower()
    if mode == "barcode_first":
        prefer_vision = not _sdk_ok_has_barcode_id_data(sdk_ok)
    elif mode == "prefer_vision":
        prefer_vision = True
    elif mode in ("fill_empty_only", "fill_empty", "merge_empty"):
        prefer_vision = False
    else:
        prefer_vision = not _sdk_ok_has_barcode_id_data(sdk_ok)

    vision_always = os.environ.get("FDN_THALES_VISION_ALWAYS", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not vision_always and _sdk_ok_barcode_complete(sdk_ok):
        sync_structured_document_fields_for_extension(structured)
        return

    path: Path | None = None
    try:
        raw_text = ""
        try:
            path = _temp_image_path_from_base64(img)
            raw_text = extract_text_with_google_vision(path)
        except ScannerError as exc:
            logger.warning("Thales Vision OCR: skipped (%s)", exc)
            sync_structured_document_fields_for_extension(structured)
            return
        if len(raw_text.strip()) < 8:
            logger.info("Thales Vision OCR: almost no text returned (len=%s).", len(raw_text))
            sync_structured_document_fields_for_extension(structured)
            return

        ocr = parse_id_fields(raw_text)
        if not any(
            (
                ocr.get("idNumber"),
                ocr.get("fullName"),
                ocr.get("lastName"),
                ocr.get("firstName"),
                ocr.get("address"),
                ocr.get("dateOfBirth"),
            ),
        ):
            logger.warning(
                "Thales Vision OCR: no ID fields parsed (raw text length=%s). First line: %r",
                len(raw_text),
                raw_text.splitlines()[0][:120] if raw_text.splitlines() else "",
            )

        _merge_vision_ocr_into_thales_structured(structured, ocr, prefer_vision=prefer_vision)
        sdk_ok["vision_ocr_fallback"] = True
        sync_structured_document_fields_for_extension(structured)
        logger.info(
            "Thales Vision OCR merged (mode=%s, document_number=%r, idNumber=%r, fullName=%r).",
            mode,
            structured.get("document_number", ""),
            structured.get("idNumber", ""),
            structured.get("fullName", ""),
        )
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


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


def normalize_front_image_b64(b64: str) -> str:
    """Auto-rotate portrait/front ID image upright (Thales raw bitmap orientation)."""
    from image_orientation import correct_id_front_image_b64

    return correct_id_front_image_b64(b64)


def _image_slots_for_single_capture(img: str, detected_side: str) -> tuple[str, str]:
    """Map one visible snapshot into ``(image_front_base64, image_back_base64)`` for a stable extension contract."""
    s = (img or "").strip()
    if not s:
        return "", ""
    if detected_side == "back":
        return "", s
    if detected_side == "front":
        return normalize_front_image_b64(s), ""
    return normalize_front_image_b64(s), ""


def infer_document_side(sdk_ok: dict[str, Any]) -> tuple[str, str]:
    """
    Best-effort **front vs back** for a single Thales read.

    US DL: PDF417/AAMVA is almost always on the **back**; the front has portrait + printed text.
    Travel MRZ: long ICAO line with ``<`` often on the **data page** (treated as ``front`` here).
    """
    if sdk_ok.get("type") != "SDK_DOCUMENT_OK":
        return "unknown", "low"
    aamva = (sdk_ok.get("aamva_raw") or "").strip()
    st = sdk_ok.get("structured") if isinstance(sdk_ok.get("structured"), dict) else {}
    bd = st.get("barcode_data") if isinstance(st.get("barcode_data"), dict) else {}
    if (
        len(aamva) >= 20
        or bd.get("source") == "pdf417_aamva"
        or any((bd.get(k) or "").strip() for k in ("DAQ", "DCS", "DAC", "DBB", "DBA"))
    ):
        return "back", "high"
    if len(aamva) >= 8:
        return "back", "medium"
    mrz = (st.get("mrz_raw") or sdk_ok.get("codeline_raw") or "").strip()
    if len(mrz) >= 30 and "<" in mrz:
        return "front", "medium"
    if not aamva and not any((bd.get(k) or "").strip() for k in ("DAQ", "DCS", "DAC")):
        return "front", "medium"
    return "unknown", "low"


def _split_back_front(c1: dict[str, Any], c2: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    """
    Given two successful SDK captures (in scan order), return ``(back_capture, front_capture, note)``.
    """
    s1, _ = infer_document_side(c1)
    s2, _ = infer_document_side(c2)
    if s1 == "back" and s2 != "back":
        return c1, c2, "scan1_back_scan2_front"
    if s2 == "back" and s1 != "back":
        return c2, c1, "scan2_back_scan1_front"
    if s1 == "back" and s2 == "back":
        l1 = len((c1.get("aamva_raw") or "").strip())
        l2 = len((c2.get("aamva_raw") or "").strip())
        if l1 >= l2:
            return c1, c2, "both_back_prefer_first"
        return c2, c1, "both_back_prefer_second"
    b1 = _sdk_ok_has_barcode_id_data(c1)
    b2 = _sdk_ok_has_barcode_id_data(c2)
    if b1 and not b2:
        return c1, c2, "barcode_hint_first"
    if b2 and not b1:
        return c2, c1, "barcode_hint_second"
    return c1, c2, "order_first_front_second_back_fallback"


def _merge_structured_id_fields(back_structured: dict[str, Any], front_structured: dict[str, Any]) -> dict[str, Any]:
    """Prefer barcode/back fields; fill gaps from the other side (front OCR, etc.)."""
    out = copy.deepcopy(back_structured) if back_structured else {}
    keys = (
        "first_name",
        "middle_name",
        "last_name",
        "document_number",
        "date_of_birth",
        "gender",
        "nationality",
        "expiry_date",
        "issue_date",
        "address",
        "street_address",
        "city",
        "state",
        "postal_code",
        "document_type",
        "full_name",
        "mrz_raw",
    )
    for k in keys:
        if not (out.get(k) or "").strip() and (front_structured.get(k) or "").strip():
            out[k] = front_structured[k]
    pb = out.get("barcode_data") if isinstance(out.get("barcode_data"), dict) else {}
    sb = front_structured.get("barcode_data") if isinstance(front_structured.get("barcode_data"), dict) else {}
    if not any((pb.get(x) or "").strip() for x in ("DAQ", "DCS", "DAC", "DBB", "DBA")) and sb:
        out["barcode_data"] = copy.deepcopy(sb)
    elif not pb and sb:
        out["barcode_data"] = copy.deepcopy(sb)
    sync_structured_document_fields_for_extension(out)
    return out


def _merge_thales_sdk_outputs(back_ok: dict[str, Any], front_ok: dict[str, Any]) -> dict[str, Any]:
    """Build one synthetic ``SDK_DOCUMENT_OK``-shaped dict for the merged result."""
    bs = back_ok.get("structured") if isinstance(back_ok.get("structured"), dict) else {}
    fs = front_ok.get("structured") if isinstance(front_ok.get("structured"), dict) else {}
    merged_struct = _merge_structured_id_fields(bs, fs)
    a_b = (back_ok.get("aamva_raw") or "").strip()
    a_f = (front_ok.get("aamva_raw") or "").strip()
    aamva = a_b if len(a_b) >= len(a_f) else a_f
    cr_b = (back_ok.get("codeline_raw") or "").strip()
    cr_f = (front_ok.get("codeline_raw") or "").strip()
    codeline_raw = cr_b if len(cr_b) >= len(cr_f) else cr_f
    if not codeline_raw:
        codeline_raw = cr_f or cr_b
    cd_b = (back_ok.get("codeline_data_raw") or "").strip()
    cd_f = (front_ok.get("codeline_data_raw") or "").strip()
    codeline_data_raw = cd_b if len(cd_b) >= len(cd_f) else cd_f
    if not codeline_data_raw:
        codeline_data_raw = cd_f or cd_b
    return {
        "type": "SDK_DOCUMENT_OK",
        "structured": merged_struct,
        "aamva_raw": aamva,
        "codeline_raw": codeline_raw,
        "codeline_data_raw": codeline_data_raw,
        "vision_ocr_fallback": bool(back_ok.get("vision_ocr_fallback"))
        or bool(front_ok.get("vision_ocr_fallback")),
        "visible_image_base64": (front_ok.get("visible_image_base64") or "").strip()
        or (back_ok.get("visible_image_base64") or "").strip(),
    }


def _build_sdk_document_result_payload(
    out: dict[str, Any],
    *,
    two_sided: bool = False,
    front_image_base64: str = "",
    back_image_base64: str = "",
    side_assignment: str = "",
) -> dict[str, Any]:
    structured = out.get("structured") or {}
    primary = front_image_base64 or (out.get("visible_image_base64") or "")
    if not two_sided:
        primary = out.get("visible_image_base64") or ""
    base: dict[str, Any] = {
        "type": "SDK_DOCUMENT_RESULT",
        "success": True,
        "sdk_engine": "thales_mmmreader",
        "document_data": structured,
        "codeline_raw": out.get("codeline_raw", ""),
        "codeline_data_raw": out.get("codeline_data_raw", ""),
        "aamva_raw": out.get("aamva_raw", ""),
        "vision_ocr_fallback": bool(out.get("vision_ocr_fallback")),
        "first_name": structured.get("first_name", ""),
        "middle_name": structured.get("middle_name", ""),
        "last_name": structured.get("last_name", ""),
        "document_number": structured.get("document_number", ""),
        "date_of_birth": structured.get("date_of_birth", ""),
        "gender": structured.get("gender", ""),
        "nationality": structured.get("nationality", ""),
        "expiry_date": structured.get("expiry_date", ""),
        "issue_date": structured.get("issue_date", ""),
        "address": structured.get("address", ""),
        "street_address": structured.get("street_address", ""),
        "city": structured.get("city", ""),
        "state": structured.get("state", ""),
        "postal_code": structured.get("postal_code", ""),
        "document_type": structured.get("document_type", ""),
        "full_name": structured.get("full_name", ""),
        "fullName": structured.get("fullName", ""),
        "middleName": structured.get("middleName", ""),
        "dateOfBirth": structured.get("dateOfBirth", ""),
        "idNumber": structured.get("idNumber", ""),
        "idType": structured.get("idType", ""),
        "issueDate": structured.get("issueDate", ""),
        "expiryDate": structured.get("expiryDate", ""),
        "streetAddress": structured.get("streetAddress", ""),
        "postalCode": structured.get("postalCode", ""),
        "sex": structured.get("sex", ""),
        "mrz_raw": structured.get("mrz_raw", ""),
        "barcode_data": structured.get("barcode_data", {}),
        "image_base64": primary,
    }
    if two_sided:
        fi = normalize_front_image_b64((front_image_base64 or "").strip())
        bi = (back_image_base64 or "").strip()
        base["two_sided"] = True
        base["image_base64"] = fi or primary
        base["image_front_base64"] = fi
        base["image_back_base64"] = bi
        base["front_image_base64"] = fi
        base["back_image_base64"] = bi
        base["side_assignment"] = side_assignment
    else:
        base["image_front_base64"] = primary
        base["image_back_base64"] = ""
    return base


def _build_sdk_document_side_result_payload(
    out: dict[str, Any],
    *,
    detected_side: str,
    side_confidence: str,
    side_assignment_note: str,
) -> dict[str, Any]:
    structured = out.get("structured") or {}
    img = out.get("visible_image_base64") or ""
    img_front, img_back = _image_slots_for_single_capture(img, detected_side)
    return {
        "type": "SDK_DOCUMENT_SIDE_RESULT",
        "success": True,
        "session_pending": True,
        "detected_side": detected_side,
        "side_confidence": side_confidence,
        "side_assignment_note": side_assignment_note,
        "sdk_engine": "thales_mmmreader",
        "document_data": structured,
        "codeline_raw": out.get("codeline_raw", ""),
        "codeline_data_raw": out.get("codeline_data_raw", ""),
        "aamva_raw": out.get("aamva_raw", ""),
        "vision_ocr_fallback": bool(out.get("vision_ocr_fallback")),
        "first_name": structured.get("first_name", ""),
        "middle_name": structured.get("middle_name", ""),
        "last_name": structured.get("last_name", ""),
        "document_number": structured.get("document_number", ""),
        "date_of_birth": structured.get("date_of_birth", ""),
        "gender": structured.get("gender", ""),
        "nationality": structured.get("nationality", ""),
        "expiry_date": structured.get("expiry_date", ""),
        "issue_date": structured.get("issue_date", ""),
        "address": structured.get("address", ""),
        "street_address": structured.get("street_address", ""),
        "city": structured.get("city", ""),
        "state": structured.get("state", ""),
        "postal_code": structured.get("postal_code", ""),
        "document_type": structured.get("document_type", ""),
        "full_name": structured.get("full_name", ""),
        "fullName": structured.get("fullName", ""),
        "middleName": structured.get("middleName", ""),
        "dateOfBirth": structured.get("dateOfBirth", ""),
        "idNumber": structured.get("idNumber", ""),
        "idType": structured.get("idType", ""),
        "issueDate": structured.get("issueDate", ""),
        "expiryDate": structured.get("expiryDate", ""),
        "streetAddress": structured.get("streetAddress", ""),
        "postalCode": structured.get("postalCode", ""),
        "sex": structured.get("sex", ""),
        "mrz_raw": structured.get("mrz_raw", ""),
        "barcode_data": structured.get("barcode_data", {}),
        "image_base64": img,
        "image_front_base64": img_front,
        "image_back_base64": img_back,
    }


def scan_document_thales_sdk(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Blocking Thales MMMReader session: wait for document, read, return structured fields.

    When the SDK returns only a visible image (common for **US DL front**: no MRZ, PDF417 on back),
    optionally runs Google Vision on that image and merges OCR — see :func:`enrich_thales_sdk_ok_with_vision`.

    **Single-sided (default):** returns ``SDK_DOCUMENT_RESULT`` with ``image_base64`` (legacy)
    and ``image_front_base64`` / ``image_back_base64`` (back empty).

    **Two-sided:** send ``{"type": "SCAN_DOCUMENT_SDK", "two_sided": true}`` for each scan.
    The first successful read returns ``SDK_DOCUMENT_SIDE_RESULT`` (``session_pending`` true);
    the second returns ``SDK_DOCUMENT_RESULT`` with ``image_front_base64``, ``image_back_base64``,
    merged ``document_data``, and ``image_base64`` set to the front image for compatibility.
    ``front_image_base64`` / ``back_image_base64`` remain as aliases of the same strings.

    Send ``cancel_two_sided: true`` to clear a pending first side without scanning.

    **Requires** ``FDN_THALES_FETCH_VISIBLE_IMAGE`` so each read includes a visible snapshot.
    """
    global _two_sided_buffer
    from scanner_thales_sdk import read_document_safe

    payload = payload or {}
    if payload.get("cancel_two_sided"):
        with _two_sided_lock:
            _two_sided_buffer = None
        return {
            "type": "SDK_TWO_SIDED_CANCELLED",
            "success": True,
            "message": "Two-sided session cleared.",
        }

    def _run_read() -> dict[str, Any]:
        out = read_document_safe()
        if out.get("type") == "ERROR":
            raise ScannerError(str(out.get("message") or "Thales SDK error"))
        if out.get("type") == "NO_DOCUMENT":
            raise ScannerError(str(out.get("message") or "No document on reader (timed out)."))
        if out.get("type") == "SDK_DOCUMENT_OK":
            enrich_thales_sdk_ok_with_vision(out)
        return out

    two_sided = bool(payload.get("two_sided"))

    if not two_sided:
        with _two_sided_lock:
            _two_sided_buffer = None
        out = _run_read()
        return _build_sdk_document_result_payload(out)

    with _two_sided_lock:
        pending = _two_sided_buffer

    if pending is None:
        out = _run_read()
        side, conf = infer_document_side(out)
        note = (
            "pdf417_aamva_or_barcode_fields"
            if side == "back"
            else ("mrz_or_printed_face_side" if side == "front" else "weak_signal")
        )
        with _two_sided_lock:
            _two_sided_buffer = copy.deepcopy(out)
        return _build_sdk_document_side_result_payload(
            out,
            detected_side=side,
            side_confidence=conf,
            side_assignment_note=note,
        )

    second = _run_read()
    first = pending
    with _two_sided_lock:
        _two_sided_buffer = None

    back_cap, front_cap, assign_note = _split_back_front(first, second)
    merged = _merge_thales_sdk_outputs(back_cap, front_cap)
    fi = (front_cap.get("visible_image_base64") or "").strip()
    bi = (back_cap.get("visible_image_base64") or "").strip()
    return _build_sdk_document_result_payload(
        merged,
        two_sided=True,
        front_image_base64=fi,
        back_image_base64=bi,
        side_assignment=assign_note,
    )


def scan_document_ambir_sdk(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    AMBIR DocketPORT scan: acquire image → Google Vision OCR → structured fields.

    Output shape is identical to scan_document_thales_sdk (SDK_DOCUMENT_RESULT) so
    the Chrome extension needs no changes to consume AMBIR results.

    Raises ScannerError on any failure (handled by the main dispatch loop).
    """
    from scanner_ambir_sdk import scan_document_safe as _ambir_safe

    out = _ambir_safe()

    if out.get("type") == "ERROR":
        raise ScannerError(str(out.get("message") or "AMBIR scan error"))
    if out.get("type") == "NO_DOCUMENT":
        raise ScannerError(str(out.get("message") or "No document detected — timeout waiting for paper."))

    image_b64 = (out.get("image_base64") or "").strip()
    if not image_b64:
        raise ScannerError("AMBIR scan returned no image data.")

    # Run Google Vision OCR on the scanned image (same pipeline as TWAIN path)
    raw_text = ""
    ocr_path: Path | None = None
    try:
        ocr_path = _temp_image_path_from_base64(image_b64)
        raw_text = extract_text_with_google_vision(ocr_path)
        logger.info(
            "AMBIR Vision OCR: %d chars extracted from %s scan",
            len(raw_text), out.get("model", ""),
        )
    except ScannerError as exc:
        logger.warning("AMBIR Vision OCR failed: %s — returning image without parsed fields", exc)
    finally:
        if ocr_path is not None:
            try:
                ocr_path.unlink(missing_ok=True)
            except OSError:
                pass

    ocr_data = parse_id_fields(raw_text) if raw_text else empty_id_fields()
    _log_parsed_fields(ocr_data)

    # Build structured dict in snake_case (Thales convention) + sync camelCase aliases
    structured: dict[str, Any] = {
        "first_name":      ocr_data.get("firstName", ""),
        "middle_name":     ocr_data.get("middleName", ""),
        "last_name":       ocr_data.get("lastName", ""),
        "full_name":       ocr_data.get("fullName", ""),
        "document_number": ocr_data.get("idNumber", ""),
        "document_type":   ocr_data.get("idType", ""),
        "date_of_birth":   ocr_data.get("dateOfBirth", ""),
        "expiry_date":     ocr_data.get("expiryDate", ""),
        "issue_date":      ocr_data.get("issueDate", ""),
        "address":         ocr_data.get("address", ""),
        "gender":          ocr_data.get("sex", ""),
        "nationality":     "",
        "street_address":  "",
        "city":            "",
        "state":           "",
        "postal_code":     "",
        "mrz_raw":         "",
        "barcode_data":    {"source": "ambir_vision_ocr"},
    }
    sync_structured_document_fields_for_extension(structured)

    return {
        "type":              "SDK_DOCUMENT_RESULT",
        "success":           True,
        "sdk_engine":        "ambir_docketport",
        "ambir_model":       out.get("model", ""),
        "vision_ocr_fallback": True,
        "document_data":     structured,
        # flat fields (extension reads these at top level)
        "first_name":        structured.get("first_name", ""),
        "middle_name":       structured.get("middle_name", ""),
        "last_name":         structured.get("last_name", ""),
        "full_name":         structured.get("full_name", ""),
        "document_number":   structured.get("document_number", ""),
        "document_type":     structured.get("document_type", ""),
        "date_of_birth":     structured.get("date_of_birth", ""),
        "expiry_date":       structured.get("expiry_date", ""),
        "issue_date":        structured.get("issue_date", ""),
        "address":           structured.get("address", ""),
        "gender":            structured.get("gender", ""),
        "nationality":       "",
        "street_address":    structured.get("street_address", ""),
        "city":              structured.get("city", ""),
        "state":             structured.get("state", ""),
        "postal_code":       structured.get("postal_code", ""),
        "mrz_raw":           "",
        "barcode_data":      structured.get("barcode_data", {}),
        # camelCase aliases for extension compatibility
        "fullName":          structured.get("fullName", ""),
        "firstName":         structured.get("firstName", ""),
        "middleName":        structured.get("middleName", ""),
        "lastName":          structured.get("lastName", ""),
        "dateOfBirth":       structured.get("dateOfBirth", ""),
        "idNumber":          structured.get("idNumber", ""),
        "idType":            structured.get("idType", ""),
        "issueDate":         structured.get("issueDate", ""),
        "expiryDate":        structured.get("expiryDate", ""),
        "streetAddress":     structured.get("streetAddress", ""),
        "postalCode":        structured.get("postalCode", ""),
        "sex":               structured.get("sex", ""),
        # image slots
        "image_base64":      image_b64,
        "image_front_base64": image_b64,
        "image_back_base64":  "",
        "codeline_raw":      "",
        "codeline_data_raw": "",
        "aamva_raw":         "",
    }


def get_device_status() -> dict[str, Any]:
    """
    Return connectivity hints for TWAIN, Thales SDK, and AMBIR SDK.

    - **TWAIN**: ``twain_sources`` non-empty ⇒ TWAIN DSM sees at least one source.
    - **Thales**: ``thales_dll_load_ok`` ⇒ MMMReaderHighLevelAPI.dll loaded OK.
    - **AMBIR**: ``ambir_available`` ⇒ DPORT*.dll found and loadable.
      ``ambir_hw_ok`` ⇒ scanner also responded on USB (confirmed by SI_OpenInterface).
    USB/device state is only certain after a successful scan (use logs for diagnosis).
    """
    from scanner_twain import list_scanners

    twain_sources: list[str] = []
    try:
        twain_sources = list_scanners()
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_device_status: list_scanners failed: %s", exc)

    thales_paths = _HOST_DIR / "config" / "thales_paths.ini"
    app_ini = _HOST_DIR / "config" / "Application.ini"

    try:
        from scanner_thales_sdk import probe_thales_sdk
        thales = probe_thales_sdk()
    except Exception as exc:  # noqa: BLE001
        thales = {"dll_load_ok": False, "detail": str(exc)}

    try:
        from scanner_ambir_sdk import probe_ambir_sdk
        ambir = probe_ambir_sdk()
    except Exception as exc:  # noqa: BLE001
        ambir = {"available": False, "hw_ok": False, "model": "", "dll_path": "", "detail": str(exc)}

    return {
        "type": "DEVICE_STATUS",
        # TWAIN
        "twain_sources": twain_sources,
        "twain_has_source": len(twain_sources) > 0,
        # Thales QS2000
        "config_thales_paths_ini_exists": thales_paths.is_file(),
        "config_application_ini_exists": app_ini.is_file(),
        "thales_dll_load_ok": bool(thales.get("dll_load_ok")),
        "thales_detail": thales.get("detail", ""),
        # AMBIR DocketPORT
        "ambir_available": bool(ambir.get("available")),
        "ambir_hw_ok": bool(ambir.get("hw_ok")),
        "ambir_model": ambir.get("model", ""),
        "ambir_dll_path": ambir.get("dll_path", ""),
        "ambir_detail": ambir.get("detail", ""),
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
    enrich_thales_sdk_ok_with_vision(sdk_ok)
    structured = sdk_ok.get("structured") or {}
    # CD_IMAGEVIS (front) and CD_IMAGEVISREAR (back) are both captured in one ReadDocument pass.
    img_front = normalize_front_image_b64((sdk_ok.get("visible_image_base64") or "").strip())
    img_back = (sdk_ok.get("visible_image_rear_base64") or "").strip()
    # Legacy single-image key: use front if available, else back.
    img_b64 = img_front or img_back
    base = {
        "type": "AUTO_SCAN_RESULT",
        "success": True,
        "source": "thales_auto_watch",
        "sdk_engine": "thales_mmmreader",
        "vision_ocr_fallback": bool(sdk_ok.get("vision_ocr_fallback")),
        "document_data": structured,
        "codeline_raw": sdk_ok.get("codeline_raw", ""),
        "codeline_data_raw": sdk_ok.get("codeline_data_raw", ""),
        "aamva_raw": sdk_ok.get("aamva_raw", ""),
        "image_base64": img_b64,
        "image_front_base64": img_front,
        "image_back_base64": img_back,
        "front_image_base64": img_front,
        "back_image_base64": img_back,
        "first_name": structured.get("first_name", ""),
        "middle_name": structured.get("middle_name", ""),
        "last_name": structured.get("last_name", ""),
        "document_number": structured.get("document_number", ""),
        "date_of_birth": structured.get("date_of_birth", ""),
        "gender": structured.get("gender", ""),
        "nationality": structured.get("nationality", ""),
        "expiry_date": structured.get("expiry_date", ""),
        "issue_date": structured.get("issue_date", ""),
        "address": structured.get("address", ""),
        "street_address": structured.get("street_address", ""),
        "city": structured.get("city", ""),
        "state": structured.get("state", ""),
        "postal_code": structured.get("postal_code", ""),
        "document_type": structured.get("document_type", ""),
        "full_name": structured.get("full_name", ""),
        "fullName": structured.get("fullName", ""),
        "middleName": structured.get("middleName", ""),
        "dateOfBirth": structured.get("dateOfBirth", ""),
        "idNumber": structured.get("idNumber", ""),
        "idType": structured.get("idType", ""),
        "issueDate": structured.get("issueDate", ""),
        "expiryDate": structured.get("expiryDate", ""),
        "streetAddress": structured.get("streetAddress", ""),
        "postalCode": structured.get("postalCode", ""),
        "sex": structured.get("sex", ""),
        "mrz_raw": structured.get("mrz_raw", ""),
        "barcode_data": structured.get("barcode_data", {}),
    }
    return base
