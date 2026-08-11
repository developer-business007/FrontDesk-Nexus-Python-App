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
  FDN_THALES_WAIT_TIMEOUT_MS — WaitForDocument timeout (default 180000).
  FDN_THALES_CD_*          — Optional overrides for GetData type IDs (integers).
  FDN_THALES_DLL_CALLING_CONVENTION — ``stdcall`` (default, ``WinDLL``) or ``cdecl`` (``CDLL``) if needed.
  FDN_THALES_SET_CWD_TO_SDK_BIN — ``1`` (default) ``chdir`` before loading the DLL. Set ``0`` to disable.
  FDN_THALES_WORKING_DIR — ``root`` (default): chdir to **parent of** ``sdk_bin`` so ``Config/Application.ini`` resolves
    (Thales 3.9 layout). Use ``bin`` to chdir to ``sdk_bin`` only.
  FDN_THALES_SYNC_APPLICATION_INI — ``1`` copies your ``Application.ini`` from ``thales_paths.ini`` into
    ``<SDK root>/Config/Application.ini`` before ``Initialise`` (optional).
  FDN_THALES_MESSAGE_PUMP — ``1`` (default) runs ``PeekMessage`` / ``DispatchMessage`` on the Thales thread.
  FDN_THALES_COINIT — ``1`` (default) calls ``CoInitializeEx`` (STA). Set ``0`` to disable.

**Per-PC paths (recommended):** copy ``config/thales_paths.example.ini`` to ``config/thales_paths.ini``
and set ``SDKBin`` and ``ApplicationIni`` there so you do not rely on system env when moving machines.

See ``config/Application.ini.example`` for [DataToSend] settings.

**US driver license (front):** The PDF417/AAMVA barcode is usually on the **back**; the front has
printed fields only. ``MMMReader_GetData(CD_AAMVA_DATA)`` may be empty for a front scan. The host
can merge **Google Cloud Vision** OCR from the visible snapshot when ``FDN_THALES_VISION_OCR_FALLBACK=1``
(see ``scanner.enrich_thales_sdk_ok_with_vision``). Requires Vision credentials like ``SCAN_ID``.
"""

from __future__ import annotations

import configparser
import ctypes
import errno
import logging
import os
import re
import shutil
import sys
from ctypes import POINTER, c_bool, c_char_p, c_int, c_uint32, c_void_p
from pathlib import Path
from typing import Any

from utils import bytes_to_base64

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    from ctypes import wintypes

    _ThalesMSG = wintypes.MSG
else:
    _ThalesMSG = None

_HOST_DIR = Path(__file__).resolve().parent
_THALES_PATHS_INI = _HOST_DIR / "config" / "thales_paths.ini"
# One parse per process (avoids triple log when resolving sdk_bin / application_ini / dll_name).
_thales_paths_cache: configparser.ConfigParser | None | bool = False
_ini_sync_permission_denied_logged = False
_aamva_getdata_empty_hint_logged = False

# Windows ``SetEnvironmentVariable`` rejects values longer than this (single var).
_WIN32_ENV_VALUE_MAX = 32767


def _path_resolved_str(path: Path) -> str:
    try:
        return str(path.resolve(strict=False))
    except TypeError:
        return str(path.resolve())
    except OSError:
        return str(path)


def _windows_short_path_str(path: Path) -> str:
    """Return 8.3 short path when available (shorter ``PATH`` segments, fewer WinError 206 edge cases)."""
    if sys.platform != "win32":
        return str(path)
    p = _path_resolved_str(path)
    buf = ctypes.create_unicode_buffer(65536)
    n = ctypes.windll.kernel32.GetShortPathNameW(p, buf, len(buf))
    if n and buf.value:
        return buf.value
    return p


def _merge_path_with_prefix_safe(prefix_parts: list[str], old_path: str, sep: str) -> str:
    """Prepend Thales dirs to ``PATH`` without exceeding Windows' per-variable size limit."""
    prefix = sep.join(prefix_parts)
    if not old_path.strip():
        return prefix
    max_len = _WIN32_ENV_VALUE_MAX
    combined = prefix + sep + old_path
    if len(combined) <= max_len:
        return combined

    seen = {x.strip().lower() for x in prefix_parts if x.strip()}
    parts_old: list[str] = []
    for part in old_path.split(sep):
        part = part.strip()
        if not part:
            continue
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        parts_old.append(part)

    old2 = sep.join(parts_old)
    combined = prefix + sep + old2
    if len(combined) <= max_len:
        logger.warning(
            "PATH exceeded %s characters; deduplicated entries. "
            "Consider trimming System/User PATH in Environment Variables.",
            max_len,
        )
        return combined

    while parts_old and len(prefix + sep + sep.join(parts_old)) > max_len:
        parts_old.pop()
    combined = prefix + sep + sep.join(parts_old)
    if len(combined) <= max_len:
        logger.warning(
            "PATH exceeded %s characters; dropped tail PATH entries. "
            "Clean System/User PATH in Environment Variables to avoid losing other tools.",
            max_len,
        )
        return combined

    logger.warning(
        "PATH exceeded %s characters; using only Thales SDK prefix. "
        "Clean System/User PATH in Environment Variables.",
        max_len,
    )
    return prefix


def _read_thales_paths_ini() -> configparser.ConfigParser | None:
    global _thales_paths_cache
    if _thales_paths_cache is not False:
        return _thales_paths_cache if isinstance(_thales_paths_cache, configparser.ConfigParser) else None
    if not _THALES_PATHS_INI.is_file():
        _thales_paths_cache = None
        return None
    cp = configparser.ConfigParser()
    read = cp.read(_THALES_PATHS_INI, encoding="utf-8")
    if not read:
        _thales_paths_cache = None
        return None
    logger.info("Loaded Thales paths from %s", _THALES_PATHS_INI)
    _thales_paths_cache = cp
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


# Default GetData type IDs — Thales Document Reader SDK 3.9.2.x ``MMMReaderDataType`` (MMMReaderHighLevelAPI.h).
# Count from ``CD_CODELINE = 0`` in the vendor enum (do not guess): CD_AAMVA_DATA is 75 in 3.9.2.49, not 74.
CD_CODELINE = int(os.environ.get("FDN_THALES_CD_CODELINE", "0"))
CD_CODELINE_DATA = int(os.environ.get("FDN_THALES_CD_CODELINE_DATA", "1"))
CD_AAMVA_DATA = int(os.environ.get("FDN_THALES_CD_AAMVA_DATA", "75"))
# CD_IMAGEVIS=6 (front visible). CD_IMAGEBARCODEREAR=16: rear image captured automatically by the
# barcode plugin whenever PDF417=1; returned in a single ReadDocument call alongside the front.
# CD_IMAGEVISREAR=7 is deprecated ID150 backward-compat and returns 0 bytes on QS2000 — do not use.
CD_VISIBLE_IMAGE = int(os.environ.get("FDN_THALES_CD_VISIBLE_IMAGE", "6").strip() or "6")
CD_VISIBLE_IMAGE_REAR = int(os.environ.get("FDN_THALES_CD_VISIBLE_IMAGE_REAR", "16").strip() or "16")


class ThalesSDKError(Exception):
    """Raised when the Thales SDK DLL cannot be loaded or returns an error code."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def _looks_like_nt_exception_code(rc: int) -> bool:
    """ctypes sometimes returns NTSTATUS/SEH codes when native code faults (e.g. 0xC0000005)."""
    if not isinstance(rc, int):
        return False
    return (rc & 0xFFFFFFFF) == 0xC0000005


def _thales_com_init() -> bool:
    """
    Initialise COM on the current thread (auto-watch runs on a worker thread).

    Many Windows device stacks use COM internally; calling them without a COM apartment
    can fault with 0xC0000005. Returns True if this thread should call CoUninitialize().
    """
    if sys.platform != "win32":
        return False
    raw = os.environ.get("FDN_THALES_COINIT", "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    ole32 = ctypes.windll.ole32
    COINIT_APARTMENTTHREADED = 0x2
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    hr_u = int(hr) & 0xFFFFFFFF
    # S_OK = 0, S_FALSE = 1 (already initialized on this thread)
    if hr_u in (0, 1):
        logger.debug("Thales: CoInitializeEx(STA) ok (hr=0x%08x)", hr_u)
        return True
    # RPC_E_CHANGED_MODE: COM already initialized differently on this thread
    if hr_u == 0x80010106:
        logger.debug("Thales: CoInitializeEx returned RPC_E_CHANGED_MODE; continuing without pair-uninit")
        return False
    logger.warning("Thales: CoInitializeEx returned 0x%08x (unexpected)", hr_u)
    return False


def _thales_com_uninit(should: bool) -> None:
    if not should or sys.platform != "win32":
        return
    try:
        ctypes.windll.ole32.CoUninitialize()
    except OSError:
        pass


def _should_chdir_to_sdk_bin() -> bool:
    """Default on: Thales DLLs often resolve plugins relative to cwd; must happen before ``WinDLL``."""
    v = os.environ.get("FDN_THALES_SET_CWD_TO_SDK_BIN", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _message_pump_enabled() -> bool:
    v = os.environ.get("FDN_THALES_MESSAGE_PUMP", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _null_void() -> c_void_p:
    """Typed NULL for ``void*`` slots."""
    return c_void_p(None)


# MMMReaderErrorCallback(enum MMMReaderErrorCode, RTCHAR* msg, void* param)
# MMMReaderDataTypes.h: RTCHAR is wchar_t only under UNDER_CE; on desktop Windows builds it is char — use void* and decode.
_ThalesErrorCallback = ctypes.CFUNCTYPE(None, c_int, c_void_p, c_void_p)

# MMMReaderErrorCode (Thales 3.9.x) — see SDK Include/MMMReaderDataTypes.h
THALES_ERROR_FEATURE_NOT_SUPPORTED = 3
THALES_ERROR_READER_NOT_CONNECTED = 9
THALES_ERROR_LOADING_DLL = 17
THALES_ERROR_TIMED_OUT = 30


def _decode_thales_error_message(msg_ptr: Any) -> str:
    """Decode ``RTCHAR*`` from the error callback (UTF-16LE wchar_t or narrow UTF-8/ANSI)."""
    if msg_ptr is None:
        return ""
    addr = int(msg_ptr) if not isinstance(msg_ptr, int) else msg_ptr
    if addr == 0:
        return ""
    try:
        raw = ctypes.string_at(addr, 4096)
    except (OSError, ValueError, ctypes.ArgumentError):
        return ""
    if not raw:
        return ""

    # wchar_t / UTF-16LE: do NOT split on the first 0x00 (that is every other byte for ASCII).
    # NUL-terminated wide string ends with U+0000 = 0x00 0x00 on an even boundary.
    even = raw[: len(raw) - (len(raw) % 2)]
    # UTF-16LE Latin: high byte 0 for each code unit (incl. short strings like "UV").
    if len(even) >= 2 and even[1] == 0 and (len(even) == 2 or (len(even) >= 4 and even[3] == 0)):
        end = 0
        while end + 1 < len(even):
            if even[end] == 0 and even[end + 1] == 0:
                break
            end += 2
        chunk16 = even[:end]
        try:
            s = chunk16.decode("utf-16-le", errors="strict")
            if s.strip():
                return s.strip()
        except UnicodeDecodeError:
            pass

    chunk = raw.split(b"\x00", 1)[0]
    try:
        return chunk.decode("utf-8", errors="replace").strip()
    except UnicodeDecodeError:
        return chunk.decode("cp1252", errors="replace").strip()


def _describe_thales_error_code(code: int) -> str:
    if code == THALES_ERROR_FEATURE_NOT_SUPPORTED:
        return "ERROR_FEATURE_NOT_SUPPORTED (modality not on this reader or disabled in config)"
    if code == THALES_ERROR_READER_NOT_CONNECTED:
        return "ERROR_READER_NOT_CONNECTED (no page reader / USB device detected)"
    if code == THALES_ERROR_LOADING_DLL:
        return (
            "ERROR_LOADING_DLL (plugin/dependency missing - install VC++ runtime, ensure Plugins/ "
            "and Bin/lib are on the DLL search path; see Thales SDK Logs)"
        )
    if code == THALES_ERROR_TIMED_OUT:
        return "ERROR_TIMED_OUT"
    return f"code {code}"


@_ThalesErrorCallback
def _thales_on_sdk_error(code: int, msg_ptr: Any, _param: Any) -> None:
    try:
        msg = _decode_thales_error_message(msg_ptr)
        logger.warning(
            "Thales SDK error %s: %s",
            _describe_thales_error_code(code),
            msg or "(no message)",
        )
    except Exception:  # noqa: BLE001
        pass


def _resolve_thales_working_dir(bin_dir: Path) -> Path:
    """
    Process cwd before ``Initialise`` so ``MMMReader.ini`` / ``Config`` paths resolve like vendor samples.

    Thales 3.9 install: ``.../3.9.2.49/Bin`` (DLLs) and ``.../3.9.2.49/Config`` (``Application.ini``).
    Default is **SDK root** (parent of ``Bin``), not ``Bin`` itself.
    """
    mode = os.environ.get("FDN_THALES_WORKING_DIR", "").strip().lower()
    bd = bin_dir.resolve()
    if mode in ("bin", "sdk_bin"):
        return bd
    if mode in ("root", "install", "sdk_root", ""):
        if bd.name.lower() == "bin" and bd.parent.is_dir():
            return bd.parent
        return bd
    p = Path(mode)
    if p.is_dir():
        return p.resolve()
    return bd


def _maybe_sync_application_ini(host_ini: Path, sdk_root: Path) -> None:
    if os.environ.get("FDN_THALES_SYNC_APPLICATION_INI", "").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    if not host_ini.is_file():
        return
    dest = sdk_root / "Config" / "Application.ini"
    global _ini_sync_permission_denied_logged
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(host_ini, dest)
        logger.info("FDN_THALES_SYNC_APPLICATION_INI: copied to %s", dest)
    except OSError as exc:
        # Writing under Program Files without elevation is expected to fail; do not spam WARNING each scan.
        denied = isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (
            errno.EACCES,
            errno.EPERM,
        )
        if denied and not _ini_sync_permission_denied_logged:
            _ini_sync_permission_denied_logged = True
            logger.info(
                "FDN_THALES_SYNC_APPLICATION_INI: skipped (cannot write %s: %s). "
                "The SDK keeps using the existing Config\\Application.ini. "
                "Copy your project ini there once as Administrator, run the host elevated, or set "
                "FDN_THALES_SYNC_APPLICATION_INI=0 to silence sync.",
                dest,
                exc,
            )
        elif not denied:
            logger.warning("FDN_THALES_SYNC_APPLICATION_INI failed: %s", exc)


def _thales_pump_messages(*, max_messages: int = 64) -> None:
    """Dispatch pending Windows messages on this thread (SDK may need a pump during init/wait)."""
    if sys.platform != "win32" or _ThalesMSG is None or not _message_pump_enabled():
        return
    user32 = ctypes.windll.user32
    PM_REMOVE = 0x0001
    msg = _ThalesMSG()
    for _ in range(max_messages):
        r = user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE)
        if not r:
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _env_path(name: str, default: Path | None = None) -> Path | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    p = Path(raw)
    if not p.is_absolute():
        p = _HOST_DIR / p
    return p


def _add_dll_search_paths(bin_dir: Path) -> None:
    """Register ``Bin``, ``Bin/lib``, and ``<SDK root>/Plugins`` so plugin DLLs resolve dependencies (error 17 / 126)."""
    if sys.platform != "win32":
        return
    roots: list[Path] = [bin_dir.resolve()]
    lib_dir = bin_dir / "lib"
    if lib_dir.is_dir():
        roots.append(lib_dir.resolve())
    if bin_dir.name.lower() == "bin":
        plugins = bin_dir.parent / "Plugins"
        if plugins.is_dir():
            roots.append(plugins.resolve())
    for p in roots:
        last_exc: Exception | None = None
        for candidate in (_windows_short_path_str(p), _path_resolved_str(p)):
            try:
                os.add_dll_directory(candidate)
                break
            except (OSError, AttributeError) as exc:
                last_exc = exc
        else:
            logger.warning("os.add_dll_directory(%s) failed (%s)", p, last_exc)

    sep = ";" if sys.platform == "win32" else ":"
    prefix_parts = [_windows_short_path_str(p) for p in roots]
    new_path = _merge_path_with_prefix_safe(prefix_parts, os.environ.get("PATH", ""), sep)
    try:
        os.environ["PATH"] = new_path
    except ValueError as exc:
        logger.warning(
            "Could not set PATH (%s). Relying on add_dll_directory only; fix bloated PATH in Windows.",
            exc,
        )


def probe_thales_sdk() -> dict[str, Any]:
    """
    Lightweight check: can we resolve ``sdk_bin`` and load ``MMMReaderHighLevelAPI.dll``?

    This does **not** call ``Initialise`` or talk to the USB device — only that the SDK
    binaries are loadable. Full device readiness may still require a successful read cycle.
    """
    if sys.platform != "win32":
        return {"dll_load_ok": False, "detail": "Thales SDK path is only used on Windows."}
    try:
        _load_high_level_dll()
        return {
            "dll_load_ok": True,
            "detail": "High-level API DLL loaded (sdk_bin path OK).",
        }
    except ThalesSDKError as exc:
        return {"dll_load_ok": False, "detail": str(exc)}


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
    if _should_chdir_to_sdk_bin():
        wd = _resolve_thales_working_dir(bin_dir)
        os.chdir(str(wd))
        logger.debug("Thales: cwd before DLL load: %s", wd)
    conv = os.environ.get("FDN_THALES_DLL_CALLING_CONVENTION", "").strip().lower()
    try:
        if conv in ("cdecl", "cdll", "c"):
            return ctypes.CDLL(str(dll_path))
        return ctypes.WinDLL(str(dll_path))
    except OSError as exc:
        raise ThalesSDKError(
            f"Failed to load {dll_path}. Install VC++ runtime; match x64 Python with x64 SDK. ({exc})"
        ) from exc


def _bind_api(dll: ctypes.WinDLL) -> dict[str, Any]:
    """
    Bind exports for Thales Document Reader SDK High-Level API (see ``MMMReaderHighLevelAPI.h``).
    """
    api: dict[str, Any] = {}

    # --- Initialise (Thales 3.9): seven parameters, NO Application.ini path ---
    # MMMReaderErrorCode MMMReader_Initialise(DataCB, EventCB, ErrorCB, CertCB, bool, bool, void* param)
    for name in ("MMMReader_Initialise", "MMMReader_Initialize"):
        if hasattr(dll, name):
            fn = getattr(dll, name)
            fn.restype = c_int
            api["initialise"] = fn
            break
    else:
        raise ThalesSDKError("No MMMReader_Initialise export found in DLL.")

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

    # --- WaitForDocumentOnWindow(int timeout_ms) — Thales 3.9 blocking API ---
    if hasattr(dll, "MMMReader_WaitForDocumentOnWindow"):
        fn = getattr(dll, "MMMReader_WaitForDocumentOnWindow")
        fn.restype = c_int
        fn.argtypes = [c_int]
        api["wait_for_document"] = fn
    elif hasattr(dll, "MMMReader_WaitForDocument"):
        fn = getattr(dll, "MMMReader_WaitForDocument")
        fn.restype = c_int
        try:
            fn.argtypes = [c_uint32]
        except Exception:  # noqa: BLE001
            fn.argtypes = []
        api["wait_for_document"] = fn
        api["wait_for_document_legacy"] = True

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

    # --- GetData(enum type, void* buf, int* len, int index) ---
    if hasattr(dll, "MMMReader_GetData"):
        fn = getattr(dll, "MMMReader_GetData")
        fn.restype = c_int
        fn.argtypes = [c_int, c_void_p, POINTER(c_int), c_int]
        api["get_data"] = fn

    return api


def _call_initialise(api: dict[str, Any], application_ini_hint: Path | None = None) -> None:
    """
    ``MMMReader_Initialise`` per Thales ``MMMReaderHighLevelAPI.h`` (SDK 3.9.x):

    Blocking mode: data + event callbacks NULL; optional error callback; cert NULL;
    ``aProcessMessages`` / ``aProcessInputMessages`` as ``bool``; ``aParam`` NULL.

    **There is no Application.ini argument** — settings load from ``<SDK root>/Config`` relative
    to the process cwd (see ``FDN_THALES_SYNC_APPLICATION_INI`` / vendor ``Application.ini``).
    """
    fn = api["initialise"]
    fn.restype = c_int
    use_err_cb = os.environ.get("FDN_THALES_INIT_ERROR_CALLBACK", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
    logger.debug(
        "MMMReader_Initialise (7-arg, vendor layout); host Application.ini hint=%s",
        application_ini_hint,
    )
    if use_err_cb:
        fn.argtypes = [c_void_p, c_void_p, _ThalesErrorCallback, c_void_p, c_bool, c_bool, c_void_p]
        rc = fn(None, None, _thales_on_sdk_error, None, c_bool(True), c_bool(False), None)
    else:
        fn.argtypes = [c_void_p, c_void_p, c_void_p, c_void_p, c_bool, c_bool, c_void_p]
        rc = fn(None, None, None, None, c_bool(True), c_bool(False), None)

    _thales_pump_messages()

    if rc is None:
        return
    if isinstance(rc, int) and rc != 0:
        if _looks_like_nt_exception_code(rc):
            raise ThalesSDKError(
                "MMMReader_Initialise failed: native access violation (0xC0000005). "
                "Confirm cwd is the SDK install root (parent of Bin) so Config/Application.ini loads; "
                "see FDN_THALES_WORKING_DIR / FDN_THALES_SYNC_APPLICATION_INI; match "
                "MMMReaderHighLevelAPI.h if your SDK build differs."
            )
        raise ThalesSDKError(f"MMMReader_Initialise failed with code {rc}")


def _call_wait(api: dict[str, Any], timeout_ms: int) -> None:
    fn = api.get("wait_for_document")
    if fn is None:
        raise ThalesSDKError(
            "MMMReader_WaitForDocumentOnWindow not exported - use SDK 3.9+ High Level API DLL."
        )
    _thales_pump_messages()
    if api.get("wait_for_document_legacy"):
        if fn.argtypes and fn.argtypes[0] == c_uint32:
            rc = fn(c_uint32(timeout_ms))
        else:
            rc = fn()
    else:
        rc = fn(c_int(timeout_ms))
    _thales_pump_messages()
    if isinstance(rc, int) and rc != 0:
        if rc == THALES_ERROR_TIMED_OUT:
            raise ThalesSDKError(
                "MMMReader_WaitForDocumentOnWindow: ERROR_TIMED_OUT - no document within the timeout. "
                "Place a document on the reader or increase FDN_THALES_WAIT_TIMEOUT_MS.",
                code=THALES_ERROR_TIMED_OUT,
            )
        if rc == THALES_ERROR_READER_NOT_CONNECTED:
            raise ThalesSDKError(
                "MMMReader_WaitForDocumentOnWindow: ERROR_READER_NOT_CONNECTED (9) - the SDK does not see a "
                "page reader on USB. Check power and cable, install Thales drivers, try another USB port, "
                "close LowLevelAPITest / vendor apps that hold the device, and confirm the reader model "
                "matches the SDK (page reader vs swipe-only).",
                code=THALES_ERROR_READER_NOT_CONNECTED,
            )
        raise ThalesSDKError(
            f"MMMReader_WaitForDocumentOnWindow failed ({_describe_thales_error_code(rc)}). "
            "See Thales SDK logs under the install Logs folder."
        )


def _call_read(api: dict[str, Any]) -> None:
    fn = api.get("read_document")
    if fn is None:
        raise ThalesSDKError("MMMReader_ReadDocument not exported.")
    rc = fn()
    if isinstance(rc, int) and rc != 0:
        raise ThalesSDKError(f"ReadDocument failed (code {rc}). Unsupported or bad document.")


def _get_data_buffer(api: dict[str, Any], data_type: int) -> bytes:
    """Two-step ``MMMReader_GetData`` (query size with NULL buffer, then fill) per vendor docs."""
    fn = api.get("get_data")
    if fn is None:
        raise ThalesSDKError("MMMReader_GetData not exported.")

    idx = int(os.environ.get("FDN_THALES_GETDATA_INDEX", "0").strip() or "0")
    size = c_int(0)
    rc = fn(c_int(data_type), None, ctypes.byref(size), c_int(idx))
    last_rc = rc if isinstance(rc, int) else 0
    n = int(size.value)
    if n <= 0:
        raise ThalesSDKError(
            f"GetData(type={data_type}) size query returned len={n} (code {last_rc}). "
            "Enable the item in Config/Application.ini [DataToSend]."
        )
    # Unsigned octets: c_byte is signed and bytes() rejects values outside 0..255 for image/binary payloads.
    buf = (ctypes.c_ubyte * n)()
    size2 = c_int(n)
    rc2 = fn(c_int(data_type), ctypes.cast(buf, c_void_p), ctypes.byref(size2), c_int(idx))
    if isinstance(rc2, int) and rc2 != 0:
        raise ThalesSDKError(f"GetData(type={data_type}) read failed (code {rc2}).")
    written = int(size2.value)
    if written <= 0:
        return b""
    if written > n:
        logger.warning(
            "GetData(type=%s): SDK reported written=%s > allocated=%s; clamping (possible API mismatch).",
            data_type,
            written,
            n,
        )
        written = n
    raw = bytes(memoryview(buf)[:written])
    return raw.rstrip(b"\x00")


def _decode_blob(blob: bytes) -> str:
    for enc in ("utf-8", "utf-16-le", "cp1252"):
        try:
            return blob.decode(enc).strip("\x00").strip()
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8", errors="replace").strip()


_AAMVA_MAX_BUF = 3000


class _ThalesMMMReaderDate(ctypes.Structure):
    # MMMReaderDataTypes.h: #pragma pack(push, 1) — no padding between fields.
    _pack_ = 1
    _fields_ = [("Day", ctypes.c_int), ("Month", ctypes.c_int), ("Year", ctypes.c_int)]


class _ThalesMMMReaderAAMVAMeasurement(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("Measurement", ctypes.c_uint), ("Format", ctypes.c_int)]


class _ThalesMMMReaderAAMVAParsedData(ctypes.Structure):
    """Layout must match ``MMMReaderAAMVAParsedData`` in ``MMMReaderDataTypes.h`` (Thales 3.9.x, MSVC x64).
    The header uses #pragma pack(push, 1) — no alignment padding anywhere in the struct."""

    # Critical: without _pack_=1, ctypes adds 3 bytes between ShortSex(char) and
    # DateOfBirth(int-aligned), shifting every date and address field by 3 bytes.
    _pack_ = 1
    _fields_ = [
        ("LicenceNumber", ctypes.c_char * _AAMVA_MAX_BUF),
        ("FullName", ctypes.c_char * _AAMVA_MAX_BUF),
        ("Surname", ctypes.c_char * _AAMVA_MAX_BUF),
        ("Forename", ctypes.c_char * _AAMVA_MAX_BUF),
        ("MiddleName", ctypes.c_char * _AAMVA_MAX_BUF),
        ("NameSuffix", ctypes.c_char * 4),
        ("GivenNames", ctypes.c_char * _AAMVA_MAX_BUF),
        ("Sex", ctypes.c_char * 8),
        ("ShortSex", ctypes.c_char),
        ("DateOfBirth", _ThalesMMMReaderDate),
        ("IssueDate", _ThalesMMMReaderDate),
        ("ExpiryDate", _ThalesMMMReaderDate),
        ("AddressStreet", ctypes.c_char * _AAMVA_MAX_BUF),
        ("AddressCity", ctypes.c_char * _AAMVA_MAX_BUF),
        ("AddressState", ctypes.c_char * _AAMVA_MAX_BUF),
        ("AddressPostalCode", ctypes.c_char * _AAMVA_MAX_BUF),
        ("AddressCountry", ctypes.c_char * 4),
        ("Height", _ThalesMMMReaderAAMVAMeasurement),
        ("Weight", _ThalesMMMReaderAAMVAMeasurement),
        ("HairColour", ctypes.c_int),
        ("EyeColour", ctypes.c_int),
    ]


def _c_char_block_to_str(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _mmm_date_yyyymmdd(d: _ThalesMMMReaderDate) -> str:
    if d.Year <= 0 or d.Month <= 0 or d.Day <= 0:
        return ""
    return f"{d.Year:04d}{d.Month:02d}{d.Day:02d}"


def _aamva_getdata_bytes_to_tagtext(blob: bytes) -> str:
    """
    ``GetData(CD_AAMVA_DATA)`` returns an ``MMMReaderAAMVAData`` blob; ``Parsed`` is the first member
    (``MMMReaderAAMVAParsedData``). Decode that prefix into synthetic Dxx lines for :func:`_parse_aamva_pipe`.
    """
    if not blob or sys.platform != "win32":
        return ""
    need = ctypes.sizeof(_ThalesMMMReaderAAMVAParsedData)
    # rstrip(b"\x00") in _get_data_buffer may trim trailing zero-value struct fields
    # (HairColour=0, EyeColour=0, etc.) making the blob shorter than the struct.
    # Pad back to at least need bytes so from_buffer_copy succeeds.
    if len(blob) < need:
        blob = blob + b"\x00" * (need - len(blob))
    try:
        parsed = _ThalesMMMReaderAAMVAParsedData.from_buffer_copy(blob[:need])
    except (ValueError, ctypes.ArgumentError, TypeError):
        return ""
    lines: list[str] = []
    lic = _c_char_block_to_str(bytes(parsed.LicenceNumber))
    if lic:
        lines.append(f"DAQ{lic}")
    sur = _c_char_block_to_str(bytes(parsed.Surname))
    if sur:
        lines.append(f"DCS{sur}")
    fnm = _c_char_block_to_str(bytes(parsed.Forename))
    if fnm:
        lines.append(f"DAC{fnm}")
    mid = _c_char_block_to_str(bytes(parsed.MiddleName))
    if mid:
        lines.append(f"DAD{mid}")
    giv = _c_char_block_to_str(bytes(parsed.GivenNames))
    if giv and not fnm:
        # GivenNames may contain "First Middle" — emit as both DCT and DAC
        lines.append(f"DAC{giv}")
        lines.append(f"DCT{giv}")
    full = _c_char_block_to_str(bytes(parsed.FullName))
    if full:
        lines.append(f"DAA{full}")
        if not fnm and not giv:
            lines.append(f"DAC{full}")
    dob = _mmm_date_yyyymmdd(parsed.DateOfBirth)
    if dob:
        lines.append(f"DBB{dob}")
    exp = _mmm_date_yyyymmdd(parsed.ExpiryDate)
    if exp:
        lines.append(f"DBA{exp}")
    iss = _mmm_date_yyyymmdd(parsed.IssueDate)
    if iss:
        lines.append(f"DBD{iss}")
    sx = _c_char_block_to_str(bytes(parsed.Sex))
    if sx:
        lines.append(f"DBC{sx}")
    street = _c_char_block_to_str(bytes(parsed.AddressStreet))
    if street:
        lines.append(f"DAG{street}")
    city = _c_char_block_to_str(bytes(parsed.AddressCity))
    if city:
        lines.append(f"DAI{city}")
    state = _c_char_block_to_str(bytes(parsed.AddressState))
    if state:
        lines.append(f"DAJ{state}")
    postal = _c_char_block_to_str(bytes(parsed.AddressPostalCode))
    if postal:
        lines.append(f"DAK{postal}")
    return "\n".join(lines)


_MAX_CODELINE_LEN = 200


class _ThalesMMMReaderCodelineDataHead(ctypes.Structure):
    """Leading fields of ``MMMReaderCodelineData`` (``MAX_CODELINE_LENGTH`` = 200)."""

    _pack_ = 1
    _fields_ = [
        ("Data", ctypes.c_char * _MAX_CODELINE_LEN),
        ("LineCount", ctypes.c_int),
        ("Line1", ctypes.c_char * _MAX_CODELINE_LEN),
        ("Line2", ctypes.c_char * _MAX_CODELINE_LEN),
        ("Line3", ctypes.c_char * _MAX_CODELINE_LEN),
    ]


def _codeline_data_bytes_to_text(blob: bytes) -> str:
    """``GetData(CD_CODELINE_DATA)`` returns ``MMMReaderCodelineData``, not a plain string."""
    if not blob or sys.platform != "win32":
        return _decode_blob(blob)
    need = ctypes.sizeof(_ThalesMMMReaderCodelineDataHead)
    if len(blob) < need:
        return _decode_blob(blob)
    try:
        head = _ThalesMMMReaderCodelineDataHead.from_buffer_copy(blob[:need])
    except (ValueError, ctypes.ArgumentError, TypeError):
        return _decode_blob(blob)
    data = _c_char_block_to_str(bytes(head.Data))
    if data:
        return data.replace("\r", "\n").strip()
    lines: list[str] = []
    for field in (head.Line1, head.Line2, head.Line3):
        t = _c_char_block_to_str(bytes(field))
        if t:
            lines.append(t)
    if lines:
        return "\n".join(lines)
    return _decode_blob(blob)


_MAX_CODELINE_FIELD_LEN = 40
_MAX_OPTIONAL_DATA_LEN = 40
_MAX_CHECKDIGIT_COUNT = 5


class _ThalesMMMReaderCodelineCheckDigit(ctypes.Structure):
    # MMMReaderOCRDataTypes.h: #pragma pack(push, 1) — no padding between puValueRead(char) and puResult(int).
    _pack_ = 1
    _fields_ = [
        ("puCheckDigitType", ctypes.c_int),
        ("puCodelineNumber", ctypes.c_int),
        ("puCodelinePos", ctypes.c_int),
        ("puValueExpected", ctypes.c_char),
        ("puValueRead", ctypes.c_char),
        ("puResult", ctypes.c_int),
    ]


class _ThalesMMMReaderCodelineDataFull(ctypes.Structure):
    """Full ``MMMReaderCodelineData`` (Thales 3.9.x / MSVC x64) for ``GetData(CD_CODELINE_DATA)``."""

    _pack_ = 1
    _fields_ = [
        ("Data", ctypes.c_char * _MAX_CODELINE_LEN),
        ("LineCount", ctypes.c_int),
        ("Line1", ctypes.c_char * _MAX_CODELINE_LEN),
        ("Line2", ctypes.c_char * _MAX_CODELINE_LEN),
        ("Line3", ctypes.c_char * _MAX_CODELINE_LEN),
        ("DocId", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("DocType", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("Surname", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("Forename", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("SecondName", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("Forenames", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("DateOfBirthMRZ", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("ExpiryDateMRZ", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("DateOfBirth", _ThalesMMMReaderDate),
        ("ExpiryDate", _ThalesMMMReaderDate),
        ("IssuingState", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("Nationality", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("DocNumber", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("Sex", ctypes.c_char * _MAX_CODELINE_FIELD_LEN),
        ("ShortSex", ctypes.c_char),
        ("OptionalData1", ctypes.c_char * _MAX_OPTIONAL_DATA_LEN),
        ("OptionalData2", ctypes.c_char * _MAX_OPTIONAL_DATA_LEN),
        ("CheckDigitDataList", _ThalesMMMReaderCodelineCheckDigit * _MAX_CHECKDIGIT_COUNT),
        ("CheckDigitDataListCount", ctypes.c_int),
        ("CodelineValidationResult", ctypes.c_int),
        ("MrzOnRearSide", ctypes.c_bool),
        ("ExpiredDocumentFlag", ctypes.c_bool),
        ("ImageSource", ctypes.c_int),
    ]


def _codeline_sdk_parsed_fields(blob: bytes) -> dict[str, str]:
    """Extract Thales pre-parsed MRZ fields from ``MMMReaderCodelineData`` (``DocNumber``, names, etc.)."""
    if not blob or sys.platform != "win32":
        return {}
    need = ctypes.sizeof(_ThalesMMMReaderCodelineDataFull)
    if len(blob) < need:
        return {}
    try:
        cl = _ThalesMMMReaderCodelineDataFull.from_buffer_copy(blob[:need])
    except (ValueError, ctypes.ArgumentError, TypeError):
        return {}
    out: dict[str, str] = {}
    docn = _c_char_block_to_str(bytes(cl.DocNumber))
    if docn:
        out["document_number"] = docn
    sur = _c_char_block_to_str(bytes(cl.Surname))
    if sur:
        out["last_name"] = sur
    fore = _c_char_block_to_str(bytes(cl.Forename)) or _c_char_block_to_str(bytes(cl.Forenames))
    if fore:
        out["first_name"] = fore
    sec = _c_char_block_to_str(bytes(cl.SecondName))
    if sec:
        out["middle_name"] = sec
    dob_mrz = _c_char_block_to_str(bytes(cl.DateOfBirthMRZ))
    if dob_mrz:
        out["date_of_birth"] = dob_mrz
    elif _mmm_date_yyyymmdd(cl.DateOfBirth):
        out["date_of_birth"] = _mmm_date_yyyymmdd(cl.DateOfBirth)
    exp_mrz = _c_char_block_to_str(bytes(cl.ExpiryDateMRZ))
    if exp_mrz:
        out["expiry_date"] = exp_mrz
    elif _mmm_date_yyyymmdd(cl.ExpiryDate):
        out["expiry_date"] = _mmm_date_yyyymmdd(cl.ExpiryDate)
    nat = _c_char_block_to_str(bytes(cl.Nationality))
    if nat:
        out["nationality"] = nat
    sex_s = _c_char_block_to_str(bytes(cl.Sex))
    ch = bytes(cl.ShortSex)[:1]
    if ch and ch != b"\x00":
        c = chr(ch[0]).upper()
        if c in "MFU":
            out["gender"] = c
    elif sex_s:
        low = sex_s.lower()
        if "male" in low and "female" not in low:
            out["gender"] = "M"
        elif "female" in low:
            out["gender"] = "F"
    doc_type = _c_char_block_to_str(bytes(cl.DocType))
    if doc_type:
        out["doc_type"] = doc_type
    return out


def _apply_sdk_codeline_struct(structured: dict[str, Any], raw: bytes) -> None:
    """Fill empty structured fields from ``MMMReaderCodelineData`` (vendor OCR parse)."""
    sdk = _codeline_sdk_parsed_fields(raw)
    if not sdk:
        return

    def _fill(key: str, sdk_key: str) -> None:
        cur = (structured.get(key) or "").strip()
        if cur:
            return
        val = (sdk.get(sdk_key) or "").strip()
        if val:
            structured[key] = val

    _fill("document_number", "document_number")
    _fill("first_name", "first_name")
    _fill("middle_name", "middle_name")
    _fill("last_name", "last_name")
    _fill("date_of_birth", "date_of_birth")
    _fill("expiry_date", "expiry_date")
    _fill("nationality", "nationality")
    _fill("gender", "gender")

    fn = (structured.get("first_name") or "").strip()
    mid = (structured.get("middle_name") or "").strip()
    ln = (structured.get("last_name") or "").strip()
    if fn or mid or ln:
        structured["full_name"] = " ".join(x for x in (fn, mid, ln) if x).strip()

    mp = structured.get("mrz_parsed")
    if isinstance(mp, dict):
        if sdk.get("document_number") and not (mp.get("document_number") or "").strip():
            mp["document_number"] = sdk["document_number"]
        if sdk.get("last_name") and not (mp.get("surname") or "").strip():
            mp["surname"] = sdk["last_name"]
        if sdk.get("first_name") and not (mp.get("given_names") or "").strip():
            mp["given_names"] = sdk["first_name"]
        if sdk.get("doc_type") and not (mp.get("doc_type") or "").strip():
            mp["doc_type"] = sdk["doc_type"]


def _log_sdk_parsed_fields(
    structured: dict[str, Any],
    *,
    codeline_raw: str,
    aamva_raw: str,
) -> None:
    """Log every field the SDK returned so the native-host.log shows exactly what was read."""
    bar = "=" * 60
    s = structured
    lines = [
        bar,
        "THALES SDK — PARSED DOCUMENT DATA",
        bar,
        f"  Full name      : {s.get('full_name') or s.get('fullName') or '(empty)'}",
        f"  First name     : {s.get('first_name') or '(empty)'}",
        f"  Middle name    : {s.get('middle_name') or '(empty)'}",
        f"  Last name      : {s.get('last_name') or '(empty)'}",
        f"  Date of birth  : {s.get('date_of_birth') or '(empty)'}",
        f"  ID / DL number : {s.get('document_number') or '(empty)'}",
        f"  Document type  : {s.get('document_type') or '(empty)'}",
        f"  Expiry date    : {s.get('expiry_date') or '(empty)'}",
        f"  Issue date     : {s.get('issue_date') or '(empty)'}",
        f"  Gender         : {s.get('gender') or '(empty)'}",
        f"  Nationality    : {s.get('nationality') or '(empty)'}",
        f"  Street address : {s.get('street_address') or '(empty)'}",
        f"  City           : {s.get('city') or '(empty)'}",
        f"  State          : {s.get('state') or '(empty)'}",
        f"  Postal code    : {s.get('postal_code') or '(empty)'}",
        f"  MRZ raw        : {(codeline_raw or '')[:120] or '(empty)'}",
        f"  AAMVA raw tags : {(aamva_raw or '')[:200] or '(empty)'}",
        bar,
    ]
    logger.info("\n".join(lines))


def _log_aamva_getdata_empty_once() -> None:
    global _aamva_getdata_empty_hint_logged
    if _aamva_getdata_empty_hint_logged:
        return
    _aamva_getdata_empty_hint_logged = True
    logger.info(
        "CD_AAMVA_DATA GetData returned length 0 — PDF417/AAMVA is likely off in "
        "<SDK>\\Config\\Application.ini [DataToSend] (set PDF417=1 and AAMVAData=1), or sync/copy "
        "your project Application.ini there with Administrator rights."
    )


def _parse_aamva_pipe(text: str) -> dict[str, str]:
    """Best-effort AAMVA / DL barcode field extraction (pipe, line-based, or Dxx element tags)."""
    out: dict[str, str] = {}
    if not text:
        return out
    # ANSI D20 / AAMVA PDF417: three-letter element id immediately followed by value (often no space).
    _aamva_tags = (
        "DAA",  # full legal name (pre-2003)
        "DCS",  # last name / family name
        "DAC",  # first name (2000–2013)
        "DCT",  # given name(s) (2016+)
        "DAD",  # middle name
        "DBB",  # date of birth
        "DBA",  # expiry date
        "DBD",  # issue date
        "DAQ",  # DL/ID number
        "DBC",  # sex
        "DCA",  # vehicle class
        "DCB",  # restrictions
        "DCD",  # endorsements
        "DAG",  # street address
        "DAI",  # city
        "DAJ",  # state/province
        "DAK",  # postal code
        "DAL",  # street (pre-2000 mag-stripe)
        "DAN",  # city (pre-2000 mag-stripe)
        "DAO",  # state (pre-2000 mag-stripe)
        "DAP",  # postal (pre-2000 mag-stripe)
    )
    tag_alt = "|".join(_aamva_tags)

    def _trim_before_next_tag(raw: str) -> str:
        s = raw.strip()
        upper = s.upper()
        cut = len(s)
        for t in _aamva_tags:
            pos = upper.find(t, 1)
            if pos != -1 and pos < cut:
                cut = pos
        return s[:cut].strip()

    for m in re.finditer(
        rf"({tag_alt})([^\n\r|]{{1,120}})",
        text,
        re.I,
    ):
        tag, val = m.group(1).upper(), _trim_before_next_tag(m.group(2))
        out[tag] = val
    if "DAQ" in out and "document_number" not in out:
        # DAQ is commonly the license / customer ID number on US credentials.
        out["document_number"] = out["DAQ"].replace("*", "").strip()
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


def _mrz_norm_chars(line: str) -> str:
    return "".join(c for c in line.strip().upper() if c.isalnum() or c == "<")


def _parse_mrz_td1(lines: list[str]) -> dict[str, str]:
    """
    ICAO 9303 TD1: three lines of 30 characters (common on national ID cards).
    Also accepts one concatenated 90-character MRZ or two lines (MRZ lines 2+3 only).
    """
    normed: list[str] = []
    for ln in lines:
        t = _mrz_norm_chars(ln)
        if len(t) >= 29:
            normed.append((t + "<" * 30)[:30])
    if len(normed) == 1 and len(normed[0]) >= 90:
        blob = normed[0][:90]
        normed = [blob[0:30], blob[30:60], blob[60:90]]
    ln2 = ""
    ln3 = ""
    if len(normed) >= 3:
        ln2, ln3 = normed[1][:30], normed[2][:30]
    elif len(normed) == 2:
        ln2, ln3 = normed[0][:30], normed[1][:30]
    if len(ln2) != 30 or len(ln3) != 30:
        return {}

    doc_num = ln2[0:9].replace("<", "").strip()
    nationality = ln2[10:13].replace("<", "")
    dob = ln2[13:19]
    sex = ln2[20]
    expiry = ln2[21:27]
    surname, given = "", ""
    if "<<" in ln3:
        surname, _, given_part = ln3.partition("<<")
        surname = surname.replace("<", " ").strip()
        given = given_part.replace("<", " ").strip()
    return {
        "document_number": doc_num,
        "nationality": nationality,
        "date_of_birth_yymmdd": dob,
        "gender": sex if sex in "MF<" else "",
        "expiry_yymmdd": expiry,
        "surname": surname,
        "given_names": given,
        "mrz_format": "TD1",
    }


def _parse_mrz_lines(mrz: str) -> dict[str, str]:
    """
    Extract fields from ICAO 9303 MRZ: TD3 (passport, two lines of 44 chars) or TD1 (ID card, 30+30+30).
    """
    lines = [ln.strip() for ln in mrz.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) < 2:
        return {}
    line2 = lines[-1]
    line1 = lines[-2]

    # TD3 / passport: two long lines (44 characters per ICAO; accept 40+ for tolerant decoding).
    if len(line2) >= 40 and len(line1) >= 40:
        doc_num = line2[0:9].replace("<", "").strip()
        nationality = line2[10:13].replace("<", "")
        dob = line2[13:19]
        sex = line2[20]
        expiry = line2[21:27]
        surname = ""
        given = ""
        if line1.startswith("P<") or line1.startswith("I<") or line1.startswith("A<"):
            rest = line1[2:]
            name_field = rest[3:]
            if "<<" in name_field:
                surname, _, given_part = name_field.partition("<<")
                given = given_part.replace("<", " ").strip()
                surname = surname.replace("<", " ").strip()
        return {
            "document_number": doc_num,
            "nationality": nationality,
            "date_of_birth_yymmdd": dob,
            "gender": sex if sex in "MF<" else "",
            "expiry_yymmdd": expiry,
            "surname": surname,
            "given_names": given,
            "mrz_format": "TD3",
        }

    td1 = _parse_mrz_td1(lines)
    if td1:
        return td1

    return {
        "mrz_format": "unknown",
        "mrz_line1": (line1[:80] if line1 else ""),
        "mrz_line2": line2[:80],
    }


def _normalize_aamva_date_display(raw: str) -> str:
    """AAMVA PDF417 dates are usually 8 digits: MMDDCCYY or YYYYMMDD — normalize to M/D/YYYY display."""
    if not raw:
        return ""
    s = raw.strip()
    digits = re.sub(r"\D", "", s)
    if len(digits) != 8:
        return s
    y1 = int(digits[0:4])
    if 1900 <= y1 <= 2100:
        # YYYYMMDD
        m, d, y = int(digits[4:6]), int(digits[6:8]), y1
        return f"{m}/{d}/{y}"
    # MMDDCCYY (US common)
    mm, dd, ccyy = int(digits[0:2]), int(digits[2:4]), int(digits[4:8])
    if 1 <= mm <= 12 and 1 <= dd <= 31:
        return f"{mm}/{dd}/{ccyy}"
    return s


def _aamva_address_line(aamva: dict[str, str]) -> str:
    """Combine AAMVA address elements (DAG street / DAI city / DAJ state / DAK postal) when present."""
    parts: list[str] = []
    for key in ("DAG", "DAI", "DAJ", "DAK"):
        v = (aamva.get(key) or "").strip()
        if v:
            parts.append(v)
    return ", ".join(parts).strip()


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

    dob = mrz_fields.get("date_of_birth_yymmdd", "")
    exp = mrz_fields.get("expiry_yymmdd", "")
    iss = ""

    # Barcode (AAMVA) takes precedence for US DL — DBB=DOB, DBA=expiry, DBD=issue (8-digit dates).
    if (aamva_parsed.get("DBB") or "").strip():
        dob = _normalize_aamva_date_display(aamva_parsed["DBB"])
    if (aamva_parsed.get("DBA") or "").strip():
        exp = _normalize_aamva_date_display(aamva_parsed["DBA"])
    if (aamva_parsed.get("DBD") or "").strip():
        iss = _normalize_aamva_date_display(aamva_parsed["DBD"])

    # Fallback: loose key match (some exports use different labels)
    if not dob or not exp:
        for k, v in aamva_parsed.items():
            kl = k.lower()
            if not dob and ("dob" in kl or "birth" in kl) and v.strip():
                dob = _normalize_aamva_date_display(v) or v
            if not exp and ("expiry" in kl or "expiration" in kl) and v.strip():
                exp = _normalize_aamva_date_display(v) or v

    first = (mrz_fields.get("given_names") or "").strip()
    last = (mrz_fields.get("surname") or "").strip()
    middle = ""

    _aamva_has_name = (
        (aamva_parsed.get("DAC") or aamva_parsed.get("DCT") or "").strip()
        or (aamva_parsed.get("DCS") or "").strip()
        or (aamva_parsed.get("DAQ") or "").strip()
    )
    if _aamva_has_name:
        # DAC = first (2000–2013); DCT = given names (2016+); prefer DAC, fall back to DCT
        first = (aamva_parsed.get("DAC") or aamva_parsed.get("DCT") or first).strip()
        last = (aamva_parsed.get("DCS") or last).strip()
        middle = (aamva_parsed.get("DAD") or "").strip()

    doc_id = (
        (mrz_fields.get("document_number") or "").strip()
        or (aamva_parsed.get("document_number") or "").strip()
        or (aamva_parsed.get("DAQ") or "").replace("*", "").strip()
    )

    gender = (mrz_fields.get("gender") or "").strip()
    dbc = (aamva_parsed.get("DBC") or "").strip().upper()
    if dbc in ("1", "M", "MALE"):
        gender = "M"
    elif dbc in ("2", "F", "FEMALE"):
        gender = "F"
    elif not gender and dbc:
        gender = dbc[:1] if dbc[0] in "MFU" else gender

    addr = _aamva_address_line(aamva_parsed)

    full_name = ""
    if first or middle or last:
        full_name = " ".join(x for x in (first, middle, last) if x).strip()
    elif mrz_fields.get("given_names") or mrz_fields.get("surname"):
        full_name = f"{mrz_fields.get('given_names', '')} {mrz_fields.get('surname', '')}".strip()

    barcode_data: dict[str, Any] = dict(aamva_parsed)
    if aamva_text and not barcode_data:
        barcode_data["raw"] = aamva_text
    if aamva_text.strip():
        barcode_data["source"] = "pdf417_aamva"

    doc_type = ""
    if (aamva_parsed.get("DAQ") or "").strip() or (aamva_parsed.get("DCS") or "").strip():
        doc_type = "Driver License"

    return {
        "first_name": first,
        "middle_name": middle,
        "last_name": last,
        "document_number": doc_id,
        "date_of_birth": dob,
        "gender": gender,
        "nationality": mrz_fields.get("nationality", ""),
        "expiry_date": exp,
        "issue_date": iss,
        "address": addr,
        "street_address": (aamva_parsed.get("DAG") or "").strip(),
        "city": (aamva_parsed.get("DAI") or "").strip(),
        "state": (aamva_parsed.get("DAJ") or "").strip(),
        "postal_code": (aamva_parsed.get("DAK") or "").strip(),
        "mrz_raw": codeline_raw or "",
        "mrz_parsed": mrz_fields,
        "barcode_data": barcode_data,
        "full_name": full_name,
        "document_type": doc_type,
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
        os.environ.get("FDN_THALES_WAIT_TIMEOUT_MS", "180000").strip() or "180000"
    )

    com_uninit = False
    try:
        com_uninit = _thales_com_init()

        dll = _load_high_level_dll()
        _maybe_sync_application_ini(ini, Path.cwd())
        api = _bind_api(dll)

        _call_initialise(api, application_ini_hint=ini)
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
                msg = str(exc)
                if "len=0" in msg:
                    _log_aamva_getdata_empty_once()
                else:
                    logger.warning("CD_AAMVA_DATA GetData: %s", exc)

            codeline_s = _decode_blob(raw_code)
            codeline_data_s = _codeline_data_bytes_to_text(raw_data)
            aamva_s = _aamva_getdata_bytes_to_tagtext(raw_aamva) or _decode_blob(raw_aamva)

            if not (codeline_s or codeline_data_s or aamva_s):
                raise ThalesSDKError("No document data returned (empty read). Check Application.ini [DataToSend].")

            structured = build_structured_payload(
                codeline_raw=codeline_s,
                codeline_data_text=codeline_data_s,
                aamva_text=aamva_s,
            )
            _apply_sdk_codeline_struct(structured, raw_data)
            _log_sdk_parsed_fields(structured, codeline_raw=codeline_s, aamva_raw=aamva_s)

            visible_image_base64 = ""
            visible_image_rear_base64 = ""
            fetch_vis = os.environ.get("FDN_THALES_FETCH_VISIBLE_IMAGE", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if fetch_vis:
                if CD_VISIBLE_IMAGE > 0:
                    try:
                        raw_vis = _get_data_buffer(api, CD_VISIBLE_IMAGE)
                        if raw_vis:
                            visible_image_base64 = bytes_to_base64(raw_vis)
                    except ThalesSDKError as exc:
                        logger.warning("Visible image (front) GetData: %s", exc)
                if CD_VISIBLE_IMAGE_REAR > 0:
                    try:
                        raw_vis_rear = _get_data_buffer(api, CD_VISIBLE_IMAGE_REAR)
                        if raw_vis_rear:
                            visible_image_rear_base64 = bytes_to_base64(raw_vis_rear)
                    except ThalesSDKError as exc:
                        logger.warning("Visible image (rear) GetData: %s", exc)

                front_bytes = len(visible_image_base64)
                rear_bytes = len(visible_image_rear_base64)
                if front_bytes and rear_bytes:
                    same = visible_image_base64 == visible_image_rear_base64
                    logger.info(
                        "Image fetch: front_b64_chars=%d rear_b64_chars=%d identical=%s",
                        front_bytes, rear_bytes, same,
                    )
                    if same:
                        logger.warning(
                            "CD_IMAGEVIS(%d) and CD_IMAGEVISREAR(%d) returned identical bytes — "
                            "extension will show the same image in both FRONT and BACK slots.",
                            CD_VISIBLE_IMAGE, CD_VISIBLE_IMAGE_REAR,
                        )
                else:
                    logger.info(
                        "Image fetch: front_b64_chars=%d rear_b64_chars=%d",
                        front_bytes, rear_bytes,
                    )
                    if not front_bytes:
                        logger.warning("CD_IMAGEVIS(%d) returned no data — FRONT slot will be empty.", CD_VISIBLE_IMAGE)
                    if not rear_bytes:
                        logger.warning("CD_IMAGEVISREAR(%d) returned no data — BACK slot will be empty.", CD_VISIBLE_IMAGE_REAR)

            if "clear_data" in api:
                try:
                    api["clear_data"]()
                except Exception:  # noqa: BLE001
                    logger.exception("MMMReader_ClearData raised")

            return {
                "codeline_raw": codeline_s,
                "codeline_data_raw": codeline_data_s,
                "aamva_raw": aamva_s,
                "structured": structured,
                "visible_image_base64": visible_image_base64,
                "visible_image_rear_base64": visible_image_rear_base64,
            }
        finally:
            if initialised:
                try:
                    api["shutdown"]()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("MMMReader_Shutdown: %s", exc)
    finally:
        _thales_com_uninit(com_uninit)


def read_document_safe() -> dict[str, Any]:
    """
    Same as :func:`read_document_blocking`, but returns a dict instead of raising for common cases:

    - ``{"type": "SDK_DOCUMENT_OK", ...}`` on success
    - ``{"type": "NO_DOCUMENT", "message": ...}`` when WaitForDocument times out (idle reader)
    - ``{"type": "ERROR", "message": ...}`` for other :class:`ThalesSDKError` / OS failures
    """
    if sys.platform != "win32":
        return {"type": "ERROR", "message": "Thales MMMReader SDK is only supported on Windows."}
    try:
        data = read_document_blocking()
        return {"type": "SDK_DOCUMENT_OK", **data}
    except ThalesSDKError as exc:
        msg = str(exc)
        # Normal idle polling: do not treat as a hard failure for auto-watch callers.
        if getattr(exc, "code", None) == THALES_ERROR_TIMED_OUT or "ERROR_TIMED_OUT" in msg:
            return {"type": "NO_DOCUMENT", "message": msg}
        return {"type": "ERROR", "message": msg}
    except ValueError as exc:
        return {"type": "ERROR", "message": f"Buffer handling error (report if reproducible): {exc}"}
    except OSError as exc:
        return {"type": "ERROR", "message": f"OS error: {exc}"}
