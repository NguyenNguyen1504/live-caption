$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
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
Write-Host "Double-click that file to start immediately in Demo Mode."
