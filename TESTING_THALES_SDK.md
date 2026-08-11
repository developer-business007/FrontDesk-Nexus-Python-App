# Thales QS2000 / MMMReader SDK — test guide

This project adds **optional** Thales Document Reader integration via `scanner_thales_sdk.py`. **Google Cloud Vision** and **`SCAN_ID`** (TWAIN + Vision) are unchanged.

## What was added

| Piece | Purpose |
|--------|---------|
| `scanner_thales_sdk.py` | `ctypes` load of `MMMReaderHighLevelAPI.dll`, blocking flow, MRZ/AAMVA parsing helpers |
| `scanner.scan_document_thales_sdk()` | Host-friendly wrapper → `SDK_DOCUMENT_RESULT` |
| Native message `SCAN_DOCUMENT_SDK` | Extension command for SDK-only reads (no Vision) |
| `config/Application.ini.example` | Template `[DataToSend]` matching your spec |

## Prerequisites

1. **Windows x64** and **64-bit Python** matching **64-bit** Thales SDK binaries.
2. **Microsoft Visual C++ Redistributable** (version required by Thales; install x64).
3. Thales **QS2000** (or supported reader) with **Document Reader** / MMMReader runtime installed.
4. SDK **Bin** folder containing at least:
   - `MMMReaderHighLevelAPI.dll`
   - Dependent DLLs (`DeviceDll`, `SettingsDll`, etc. — names per your install)

## Configuration (paths on each PC)

**Recommended:** use a single project file so you can copy the repo to another machine and only edit one place.

1. Copy `config/thales_paths.example.ini` → `config/thales_paths.ini`.
2. Edit **`sdk_bin`** (folder with `MMMReaderHighLevelAPI.dll`) and **`application_ini`** (path to `Application.ini`).
3. Copy `config/Application.ini.example` → `config/Application.ini` and adjust `[DataToSend]` if needed.

`thales_paths.ini` is listed in `.gitignore` so local paths are not committed.

**Override order:** if a variable is set in the environment, it wins over `thales_paths.ini`:

| Variable | Example | Meaning |
|----------|---------|---------|
| `FDN_THALES_SDK_BIN` | `C:\Program Files\Thales\...\Bin` | Folder containing `MMMReaderHighLevelAPI.dll` |
| `FDN_THALES_APPLICATION_INI` | `F:\...\config\Application.ini` | Full path to `Application.ini` |
| `FDN_THALES_WAIT_TIMEOUT_MS` | `120000` | `WaitForDocument` timeout (ms) |
| `FDN_THALES_DLL_NAME` | `MMMReaderHighLevelAPI.dll` | Override if your DLL name differs |

3. **Verify `GetData` type IDs** in your SDK headers (`CD_CODELINE`, `CD_CODELINE_DATA`, `CD_AAMVA_DATA`). If values differ, set:

- `FDN_THALES_CD_CODELINE`
- `FDN_THALES_CD_CODELINE_DATA`
- `FDN_THALES_CD_AAMVA_DATA`

4. **Verify exported function names** in `scanner_thales_sdk.py` (`_bind_api`). If your SDK renames exports (e.g. `Initialize` vs `Initialise`), adjust the bindings to match **Dependency Walker** or the vendor `.h` / `.lib` list.

## Thales 3.9 High Level API (why `0xC0000005` happened before)

Per **`SDK/Include/MMMReaderHighLevelAPI.h`** (e.g. 3.9.2.49), **`MMMReader_Initialise` does not take `Application.ini`**. It takes **seven** arguments: four callbacks (NULL for blocking data/event), optional error callback, two **`bool`** flags (`aProcessMessages`, `aProcessInputMessages`), and **`void* aParam`**. Passing a path string as the first argument **corrupts the stack** and faults.

**`Application.ini`** is read from **`Config/Application.ini`** under the SDK install root (relative to process **cwd**). The host defaults **`cwd`** to the **parent of `sdk_bin`** (install root), not `Bin` alone. Use **`FDN_THALES_SYNC_APPLICATION_INI=1`** to copy your project `Application.ini` into that folder before `Initialise`.

**`MMMReader_WaitForDocumentOnWindow(int timeoutMs)`** replaces older `WaitForDocument` names. **`MMMReader_GetData`** uses **`(enum, void* buf, int* len, int index)`** — two-step NULL buffer then fill.

Default **`FDN_THALES_CD_*`** values match the 3.9 enum (`CD_CODELINE=0`, `CD_CODELINE_DATA=1`, `CD_AAMVA_DATA=74`).

## API alignment (required)

Match **`SDK/Include/MMMReaderHighLevelAPI.h`** for your installed version. The repo targets **3.9.x** layout above.

Edit `_bind_api`, `_call_initialise`, `_call_wait`, and `_get_data_buffer` in `scanner_thales_sdk.py` if your headers differ.

## Automatic workflow (no “Scan ID” click)

Goal: when a user places a document on the Thales reader, the host **pushes** parsed data + optional image to the extension.

1. **`run-native-host.cmd`** sets **`FDN_THALES_AUTO_WATCH=1`** by default. To turn off automatic reads, set **`FDN_THALES_AUTO_WATCH=0`** in that file (or override in system env). Also ensure **`config/thales_paths.ini`** and **`config/Application.ini`** exist (copy from the `*.example` files).
2. The extension must **connect** the native host when your UI loads (e.g. background script or page load): `chrome.runtime.connectNative('com.frontdesk.nexus')` — **not** only when a button is clicked.
3. Register **`port.onMessage.addListener(...)`** and handle **`type === "AUTO_SCAN_RESULT"`** — same payload shape as manual `SCAN_DOCUMENT_SDK` plus `source: "thales_auto_watch"`.
4. Optional image: set **`VisibleImage=1`** in `Application.ini`, **`FDN_THALES_FETCH_VISIBLE_IMAGE=1`**, and **`FDN_THALES_CD_VISIBLE_IMAGE`** to the correct `GetData` type from your SDK header.

The host process is only started after the extension opens the native port; until then Python is not running.

## Quick Python test (no Chrome)

From the project root, with env vars set:

```powershell
$env:FDN_THALES_SDK_BIN="C:\Path\To\Thales\Bin"
$env:FDN_THALES_APPLICATION_INI="F:\...\config\Application.ini"
python -c "from scanner_thales_sdk import read_document_safe; import json; print(json.dumps(read_document_safe(), indent=2, ensure_ascii=False))"
```

Place a document when the reader waits; confirm `SDK_DOCUMENT_OK` and non-empty `structured` / `codeline_raw` / `aamva_raw`.

## Native messaging test

Send JSON:

```json
{ "type": "SCAN_DOCUMENT_SDK" }
```

Expect `type`: `SDK_DOCUMENT_RESULT` on success, or `ERROR` with `message`.

`SCAN_ID` still uses **Vision + TWAIN/sample** as before.

## Functional tests (manual)

| Test | Steps | Expected |
|------|--------|----------|
| Passport (MRZ) | ICAO passport, good lighting | `mrz_raw` / MRZ-derived fields populated |
| Driver license (AAMVA) | Barcode side to reader | `barcode_data` / `aamva_raw` non-empty |
| No document | Wait until timeout | `ERROR` or Thales error code in message |
| Unplug device | Start read with device offline | Load or init failure → `ERROR` |
| Wrong bitness | 32-bit Python with 64-bit DLL | DLL load failure message |

## Performance

Target **2–3 seconds** per cycle depends on hardware and `FDN_THALES_WAIT_TIMEOUT_MS`. The Python host runs the SDK on its **stdin loop thread**; do not run long blocking calls on the extension UI thread—only the native host blocks.

## Troubleshooting

- **DLL load failed** — Add `FDN_THALES_SDK_BIN` to PATH or rely on `os.add_dll_directory` (already used); install VC++ runtime; match x64.
- **Empty GetData** — Confirm `[DataToSend]` in `Application.ini` and type constants.
- **Initialise non-zero** — Check INI path, reader license, and USB.
