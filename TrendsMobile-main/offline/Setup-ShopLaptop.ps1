# One-time setup of a shop laptop that keeps working without internet (see OFFLINE_LAPTOP_GUIDE.md).
# Needs: Docker Desktop (set to start when Windows starts), an internet connection for this first run, the cloud ERP
# address and the laptop's sync key (from `manage.py sync_register_node` on the cloud side).
param(
    [Parameter(Mandatory=$true)][string]$HubUrl,
    [Parameter(Mandatory=$true)][string]$NodeKey,
    [string]$SupabaseUrl = '',
    [string]$SupabasePublishableKey = ''
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$compose = @('compose', '-f', 'compose.yaml', '-f', 'compose.node.yaml')

function New-LocalSecret {
    $bytes = New-Object byte[] 48
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $random.GetBytes($bytes); $random.Dispose()
    return [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
}

if (Test-Path '.env') { throw '.env already exists: this laptop is already set up. To start it, run Start-ShopLaptop.ps1.' }
$lines = @(
    "DJANGO_SECRET_KEY=$(New-LocalSecret)",
    "POSTGRES_PASSWORD=$(New-LocalSecret)",
    'DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,web,testserver',
    'COVISON_BIND_ADDRESS=127.0.0.1',
    'COVISON_NODE=1',
    "SYNC_HUB_URL=$($HubUrl.TrimEnd('/'))",
    "SYNC_NODE_KEY=$NodeKey"
)
if ($SupabaseUrl) { $lines += "SUPABASE_URL=$SupabaseUrl"; $lines += "SUPABASE_PUBLISHABLE_KEY=$SupabasePublishableKey" }
$lines | Set-Content -Encoding ASCII '.env'
New-Item -ItemType Directory -Force imports,backups,artifacts | Out-Null

docker image inspect covison/offline:development *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Building the application image for this laptop (needs internet, a few minutes)...'
    docker compose build web
    if ($LASTEXITCODE -ne 0) { throw 'The image could not be built. Check the internet connection and Docker Desktop.' }
}
docker @compose up -d --no-build db web
if ($LASTEXITCODE -ne 0) { throw 'The local server could not start. Check Docker Desktop.' }
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try { Invoke-WebRequest -UseBasicParsing 'http://localhost:8081/health/' -TimeoutSec 2 | Out-Null; break }
    catch {
        if ($attempt -eq 0) { docker @compose up -d --no-build proxy | Out-Null }
        if ($attempt -eq 59) { throw 'The local server did not become ready. Run: docker compose logs web' }
        Start-Sleep -Seconds 2
    }
}
Write-Host 'Copying the cloud data to this laptop...'
docker @compose exec -T web python manage.py sync_provision
if ($LASTEXITCODE -ne 0) { throw 'The cloud data could not be copied. Check the address, the sync key and the internet connection, then run this script again after deleting .env.' }
docker @compose up -d --no-build
Write-Host 'Done. This laptop now works with or without internet. Sign in once now, while online, with each login that will use it.'
Start-Process 'http://localhost:8081'
