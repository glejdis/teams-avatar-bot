# ============================================================================
# setup-vm.ps1 — runs ON the VM via `az vm run-command`
# Installs Python, FFmpeg, NSSM; registers both services with correct order.
# ============================================================================

$ErrorActionPreference = "Stop"

$BotDir      = "C:\bot"
$SidecarDir  = "C:\bot\avatar-sidecar"
$LogsDir     = "C:\bot\logs"
$StagingDir  = "C:\bot-staging"

# These must be set as environment variables before running this script
# (deploy-to-vm.sh injects them via `az vm run-command`).
$FoundryEndpoint    = if ($env:FOUNDRY_ENDPOINT)        { $env:FOUNDRY_ENDPOINT }        else { throw "FOUNDRY_ENDPOINT env var is required" }
$LisaAgentName      = if ($env:LISA_FOUNDRY_AGENT_NAME) { $env:LISA_FOUNDRY_AGENT_NAME } else { "lisa" }
$LisaAgentVersion   = if ($env:LISA_FOUNDRY_AGENT_VERSION) { $env:LISA_FOUNDRY_AGENT_VERSION } else { "" }
$LisaProjectName    = if ($env:LISA_FOUNDRY_PROJECT_NAME)  { $env:LISA_FOUNDRY_PROJECT_NAME }  else { throw "LISA_FOUNDRY_PROJECT_NAME env var is required" }
$AzureTenantId      = if ($env:AZURE_TENANT_ID)         { $env:AZURE_TENANT_ID }         else { "" }

# Bot AAD + DNS settings injected into EchoBot/appsettings.json.
# Required on first deploy; optional on re-deploy (existing values preserved if missing).
$BotAadAppId        = if ($env:BOT_AAD_APP_ID)         { $env:BOT_AAD_APP_ID }         else { "" }
$BotAadAppSecret    = if ($env:BOT_AAD_APP_SECRET)     { $env:BOT_AAD_APP_SECRET }     else { "" }
$BotAadTenantId     = if ($env:BOT_AAD_TENANT_ID)      { $env:BOT_AAD_TENANT_ID }      else { $AzureTenantId }
$BotDnsName         = if ($env:BOT_DNS_NAME)           { $env:BOT_DNS_NAME }           else { "" }
$BotCertThumbprint  = if ($env:BOT_CERT_THUMBPRINT)    { $env:BOT_CERT_THUMBPRINT }    else { "" }

Write-Host "==> Creating directories"
New-Item -ItemType Directory -Force -Path $BotDir, $SidecarDir, $LogsDir | Out-Null

# ── 1) Stop existing services so files can be replaced ─────────────────────
Write-Host "==> Stopping existing services (if any)"
@('EchoBot','AvatarSidecar') | ForEach-Object {
    if (Get-Service $_ -ErrorAction SilentlyContinue) {
        Stop-Service $_ -Force -ErrorAction SilentlyContinue
    }
}

# ── 2) Move staged files into place ────────────────────────────────────────
Write-Host "==> Deploying bot binaries"
Copy-Item -Path "$StagingDir\bot\*" -Destination $BotDir -Recurse -Force

Write-Host "==> Deploying sidecar"
Copy-Item -Path "$StagingDir\avatar-sidecar\*" -Destination $SidecarDir -Recurse -Force

# ── 2a) Patch EchoBot/appsettings.json with bot AAD + DNS + Foundry config ─
Write-Host "==> Patching appsettings.json"
$appsettingsPath = Get-ChildItem -Path $BotDir -Filter 'appsettings.json' -Recurse |
    Select-Object -First 1
if (-not $appsettingsPath) {
    throw "appsettings.json not found under $BotDir — bot publish output may be incomplete"
}

$appsettings = Get-Content -Raw -Path $appsettingsPath.FullName | ConvertFrom-Json
if (-not $appsettings.AppSettings) {
    $appsettings | Add-Member -NotePropertyName AppSettings -NotePropertyValue ([pscustomobject]@{}) -Force
}
$as = $appsettings.AppSettings

function Set-IfProvided($obj, $name, $value) {
    if ($null -ne $value -and $value -ne '') {
        $obj | Add-Member -NotePropertyName $name -NotePropertyValue $value -Force
    }
}

Set-IfProvided $as 'AadAppId'              $BotAadAppId
Set-IfProvided $as 'AadAppSecret'          $BotAadAppSecret
Set-IfProvided $as 'AadTenantId'           $BotAadTenantId
Set-IfProvided $as 'ServiceDnsName'        $BotDnsName
Set-IfProvided $as 'MediaDnsName'          $BotDnsName
Set-IfProvided $as 'CertificateThumbprint' $BotCertThumbprint

# Wire the bot's local sidecar endpoint + Foundry routing so EchoBot.exe and
# the AvatarSidecar agree on transport.
Set-IfProvided $as 'AvatarEndpoint'              'ws://localhost:5001'
$as | Add-Member -NotePropertyName 'UseAvatar' -NotePropertyValue $true -Force
$as | Add-Member -NotePropertyName 'UseSpeechService' -NotePropertyValue $true -Force
Set-IfProvided $as 'FoundryEndpoint'             $FoundryEndpoint
Set-IfProvided $as 'LisaFoundryAgentName'        $LisaAgentName
Set-IfProvided $as 'LisaFoundryAgentVersion'     $LisaAgentVersion
Set-IfProvided $as 'LisaFoundryProjectName'      $LisaProjectName

# Validate — bot won't start without these.
$missing = @()
foreach ($k in 'AadAppId','AadAppSecret','AadTenantId','ServiceDnsName','CertificateThumbprint') {
    if (-not $as.$k -or $as.$k -like '<*>') { $missing += $k }
}
if ($missing.Count -gt 0) {
    throw "appsettings.json is missing required values: $($missing -join ', '). Pass them via BOT_AAD_APP_ID / BOT_AAD_APP_SECRET / BOT_AAD_TENANT_ID / BOT_DNS_NAME / BOT_CERT_THUMBPRINT."
}

$appsettings | ConvertTo-Json -Depth 10 |
    Out-File -FilePath $appsettingsPath.FullName -Encoding utf8 -Force
Write-Host "  patched: $($appsettingsPath.FullName)"

# ── 3) Install Chocolatey (if needed) ──────────────────────────────────────
if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
    Write-Host "==> Installing Chocolatey"
    Set-ExecutionPolicy Bypass -Scope Process -Force
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072
    iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}

# ── 4) Install prerequisites ───────────────────────────────────────────────
Write-Host "==> Installing Python, FFmpeg, NSSM"
choco install -y python311 ffmpeg nssm --no-progress | Out-Null
$env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")

# ── 5) Create Python venv + install deps ───────────────────────────────────
Write-Host "==> Creating Python venv"
Push-Location $SidecarDir
if (-not (Test-Path "$SidecarDir\venv")) {
    python -m venv venv
}
& "$SidecarDir\venv\Scripts\pip.exe" install --upgrade pip --quiet
& "$SidecarDir\venv\Scripts\pip.exe" install -r requirements.txt --quiet
Pop-Location

# ── 6) Write .env for sidecar (uses Managed Identity, no key) ─────────────
Write-Host "==> Writing sidecar .env"
@"
AZURE_VOICELIVE_ENDPOINT=$FoundryEndpoint
# Empty API key -> uses Managed Identity via DefaultAzureCredential
AZURE_VOICELIVE_API_VERSION=2026-01-01-preview
AZURE_TENANT_ID=$AzureTenantId
# Foundry agent routing (Lisa's brain runs in the deployed agent)
LISA_FOUNDRY_AGENT_NAME=$LisaAgentName
LISA_FOUNDRY_AGENT_VERSION=$LisaAgentVersion
LISA_FOUNDRY_PROJECT_NAME=$LisaProjectName
# Avatar
AVATAR_CHARACTER=lisa
AVATAR_STYLE=casual-sitting
VIDEO_WIDTH=1920
VIDEO_HEIGHT=1080
"@ | Out-File -FilePath "$SidecarDir\.env" -Encoding utf8 -Force

# ── 7) Register AvatarSidecar as Windows Service via NSSM ──────────────────
Write-Host "==> Registering AvatarSidecar service"
if (Get-Service AvatarSidecar -ErrorAction SilentlyContinue) {
    nssm remove AvatarSidecar confirm | Out-Null
}
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

# ── 8) Register EchoBot as Windows Service, DEPENDENT on AvatarSidecar ─────
Write-Host "==> Registering EchoBot service"
$BotExe = Get-ChildItem -Path $BotDir -Filter "EchoBot.exe" -Recurse | Select-Object -First 1
if (-not $BotExe) { throw "EchoBot.exe not found in $BotDir" }

if (Get-Service EchoBot -ErrorAction SilentlyContinue) {
    nssm remove EchoBot confirm | Out-Null
}
nssm install EchoBot $BotExe.FullName
nssm set EchoBot AppDirectory $BotExe.Directory.FullName
nssm set EchoBot AppStdout "$LogsDir\bot.log"
nssm set EchoBot AppStderr "$LogsDir\bot-err.log"
nssm set EchoBot AppRotateFiles 1
nssm set EchoBot AppRotateBytes 10485760
nssm set EchoBot Start SERVICE_AUTO_START
nssm set EchoBot AppExit Default Restart
nssm set EchoBot AppRestartDelay 10000
nssm set EchoBot DependOnService AvatarSidecar   # <-- critical: sidecar first!
nssm set EchoBot DisplayName "EchoBot (Teams Calling Bot)"

# ── 9) Start services in correct order ─────────────────────────────────────
Write-Host "==> Starting AvatarSidecar"
Start-Service AvatarSidecar
Start-Sleep -Seconds 3

Write-Host "==> Starting EchoBot"
Start-Service EchoBot

# ── 10) Health check ───────────────────────────────────────────────────────
Write-Host "==> Health check"
Start-Sleep -Seconds 5
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:5001/health" -UseBasicParsing -TimeoutSec 5
    Write-Host "Sidecar health: $($resp.StatusCode) $($resp.Content)"
} catch {
    Write-Warning "Sidecar health check failed: $_"
}

Get-Service EchoBot, AvatarSidecar | Format-Table Name, Status, StartType

Write-Host "==> Setup complete"
