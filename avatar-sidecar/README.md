# Lisa Avatar Sidecar

Python FastAPI service that bridges the C# `EchoBot` calling-bot with **Azure
Voice Live + Avatar**, routed to the deployed **`lisa-foundry-agent`** Foundry
hosted agent. This lets the avatar version of Lisa join Microsoft Teams
meetings as a participant — speaking, listening, and showing a video face.

## Architecture

```
Teams meeting
    │ (audio + video sockets, Skype.Bots.Media)
    ▼
EchoBot (C#, Windows VM)
    │ WebSocket  ws://<host>:5001/stream
    ▼
This sidecar (Python, FastAPI)
    │ WSS (Voice Live, agent mode)
    ▼
Azure Voice Live + Avatar
    │ (Foundry agent routing — agent_name + project_name)
    ▼
lisa-foundry-agent  (hosted agent → instructions + tools)
```

Voice Live owns **STT → LLM (the Foundry agent) → TTS → Avatar video**.
The sidecar only forwards audio + decodes the H.264 avatar stream into NV12
frames the C# bot can push into the Teams video socket.

## Prerequisites

- **Python 3.10+**
- **FFmpeg** on PATH (decodes the avatar's fMP4 H.264 to NV12)
- **Foundry resource** with Voice Live enabled. Avatar regions: Southeast
  Asia, North Europe, West Europe, Sweden Central, South Central US, East US 2,
  West US 2.
- **Deployed `lisa-foundry-agent`** (see `../../../lisa-foundry-agent/`). The
  agent's `microsoft.voice-live.configuration` metadata must define the
  voice, VAD, and transcription — the sidecar does not set them in agent mode.
- Caller principal needs **`Cognitive Services User`** + **`Azure AI User`**
  on the Foundry resource (when using Entra ID auth).

## Setup

```powershell
cd avatar-sidecar
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Edit .env — at minimum set:
#   AZURE_VOICELIVE_ENDPOINT
#   LISA_FOUNDRY_AGENT_NAME / _AGENT_VERSION / _PROJECT_NAME
#   AZURE_TENANT_ID  (your Azure tenant)
az login --tenant <TENANT_ID>
```

## Run

```powershell
python main.py
```

The sidecar listens on `ws://0.0.0.0:5001/stream`. Health check:

```powershell
curl http://localhost:5001/health
```

The health response shows whether the sidecar resolved into agent mode and
which agent + avatar it will use.

## Modes

### Agent mode (preferred)

Set all three:

```dotenv
LISA_FOUNDRY_AGENT_NAME=lisa
LISA_FOUNDRY_AGENT_VERSION=1
LISA_FOUNDRY_PROJECT_NAME=<your-foundry-project>
```

The sidecar opens Voice Live with `AgentSessionConfig`. Lisa's instructions
and the `lookup_job_requirements` tool live entirely inside the deployed
agent. `session.update` only carries audio formats + avatar config —
`instructions` / `voice` / `tools` / `turn_detection` are **omitted** because
the agent owns them (per MS Learn voice-live-agents-quickstart).

### Inline-fallback mode

Used when the agent vars above are missing — useful for local dev without a
deployed agent. The sidecar sets a minimal Lisa system prompt + German voice
+ semantic VAD directly on the session.

## Protocol (unchanged from the upstream sample)

| Direction      | Frame                                                                      |
|----------------|----------------------------------------------------------------------------|
| Bot → Sidecar  | `{"type":"audio","data":"<base64 PCM16 16kHz mono>"}`                      |
| Sidecar → Bot  | `{"type":"audio","data":"<base64 PCM16 16kHz mono>"}`                      |
| Sidecar → Bot  | `{"type":"video","data":"<base64 NV12>","width":W,"height":H,"timestamp":T}` |
| Sidecar → Bot  | `{"type":"speaking","value":true/false}`                                   |

Audio is resampled 16 kHz ↔ 24 kHz inside the sidecar (Teams uses 16 kHz,
Voice Live uses 24 kHz).

## Cost telemetry

The sidecar meters every Voice Live session and can persist one **`CostRecord`**
per call to the shared **Azure Table** (`callcosts`), the same store the
`browser-fallback` app and the `costboard` dashboard use.

- **Module:** `cost_telemetry.py` is a **vendored verbatim copy** of the parent
  repo's `core/cost.py`. The sidecar ships self-contained (the VMSS hot-patch
  path copies only `main.py` + siblings) so it can't import `core`. **Keep
  `cost_telemetry.py` in sync with `core/cost.py`** when rates, the record
  schema, or the sink change.
- **Lifecycle:** `main.py` starts a `CostMeter(is_teams=True)` on `/stream`
  connect, folds Voice Live token usage in on each `response.done`, and on
  disconnect writes a `CostRecord(transport="vmss")` (ACS rate forced to 0 — the
  Graph-bot path has no ACS leg).
- **Opt-in & fail-soft:** with no `COST_STORE_*` configured — or if
  `azure-data-tables` isn't installed — the sidecar runs normally and writes no
  rows. A storage hiccup never takes down a live call.
- **Auth:** on the VMSS, `install.ps1` sets `USE_MANAGED_IDENTITY=1` +
  `AZURE_TENANT_ID`, so the sink authenticates as the VMSS **system-assigned
  managed identity**. That identity needs **Storage Table Data Contributor** on
  the storage account.

Enable it by setting `COST_STORE_ACCOUNT` (and optionally `COST_STORE_TABLE`,
default `callcosts`) — see `.env.example`. View the data with the `costboard`
app; full reference in [`docs/RUNBOOK.md` §7](../../docs/RUNBOOK.md#7-cost-telemetry--the-costboard-dashboard).

## Common issues

- **`Tenant provided in token does not match resource token`** — set
  `AZURE_TENANT_ID` in `.env` and run `az login --tenant <id>`. The sidecar
  uses `AzureCliCredential(tenant_id=...)` to avoid the multi-tenant token leak.
- **400 on the WSS handshake** — agent mode requires
  `<resource>.services.ai.azure.com`; the sidecar auto-rewrites
  `cognitiveservices.azure.com` → `services.ai.azure.com` when agent mode is on.
- **`max_config_attempts_exceeded`** — something inside `session.update`
  conflicts with the agent's metadata (likely you're sending `instructions`,
  `voice`, or `tools` in agent mode). The sidecar already strips these; if you
  modified `_build_session_config`, double-check.
- **No avatar video** — confirm FFmpeg is on PATH and the Foundry resource's
  region supports Avatar. Check the sidecar log for the `[VL event] response.video.delta` line.
