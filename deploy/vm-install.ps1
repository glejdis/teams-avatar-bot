# ============================================================================
# vm-install.ps1 — Run this ON THE VM (as Administrator) after unzipping
# ============================================================================
# Assumes you have in C:\Users\<your-vm-user>\Downloads\:
#   - echobot-publish.zip   (the C# bot build)
#   - avatar-sidecar.zip    (the Python sidecar)
#   - vm-install.ps1        (this file)
# ============================================================================

$ErrorActionPreference = "Stop"

$BotDir     = "C:\bot"
$SidecarDir = "C:\bot\avatar-sidecar"
$LogsDir    = "C:\bot\logs"
$DownloadsDir = if ($env:LISA_DOWNLOADS_DIR) { $env:LISA_DOWNLOADS_DIR } else { "$env:USERPROFILE\Downloads" }

# ── Config: set these as env vars before running this script ───────────────
$FoundryEndpoint  = if ($env:FOUNDRY_ENDPOINT)            { $env:FOUNDRY_ENDPOINT }            else { throw "FOUNDRY_ENDPOINT env var is required" }
$LisaAgentName    = if ($env:LISA_FOUNDRY_AGENT_NAME)     { $env:LISA_FOUNDRY_AGENT_NAME }     else { "lisa" }
$LisaAgentVersion = if ($env:LISA_FOUNDRY_AGENT_VERSION)  { $env:LISA_FOUNDRY_AGENT_VERSION }  else { "" }
$LisaProjectName  = if ($env:LISA_FOUNDRY_PROJECT_NAME)   { $env:LISA_FOUNDRY_PROJECT_NAME }   else { throw "LISA_FOUNDRY_PROJECT_NAME env var is required" }
$AzureTenantId    = if ($env:AZURE_TENANT_ID)             { $env:AZURE_TENANT_ID }             else { "" }
$VoiceLiveModel   = "gpt-realtime"

Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  EchoBot + Avatar Sidecar (Lisa) — VM Install" -ForegroundColor Cyan
Write-Host "════════════════════════════════════════════════════" -ForegroundColor Cyan

# ── 0) Need admin ──────────────────────────────────────────────────────────
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator!"
    exit 1
}

# ── 1) Create directories ──────────────────────────────────────────────────
Write-Host "`n[1/10] Creating directories..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path $BotDir, $SidecarDir, $LogsDir | Out-Null

# ── 2) Stop services if they exist ─────────────────────────────────────────
Write-Host "`n[2/10] Stopping existing services..." -ForegroundColor Yellow
@('EchoBot','AvatarSidecar') | ForEach-Object {
    if (Get-Service $_ -ErrorAction SilentlyContinue) {
        Write-Host "  Stopping $_..."
        Stop-Service $_ -Force -ErrorAction SilentlyContinue
    }
}

# ── 3) Backup old bot dir ──────────────────────────────────────────────────
Write-Host "`n[3/10] Backing up current bot (if any)..." -ForegroundColor Yellow
$hasOldBot = Get-ChildItem $BotDir -Exclude 'avatar-sidecar','logs' -ErrorAction SilentlyContinue
if ($hasOldBot) {
    $backup = "$BotDir-backup-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Write-Host "  Backing up current bot files to $backup"
    New-Item -ItemType Directory -Path $backup | Out-Null
    Get-ChildItem $BotDir -Exclude 'avatar-sidecar','logs' | Move-Item -Destination $backup
}

# ── 4) Extract bot ZIP ─────────────────────────────────────────────────────
Write-Host "`n[4/10] Extracting bot..." -ForegroundColor Yellow
$botZip = Join-Path $DownloadsDir "echobot-publish.zip"
if (-not (Test-Path $botZip)) { throw "Bot ZIP not found: $botZip" }
Expand-Archive -Path $botZip -DestinationPath "$env:TEMP\echobot-extract" -Force
# The ZIP contains a `publish/` folder
Copy-Item "$env:TEMP\echobot-extract\publish\*" -Destination $BotDir -Recurse -Force
Remove-Item "$env:TEMP\echobot-extract" -Recurse -Force

# ── 5) Extract sidecar ZIP ─────────────────────────────────────────────────
Write-Host "`n[5/10] Extracting sidecar..." -ForegroundColor Yellow
$sidecarZip = Join-Path $DownloadsDir "avatar-sidecar.zip"
if (-not (Test-Path $sidecarZip)) { throw "Sidecar ZIP not found: $sidecarZip" }
Expand-Archive -Path $sidecarZip -DestinationPath "$env:TEMP\sidecar-extract" -Force
Copy-Item "$env:TEMP\sidecar-extract\avatar-sidecar\*" -Destination $SidecarDir -Recurse -Force
Remove-Item "$env:TEMP\sidecar-extract" -Recurse -Force

# ── 6) Install Chocolatey (if missing) + Python + FFmpeg + NSSM ────────────
Write-Host "`n[6/10] Installing Python, FFmpeg, NSSM..." -ForegroundColor Yellow
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Host "  Installing Chocolatey..."
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}
choco install -y python311 ffmpeg nssm --no-progress --limit-output
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# ── 7) Python venv + deps ──────────────────────────────────────────────────
Write-Host "`n[7/10] Setting up Python venv + dependencies..." -ForegroundColor Yellow
Push-Location $SidecarDir
if (-not (Test-Path "$SidecarDir\venv")) {
    python -m venv venv
}
& "$SidecarDir\venv\Scripts\pip.exe" install --upgrade pip --quiet
& "$SidecarDir\venv\Scripts\pip.exe" install -r requirements.txt --quiet
Pop-Location

# ── 8) Write .env ──────────────────────────────────────────────────────────
Write-Host "`n[8/10] Writing sidecar .env..." -ForegroundColor Yellow
@"
AZURE_VOICELIVE_ENDPOINT=$FoundryEndpoint
# Empty key -> Managed Identity (DefaultAzureCredential)
AZURE_VOICELIVE_API_VERSION=2026-01-01-preview
AZURE_TENANT_ID=$AzureTenantId
# Foundry agent routing (Lisa's brain runs in the deployed agent)
LISA_FOUNDRY_AGENT_NAME=$LisaAgentName
LISA_FOUNDRY_AGENT_VERSION=$LisaAgentVersion
LISA_FOUNDRY_PROJECT_NAME=$LisaProjectName
# Inline-fallback model (used only if the agent vars above are unset)
VOICELIVE_MODEL=$VoiceLiveModel
VOICELIVE_VOICE=de-DE-KatjaNeural
AVATAR_CHARACTER=lisa
AVATAR_STYLE=casual-sitting
VIDEO_WIDTH=1920
VIDEO_HEIGHT=1080
"@ | Out-File -FilePath "$SidecarDir\.env" -Encoding utf8 -Force

# ── 9) Register both services with NSSM ────────────────────────────────────
Write-Host "`n[9/10] Registering Windows services..." -ForegroundColor Yellow

# AvatarSidecar
if (Get-Service AvatarSidecar -ErrorAction SilentlyContinue) { nssm remove AvatarSidecar confirm | Out-Null }
nssm install AvatarSidecar "$SidecarDir\venv\Scripts\python.exe" "$SidecarDir\main.py"
nssm set AvatarSidecar AppDirectory $SidecarDir
nssm set AvatarSidecar AppStdout "$LogsDir\sidecar.log"
nssm set AvatarSidecar AppStderr "$LogsDir\sidecar-err.log"
nssm set AvatarSidecar AppRotateFiles 1
nssm set AvatarSidecar AppRotateBytes 10485760
nssm set AvatarSidecar Start SERVICE_AUTO_START
nssm set AvatarSidecar AppExit Default Restart
nssm set AvatarSidecar AppRestartDelay 5000
nssm set AvatarSidecar DisplayName "Avatar Sidecar (Voice Live API)"
nssm set AvatarSidecar Description "Python bridge between EchoBot and Azure Voice Live API"
Write-Host "  ✓ AvatarSidecar registered"

# EchoBot (depends on AvatarSidecar)
$BotExe = Get-ChildItem $BotDir -Filter "EchoBot.exe" | Select-Object -First 1
if (-not $BotExe) { throw "EchoBot.exe not found in $BotDir" }
if (Get-Service EchoBot -ErrorAction SilentlyContinue) { nssm remove EchoBot confirm | Out-Null }
nssm install EchoBot $BotExe.FullName
nssm set EchoBot AppDirectory $BotDir
nssm set EchoBot AppStdout "$LogsDir\bot.log"
nssm set EchoBot AppStderr "$LogsDir\bot-err.log"
nssm set EchoBot AppRotateFiles 1
nssm set EchoBot AppRotateBytes 10485760
nssm set EchoBot Start SERVICE_AUTO_START
nssm set EchoBot AppExit Default Restart
nssm set EchoBot AppRestartDelay 10000
nssm set EchoBot DependOnService AvatarSidecar
nssm set EchoBot DisplayName "EchoBot (Teams Calling Bot)"
Write-Host "  ✓ EchoBot registered (depends on AvatarSidecar)"

# ── 10) Start services + health check ─────────────────────────────────────
Write-Host "`n[10/10] Starting services..." -ForegroundColor Yellow
Start-Service AvatarSidecar
Start-Sleep -Seconds 5
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:5001/health" -UseBasicParsing -TimeoutSec 5
    Write-Host "  ✓ Sidecar health: $($resp.Content)" -ForegroundColor Green
} catch {
    Write-Warning "  ⚠ Sidecar health check failed: $_"
    Write-Host "  Check logs: $LogsDir\sidecar-err.log"
}

Start-Service EchoBot
Start-Sleep -Seconds 3

Write-Host "`n════════════════════════════════════════════════════" -ForegroundColor Cyan
Get-Service EchoBot, AvatarSidecar | Format-Table Name, Status, StartType
Write-Host "`n✅ Install complete!" -ForegroundColor Green
Write-Host ""
Write-Host "Logs:"
Write-Host "  $LogsDir\sidecar.log"
Write-Host "  $LogsDir\bot.log"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1) Make sure Managed Identity has 'Cognitive Services User' + 'Azure AI User'"
Write-Host "     on the Foundry resource (run: az role assignment create --assignee <PID> ...)"
Write-Host "  2) Call the bot's web API to invite Lisa to a Teams meeting!"
