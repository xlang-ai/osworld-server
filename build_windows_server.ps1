param(
    [string]$PythonExe = "python",
    [string]$SyncDir = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

Write-Host "[1/4] Installing dependencies..."
& $PythonExe -m pip install -r requirements.txt
& $PythonExe -m pip install pyinstaller

Write-Host "[2/4] Cleaning old build artifacts..."
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

Write-Host "[3/4] Building one-file server executable..."
& $PythonExe -m PyInstaller --noconfirm --clean --onefile --name main --add-data ".\h264_browser_client.html;." main.py
Copy-Item "dist\main.exe" ".\main.exe" -Force

if ($SyncDir -ne "") {
    $resolvedSyncDir = [System.IO.Path]::GetFullPath($SyncDir)
    $resolvedScriptDir = [System.IO.Path]::GetFullPath($scriptDir)
    if ($resolvedSyncDir -ieq $resolvedScriptDir) {
        Write-Host "[4/4] SyncDir is current directory; skipping sync copy."
    } else {
        Write-Host "[4/4] Syncing artifacts to $SyncDir ..."
        New-Item -ItemType Directory -Path $SyncDir -Force | Out-Null
        Copy-Item ".\main.exe" (Join-Path $SyncDir "main.exe") -Force
        Copy-Item ".\main.py" (Join-Path $SyncDir "main.py") -Force
        Copy-Item ".\src" (Join-Path $SyncDir "src") -Recurse -Force
        Copy-Item ".\requirements.txt" (Join-Path $SyncDir "requirements.txt") -Force
        Copy-Item ".\h264_browser_client.html" (Join-Path $SyncDir "h264_browser_client.html") -Force
    }
}

Write-Host "Done. Built server binary: $scriptDir\main.exe"
