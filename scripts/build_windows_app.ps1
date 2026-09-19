$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    $pythonCmd = (Get-Command py.exe, python.exe -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($null -eq $pythonCmd) {
        throw "Python was not found on PATH. Install Python 3.12 or newer."
    }
    if ($pythonCmd.Name -eq "py.exe") {
        & py -3 -m venv .venv
    } else {
        & python -m venv .venv
    }
}

& .venv\Scripts\python.exe -m pip install -e ".[package]"
& .venv\Scripts\pyinstaller.exe `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name LocalDictationCaptions `
    --paths src `
    --collect-submodules keyring.backends `
    packaging\windows_app.py

Write-Host ""
Write-Host "Built: $RepoRoot\dist\LocalDictationCaptions.exe"
Write-Host "Double-click that file to start immediately in Real API Mode."

