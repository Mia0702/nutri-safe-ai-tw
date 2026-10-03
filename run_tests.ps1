$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = ".\.venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Host "找不到 .venv。請先執行 setup_windows.ps1。" -ForegroundColor Red
    exit 1
}

& $python -m pytest
