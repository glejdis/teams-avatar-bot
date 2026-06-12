# ============================================================================
# Deploy EchoBot + Avatar Sidecar to Azure VM (voice-proxy-vm)
# ============================================================================
# Run LOCALLY on your Mac. This script:
#   1. Builds the C# bot
#   2. Copies bot + sidecar to the VM via SCP
#   3. Installs Python, FFmpeg, NSSM on the VM
#   4. Sets up both Windows services with correct start order
#   5. Enables Managed Identity + assigns Azure AI roles
# ============================================================================

set -euo pipefail

# ── Configuration (override via env vars) ──────────────────────────────────
RESOURCE_GROUP="${RESOURCE_GROUP:-lisa-bot-rg}"
VM_NAME="${VM_NAME:-lisa-bot-vm}"
VM_USER="${VM_USER:-azureuser}"
VM_IP="${VM_IP:?Set VM_IP=<public ip of the bot VM>}"

# AI Foundry resource for Voice Live API
FOUNDRY_RG="${FOUNDRY_RG:?Set FOUNDRY_RG=<RG of the Foundry resource>}"
FOUNDRY_NAME="${FOUNDRY_NAME:?Set FOUNDRY_NAME=<Foundry resource name>}"
FOUNDRY_ENDPOINT="${FOUNDRY_ENDPOINT:-https://${FOUNDRY_NAME}.services.ai.azure.com/}"
LISA_FOUNDRY_AGENT_NAME="${LISA_FOUNDRY_AGENT_NAME:-lisa}"
LISA_FOUNDRY_AGENT_VERSION="${LISA_FOUNDRY_AGENT_VERSION:?Set LISA_FOUNDRY_AGENT_VERSION (output of deploy.sh)}"
LISA_FOUNDRY_PROJECT_NAME="${LISA_FOUNDRY_PROJECT_NAME:?Set LISA_FOUNDRY_PROJECT_NAME}"
AZURE_TENANT_ID="${AZURE_TENANT_ID:?Set AZURE_TENANT_ID (your Azure tenant GUID)}"

# Bot AAD app + DNS + cert (filled into EchoBot/appsettings.json on the VM).
BOT_AAD_APP_ID="${BOT_AAD_APP_ID:?Set BOT_AAD_APP_ID (from script 02)}"
BOT_AAD_APP_SECRET="${BOT_AAD_APP_SECRET:?Set BOT_AAD_APP_SECRET (from script 02 — store in Key Vault!)}"
BOT_AAD_TENANT_ID="${BOT_AAD_TENANT_ID:-$AZURE_TENANT_ID}"
BOT_DNS_NAME="${BOT_DNS_NAME:?Set BOT_DNS_NAME (FQDN of the bot, must match the TLS cert)}"
BOT_CERT_THUMBPRINT="${BOT_CERT_THUMBPRINT:?Set BOT_CERT_THUMBPRINT (PFX installed in LocalMachine\\My on the VM)}"

# Paths on the VM (Windows)
VM_BOT_DIR='C:\bot'
VM_SIDECAR_DIR='C:\bot\avatar-sidecar'
VM_LOGS_DIR='C:\bot\logs'

# Paths locally
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOT_SRC="$SCRIPT_DIR/src/EchoBot"
SIDECAR_SRC="$SCRIPT_DIR/avatar-sidecar"
PUBLISH_DIR="$SCRIPT_DIR/publish"

# ── 1) Build the bot locally ───────────────────────────────────────────────
echo "▶ Step 1/6: Building EchoBot (net6.0, win-x64)..."
rm -rf "$PUBLISH_DIR"
dotnet publish "$BOT_SRC/EchoBot.csproj" \
    -c Release \
    -r win-x64 \
    --self-contained false \
    -o "$PUBLISH_DIR/bot"

# ── 2) Stage the sidecar ───────────────────────────────────────────────────
echo "▶ Step 2/6: Staging sidecar..."
mkdir -p "$PUBLISH_DIR/avatar-sidecar"
cp -r "$SIDECAR_SRC/"* "$PUBLISH_DIR/avatar-sidecar/"
# Don't ship the dev .env — create a clean one on the VM instead
rm -f "$PUBLISH_DIR/avatar-sidecar/.env"

# ── 3) Copy to VM via SCP ──────────────────────────────────────────────────
echo "▶ Step 3/6: Copying files to VM (this may take a minute)..."
# Requires SSH key setup. If SSH is not available, set DEPLOY_VIA_BLOB=1 and
# use az storage blob + az vm run-command Invoke-WebRequest as an alternative
# (not implemented here — file an issue if you need it).
#
# SSH_KEY_PATH overrides the default ~/.ssh/id_* lookup. Recommended workflow:
# generate a dedicated ed25519 key for VM access (see
# scripts/lisa-stack/00-bootstrap-ssh.ps1) and export SSH_KEY_PATH=...
SSH_OPTS=()
if [[ -n "${SSH_KEY_PATH:-}" ]]; then
    SSH_OPTS+=( -i "$SSH_KEY_PATH" )
fi
# Disable agent forwarding + don't pollute known_hosts on rotating VM IPs.
SSH_OPTS+=( -o StrictHostKeyChecking=accept-new )

scp "${SSH_OPTS[@]}" -r "$PUBLISH_DIR/"* "$VM_USER@$VM_IP:C:/bot-staging/"

# ── 4) Enable Managed Identity on VM + assign AI roles ─────────────────────
echo "▶ Step 4/6: Enabling Managed Identity + assigning roles..."
az vm identity assign \
    --resource-group "$RESOURCE_GROUP" \
    --name "$VM_NAME" \
    --output none

PRINCIPAL_ID=$(az vm show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$VM_NAME" \
    --query identity.principalId -o tsv)

FOUNDRY_ID=$(az cognitiveservices account show \
    --resource-group "$FOUNDRY_RG" \
    --name "$FOUNDRY_NAME" \
    --query id -o tsv)

echo "  Principal: $PRINCIPAL_ID"
echo "  Scope:     $FOUNDRY_ID"

for ROLE in "Cognitive Services User" "Azure AI User"; do
    az role assignment create \
        --assignee "$PRINCIPAL_ID" \
        --role "$ROLE" \
        --scope "$FOUNDRY_ID" \
        --output none 2>/dev/null || echo "  (role '$ROLE' already assigned)"
done

# ── 5) Run remote setup script on VM ───────────────────────────────────────
echo "▶ Step 5/6: Running remote setup on VM..."

# az vm run-command does not propagate local env vars. Build a wrapper that
# prepends $env:VAR assignments (base64-encoded to avoid quoting/escape pain),
# then concatenates the real setup-vm.ps1 below it.
WRAPPER="$(mktemp -t lisa-setup-vm.XXXXXX.ps1)"
trap 'rm -f "$WRAPPER"' EXIT

emit_env() {
    # $1 = name, $2 = value (may be empty, may contain quotes/$/spaces)
    local name="$1" value="${2:-}"
    if command -v base64 >/dev/null 2>&1; then
        local b64
        b64=$(printf %s "$value" | base64 | tr -d '\n')
        echo "\$env:${name} = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${b64}'))"
    else
        # Fallback: single-quote escape (PowerShell single-quoted strings: '' = literal ')
        local escaped="${value//\'/\'\'}"
        echo "\$env:${name} = '${escaped}'"
    fi
}

{
    echo "# --- Auto-generated wrapper: inject env then run setup-vm.ps1 ---"
    emit_env FOUNDRY_ENDPOINT            "$FOUNDRY_ENDPOINT"
    emit_env LISA_FOUNDRY_AGENT_NAME     "$LISA_FOUNDRY_AGENT_NAME"
    emit_env LISA_FOUNDRY_AGENT_VERSION  "$LISA_FOUNDRY_AGENT_VERSION"
    emit_env LISA_FOUNDRY_PROJECT_NAME   "$LISA_FOUNDRY_PROJECT_NAME"
    emit_env AZURE_TENANT_ID             "$AZURE_TENANT_ID"
    emit_env BOT_AAD_APP_ID              "$BOT_AAD_APP_ID"
    emit_env BOT_AAD_APP_SECRET          "$BOT_AAD_APP_SECRET"
    emit_env BOT_AAD_TENANT_ID           "$BOT_AAD_TENANT_ID"
    emit_env BOT_DNS_NAME                "$BOT_DNS_NAME"
    emit_env BOT_CERT_THUMBPRINT         "$BOT_CERT_THUMBPRINT"
    echo "# --- End wrapper prologue ---"
    cat "$SCRIPT_DIR/deploy/setup-vm.ps1"
} > "$WRAPPER"

az vm run-command invoke \
    --resource-group "$RESOURCE_GROUP" \
    --name "$VM_NAME" \
    --command-id RunPowerShellScript \
    --scripts @"$WRAPPER" \
    --output table

# ── 6) Verify ──────────────────────────────────────────────────────────────
echo "▶ Step 6/6: Verifying services..."
az vm run-command invoke \
    --resource-group "$RESOURCE_GROUP" \
    --name "$VM_NAME" \
    --command-id RunPowerShellScript \
    --scripts 'Get-Service EchoBot, AvatarSidecar | Format-Table Name, Status, StartType' \
    --output table

echo ""
echo "✅ Done!"
echo "   Check logs on VM: $VM_LOGS_DIR\sidecar.log"
echo "   Test the bot:     Call your Teams bot"
