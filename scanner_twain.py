"""
TWAIN acquisition for Windows scanners (e.g. Thales QS2000) using pytwain.

Falls back to a local sample image when TWAIN is unavailable or no sources exist.
On fatal acquisition errors, returns ``{"type": "ERROR", "message": "..."}``.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from utils import bytes_to_base64, file_to_base64

logger = logging.getLogger(__name__)

_HOST_DIR = Path(__file__).resolve().parent
_DEFAULT_SAMPLE = _HOST_DIR / "samples" / "id_card.png"

_PREFERRED_ENV = "FDN_TWAIN_PREFERRED"
_FORCE_SIM_ENV = "FDN_FORCE_TWAIN_SIMULATION"
_DSM_PATH_ENV = "FDN_TWAIN_DSM_PATH"
_SHOW_UI_ENV = "FDN_TWAIN_SHOW_UI"

_CANDIDATE_DSM_PATHS = [
    r"C:\Program Files (x86)\Ambir Technology\AmbirScanX\TWAINDSM.dll",
    r"C:\Program Files\Ambir Technology\AmbirScanX\TWAINDSM.dll",
    r"C:\Program Files (x86)\Common Files\TWAIN\twaindsm.dll",
    r"C:\Windows\System32\twaindsm.dll",
    r"C:\Windows\SysWOW64\twaindsm.dll",
]

try:
    import twain  # type: ignore[import-untyped]
except ImportError:
    twain = None  # type: ignore[assignment]


def _twain_usable() -> bool:
    if twain is None:
        return False
    if sys.platform not in ("win32", "darwin"):
        return False
    return True


def _force_simulation() -> bool:
    v = os.environ.get(_FORCE_SIM_ENV, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _dsm_name() -> str | None:
    raw = os.environ.get(_DSM_PATH_ENV, "").strip()
    if raw:
        return raw
    for candidate in _CANDIDATE_DSM_PATHS:
        if Path(candidate).is_file():
            logger.info("Auto-detected TWAIN DSM: %s", candidate)
            return candidate
    return None


def _preferred_substring() -> str:
    return (os.environ.get(_PREFERRED_ENV, "QS2000").strip() or "QS2000")


def _show_ui_flags() -> tuple[bool, bool]:
    v = os.environ.get(_SHOW_UI_ENV, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True, True
    return False, False


def _log_detection_banner(sources: list[str]) -> None:
    if not sources:
        logger.debug("TWAIN: no devices found")
    else:
        logger.info("TWAIN devices: %s", ", ".join(sources))


def _log_scan_banner(*, device: str, simulated: bool) -> None:
    if simulated:
        logger.debug("TWAIN: simulation mode — device=%r", device)
    else:
        logger.info("TWAIN: scanning with %r — waiting for card", device)


def _log_scan_done(*, success: bool, simulated: bool) -> None:
    if success:
        logger.debug("TWAIN: scan done (simulated=%s)", simulated)
    else:
        logger.debug("TWAIN: scan did not complete")


def list_scanners() -> list[str]:
    """
    Return TWAIN source names (ProductName) reported by the Data Source Manager.

    Returns an empty list if TWAIN is unavailable, the DSM cannot load, or there are no sources.
    """
    if not _twain_usable():
        _log_detection_banner([])
        return []

    dsm = _dsm_name()
    try:
        sm = twain.SourceManager(None, dsm_name=dsm)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 — TWAIN init: many exception types
        logger.warning("TWAIN Source Manager could not start: %s", exc)
        _log_detection_banner([])
        return []

    try:
        sources = list(sm.source_list)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not enumerate TWAIN sources: %s", exc)
        sources = []
    finally:
        try:
            sm.close()
        except Exception:  # noqa: BLE001
            logger.debug("Ignoring error while closing TWAIN Source Manager", exc_info=True)

    _log_detection_banner(sources)
    return sources


def _pick_try_order(sources: list[str], preferred: str) -> list[str]:
    """Prefer a source whose name contains ``preferred`` (case-insensitive), then others."""
    pref_l = preferred.lower()
    primary: list[str] = []
    rest: list[str] = []
    for name in sources:
        if pref_l and pref_l in name.lower():
            primary.append(name)
        else:
            rest.append(name)
    ordered = primary + rest
    seen: set[str] = set()
    out: list[str] = []
    for n in ordered:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _simulation_payload(*, sample_path: Path, reason: str) -> dict[str, Any]:
    if not sample_path.is_file():
        return {"type": "ERROR", "message": f"Sample ID image not found: {sample_path}"}
    b64 = file_to_base64(sample_path)
    logger.info("Simulation fallback (%s) using %s", reason, sample_path)
    _log_scan_done(success=True, simulated=True)
    return {
        "type": "TWAIN_OK",
        "image_base64": b64,
        "source_name": sample_path.name,
        "simulated": True,
    }


def _acquire_bmp_bytes(src: Any, *, show_ui: bool, modal: bool) -> bytes:
    """Run one native transfer via pytwain's modal loop; returns BMP file bytes."""
    captured: list[bytes] = []

    def after(img: Any, more: int) -> None:
        fd, tmp_path = tempfile.mkstemp(suffix=".bmp")
        os.close(fd)
        tmp = Path(tmp_path)
        try:
            img.save(str(tmp))
            captured.append(tmp.read_bytes())
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
        img.close()
        if more:
            raise twain.exceptions.CancelAll  # type: ignore[union-attr]

    src.acquire_natively(after=after, show_ui=show_ui, modal=modal)  # type: ignore[union-attr]
    if not captured:
        raise RuntimeError("TWAIN native transfer produced no image data")
    return captured[0]


def scan_image(
    *,
    preferred_substring: str | None = None,
    sample_path: Path | None = None,
    show_ui: bool | None = None,
    modal_ui: bool | None = None,
) -> dict[str, Any]:
    """
    Acquire one image via TWAIN and return base64-encoded BMP bytes.

    Returns on success::

        {
            "type": "TWAIN_OK",
            "image_base64": "<string>",
            "source_name": "<TWAIN product name or sample filename>",
            "simulated": <bool>,
        }

    Returns on failure::

        {"type": "ERROR", "message": "<reason>"}
    """
    sample = sample_path or _DEFAULT_SAMPLE
    preferred = (preferred_substring or _preferred_substring()).strip()

    if _force_simulation() or not _twain_usable():
        reason = "FDN_FORCE_TWAIN_SIMULATION" if _force_simulation() else "TWAIN not available on this platform or pytwain not installed"
        _log_scan_banner(device=sample.name, simulated=True)
        return _simulation_payload(sample_path=sample, reason=reason)

    env_su, env_mu = _show_ui_flags()
    su = env_su if show_ui is None else bool(show_ui)
    mu = env_mu if modal_ui is None else bool(modal_ui)

    dsm = _dsm_name()
    try:
        sm = twain.SourceManager(None, dsm_name=dsm)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001
        logger.warning("TWAIN Source Manager could not start: %s", exc)
        _log_scan_banner(device=sample.name, simulated=True)
        return _simulation_payload(sample_path=sample, reason="DSM load or open failed")

    sources: list[str] = []
    try:
        sources = list(sm.source_list)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not enumerate TWAIN sources: %s", exc)
    finally:
        if not sources:
            try:
                sm.close()
            except Exception:  # noqa: BLE001
                pass
            _log_scan_banner(device=sample.name, simulated=True)
            return _simulation_payload(sample_path=sample, reason="no TWAIN sources")

    _log_detection_banner(sources)

    try_order = _pick_try_order(sources, preferred)
    src: Any | None = None
    used_name = ""
    last_open_error: str | None = None

    for name in try_order:
        try:
            src = sm.open_source(name)
        except Exception as exc:  # noqa: BLE001
            last_open_error = str(exc)
            logger.warning("open_source(%r) failed: %s", name, exc)
            src = None
        if src is not None:
            used_name = name
            break

    if src is None:
        try:
            sm.close()
        except Exception:  # noqa: BLE001
            pass
        msg = "Scanner not found or failed"
        if last_open_error:
            msg = f"Device not available ({last_open_error})"
        logger.error("%s", msg)
        _log_scan_done(success=False, simulated=False)
        return {"type": "ERROR", "message": msg}

    _log_scan_banner(device=used_name, simulated=False)

    try:
        try:
            bmp = _acquire_bmp_bytes(src, show_ui=su, modal=mu)
        except twain.exceptions.DSTransferCancelled as exc:  # type: ignore[union-attr]
            logger.error("TWAIN transfer cancelled: %s", exc)
            _log_scan_done(success=False, simulated=False)
            return {"type": "ERROR", "message": "Scanner transfer was cancelled or failed"}
        except twain.exceptions.TwainError as exc:  # type: ignore[union-attr]
            logger.exception("TWAIN error during acquisition: %s", exc)
            _log_scan_done(success=False, simulated=False)
            return {"type": "ERROR", "message": "Scanner not found or failed"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during TWAIN acquisition: %s", exc)
            _log_scan_done(success=False, simulated=False)
            return {"type": "ERROR", "message": "Scanner not found or failed"}

        b64 = bytes_to_base64(bmp)
        _log_scan_done(success=True, simulated=False)
        return {
            "type": "TWAIN_OK",
            "image_base64": b64,
            "source_name": used_name,
            "simulated": False,
        }
    finally:
        try:
            src.close()
        except Exception:  # noqa: BLE001
            logger.debug("Error closing TWAIN source", exc_info=True)
        try:
            sm.close()
        except Exception:  # noqa: BLE001
            logger.debug("Error closing TWAIN Source Manager", exc_info=True)
