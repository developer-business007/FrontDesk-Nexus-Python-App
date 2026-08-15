"""
AMBIR DocketPORT scanner via ctypes — image acquisition only.

Hardware: AMBIR DocketPORT 467/468/487/488/667/687 USB document/ID feed scanner.
SDK:      AMBIR SDK 2021 — model-specific DLL (e.g. DPORT487.dll), all exports __cdecl.

Flow:
    OpenInterface → (calibration check) → WaitForPaper → SetProperties →
    StartScan → ReadImageData loop → EndScan → FeedPaperOut → CloseInterface
    → assemble BMP bytes → base64

The DLL is NOT included in the SDK folder — it is installed by the AMBIR USB driver.
After installing the driver, find the DLL (e.g. C:\\Windows\\System32\\DPORT487.dll)
and point config/ambir_paths.ini [Paths] dll_path at it.

Environment (each overrides the matching ini key):
  FDN_AMBIR_DLL_PATH        — full path to DPORT*.dll
  FDN_AMBIR_MODEL           — scanner model name, e.g. DocketPORT487
  FDN_AMBIR_RESOLUTION      — scan DPI (default 300)
  FDN_AMBIR_WAIT_TIMEOUT_S  — seconds to wait for paper (default 30)

Structure layout notes (64-bit Windows, __cdecl, #pragma pack(8)):
  SIValue  = 8-byte union (largest member = c_void_p on x64)
  SISingle = 16 bytes   (2 × SIValue)
  SIRange  = 40 bytes   (5 × SIValue)
  SIList   = 32 bytes   (int32 + 4-byte pad + 3 × SIValue)
  SIProperty = 48 bytes (4 × uint16 + union of above = 8 + 40)
"""

from __future__ import annotations

import base64
import configparser
import ctypes
import logging
import os
import struct
import sys
import time
from ctypes import byref, c_int32, c_uint32, c_uint8, c_uint16
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HOST_DIR = Path(__file__).resolve().parent
_AMBIR_PATHS_INI = _HOST_DIR / "config" / "ambir_paths.ini"
_ambir_paths_cache: configparser.ConfigParser | None | bool = False  # False = not yet read

# All DocketPORT models, highest-capability first (687 > 667 > 488 > 487 > 468 > 467).
# The probe tries them in this order when no model is configured.
_AMBIR_MODELS: list[tuple[str, str]] = [
    ("DocketPORT687", "DPORT687.dll"),
    ("DocketPORT667", "DPORT667.dll"),
    ("DocketPORT488", "DPORT488.dll"),
    ("DocketPORT487", "DPORT487.dll"),
    ("DocketPORT468", "DPORT468.dll"),
    ("DocketPORT467", "DPORT467.dll"),
]

# ── SIResult codes (ScannerAPI.h) ────────────────────────────────────────────
SIR_SUCCESS = 0
SIR_UNKNOWN_MODEL_NAME = 10
SIR_SCANNER_NOT_READY = 11
SIR_BAD_PARAMETER = 14
SIR_ALREADY_OPEN = 15
SIR_INTERFACE_NOT_OPEN = 17
SIR_SCANNER_BUSY = 19
SIR_ENDOFDATA = 20
SIR_NOT_SCANNING = 21
SIR_DEVICE_COMMUNICATION_ERROR = 0x100
SIR_NOT_CALIBRATED = 0x802
SIR_PROPERTY_UNSUPPORTED = 0x900

# ── Property IDs ─────────────────────────────────────────────────────────────
SIP_XRESOLUTION = 4
SIP_YRESOLUTION = 5
SIP_BITS_PER_PIXEL = 7
SIP_SCAN_MODE = 8
SIP_YOFFSET = 12
SIP_XOFFSET = 13
SIP_SCAN_WIDTH_IN_PIXELS = 14
SIP_SCAN_LENGTH_IN_LINES = 15
SIP_CHANNEL_ORDER = 17
SIP_LINE_WIDTH_IN_BYTES = 19
SIP_EOP_DETECT_ENABLED = 33

# ── Container / item / value constants ───────────────────────────────────────
SICON_SINGLE = 0
SICON_RANGE = 1
SICON_LIST = 2
SI_INT32 = 1
SI_BOOL = 7
SI_SCANMODE_RGB = 2
SI_CO_BGR = 1       # BGR order required by BMP format
SI_TRUE = 1
SI_FALSE = 0
# SIPaperStatus (ScannerAPI.h)
SI_PS_PAPER_OUT = 0
SI_PS_PAPER_IN = 1

# Timeout sentinel code (used for error classification)
_ERR_CODE_TIMEOUT = -1


# ──────────────────────────────────────────────────────────────────────────────
# ctypes struct definitions
# All use _pack_=8 matching the SDK's #pragma PACK8 on Windows x64.
# ──────────────────────────────────────────────────────────────────────────────

class SIValue(ctypes.Union):
    """
    8-byte union on x64 Windows (c_void_p = 8 bytes dominates).
    iVal / bVal give the integer/boolean view; _ptr gives the pointer view
    (used to read piVal[] arrays from SI_GetProperty list containers).
    """
    _fields_ = [
        ("iVal", c_int32),
        ("fVal", ctypes.c_float),
        ("bVal", c_int32),
        ("_ptr", ctypes.c_void_p),
    ]


class SISingle(ctypes.Structure):
    _pack_ = 8
    _fields_ = [("current", SIValue), ("def_", SIValue)]


class SIRange(ctypes.Structure):
    _pack_ = 8
    _fields_ = [
        ("minimum", SIValue),
        ("maximum", SIValue),
        ("stepSize", SIValue),
        ("def_", SIValue),
        ("current", SIValue),
    ]


class SIList(ctypes.Structure):
    """
    int32 numItems (4 B) + 4-byte implicit pad (to align SIValue to 8) +
    3 × SIValue (8 B each) = 32 bytes total.
    ctypes inserts the padding automatically with _pack_=8.
    """
    _pack_ = 8
    _fields_ = [
        ("numItems", c_int32),
        ("current", SIValue),
        ("def_", SIValue),
        ("items", SIValue),   # items._ptr → int32[] in DLL memory
    ]


class _SIPropertyContainer(ctypes.Union):
    # No _pack_ needed on the union itself; member structs already carry it.
    _fields_ = [
        ("single", SISingle),   # 16 B
        ("range", SIRange),     # 40 B  ← largest, sets union size
        ("list", SIList),       # 32 B
    ]


class SIProperty(ctypes.Structure):
    """
    4 × uint16 (8 B) + _SIPropertyContainer (40 B) = 48 B total.
    The four header fields land on bytes 0-7; the union starts at offset 8
    (naturally 8-byte aligned after the 4 × 2-byte fields, no padding needed).
    """
    _pack_ = 8
    _fields_ = [
        ("propertyID",    c_uint16),
        ("containerType", c_uint16),
        ("itemType",      c_uint16),
        ("access",        c_uint16),
        ("_container",    _SIPropertyContainer),
    ]

    @property
    def single(self) -> SISingle:
        return self._container.single

    @property
    def range(self) -> SIRange:
        return self._container.range

    @property
    def list(self) -> SIList:
        return self._container.list


# ──────────────────────────────────────────────────────────────────────────────
# Error class
# ──────────────────────────────────────────────────────────────────────────────

class AmbirSDKError(Exception):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


# ──────────────────────────────────────────────────────────────────────────────
# Config helpers
# ──────────────────────────────────────────────────────────────────────────────

def _read_ambir_paths_ini() -> configparser.ConfigParser | None:
    global _ambir_paths_cache
    if _ambir_paths_cache is not False:
        return _ambir_paths_cache if isinstance(_ambir_paths_cache, configparser.ConfigParser) else None
    if not _AMBIR_PATHS_INI.is_file():
        _ambir_paths_cache = None
        return None
    cp = configparser.ConfigParser()
    cp.read(_AMBIR_PATHS_INI, encoding="utf-8")
    _ambir_paths_cache = cp
    logger.info("AMBIR: loaded config from %s", _AMBIR_PATHS_INI)
    return cp


def _ini_get(key: str) -> str:
    cp = _read_ambir_paths_ini()
    if cp and cp.has_option("Paths", key):
        return cp.get("Paths", key, fallback="").strip()
    return ""


def _resolve_dll_path() -> Path | None:
    """Resolve DPORT*.dll from env → ini → common install dirs."""
    env = os.environ.get("FDN_AMBIR_DLL_PATH", "").strip()
    if env:
        p = Path(env)
        return p if p.is_absolute() else (_HOST_DIR / p)
    raw = _ini_get("dll_path")
    if raw:
        p = Path(raw)
        return p if p.is_absolute() else (_HOST_DIR / p)
    return None


def _resolve_model() -> str | None:
    """Resolve configured model name from env or ini."""
    v = os.environ.get("FDN_AMBIR_MODEL", "").strip()
    if v:
        return v
    return _ini_get("scanner_model") or None


def _dll_name_for_model(model: str) -> str:
    for m, d in _AMBIR_MODELS:
        if m.lower() == model.lower():
            return d
    return f"DPORT{model.replace('DocketPORT', '')}.dll"


def _system_dll_candidates() -> list[tuple[str, Path]]:
    """
    Search Windows system directories for any known AMBIR DLL.
    Returns list of (model_name, dll_path) for all found models.
    """
    search_dirs = [
        Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32",
        Path(os.environ.get("SystemRoot", "C:/Windows")) / "SysWOW64",
    ]
    found: list[tuple[str, Path]] = []
    for model, dll_name in _AMBIR_MODELS:
        for sd in search_dirs:
            p = sd / dll_name
            if p.is_file():
                found.append((model, p))
                break
    return found


def _build_candidate_list() -> list[tuple[str, Path]]:
    """Return ordered list of (model, dll_path) to try."""
    dll_path = _resolve_dll_path()
    model_cfg = _resolve_model()

    if dll_path and model_cfg:
        return [(model_cfg, dll_path)]

    if dll_path:
        # Infer model from DLL stem (e.g. DPORT487 → DocketPORT487)
        stem = dll_path.stem.upper()
        for m, d in _AMBIR_MODELS:
            if d.upper().replace(".DLL", "") == stem:
                return [(m, dll_path)]
        # Unknown DLL name — use first model as placeholder
        return [(_AMBIR_MODELS[0][0], dll_path)]

    if model_cfg:
        # Model known but no explicit DLL path — look in system dirs
        dll_name = _dll_name_for_model(model_cfg)
        for sd in [
            Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32",
            Path(os.environ.get("SystemRoot", "C:/Windows")) / "SysWOW64",
        ]:
            p = sd / dll_name
            if p.is_file():
                return [(model_cfg, p)]

    # Full auto-detect: try all known models from system dirs
    return _system_dll_candidates()


# ──────────────────────────────────────────────────────────────────────────────
# DLL loading and function binding
# ──────────────────────────────────────────────────────────────────────────────

def _load_dll(dll_path: Path) -> ctypes.CDLL:
    """Load the AMBIR scanner DLL (cdecl calling convention)."""
    if not dll_path.is_file():
        raise AmbirSDKError(f"AMBIR DLL not found at: {dll_path}")
    try:
        os.add_dll_directory(str(dll_path.parent.resolve()))
    except (OSError, AttributeError):
        pass
    try:
        return ctypes.CDLL(str(dll_path))
    except OSError as exc:
        raise AmbirSDKError(
            f"Failed to load {dll_path.name}. Ensure the AMBIR USB driver is "
            f"installed and Python bitness matches DLL bitness (both must be 64-bit). ({exc})"
        ) from exc


def _bind(dll: ctypes.CDLL) -> None:
    """
    Set restype=c_uint32 for all used exports (prevents sign-extension of
    error codes like 0x900). argtypes are set only where they help correctness;
    struct-pointer args use byref() directly to avoid ctypes type-check overhead.
    """
    for name in (
        "SI_OpenInterface", "SI_CloseInterface",
        "SI_IsCalibrated", "SI_GetScannerStatus", "SI_GetPaperStatus",
        "SI_GetProperty", "SI_SetProperty",
        "SI_StartScan", "SI_ReadImageData",
        "SI_EndScan", "SI_FeedPaperOut", "SI_Feed", "SI_Reset",
        "SI_GetLastErrorText",
    ):
        if hasattr(dll, name):
            getattr(dll, name).restype = c_uint32

    dll.SI_OpenInterface.argtypes = [ctypes.c_char_p]
    # Pointer args for simple uint32* outputs
    dll.SI_IsCalibrated.argtypes   = [ctypes.c_void_p]
    dll.SI_GetPaperStatus.argtypes = [ctypes.c_void_p]
    dll.SI_GetScannerStatus.argtypes = [ctypes.c_void_p]
    dll.SI_GetLastErrorText.argtypes = [ctypes.c_void_p]
    # ReadImageData: buffer + 3 counts
    dll.SI_ReadImageData.argtypes = [
        ctypes.c_void_p,  # buffer
        c_uint32,         # numberOfLinesToRead
        c_uint32,         # pageNumber (always 0 for single-sided)
        ctypes.c_void_p,  # numberOfLinesReturned (uint32*)
    ]


# ──────────────────────────────────────────────────────────────────────────────
# Helper wrappers
# ──────────────────────────────────────────────────────────────────────────────

def _get_error_text(dll: ctypes.CDLL) -> str:
    buf = ctypes.c_char_p()
    try:
        dll.SI_GetLastErrorText(ctypes.addressof(buf))
        if buf.value:
            return buf.value.decode("utf-8", errors="replace").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _check(dll: ctypes.CDLL, rc: int, op: str) -> None:
    if rc == SIR_SUCCESS:
        return
    err = _get_error_text(dll)
    raise AmbirSDKError(
        f"{op} failed (code={rc:#x})" + (f": {err}" if err else ""),
        code=rc,
    )


def _get_prop(dll: ctypes.CDLL, prop_id: int) -> SIProperty:
    prop = SIProperty()
    prop.propertyID = prop_id
    rc = dll.SI_GetProperty(byref(prop))
    _check(dll, rc, f"SI_GetProperty(id={prop_id})")
    return prop


def _set_prop_single_int(dll: ctypes.CDLL, prop_id: int, value: int) -> None:
    """Set a property using SICON_SINGLE / SI_INT32 container."""
    prop = SIProperty()
    prop.propertyID    = prop_id
    prop.containerType = SICON_SINGLE
    prop.itemType      = SI_INT32
    prop.single.current.iVal = value
    rc = dll.SI_SetProperty(byref(prop))
    if rc == SIR_PROPERTY_UNSUPPORTED:
        logger.debug("AMBIR: SI_SetProperty id=%d unsupported — skipping", prop_id)
    elif rc != SIR_SUCCESS:
        err = _get_error_text(dll)
        logger.warning("AMBIR: SI_SetProperty id=%d rc=%#x %s", prop_id, rc, err)


def _set_prop_bool(dll: ctypes.CDLL, prop_id: int, enabled: bool) -> bool:
    """
    Set a SI_BOOL property the SDK-sample way: SI_GetProperty → mutate bVal → SI_SetProperty.

    Duplex / EOP / Prefeed on nScan 690gt reject SI_INT32 (0x901); they require SI_BOOL.
    Returns True when the property is set successfully.
    """
    try:
        prop = _get_prop(dll, prop_id)
    except AmbirSDKError as exc:
        logger.debug("AMBIR: SI_GetProperty id=%d for bool set failed: %s", prop_id, exc)
        return False

    if prop.containerType != SICON_SINGLE:
        logger.warning(
            "AMBIR: SI_SetProperty id=%d expected SICON_SINGLE, got %s — skipping",
            prop_id,
            prop.containerType,
        )
        return False

    # Keep container/item types from GetProperty; SDK samples only change bVal.
    prop.itemType = SI_BOOL
    prop.single.current.bVal = SI_TRUE if enabled else SI_FALSE
    prop.single.current.iVal = SI_TRUE if enabled else SI_FALSE
    rc = dll.SI_SetProperty(byref(prop))
    if rc == SIR_PROPERTY_UNSUPPORTED:
        logger.debug("AMBIR: SI_SetProperty id=%d (bool) unsupported — skipping", prop_id)
        return False
    if rc != SIR_SUCCESS:
        err = _get_error_text(dll)
        logger.warning("AMBIR: SI_SetProperty id=%d (bool) rc=%#x %s", prop_id, rc, err)
        return False
    return True


def _set_prop_list_int(dll: ctypes.CDLL, prop: SIProperty, value: int) -> None:
    """Set current value of a SICON_LIST property (prop must come from _get_prop)."""
    prop.list.current.iVal = value
    rc = dll.SI_SetProperty(byref(prop))
    if rc == SIR_PROPERTY_UNSUPPORTED:
        logger.debug("AMBIR: SI_SetProperty id=%d (list) unsupported — skipping", prop.propertyID)
    elif rc != SIR_SUCCESS:
        err = _get_error_text(dll)
        logger.warning("AMBIR: SI_SetProperty id=%d (list) rc=%#x %s", prop.propertyID, rc, err)


def _list_int_values(prop: SIProperty) -> list[int]:
    """Read integer items from a SICON_LIST property (reads from DLL-internal memory)."""
    if prop.containerType != SICON_LIST or prop.list.numItems <= 0:
        return []
    addr = prop.list.items._ptr
    if not addr:
        return []
    try:
        n = prop.list.numItems
        arr = (c_int32 * n).from_address(addr)
        return list(arr)
    except (ValueError, ctypes.ArgumentError, OSError):
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Scanner configuration for ID scanning
# ──────────────────────────────────────────────────────────────────────────────

def _configure_id_scan(dll: ctypes.CDLL, target_dpi: int = 300) -> tuple[int, int, int]:
    """
    Configure the scanner for optimal ID card capture.
    Returns (width_pixels, height_lines, bytes_per_line).

    Strategy:
    - RGB color at 300 DPI (ideal for Google Vision OCR on ID text)
    - Full available width
    - EOP detection enabled so feed stops at card edge automatically
    - BGR channel order required by BMP format
    """
    # ── Resolution ───────────────────────────────────────────────────────────
    chosen_dpi = target_dpi
    x_prop = _get_prop(dll, SIP_XRESOLUTION)
    available_dpi = _list_int_values(x_prop)
    if available_dpi:
        if target_dpi in available_dpi:
            chosen_dpi = target_dpi
        else:
            # Pick the closest available DPI, preferring higher
            chosen_dpi = min(available_dpi, key=lambda x: abs(x - target_dpi))
        logger.info("AMBIR: available DPI=%s → chosen=%d", available_dpi, chosen_dpi)

    if x_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, x_prop, chosen_dpi)
    else:
        _set_prop_single_int(dll, SIP_XRESOLUTION, chosen_dpi)

    y_prop = _get_prop(dll, SIP_YRESOLUTION)
    if y_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, y_prop, chosen_dpi)
    else:
        _set_prop_single_int(dll, SIP_YRESOLUTION, chosen_dpi)

    # ── Scan mode: RGB color ──────────────────────────────────────────────────
    mode_prop = _get_prop(dll, SIP_SCAN_MODE)
    if mode_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, mode_prop, SI_SCANMODE_RGB)
    else:
        _set_prop_single_int(dll, SIP_SCAN_MODE, SI_SCANMODE_RGB)

    # ── Offsets to zero (full document area) ─────────────────────────────────
    _set_prop_single_int(dll, SIP_XOFFSET, 0)
    _set_prop_single_int(dll, SIP_YOFFSET, 0)

    # ── Scan width: maximum available ────────────────────────────────────────
    w_prop = _get_prop(dll, SIP_SCAN_WIDTH_IN_PIXELS)
    if w_prop.containerType == SICON_RANGE:
        w_prop.range.current.iVal = w_prop.range.maximum.iVal
        dll.SI_SetProperty(byref(w_prop))

    # ── Scan length: EOP detection (auto-stop at card edge) ──────────────────
    # Enable EOP detect so the scan ends when the card exits the feed path.
    _set_prop_single_int(dll, SIP_EOP_DETECT_ENABLED, SI_TRUE)

    # Also set a safe maximum scan length (6 inches covers any standard ID).
    h_prop = _get_prop(dll, SIP_SCAN_LENGTH_IN_LINES)
    if h_prop.containerType == SICON_RANGE:
        max_lines = h_prop.range.maximum.iVal
        six_inch  = min(6 * chosen_dpi, max_lines)
        h_prop.range.current.iVal = six_inch
        dll.SI_SetProperty(byref(h_prop))

    # ── Channel order: BGR (required by BMP format) ───────────────────────────
    co_prop = _get_prop(dll, SIP_CHANNEL_ORDER)
    if co_prop.containerType == SICON_LIST:
        _set_prop_list_int(dll, co_prop, SI_CO_BGR)
    else:
        _set_prop_single_int(dll, SIP_CHANNEL_ORDER, SI_CO_BGR)

    # ── Read back actual values ───────────────────────────────────────────────
    w_actual = _get_prop(dll, SIP_SCAN_WIDTH_IN_PIXELS)
    if w_actual.containerType == SICON_RANGE:
        width = w_actual.range.current.iVal
    else:
        width = w_actual.single.current.iVal

    h_actual = _get_prop(dll, SIP_SCAN_LENGTH_IN_LINES)
    if h_actual.containerType == SICON_RANGE:
        height = h_actual.range.current.iVal
    else:
        height = h_actual.single.current.iVal

    lw_prop = _get_prop(dll, SIP_LINE_WIDTH_IN_BYTES)
    bytes_per_line = lw_prop.single.current.iVal

    logger.info(
        "AMBIR: scan config → %d×%d px at %d DPI, %d bytes/line",
        width, height, chosen_dpi, bytes_per_line,
    )
    return width, height, bytes_per_line


# ──────────────────────────────────────────────────────────────────────────────
# Scan loop and BMP assembly
# ──────────────────────────────────────────────────────────────────────────────

_LINES_PER_READ = 16  # batch size for SI_ReadImageData — large enough to be efficient


def _scan_to_bmp_bytes(
    dll: ctypes.CDLL,
    width: int,
    height: int,
    bytes_per_line: int,
) -> bytes:
    """
    Run the full StartScan → ReadImageData loop → EndScan sequence.
    Assembles scan lines into a complete top-down 24-bit RGB BMP file.

    BMP rows must be padded to 4-byte boundaries. A negative biHeight
    value in the DIB header tells Windows the rows are stored top-to-bottom
    (same order the scanner delivers them) — this avoids a row-reversal copy.
    """
    # Row stride padded to 4-byte boundary (required by BMP format)
    row_stride = (bytes_per_line + 3) & ~3

    buf = (c_uint8 * (row_stride * _LINES_PER_READ))()
    all_rows: list[bytes] = []

    rc = dll.SI_StartScan()
    _check(dll, rc, "SI_StartScan")

    scan_started = True
    try:
        while True:
            lines_returned = c_uint32(0)
            rc = dll.SI_ReadImageData(
                ctypes.cast(buf, ctypes.c_void_p),
                c_uint32(_LINES_PER_READ),
                c_uint32(0),
                ctypes.addressof(lines_returned),
            )

            n = lines_returned.value
            if n > 0:
                for i in range(n):
                    start = i * bytes_per_line
                    row   = bytes(buf[start : start + bytes_per_line])
                    if row_stride > bytes_per_line:
                        row += b"\x00" * (row_stride - bytes_per_line)
                    all_rows.append(row)

            if rc == SIR_ENDOFDATA:
                break
            if rc != SIR_SUCCESS:
                err = _get_error_text(dll)
                raise AmbirSDKError(
                    f"SI_ReadImageData failed (code={rc:#x})" + (f": {err}" if err else ""),
                    code=rc,
                )
            if n == 0:
                time.sleep(0.01)  # scanner is catching up; brief yield

        rc = dll.SI_EndScan()
        scan_started = False
        if rc != SIR_SUCCESS:
            logger.warning("AMBIR: SI_EndScan returned %d (non-fatal)", rc)

    except Exception:
        if scan_started:
            try:
                dll.SI_EndScan()
            except Exception:  # noqa: BLE001
                pass
        raise

    actual_height = len(all_rows)
    if actual_height == 0:
        raise AmbirSDKError("Scanner returned 0 scan lines — confirm document is inserted and calibrated.")

    logger.info("AMBIR: %d lines captured → assembling BMP", actual_height)

    pixel_data = b"".join(all_rows)
    pixel_size = len(pixel_data)
    file_size  = 14 + 40 + pixel_size

    # BITMAPFILEHEADER (14 bytes)
    bfh = struct.pack(
        "<2sIHHI",
        b"BM",
        file_size,
        0, 0,   # reserved
        54,     # pixel data offset (14 + 40)
    )
    # BITMAPINFOHEADER (40 bytes) — negative height = top-down row order
    bih = struct.pack(
        "<IiiHHIIiiII",
        40,              # biSize
        width,           # biWidth
        -actual_height,  # biHeight  (negative → top-down, matches scanner delivery order)
        1,               # biPlanes
        24,              # biBitCount (RGB)
        0,               # biCompression (BI_RGB = uncompressed)
        pixel_size,      # biSizeImage
        0, 0,            # XPelsPerMeter, YPelsPerMeter
        0, 0,            # ClrUsed, ClrImportant
    )
    return bfh + bih + pixel_data


# ──────────────────────────────────────────────────────────────────────────────
# DLL open / close
# ──────────────────────────────────────────────────────────────────────────────

def _open_scanner() -> tuple[ctypes.CDLL, str, Path]:
    """
    Load DLL, bind exports, call SI_OpenInterface.
    Returns (dll, model_name, dll_path).
    Raises AmbirSDKError if no scanner responds.
    """
    if sys.platform != "win32":
        raise AmbirSDKError("AMBIR SDK is only supported on Windows.")

    candidates = _build_candidate_list()
    if not candidates:
        raise AmbirSDKError(
            "AMBIR DLL not found. Steps:\n"
            "  1. Install the AMBIR DocketPORT USB driver from ambir.com.\n"
            "  2. Find the installed DLL (e.g. C:\\Windows\\System32\\DPORT487.dll).\n"
            "  3. Set FDN_AMBIR_DLL_PATH=<full_path> or add [Paths] dll_path in "
            "config/ambir_paths.ini."
        )

    last_err: Exception = AmbirSDKError("No AMBIR candidate found.")
    for model, dll_path in candidates:
        try:
            dll = _load_dll(dll_path)
            _bind(dll)
        except AmbirSDKError as exc:
            last_err = exc
            logger.debug("AMBIR: DLL load failed for %s: %s", dll_path, exc)
            continue

        # Try to close any lingering session before opening
        try:
            dll.SI_CloseInterface()
        except Exception:  # noqa: BLE001
            pass

        rc = dll.SI_OpenInterface(model.encode("ascii"))
        if rc == SIR_SUCCESS or rc == SIR_ALREADY_OPEN:
            logger.info("AMBIR: opened %s from %s (rc=%#x)", model, dll_path, rc)
            return dll, model, dll_path

        err_txt = _get_error_text(dll)
        msg = (
            f"SI_OpenInterface({model!r}) → {rc:#x}"
            + (f": {err_txt}" if err_txt else "")
        )
        if rc == SIR_UNKNOWN_MODEL_NAME:
            logger.debug("AMBIR: wrong model name for %s — trying next", dll_path)
        elif rc == SIR_DEVICE_COMMUNICATION_ERROR:
            logger.debug("AMBIR: %s DLL loaded but scanner not responding on USB", model)
        else:
            logger.debug("AMBIR: %s", msg)

        last_err = AmbirSDKError(msg, code=rc)
        try:
            dll.SI_CloseInterface()
        except Exception:  # noqa: BLE001
            pass

    raise AmbirSDKError(
        f"Could not open any AMBIR scanner. Last error: {last_err}. "
        "Confirm the USB cable is plugged in, the driver is installed, "
        "and no other application is using the scanner.",
        code=getattr(last_err, "code", None),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def probe_ambir_sdk() -> dict[str, Any]:
    """
    Lightweight probe: try to load DLL and (if possible) open the scanner interface.
    Does NOT scan anything. Used by DEVICE_STATUS to report scanner availability.

    Returns:
        {
            "available": bool,
            "model":     str,   # e.g. "DocketPORT487"
            "dll_path":  str,   # absolute path to DLL
            "detail":    str,   # human-readable status
        }
    """
    if sys.platform != "win32":
        return {"available": False, "model": "", "dll_path": "", "detail": "AMBIR SDK only supported on Windows."}

    candidates = _build_candidate_list()
    if not candidates:
        return {
            "available": False,
            "model": "",
            "dll_path": "",
            "detail": (
                "AMBIR DLL not found. Install the AMBIR USB driver, then set "
                "[Paths] dll_path in config/ambir_paths.ini."
            ),
        }

    for model, dll_path in candidates:
        try:
            dll = _load_dll(dll_path)
            _bind(dll)
        except AmbirSDKError as exc:
            logger.debug("AMBIR probe: DLL load failed %s", exc)
            continue

        # DLL loaded successfully — this is enough to report "dll available".
        # Try SI_OpenInterface to check hardware; don't block on failure.
        try:
            dll.SI_CloseInterface()  # clear any stale session
        except Exception:  # noqa: BLE001
            pass

        hw_ok = False
        hw_note = "DLL loaded; hardware status unknown (run a scan to confirm USB)."
        try:
            rc = dll.SI_OpenInterface(model.encode("ascii"))
            if rc in (SIR_SUCCESS, SIR_ALREADY_OPEN):
                hw_ok = True
                hw_note = f"Scanner {model} responded on USB."
                dll.SI_CloseInterface()
            elif rc == SIR_DEVICE_COMMUNICATION_ERROR:
                hw_note = f"{model} DLL loaded but scanner not found on USB — check cable/driver."
                dll.SI_CloseInterface()
            else:
                hw_note = f"{model} DLL loaded; SI_OpenInterface returned {rc:#x}."
                dll.SI_CloseInterface()
        except Exception as exc:  # noqa: BLE001
            hw_note = f"DLL loaded but probe raised: {exc}"

        logger.info("AMBIR probe: model=%s dll=%s hw_ok=%s", model, dll_path, hw_ok)
        return {
            "available": True,          # DLL at minimum is loadable
            "hw_ok": hw_ok,
            "model": model,
            "dll_path": str(dll_path),
            "detail": hw_note,
        }

    return {
        "available": False,
        "model": "",
        "dll_path": "",
        "detail": "AMBIR DLL found in candidate list but could not be loaded. Check DLL bitness (must be 64-bit).",
    }


def scan_document_blocking(
    *,
    wait_timeout_s: int = 30,
    resolution: int = 300,
) -> dict[str, Any]:
    """
    Full blocking scan cycle:
      open → calibration check → wait for paper → configure → scan → eject → close.

    Returns:
        {
            "image_base64": str,  # base64-encoded BMP
            "model":        str,
            "resolution":   int,
            "width":        int,
            "height":       int,
        }

    Raises:
        AmbirSDKError — on DLL load failures, hardware errors, or timeout.
    """
    dll, model, dll_path = _open_scanner()
    logger.info("AMBIR: scanner open — model=%s dll=%s", model, dll_path)

    try:
        # Calibration check (warn only; do not block the scan)
        cal_state = c_uint32(0)
        rc = dll.SI_IsCalibrated(ctypes.addressof(cal_state))
        if rc == SIR_SUCCESS and cal_state.value == SI_FALSE:
            logger.warning(
                "AMBIR: scanner not calibrated — scan quality may be degraded. "
                "Run calibration using the AMBIR calibration sheet if OCR accuracy is poor."
            )

        # Wait for document
        logger.info("AMBIR: waiting up to %d s for document insertion...", wait_timeout_s)
        deadline = time.monotonic() + wait_timeout_s
        while True:
            paper = c_uint32(0)
            dll.SI_GetPaperStatus(ctypes.addressof(paper))
            if paper.value == SI_PS_PAPER_IN:
                break
            if time.monotonic() >= deadline:
                raise AmbirSDKError(
                    f"No document inserted within {wait_timeout_s}s. "
                    "Place the ID card into the AMBIR scanner's paper feed slot.",
                    code=_ERR_CODE_TIMEOUT,
                )
            time.sleep(0.2)

        logger.info("AMBIR: document detected — configuring scan properties...")
        width, height, bytes_per_line = _configure_id_scan(dll, target_dpi=resolution)

        logger.info("AMBIR: starting scan...")
        bmp_bytes = _scan_to_bmp_bytes(dll, width, height, bytes_per_line)

        # Eject the document
        eject_rc = dll.SI_FeedPaperOut()
        if eject_rc != SIR_SUCCESS:
            logger.warning("AMBIR: SI_FeedPaperOut returned %#x (non-fatal)", eject_rc)

        b64 = base64.b64encode(bmp_bytes).decode("ascii")
        logger.info(
            "AMBIR: scan complete — BMP %d bytes, base64 %d chars",
            len(bmp_bytes), len(b64),
        )

        # Read actual scan height from assembled lines
        actual_height = (len(bmp_bytes) - 54) // ((width * 3 + 3) & ~3)

        return {
            "image_base64": b64,
            "model":        model,
            "resolution":   resolution,
            "width":        width,
            "height":       actual_height,
        }

    finally:
        try:
            dll.SI_CloseInterface()
        except Exception as exc:  # noqa: BLE001
            logger.warning("AMBIR: SI_CloseInterface: %s", exc)


def scan_document_safe() -> dict[str, Any]:
    """
    Same as scan_document_blocking() but returns a result dict instead of raising.

    Return shapes:
        {"type": "AMBIR_SCAN_OK",  "image_base64": ..., "model": ..., ...}
        {"type": "NO_DOCUMENT",    "message": ...}   — timeout waiting for paper
        {"type": "ERROR",          "message": ...}   — hardware / DLL failure
    """
    if sys.platform != "win32":
        return {"type": "ERROR", "message": "AMBIR SDK is only supported on Windows."}
    try:
        wait_s = int(os.environ.get("FDN_AMBIR_WAIT_TIMEOUT_S", "30").strip() or "30")
        dpi    = int(os.environ.get("FDN_AMBIR_RESOLUTION", "300").strip() or "300")
        data   = scan_document_blocking(wait_timeout_s=wait_s, resolution=dpi)
        return {"type": "AMBIR_SCAN_OK", **data}
    except AmbirSDKError as exc:
        code = getattr(exc, "code", None)
        msg  = str(exc)
        if code == _ERR_CODE_TIMEOUT or "timeout" in msg.lower() or "no document" in msg.lower():
            return {"type": "NO_DOCUMENT", "message": msg}
        return {"type": "ERROR", "message": msg}
    except (OSError, ValueError) as exc:
        return {"type": "ERROR", "message": str(exc)}
