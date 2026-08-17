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
import os
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
# Duplex CIS sensors are 180° apart, so one face is already inverted vs the other.
# BACK = the PDF417 face (rotated so the barcode reads). FRONT = the other face,
# rotated the opposite way. Override both faces: FDN_NSCAN690GT_ROTATE_CW=180.


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


# zxing / some dumpers spell AAMVA separators as these tokens instead of control chars.
_PLACEHOLDER_SEPS: tuple[str, ...] = (
    "<CRLF>", "<LF>", "<CR>", "<RS>", "<GS>", "<FS>",
    "&lt;LF&gt;", "&lt;CR&gt;", "&lt;RS&gt;",
)
_NAME_SUFFIXES = frozenset({"JR", "SR", "I", "II", "III", "IV", "V", "2ND", "3RD"})


def _normalize_aamva_raw(raw: str) -> str:
    """UTF-16 leftovers + AAMVA / placeholder separators → newline-separated text."""
    t = raw or ""
    if "\x00" in t:
        # zxing sometimes yields UTF-16LE as Latin-1
        try:
            t = t.encode("latin-1", errors="replace").decode("utf-16-le", errors="replace")
        except Exception:  # noqa: BLE001
            t = t.replace("\x00", "")
    for tok in _PLACEHOLDER_SEPS:
        t = t.replace(tok, "\n")
    t = t.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    for sep in ("\x1e", "\x1c", "\x1d", "\x0b", "\x0c", "\u2028", "\u2029", "\u0085"):
        t = t.replace(sep, "\n")
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\n+", "\n", t)


def _clean_aamva_value(raw: str) -> str:
    """First AAMVA field only; strip separators; AAMVA 'NONE' → empty."""
    s = _normalize_aamva_raw("" if raw is None else str(raw))
    s = s.split("\n", 1)[0]
    s = re.sub(r"<[A-Z]{1,4}>", "", s)
    s = s.strip(" \t*")
    if s.upper() in _AAMVA_NONE:
        return ""
    return s


def _payload_has_extra_tags(val: str) -> bool:
    """True when a 'value' still contains more Dxx/Zxx records (unsplit barcode)."""
    u = _normalize_aamva_raw(val).upper()
    if "\n" in u:
        return True
    return bool(re.search(r"(?:^|[^A-Z])(?:D|Z)[A-Z]{2}", u[1:])) if len(u) > 4 else False


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

    cleaned = {k: _clean_aamva_value(v) for k, v in fields.items()}
    daq = cleaned.get("DAQ") or ""
    has_name = bool(cleaned.get("DAC") or cleaned.get("DCS") or cleaned.get("DCT"))
    if has_name and daq and not _payload_has_extra_tags(fields.get("DAQ") or daq):
        return cleaned

    # Packed fallback (no LFs, or a fat DAQ that still contains DCS/DAC/…).
    packed = _extract_packed_aamva(text)
    merged = dict(packed)
    for k, v in cleaned.items():
        if v and not _payload_has_extra_tags(fields.get(k) or v):
            merged[k] = v
    return {k: _clean_aamva_value(v) for k, v in merged.items()}


def _extract_packed_aamva(text: str) -> dict[str, str]:
    """Single-line AAMVA: walk known element IDs left-to-right."""
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
            if (not prev.isalpha()) or prefix2 in ("DL", "ID", "ZV", "ZN"):
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
        return ""
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

    suffix = _guest_text(fields.get("DCU") or "")
    if suffix.upper() in _NAME_SUFFIXES and suffix.upper() not in first.upper().split():
        first = f"{first} {suffix}".strip()

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
    for k in ("DAQ", "DCS", "DAC", "DCT", "DAD", "DCU", "DBB", "DBA", "DBD", "DAG", "DAI", "DAJ", "DAK", "DBC"):
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

def _aamva_identity_ok(structured: dict[str, Any] | None) -> bool:
    if not structured:
        return False
    doc = structured.get("document_number") or ""
    name_ok = bool(structured.get("first_name") or structured.get("last_name"))
    return bool(doc) and name_ok and not _payload_has_extra_tags(doc)


def _is_aamva_text(text: str) -> bool:
    t = text or ""
    u = t.upper()
    if t.startswith("@") or "ANSI" in u or "AAMVA" in u:
        return True
    return "DAQ" in u and ("DCS" in u or "DAC" in u or "DCT" in u or "DBB" in u)


def _pdf417_candidates_from_pil(img: Any) -> list[tuple[str, Any]]:
    """Run zxingcpp on one PIL image; return (text, result) pairs."""
    import zxingcpp

    out: list[tuple[str, Any]] = []
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
        return out

    for r in results or []:
        text = (getattr(r, "text", None) or "").strip()
        if text:
            out.append((text, r))
    return out


def _barcode_y_frac(result: Any, img_h: int) -> float | None:
    if img_h <= 0:
        return None
    pos = getattr(result, "position", None)
    if pos is None:
        return None
    ys: list[float] = []
    for name in ("top_left", "top_right", "bottom_left", "bottom_right"):
        pt = getattr(pos, name, None)
        if pt is None:
            continue
        y = getattr(pt, "y", None)
        if y is None:
            y = getattr(pt, "Y", None)
        if y is not None:
            try:
                ys.append(float(y))
            except (TypeError, ValueError):
                pass
    if not ys:
        return None
    return (sum(ys) / len(ys)) / float(img_h)


def _barcode_image_variants(raw_bytes: bytes) -> list[tuple[Any, int | None]]:
    """(image, rotate_cw_if_this_variant_decodes). None = unknown (cropped)."""
    from PIL import Image, ImageEnhance, ImageOps

    base = Image.open(io.BytesIO(raw_bytes))
    gray = ImageOps.exif_transpose(base).convert("L")
    w, h = gray.size
    inverted = ImageOps.invert(gray)
    variants: list[tuple[Any, int | None]] = [
        (gray, 0),
        (inverted, 0),
        (ImageOps.autocontrast(gray), 0),
        (gray.rotate(180, expand=True), 180),
        (inverted.rotate(180, expand=True), 180),
        (ImageEnhance.Contrast(gray).enhance(1.8), 0),
    ]
    if h >= 80:
        variants.append((gray.crop((0, int(h * 0.35), w, h)), None))
        variants.append((inverted.crop((0, int(h * 0.35), w, h)), None))
    return variants


def _try_pdf417(raw_bytes: bytes) -> tuple[dict[str, Any] | None, str, int]:
    """
    Attempt PDF417 barcode decode on the scanned image.
    Returns (structured_dict, aamva_raw_text, rotate_cw) on success, or (None, "", 0).
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
        return None, "", 0

    try:
        variants = _barcode_image_variants(raw_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("nScan690gt: cannot open scan image for PDF417: %s", exc)
        return None, "", 0

    seen: set[str] = set()
    for img, variant_rot in variants:
        for text, result in _pdf417_candidates_from_pil(img):
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
            if not _aamva_identity_ok(structured):
                logger.info("nScan690gt: AAMVA text parsed but empty/unsplit key fields — continue")
                continue
            rotate_cw = variant_rot if variant_rot else 0
            if rotate_cw == 0:
                frac = _barcode_y_frac(result, getattr(img, "size", (0, 0))[1])
                # Upright US DL barcode sits in the lower half. Top-half = inverted raster.
                if frac is not None and frac < 0.45:
                    rotate_cw = 180
                elif frac is None and variant_rot is None:
                    rotate_cw = 0
            logger.info(
                "nScan690gt: PDF417/AAMVA decoded — doc=%r name=%r dob=%r rotate_cw=%d",
                structured.get("document_number"),
                structured.get("full_name"),
                structured.get("date_of_birth"),
                rotate_cw,
            )
            return structured, text, rotate_cw

    logger.info(
        "nScan690gt: no usable AAMVA PDF417 (%d variants, %d unique texts)",
        len(variants),
        len(seen),
    )
    return None, "", 0


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


def _panel_rotate_cw_override() -> int | None:
    """Optional forced clockwise rotation for both sides. Unset = per-side auto."""
    if "FDN_NSCAN690GT_ROTATE_CW" not in os.environ:
        return None
    raw = os.environ.get("FDN_NSCAN690GT_ROTATE_CW", "").strip()
    if not raw:
        return None
    try:
        return int(raw) % 360
    except ValueError:
        return None


def _rotate_raster_cw(raw_bytes: bytes, degrees: int) -> bytes:
    if not raw_bytes or not degrees:
        return raw_bytes
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(raw_bytes))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    img = img.rotate(-int(degrees) % 360, expand=True)
    buf = io.BytesIO()
    img.save(buf, format="BMP")
    return buf.getvalue()


def _portrait_upright_score(raw_bytes: bytes, rotate_cw: int) -> float:
    """
    Higher when the top band looks like a DL header (text/edges) rather than the
    bottom band. Used to pick 0° vs 180° for the photo side.
    """
    from PIL import Image, ImageFilter, ImageOps, ImageStat

    img = Image.open(io.BytesIO(raw_bytes))
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:  # noqa: BLE001
        pass
    img = img.convert("L")
    if rotate_cw:
        img = img.rotate(-int(rotate_cw) % 360, expand=True)
    w, h = img.size
    if h < 20:
        return 0.0
    band = max(4, h // 5)
    top = img.crop((0, 0, w, band)).filter(ImageFilter.FIND_EDGES)
    bot = img.crop((0, h - band, w, h)).filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(top).mean[0]) - float(ImageStat.Stat(bot).mean[0])


def _opposite_180(rotate_cw: int) -> int:
    return (int(rotate_cw) + 180) % 360


def _orient_duplex_faces(
    side0: bytes,
    side1: bytes,
) -> tuple[bytes, bytes, dict[str, Any] | None, str]:
    """
    DLL side 0/1 is insert-order, not photo vs barcode.

    BACK = the raster with PDF417/AAMVA, rotated so the barcode is readable.
    FRONT = the other raster, rotated 180° relative to BACK (the two CIS
    units face opposite directions).
    """
    s0, raw0, rot0 = _try_pdf417(side0) if side0 else (None, "", 0)
    if s0:
        photo_rot = _opposite_180(rot0)
        logger.info(
            "%s duplex assign: barcode on DLL side0 rot=%d → FRONT rot=%d (opposite CIS)",
            _LOG_TAG, rot0, photo_rot,
        )
        return _rotate_raster_cw(side1, photo_rot), _rotate_raster_cw(side0, rot0), s0, raw0

    s1, raw1, rot1 = _try_pdf417(side1) if side1 else (None, "", 0)
    if s1:
        photo_rot = _opposite_180(rot1)
        logger.info(
            "%s duplex assign: barcode on DLL side1 rot=%d → FRONT rot=%d (opposite CIS)",
            _LOG_TAG, rot1, photo_rot,
        )
        return _rotate_raster_cw(side0, photo_rot), _rotate_raster_cw(side1, rot1), s1, raw1

    rot_a = 0
    if side0:
        s_a0 = _portrait_upright_score(side0, 0)
        s_a180 = _portrait_upright_score(side0, 180)
        rot_a = 180 if s_a180 > s_a0 else 0
        logger.info(
            "%s duplex assign: no PDF417 — FRONT from DLL side0 rot=%d (score0=%.1f score180=%.1f)",
            _LOG_TAG, rot_a, s_a0, s_a180,
        )
    else:
        logger.info("%s duplex assign: no PDF417 — keep DLL order", _LOG_TAG)
    return _rotate_raster_cw(side0, rot_a), _rotate_raster_cw(side1, _opposite_180(rot_a)), None, ""


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


def _decode_from_images(
    front_b64: str,
    back_b64: str,
    *,
    allow_pdf417: bool = True,
) -> tuple[dict[str, Any], str, int]:
    """PDF417/AAMVA first (back then front); Google Vision OCR fallback. Never hang the host."""
    from scanner import empty_id_fields, parse_id_fields

    front_bytes = base64.b64decode(front_b64) if front_b64 else b""
    back_bytes = base64.b64decode(back_b64) if back_b64 else b""

    _log_step(
        "decode start — front=%d bytes back=%d bytes pdf417=%s",
        len(front_bytes),
        len(back_bytes),
        allow_pdf417,
    )

    structured, aamva_raw, rotate_cw = None, "", 0
    if allow_pdf417:
        for label, raw in (("back", back_bytes), ("front", front_bytes)):
            if not raw:
                continue
            _log_step("PDF417/AAMVA decode on %s…", label)
            structured, aamva_raw, rotate_cw = _try_pdf417(raw)
            if structured:
                _log_step(
                    "PDF417 OK on %s — doc=%r name=%r rotate_cw=%d",
                    label,
                    structured.get("document_number"),
                    structured.get("full_name"),
                    rotate_cw,
                )
                break

    if structured:
        return structured, aamva_raw, rotate_cw

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
    return structured, "", 0


def _sdk_ok_to_document_result(sdk_ok: dict[str, Any], *, scan_mode: str) -> dict[str, Any]:
    front_b64 = (sdk_ok.get("image_front_base64") or sdk_ok.get("front_image_base64") or "").strip()
    back_b64 = (sdk_ok.get("image_back_base64") or sdk_ok.get("back_image_base64") or "").strip()
    if not front_b64 and not back_b64:
        raise ValueError("nScan 690gt returned no image data")

    front_bytes = base64.b64decode(front_b64) if front_b64 else b""
    back_bytes = base64.b64decode(back_b64) if back_b64 else b""

    override = _panel_rotate_cw_override()
    if override is not None:
        structured, aamva_raw, _decode_rotate = _decode_from_images(front_b64, back_b64)
        if override:
            logger.info(
                "%s FDN_NSCAN690GT_ROTATE_CW=%d (both faces, decode guessed %d°)",
                _LOG_TAG, override, _decode_rotate,
            )
            if front_bytes:
                front_bytes = _rotate_raster_cw(front_bytes, override)
            if back_bytes:
                back_bytes = _rotate_raster_cw(back_bytes, override)
    else:
        front_bytes, back_bytes, structured, aamva_raw = _orient_duplex_faces(front_bytes, back_bytes)
        if not structured:
            structured, aamva_raw, _ = _decode_from_images(
                base64.b64encode(front_bytes).decode("ascii") if front_bytes else "",
                base64.b64encode(back_bytes).decode("ascii") if back_bytes else "",
                allow_pdf417=False,
            )
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
