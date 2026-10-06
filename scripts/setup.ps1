# One-time setup on Windows (PowerShell). Run from the project root:  .\scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3.12 is required (python.org installer, tick 'Add to PATH')." }
python --version

if (-not (Test-Path .venv)) { python -m venv .venv }
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -r requirements.txt

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    $secret = -join ((1..48) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
    (Get-Content .env) -replace '^STORAGE_SECRET=.*', "STORAGE_SECRET=$secret" | Set-Content .env -Encoding utf8
    $toPlain = { param($sec) [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)) }
    while ($true) {
        $plain = & $toPlain (Read-Host "Choose the first admin password (min 10 chars)" -AsSecureString)
        $again = & $toPlain (Read-Host "Type it again to confirm" -AsSecureString)
        if ($plain.Length -lt 10) { Write-Host "Too short - use at least 10 characters." -ForegroundColor Red; continue }
        if ($plain -ne $again) { Write-Host "The two entries do not match - try again." -ForegroundColor Red; continue }
        break
    }
    (Get-Content .env) -replace '^ADMIN_PASSWORD=.*', "ADMIN_PASSWORD=$plain" | Set-Content .env -Encoding utf8
    Write-Host ".env created. Remove ADMIN_PASSWORD from .env after the first start." -ForegroundColor Yellow
}

# Restrict .env and data to the current user (no other local accounts can read secrets/documents)
New-Item -ItemType Directory -Force data | Out-Null
icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
icacls data /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null

.\.venv\Scripts\python scripts\make_sample_docs.py
Write-Host "Setup complete. Start with: .\scripts\run.ps1" -ForegroundColor Green
