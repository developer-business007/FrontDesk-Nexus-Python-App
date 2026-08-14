@echo off
REM Chrome starts this launcher; it forwards Chrome's extra args (%*) to Python.
REM Edit the hardcoded python.exe path if yours differs.
REM Logs go to logs\native-host.log (open in VS Code or: Get-Content -Wait .\logs\native-host.log)
setlocal
cd /d "%~dp0"

if not exist "%~dp0logs" mkdir "%~dp0logs"
set "FDN_LOG_FILE=%~dp0logs\native-host.log"

REM Automatic document flow: card on Thales reader -> host pushes AUTO_SCAN_RESULT (no button).
REM Disable: set "FDN_THALES_AUTO_WATCH=0" below.
set "FDN_THALES_AUTO_WATCH=1"

REM Thales 3.9: Initialise has no ini path argument; copy host Application.ini into SDK Config
REM so [DataToSend] (AAMVA, VisibleImage, etc.) matches your project. Set to 0 to use only the install default.
REM Point working dir to project folder so SDK reads Config\Application.ini from here (no admin needed).
set "FDN_THALES_WORKING_DIR=%~dp0"
set "FDN_THALES_SYNC_APPLICATION_INI=0"

REM Include visible image bytes in AUTO_SCAN_RESULT (needs VisibleImage=1 in Application.ini above).
REM Required for two-sided SCAN_DOCUMENT_SDK (front_image_base64 + back_image_base64).
REM Disable: set "FDN_THALES_FETCH_VISIBLE_IMAGE=0"
set "FDN_THALES_FETCH_VISIBLE_IMAGE=1"

REM Using Thales SDK OCR only — Google Vision is disabled.
REM To re-enable Vision as a fallback, change to: FDN_THALES_VISION_OCR_FALLBACK=1
set "FDN_THALES_VISION_OCR_FALLBACK=0"

REM Cwd defaults to SDK install root (parent of Bin) before DLL load. Override: FDN_THALES_WORKING_DIR=bin
REM To disable chdir: set "FDN_THALES_SET_CWD_TO_SDK_BIN=0"

REM Longer wait if users place the card slowly (milliseconds).
REM set "FDN_THALES_WAIT_TIMEOUT_MS=240000"

REM How long to wait for the document to be removed after a successful scan (milliseconds).
REM Default 60000 (60 s). Set to 0 to disable removal wait (not recommended).
REM set "FDN_THALES_REMOVAL_TIMEOUT_MS=60000"

REM ── RFID Key Card Encoder ────────────────────────────────────────────────────
REM com_port : open Device Manager → Ports (COM & LPT) to find the right COM number
set "FDN_RFID_HOTEL_ID=2108"
set "FDN_RFID_AUTH_CODE=80662903"
set "FDN_RFID_COM_PORT=COM6"
REM ─────────────────────────────────────────────────────────────────────────────

set "PYEXE=C:\Users\amari\AppData\Local\Programs\Python\Python312\python.exe"
if exist "%PYEXE%" (
  "%PYEXE%" "%~dp0main.py" --native-messaging %*
) else (
  py -3 "%~dp0main.py" --native-messaging %*
)
