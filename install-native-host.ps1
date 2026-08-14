#Requires -Version 5.1
<#
.SYNOPSIS
  Registers FrontDesk Nexus native messaging host with Chrome (current user).

.DESCRIPTION
  Writes com.frontdesk.nexus.json with an absolute path to run-native-host.cmd
  and sets HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.frontdesk.nexus

  Run this ONCE on the hotel PC from the host folder, then fully quit and reopen Chrome.

.PARAMETER ExtensionId
  Chrome extension ID (from chrome://extensions with Developer mode).
  Default matches the shipped production/unpacked ID used in this project.

.EXAMPLE
  cd C:\Project\FontNexus-Native-Messaging-Host
  powershell -ExecutionPolicy Bypass -File .\install-native-host.ps1

.EXAMPLE
  .\install-native-host.ps1 -ExtensionId oellgjjhompcikojenbbfepojdcpnpla
#>
param(
  [string]$ExtensionId = "oellgjjhompcikojenbbfepojdcpnpla"
)

$ErrorActionPreference = "Stop"
$HostDir = $PSScriptRoot
$CmdPath = Join-Path $HostDir "run-native-host.cmd"
$JsonPath = Join-Path $HostDir "com.frontdesk.nexus.json"
$RegKey = "HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.frontdesk.nexus"

Write-Host "=== FrontDesk Nexus native host installer ===" -ForegroundColor Cyan
Write-Host "Host folder : $HostDir"

if (-not (Test-Path -LiteralPath $CmdPath)) {
  throw "Missing run-native-host.cmd at $CmdPath"
}

# Find Python (same search order as run-native-host.cmd)
$pyCandidates = @(
  $env:FDN_PYTHON_EXE,
  "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
  "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
  "C:\Users\amari\AppData\Local\Programs\Python\Python312\python.exe",
  "C:\Python312\python.exe",
  "C:\Program Files\Python312\python.exe"
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

$PythonExe = $pyCandidates | Select-Object -First 1
if (-not $PythonExe) {
  $pyCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($pyCmd) {
    Write-Host "Python via: py -3" -ForegroundColor Yellow
  } else {
    throw "64-bit Python 3 not found. Install Python 3.12+ x64, then re-run."
  }
} else {
  Write-Host "Python     : $PythonExe"
  $bitness = & $PythonExe -c "import struct; print(struct.calcsize('P')*8)"
  Write-Host "Python bits: $bitness"
  if ($bitness -ne "64") {
    Write-Warning "Python is not 64-bit. NS690gt.DLL in System32 needs 64-bit Python."
  }
}

if ($ExtensionId -notmatch '^[a-p]{32}$') {
  throw "ExtensionId must be 32 chars a-p (from chrome://extensions). Got: $ExtensionId"
}

$origin = "chrome-extension://$ExtensionId/"
# JSON "path" must use escaped backslashes for Chrome
$cmdAbs = (Resolve-Path -LiteralPath $CmdPath).Path
$manifest = [ordered]@{
  name            = "com.frontdesk.nexus"
  description     = "FrontDesk Nexus Native Messaging Host"
  path            = $cmdAbs
  type            = "stdio"
  allowed_origins = @($origin)
}

$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $JsonPath -Encoding UTF8
Write-Host "Wrote manifest: $JsonPath"
Write-Host "  path            = $cmdAbs"
Write-Host "  allowed_origins = $origin"

New-Item -Path $RegKey -Force | Out-Null
New-ItemProperty -Path $RegKey -Name "(default)" -Value $JsonPath -PropertyType String -Force | Out-Null
Write-Host "Registry OK: $RegKey -> $JsonPath" -ForegroundColor Green

$nsDll = "$env:SystemRoot\System32\NS690gt.DLL"
if (Test-Path -LiteralPath $nsDll) {
  Write-Host "NS690gt.DLL : FOUND ($nsDll)" -ForegroundColor Green
} else {
  Write-Host "NS690gt.DLL : not in System32 (install Ambir nScan 690gt driver if using Ambir)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "NEXT STEPS:" -ForegroundColor Cyan
Write-Host "  1. chrome://extensions — confirm this extension ID is exactly: $ExtensionId"
Write-Host "     If different, re-run: .\install-native-host.ps1 -ExtensionId <your-id>"
Write-Host "  2. Fully quit Chrome (all windows) and reopen."
Write-Host "  3. Open the FrontDesk Nexus side panel — native host should connect."
Write-Host "  4. Do NOT double-click run-native-host.cmd to connect."
Write-Host "  5. Tail log: Get-Content .\logs\native-host.log -Wait"
Write-Host ""
Write-Host "Done." -ForegroundColor Green
