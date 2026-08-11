"""
Thales QS2000 / MMMReader High-Level API via ctypes.

**You must align function signatures and data-type constants with your SDK headers**
(``MMMReaderHighLevelAPI.h``, ``MMMReaderConstants.h`` or equivalent). This module uses
the blocking flow described in the Thales programmer documentation:

  Initialise → WaitForDocument → ReadDocument → GetData → ClearData → Shutdown

Environment (optional — each overrides the same key in ``config/thales_paths.ini``):
  FDN_THALES_SDK_BIN       — Directory containing ``MMMReaderHighLevelAPI.dll`` and dependencies.
  FDN_THALES_APPLICATION_INI — Path to ``Application.ini``.
  FDN_THALES_DLL_NAME      — Alternate high-level DLL file name.
  FDN_THALES_WAIT_TIMEOUT_MS — WaitForDocument timeout (default 120000).
  FDN_THALES_CD_*          — Optional overrides for GetData type IDs (integers).

**Per-PC paths (recommended):** copy ``config/thales_paths.example.ini`` to ``config/thales_paths.ini``
and set ``SDKBin`` and ``ApplicationIni`` there so you do not rely on system env when moving machines.

See ``config/Application.ini.example`` for [DataToSend] settings.
"""

from __future__ import annotations

import configparser
import ctypes
import logging
import os
import re
import sys
from ctypes import POINTER, c_char_p, c_int, c_uint32
from pathlib import Path
from typing import Any

from utils import bytes_to_base64

logger = logging.getLogger(__name__)

_HOST_DIR = Path(__file__).resolve().parent
_THALES_PATHS_INI = _HOST_DIR / "config" / "thales_paths.ini"


def _read_thales_paths_ini() -> configparser.ConfigParser | None:
    if not _THALES_PATHS_INI.is_file():
        return None
    cp = configparser.ConfigParser()
    read = cp.read(_THALES_PATHS_INI, encoding="utf-8")
    if not read:
        return None
    logger.info("Loaded Thales paths from %s", _THALES_PATHS_INI)
    return cp


def _path_from_ini(cp: configparser.ConfigParser, option: str) -> Path | None:
    if not cp.has_section("Paths"):
        return None
    # ConfigParser folds keys with optionxform (default: lower); use lowercase names in thales_paths.ini
    if not cp.has_option("Paths", option):
        return None
    raw = cp.get("Paths", option, fallback="").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = _HOST_DIR / p
    return p


def _resolve_sdk_bin() -> Path | None:
    env = _env_path("FDN_THALES_SDK_BIN")
    if env is not None:
        return env
    cp = _read_thales_paths_ini()
    if cp is None:
        return None
    return _path_from_ini(cp, "sdk_bin")


def _resolve_application_ini() -> Path | None:
    env = _env_path("FDN_THALES_APPLICATION_INI")
    if env is not None:
        return env
    cp = _read_thales_paths_ini()
    if cp is None:
        return None
    return _path_from_ini(cp, "application_ini")


def _resolve_dll_name() -> str:
    raw = os.environ.get("FDN_THALES_DLL_NAME", "").strip()
    if raw:
        return raw
    cp = _read_thales_paths_ini()
    if cp is not None and cp.has_section("Paths") and cp.has_option("Paths", "dll_name"):
        ini_name = cp.get("Paths", "dll_name", fallback="").strip()
        if ini_name:
            return ini_name
    return "MMMReaderHighLevelAPI.dll"


# Default GetData type IDs — VERIFY against your SDK header (names may differ).
CD_CODELINE = int(os.environ.get("FDN_THALES_CD_CODELINE", "1"))
CD_CODELINE_DATA = int(os.environ.get("FDN_THALES_CD_CODELINE_DATA", "2"))
CD_AAMVA_DATA = int(os.environ.get("FDN_THALES_CD_AAMVA_DATA", "3"))
# Raw PDF417 barcode bytes — returned when PDF417=1 in Application.ini [DataToSend].
# Standard Thales MMMReader SDK value is 68. Override with FDN_THALES_CD_BARCODE_PDF417.
# Used as fallback when CD_AAMVA_DATA returns empty (e.g. US Driver License on some SDK versions).
CD_BARCODE_PDF417 = int(os.environ.get("FDN_THALES_CD_BARCODE_PDF417", "68"))
# Set FDN_THALES_CD_VISIBLE_IMAGE + enable VisibleImage in Application.ini to include document image
CD_VISIBLE_IMAGE = int(os.environ.get("FDN_THALES_CD_VISIBLE_IMAGE", "0").strip() or "0")


class ThalesSDKError(Exception):
    """Raised when the Thales SDK DLL cannot be loaded or returns an error code."""


def _env_path(name: str, default: Path | None = None) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    p = Path(raw)
    if not p.is_absolute():
        p = _HOST_DIR / p
    return p


def _add_dll_search_paths(bin_dir: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        os.add_dll_directory(str(bin_dir.resolve()))
    except (OSError, AttributeError) as exc:
        logger.warning("os.add_dll_directory failed (%s); PATH may still find DLLs", exc)
    # Prepend for child DLL resolution in some setups
    sep = ";" if sys.platform == "win32" else ":"
    os.environ["PATH"] = str(bin_dir.resolve()) + sep + os.environ.get("PATH", "")


def _load_high_level_dll() -> ctypes.WinDLL:
    bin_dir = _resolve_sdk_bin()
    if bin_dir is None or not bin_dir.is_dir():
        raise ThalesSDKError(
            "Set the SDK bin folder: either environment variable FDN_THALES_SDK_BIN, "
            "or [Paths] SDKBin in config/thales_paths.ini (see config/thales_paths.example.ini)."
        )
    _add_dll_search_paths(bin_dir)
    dll_name = _resolve_dll_name()
    dll_path = bin_dir / dll_name
    if not dll_path.is_file():
        raise ThalesSDKError(f"DLL not found: {dll_path}")
    try:
        return ctypes.WinDLL(str(dll_path))
    except OSError as exc:
        raise ThalesSDKError(
            f"Failed to load {dll_path}. Install VC++ runtime; match x64 Python with x64 SDK. ({exc})"
        ) from exc


def _bind_api(dll: ctypes.WinDLL) -> dict[str, Any]:
    """
    Bind exported functions. Adjust argtypes/restype here if your SDK differs.
    """
    api: dict[str, Any] = {}

    # --- Initialise (int Initialise(char const* path) is common; verify header) ---
    for name in ("MMMReader_Initialise", "MMMReader_Initialize"):
        if hasattr(dll, name):
            api["initialise"] = getattr(dll, name)
            api["initialise"].restype = c_int
            break
    else:
        raise ThalesSDKError("No MMMReader_Initialise/Initialize export found in DLL.")

    # --- Shutdown ---
    for name in ("MMMReader_Shutdown",):
        if hasattr(dll, name):
            fn = getattr(dll, name)
            fn.argtypes = []
            fn.restype = c_int
            api["shutdown"] = fn
            break
    else:
        raise ThalesSDKError("No MMMReader_Shutdown export found.")

    # --- WaitForDocument (optional timeout parameter in some builds) ---
    if hasattr(dll, "MMMReader_WaitForDocument"):
        fn = getattr(dll, "MMMReader_WaitForDocument")
        fn.restype = c_int
        # Prefer two-parameter form if present
        try:
            fn.argtypes = [c_uint32]
            api["wait_for_document"] = ("timeout_ms", fn)
        except Exception:  # noqa: BLE001
            fn.argtypes = []
            api["wait_for_document"] = ("none", fn)

    # --- ReadDocument ---
    if hasattr(dll, "MMMReader_ReadDocument"):
        fn = getattr(dll, "MMMReader_ReadDocument")
        fn.argtypes = []
        fn.restype = c_int
        api["read_document"] = fn

    # --- ClearData ---
    if hasattr(dll, "MMMReader_ClearData"):
        fn = getattr(dll, "MMMReader_ClearData")
        fn.argtypes = []
        fn.restype = c_int
        api["clear_data"] = fn

    # --- WaitForDocumentRemoval (optional — not present in all SDK versions) ---
    if hasattr(dll, "MMMReader_WaitForDocumentRemoval"):
        fn = getattr(dll, "MMMReader_WaitForDocumentRemoval")
        fn.restype = c_int
        try:
            fn.argtypes = [c_uint32]
            api["wait_for_removal"] = ("timeout_ms", fn)
        except Exception:
            fn.argtypes = []
            api["wait_for_removal"] = ("none", fn)

    # --- GetData: int GetData(int typeId, char* buf, int* len) ---
    if hasattr(dll, "MMMReader_GetData"):
        fn = getattr(dll, "MMMReader_GetData")
        fn.restype = c_int
        fn.argtypes = [c_int, c_char_p, POINTER(c_int)]
        api["get_data"] = fn

    return api


def _call_initialise(api: dict[str, Any], ini_path: Path | None) -> None:
    fn = api["initialise"]
    rc: Any
    if ini_path is not None and ini_path.is_file():
        fn.argtypes = [c_char_p]
        fn.restype = c_int
        rc = fn(c_char_p(str(ini_path.resolve()).encode("utf-8")))
    else:
        fn.argtypes = []
        fn.restype = c_int
        rc = fn()

    if isinstance(rc, int) and rc != 0:
        raise ThalesSDKError(f"MMMReader_Initialise failed with code {rc}")


def _call_wait(api: dict[str, Any], timeout_ms: int) -> None:
    w = api.get("wait_for_document")
    if w is None:
        raise ThalesSDKError("MMMReader_WaitForDocument not exported — check SDK version.")
    kind, fn = w
    if kind == "timeout_ms":
        rc = fn(c_uint32(timeout_ms))
    else:
        rc = fn()
    if isinstance(rc, int) and rc != 0:
        raise ThalesSDKError(f"WaitForDocument failed or timed out (code {rc}).")


def _call_read(api: dict[str, Any]) -> None:
    fn = api.get("read_document")
    if fn is None:
        raise ThalesSDKError("MMMReader_ReadDocument not exported.")
    rc = fn()
    if isinstance(rc, int) and rc != 0:
        # Non-zero may mean "unsupported document" on some SDK versions, but the scanner
        # may still have captured a visible image. Log and continue — the downstream check
        # (no text AND no image → error) is the real gate for foreign/unrecognized docs.
        logger.warning("[thales] ReadDocument returned code %d — continuing to attempt data/image fetch", rc)


def _call_wait_removal(api: dict[str, Any], timeout_ms: int) -> bool:
    """
    Call WaitForDocumentRemoval. Returns True when document removed, False on timeout.
    Raises ThalesSDKError if the function is not exported by this SDK version.
    """
    w = api.get("wait_for_removal")
    if w is None:
        raise ThalesSDKError(
            "MMMReader_WaitForDocumentRemoval not exported by this SDK version."
        )
    kind, fn = w
    rc = fn(c_uint32(timeout_ms)) if kind == "timeout_ms" else fn()
    return not (isinstance(rc, int) and rc != 0)


def _get_data_buffer(api: dict[str, Any], data_type: int) -> bytes:
    fn = api.get("get_data")
    if fn is None:
        raise ThalesSDKError("MMMReader_GetData not exported.")

    last_rc = 0
    for n in (65536, 256 * 1024, 1024 * 1024):
        buf = ctypes.create_string_buffer(n)
        size = c_int(n)
        rc = fn(c_int(data_type), buf, ctypes.byref(size))
        last_rc = rc if isinstance(rc, int) else 0
        written = int(size.value)
        if written <= 0:
            continue
        if written > n:
            continue
        raw = buf.raw[:written]
        if isinstance(rc, int) and rc != 0 and not raw.strip(b"\x00"):
            continue
        return raw.rstrip(b"\x00")
    raise ThalesSDKError(f"GetData(type={data_type}) failed (last code {last_rc}).")


def _decode_blob(blob: bytes) -> str:
    for enc in ("utf-8", "utf-16-le", "cp1252"):
        try:
            return blob.decode(enc).strip("\x00").strip()
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", errors="replace").strip()


def _parse_aamva_pipe(text: str) -> dict[str, str]:
    """Best-effort AAMVA / DL barcode field extraction (pipe or line-based)."""
    out: dict[str, str] = {}
    if not text:
        return out
    if "|" in text:
        parts = text.split("|")
        for i, p in enumerate(parts):
            key = f"field_{i:03d}"
            if "=" in p:
                k, _, v = p.partition("=")
                out[k.strip()] = v.strip()
            else:
                out[key] = p.strip()
    else:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "\t" in line:
                k, _, v = line.partition("\t")
                out[k.strip()] = v.strip()
            elif ":" in line:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
    return out


def _is_aamva_element_text(text: str) -> bool:
    """Return True when text looks like raw AAMVA barcode content (element-ID format)."""
    if not text:
        return False
    if text.startswith("@"):
        return True
    return bool(re.search(r"\b(DAA|DCS|DAC|DAQ|DBB|DBA|DBD|DAG|DAI|DAJ)\b", text[:300]))


def _parse_aamva_elements(text: str) -> dict[str, str]:
    """
    Parse raw AAMVA DL/ID PDF417 barcode text into element-ID → value dict.

    AAMVA format: header '@\\n\\rANSI …', then lines like 'DAALast,First,Middle'
    or 'DAQ20574518'. Element IDs are 3 uppercase alpha chars (DAA, DCS, DBB, DAQ …).
    Covers AAMVA DL/ID Design Standard v1–v10 (all US states).
    """
    out: dict[str, str] = {}
    if not text:
        return out
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for line in text.split("\n"):
        line = line.strip("\x00").strip()
        if len(line) >= 4 and line[:3].isalpha() and line[:3] == line[:3].upper():
            key = line[:3]
            value = line[3:].strip("\x00").strip()
            if value and key.isalpha():
                out[key] = value
    return out


def _normalise_aamva_date(raw: str) -> str:
    """
    Convert AAMVA date string to YYYY-MM-DD.

    AAMVA v1–v7: MMDDYYYY  (e.g. '06021981' → 1981-06-02)
    AAMVA v8+:   CCYYMMDD  (e.g. '19810602' → 1981-06-02)
    Heuristic: if the first 4 digits parse as a year > 1231 it is CCYYMMDD.
    """
    s = raw.strip().strip("\x00")
    if not s or not s.isdigit() or len(s) != 8:
        return s
    first4 = int(s[:4])
    if first4 > 1231:          # CCYYMMDD — year field (1900–2099)
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return f"{s[4:8]}-{s[0:2]}-{s[2:4]}"  # MMDDYYYY


def _aamva_elements_to_structured(elems: dict[str, str]) -> dict[str, Any]:
    """
    Map parsed AAMVA element-ID dict to the same structured-payload shape used by the extension.
    Handles all common US DL / ID card element identifiers.
    """
    # ── Name ─────────────────────────────────────────────────────────────────
    last   = elems.get("DCS", "").strip()
    first  = elems.get("DAC", "").strip()
    middle = elems.get("DAD", "").strip()

    if first or last:
        given     = " ".join(p for p in [first, middle] if p)
        full_name = f"{given} {last}".strip() if given else last
    else:
        # Fallback: DAA stores "LAST,FIRST,MIDDLE"
        daa = elems.get("DAA", "").strip()
        if "," in daa:
            seg    = [s.strip() for s in daa.split(",")]
            last   = seg[0]
            first  = seg[1] if len(seg) > 1 else ""
            middle = seg[2] if len(seg) > 2 else ""
            given  = " ".join(p for p in [first, middle] if p)
            full_name = f"{given} {last}".strip() if given else last
        else:
            full_name = daa

    # ── Dates ─────────────────────────────────────────────────────────────────
    dob    = _normalise_aamva_date(elems.get("DBB", ""))
    expiry = _normalise_aamva_date(elems.get("DBA", ""))
    issued = _normalise_aamva_date(elems.get("DBD", ""))

    # ── Address ───────────────────────────────────────────────────────────────
    street = elems.get("DAG", "").strip()
    city   = elems.get("DAI", "").strip()
    state  = elems.get("DAJ", "").strip()
    postal = elems.get("DAK", "").strip().strip("\x00")
    # Trim trailing zeros: '768010000  ' → '76801'
    if postal and postal.isdigit() and len(postal) > 5:
        postal = postal[:5]
    addr_parts = [p for p in [street, city, f"{state} {postal}".strip()] if p]
    address = ", ".join(addr_parts)

    # ── Other ─────────────────────────────────────────────────────────────────
    id_number = elems.get("DAQ", "").strip()
    sex_code  = elems.get("DBC", "").strip()
    gender    = {"1": "M", "2": "F", "9": ""}.get(sex_code, sex_code)

    return {
        "first_name":      first.strip(),
        "last_name":       last.strip(),
        "full_name":       full_name,
        "document_number": id_number,
        "date_of_birth":   dob,
        "gender":          gender,
        "nationality":     elems.get("DCG", "").strip(),
        "expiry_date":     expiry,
        "issue_date":      issued,
        "document_type":   "Driver License",
        "address":         address,
        "mrz_raw":         "",
        "mrz_parsed":      {},
        "barcode_data":    elems,
    }


_MRZ_DOC_TYPE_MAP: dict[str, str] = {
    "P": "Passport",
    "I": "ID Card",
    "A": "ID Card",
    "C": "ID Card",
    "V": "Visa",
}


def _detect_mrz_format(lines: list[str]) -> str:
    """Detect ICAO 9303 MRZ format (TD1/TD2/TD3) from stripped line lengths."""
    if len(lines) >= 3:
        last3_lens = [len(lines[-3]), len(lines[-2]), len(lines[-1])]
        if all(28 <= n <= 32 for n in last3_lens):
            return "TD1"
    if len(lines) >= 2:
        l1_len, l2_len = len(lines[-2]), len(lines[-1])
        if 34 <= l1_len <= 38 and 34 <= l2_len <= 38:
            return "TD2"
        if l1_len >= 44 and l2_len >= 44:
            return "TD3"
    return "unknown"


def _parse_name_from_mrz_field(name_field: str) -> tuple[str, str]:
    """Split MRZ name 'SURNAME<<GIVEN<NAMES' into (surname, given_names)."""
    if "<<" in name_field:
        surname_raw, _, given_raw = name_field.partition("<<")
        return surname_raw.replace("<", " ").strip(), given_raw.replace("<", " ").strip()
    return name_field.replace("<", " ").strip(), ""


def _normalise_mrz_date(yymmdd: str) -> str:
    """Convert MRZ YYMMDD to YYYY-MM-DD. Years 00-29 → 20xx, 30-99 → 19xx."""
    s = yymmdd.strip()
    if not s or not s.isdigit() or len(s) != 6:
        return s
    yy = int(s[0:2])
    year = 2000 + yy if yy < 30 else 1900 + yy
    return f"{year}-{s[2:4]}-{s[4:6]}"


def _parse_mrz_lines(mrz: str) -> dict[str, str]:
    """
    Extract common fields from ICAO 9303 MRZ.
    Handles TD1 (3×30 chars — national ID cards), TD2 (2×36 — older EU IDs),
    and TD3 (2×44 — passports and travel docs).
    """
    lines = [ln.strip() for ln in mrz.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) < 2:
        return {}

    fmt = _detect_mrz_format(lines)
    out: dict[str, str] = {"mrz_format": fmt}

    if fmt == "TD1":
        # Line 1: type(2) issuer(3) doc_number(9) check(1) optional(15)
        # Line 2: dob(6) check(1) sex(1) expiry(6) check(1) nationality(3) optional(11) composite_check(1)
        # Line 3: name(30)
        line1, line2, line3 = lines[-3], lines[-2], lines[-1]
        doc_type_char = line1[0].upper() if line1 else ""
        issuer = line1[2:5].replace("<", "").strip()
        doc_num = line1[5:14].replace("<", "").strip()
        dob = line2[0:6]
        sex = line2[7] if len(line2) > 7 else ""
        expiry = line2[8:14]
        nationality = line2[15:18].replace("<", "").strip() if len(line2) >= 18 else ""
        surname, given = _parse_name_from_mrz_field(line3)
        out.update({
            "document_number": doc_num,
            "nationality": nationality,
            "issuer": issuer,
            "date_of_birth_yymmdd": dob,
            "gender": sex if sex in "MF" else "",
            "expiry_yymmdd": expiry,
            "surname": surname,
            "given_names": given,
            "doc_type": _MRZ_DOC_TYPE_MAP.get(doc_type_char, "ID Card"),
        })

    elif fmt == "TD2":
        # Line 1: type(2) issuer(3) name(31)
        # Line 2: doc_number(9) check(1) nationality(3) dob(6) check(1) sex(1) expiry(6) check(1) optional(7) composite_check(1)
        line1, line2 = lines[-2], lines[-1]
        doc_type_char = line1[0].upper() if line1 else ""
        issuer = line1[2:5].replace("<", "").strip()
        surname, given = _parse_name_from_mrz_field(line1[5:36])
        doc_num = line2[0:9].replace("<", "").strip()
        nationality = line2[10:13].replace("<", "").strip()
        dob = line2[13:19]
        sex = line2[20] if len(line2) > 20 else ""
        expiry = line2[21:27]
        out.update({
            "document_number": doc_num,
            "nationality": nationality,
            "issuer": issuer,
            "date_of_birth_yymmdd": dob,
            "gender": sex if sex in "MF" else "",
            "expiry_yymmdd": expiry,
            "surname": surname,
            "given_names": given,
            "doc_type": _MRZ_DOC_TYPE_MAP.get(doc_type_char, "ID Card"),
        })

    elif fmt == "TD3":
        # Line 1: type(2) issuer(3) name(39)
        # Line 2: doc_number(9) check(1) nationality(3) dob(6) check(1) sex(1) expiry(6) check(1) optional(14) composite_check(1)
        line1, line2 = lines[-2], lines[-1]
        doc_type_char = line1[0].upper() if line1 else ""
        issuer = line1[2:5].replace("<", "").strip()
        surname, given = _parse_name_from_mrz_field(line1[5:44])
        doc_num = line2[0:9].replace("<", "").strip()
        nationality = line2[10:13].replace("<", "").strip()
        dob = line2[13:19]
        sex = line2[20] if len(line2) > 20 else ""
        expiry = line2[21:27]
        out.update({
            "document_number": doc_num,
            "nationality": nationality,
            "issuer": issuer,
            "date_of_birth_yymmdd": dob,
            "gender": sex if sex in "MF" else "",
            "expiry_yymmdd": expiry,
            "surname": surname,
            "given_names": given,
            "doc_type": _MRZ_DOC_TYPE_MAP.get(doc_type_char, "Passport"),
        })

    else:
        out["mrz_line1"] = lines[-2] if len(lines) >= 2 else ""
        out["mrz_line2"] = lines[-1]

    return out


def build_structured_payload(
    *,
    codeline_raw: str,
    codeline_data_text: str,
    aamva_text: str,
) -> dict[str, Any]:
    """Map SDK buffers to the JSON shape expected by the extension."""
    mrz = codeline_raw or codeline_data_text
    mrz_fields = _parse_mrz_lines(mrz) if mrz else {}
    aamva_parsed = _parse_aamva_pipe(aamva_text)

    dob = _normalise_mrz_date(mrz_fields.get("date_of_birth_yymmdd", ""))
    exp = _normalise_mrz_date(mrz_fields.get("expiry_yymmdd", ""))

    # Prefer explicit AAMVA dates if present (common keys vary by issuer)
    for k, v in aamva_parsed.items():
        kl = k.lower()
        if "dob" in kl or "birth" in kl:
            dob = v or dob
        if "expiry" in kl or "expiration" in kl:
            exp = v or exp

    full_name = ""
    if mrz_fields.get("given_names") or mrz_fields.get("surname"):
        full_name = f"{mrz_fields.get('given_names', '')} {mrz_fields.get('surname', '')}".strip()

    barcode_data: dict[str, Any] = dict(aamva_parsed)
    if aamva_text and not barcode_data:
        barcode_data["raw"] = aamva_text

    return {
        "first_name": mrz_fields.get("given_names", ""),
        "last_name": mrz_fields.get("surname", ""),
        "full_name": full_name,
        "document_number": mrz_fields.get("document_number", "")
        or aamva_parsed.get("document_number", ""),
        "date_of_birth": dob,
        "gender": mrz_fields.get("gender", "") or aamva_parsed.get("sex", ""),
        "nationality": mrz_fields.get("nationality", ""),
        "expiry_date": exp,
        "issue_date": "",
        "document_type": mrz_fields.get("doc_type", ""),
        "address": "",
        "mrz_raw": codeline_raw or "",
        "mrz_parsed": mrz_fields,
        "barcode_data": barcode_data,
    }


def read_document_blocking(
    *,
    application_ini: Path | None = None,
    wait_timeout_ms: int | None = None,
) -> dict[str, Any]:
    """
    Run full blocking cycle and return structured document fields plus raw SDK strings.

    Raises:
        ThalesSDKError: on load failures, timeouts, or non-zero SDK return codes.
    """
    if sys.platform != "win32":
        raise ThalesSDKError("Thales MMMReader integration is only supported on Windows.")

    ini = application_ini or _resolve_application_ini() or (_HOST_DIR / "config" / "Application.ini")
    timeout = wait_timeout_ms if wait_timeout_ms is not None else int(
        os.environ.get("FDN_THALES_WAIT_TIMEOUT_MS", "120000").strip() or "120000"
    )

    dll = _load_high_level_dll()
    api = _bind_api(dll)

    _call_initialise(api, ini)
    initialised = True

    try:
        _call_wait(api, timeout)
        _call_read(api)

        raw_code = b""
        raw_data = b""
        raw_aamva = b""
        try:
            raw_code = _get_data_buffer(api, CD_CODELINE)
        except ThalesSDKError as exc:
            logger.warning("CD_CODELINE GetData: %s", exc)
        try:
            raw_data = _get_data_buffer(api, CD_CODELINE_DATA)
        except ThalesSDKError as exc:
            logger.warning("CD_CODELINE_DATA GetData: %s", exc)
        try:
            raw_aamva = _get_data_buffer(api, CD_AAMVA_DATA)
        except ThalesSDKError as exc:
            logger.warning("CD_AAMVA_DATA GetData: %s", exc)

        codeline_s = _decode_blob(raw_code)
        codeline_data_s = _decode_blob(raw_data)
        aamva_s = _decode_blob(raw_aamva)

        # ── PDF417 fallback for Driver Licenses ───────────────────────────────
        # CD_AAMVA_DATA (type 3) is the SDK's pre-parsed AAMVA result. On some SDK
        # versions it returns empty even when the barcode decoded successfully.
        # CD_BARCODE_PDF417 (type 68) holds the raw barcode bytes — the same AAMVA
        # text the extension needs, just unparsed. We read it whenever AAMVA is empty.
        pdf417_raw_s = ""
        if not aamva_s and CD_BARCODE_PDF417 > 0:
            try:
                raw_pdf417 = _get_data_buffer(api, CD_BARCODE_PDF417)
                pdf417_raw_s = _decode_blob(raw_pdf417) if raw_pdf417 else ""
                if pdf417_raw_s:
                    logger.info(
                        "[thales] CD_BARCODE_PDF417(type=%d) returned %d chars — using as AAMVA source",
                        CD_BARCODE_PDF417, len(pdf417_raw_s),
                    )
                else:
                    logger.debug("[thales] CD_BARCODE_PDF417(type=%d) returned no data", CD_BARCODE_PDF417)
            except ThalesSDKError as exc:
                logger.debug("CD_BARCODE_PDF417 GetData: %s", exc)

        # ── Fetch visible image (always attempt when type ID is configured) ────
        # Must happen before the "no data" check so foreign docs that have an image
        # but no barcode/MRZ can still succeed and return the image for manual entry.
        visible_image_base64 = ""
        if CD_VISIBLE_IMAGE > 0:
            try:
                raw_vis = _get_data_buffer(api, CD_VISIBLE_IMAGE)
                if raw_vis:
                    visible_image_base64 = bytes_to_base64(raw_vis)
                    logger.info("[thales] visible image captured (%d bytes raw)", len(raw_vis))
            except ThalesSDKError as exc:
                logger.warning("Visible image GetData: %s", exc)

        has_text = bool(codeline_s or codeline_data_s or aamva_s or pdf417_raw_s)
        if not has_text and not visible_image_base64:
            raise ThalesSDKError("No document data returned (empty read). Check Application.ini [DataToSend].")

        # ── Build structured payload ──────────────────────────────────────────
        if not has_text:
            # Foreign or unsupported document with image only — clerk fills fields manually.
            logger.info("[thales] no text data — image-only result returned for manual entry")
            structured: dict[str, Any] = {
                "first_name": "", "last_name": "", "full_name": "",
                "document_number": "", "date_of_birth": "", "gender": "",
                "nationality": "", "expiry_date": "", "issue_date": "",
                "document_type": "", "address": "",
                "mrz_raw": "", "mrz_parsed": {}, "barcode_data": {},
            }
        elif pdf417_raw_s and _is_aamva_element_text(pdf417_raw_s):
            logger.info("[thales] Parsing DL fields from AAMVA element IDs (CD_BARCODE_PDF417 path)")
            elems = _parse_aamva_elements(pdf417_raw_s)
            structured = _aamva_elements_to_structured(elems)
        elif aamva_s and _is_aamva_element_text(aamva_s):
            logger.info("[thales] CD_AAMVA_DATA contains AAMVA element-ID text — parsing directly")
            elems = _parse_aamva_elements(aamva_s)
            structured = _aamva_elements_to_structured(elems) if elems else build_structured_payload(
                codeline_raw=codeline_s,
                codeline_data_text=codeline_data_s,
                aamva_text=aamva_s,
            )
        else:
            structured = build_structured_payload(
                codeline_raw=codeline_s,
                codeline_data_text=codeline_data_s,
                aamva_text=aamva_s,
            )

        if "clear_data" in api:
            try:
                api["clear_data"]()
            except Exception:  # noqa: BLE001
                logger.exception("MMMReader_ClearData raised")

        return {
            "codeline_raw": codeline_s,
            "codeline_data_raw": codeline_data_s,
            "aamva_raw": aamva_s,
            "pdf417_raw": pdf417_raw_s,
            "structured": structured,
            "visible_image_base64": visible_image_base64,
        }
    finally:
        if initialised:
            try:
                api["shutdown"]()
            except Exception as exc:  # noqa: BLE001
                logger.warning("MMMReader_Shutdown: %s", exc)


def read_document_safe() -> dict[str, Any]:
    """
    Same as :func:`read_document_blocking`, but returns an error dict instead of raising
    for common failure modes (used by the native host boundary).
    """
    if sys.platform != "win32":
        return {"type": "ERROR", "message": "Thales MMMReader SDK is only supported on Windows."}
    try:
        data = read_document_blocking()
        return {"type": "SDK_DOCUMENT_OK", **data}
    except ThalesSDKError as exc:
        return {"type": "ERROR", "message": str(exc)}
    except OSError as exc:
        return {"type": "ERROR", "message": f"OS error: {exc}"}


_REMOVAL_TIMEOUT_MS = int(
    os.environ.get("FDN_THALES_REMOVAL_TIMEOUT_MS", "60000").strip() or "60000"
)


def wait_for_document_removal_safe() -> dict[str, Any]:
    """
    Block until the document is removed from the Thales scanner.

    Runs a fresh Initialise → WaitForDocumentRemoval → Shutdown cycle so the
    auto-watch thread can call this *after* pushing AUTO_SCAN_RESULT, ensuring
    the next WaitForDocument loop only fires once the ID is physically gone.

    Returns {"type": "REMOVAL_OK"} or {"type": "REMOVAL_ERROR", "message": ...}.
    REMOVAL_ERROR means WaitForDocumentRemoval is not in this SDK build; the caller
    should fall back to a short sleep.
    """
    if sys.platform != "win32":
        return {"type": "REMOVAL_OK"}
    try:
        dll = _load_high_level_dll()
        api = _bind_api(dll)
        if "wait_for_removal" not in api:
            raise ThalesSDKError(
                "MMMReader_WaitForDocumentRemoval not available in this SDK version."
            )
        ini = _resolve_application_ini() or (_HOST_DIR / "config" / "Application.ini")
        _call_initialise(api, ini)
        try:
            removed = _call_wait_removal(api, _REMOVAL_TIMEOUT_MS)
            if removed:
                logger.info("[thales] document removed from scanner")
            else:
                logger.warning(
                    "[thales] WaitForDocumentRemoval timed out after %dms — "
                    "document may still be on scanner",
                    _REMOVAL_TIMEOUT_MS,
                )
            return {"type": "REMOVAL_OK"}
        finally:
            try:
                api["shutdown"]()
            except Exception:
                pass
    except ThalesSDKError as exc:
        logger.warning("[thales] wait_for_document_removal_safe: %s", exc)
        return {"type": "REMOVAL_ERROR", "message": str(exc)}
    except Exception as exc:
        logger.exception("[thales] wait_for_document_removal_safe unexpected error")
        return {"type": "REMOVAL_ERROR", "message": f"Unexpected error: {exc}"}
