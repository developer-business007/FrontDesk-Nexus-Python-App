"""
FrontDesk Nexus — RFID encoder standalone test script.
Run this directly in a terminal (NOT through Chrome) to verify hardware works.

Usage:
  cd PythonNativeMessagingHost/Python-App
  python test_rfid.py

Steps it runs:
  1. Load DLL
  2. Handshake (connect)
  3. IsHandShake (quick re-check)
  4. Make a test key (requires blank card on reader)
  5. Read card back
  6. Disable card (requires card on reader)

Edit HOTEL_ID and AUTH_CODE below before running with real hardware.
Keep SIMULATION=True for a dry run with no hardware needed.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

# ── Config — edit these before live test ─────────────────────────────────────
HOTEL_ID    = "2108"        # 4-digit hotel ID from Elox setup
AUTH_CODE   = "80662903"    # 8-digit authorization code from Elox setup
ROOM_NUMBER = "108"         # any room number to test with
CHECKIN     = "2026-05-11T00:00:00"
CHECKOUT    = "2026-05-13T00:00:00"
SIMULATION  = False         # set True to run without hardware
COM_PORT    = "COM6"        # USB-serial port for the RFID encoder
                            # Open Device Manager → Ports (COM & LPT) to find it
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
)

# Make sure we find the module
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rfid_encoder  # noqa: E402

os.environ["FDN_RFID_COM_PORT"] = COM_PORT

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"


def check_com_port(port: str) -> bool:
    """Try to open the COM port via PowerShell. Returns True if free/accessible."""
    cmd = (
        f'$p = New-Object System.IO.Ports.SerialPort("{port}", 9600); '
        f'try {{ $p.Open(); "FREE"; $p.Close() }} '
        f'catch {{ "BUSY: $($_.Exception.Message)" }}'
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=6,
        )
        out = r.stdout.strip()
    except Exception as e:
        out = f"ERROR: {e}"

    if out == "FREE":
        print(f"  {PASS}  {port} is accessible (not held by another program)")
        return True
    else:
        print(f"  {FAIL}  {port} is NOT accessible — {out}")
        print(f"         → Close INNGuru / any serial terminal using {port} and retry.")
        return False


def check(label: str, result: dict) -> bool:
    ok = result.get("success") is True
    status = PASS if ok else FAIL
    print(f"  {status}  {label}")
    print(f"         success={result.get('success')}  "
          f"return_msg={result.get('return_msg')!r}  "
          f"error={result.get('error')!r}")
    for k, v in result.items():
        if k not in ("success", "return_msg", "error", "type"):
            print(f"         {k}={v!r}")
    return ok


def run_simulation():
    print("\n══ SIMULATION MODE (no hardware) ══════════════════════════════")
    tests = [
        ("RFID_HANDSHAKE",    {"type": "RFID_HANDSHAKE",   "hotel_id": HOTEL_ID}),
        ("RFID_MAKE_KEY",     {"type": "RFID_MAKE_KEY",    "room_number": ROOM_NUMBER,
                               "checkin_time": CHECKIN, "checkout_time": CHECKOUT,
                               "card_serial": 1}),
        ("RFID_READ_CARD",    {"type": "RFID_READ_CARD"}),
        ("RFID_DISABLE_CARD", {"type": "RFID_DISABLE_CARD", "room_number": ROOM_NUMBER}),
        ("RFID_ENABLE_CARD",  {"type": "RFID_ENABLE_CARD",  "room_number": ROOM_NUMBER,
                               "checkin_time": CHECKIN, "checkout_time": CHECKOUT}),
    ]
    all_pass = True
    for label, payload in tests:
        result = rfid_encoder.handle_rfid_command_simulated(payload)
        all_pass = check(label, result) and all_pass
    return all_pass


def run_hardware():
    print("\n══ HARDWARE MODE ═══════════════════════════════════════════════")
    print(f"  hotel_id={HOTEL_ID!r}  auth_code={AUTH_CODE!r}  room={ROOM_NUMBER!r}")
    all_pass = True

    # ── Step 0: COM port accessibility check ─────────────────────────────────
    print(f"\n[0] Checking {COM_PORT} is free …")
    if not check_com_port(COM_PORT):
        return False

    # ── Step 1: Load DLL ──────────────────────────────────────────────────────
    print("\n[1] Loading EloxReaderSDK.dll …")
    try:
        rfid_encoder._load_sdk()
        print(f"  {PASS}  DLL loaded")
    except rfid_encoder.RfidEncoderError as e:
        print(f"  {FAIL}  DLL load failed: {e}")
        print("\n  → Make sure EloxReaderSDK.dll and Newtonsoft.Json.dll are in Python-App/")
        print("  → Install pythonnet: pip install pythonnet")
        return False

    # ── Step 2: HandShake ─────────────────────────────────────────────────────
    print("\n[2] HandShake() — opening serial port + contacting device …")
    result = rfid_encoder.rfid_handshake(HOTEL_ID, AUTH_CODE)
    ok = check("HandShake()", result)
    all_pass = all_pass and ok
    if not ok or not result.get("connected"):
        print("\n  → Device not responding. Check:")
        print("    • USB cable is plugged in")
        print("    • Device is powered on")
        print("    • No other software is using the same COM port")
        print("    • Run 'mode' in cmd.exe to see available COM ports")
        return False

    # ── Step 3: IsHandShake (quick re-check) ──────────────────────────────────
    print("\n[3] IsHandShake() — lightweight connection re-check …")
    ph = rfid_encoder._get_instance()
    is_ok = bool(ph.IsHandShake())
    status = PASS if is_ok else FAIL
    print(f"  {status}  IsHandShake() → {is_ok}")
    all_pass = all_pass and is_ok

    # ── Step 4: Make Key ──────────────────────────────────────────────────────
    print(f"\n[4] RFID_MAKE_KEY — room={ROOM_NUMBER}  serial=1")
    input("  → Place a BLANK key card on the encoder, then press ENTER …")
    result = rfid_encoder.rfid_make_key(
        HOTEL_ID, AUTH_CODE, ROOM_NUMBER, CHECKIN, CHECKOUT, card_serial=1
    )
    ok = check("rfid_make_key()", result)
    all_pass = all_pass and ok
    if result.get("return_msg") == "A0AA":
        print("  → No card detected. Place card flat and retry.")

    # ── Step 5: Read Card ─────────────────────────────────────────────────────
    print("\n[5] RFID_READ_CARD — reading back the card just written …")
    input("  → Keep the card on the encoder, then press ENTER …")
    result = rfid_encoder.rfid_read_card()
    check("rfid_read_card()", result)

    # ── Step 6: Disable Card ──────────────────────────────────────────────────
    print("\n[6] RFID_DISABLE_CARD — writing cancel card (blocks room access) …")
    input("  → Keep the same card on the encoder, then press ENTER …")
    result = rfid_encoder.rfid_disable_card(HOTEL_ID, AUTH_CODE, ROOM_NUMBER)
    ok = check("rfid_disable_card()", result)
    all_pass = all_pass and ok

    # ── Step 7: Duplicate key (serial=2) ────────────────────────────────────
    print("\n[7] RFID_MAKE_KEY serial=2 — making a duplicate key …")
    input("  → Place a SECOND blank card on the encoder, then press ENTER …")
    result = rfid_encoder.rfid_make_key(
        HOTEL_ID, AUTH_CODE, ROOM_NUMBER, CHECKIN, CHECKOUT, card_serial=2
    )
    ok = check("rfid_make_key(serial=2)", result)
    all_pass = all_pass and ok

    return all_pass


def main():
    print("═══════════════════════════════════════════════════════════")
    print("  FrontDesk Nexus — RFID Encoder Test")
    print("═══════════════════════════════════════════════════════════")

    if SIMULATION:
        passed = run_simulation()
    else:
        passed = run_hardware()

    print("\n═══════════════════════════════════════════════════════════")
    if passed:
        print(f"  {PASS}  All tests passed")
    else:
        print(f"  {FAIL}  Some tests failed — see output above")
    print("═══════════════════════════════════════════════════════════\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
