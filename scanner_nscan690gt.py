"""
AMBIR nScan 690gt scan module.

NS690gt.DLL is image capture only (SI_*). There is no OCR/PDF417 in the 690gt SDK.
AmbirScan's Auto/Manual OCR is a separate app layer — we implement the equivalent here:

  1. Duplex capture via NS690gt.DLL
  2. Software PDF417 → AAMVA (US DL/ID) via zxing-cpp  — primary, structured fields
  3. Google Cloud Vision document OCR on front+back     — fallback (same as DocketPORT)
  4. Return SDK_DOCUMENT_RESULT / AUTO_SCAN_RESULT
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_HOST_DIR = Path(__file__).resolve().parent
_LOG_TAG = "[nScan690gt]"


def _log_step(msg: str, *args: Any) -> None:
    """Always-visible milestone lines in native-host.log (prefix for easy grepping)."""
    logger.info("%s " + msg, _LOG_TAG, *args)

# AAMVA DL/ID Design Standard element IDs used on US credentials (v1–v10).
# Include DDE/DDF/DDG so LF-separated values are not swallowed into the previous field.
_AAMVA_TAGS: tuple[str, ...] = (
    "DAA", "DAB", "DAC", "DAD", "DAG", "DAI", "DAJ", "DAK", "DAQ", "DAU", "DAY",
    "DBA", "DBB", "DBC", "DBD", "DBF",
    "DCA", "DCB", "DCD", "DCF", "DCG", "DCK", "DCS", "DCT", "DCU",
    "DDA", "DDB", "DDC", "DDD", "DDE", "DDF", "DDG", "DDH", "DDI", "DDJ", "DDK", "DDL",
    "ZNA", "ZNB", "ZNC",
)
_AAMVA_NONE = frozenset({"NONE", "UNAVAILABLE", "N/A", "NA", "NULL"})
# Chrome native-messaging host→extension max is 1 MiB. Stay under it or Chrome kills the pipe.
_CHROME_NATIVE_MAX_BYTES = 1024 * 1024
# Leave room for requestId / framing after we attach metadata in dispatch().
_CHROME_PAYLOAD_BUDGET = _CHROME_NATIVE_MAX_BYTES - 4096
_JPEG_ATTEMPTS: tuple[tuple[int, int], ...] = (
    (1280, 72),
    (1100, 62),
    (900, 55),
    (720, 48),
    (560, 42),
    (420, 36),
    (320, 32),
)


# ─────────────────────────────────────────────────────────────────────────────
# AAMVA PDF417 parser
# ─────────────────────────────────────────────────────────────────────────────

def _preview_barcode(raw: str, *, limit: int = 160) -> str:
    """Log-safe barcode preview (control chars escaped, truncated)."""
    out = []
    for ch in (raw or "")[:limit]:
        o = ord(ch)
        if ch in "\n\r\t" or 32 <= o < 127:
            out.append(ch if ch != "\n" else "\\n")
        else:
            out.append(f"\\x{o:02x}")
    extra = max(0, len(raw or "") - limit)
    return "".join(out) + (f"…(+{extra})" if extra else "")


def _normalize_aamva_raw(raw: str) -> str:
    """UTF-16 leftovers + AAMVA record separators → searchable text."""
    t = raw or ""
    if "\x00" in t:
        # zxing sometimes yields UTF-16LE as Latin-1
        try:
            t = t.encode("latin-1", errors="replace").decode("utf-16-le", errors="replace")
        except Exception:  # noqa: BLE001
            t = t.replace("\x00", "")
    for sep in ("\x1e", "\x1c", "\x1d", "\x0b", "\x0c"):
        t = t.replace(sep, "\n")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return t


def _clean_aamva_value(raw: str) -> str:
    """First line only; strip control chars; AAMVA 'NONE' → empty."""
    s = (raw or "")
    for sep in ("\r", "\x00", "\x1e", "\x1c", "\x1d", "\x0b", "\x0c"):
        s = s.replace(sep, "\n")
    s = s.split("\n", 1)[0].strip(" \t*")
    if s.upper() in _AAMVA_NONE:
        return ""
    return s


def _guest_text(value: Any) -> str:
    """Guest-facing string: never leak barcode line-feeds into the extension UI."""
    return _clean_aamva_value("" if value is None else str(value))


def _extract_aamva_elements(raw: str) -> dict[str, str]:
    """
    Extract AAMVA element-id → value.

    Real 690gt barcodes are LF-separated after the ANSI header, e.g.
    ``…ZT03330056DLDCAC\\nDCBNONE\\nDCSMARTINEZ\\nDACRUBEN``.
    """
    text = _normalize_aamva_raw(raw)
    fields: dict[str, str] = {}

    # Data subfile starts at the last "DL" / "ID" that is immediately followed by a Dxx tag.
    data = text
    starts = [m.start() for m in re.finditer(r"(?:DL|ID)(D[A-Z]{2})", text)]
    if starts:
        data = text[starts[-1] :]
        if data.startswith(("DL", "ID")):
            data = data[2:]

    for line in data.split("\n"):
        line = line.strip()
        if len(line) >= 3 and line[0] in "DZ" and line[1:3].isalpha() and line[1:3].isupper():
            tag = line[:3]
            val = _clean_aamva_value(line[3:])
            if val or tag in ("DAD",):  # explicit NONE already cleaned to ""
                fields[tag] = val

    if fields.get("DAC") or fields.get("DCS") or fields.get("DAQ"):
        return {k: _clean_aamva_value(v) for k, v in fields.items()}

    # Packed single-line fallback (no LFs between tags).
    upper = text.upper()
    hits: list[tuple[int, str]] = []
    for tag in _AAMVA_TAGS:
        start = 0
        while True:
            pos = upper.find(tag, start)
            if pos < 0:
                break
            prev = upper[pos - 1] if pos > 0 else "\n"
            prefix2 = upper[pos - 2 : pos] if pos >= 2 else ""
            if (not prev.isalpha()) or prefix2 in ("DL", "ID", "ZV"):
                hits.append((pos, tag))
            start = pos + 1
    hits.sort(key=lambda x: x[0])
    packed: dict[str, str] = {}
    for i, (pos, tag) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(text)
        value = _clean_aamva_value(text[pos + 3 : end])
        if value:
            packed[tag] = value
    return packed


def _aamva_date(raw: str) -> str:
    """AAMVA v1–v7 MMDDYYYY or v8+ CCYYMMDD → YYYY-MM-DD."""
    s = (raw or "").strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 8:
        s = digits[:8]
    else:
        return (raw or "").strip()
    first4 = int(s[:4])
    if first4 > 1231:
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return f"{s[4:8]}-{s[0:2]}-{s[2:4]}"


def _parse_aamva(raw: str) -> dict[str, Any]:
    """Parse AAMVA PDF417 raw text into structured snake_case guest fields."""
    fields = _extract_aamva_elements(raw)

    first = _guest_text(fields.get("DAC") or fields.get("DCT") or "")
    last = _guest_text(fields.get("DCS") or "")
    middle = _guest_text(fields.get("DAD") or "")

    if not (first or last):
        daa = _guest_text(fields.get("DAA", ""))
        parts = [p.strip() for p in re.split(r"[,;$]", daa) if p.strip()]
        if len(parts) >= 2:
            last, first = parts[0], parts[1]
            middle = parts[2] if len(parts) >= 3 else ""
        elif len(parts) == 1:
            last = parts[0]

    # Truncate given-name packing: "JOHN ROBERT" may include extra AAMVA junk after space-run
    first = _guest_text(re.split(r"\s{2,}", first)[0])
    last = _guest_text(re.split(r"\s{2,}", last)[0])
    middle = _guest_text(re.split(r"\s{2,}", middle)[0])

    full_name = " ".join(x for x in (first, middle, last) if x)
    dob = _aamva_date(_guest_text(fields.get("DBB", "")))
    expiry = _aamva_date(_guest_text(fields.get("DBA", "")))
    issue = _aamva_date(_guest_text(fields.get("DBD", "")))
    dl_num = _guest_text(fields.get("DAQ") or "")
    street = _guest_text(fields.get("DAG") or "")
    city = _guest_text(fields.get("DAI") or "")
    state_code = _guest_text(fields.get("DAJ") or "")[:2]
    postal_raw = re.sub(r"\D", "", _guest_text(fields.get("DAK", "")))
    postal = postal_raw[:5] if postal_raw else ""
    sex_code = _guest_text(fields.get("DBC") or "").upper()
    sex = {"1": "M", "2": "F", "M": "M", "F": "F", "9": ""}.get(sex_code, "")

    city_state_zip = f"{city}, {state_code} {postal}".strip(" ,") if (city or state_code) else ""
    address = f"{street}, {city_state_zip}".strip(" ,") if city_state_zip else street

    barcode_data: dict[str, Any] = {"source": "pdf417_aamva", "tags": sorted(fields.keys())}
    for k in ("DAQ", "DCS", "DAC", "DCT", "DAD", "DBB", "DBA", "DBD", "DAG", "DAI", "DAJ", "DAK", "DBC"):
        if k in fields:
            barcode_data[k] = _guest_text(fields[k])

    doc_type = "ID Card" if re.search(r"\bID\b", raw[:80], re.I) and "DL" not in raw[:80].upper() else "Driver License"

    return {
        "first_name": first,
        "middle_name": middle,
        "last_name": last,
        "full_name": full_name,
        "document_number": dl_num,
        "document_type": doc_type,
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

def _is_aamva_text(text: str) -> bool:
    t = text or ""
    u = t.upper()
    if t.startswith("@") or "ANSI" in u or "AAMVA" in u:
        return True
    return "DAQ" in u and ("DCS" in u or "DAC" in u or "DCT" in u or "DBB" in u)


def _pdf417_candidates_from_pil(img: Any) -> list[str]:
    """Run zxingcpp on one PIL image; return candidate barcode texts."""
    import zxingcpp

    texts: list[str] = []
    try:
        fmt = getattr(zxingcpp, "BarcodeFormat", None)
        pdf417 = getattr(fmt, "PDF417", None) if fmt is not None else None
        if pdf417 is not None and hasattr(zxingcpp, "read_barcodes"):
            try:
                results = zxingcpp.read_barcodes(img, formats=pdf417)
            except TypeError:
                results = zxingcpp.read_barcodes(img)
        else:
            results = zxingcpp.read_barcodes(img)
    except Exception as exc:  # noqa: BLE001
        logger.debug("nScan690gt: zxingcpp.read_barcodes failed: %s", exc)
        return texts

    for r in results or []:
        text = (getattr(r, "text", None) or "").strip()
        if text:
            texts.append(text)
    return texts


def _barcode_image_variants(raw_bytes: bytes) -> list[Any]:
    from PIL import Image, ImageEnhance, ImageOps

    base = Image.open(io.BytesIO(raw_bytes))
    gray = ImageOps.exif_transpose(base).convert("L")
    variants: list[Any] = [gray]
    w, h = gray.size
    inverted = ImageOps.invert(gray)
    variants.append(inverted)
    variants.append(ImageOps.autocontrast(gray))
    if h >= 80:
        variants.append(gray.crop((0, int(h * 0.35), w, h)))
        variants.append(inverted.crop((0, int(h * 0.35), w, h)))
    variants.append(gray.rotate(180, expand=True))
    variants.append(inverted.rotate(180, expand=True))
    variants.append(ImageEnhance.Contrast(gray).enhance(1.8))
    return variants


def _try_pdf417(raw_bytes: bytes) -> tuple[dict[str, Any] | None, str]:
    """
    Attempt PDF417 barcode decode on the scanned image.
    Returns (structured_dict, aamva_raw_text) on success, or (None, "") on failure.
    """
    try:
        import zxingcpp  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError as exc:
        logger.warning(
            "nScan690gt: zxingcpp or Pillow not installed — skipping PDF417: %s. "
            "Install on hotel PC:  python -m pip install zxing-cpp Pillow",
            exc,
        )
        return None, ""

    try:
        variants = _barcode_image_variants(raw_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nScan690gt: cannot open scan image for PDF417: %s", exc)
        return None, ""

    seen: set[str] = set()
    for img in variants:
        for text in _pdf417_candidates_from_pil(img):
            if text in seen:
                continue
            seen.add(text)
            logger.info(
                "nScan690gt: barcode text (%d chars) preview=%s",
                len(text),
                _preview_barcode(text),
            )
            if not _is_aamva_text(text):
                logger.info("nScan690gt: barcode found but not AAMVA — skip")
                continue
            structured = _parse_aamva(text)
            tags = []
            bd = structured.get("barcode_data")
            if isinstance(bd, dict):
                tags = bd.get("tags") or []
            logger.info("nScan690gt: AAMVA tags=%s", tags)
            if structured.get("document_number") or structured.get("last_name") or structured.get("first_name"):
                logger.info(
                    "nScan690gt: PDF417/AAMVA decoded — doc=%r name=%r dob=%r",
                    structured.get("document_number"),
                    structured.get("full_name"),
                    structured.get("date_of_birth"),
                )
                return structured, text
            logger.info("nScan690gt: AAMVA text parsed but empty key fields — continue")

    logger.info(
        "nScan690gt: no usable AAMVA PDF417 (%d variants, %d unique texts)",
        len(variants),
        len(seen),
    )
    return None, ""


def _bmp_to_jpeg_file(raw_bytes: bytes, *, max_edge: int = 2200) -> Path:
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw_bytes))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if longest > max_edge:
        scale = max_edge / float(longest)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    path = Path(tmp.name)
    img.save(path, format="JPEG", quality=90)
    return path


def _ocr_google_vision(raw_bytes: bytes) -> str:
    """Google Cloud Vision document OCR — 690gt SDK has none; same engine as DocketPORT."""
    from scanner import ScannerError, extract_text_with_google_vision

    path: Path | None = None
    try:
        path = _bmp_to_jpeg_file(raw_bytes)
        text = extract_text_with_google_vision(path)
        return (text or "").strip()
    except ScannerError as exc:
        logger.warning("nScan690gt: Google Vision OCR unavailable/failed: %s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("nScan690gt: Google Vision OCR error: %s", exc)
        return ""
    finally:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _to_jpeg_b64(raw_bytes: bytes, *, max_edge: int, quality: int) -> str:
    """Compress a BMP/JPEG raster for Chrome Native Messaging (1 MB cap)."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw_bytes))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if longest > max_edge:
        scale = max_edge / float(longest)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    try:
        img.save(buf, format="JPEG", quality=quality, optimize=True)
    except Exception:  # noqa: BLE001
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _wire_size(result: dict[str, Any]) -> int:
    return len(json.dumps(result, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _compact_side_jpegs(
    front_bytes: bytes,
    back_bytes: bytes,
    *,
    max_edge: int,
    quality: int,
) -> tuple[str, str]:
    """JPEG-encode one front/back pair at a given size. Decode still uses original BMP."""
    front_b64 = ""
    back_b64 = ""
    try:
        if front_bytes:
            front_b64 = _to_jpeg_b64(front_bytes, max_edge=max_edge, quality=quality)
        if back_bytes:
            back_b64 = _to_jpeg_b64(back_bytes, max_edge=max_edge, quality=quality)
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s JPEG compress failed edge=%d q=%d: %s", _LOG_TAG, max_edge, quality, exc)
        return "", ""
    logger.info(
        "%s JPEG panel images edge=%d q=%d front_b64=%d back_b64=%d total=%d",
        _LOG_TAG, max_edge, quality, len(front_b64), len(back_b64), len(front_b64) + len(back_b64),
    )
    return front_b64, back_b64


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

    for key in (
        "first_name", "middle_name", "last_name", "full_name", "document_number",
        "document_type", "date_of_birth", "expiry_date", "issue_date", "address",
        "street_address", "city", "state", "postal_code", "gender",
    ):
        if key in structured:
            structured[key] = _guest_text(structured.get(key, ""))

    sync_structured_document_fields_for_extension(structured)
    front = (front_b64 or "").strip()
    back = (back_b64 or "").strip()
    # One copy per side only — duplicate aliases would 2–3× the payload past Chrome's 1 MB cap.
    aamva = (aamva_raw or "")[:2000]

    return {
        "type": "SDK_DOCUMENT_RESULT",
        "success": True,
        "sdk_engine": "nscan690gt_sdk",
        "scan_mode": scan_mode,
        "two_sided": bool(front and back),
        "vision_ocr_fallback": not bool(aamva_raw),
        "document_data": structured,
        "aamva_raw": aamva,
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
        "image_front_base64": front,
        "image_back_base64": back,
    }


def _decode_from_images(front_b64: str, back_b64: str) -> tuple[dict[str, Any], str]:
    """PDF417/AAMVA first (back then front); Google Vision OCR fallback. Never hang the host."""
    from scanner import empty_id_fields, parse_id_fields

    front_bytes = base64.b64decode(front_b64) if front_b64 else b""
    back_bytes = base64.b64decode(back_b64) if back_b64 else b""

    _log_step(
        "decode start — front=%d bytes back=%d bytes",
        len(front_bytes),
        len(back_bytes),
    )

    structured, aamva_raw = None, ""
    for label, raw in (("back", back_bytes), ("front", front_bytes)):
        if not raw:
            continue
        _log_step("PDF417/AAMVA decode on %s…", label)
        structured, aamva_raw = _try_pdf417(raw)
        if structured:
            _log_step("PDF417 OK on %s — doc=%r name=%r", label, structured.get("document_number"), structured.get("full_name"))
            break

    if structured:
        return structured, aamva_raw

    _log_step("no usable barcode — Google Vision OCR on front then back")
    combined_text = ""
    for label, raw in (("front", front_bytes), ("back", back_bytes)):
        if not raw:
            continue
        text = _ocr_google_vision(raw)
        if text:
            _log_step("Vision OCR %s — %d chars", label, len(text))
            combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    if combined_text:
        ocr_data = parse_id_fields(combined_text)
        _log_step(
            "OCR fields — name=%r %r id=%r",
            ocr_data.get("firstName"),
            ocr_data.get("lastName"),
            ocr_data.get("idNumber"),
        )
        source = "google_vision"
    else:
        logger.warning("%s OCR returned no text — returning images so the panel still updates", _LOG_TAG)
        ocr_data = empty_id_fields()
        source = "none"

    structured = {
        "first_name": _guest_text(ocr_data.get("firstName", "")),
        "middle_name": _guest_text(ocr_data.get("middleName", "")),
        "last_name": _guest_text(ocr_data.get("lastName", "")),
        "full_name": _guest_text(ocr_data.get("fullName", "")),
        "document_number": _guest_text(ocr_data.get("idNumber", "")),
        "document_type": _guest_text(ocr_data.get("idType", "")),
        "date_of_birth": _guest_text(ocr_data.get("dateOfBirth", "")),
        "expiry_date": _guest_text(ocr_data.get("expiryDate", "")),
        "issue_date": _guest_text(ocr_data.get("issueDate", "")),
        "address": _guest_text(ocr_data.get("address", "")),
        "street_address": "",
        "city": "",
        "state": "",
        "postal_code": "",
        "gender": _guest_text(ocr_data.get("sex", "")),
        "nationality": "",
        "mrz_raw": "",
        "barcode_data": {"source": source},
    }
    return structured, ""


def _sdk_ok_to_document_result(sdk_ok: dict[str, Any], *, scan_mode: str) -> dict[str, Any]:
    front_b64 = (sdk_ok.get("image_front_base64") or sdk_ok.get("front_image_base64") or "").strip()
    back_b64 = (sdk_ok.get("image_back_base64") or sdk_ok.get("back_image_base64") or "").strip()
    if not front_b64 and not back_b64:
        raise ValueError("nScan 690gt returned no image data")

    front_bytes = base64.b64decode(front_b64) if front_b64 else b""
    back_bytes = base64.b64decode(back_b64) if back_b64 else b""

    structured, aamva_raw = _decode_from_images(front_b64, back_b64)
    result: dict[str, Any] | None = None
    for max_edge, quality in _JPEG_ATTEMPTS:
        jpeg_front, jpeg_back = _compact_side_jpegs(
            front_bytes, back_bytes, max_edge=max_edge, quality=quality,
        )
        if (front_bytes and not jpeg_front) or (back_bytes and not jpeg_back):
            continue
        result = _build_result(
            structured,
            front_b64=jpeg_front,
            back_b64=jpeg_back,
            aamva_raw=aamva_raw,
            scan_mode=scan_mode,
        )
        n = _wire_size(result)
        logger.info("%s outbound payload %d bytes (budget %d)", _LOG_TAG, n, _CHROME_PAYLOAD_BUDGET)
        if n < _CHROME_PAYLOAD_BUDGET:
            return result

    if result is None:
        result = _build_result(
            structured, front_b64="", back_b64="", aamva_raw=aamva_raw, scan_mode=scan_mode,
        )

    # Last resort: keep ID fields + tiniest JPEGs, drop optional raw barcode text.
    logger.warning("%s payload still large — dropping aamva_raw, keeping JPEG panels", _LOG_TAG)
    result["aamva_raw"] = ""
    bd = result.get("barcode_data")
    if isinstance(bd, dict):
        result["barcode_data"] = {"source": bd.get("source", "pdf417_aamva")}
    doc = result.get("document_data")
    if isinstance(doc, dict) and isinstance(doc.get("barcode_data"), dict):
        src = doc["barcode_data"].get("source", "pdf417_aamva")
        doc["barcode_data"] = {"source": src}
    n = _wire_size(result)
    logger.info("%s outbound payload after trim %d bytes", _LOG_TAG, n)
    return result


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
