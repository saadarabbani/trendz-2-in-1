# Start a shop laptop that was set up with Setup-ShopLaptop.ps1. Docker Desktop restarts the stack by itself after a
# power cut; use this only if it is not running. No internet is needed.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.env')) { throw 'This laptop is not set up yet. Run Setup-ShopLaptop.ps1 first.' }
docker compose -f compose.yaml -f compose.node.yaml up -d --no-build --pull never
if ($LASTEXITCODE -ne 0) { throw 'The local server could not start. Check that Docker Desktop is running.' }
Start-Process 'http://localhost:8081'
