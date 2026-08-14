@echo off
REM Chrome starts this launcher; it forwards Chrome's extra args (%*) to Python.
REM CRITICAL: Never write to stdout before/while Python runs — Chrome uses stdout
REM for native-messaging framing. All human messages must go to stderr (1>&2).
REM Logs: logs\native-host.log
setlocal EnableExtensions
cd /d "%~dp0"

if not exist "%~dp0logs" mkdir "%~dp0logs"
set "FDN_LOG_FILE=%~dp0logs\native-host.log"

REM Automatic document flow: card on Thales reader -> host pushes AUTO_SCAN_RESULT (no button).
REM On Ambir-only PCs you can set: FDN_THALES_AUTO_WATCH=0
set "FDN_THALES_AUTO_WATCH=1"

set "FDN_THALES_WORKING_DIR=%~dp0"
set "FDN_THALES_SYNC_APPLICATION_INI=0"
set "FDN_THALES_FETCH_VISIBLE_IMAGE=1"
set "FDN_THALES_VISION_OCR_FALLBACK=0"

REM ── RFID Key Card Encoder ────────────────────────────────────────────────────
set "FDN_RFID_HOTEL_ID=2108"
set "FDN_RFID_AUTH_CODE=80662903"
set "FDN_RFID_COM_PORT=COM6"
REM ─────────────────────────────────────────────────────────────────────────────

REM Resolve 64-bit python.exe (hotel PCs often are not user "amari")
set "PYEXE="
if defined FDN_PYTHON_EXE if exist "%FDN_PYTHON_EXE%" set "PYEXE=%FDN_PYTHON_EXE%"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PYEXE if exist "C:\Users\amari\AppData\Local\Programs\Python\Python312\python.exe" set "PYEXE=C:\Users\amari\AppData\Local\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "C:\Python312\python.exe" set "PYEXE=C:\Python312\python.exe"
if not defined PYEXE if exist "C:\Program Files\Python312\python.exe" set "PYEXE=C:\Program Files\Python312\python.exe"

if defined PYEXE (
  echo [FDN] Using Python: %PYEXE% 1>&2
  "%PYEXE%" "%~dp0main.py" --native-messaging %*
  exit /b %ERRORLEVEL%
)

where py >nul 2>&1
if not errorlevel 1 (
  echo [FDN] Using py -3 launcher 1>&2
  py -3 "%~dp0main.py" --native-messaging %*
  exit /b %ERRORLEVEL%
)

where python >nul 2>&1
if not errorlevel 1 (
  echo [FDN] Using python on PATH 1>&2
  python "%~dp0main.py" --native-messaging %*
  exit /b %ERRORLEVEL%
)

echo ERROR: Python 3 not found. Install 64-bit Python 3.12+ or set FDN_PYTHON_EXE. 1>&2
exit /b 1
