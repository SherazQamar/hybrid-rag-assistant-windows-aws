# Runs the automated test suites.
#   .\scripts\test.ps1                 -> backend + security tests (offline embedder, fast)
#   .\scripts\test.ps1 -RealEmbeddings -> same tests with the real local embedding model
#   .\scripts\test.ps1 -E2E            -> Playwright browser dialogues against the running app (BASE_URL)
param([switch]$RealEmbeddings, [switch]$E2E, [string]$BaseUrl = "http://127.0.0.1:8080")
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force reports | Out-Null
if ($RealEmbeddings) { $env:TEST_EMBEDDING = "fastembed" }
if ($E2E) {
    $env:BASE_URL = $BaseUrl
    .\.venv\Scripts\python -m playwright install chromium
    .\.venv\Scripts\python -m pytest tests/e2e -m e2e --junitxml=reports/e2e.xml --tracing=retain-on-failure --screenshot=only-on-failure --output=reports/e2e-artifacts
} else {
    .\.venv\Scripts\python -m pytest --ignore=tests/e2e --junitxml=reports/backend.xml
}
