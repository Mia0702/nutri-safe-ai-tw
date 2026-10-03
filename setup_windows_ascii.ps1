$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "=== nutri_safe_ai setup ===" -ForegroundColor Cyan
Write-Host ""

# 1. Check Python
try {
    $pythonVersion = python --version
    Write-Host "[OK] $pythonVersion" -ForegroundColor Green
}
catch {
    Write-Host "[ERROR] Python was not found. Install Python 3.11+ and add it to PATH." -ForegroundColor Red
    exit 1
}

# 2. Create virtual environment
if (-not (Test-Path ".venv")) {
    Write-Host "[INFO] Creating .venv..." -ForegroundColor Yellow
    python -m venv .venv
}
else {
    Write-Host "[OK] .venv already exists." -ForegroundColor Green
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[ERROR] .venv Python was not created correctly." -ForegroundColor Red
    exit 1
}

# 3. Upgrade pip
Write-Host "[INFO] Upgrading pip..." -ForegroundColor Yellow
& $venvPython -m pip install --upgrade pip

# 4. Install dependencies
if (Test-Path "requirements.txt") {
    Write-Host "[INFO] Installing requirements.txt..." -ForegroundColor Yellow
    & $venvPython -m pip install -r requirements.txt
}
else {
    Write-Host "[ERROR] requirements.txt was not found." -ForegroundColor Red
    exit 1
}

# 5. Create .env only if missing
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "[OK] Created .env from .env.example." -ForegroundColor Green
    }
    else {
        Write-Host "[WARN] .env.example was not found. .env was not created." -ForegroundColor Yellow
    }
}
else {
    Write-Host "[OK] .env already exists. It was not overwritten." -ForegroundColor Green
}

# 6. Initialize Git if available
try {
    $null = git --version
    if (-not (Test-Path ".git")) {
        git init | Out-Null
        Write-Host "[OK] Git repository initialized." -ForegroundColor Green
    }
    else {
        Write-Host "[OK] Git repository already exists." -ForegroundColor Green
    }
}
catch {
    Write-Host "[WARN] Git was not found. You can install Git for Windows later." -ForegroundColor Yellow
}

# 7. Run starter test
Write-Host "[INFO] Running pytest..." -ForegroundColor Yellow
& $venvPython -m pytest

if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] Setup finished, but pytest reported a failure." -ForegroundColor Yellow
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "=== Setup complete ===" -ForegroundColor Cyan
Write-Host "Next: open Claude Code and send PROMPTS/00_FIRST_MESSAGE_TO_CLAUDE.md"
Write-Host ""
