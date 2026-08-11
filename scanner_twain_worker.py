"""
Subprocess worker for AMBIR nScan 690gt TWAIN scan.

Spawned by scanner_nscan690gt.scan_document() so the pytwain modal loop owns this
process's main thread (required for Windows message pump to receive MSG_XFERREADY).
Prints one JSON object to stdout and exits.
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

_HOST_DIR = Path(__file__).resolve().parent
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

# Keep worker output clean — only warnings+errors on stderr
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
)


def main() -> None:
    from scanner_twain import scan_image

    result = scan_image(
        preferred_substring="690gt",
        sample_path=_HOST_DIR / "samples" / "id_card.png",
        show_ui=False,
        modal_ui=False,
    )
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        sys.stdout.write(json.dumps({"type": "ERROR", "message": str(exc)}))
        sys.stdout.flush()
        sys.exit(1)