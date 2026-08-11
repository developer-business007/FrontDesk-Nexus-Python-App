"""
FrontDesk Nexus — RFID Key Card Encoder module.

Verified against EloxReaderSDK.dll v1.0.0.0 via .NET reflection.

DLL location  : PythonNativeMessagingHost/Python-App/EloxReaderSDK.dll
                                                      Newtonsoft.Json.dll
Install dep   : pip install pythonnet>=3.0.3

── PortHelper API (instance methods, not static) ────────────────────────────
  ph = PortHelper()                          create instance (opens serial port on HandShake)
  ph.HandShake()         → bool              open port + send init command; True = device responded
  ph.IsHandShake()       → bool              lightweight check: is port still open and device alive?
  ph.GetStr16(json_str)  → str               encode card JSON → SDK hex string
  ph.MakeCard(str16, ref returnmsg) → bool   write to card; returnmsg = "A0" ok / "A0AA" no card
  ph.ReadCardCK()        → str               read card data; returns raw string
  ph.mkPort              : SerialPort        underlying serial port (internal; do not call directly)

── CardEntity properties (for reference; we use JSON string approach, not CardEntity directly) ──
  card_type, card_id, hotel_id, room_number, cardserial_number,
  checkin_time, checkout_time, authorization_code,
  area_code, zone_code, building_code, nouse_code,
  checkbox_time1, checkbox_time2,
  time_window1 / time_window1_1 / time_window1_2
  time_window2 / time_window2_1 / time_window2_2
  time_window3 / time_window3_1 / time_window3_2

── SDK return codes ──────────────────────────────────────────────────────────
  "A0"    = success (card written or read OK)
  "A0AA"  = no card on reader (place card, retry)
  ""      = device not responding or command failed

── Card JSON format ─────────────────────────────────────────────────────────
  hotel_id           : 4-digit left-padded  ("42" → "0042")
  room_number        : 6-digit left-padded + "00" suffix  ("600" → "00060000")
  checkin/checkout   : yyyyMMddHHmm  ("2025-05-07T14:00" → "202505071400")
  cardserial_number  : "1" = primary key, "2"–"8" = duplicate copies
  authorization_code : 8-digit  ("33333333")
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODULE_DIR = Path(__file__).resolve().parent
_ELOX_DLL   = "EloxReaderSDK"
_NEWTON_DLL = "Newtonsoft.Json"

# ── Module-level cache ────────────────────────────────────────────────────────
# We cache the PortHelper CLASS (from _load_sdk) and one INSTANCE (_get_instance).
# The instance holds the open SerialPort (mkPort field) — reuse it across calls.

_PortHelperClass: Any = None   # the EloxReaderSDK.PortHelper type
_ph_instance:     Any = None   # one PortHelper() instance per process
_load_error: str | None = None


class RfidEncoderError(Exception):
    """Raised for RFID hardware / DLL errors."""


# ── SDK loading ───────────────────────────────────────────────────────────────

def _load_sdk() -> Any:
    """
    Load EloxReaderSDK.dll via pythonnet and return the PortHelper class.
    Called once per process; result cached in _PortHelperClass.
    """
    global _PortHelperClass, _load_error

    if _PortHelperClass is not None:
        return _PortHelperClass

    dll_path = _MODULE_DIR / f"{_ELOX_DLL}.dll"
    if not dll_path.exists():
        _load_error = (
            f"EloxReaderSDK.dll not found at {dll_path}. "
            "Copy EloxReaderSDK.dll and Newtonsoft.Json.dll into "
            "PythonNativeMessagingHost/Python-App/."
        )
        raise RfidEncoderError(_load_error)

    try:
        if str(_MODULE_DIR) not in sys.path:
            sys.path.insert(0, str(_MODULE_DIR))

        import clr  # pythonnet >= 3.0  — pip install pythonnet  # noqa: PLC0415

        clr.AddReference(str(_MODULE_DIR / _NEWTON_DLL))
        clr.AddReference(str(_MODULE_DIR / _ELOX_DLL))

        from EloxReaderSDK import PortHelper  # type: ignore[import]  # noqa: PLC0415

        _PortHelperClass = PortHelper
        _load_error = None
        logger.info("[rfid] EloxReaderSDK.dll loaded OK from %s", dll_path)
        return _PortHelperClass

    except ImportError as exc:
        _load_error = (
            f"pythonnet (clr) not available: {exc}. "
            "Install with: pip install pythonnet"
        )
        raise RfidEncoderError(_load_error) from exc

    except Exception as exc:
        _load_error = f"Failed to load EloxReaderSDK.dll: {exc}"
        logger.exception("[rfid] DLL load failed")
        raise RfidEncoderError(_load_error) from exc


def _get_instance() -> Any:
    """
    Return the cached PortHelper() instance, creating it if needed.
    PortHelper methods are INSTANCE methods — do NOT call them on the class.
    The instance holds the open SerialPort (ph.mkPort); reuse it across operations.
    If the instance has been created but the device disconnects, HandShake() is
    called again to re-open the port before each operation.
    """
    global _ph_instance

    PortHelper = _load_sdk()

    if _ph_instance is None:
        _ph_instance = PortHelper()
        com_port = os.environ.get("FDN_RFID_COM_PORT", "COM1")
        try:
            _ph_instance.mkPort.PortName = com_port
            logger.info("[rfid] PortHelper instance created — COM port set to %s", com_port)
        except Exception as exc:
            logger.warning("[rfid] Could not set mkPort properties on %s: %s", com_port, exc)

    return _ph_instance


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_hotel_id(hotel_id: str) -> str:
    """Left-pad to 4 digits: '42' → '0042'."""
    return hotel_id.strip().zfill(4)[:4]


def _fmt_room_number(room: str) -> str:
    """
    SDK 8-char room format: 6-digit left-padded + '00' suffix.
    '600' → '00060000',  '101' → '00010100',  '1' → '00000100'
    """
    digits = re.sub(r"\D", "", room.strip())
    if not digits:
        return "00000000"
    return digits.zfill(6)[:6] + "00"


def _fmt_datetime(dt_str: str) -> str:
    """
    Parse ISO or SDK datetime → yyyyMMddHHmm.
    Accepts: '2025-05-07T14:00:00', '2025-05-07 14:00', '202505071400', '2025-05-07'.
    """
    s = dt_str.strip()
    if re.match(r"^\d{12}$", s):
        return s
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(s, fmt).strftime("%Y%m%d%H%M")
        except ValueError:
            continue
    raise RfidEncoderError(
        f"Cannot parse datetime {s!r}. "
        "Use ISO (2025-05-07T14:00:00) or SDK format (202505071400)."
    )


def _build_card_json(
    hotel_id: str,
    auth_code: str,
    room_number: str,
    checkin_time: str,
    checkout_time: str,
    card_serial: int,
) -> str:
    """Build the JSON string passed to ph.GetStr16()."""
    return json.dumps(
        {
            "card_type": "guest card",
            "hotel_id": _fmt_hotel_id(hotel_id),
            "card_id": "00",
            "room_number": _fmt_room_number(room_number),
            "cardserial_number": str(max(1, min(8, int(card_serial)))),
            "checkin_time": _fmt_datetime(checkin_time),
            "checkout_time": _fmt_datetime(checkout_time),
            "authorization_code": auth_code.strip().zfill(8)[:8],
        },
        separators=(",", ":"),
    )


def _unpack_make_card(raw: Any) -> tuple[bool, str]:
    """
    Unpack MakeCard() result.
    DLL signature : Boolean MakeCard(String str16, String ByRef returnmsg)
    pythonnet 3.x : ok, return_msg = ph.MakeCard(str16, "")  → returns (bool, str) tuple
    """
    if isinstance(raw, (tuple, list)) and len(raw) >= 2:
        return bool(raw[0]), str(raw[1]) if raw[1] is not None else ""
    # Older pythonnet may return just the bool; log so we can diagnose.
    logger.debug("[rfid] MakeCard returned non-tuple %r — ref param not captured", raw)
    return bool(raw), ""


# ── Response builders ─────────────────────────────────────────────────────────

def _ok(return_msg: str, **extra: Any) -> dict[str, Any]:
    return {"success": True, "return_msg": return_msg, "error": None, **extra}


def _err(error: str, return_msg: str = "") -> dict[str, Any]:
    return {"success": False, "return_msg": return_msg, "error": error}


# ── Hardware operations ───────────────────────────────────────────────────────

def rfid_handshake(hotel_id: str, auth_code: str) -> dict[str, Any]:
    """
    Verify the encoder is connected and powered.

    Per SDK docs: HandShake() True = device responded = connected.
                  HandShake() False = no response = not connected or not powered.
    mkPort.IsOpen is unreliable through pythonnet — HandShake() return value is authoritative.
    Called once on startup and on manual Check button press — not on a periodic timer.
    """
    global _ph_instance
    com_port = os.environ.get("FDN_RFID_COM_PORT", "COM1")
    logger.info("[rfid] handshake — port=%s", com_port)
    try:
        ph = _get_instance()

        logger.info("[rfid] calling HandShake()")
        handshake_raw = bool(ph.HandShake())
        logger.info("[rfid] HandShake() = %s", handshake_raw)

        if not handshake_raw:
            # Reset so next check creates a fresh PortHelper — needed after device replug.
            _ph_instance = None
            logger.warning("[rfid] HandShake() False — encoder not responding on %s", com_port)
            return {
                "success": True,
                "connected": False,
                "return_msg": "",
                "error": (
                    f"RFID encoder did not respond on '{com_port}'. "
                    "Check USB cable and power adapter."
                ),
            }

        try:
            ph.mkPort.ReadTimeout = 5000
            ph.mkPort.WriteTimeout = 5000
        except Exception:
            pass

        return {"success": True, "connected": True, "return_msg": "A0", "error": None}

    except RfidEncoderError as exc:
        logger.error("[rfid] handshake RfidEncoderError: %s", exc)
        _ph_instance = None
        return {"success": False, "connected": False, "return_msg": "", "error": str(exc)}
    except Exception as exc:
        exc_str = str(exc)
        _ph_instance = None
        if "denied" in exc_str.lower() or "access" in exc_str.lower():
            error = (
                f"Serial port '{com_port}' is in use by another program. "
                "Close INNGuru GMS and try again."
            )
        elif "does not exist" in exc_str or "not found" in exc_str.lower():
            error = (
                f"Serial port '{com_port}' not found. "
                "Open Device Manager to find the correct port, "
                "then update FDN_RFID_COM_PORT in run-native-host.cmd."
            )
        else:
            error = f"HandShake exception: {exc_str}"
        logger.error("[rfid] handshake exception on %s: %s", com_port, exc_str)
        return {"success": False, "connected": False, "return_msg": "", "error": error}


def rfid_make_key(
    hotel_id: str,
    auth_code: str,
    room_number: str,
    checkin_time: str,
    checkout_time: str,
    card_serial: int = 1,
) -> dict[str, Any]:
    """
    Encode a guest room key onto the card placed in the encoder.
    Flow: HandShake() → GetStr16(json) → MakeCard(str16, ref returnmsg) → ReadCardCK() verify
    ReadCardCK is called after a successful MakeCard to confirm the physical RFID write
    actually happened (green LED flash). If ReadCardCK returns empty, MakeCard was a false
    positive (USB adapter answered but encoder head was off).
    """
    logger.info(
        "[rfid] make_key — hotel=%s room=%s serial=%s checkin=%s checkout=%s",
        hotel_id, room_number, card_serial, checkin_time, checkout_time,
    )
    try:
        ph = _get_instance()

        # HandShake() is the reliable pre-check: returns True when encoder head responds.
        # IsHandShake() always returns False for this device model — do not use it.
        logger.info("[rfid] make_key pre-check: calling HandShake()")
        if not bool(ph.HandShake()):
            logger.warning("[rfid] HandShake() returned False before MakeCard — encoder not responding")
            return _err(
                "RFID encoder not responding — check that the device is powered on "
                "and the USB cable is connected."
            )
        try:
            ph.mkPort.ReadTimeout = 5000
            ph.mkPort.WriteTimeout = 5000
        except Exception:
            pass
        logger.info("[rfid] HandShake() OK — proceeding to encode")

        ci_sdk = _fmt_datetime(checkin_time)
        co_sdk = _fmt_datetime(checkout_time)
        card_json = _build_card_json(
            hotel_id, auth_code, room_number, ci_sdk, co_sdk, card_serial
        )
        logger.debug("[rfid] card JSON: %s", card_json)

        str16 = ph.GetStr16(card_json)
        logger.info("[rfid] GetStr16 → %r", str16[:40] + "…" if str16 and len(str16) > 40 else str16)
        if not str16:
            return _err("GetStr16 returned empty — card JSON may be malformed.")

        # DLL: Boolean MakeCard(String str16, String ByRef returnmsg)
        # pythonnet 3.x: pass "" as the ref param → returns (bool, str)
        raw = ph.MakeCard(str16, "")
        ok, return_msg = _unpack_make_card(raw)
        logger.info("[rfid] MakeCard ok=%s return_msg=%r", ok, return_msg)

        if return_msg == "A0AA":
            return _err(
                "No card detected — place a blank key card flat on the encoder and try again.",
                return_msg,
            )
        if return_msg == "":
            return _err(
                "Encoder did not respond to MakeCard (return_msg empty). "
                "Device may be powered off or card was removed too quickly.",
                return_msg,
            )
        if not ok or return_msg != "A0":
            return _err(
                f"Card encoding failed (return_msg={return_msg!r}). "
                "Ensure the card is flat on the encoder.",
                return_msg,
            )

        # ── Post-write verification ────────────────────────────────────────────
        # Per official SDK docs: ReadCardCK empty = power supply issue = write did NOT happen.
        # Exception also treated as failure — never silently return success on verify error.
        logger.info("[rfid] MakeCard A0 — verifying write with ReadCardCK()")
        verify_data = ""
        try:
            verify_data = ph.ReadCardCK()
            verify_data = str(verify_data) if verify_data is not None else ""
        except Exception as verify_exc:
            logger.error("[rfid] ReadCardCK verification threw exception: %s", verify_exc)
            verify_data = ""

        logger.info("[rfid] ReadCardCK (verify) -> %r", verify_data[:80] if verify_data else "")
        if not verify_data:
            logger.error(
                "[rfid] MakeCard reported A0 but ReadCardCK returned empty — "
                "encoder head not powered or card removed. Write did NOT happen."
            )
            return _err(
                "Key card write failed — encoder head did not respond during verification. "
                "Ensure the device is powered on, the card is flat on the reader, and try again."
            )

        return _ok(
            return_msg,
            room_number=room_number,
            card_serial=card_serial,
            encoded_data=verify_data,
            checkin_time=ci_sdk,
            checkout_time=co_sdk,
        )

    except RfidEncoderError as exc:
        logger.error("[rfid] make_key RfidEncoderError: %s", exc)
        return _err(str(exc))
    except Exception as exc:
        logger.exception("[rfid] make_key unexpected exception")
        return _err(f"Unexpected error during key encoding: {exc}")


def _parse_card_data(raw: str) -> dict[str, Any] | None:
    """
    Decode a raw ReadCardCK() string back to card fields.
    Elox SDK format (49 chars):
      hotel_id(4) + card_id(2) + room_sdk(8) + serial(1) +
      checkin(12, yyyyMMddHHmm) + checkout(12, yyyyMMddHHmm) + byte(1) + auth(8) + byte(1)

    Returns None if the string doesn't look like an Elox-encoded card.
    """
    if len(raw) < 47:
        return None
    try:
        hotel_id = raw[0:4]
        room_sdk = raw[6:14]
        serial   = raw[14:15]
        checkin  = raw[15:27]   # yyyyMMddHHmm
        co_raw   = raw[27:39]   # yyyyMMddHHmm — same layout as checkin

        room_number = room_sdk[:6].lstrip("0") or "0"

        if not hotel_id.isdigit() or not room_number.isdigit():
            return None

        # Months 1–9: bytes [4:6] are 13–19 (extra '1' byte + single-digit month, no leading zero).
        # Months 10–12: bytes [4:6] are 10–12 (standard two-digit month).
        co_mm = int(co_raw[4:6])
        if co_mm > 12:
            co_month = int(co_raw[5:6])
            checkout = f"{co_raw[0:4]}{co_month:02d}{co_raw[6:]}"
        else:
            checkout = co_raw

        return {
            "hotel_id":     hotel_id,
            "room_number":  room_number,
            "card_serial":  int(serial) if serial.isdigit() else 1,
            "checkin_time": checkin,
            "checkout_time": checkout,
        }
    except Exception:
        return None


def rfid_read_card() -> dict[str, Any]:
    """
    Read data from a card on the encoder.
    DLL: String ReadCardCK()  — no parameters, returns raw card string directly.
    """
    logger.info("[rfid] read_card — calling HandShake() pre-check")
    try:
        ph = _get_instance()

        if not bool(ph.HandShake()):
            logger.warning("[rfid] HandShake() returned False before ReadCardCK")
            return _err(
                "RFID encoder not responding — check device power and USB connection."
            )
        try:
            ph.mkPort.ReadTimeout = 5000
            ph.mkPort.WriteTimeout = 5000
        except Exception:
            pass

        # DLL: String ReadCardCK()  — returns string directly, no ref/out params
        logger.info("[rfid] calling ReadCardCK()")
        return_msg = ph.ReadCardCK()
        return_msg = str(return_msg) if return_msg is not None else ""
        logger.info("[rfid] ReadCardCK → %r", return_msg)

        if not return_msg:
            return _err(
                "No card data returned — ensure a card is placed flat on the reader."
            )

        parsed = _parse_card_data(return_msg)
        logger.info("[rfid] ReadCardCK parsed: %s", parsed)
        return _ok(
            return_msg,
            card_data=return_msg,
            room_number=parsed["room_number"] if parsed else None,
            card_serial=parsed["card_serial"] if parsed else None,
            checkin_time=parsed["checkin_time"] if parsed else None,
            checkout_time=parsed["checkout_time"] if parsed else None,
        )

    except RfidEncoderError as exc:
        logger.error("[rfid] read_card RfidEncoderError: %s", exc)
        return _err(str(exc))
    except Exception as exc:
        logger.exception("[rfid] read_card unexpected exception")
        return _err(f"Unexpected error during card read: {exc}")


def rfid_make_lost_key(
    hotel_id: str,
    auth_code: str,
    room_number: str,
    checkout_time: str,
) -> dict[str, Any]:
    """
    Encode a replacement guest key for a lost card.
    Uses serial 1 and a fresh check-in time (DateTime.Now) — the lock automatically
    invalidates the previous key when this card is first tapped at the door.
    No disable card needed: tapping the new card at the lock deactivates all prior keys.
    """
    new_checkin_time = datetime.now().strftime("%Y%m%d%H%M")
    logger.info(
        "[rfid] make_lost_key — hotel=%s room=%s checkout=%s new_checkin=%s",
        hotel_id, room_number, checkout_time, new_checkin_time,
    )
    try:
        ph = _get_instance()

        logger.info("[rfid] make_lost_key pre-check: calling HandShake()")
        if not bool(ph.HandShake()):
            logger.warning("[rfid] HandShake() returned False — encoder not responding")
            return _err("Reader not connected — check that the device is powered on and the USB cable is connected.")
        try:
            ph.mkPort.ReadTimeout = 5000
            ph.mkPort.WriteTimeout = 5000
        except Exception:
            pass
        logger.info("[rfid] HandShake() OK — proceeding to encode lost key replacement")

        co_sdk = _fmt_datetime(checkout_time)
        card_json = _build_card_json(
            hotel_id, auth_code, room_number, new_checkin_time, co_sdk, card_serial=1
        )
        logger.debug("[rfid] lost key card JSON: %s", card_json)

        str16 = ph.GetStr16(card_json)
        logger.info("[rfid] GetStr16 → %r", str16[:40] + "…" if str16 and len(str16) > 40 else str16)
        if not str16:
            return _err("GetStr16 returned empty — card JSON may be malformed.")

        raw = ph.MakeCard(str16, "")
        ok, return_msg = _unpack_make_card(raw)
        logger.info("[rfid] MakeCard ok=%s return_msg=%r", ok, return_msg)

        if return_msg == "A0AA":
            return _err("Place card on reader and try again.", return_msg)
        if return_msg == "":
            return _err("Check power connection — encoder did not respond to MakeCard.", return_msg)
        if not ok or return_msg != "A0":
            return _err(f"Card encoding failed (return_msg={return_msg!r}).", return_msg)

        logger.info("[rfid] MakeCard A0 — verifying write with ReadCardCK()")
        verify_data = ""
        try:
            verify_data = ph.ReadCardCK()
            verify_data = str(verify_data) if verify_data is not None else ""
        except Exception as verify_exc:
            logger.error("[rfid] ReadCardCK verification threw exception: %s", verify_exc)
            verify_data = ""

        logger.info("[rfid] ReadCardCK (verify) -> %r", verify_data[:80] if verify_data else "")
        if not verify_data:
            logger.error(
                "[rfid] MakeCard reported A0 but ReadCardCK returned empty — "
                "encoder head not powered or card removed. Write did NOT happen."
            )
            return _err(
                "Key card write failed — encoder head did not respond during verification. "
                "Ensure the device is powered on, the card is flat on the reader, and try again."
            )

        return _ok(
            return_msg,
            room_number=room_number,
            card_serial=1,
            encoded_data=verify_data,
            new_checkin_time=new_checkin_time,
            checkout_time=co_sdk,
        )

    except RfidEncoderError as exc:
        logger.error("[rfid] make_lost_key RfidEncoderError: %s", exc)
        return _err(str(exc))
    except Exception as exc:
        logger.exception("[rfid] make_lost_key unexpected exception")
        return _err(f"Unexpected error during lost key encoding: {exc}")


# ── Dispatch ──────────────────────────────────────────────────────────────────

def handle_rfid_command(
    payload: dict[str, Any],
    hotel_id: str,
    auth_code: str,
) -> dict[str, Any]:
    """Route an RFID command from main.py dispatch table to the correct function."""
    cmd = payload.get("type", "")

    if cmd == "RFID_HANDSHAKE":
        return {"type": "RFID_HANDSHAKE_RESULT", **rfid_handshake(hotel_id, auth_code)}

    if cmd == "RFID_MAKE_KEY":
        result = rfid_make_key(
            hotel_id, auth_code,
            room_number=str(payload.get("room_number", "")),
            checkin_time=str(payload.get("checkin_time", "")),
            checkout_time=str(payload.get("checkout_time", "")),
            card_serial=int(payload.get("card_serial", 1)),
        )
        return {"type": "RFID_KEY_RESULT", **result}

    if cmd == "RFID_READ_CARD":
        return {"type": "RFID_READ_RESULT", **rfid_read_card()}

    if cmd == "RFID_MAKE_LOST_KEY":
        result = rfid_make_lost_key(
            hotel_id, auth_code,
            room_number=str(payload.get("room_number", "")),
            checkout_time=str(payload.get("checkout_time", "")),
        )
        return {"type": "RFID_LOST_KEY_RESULT", **result}

    return {"type": "ERROR", "message": f"Unknown RFID command: {cmd!r}"}


def handle_rfid_command_simulated(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Mock responses — identical shape to real hardware, no DLL or device needed.
    Used when FDN_SIMULATION_MODE=1 (default during development).
    """
    cmd = payload.get("type", "")
    logger.info("[rfid-sim] simulating: %s", cmd)

    if cmd == "RFID_HANDSHAKE":
        return {
            "type": "RFID_HANDSHAKE_RESULT",
            "success": True,
            "connected": True,
            "return_msg": "A0",
            "error": None,
        }

    if cmd == "RFID_MAKE_KEY":
        return {
            "type": "RFID_KEY_RESULT",
            "success": True,
            "return_msg": "A0",
            "error": None,
            "room_number": str(payload.get("room_number", "101")),
            "card_serial": int(payload.get("card_serial", 1)),
        }

    if cmd == "RFID_READ_CARD":
        return {
            "type": "RFID_READ_RESULT",
            "success": True,
            "return_msg": "A0",
            "error": None,
            "card_data": "SIMULATED_CARD_DATA",
        }

    if cmd == "RFID_MAKE_LOST_KEY":
        new_checkin_time = datetime.now().strftime("%Y%m%d%H%M")
        return {
            "type": "RFID_LOST_KEY_RESULT",
            "success": True,
            "return_msg": "A0",
            "error": None,
            "room_number": str(payload.get("room_number", "101")),
            "card_serial": 1,
            "new_checkin_time": new_checkin_time,
        }

    return {"type": "ERROR", "message": f"Unknown RFID command: {cmd!r}"}
