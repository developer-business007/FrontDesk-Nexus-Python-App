"""
Quick read-only test: place an already-encoded card on the reader and run this.
Shows the raw card bytes and the dates at the fixed SDK offsets used by the extension.

Usage:
  cd PythonNativeMessagingHost/Python-App
  python read_card_test.py
"""

import os
import sys
from pathlib import Path

COM_PORT = "COM6"   # change if your encoder is on a different port

os.environ["FDN_RFID_COM_PORT"] = COM_PORT
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rfid_encoder  # noqa: E402

print("Place the encoded card flat on the reader, then press ENTER ...")
input()

print("Reading card ...")
result = rfid_encoder.rfid_read_card()

if not result["success"]:
    print(f"\nFAIL: {result['error']}")
    sys.exit(1)

card = str(result.get("card_data") or result.get("return_msg") or "")

print(f"\nRaw card bytes ({len(card)} chars):")
print(f"  {card}")

if len(card) >= 39:
    checkin = card[15:27]   # yyyyMMddHHmm — standard
    co_raw  = card[27:39]   # SDK variant format for months 1–9
    co_mm   = int(co_raw[4:6])
    if co_mm > 12:
        co_month = int(co_raw[5:6])
        checkout = f"{co_raw[0:4]}{co_month:02d}{co_raw[6:]}"
    else:
        checkout = co_raw
    print(f"\nDates parsed from raw card bytes:")
    print(f"  Check-in  [15:27] = {checkin}  (raw: {checkin})")
    print(f"  Check-out [27:39] = {checkout}  (raw: {co_raw})")
else:
    print("\nCard string too short to read date offsets.")

print(f"\nPython _parse_card_data result:")
print(f"  room     = {result.get('room_number')}")
print(f"  serial   = {result.get('card_serial')}")
print(f"  check-in = {result.get('checkin_time')}")
print(f"  check-out= {result.get('checkout_time')}")
