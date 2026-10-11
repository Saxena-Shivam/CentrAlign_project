$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$env:APP_HOST = "127.0.0.1"
$env:APP_PORT = "3000"
$env:APP_BASE_URL = "http://127.0.0.1:3000"
$env:DEMO_USERNAME = "demo"
$env:DEMO_PASSWORD = "demo"
$env:INBOX_DIR = "$projectRoot\inbox"

Write-Host "Starting company app..."
Start-Process -FilePath "powershell" -ArgumentList "-NoExit", "-Command", "cd '$projectRoot'; .\.venv\Scripts\python.exe -m centralign_agent.cli app" -WorkingDirectory $projectRoot

Start-Sleep -Seconds 2
Write-Host "Starting worker..."
.\.venv\Scripts\python.exe -m centralign_agent.cli run --goal "Login to the company app and add a payable for Northwind with invoice INV-3099 and due date 2026-12-15."
