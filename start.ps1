# start.ps1 - Launch the News Digest app and capture all terminal output to a log file.
# Usage: .\start.ps1
# Each run creates a new timestamped file under logs\sessions\

$ErrorActionPreference = "Continue"
$env:PYTHONUNBUFFERED  = "1"
$env:PYTHONIOENCODING  = "utf-8"

# Auto-detect Python executable (supports both .venv and venv)
if (Test-Path ".venv\Scripts\python.exe") {
    $python = ".venv\Scripts\python.exe"
} elseif (Test-Path "venv\Scripts\python.exe") {
    $python = "venv\Scripts\python.exe"
} else {
    Write-Host "ERROR: Cannot find Python in .venv or venv. Activate your virtual environment first."
    exit 1
}

$sessionDir = "logs\sessions"
if (-not (Test-Path $sessionDir)) {
    New-Item -ItemType Directory -Path $sessionDir | Out-Null
}

$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$logFile   = "$sessionDir\$timestamp.log"

Write-Host ""
Write-Host "  News Digest - starting server"
Write-Host "  Python     : $python"
Write-Host "  Session log: $logFile"
Write-Host "  Press Ctrl+C to stop"
Write-Host ""

"=== Session started $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
    Out-File -FilePath $logFile -Encoding utf8

# cmd /c merges stderr into stdout cleanly - avoids PowerShell 5.1 wrapping
# native process stderr in ErrorRecord objects which garbles Tee-Object output.
# Quotes around $python handle paths with spaces.
cmd /c "`"$python`" -u -m uvicorn main:app --reload 2>&1" |
    Tee-Object -FilePath $logFile -Append

"`n=== Session ended $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===" |
    Add-Content -Path $logFile -Encoding utf8

Write-Host ""
Write-Host "  Session log saved to: $logFile"
