"""
Subprocess worker for AMBIR nScan 690gt TWAIN scan.

Spawned by scanner_nscan690gt.scan_document() so the pytwain modal loop owns this
process's main thread (required for Windows message pump to receive MSG_XFERREADY).
Prints one JSON object to stdout and exits.

Logging: writes INFO+ into the same native-host.log when FDN_NSCAN690GT_LOG_FILE
(or FDN_LOG_FILE) is set by the parent, and also mirrors to stderr for the parent.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_HOST_DIR = Path(__file__).resolve().parent
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

_LOG_TAG = "[nScan690gt][worker]"


def _configure_logging() -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")

    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    log_path = (
        os.environ.get("FDN_NSCAN690GT_LOG_FILE", "").strip()
        or os.environ.get("FDN_LOG_FILE", "").strip()
    )
    if not log_path:
        return
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8", mode="a")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        logging.getLogger(__name__).warning("%s could not open log file %s: %s", _LOG_TAG, log_path, exc)


def main() -> None:
    from scanner_twain import scan_image

    log = logging.getLogger(__name__)
    log.info("%s start — preferred_substring=690gt show_ui=False", _LOG_TAG)
    log.info(
        "%s about to open TWAIN DSM / source and wait for insert (MSG_XFERREADY)",
        _LOG_TAG,
    )

    result = scan_image(
        preferred_substring="690gt",
        sample_path=_HOST_DIR / "samples" / "id_card.png",
        show_ui=False,
        modal_ui=False,
    )

    log.info(
        "%s scan_image returned type=%s source=%r simulated=%s",
        _LOG_TAG,
        result.get("type"),
        result.get("source_name"),
        result.get("simulated"),
    )
    if result.get("simulated"):
        log.warning(
            "%s simulated=true — no hardware image; check TWAIN DSM and source list",
            _LOG_TAG,
        )

    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    _configure_logging()
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).exception("%s fatal: %s", _LOG_TAG, exc)
        sys.stdout.write(json.dumps({"type": "ERROR", "message": str(exc)}))
        sys.stdout.flush()
        sys.exit(1)
