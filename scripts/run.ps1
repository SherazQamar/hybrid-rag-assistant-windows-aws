# Starts the app on http://127.0.0.1:8080 (localhost only; remote users arrive via the AWS tunnel).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
$env:NICEGUI_STORAGE_PATH = (Join-Path (Get-Location) "data\.nicegui")
.\.venv\Scripts\python -m app.main
