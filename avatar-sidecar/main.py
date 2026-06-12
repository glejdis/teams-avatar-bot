"""
Avatar Sidecar — Bridge between EchoBot (C#) and Azure Voice Live + Avatar
for the **Lisa** Company X HR screening agent.

Architecture:
    Teams → EchoBot (C#) ←─ WebSocket ─→ This Sidecar ←─ Voice Live API ─→ Azure
                ↑                               ↓
                └─── audio + NV12 video ────────┘

Voice Live runs in **Foundry Agent mode** when the LISA_FOUNDRY_AGENT_*
env vars are set. In that mode Lisa's brain (instructions + the
`lookup_job_requirements` tool) lives entirely inside the deployed
``lisa-foundry-agent`` Foundry agent, and this sidecar only carries audio
and the avatar video stream.

If agent mode is NOT configured we fall back to inline instructions so
local dev still works without a deployed agent.

Protocol (WebSocket on /stream — unchanged from the original sample):
    Bot → Sidecar:  {"type":"audio","data":"<base64 PCM16 16kHz mono>"}
    Sidecar → Bot:  {"type":"audio","data":"<base64 PCM16 16kHz mono>"}
    Sidecar → Bot:  {"type":"video","data":"<base64 NV12>","width":W,"height":H,"timestamp":T}
    Sidecar → Bot:  {"type":"speaking","value":true/false}
"""

import asyncio
import base64
import json
import uuid
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any, Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from azure.ai.voicelive.aio import AgentSessionConfig, connect
from azure.identity.aio import (
    AzureCliCredential,
    ChainedTokenCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
)
from azure.ai.voicelive.models import (
    AssistantMessageItem,
    AvatarConfig,
    AzureSemanticVad,
    AzureStandardVoice,
    AudioInputTranscriptionOptions,
    Background,
    InputAudioFormat,
    Modality,
    OutputTextContentPart,
    OutputAudioFormat,
    RequestSession,
    RequestTextContentPart,
    ServerVad,
    ServerEventType,
    SystemMessageItem,
    VideoParams,
    VideoResolution,
)
from azure.core.credentials import AzureKeyCredential

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
)
logger = logging.getLogger("lisa-avatar-sidecar")


# ── Cost telemetry (optional, fail-soft) ────────────────────────────────────
# Vendored from the parent repo's core/cost.py (see cost_telemetry.py header).
# Meters each call's Voice Live + avatar usage and persists one CostRecord to
# the Azure Table named by COST_STORE_TABLE on COST_STORE_ACCOUNT (set by
# install.ps1). Auth = the VMSS managed identity (USE_MANAGED_IDENTITY=1).
# Any failure here must never affect a live call, so the whole feature is
# guarded and degrades to a no-op if the module/deps/config are absent.
from datetime import datetime, timezone

try:
    from cost_telemetry import (  # type: ignore
        CostMeter,
        CostRecord,
        build_sink_from_env,
        cost_rates,
    )

    _COST_SINK = build_sink_from_env()
    logger.info(
        "cost telemetry: sink=%s table=%s",
        type(_COST_SINK).__name__,
        os.getenv("COST_STORE_TABLE", "callcosts"),
    )
except Exception as _cost_import_exc:  # pragma: no cover - defensive
    CostMeter = None  # type: ignore
    CostRecord = None  # type: ignore
    cost_rates = None  # type: ignore
    _COST_SINK = None
    logging.getLogger("lisa-avatar-sidecar").warning(
        "cost telemetry disabled: %s", _cost_import_exc
    )


def _cost_enabled() -> bool:
    return CostMeter is not None and getattr(_COST_SINK, "enabled", False)



# ── Configuration (env vars or defaults) ────────────────────────────────────
AZURE_ENDPOINT = os.getenv(
    "AZURE_VOICELIVE_ENDPOINT",
    "https://<your-foundry-resource>.services.ai.azure.com/",
)
# API key is optional — if empty, Entra ID is used (recommended for prod).
AZURE_API_KEY = os.getenv("AZURE_VOICELIVE_API_KEY", "")
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID") or os.getenv("FOUNDRY_TENANT_ID") or ""
API_VERSION = os.getenv("AZURE_VOICELIVE_API_VERSION", "2026-01-01-preview")


def _env_enabled(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")

# Foundry agent routing (preferred path — Lisa's brain runs in the agent).
LISA_AGENT_NAME = os.getenv("LISA_FOUNDRY_AGENT_NAME", "")
LISA_AGENT_VERSION = os.getenv("LISA_FOUNDRY_AGENT_VERSION", "")
LISA_PROJECT_NAME = os.getenv("LISA_FOUNDRY_PROJECT_NAME", "")

# Voice + avatar (used in inline fallback mode; ignored in agent mode where
# the agent's microsoft.voice-live.configuration metadata owns these).
MODEL = os.getenv("VOICELIVE_MODEL", "gpt-realtime")
VOICE_NAME = os.getenv("VOICELIVE_VOICE", "en-US-AvaMultilingualNeural")
AVATAR_CHARACTER = os.getenv("AVATAR_CHARACTER", "meg")
AVATAR_STYLE = os.getenv("AVATAR_STYLE", "business")
AVATAR_BACKGROUND_IMAGE_URL = os.getenv("AVATAR_BACKGROUND_IMAGE_URL", "").strip()
AVATAR_BACKGROUND_COLOR = os.getenv("AVATAR_BACKGROUND_COLOR", "").strip()
LANG = os.getenv("LISA_LANG", "en").lower()
ENABLE_AVATAR_VIDEO = os.getenv("LISA_ENABLE_AVATAR_VIDEO", "false").lower() in (
    "1",
    "true",
    "yes",
)

VIDEO_WIDTH = int(os.getenv("VIDEO_WIDTH", "1280"))
VIDEO_HEIGHT = int(os.getenv("VIDEO_HEIGHT", "720"))
VIDEO_BITRATE = int(os.getenv("VIDEO_BITRATE", "3500000"))
VIDEO_FPS = max(1, int(os.getenv("VIDEO_FPS", "15") or "15"))
VIDEO_GOP_SIZE = int(os.getenv("VIDEO_GOP_SIZE", "30"))
NV12_FRAME_SIZE = VIDEO_WIDTH * VIDEO_HEIGHT * 3 // 2

TURN_DETECTION_MODE = os.getenv("LISA_TURN_DETECTION", "azure_semantic_vad").strip().lower()
VAD_THRESHOLD = float(os.getenv("LISA_VAD_THRESHOLD", "0.35"))
VAD_PREFIX_PADDING_MS = int(os.getenv("LISA_VAD_PREFIX_PADDING_MS", "300"))
VAD_SPEECH_DURATION_MS = int(os.getenv("LISA_VAD_SPEECH_DURATION_MS", "180"))
VAD_SILENCE_DURATION_MS = int(os.getenv("LISA_VAD_SILENCE_DURATION_MS", "900"))
EOU_THRESHOLD_LEVEL = os.getenv("LISA_EOU_THRESHOLD_LEVEL", "high").strip().lower()
EOU_TIMEOUT_MS = int(os.getenv("LISA_EOU_TIMEOUT_MS", "1400"))
AUDIO_RESAMPLER_MODE = os.getenv("LISA_AUDIO_RESAMPLER", "ffmpeg").strip().lower()

# Lip-sync compensation: delay video frames by N ms before forwarding to
# the .NET bot so they line up with the audio jitter buffer in the Teams
# Media Platform. Lips currently arrive ahead of voice on the receiver
# side; positive value pushes lips later. Tune via NSSM env var without
# code change. 0 = no delay (legacy behavior).
LISA_VIDEO_DELAY_MS = max(0, int(os.getenv("LISA_VIDEO_DELAY_MS", "0") or "0"))
VIDEO_PREROLL_MAX_FRAMES = max(
    0, int(os.getenv("LISA_VIDEO_PREROLL_MAX_FRAMES", "60") or "0")
)

# Decoder memory guards. The VMSS sidecar runs in a small memory envelope; keep
# FFmpeg output queues bounded and log enough state to classify first-zero bugs.
MAX_FMP4_DELTA_BYTES = max(
    1024, int(os.getenv("LISA_MAX_FMP4_DELTA_BYTES", str(8 * 1024 * 1024)) or "0")
)
FMP4_PTS_BUFFER_MAX_BYTES = max(
    1024, int(os.getenv("LISA_FMP4_PTS_BUFFER_MAX_BYTES", str(4 * 1024 * 1024)) or "0")
)
VIDEO_DECODER_QUEUE_MAX_FRAMES = max(
    1, int(os.getenv("LISA_VIDEO_DECODER_QUEUE_MAX_FRAMES", "12") or "0")
)
VIDEO_DECODER_DRAIN_MAX_FRAMES = max(
    1, int(os.getenv("LISA_VIDEO_DECODER_DRAIN_MAX_FRAMES", "6") or "0")
)
AUDIO_DECODER_QUEUE_MAX_CHUNKS = max(
    1, int(os.getenv("LISA_AUDIO_DECODER_QUEUE_MAX_CHUNKS", "200") or "0")
)
AUDIO_DECODER_DRAIN_MAX_CHUNKS = max(
    1, int(os.getenv("LISA_AUDIO_DECODER_DRAIN_MAX_CHUNKS", "80") or "0")
)

# The bot mutes candidate audio while Lisa is speaking. If Voice Live misses
# or delays response.done, that gate can otherwise stay closed forever. Reopen
# it after avatar/audio output has gone idle for a short interval.
SPEAKING_IDLE_RESET_MS = max(
    0, int(os.getenv("LISA_SPEAKING_IDLE_RESET_MS", "650") or "0")
)

# When True, the AAC track muxed inside Voice Live's fMP4 video stream is
# decoded by FFmpegAudioDecoder and forwarded to the bot. That stream is
# already lip-synced to the video frames AND already at 16 kHz mono with
# dither, so we MUST then suppress the parallel response.audio.delta
# forwarding to avoid double-audio (overlapping copies of Lisa's voice =
# robotic / phasey timbre + apparent delay).
_FORWARD_MUXED_AUDIO = ENABLE_AVATAR_VIDEO and os.getenv("LISA_FORWARD_MUXED_AUDIO", "0").lower() in (
    "1",
    "true",
    "yes",
)

# Optional server-side background compositing. Disabled by default. When
# enabled, Voice Live should render the avatar over a solid chroma background
# and this sidecar replaces that chroma with a local image before sending NV12
# frames to Teams.
COMPOSITE_BACKGROUND_ENABLED = _env_enabled("LISA_COMPOSITE_BACKGROUND_ENABLED")
COMPOSITE_BACKGROUND_IMAGE = os.getenv("LISA_COMPOSITE_BACKGROUND_IMAGE", "").strip()
COMPOSITE_CHROMA_COLOR = os.getenv("LISA_COMPOSITE_CHROMA_COLOR", "#00ff00").strip()
COMPOSITE_CHROMA_TOLERANCE = max(
    1, int(os.getenv("LISA_COMPOSITE_CHROMA_TOLERANCE", "45") or "45")
)
COMPOSITE_GREEN_MIN = min(
    255, max(0, int(os.getenv("LISA_COMPOSITE_GREEN_MIN", "120") or "120"))
)
COMPOSITE_GREEN_MARGIN = min(
    255, max(1, int(os.getenv("LISA_COMPOSITE_GREEN_MARGIN", "35") or "35"))
)
COMPOSITE_EDGE_SOFTNESS = min(
    255, max(1, int(os.getenv("LISA_COMPOSITE_EDGE_SOFTNESS", "55") or "55"))
)
COMPOSITE_DESPILL_MARGIN = min(
    255, max(0, int(os.getenv("LISA_COMPOSITE_DESPILL_MARGIN", "12") or "12"))
)
COMPOSITE_DESPILL_STRENGTH = min(
    1.0, max(0.0, float(os.getenv("LISA_COMPOSITE_DESPILL_STRENGTH", "0.85") or "0.85"))
)
COMPOSITE_MATTE_ERODE_PX = min(
    4, max(0, int(os.getenv("LISA_COMPOSITE_MATTE_ERODE_PX", "0") or "0"))
)
VIDEO_SEND_MAX_FRAMES_PER_DELTA = max(
    1, int(os.getenv("LISA_VIDEO_SEND_MAX_FRAMES_PER_DELTA", "2") or "2")
)

# Greeting-gate: when enabled, delay Lisa's first response.create until the
# bot signals {"type":"call_established"} over the WS. Default off → no
# behavior change. The bot emits this signal when Teams Media transitions
# the call to CallState.Established (see SpeechService.NotifyCallEstablishedAsync).
WAIT_FOR_CALL_ESTABLISHED = os.getenv(
    "LISA_WAIT_FOR_CALL_ESTABLISHED", "0"
).lower() in ("1", "true", "yes")
CALL_ESTABLISHED_TIMEOUT_S = float(
    os.getenv("LISA_CALL_ESTABLISHED_TIMEOUT_S", "60")
)
TTS_PROBE_ENABLED = _env_enabled("LISA_VL_TTS_PROBE")
AUDIO_PIPELINE_DIAG = _env_enabled("LISA_AUDIO_PIPELINE_DIAG")
LATENCY_DIAG = _env_enabled("LISA_LATENCY_DIAG")
LATENCY_OUTPUT_AUDIO_DIAG_CHUNKS = max(
    0, int(os.getenv("LISA_LATENCY_OUTPUT_AUDIO_DIAG_CHUNKS", "8") or "8")
)
LATENCY_NON_SILENT_PEAK = max(
    0, int(os.getenv("LISA_LATENCY_NON_SILENT_PEAK", "200") or "200")
)
LATENCY_NON_SILENT_RMS = max(
    0, int(os.getenv("LISA_LATENCY_NON_SILENT_RMS", "40") or "40")
)
TRIM_LEADING_AUDIO_SILENCE = _env_enabled("LISA_TRIM_LEADING_AUDIO_SILENCE")
LEADING_AUDIO_PREROLL_MS = min(
    200, max(0, int(os.getenv("LISA_LEADING_AUDIO_PREROLL_MS", "60") or "60"))
)
LEADING_AUDIO_TRIM_MAX_HOLD_MS = max(
    0, int(os.getenv("LISA_LEADING_AUDIO_TRIM_MAX_HOLD_MS", "3000") or "3000")
)
RESPONSE_SHAPE_DIAG = _env_enabled(
    "LISA_RESPONSE_SHAPE_DIAG", "1" if AUDIO_PIPELINE_DIAG else "0"
)
RESPONSE_SHAPE_TIMEOUT_S = max(
    1.0, float(os.getenv("LISA_RESPONSE_SHAPE_TIMEOUT_S", "5") or "5")
)
DOTNET_UNIX_EPOCH_TICKS = 621355968000000000


def _dotnet_utc_ticks() -> int:
    return DOTNET_UNIX_EPOCH_TICKS + (time.time_ns() // 100)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _elapsed_ms(start: Optional[float], now: Optional[float] = None) -> Optional[int]:
    if start is None:
        return None
    if now is None:
        now = time.perf_counter()
    return int((now - start) * 1000)


def _pcm16_energy(pcm: bytes) -> tuple[int, int, int, int]:
    usable_len = len(pcm) - (len(pcm) % 2)
    if usable_len <= 0:
        return 0, 0, 0, 0
    try:
        samples = memoryview(pcm[:usable_len]).cast("h")
        sum_sq = 0
        peak = 0
        nonzero = 0
        for sample in samples:
            sample_sq = sample * sample
            sum_sq += sample_sq
            abs_sample = -sample if sample < 0 else sample
            if abs_sample > peak:
                peak = abs_sample
            if sample != 0:
                nonzero += 1
        sample_count = len(samples)
        rms = int((sum_sq / sample_count) ** 0.5) if sample_count else 0
        return rms, peak, nonzero, sample_count
    except Exception:
        return 0, 0, 0, 0


def _pcm16_duration_ms(pcm: bytes, sample_rate: int = 16000) -> int:
    if sample_rate <= 0:
        return 0
    samples = max(0, len(pcm)) // 2
    return int(round((samples / sample_rate) * 1000))

# ── Phase 2: PTS propagation (lipsync) ───────────────────────────────────────
# Per design doc Phase 2 v1.1 (LOCKED 2026-05-06). When enabled, emit
# stream-relative PTS on every audio/video JSON message so the bot can
# anchor a single wall-clock origin (on first audio buffer per Z4) and
# convert subsequent buffers' PTS into Skype Bots Media SDK timestamps.
#
# DEVIATION FROM DOC §4 (Approach A with mpegts): mpegts cannot carry raw
# NV12 video or raw PCM audio (only encoded elementary streams). We extract
# video PTS in-process via a minimal fMP4 moof parser running on the SAME
# bytes already fed to the existing ffmpeg subprocess, paired with decoder
# output frames 1:1 by index. Zero new dependencies, zero ffmpeg subprocess
# change — preserves the AAC dither hack and subprocess crash isolation.
# Audio is self-clocked at the PCM output rate (1/16000 time-base, +320
# samples per 640-byte chunk) since PCM is fixed-rate by definition; we
# don't need to extract audio PTS from fMP4 to achieve this.
#
# Default: 0 (no behavior change). Flip to 1 only after Phase 1 production
# validation. Bot-side LISA_USE_PTS controls whether the bot consumes the
# emitted fields; sending them to a bot with LISA_USE_PTS=0 is a no-op.
USE_PTS = os.getenv("LISA_USE_PTS", "0").lower() in ("1", "true", "yes")

# Where to persist conversation transcripts (one JSONL line per turn).
# Defaults to the same logs directory NSSM writes to so `Get-Content` /
# `az vmss run-command` can pull them with the rest of the diagnostics.
TRANSCRIPT_DIR = os.getenv(
    "LISA_TRANSCRIPT_DIR",
    r"C:\ProgramData\lisa\logs\transcripts" if os.name == "nt" else "./transcripts",
)


def _safe_cid(cid: str) -> str:
    """Sanitize a Voice Live conversation_id for use in a filename."""
    if not cid:
        return ""
    keep = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in cid)
    return keep[:24]


def _write_transcript(
    role: str,
    text: str,
    conversation_id: str = "",
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    """Append one transcript line to a per-conversation JSONL file.

    Each Voice Live ``conversation_id`` lands in its own file so that one
    Teams call \u2192 one transcript download. Falls back to a per-day file
    when no conversation_id is known yet (e.g. very first event before
    the session is fully established).

    Best-effort: any I/O error is logged at WARNING and swallowed so a
    transcript-write failure can never break the audio pipeline.
    """
    text = (text or "").strip()
    if not text:
        return
    try:
        os.makedirs(TRANSCRIPT_DIR, exist_ok=True)
        ts = time.strftime("%Y-%m-%d", time.gmtime())
        cid_safe = _safe_cid(conversation_id)
        candidate_safe = _safe_cid(str((metadata or {}).get("candidate_id") or ""))
        if candidate_safe and cid_safe:
            path = os.path.join(TRANSCRIPT_DIR, f"lisa-{ts}-{candidate_safe}-{cid_safe}.jsonl")
        elif candidate_safe:
            path = os.path.join(TRANSCRIPT_DIR, f"lisa-{ts}-{candidate_safe}.jsonl")
        elif cid_safe:
            path = os.path.join(TRANSCRIPT_DIR, f"lisa-{ts}-{cid_safe}.jsonl")
        else:
            path = os.path.join(TRANSCRIPT_DIR, f"lisa-{ts}.jsonl")
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "role": role,
            "conversation_id": conversation_id or "",
            "text": text,
        }
        for key in ("candidate_id", "candidate_name", "position", "session_id"):
            value = str((metadata or {}).get(key) or "").strip()
            if value:
                row[key] = value
        line = json.dumps(row, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.info(f"[transcript] {role}: {text[:200]}")
    except Exception as e:
        logger.warning(f"[transcript] write failed: {e}")


# ── Initial greeting and inline fallback instructions ───────────────────────
LISA_INITIAL_CONSENT_PROMPT = (
    "Hallo, schön, dass Sie da sind! Ich bin Lisa, eine KI-gestützte "
    "Recruiting-Assistentin von Company X HR. Bevor wir beginnen: Dieses Gespräch ist "
    "ein KI-gestütztes Screening-Interview. Sind Sie damit einverstanden, das "
    "Gespräch mit einer KI-Assistentin fortzusetzen?"
)


# Inline fallback instructions are only used if agent mode is not configured.
LISA_FALLBACK_INSTRUCTIONS = """\
You are Lisa, an AI recruiting assistant for Company X HR, speaking with the warmth
and judgment of an experienced HR recruiter. You are conducting a short
first-screening voice call with a candidate for a retail position.

Your job is to gather a few key facts for the hiring team. You do not make
hiring decisions, evaluate the candidate, or explain the role in detail.
Be transparent that the candidate is speaking with AI.

# Pronunciation
Always pronounce "Company X" the German way: "REH-veh" (IPA: ˈʁeːvə).
Never pronounce it as "rev" or "ree-wee".

# Language
- The conversation starts in German.
- Begin with a short German Responsible AI disclosure and consent prompt.
- Do not ask the first screening question until the candidate clearly agrees to
    continue the AI screening interview.
- If the candidate asks to switch to English, immediately agree and switch.
- After switching to English, conduct the rest of the screening only in English.
- Do not switch back to German.
- If the candidate continues in German after the switch, politely continue in
  English.

# Voice and conversation style
- Sound warm, calm, friendly, and unhurried.
- Keep every reply short: one or two spoken sentences.
- Ask one question at a time.
- Use brief neutral acknowledgements like "Got it, thanks!", "Perfect.", or
  "Makes sense."
- Do not restate, paraphrase, or summarize the candidate's answers.
- Do not read lists, markdown, asterisks, headings, or section names aloud.

# Listening behavior
- Wait until the candidate has clearly finished speaking before replying.
- Treat short pauses as thinking time.
- Wait at least 1.5 seconds of clear silence before responding.
- If you accidentally speak over the candidate, stop and let them finish.
- If an answer is unclear, ask one short clarification at most. If the meaning
  is likely clear, accept it and move on.

# Hard rules
- At the start of every call, before any screening question, disclose that this
    is an AI screening interview with Lisa, an AI recruiting assistant from Company X
    HR, and ask whether the candidate is comfortable continuing with an AI
    interviewer. If they decline or seem uncomfortable, politely stop and say an
    HR colleague will follow up. This consent check does not count as a screening
    question.
- Never ask about age, gender, ethnicity, religion, disability, sexual
  orientation, or nationality.
- Do not ask about work authorization in this screening.
- Do not ask for the candidate's name unless it is already provided in context.
- Never tell the candidate what the role requires.
- Do not explain shifts, salary ranges, or detailed job requirements.
- If asked about the role, give one generic sentence only:
  "It's a customer-facing role in one of our stores."
  Then return to the next screening question.
- Do not ask follow-up "why" questions.
- Do not pressure the candidate for more detail.
- Track answers internally. Once a topic has been answered, never ask it again.

# Screening flow
Start in German with a short greeting, Responsible AI disclosure, and consent
question.

Example German opening:
"Hallo, schön, dass Sie da sind! Ich bin Lisa, eine KI-gestützte
Recruiting-Assistentin von Company X HR. Bevor wir beginnen: Dieses Gespräch ist
ein KI-gestütztes Screening-Interview. Sind Sie damit einverstanden, das
Gespräch mit einer KI-Assistentin fortzusetzen?"

Wait for the candidate to agree before asking the first screening question. If
they do not agree, say politely:
"Kein Problem, dann beenden wir das Gespräch hier. Eine Kollegin oder ein
Kollege aus HR wird sich mit den nächsten Schritten bei Ihnen melden."
Then stop the screening.

If the candidate asks to switch to English, say:
"Of course — we can continue in English."

Then continue the screening in English.

After switching to English, ask at most three question turns, in this order:

1. Motivation:
   Ask what interested them in this position and in Company X.

2. Start date:
   Ask when they could start, ideally a specific day or month.

3. Availability:
   Ask how many contracted hours per week they would like, and which specific
   days or time blocks they cannot work.

If the candidate answers multiple topics at once, do not re-ask covered topics.
Move to the next missing topic.

After all three topics are covered, or after three question turns have been
used, close the call.

# Closing
Close warmly in English. Do not summarize or evaluate the candidate.

Use this closing style:
"That's everything I needed for now — thank you so much for taking the time
today! My HR colleague will review your profile and reach out with the next
steps by the end of the week. Have a great day!"

If you know the candidate's name, use it once in the closing. If not, close
without using a name.
"""

def _agent_mode_configured() -> bool:
    return bool(LISA_AGENT_NAME and LISA_PROJECT_NAME)


def _build_agent_config() -> Optional[AgentSessionConfig]:
    if not _agent_mode_configured():
        return None
    cfg: AgentSessionConfig = {
        "agent_name": LISA_AGENT_NAME,
        "project_name": LISA_PROJECT_NAME,
    }
    if LISA_AGENT_VERSION:
        cfg["agent_version"] = LISA_AGENT_VERSION
    return cfg


def _resolve_endpoint(endpoint: str, agent_mode: bool) -> str:
    """Rewrite cognitiveservices.azure.com → services.ai.azure.com in agent mode.

    The Voice Live SDK sends the WSS handshake to exactly the host you give
    it. In Foundry agent mode the service only accepts the
    ``services.ai.azure.com`` host; the legacy Cognitive Services host
    returns 400 on the handshake.
    """
    if agent_mode and ".cognitiveservices.azure.com" in endpoint:
        endpoint = endpoint.replace(
            ".cognitiveservices.azure.com", ".services.ai.azure.com"
        )
    return endpoint


def _build_credential():
    """Tenant-pinned credential.

    Order of preference:
    1. API key (if AZURE_VOICELIVE_API_KEY set).
    2. ManagedIdentityCredential — preferred when running on an Azure VM /
       VMSS / Container instance. Honors AZURE_CLIENT_ID for user-assigned MI.
    3. AzureCliCredential pinned to AZURE_TENANT_ID (dev boxes).
    4. DefaultAzureCredential fallback.

    DefaultAzureCredential's AzureCliCredential leg has no tenant kwarg, so on
    multi-tenant dev boxes it can silently return a token from the wrong
    tenant → Foundry rejects with "Tenant provided in token does not match
    resource token". Pin via AzureCliCredential(tenant_id=...) when set.
    """
    if AZURE_API_KEY:
        logger.info("Auth: API key")
        return AzureKeyCredential(AZURE_API_KEY)

    # On Azure VM/VMSS, prefer the system/user-assigned managed identity.
    # Detect via IMDS env hint or the explicit USE_MANAGED_IDENTITY toggle.
    use_mi = os.getenv("USE_MANAGED_IDENTITY", "").lower() in ("1", "true", "yes")
    if not use_mi:
        # Heuristic: Azure VMs always have these populated.
        use_mi = bool(os.getenv("IDENTITY_ENDPOINT")) or os.path.exists(
            r"C:\Packages\Plugins\Microsoft.ManagedIdentity.ManagedIdentityExtensionForWindows"
        )
    if use_mi:
        client_id = os.getenv("AZURE_CLIENT_ID") or None
        logger.info(
            "Auth: ManagedIdentityCredential (client_id=%s)", client_id or "system"
        )
        return ManagedIdentityCredential(client_id=client_id)

    if AZURE_TENANT_ID:
        logger.info("Auth: AzureCliCredential pinned to tenant %s", AZURE_TENANT_ID)
        return AzureCliCredential(tenant_id=AZURE_TENANT_ID)
    logger.info("Auth: DefaultAzureCredential (no tenant pinning)")
    return DefaultAzureCredential()


# ── FastAPI app ─────────────────────────────────────────────────────────────
app = FastAPI(title="Lisa Avatar Sidecar")


# ── Helpers ─────────────────────────────────────────────────────────────────


try:
    from scipy.signal import resample_poly as _scipy_resample_poly  # type: ignore
    _HAS_SCIPY = True
except Exception:  # pragma: no cover - fallback path
    _HAS_SCIPY = False
    logger.warning(
        "scipy not available; falling back to linear-interp resampler "
        "(robotic timbre). Install scipy for high-quality 24k\u219216k."
    )


def resample_pcm16(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample PCM16 mono audio.

    Uses scipy.signal.resample_poly (polyphase FIR with Kaiser window) when
    available \u2014 this is what the user actually hears on the Teams call,
    and linear interpolation here is the dominant source of \u201crobotic\u201d
    timbre because it does not low-pass below the new Nyquist (aliasing
    above ~8 kHz when going 24 kHz \u2192 16 kHz).
    """
    if from_rate == to_rate or len(data) < 4:
        return data
    samples = np.frombuffer(data, dtype=np.int16)
    if _HAS_SCIPY:
        from math import gcd
        g = gcd(int(from_rate), int(to_rate))
        up = int(to_rate) // g
        down = int(from_rate) // g
        # padtype='line' (scipy>=1.5) extends each chunk with linear
        # extrapolation instead of zeros, which suppresses click/buzz at
        # chunk boundaries when resampling streamed audio chunks.
        try:
            out = _scipy_resample_poly(
                samples.astype(np.float32), up, down, padtype="line"
            )
        except TypeError:
            out = _scipy_resample_poly(samples.astype(np.float32), up, down)
    else:
        num_out = int(len(samples) * to_rate / from_rate)
        indices = np.linspace(0, len(samples) - 1, num_out)
        out = np.interp(indices, np.arange(len(samples)), samples.astype(np.float64))
    return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


async def _delayed_ws_send(ws: WebSocket, payload: str, delay_s: float) -> None:
    """Sleep `delay_s` seconds then forward `payload` on `ws`. Best-effort.

    Used to push avatar video frames slightly later so they align with
    the audio jitter buffer in the Teams Media Platform (audio currently
    lags lips on the receiving end). Tasks fire in order because they
    are scheduled in order with the same delay; asyncio's queue is FIFO
    for ready tasks.
    """
    try:
        await asyncio.sleep(delay_s)
        await ws.send_text(payload)
    except Exception:
        # The connection may have closed during the delay; that's fine.
        pass


def _clear_queue(q: queue.Queue) -> int:
    cleared = 0
    while True:
        try:
            q.get_nowait()
            cleared += 1
        except queue.Empty:
            return cleared


def _process_memory_snapshot() -> str:
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(ProcessMemoryCounters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if ok:
                return (
                    f"rss_mb={counters.WorkingSetSize / (1024 * 1024):.1f} "
                    f"private_mb={counters.PagefileUsage / (1024 * 1024):.1f}"
                )
        else:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss_kb = usage.ru_maxrss
            if rss_kb > 10 * 1024 * 1024:
                rss_kb = rss_kb / 1024
            return f"rss_mb={rss_kb / 1024:.1f}"
    except Exception as exc:
        return f"unavailable:{type(exc).__name__}"
    return "unavailable"


# ── Chroma-key background compositing (NV12/YUV) ────────────────────────────


def _parse_hex_rgb(color: str) -> tuple[int, int, int]:
    raw = (color or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if len(raw) not in (6, 8):
        raise ValueError(f"Expected #RRGGBB or #RRGGBBAA color, got {color!r}")
    try:
        value = int(raw, 16)
    except ValueError as exc:
        raise ValueError(f"Expected #RRGGBB or #RRGGBBAA color, got {color!r}") from exc
    if len(raw) == 8:
        value >>= 8
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def _normalize_voicelive_background_color(color: str) -> str:
    raw = (color or "").strip()
    if not raw:
        return ""
    prefix = "#" if raw.startswith("#") else ""
    digits = raw[1:] if prefix else raw
    if len(digits) == 6:
        digits = digits + "FF"
    if len(digits) != 8:
        raise ValueError(f"Expected #RRGGBB or #RRGGBBAA color, got {color!r}")
    int(digits, 16)
    return "#" + digits.upper()


def _rgb_to_yuv_values(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    red, green, blue = (float(v) for v in rgb)
    y = 16.0 + (0.256788 * red) + (0.504129 * green) + (0.097906 * blue)
    u = 128.0 - (0.148223 * red) - (0.290993 * green) + (0.439216 * blue)
    v = 128.0 + (0.439216 * red) - (0.367788 * green) - (0.071427 * blue)
    return y, u, v


def _nv12_to_rgb(frame: bytes, width: int, height: int) -> np.ndarray:
    expected = width * height * 3 // 2
    if len(frame) != expected:
        raise ValueError(f"NV12 frame length={len(frame)} expected={expected}")
    if width % 2 or height % 2:
        raise ValueError(f"NV12 dimensions must be even, got {width}x{height}")

    raw = np.frombuffer(frame, dtype=np.uint8)
    y_plane = raw[: width * height].reshape((height, width)).astype(np.float32)
    uv = raw[width * height :].reshape((height // 2, width // 2, 2))
    u_plane = uv[:, :, 0].repeat(2, axis=0).repeat(2, axis=1).astype(np.float32)
    v_plane = uv[:, :, 1].repeat(2, axis=0).repeat(2, axis=1).astype(np.float32)

    c = y_plane - 16.0
    d = u_plane - 128.0
    e = v_plane - 128.0
    rgb = np.empty((height, width, 3), dtype=np.float32)
    rgb[:, :, 0] = 1.164383 * c + 1.596027 * e
    rgb[:, :, 1] = 1.164383 * c - 0.391762 * d - 0.812968 * e
    rgb[:, :, 2] = 1.164383 * c + 2.017232 * d
    return np.clip(rgb, 0, 255).astype(np.uint8)


def _rgb_to_nv12(rgb: np.ndarray) -> bytes:
    y_plane, uv = _rgb_to_nv12_planes(rgb)
    return y_plane.tobytes() + uv.tobytes()


def _rgb_to_nv12_planes(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError(f"Expected RGB array shaped HxWx3, got {rgb.shape}")
    height, width, _ = rgb.shape
    if width % 2 or height % 2:
        raise ValueError(f"NV12 dimensions must be even, got {width}x{height}")

    rgb_f = rgb.astype(np.float32)
    red = rgb_f[:, :, 0]
    green = rgb_f[:, :, 1]
    blue = rgb_f[:, :, 2]
    y = 16.0 + (0.256788 * red) + (0.504129 * green) + (0.097906 * blue)
    u = 128.0 - (0.148223 * red) - (0.290993 * green) + (0.439216 * blue)
    v = 128.0 + (0.439216 * red) - (0.367788 * green) - (0.071427 * blue)

    y_plane = np.clip(np.rint(y), 0, 255).astype(np.uint8)
    u_sub = u.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))
    v_sub = v.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))
    uv = np.empty((height // 2, width // 2, 2), dtype=np.uint8)
    uv[:, :, 0] = np.clip(np.rint(u_sub), 0, 255).astype(np.uint8)
    uv[:, :, 1] = np.clip(np.rint(v_sub), 0, 255).astype(np.uint8)
    return y_plane, uv


def _resize_cover_rgb_nearest(rgb: np.ndarray, width: int, height: int) -> np.ndarray:
    source_height, source_width, _ = rgb.shape
    scale = max(width / source_width, height / source_height)
    scaled_width = max(1, int(round(source_width * scale)))
    scaled_height = max(1, int(round(source_height * scale)))
    x_idx = np.minimum((np.arange(scaled_width) / scale).astype(np.int64), source_width - 1)
    y_idx = np.minimum((np.arange(scaled_height) / scale).astype(np.int64), source_height - 1)
    scaled = rgb[y_idx[:, None], x_idx[None, :], :]
    x0 = max(0, (scaled_width - width) // 2)
    y0 = max(0, (scaled_height - height) // 2)
    return scaled[y0 : y0 + height, x0 : x0 + width, :].copy()


class ChromaKeyBackgroundCompositor:
    def __init__(
        self,
        *,
        enabled: bool,
        image_path: str,
        chroma_color: str,
        tolerance: int,
        green_min: int = 120,
        green_margin: int = 35,
        edge_softness: int = 55,
        despill_margin: int = 12,
        despill_strength: float = 0.85,
        matte_erode_px: int = 0,
        background_rgb: Optional[np.ndarray] = None,
    ):
        self.enabled = enabled
        self.image_path = image_path
        self.chroma_color = _normalize_voicelive_background_color(chroma_color)
        self.chroma_rgb_tuple = _parse_hex_rgb(self.chroma_color)
        self.chroma_rgb = np.array(self.chroma_rgb_tuple, dtype=np.float32)
        self.chroma_yuv = np.array(_rgb_to_yuv_values(self.chroma_rgb_tuple), dtype=np.float32)
        self.tolerance = max(1, int(tolerance))
        self.green_min = min(255, max(0, int(green_min)))
        self.green_margin = min(255, max(1, int(green_margin)))
        self.edge_softness = min(255, max(1, int(edge_softness)))
        self.despill_margin = min(255, max(0, int(despill_margin)))
        self.despill_strength = min(1.0, max(0.0, float(despill_strength)))
        self.matte_erode_px = min(4, max(0, int(matte_erode_px)))
        self._source_background_rgb = (
            background_rgb.astype(np.uint8).copy() if background_rgb is not None else None
        )
        self._background_cache: dict[tuple[int, int], np.ndarray] = {}
        self._background_nv12_cache: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
        self._logged_ready = False
        self._logged_no_chroma = False
        self.frames_processed = 0
        self.frames_composited = 0
        self.frames_bypassed_no_chroma = 0
        self.frames_failed = 0
        self.last_frame_size: Optional[tuple[int, int]] = None
        self.last_chroma_ratio: Optional[float] = None
        self.last_output_chroma_ratio: Optional[float] = None
        self.last_composited: Optional[bool] = None
        self.last_error: Optional[str] = None

    def _load_background_rgb_with_ffmpeg(self, width: int, height: int) -> np.ndarray:
        if not self.image_path:
            raise ValueError("LISA_COMPOSITE_BACKGROUND_IMAGE is required")
        vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
        proc = subprocess.run(
            [
                "ffmpeg",
                "-loglevel", "error",
                "-i", self.image_path,
                "-vf", vf,
                "-frames:v", "1",
                "-f", "rawvideo",
                "-pix_fmt", "rgb24",
                "pipe:1",
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        expected = width * height * 3
        if len(proc.stdout) != expected:
            raise ValueError(
                f"Background decode bytes={len(proc.stdout)} expected={expected}"
            )
        return np.frombuffer(proc.stdout, dtype=np.uint8).reshape((height, width, 3)).copy()

    def _background_rgb(self, width: int, height: int) -> np.ndarray:
        key = (width, height)
        cached = self._background_cache.get(key)
        if cached is not None:
            return cached
        if self._source_background_rgb is not None:
            background = _resize_cover_rgb_nearest(self._source_background_rgb, width, height)
        else:
            background = self._load_background_rgb_with_ffmpeg(width, height)
        self._background_cache[key] = background
        if not self._logged_ready:
            self._logged_ready = True
            logger.info(
                "Background compositor ready image=%s size=%sx%s chroma=%s tolerance=%s green_min=%s green_margin=%s edge_softness=%s despill_margin=%s despill_strength=%.2f matte_erode_px=%s",
                self.image_path or "<in-memory>",
                width,
                height,
                self.chroma_color,
                self.tolerance,
                self.green_min,
                self.green_margin,
                self.edge_softness,
                self.despill_margin,
                self.despill_strength,
                self.matte_erode_px,
            )
        return background

    def _erode_alpha(self, alpha: np.ndarray) -> np.ndarray:
        radius = self.matte_erode_px
        if radius <= 0:
            return alpha
        padded = np.pad(alpha, radius, mode="edge")
        eroded = alpha.copy()
        height, width = alpha.shape
        for y_offset in range(radius * 2 + 1):
            for x_offset in range(radius * 2 + 1):
                eroded = np.minimum(
                    eroded,
                    padded[y_offset : y_offset + height, x_offset : x_offset + width],
                )
        return eroded

    def _background_nv12(self, width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
        key = (width, height)
        cached = self._background_nv12_cache.get(key)
        if cached is not None:
            return cached
        background = self._background_rgb(width, height)
        planes = _rgb_to_nv12_planes(background)
        self._background_nv12_cache[key] = planes
        return planes

    def ensure_loaded(self, width: int, height: int) -> None:
        self._background_rgb(width, height)

    def record_failure(self, exc: BaseException) -> None:
        self.frames_failed += 1
        self.last_error = f"{type(exc).__name__}: {exc}"

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "image_path": self.image_path,
            "background_loaded": bool(self._background_cache),
            "edge_softness": self.edge_softness,
            "despill_margin": self.despill_margin,
            "despill_strength": self.despill_strength,
            "matte_erode_px": self.matte_erode_px,
            "frames_processed": self.frames_processed,
            "frames_composited": self.frames_composited,
            "frames_bypassed_no_chroma": self.frames_bypassed_no_chroma,
            "frames_failed": self.frames_failed,
            "last_frame_size": (
                f"{self.last_frame_size[0]}x{self.last_frame_size[1]}"
                if self.last_frame_size is not None
                else None
            ),
            "last_chroma_ratio": self.last_chroma_ratio,
            "last_output_chroma_ratio": self.last_output_chroma_ratio,
            "last_composited": self.last_composited,
            "last_error": self.last_error,
        }

    def composite_nv12(self, frame: bytes, width: int, height: int) -> bytes:
        if not self.enabled:
            return frame
        expected = width * height * 3 // 2
        if len(frame) != expected:
            raise ValueError(f"NV12 frame length={len(frame)} expected={expected}")
        if width % 2 or height % 2:
            raise ValueError(f"NV12 dimensions must be even, got {width}x{height}")

        raw = np.frombuffer(frame, dtype=np.uint8)
        y_plane = raw[: width * height].reshape((height, width)).astype(np.float32)
        uv = raw[width * height :].reshape((height // 2, width // 2, 2)).astype(np.float32)
        u_plane = uv[:, :, 0].repeat(2, axis=0).repeat(2, axis=1)
        v_plane = uv[:, :, 1].repeat(2, axis=0).repeat(2, axis=1)

        yuv_luma = y_plane - 16.0
        yuv_u = u_plane - 128.0
        yuv_v = v_plane - 128.0
        red_plane = np.clip(1.164383 * yuv_luma + 1.596027 * yuv_v, 0, 255)
        green_plane = np.clip(
            1.164383 * yuv_luma - 0.391762 * yuv_u - 0.812968 * yuv_v,
            0,
            255,
        )
        blue_plane = np.clip(1.164383 * yuv_luma + 2.017232 * yuv_u, 0, 255)

        target_red, target_green, target_blue = self.chroma_rgb
        green_dominant = (
            (green_plane >= float(self.green_min))
            & ((green_plane - red_plane) >= float(self.green_margin))
            & ((green_plane - blue_plane) >= float(self.green_margin))
        )

        green_excess = green_plane - np.maximum(red_plane, blue_plane)
        target_y, target_u, target_v = self.chroma_yuv
        distance_sq = (
            (red_plane - target_red) ** 2
            + (green_plane - target_green) ** 2
            + (blue_plane - target_blue) ** 2
        )
        hard = float(self.tolerance)
        soft = float(self.tolerance + self.green_margin)
        key_alpha = np.clip(
            (distance_sq - (hard * hard)) / ((soft * soft) - (hard * hard)),
            0.0,
            1.0,
        )
        dominance_alpha = np.clip(
            (float(self.green_margin + self.edge_softness) - green_excess)
            / float(self.edge_softness),
            0.0,
            1.0,
        )
        alpha = np.where(green_dominant, np.minimum(key_alpha, dominance_alpha), 1.0)
        chroma_pixels = int(np.count_nonzero(alpha < 0.5))
        self.last_chroma_ratio = chroma_pixels / float(width * height)
        self.last_frame_size = (width, height)

        self.frames_processed += 1
        if chroma_pixels == 0:
            self.frames_bypassed_no_chroma += 1
            self.last_output_chroma_ratio = self.last_chroma_ratio
            self.last_composited = False
            if not self._logged_no_chroma or self.frames_processed % 250 == 0:
                self._logged_no_chroma = True
                logger.warning(
                    "Background compositor found no chroma pixels; returning original frame "
                    "frames=%s chroma=%s tolerance=%s. Check that Voice Live received "
                    "an opaque #RRGGBBAA background color.",
                    self.frames_processed,
                    self.chroma_color,
                    self.tolerance,
                )
            return frame

        alpha = self._erode_alpha(alpha)
        if self.matte_erode_px > 0:
            chroma_pixels = int(np.count_nonzero(alpha < 0.5))
            self.last_chroma_ratio = chroma_pixels / float(width * height)

        spill_mask = (
            (alpha > 0.05)
            & (green_plane >= float(max(1, self.green_min - 40)))
            & (green_excess > float(self.despill_margin))
        )
        if self.despill_strength > 0.0 and np.any(spill_mask):
            green_limit = np.maximum(red_plane, blue_plane) + float(self.despill_margin)
            despilled_green = np.where(
                spill_mask,
                green_plane - ((green_plane - green_limit) * float(self.despill_strength)),
                green_plane,
            )
            despilled_green = np.minimum(green_plane, np.clip(despilled_green, 0, 255))
        else:
            despilled_green = green_plane

        source_y = (
            16.0
            + (0.256788 * red_plane)
            + (0.504129 * despilled_green)
            + (0.097906 * blue_plane)
        )
        source_u = (
            128.0
            - (0.148223 * red_plane)
            - (0.290993 * despilled_green)
            + (0.439216 * blue_plane)
        )
        source_v = (
            128.0
            + (0.439216 * red_plane)
            - (0.367788 * despilled_green)
            - (0.071427 * blue_plane)
        )
        source_uv = np.empty((height // 2, width // 2, 2), dtype=np.float32)
        source_uv[:, :, 0] = source_u.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))
        source_uv[:, :, 1] = source_v.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))

        background_y, background_uv = self._background_nv12(width, height)
        composed_y = (source_y * alpha) + (background_y.astype(np.float32) * (1.0 - alpha))
        alpha_uv = alpha.reshape(height // 2, 2, width // 2, 2).mean(axis=(1, 3))
        composed_uv = (source_uv * alpha_uv[:, :, None]) + (
            background_uv.astype(np.float32) * (1.0 - alpha_uv[:, :, None])
        )
        output_y = np.clip(np.rint(composed_y), 0, 255).astype(np.uint8)
        output_uv = np.clip(np.rint(composed_uv), 0, 255).astype(np.uint8)

        if self.frames_processed == 1 or self.frames_processed % 50 == 0:
            output_u = output_uv[:, :, 0].repeat(2, axis=0).repeat(2, axis=1).astype(np.float32)
            output_v = output_uv[:, :, 1].repeat(2, axis=0).repeat(2, axis=1).astype(np.float32)
            output_distance_sq = (
                ((output_y.astype(np.float32) - target_y) * 0.75) ** 2
                + (output_u - target_u) ** 2
                + (output_v - target_v) ** 2
            )
            self.last_output_chroma_ratio = int(np.count_nonzero(output_distance_sq < (hard * hard))) / float(width * height)
        else:
            self.last_output_chroma_ratio = 0.0
        output = output_y.tobytes() + output_uv.tobytes()
        self.frames_composited += 1
        self.last_composited = True
        if self.frames_processed == 1 or self.frames_processed % 50 == 0:
            logger.info(
                "Background compositor processed frames=%s composited=%s chroma_ratio=%.4f output_chroma_ratio=%.4f size=%sx%s",
                self.frames_processed,
                self.frames_composited,
                self.last_chroma_ratio,
                self.last_output_chroma_ratio,
                width,
                height,
            )
        return output


_background_compositor: Optional[ChromaKeyBackgroundCompositor] = None
_background_compositor_config_warned = False


def _get_background_compositor() -> Optional[ChromaKeyBackgroundCompositor]:
    global _background_compositor, _background_compositor_config_warned
    if not COMPOSITE_BACKGROUND_ENABLED:
        return None
    if not COMPOSITE_BACKGROUND_IMAGE:
        if not _background_compositor_config_warned:
            _background_compositor_config_warned = True
            logger.warning(
                "Background compositing enabled but LISA_COMPOSITE_BACKGROUND_IMAGE is empty; compositing disabled"
            )
        return None
    if _background_compositor is None:
        _background_compositor = ChromaKeyBackgroundCompositor(
            enabled=True,
            image_path=COMPOSITE_BACKGROUND_IMAGE,
            chroma_color=COMPOSITE_CHROMA_COLOR,
            tolerance=COMPOSITE_CHROMA_TOLERANCE,
            green_min=COMPOSITE_GREEN_MIN,
            green_margin=COMPOSITE_GREEN_MARGIN,
            edge_softness=COMPOSITE_EDGE_SOFTNESS,
            despill_margin=COMPOSITE_DESPILL_MARGIN,
            despill_strength=COMPOSITE_DESPILL_STRENGTH,
            matte_erode_px=COMPOSITE_MATTE_ERODE_PX,
        )
    return _background_compositor


def _background_compositor_health(load_background: bool = False) -> dict[str, Any]:
    state: dict[str, Any] = {
        "enabled": COMPOSITE_BACKGROUND_ENABLED,
        "image_configured": bool(COMPOSITE_BACKGROUND_IMAGE),
        "chroma_color": COMPOSITE_CHROMA_COLOR,
        "tolerance": COMPOSITE_CHROMA_TOLERANCE,
        "green_min": COMPOSITE_GREEN_MIN,
        "green_margin": COMPOSITE_GREEN_MARGIN,
        "edge_softness": COMPOSITE_EDGE_SOFTNESS,
        "despill_margin": COMPOSITE_DESPILL_MARGIN,
        "despill_strength": COMPOSITE_DESPILL_STRENGTH,
        "matte_erode_px": COMPOSITE_MATTE_ERODE_PX,
        "background_loaded": False,
        "frames_processed": 0,
        "frames_failed": 0,
    }
    compositor = _get_background_compositor()
    if compositor is None:
        return state
    if load_background:
        try:
            compositor.ensure_loaded(VIDEO_WIDTH, VIDEO_HEIGHT)
        except Exception as exc:
            compositor.record_failure(exc)
            logger.exception("Background compositor image load failed")
    state.update(compositor.stats())
    return state


# ── FFmpeg video decoder (fMP4 H.264 → NV12 raw frames) ────────────────────


class FFmpegDecoder:
    """Decodes fMP4 H.264 video to raw NV12 frames via an FFmpeg subprocess."""

    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        self.frame_size = width * height * 3 // 2
        self.proc: Optional[subprocess.Popen] = None
        self._frame_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=VIDEO_DECODER_QUEUE_MAX_FRAMES
        )
        self._reader: Optional[threading.Thread] = None
        self._reader_failed = False
        self._restart_count = 0
        self._dropped_frames = 0

    def stats(self) -> str:
        pid = self.proc.pid if self.proc else None
        returncode = self.proc.poll() if self.proc else None
        return (
            f"pid={pid} returncode={returncode} "
            f"queue={self._frame_queue.qsize()}/{self._frame_queue.maxsize} "
            f"reader_failed={self._reader_failed} restarts={self._restart_count} "
            f"dropped_frames={self._dropped_frames} frame_size={self.frame_size}"
        )

    def start(self):
        self._reader_failed = False
        # NOTE: dropped `+nobuffer` and `-flags low_delay` from the demuxer
        # args. Voice Live sends fMP4 fragments incrementally; with those
        # flags ffmpeg refuses to wait for a complete `moov` initialization
        # segment, exits with "Invalid data found when processing input",
        # and every subsequent stdin.write raises BrokenPipeError -> we
        # restart the decoder, hit the same error, and loop forever.
        # Without those flags ffmpeg buffers the init seg, demuxes cleanly,
        # and emits NV12 frames as expected. The added latency is on the
        # order of one fMP4 fragment (~30ms), imperceptible at human scales.
        # Also: stderr is routed to DEVNULL — leaving it as PIPE without an
        # active reader thread can deadlock ffmpeg on stderr writes.
        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel", "error",
                "-f", "mp4",
                "-fflags", "+genpts",
                "-i", "pipe:0",
                "-vf", f"fps={VIDEO_FPS}",
                "-f", "rawvideo",
                "-pix_fmt", "nv12",
                "-s", f"{self.width}x{self.height}",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        logger.info(
            "FFmpeg decoder started (%sx%s, NV12, fps=%s, frame_size=%s, queue_max=%s)",
            self.width,
            self.height,
            VIDEO_FPS,
            self.frame_size,
            self._frame_queue.maxsize,
        )

    def _read_loop(self):
        buf = b""
        try:
            while self.proc and self.proc.poll() is None:
                chunk = self.proc.stdout.read(self.frame_size - len(buf))
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= self.frame_size:
                    frame = buf[: self.frame_size]
                    buf = buf[self.frame_size :]
                    try:
                        self._frame_queue.put_nowait(frame)
                    except queue.Full:
                        try:
                            self._frame_queue.get_nowait()
                            self._dropped_frames += 1
                        except queue.Empty:
                            pass
                        self._frame_queue.put_nowait(frame)
        except MemoryError:
            self._reader_failed = True
            cleared = _clear_queue(self._frame_queue)
            logger.exception(
                "FFmpeg video reader MemoryError; cleared_frames=%s state=%s memory=%s",
                cleared,
                self.stats(),
                _process_memory_snapshot(),
            )
        except Exception as e:
            self._reader_failed = True
            logger.error(f"FFmpeg reader error: {e}")

    def restart(self, reason: str) -> None:
        self._restart_count += 1
        logger.warning(
            "Restarting FFmpeg video decoder reason=%s state_before=%s memory=%s",
            reason,
            self.stats(),
            _process_memory_snapshot(),
        )
        self.stop()
        self.start()

    def feed(self, data: bytes) -> list[bytes]:
        if self.proc is None or self.proc.poll() is not None or self._reader_failed:
            if self._reader_failed:
                logger.warning("FFmpeg video reader failed; restarting before feed")
            self.stop()
            self.start()
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except MemoryError:
            logger.exception(
                "FFmpeg video feed MemoryError; state=%s memory=%s",
                self.stats(),
                _process_memory_snapshot(),
            )
            self.restart("video_feed_memory_error")
            return []
        except (BrokenPipeError, OSError):
            logger.warning("FFmpeg pipe broken — restarting decoder")
            self.restart("video_pipe_broken")
            return []

        frames = []
        while len(frames) < VIDEO_DECODER_DRAIN_MAX_FRAMES and not self._frame_queue.empty():
            try:
                frames.append(self._frame_queue.get_nowait())
            except queue.Empty:
                break
        return frames

    def stop(self):
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                if self.proc:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None
        _clear_queue(self._frame_queue)
        self._reader_failed = False


# ── FFmpeg audio decoder (fMP4 AAC → PCM16 16kHz mono) ─────────────────────
#
# In Voice Live avatar mode (output_protocol=websocket, codec=h264) the audio
# is muxed INTO the same fMP4 container as the video. Voice Live does NOT
# send response.audio.delta separately. We need a second ffmpeg process that
# extracts the audio track and outputs raw PCM that the bot can play.


class FFmpegAudioDecoder:
    """Decodes fMP4 AAC audio to raw PCM16 16kHz mono via an FFmpeg subprocess."""

    # 20ms @ 16kHz/16bit/mono = 640 bytes per AudioMediaBuffer chunk.
    CHUNK_SIZE = 640

    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self._chunk_queue: queue.Queue[bytes] = queue.Queue(
            maxsize=AUDIO_DECODER_QUEUE_MAX_CHUNKS
        )
        self._reader: Optional[threading.Thread] = None
        self._reader_failed = False
        self._restart_count = 0
        self._dropped_chunks = 0

    def stats(self) -> str:
        pid = self.proc.pid if self.proc else None
        returncode = self.proc.poll() if self.proc else None
        return (
            f"pid={pid} returncode={returncode} "
            f"queue={self._chunk_queue.qsize()}/{self._chunk_queue.maxsize} "
            f"reader_failed={self._reader_failed} restarts={self._restart_count} "
            f"dropped_chunks={self._dropped_chunks} chunk_size={self.CHUNK_SIZE}"
        )

    def start(self):
        self._reader_failed = False
        # NOTE: We deliberately do NOT pass +discardcorrupt / +nobuffer /
        # low_delay here. Those flags are appropriate for the H264 video
        # decoder (small key-frame gap, drop tolerance) but they corrupt
        # AAC playback: ffmpeg drops AAC frames at segment boundaries and
        # skips decoder warmup (priming samples), producing audibly robotic
        # / glitchy output. For audio we just want a clean continuous decode
        # at the expense of ~50ms extra latency, which is imperceptible vs
        # the avatar video pipeline latency.
        #
        # Audio quality tweak:
        #   -af aresample=dither_method=triangular_hp
        #     Apply triangular high-pass dither on the 32-bit-internal -> 16-bit
        #     quantization step that happens when Voice Live's AAC output
        #     (typically 24 kHz internal) is downsampled to the 16 kHz Teams
        #     format. Without dither, ffmpeg truncates and the resulting
        #     quantization noise correlates with the signal, producing audibly
        #     "metallic" / "robotic" timbre on speech sibilants. Triangular
        #     dither decorrelates that noise into a faint smooth hiss far below
        #     the speech level, restoring a more natural voice.
        #
        #     NOTE: high-quality `resampler=soxr` is NOT available — the
        #     ffmpeg build on the VM was not compiled with libsoxr (see
        #     `ffmpeg -af aresample=resampler=soxr ...` -> "Invalid argument").
        #     ffmpeg's default swr resampler is acceptable here; the dither
        #     step alone is the dominant audible improvement at 16-bit.
        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel", "error",
                "-f", "mp4",
                "-fflags", "+genpts",
                "-i", "pipe:0",
                "-vn",
                "-af", "aresample=dither_method=triangular_hp",
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "-ar", "16000",
                "-ac", "1",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        logger.info(
            "FFmpeg audio decoder started (PCM16 16kHz mono, quality mode, queue_max=%s)",
            self._chunk_queue.maxsize,
        )

    def _read_loop(self):
        buf = b""
        try:
            while self.proc and self.proc.poll() is None:
                chunk = self.proc.stdout.read(self.CHUNK_SIZE - len(buf))
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= self.CHUNK_SIZE:
                    pcm = buf[: self.CHUNK_SIZE]
                    buf = buf[self.CHUNK_SIZE :]
                    try:
                        self._chunk_queue.put_nowait(pcm)
                    except queue.Full:
                        try:
                            self._chunk_queue.get_nowait()
                            self._dropped_chunks += 1
                        except queue.Empty:
                            pass
                        self._chunk_queue.put_nowait(pcm)
        except MemoryError:
            self._reader_failed = True
            cleared = _clear_queue(self._chunk_queue)
            logger.exception(
                "FFmpeg audio reader MemoryError; cleared_chunks=%s state=%s memory=%s",
                cleared,
                self.stats(),
                _process_memory_snapshot(),
            )
        except Exception as e:
            self._reader_failed = True
            logger.error(f"FFmpeg audio reader error: {e}")

    def restart(self, reason: str) -> None:
        self._restart_count += 1
        logger.warning(
            "Restarting FFmpeg audio decoder reason=%s state_before=%s memory=%s",
            reason,
            self.stats(),
            _process_memory_snapshot(),
        )
        self.stop()
        self.start()

    def feed(self, data: bytes) -> list[bytes]:
        if self.proc is None or self.proc.poll() is not None or self._reader_failed:
            if self._reader_failed:
                logger.warning("FFmpeg audio reader failed; restarting before feed")
            self.stop()
            self.start()
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except MemoryError:
            logger.exception(
                "FFmpeg audio feed MemoryError; state=%s memory=%s",
                self.stats(),
                _process_memory_snapshot(),
            )
            self.restart("audio_feed_memory_error")
            return []
        except (BrokenPipeError, OSError):
            logger.warning("FFmpeg audio pipe broken — restarting decoder")
            self.restart("audio_pipe_broken")
            return []

        chunks = []
        while len(chunks) < AUDIO_DECODER_DRAIN_MAX_CHUNKS and not self._chunk_queue.empty():
            try:
                chunks.append(self._chunk_queue.get_nowait())
            except queue.Empty:
                break
        return chunks

    def stop(self):
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                if self.proc:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
            self.proc = None
        _clear_queue(self._chunk_queue)
        self._reader_failed = False


class FFmpegPcmResampler:
    """Streaming PCM16 24kHz -> 16kHz resampler via FFmpeg.

    This replaces the numpy linear-interpolation fallback for
    response.audio.delta. Linear downsampling aliases speech above 8kHz and
    is the audible source of the metallic/robotic timbre when SciPy is not
    installed on the VM.
    """

    CHUNK_SIZE = 640

    def __init__(self, from_rate: int = 24000, to_rate: int = 16000):
        self.from_rate = from_rate
        self.to_rate = to_rate
        self.proc: Optional[subprocess.Popen] = None
        self._chunk_queue: queue.Queue[bytes] = queue.Queue(maxsize=400)
        self._reader: Optional[threading.Thread] = None

    def start(self):
        self.proc = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel", "error",
                "-f", "s16le",
                "-ar", str(self.from_rate),
                "-ac", "1",
                "-i", "pipe:0",
                "-af", "aresample=dither_method=triangular_hp",
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "-ar", str(self.to_rate),
                "-ac", "1",
                "pipe:1",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        logger.info("FFmpeg PCM resampler started (%sHz -> %sHz, dithered)", self.from_rate, self.to_rate)

    def _read_loop(self):
        buf = b""
        try:
            while self.proc and self.proc.poll() is None:
                chunk = self.proc.stdout.read(self.CHUNK_SIZE - len(buf))
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= self.CHUNK_SIZE:
                    pcm = buf[: self.CHUNK_SIZE]
                    buf = buf[self.CHUNK_SIZE :]
                    try:
                        self._chunk_queue.put_nowait(pcm)
                    except queue.Full:
                        try:
                            self._chunk_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self._chunk_queue.put_nowait(pcm)
        except Exception as e:
            logger.error(f"FFmpeg PCM resampler reader error: {e}")

    def _drain(self, wait_first_ms: int = 0) -> list[bytes]:
        if wait_first_ms > 0 and self._chunk_queue.empty():
            deadline = time.monotonic() + wait_first_ms / 1000.0
            while self._chunk_queue.empty() and time.monotonic() < deadline:
                time.sleep(0.002)

        chunks = []
        while not self._chunk_queue.empty():
            try:
                chunks.append(self._chunk_queue.get_nowait())
            except queue.Empty:
                break
        return chunks

    def feed(self, data: bytes) -> list[bytes]:
        if self.from_rate == self.to_rate:
            return [data]
        if self.proc is None:
            try:
                self.start()
            except Exception as e:
                logger.error(f"FFmpeg PCM resampler unavailable; falling back to in-process resampler: {e}")
                return [resample_pcm16(data, self.from_rate, self.to_rate)]
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            logger.warning(f"FFmpeg PCM resampler pipe broken; falling back for this chunk: {e}")
            self.stop()
            return [resample_pcm16(data, self.from_rate, self.to_rate)]

        return self._drain(wait_first_ms=20)

    def finish(self) -> list[bytes]:
        if not self.proc:
            return []
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        deadline = time.monotonic() + 0.5
        while self.proc and self.proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        chunks = self._drain(wait_first_ms=50)
        self.proc = None
        return chunks

    def stop(self):
        if self.proc:
            try:
                self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except Exception:
                if self.proc:
                    self.proc.kill()
            self.proc = None


# ── Phase 2: PTS extraction ─────────────────────────────────────────────────
#
# In-process fMP4 (ISO BMFF) parser. We feed it the SAME bytes that go to
# the ffmpeg video subprocess, walk the box structure, and extract per-
# sample PTS values from each `moof` fragment for the video track. The
# output queue is popped 1:1 with NV12 frames emitted by the ffmpeg
# decoder, attaching true Voice-Live encoder PTS to each forwarded video
# message.
#
# Why in-process over a side-channel ffmpeg run: zero new deps, zero
# subprocess change (preserves the AAC dither hack and crash isolation
# of the existing decoders), and exact 1:1 pairing because both consumers
# see the same byte stream in the same order.
#
# Audio is NOT extracted here \u2014 it's self-clocked at the PCM output rate
# via AudioPtsClock below. PCM is fixed-rate by definition, so its PTS is
# just (samples-emitted-so-far) at tb=1/16000.
#
# Box format reference: ISO/IEC 14496-12. We only parse what's needed:
#   moov
#     trak
#       tkhd       \u2192 track_id
#       mdia
#         mdhd     \u2192 timescale (per-track time-base denominator)
#         hdlr     \u2192 handler_type ('vide' / 'soun')
#   moof
#     traf
#       tfhd       \u2192 track_id, default_sample_duration (optional)
#       tfdt       \u2192 base_media_decode_time
#       trun       \u2192 sample_count, per-sample duration + composition_time_offset


import struct as _struct  # local alias to avoid colliding with anything else


class Fmp4PtsExtractor:
    """Minimal fMP4 parser that extracts video-sample PTS values."""

    def __init__(self):
        self._buf = b""
        self._video_track_id: Optional[int] = None
        self._video_timescale: Optional[int] = None
        self._default_sample_duration: int = 0
        # Bounded queue: ~1 second of 30 fps video; protects against memory
        # blowup if the bot stops consuming frames.
        self._video_pts_queue: queue.Queue = queue.Queue(maxsize=120)
        self._last_pts: Optional[int] = None
        self._monotonic_clamps = 0
        self._negative_drops = 0
        self._extracted_count = 0
        self._first_logged = 0
        # §9.5 desync canaries
        self._popped_count = 0
        self._empty_pops = 0
        self._queue_drops = 0
        self._b_frame_warned = False  # one-shot: non-zero composition_time_offset

    @property
    def video_time_base(self) -> tuple[int, int]:
        # tb_num / tb_den. Default 1/90000 if moov hasn't been parsed yet.
        return (1, self._video_timescale or 90000)

    def feed(self, data: bytes) -> None:
        self._buf += data
        if len(self._buf) > FMP4_PTS_BUFFER_MAX_BYTES:
            logger.warning(
                "fMP4 PTS: buffer exceeded %s bytes; dropping parser backlog",
                FMP4_PTS_BUFFER_MAX_BYTES,
            )
            self._buf = b""
            return
        while True:
            if len(self._buf) < 8:
                break
            size, btype = _struct.unpack(">I4s", self._buf[:8])
            hdr = 8
            if size == 1:
                if len(self._buf) < 16:
                    break
                size = _struct.unpack(">Q", self._buf[8:16])[0]
                hdr = 16
            elif size == 0:
                # Box extends to EOF \u2014 not meaningful for streamed input. Drop.
                self._buf = b""
                break
            if size < hdr or len(self._buf) < size:
                break
            box = self._buf[:size]
            self._buf = self._buf[size:]
            try:
                self._handle_box(btype, box[hdr:])
            except Exception as e:
                logger.warning(f"fMP4 PTS: box {btype!r} parse error (continuing): {e}")

    def _handle_box(self, btype: bytes, payload: bytes) -> None:
        if btype == b"moov":
            self._parse_moov(payload)
        elif btype == b"moof":
            self._parse_moof(payload)
        # ftyp / sidx / mdat / styp / etc. \u2014 ignored.

    @staticmethod
    def _walk(payload: bytes):
        """Yield (btype, sub_payload) for direct child boxes."""
        i = 0
        n = len(payload)
        while i + 8 <= n:
            size, btype = _struct.unpack(">I4s", payload[i:i+8])
            hdr = 8
            if size == 1:
                if i + 16 > n:
                    break
                size = _struct.unpack(">Q", payload[i+8:i+16])[0]
                hdr = 16
            if size < hdr or i + size > n:
                break
            yield btype, payload[i+hdr:i+size]
            i += size

    def _parse_moov(self, payload: bytes) -> None:
        for btype, sub in self._walk(payload):
            if btype == b"mvex":
                # Default sample duration may live here in `trex`; rarely
                # needed for Voice Live (per-sample durations in trun) but
                # parse defensively.
                for b2, p2 in self._walk(sub):
                    if b2 == b"trex" and len(p2) >= 24:
                        # version+flags(4) track_id(4) default_sample_description_index(4)
                        # default_sample_duration(4) ...
                        self._default_sample_duration = _struct.unpack(">I", p2[12:16])[0]
            elif btype == b"trak":
                self._parse_trak(sub)

    def _parse_trak(self, payload: bytes) -> None:
        track_id: Optional[int] = None
        timescale: Optional[int] = None
        is_video = False
        for btype, sub in self._walk(payload):
            if btype == b"tkhd" and len(sub) >= 24:
                version = sub[0]
                if version == 1 and len(sub) >= 32:
                    track_id = _struct.unpack(">I", sub[20:24])[0]
                else:
                    track_id = _struct.unpack(">I", sub[12:16])[0]
            elif btype == b"mdia":
                for b2, p2 in self._walk(sub):
                    if b2 == b"mdhd" and len(p2) >= 24:
                        v = p2[0]
                        if v == 1 and len(p2) >= 32:
                            timescale = _struct.unpack(">I", p2[20:24])[0]
                        else:
                            timescale = _struct.unpack(">I", p2[12:16])[0]
                    elif b2 == b"hdlr" and len(p2) >= 12:
                        if p2[8:12] == b"vide":
                            is_video = True
        if is_video and track_id is not None and timescale is not None:
            self._video_track_id = track_id
            self._video_timescale = timescale
            logger.info(
                f"fMP4 PTS: video track_id={track_id} timescale={timescale} (tb=1/{timescale})"
            )

    def _parse_moof(self, payload: bytes) -> None:
        for btype, sub in self._walk(payload):
            if btype == b"traf":
                self._parse_traf(sub)

    def _parse_traf(self, payload: bytes) -> None:
        track_id: Optional[int] = None
        base_decode_time: int = 0
        default_sample_duration = self._default_sample_duration
        truns: list[bytes] = []
        for btype, sub in self._walk(payload):
            if btype == b"tfhd" and len(sub) >= 8:
                flags = _struct.unpack(">I", sub[0:4])[0] & 0xFFFFFF
                track_id = _struct.unpack(">I", sub[4:8])[0]
                off = 8
                if flags & 0x000001:  # base_data_offset
                    off += 8
                if flags & 0x000002:  # sample_description_index
                    off += 4
                if flags & 0x000008 and off + 4 <= len(sub):  # default_sample_duration
                    default_sample_duration = _struct.unpack(">I", sub[off:off+4])[0]
                    off += 4
            elif btype == b"tfdt" and len(sub) >= 8:
                v = sub[0]
                if v == 1 and len(sub) >= 12:
                    base_decode_time = _struct.unpack(">Q", sub[4:12])[0]
                else:
                    base_decode_time = _struct.unpack(">I", sub[4:8])[0]
            elif btype == b"trun":
                truns.append(sub)

        # Only emit PTS for the video track.
        if track_id is None or self._video_track_id is None or track_id != self._video_track_id:
            return

        decode_time = base_decode_time
        for trun in truns:
            if len(trun) < 8:
                continue
            flags = _struct.unpack(">I", trun[0:4])[0] & 0xFFFFFF
            sample_count = _struct.unpack(">I", trun[4:8])[0]
            off = 8
            if flags & 0x000001:  # data_offset
                off += 4
            if flags & 0x000004:  # first_sample_flags
                off += 4
            for _ in range(sample_count):
                duration = default_sample_duration
                cto = 0
                if flags & 0x000100 and off + 4 <= len(trun):  # sample_duration
                    duration = _struct.unpack(">I", trun[off:off+4])[0]
                    off += 4
                if flags & 0x000200:  # sample_size
                    off += 4
                if flags & 0x000400:  # sample_flags
                    off += 4
                if flags & 0x000800 and off + 4 <= len(trun):  # composition_time_offset
                    # Treat as signed (works for v0 small values too since we
                    # don't expect huge offsets in low-latency H.264).
                    cto = _struct.unpack(">i", trun[off:off+4])[0]
                    off += 4
                    # §9.5: B-frames present → decode order ≠ presentation order.
                    # The 1:1 pop-by-index pairing in the bot would silently
                    # swap timestamps. Voice Live's H.264 is expected to be
                    # baseline/main (no B-frames); fire one-shot if seen so
                    # we get a clear signal in production.
                    if cto != 0 and not self._b_frame_warned:
                        self._b_frame_warned = True
                        logger.warning(
                            f"fMP4 PTS: non-zero composition_time_offset ({cto}) "
                            f"observed — B-frames present? 1:1 pairing may swap "
                            f"audio/video timestamps. See phase2 doc §11."
                        )
                pts = decode_time + cto
                self._enqueue_pts(pts)
                decode_time += duration

    def _enqueue_pts(self, pts: int) -> None:
        # AAC priming / decoder warmup can produce PTS<0; drop those.
        # (Voice Live's H.264 encoder shouldn't, but handle defensively.)
        if pts < 0:
            self._negative_drops += 1
            if self._negative_drops <= 3:
                logger.warning(f"fMP4 PTS: dropped negative PTS={pts}")
            return
        # Z2 monotonicity clamp (cheap belt-and-suspenders).
        if self._last_pts is not None and pts <= self._last_pts:
            self._monotonic_clamps += 1
            if self._monotonic_clamps <= 3:
                logger.warning(
                    f"fMP4 PTS: monotonic clamp pts={pts} <= last={self._last_pts}; "
                    f"forcing pts={self._last_pts + 1}"
                )
            pts = self._last_pts + 1
        self._last_pts = pts
        self._extracted_count += 1
        if self._first_logged < 5:
            self._first_logged += 1
            logger.info(f"fMP4 PTS: video sample[{self._extracted_count}] pts={pts}")
        try:
            self._video_pts_queue.put_nowait(pts)
        except queue.Full:
            # Bot is not consuming — drop oldest to keep up. §9.5: log so
            # the desync is visible (otherwise it stays silent).
            self._queue_drops += 1
            if self._queue_drops <= 3 or self._queue_drops % 100 == 0:
                logger.warning(
                    f"fMP4 PTS: queue full (drops={self._queue_drops}) — "
                    f"bot consumer slow OR more samples parsed than decoded"
                )
            try:
                self._video_pts_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._video_pts_queue.put_nowait(pts)
            except queue.Full:
                pass

    def pop_video_pts(self) -> Optional[int]:
        try:
            pts = self._video_pts_queue.get_nowait()
            self._popped_count += 1
            return pts
        except queue.Empty:
            # §9.5: more decoder frames emitted than fMP4 samples parsed.
            # Causes: moov not yet parsed (very early frames), ffmpeg
            # decoder error recovery, B-frame reorder. Log first 3 then
            # sample at 100x to keep logs bounded on chronic desync.
            self._empty_pops += 1
            if self._empty_pops <= 3 or self._empty_pops % 100 == 0:
                logger.warning(
                    f"fMP4 PTS: pop on empty queue (count={self._empty_pops}) — "
                    f"extracted={self._extracted_count} popped={self._popped_count}"
                )
            return None


class AudioPtsClock:
    """Self-clocked audio PTS counter. PCM is fixed-rate (16 kHz mono 16-bit
    little-endian by configuration), so the PTS of any chunk is exactly the
    cumulative sample count emitted before it. Time-base is always 1/16000.
    """

    SAMPLE_RATE = 16000

    def __init__(self):
        self._samples_emitted = 0
        self._first_logged = 0

    @property
    def time_base(self) -> tuple[int, int]:
        return (1, self.SAMPLE_RATE)

    def next_pts(self, pcm_byte_count: int) -> int:
        """Return the PTS for a chunk that starts here, then advance the
        cursor past it. `pcm_byte_count` must be in 16-bit-mono bytes
        (i.e. samples = bytes // 2).
        """
        pts = self._samples_emitted
        self._samples_emitted += max(0, pcm_byte_count) // 2
        if self._first_logged < 5:
            self._first_logged += 1
            logger.info(f"audio PTS: chunk[{self._first_logged}] pts={pts} samples={pcm_byte_count // 2}")
        return pts


def _delay_ms_to_pts(delay_ms: int, tb_num: int, tb_den: int) -> int:
    if delay_ms <= 0 or tb_num <= 0 or tb_den <= 0:
        return 0
    return int(round((delay_ms / 1000.0) * (tb_den / tb_num)))


# ── Session config builders ─────────────────────────────────────────────────


def _build_avatar_config() -> AvatarConfig:
    avatar_background = None
    effective_background_color = AVATAR_BACKGROUND_COLOR
    if COMPOSITE_BACKGROUND_ENABLED and not AVATAR_BACKGROUND_IMAGE_URL and not effective_background_color:
        effective_background_color = COMPOSITE_CHROMA_COLOR
    if AVATAR_BACKGROUND_IMAGE_URL:
        avatar_background = Background(image_url=AVATAR_BACKGROUND_IMAGE_URL)
    elif effective_background_color:
        avatar_background = Background(color=_normalize_voicelive_background_color(effective_background_color))

    video_params = {
        "codec": "h264",
        "resolution": VideoResolution(width=VIDEO_WIDTH, height=VIDEO_HEIGHT),
        "bitrate": VIDEO_BITRATE,
        "gop_size": VIDEO_GOP_SIZE,
    }
    if avatar_background is not None:
        video_params["background"] = avatar_background

    avatar = AvatarConfig(
        character=AVATAR_CHARACTER,
        style=AVATAR_STYLE,
        video=VideoParams(**video_params),
    )
    # Request WebSocket video output (not WebRTC). The SDK model may be a
    # TypedDict or a typed object — try both.
    try:
        avatar["output_protocol"] = "websocket"  # type: ignore[index]
    except Exception:
        try:
            avatar.output_protocol = "websocket"  # type: ignore[attr-defined]
        except Exception:
            logger.warning("Could not set avatar output_protocol to websocket")
    return avatar


def _build_turn_detection():
    if TURN_DETECTION_MODE in ("semantic", "azure_semantic_vad"):
        return AzureSemanticVad(
            threshold=VAD_THRESHOLD,
            prefix_padding_ms=VAD_PREFIX_PADDING_MS,
            speech_duration_ms=VAD_SPEECH_DURATION_MS,
            silence_duration_ms=VAD_SILENCE_DURATION_MS,
            remove_filler_words=True,
            create_response=True,
            interrupt_response=False,
        )

    return ServerVad(
        threshold=VAD_THRESHOLD,
        prefix_padding_ms=VAD_PREFIX_PADDING_MS,
        silence_duration_ms=VAD_SILENCE_DURATION_MS,
        create_response=True,
        interrupt_response=False,
    )


def _build_session_config(agent_mode: bool) -> RequestSession:
    """Build session.update payload.

    In agent mode the deployed Foundry agent owns instructions, tools, voice,
    VAD, and transcription via its ``microsoft.voice-live.configuration``
    metadata. We must NOT set those here or the service rejects with
    ``max_config_attempts_exceeded``.
    """
    avatar = _build_avatar_config() if ENABLE_AVATAR_VIDEO else None
    logger.info(
        "Voice Live config: voice=%s avatar_video=%s video=%sx%s bitrate=%s gop=%s turn_detection=%s vad_threshold=%s silence_ms=%s",
        VOICE_NAME,
        ENABLE_AVATAR_VIDEO,
        VIDEO_WIDTH,
        VIDEO_HEIGHT,
        VIDEO_BITRATE,
        VIDEO_GOP_SIZE,
        TURN_DETECTION_MODE,
        VAD_THRESHOLD,
        VAD_SILENCE_DURATION_MS,
    )

    if agent_mode:
        # NOTE: the platform's metadata-based voice-live config
        # (microsoft.voice-live.configuration) is capped at 512 chars per
        # metadata value, which is too small to hold a real config. With
        # NO config attached on the agent, the runtime has no VAD / no
        # voice / no transcription, so the candidate is never heard and
        # Lisa can't speak. We supply turn_detection / voice / transcription
        # via session.update here as a workaround — these are normally
        # rejected in agent mode when metadata config IS present, but
        # they're accepted (and required) when it is absent.
        transcription_lang = "de" if LANG == "de" else "en"
        session_kwargs: dict[str, Any] = {
            "modalities": [Modality.AUDIO],
            "input_audio_format": InputAudioFormat.PCM16,
            "input_audio_sampling_rate": 16000,
            "output_audio_format": OutputAudioFormat.PCM16,
            "input_audio_transcription": AudioInputTranscriptionOptions(
                model="azure-speech",
                language=transcription_lang,
            ),
            "turn_detection": _build_turn_detection(),
            "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
            "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
        }
        if avatar is not None:
            session_kwargs["avatar"] = avatar
        else:
            session_kwargs["voice"] = AzureStandardVoice(name=VOICE_NAME)
        return RequestSession(**session_kwargs)

    # Inline fallback — full config lives here.
    transcription_lang = "de" if LANG == "de" else "en"
    session_kwargs = {
        "modalities": [Modality.TEXT, Modality.AUDIO],
        "instructions": LISA_FALLBACK_INSTRUCTIONS,
        "input_audio_format": InputAudioFormat.PCM16,
        "input_audio_sampling_rate": 16000,
        "output_audio_format": OutputAudioFormat.PCM16,
        "input_audio_transcription": AudioInputTranscriptionOptions(
            model="azure-speech",
            language=transcription_lang,
        ),
        "turn_detection": _build_turn_detection(),
        "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
        "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
    }
    if avatar is not None:
        session_kwargs["avatar"] = avatar
    else:
        session_kwargs["voice"] = AzureStandardVoice(name=VOICE_NAME)
    return RequestSession(**session_kwargs)


# ── WebSocket endpoint ──────────────────────────────────────────────────────


@app.websocket("/stream")
async def stream_endpoint(ws: WebSocket):
    """Main WebSocket bridging EchoBot ⟷ Voice Live API."""
    await ws.accept()
    logger.info("EchoBot connected")

    video_decoder = FFmpegDecoder(VIDEO_WIDTH, VIDEO_HEIGHT) if ENABLE_AVATAR_VIDEO else None
    # In avatar mode the avatar fMP4 stream contains both H.264 video AND
    # AAC audio; Voice Live does NOT send response.audio.delta separately.
    # We need a parallel ffmpeg to extract the audio track for the bot.
    audio_decoder = FFmpegAudioDecoder() if ENABLE_AVATAR_VIDEO else None
    response_resampler = FFmpegPcmResampler()

    # Cost metering vars assigned once the session is up; initialized here so
    # the finally block can always reference them, even on early failure.
    cost_meter: Any = None
    cost_started_iso = datetime.now(timezone.utc).isoformat()

    agent_mode = _agent_mode_configured()
    agent_config = _build_agent_config()
    endpoint = _resolve_endpoint(AZURE_ENDPOINT, agent_mode=agent_mode)

    if agent_mode:
        target = (
            f"agent={LISA_AGENT_NAME}/{LISA_PROJECT_NAME}"
            f"@{LISA_AGENT_VERSION or 'latest'}"
        )
    else:
        target = f"inline-fallback model={MODEL} voice={VOICE_NAME}"
    logger.info(
        "Voice Live: endpoint=%s api=%s %s avatar_video=%s avatar=%s/%s",
        endpoint, API_VERSION, target, ENABLE_AVATAR_VIDEO, AVATAR_CHARACTER, AVATAR_STYLE,
    )

    credential: Any = None
    try:
        credential = _build_credential()

        connect_kwargs: dict[str, Any] = {
            "endpoint": endpoint,
            "credential": credential,
            "api_version": API_VERSION,
        }
        if agent_config is not None:
            connect_kwargs["agent_config"] = agent_config
        else:
            connect_kwargs["model"] = MODEL

        async with connect(**connect_kwargs) as connection:
            logger.info("Connected to Voice Live API")

            await connection.session.update(
                session=_build_session_config(agent_mode=agent_mode)
            )

            # Wait for SESSION_UPDATED
            first = await connection.recv()
            logger.info(f"Session ready: {getattr(first, 'type', '?')}")

            # Per-connection event used to gate Lisa's greeting on the
            # bot reporting CallState.Established. When the gate is
            # disabled (default), pre-set so the await is a no-op.
            call_established_evt = asyncio.Event()
            if not WAIT_FOR_CALL_ESTABLISHED:
                call_established_evt.set()
            call_context: dict[str, str] = {}

            # Per-call cost meter (Teams channel). Stashed in call_context so the
            # Voice Live event loop can add usage without a signature change.
            if _cost_enabled():
                cost_meter = CostMeter(is_teams=True)
                cost_started_iso = datetime.now(timezone.utc).isoformat()
                call_context["_cost_meter"] = cost_meter  # type: ignore[assignment]

            # Spawn bidirectional pipes BEFORE priming so we can
            # receive {"type":"call_established"} from the bot while
            # the gate is open. vl_task is also safe to start early
            # — Voice Live emits no events until we prime.
            bot_task = asyncio.create_task(
                _bot_to_voicelive(ws, connection, call_established_evt, call_context)
            )
            vl_task = asyncio.create_task(
                _voicelive_to_bot(connection, ws, video_decoder, audio_decoder, response_resampler, call_context)
            )

            # Greeting gate.
            if WAIT_FOR_CALL_ESTABLISHED:
                logger.info(
                    f"Waiting for call_established (timeout={CALL_ESTABLISHED_TIMEOUT_S}s)"
                )
                try:
                    await asyncio.wait_for(
                        call_established_evt.wait(),
                        timeout=CALL_ESTABLISHED_TIMEOUT_S,
                    )
                    logger.info("call_established received — priming Lisa")
                except asyncio.TimeoutError:
                    # Fall through and prime anyway — better to greet
                    # late than to leave Lisa silent if the bot signal
                    # is misconfigured. Do NOT return here — that would
                    # tear down both pipe tasks mid-call.
                    logger.warning(
                        "call_established timeout — priming anyway "
                        "(check LISA_WAIT_FOR_CALL_ESTABLISHED on bot side)"
                    )

            # Prime Lisa to open the call with the required disclosure and
            # consent question. Use a pre-generated assistant message so the
            # very first spoken output is deterministic and cannot be
            # paraphrased by the model.
            try:
                response_payload = {
                    "modalities": [Modality.AUDIO],
                    "output_audio_format": OutputAudioFormat.PCM16,
                    "pre_generated_assistant_message": AssistantMessageItem(
                        content=[OutputTextContentPart(text=LISA_INITIAL_CONSENT_PROMPT)]
                    ),
                }
                if not ENABLE_AVATAR_VIDEO:
                    response_payload["voice"] = AzureStandardVoice(name=VOICE_NAME)
                await connection.response.create(response=response_payload)
                logger.info(
                    "Sent required initial consent prompt via pre_generated_assistant_message"
                )
            except Exception as e:
                logger.warning(f"priming failed (continuing): {e}")

            done, pending = await asyncio.wait(
                [bot_task, vl_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()

    except WebSocketDisconnect:
        logger.info("EchoBot disconnected")
    except Exception as e:
        logger.error(f"Session error: {e}", exc_info=True)
    finally:
        if video_decoder is not None:
            video_decoder.stop()
        if audio_decoder is not None:
            audio_decoder.stop()
        response_resampler.stop()
        # Async credentials need explicit close; key creds don't.
        close = getattr(credential, "close", None)
        if close is not None:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        # Persist the per-call cost record (fail-soft; never raises).
        if cost_meter is not None and _cost_enabled():
            try:
                cost_meter.mark_ended()
                # The VMSS Graph-bot path joins Teams via Graph, not ACS, so the
                # ACS per-minute meter does not apply here — zero it out.
                rates = {**cost_rates(), "acsPerMin": 0.0}
                snap = cost_meter.snapshot(rates)
                persona = (LISA_AGENT_NAME or AVATAR_CHARACTER or "").strip()
                meeting_id = (
                    call_context.get("session_id")
                    or call_context.get("candidate_id")
                    or ""
                )
                record = CostRecord.from_snapshot(
                    uuid.uuid4().hex,
                    snap,
                    transport="vmss",
                    persona=persona,
                    model=MODEL,
                    meeting_id=meeting_id,
                    started_at=cost_started_iso,
                    region=os.getenv("AZURE_REGION", ""),
                    rates=rates,
                )
                await _COST_SINK.write(record)
            except Exception:
                logger.warning("cost record persist failed", exc_info=True)
        logger.info("Session ended")


# ── Pipeline: Bot → Voice Live ──────────────────────────────────────────────


async def _bot_to_voicelive(
    ws: WebSocket,
    conn,
    call_established_evt: asyncio.Event,
    call_context: dict[str, str],
):
    """Forward PCM16 16 kHz audio from EchoBot to Voice Live (no resample;
    session is configured with input_audio_sampling_rate=16000)."""
    msg_total = 0
    audio_chunks = 0
    audio_bytes = 0
    first_chunk_logged = False
    other_types: dict[str, int] = {}
    last_log = time.monotonic()
    # Energy stats over the rollup window — diagnose silent mic.
    rms_sum_sq = 0
    rms_samples = 0
    rms_peak = 0
    zero_rms_chunks = 0
    try:
        while True:
            raw = await ws.receive_text()
            received_ticks = _dotnet_utc_ticks()
            msg_total += 1
            msg = json.loads(raw)
            mtype = msg.get("type")
            # Signal from bot that the Teams call is now Established. Used
            # by the priming gate (LISA_WAIT_FOR_CALL_ESTABLISHED=1) to
            # delay Lisa's greeting until the candidate has accepted.
            if mtype == "call_established":
                for key in ("candidate_id", "candidate_name", "position", "session_id"):
                    value = str(msg.get(key) or "").strip()
                    if value:
                        call_context[key] = value
                if not call_established_evt.is_set():
                    logger.info(
                        "[bot→VL] call_established received candidate_id=%s",
                        call_context.get("candidate_id", ""),
                    )
                    call_established_evt.set()
                continue
            if mtype != "audio":
                other_types[mtype or "?"] = other_types.get(mtype or "?", 0) + 1
                if msg_total <= 5:
                    logger.info(f"[bot→VL] non-audio msg #{msg_total}: type={mtype} keys={list(msg.keys())}")
                continue
            pcm_16k = base64.b64decode(msg["data"])
            audio_chunks += 1
            audio_bytes += len(pcm_16k)
            if LATENCY_DIAG and (audio_chunks <= 5 or audio_chunks % 50 == 0):
                bot_sent_ticks = _safe_int(msg.get("sent_at_ticks"))
                bot_to_sidecar_ms = (
                    (received_ticks - bot_sent_ticks) // 10000
                    if bot_sent_ticks is not None
                    else None
                )
                logger.warning(
                    "[latency][sidecar] bot_audio_received count=%s seq=%s pcm_bytes=%s bot_to_sidecar_ms=%s",
                    audio_chunks,
                    msg.get("client_audio_seq", ""),
                    len(pcm_16k),
                    bot_to_sidecar_ms,
                )
            # Compute energy stats (int16 little-endian).
            try:
                samples = memoryview(pcm_16k).cast("h")
                chunk_sum_sq = 0
                chunk_peak = 0
                for s in samples:
                    sample_sq = s * s
                    rms_sum_sq += sample_sq
                    chunk_sum_sq += sample_sq
                    if s < 0:
                        s = -s
                    if s > rms_peak:
                        rms_peak = s
                    if s > chunk_peak:
                        chunk_peak = s
                rms_samples += len(samples)
                if chunk_peak == 0:
                    zero_rms_chunks += 1

                call_context["_audio_total_chunks"] = int(call_context.get("_audio_total_chunks", 0) or 0) + 1
                call_context["_audio_total_bytes"] = int(call_context.get("_audio_total_bytes", 0) or 0) + len(pcm_16k)
                call_context["_audio_total_zero_rms_chunks"] = int(call_context.get("_audio_total_zero_rms_chunks", 0) or 0) + (1 if chunk_peak == 0 else 0)
                call_context["_audio_total_sum_sq"] = int(call_context.get("_audio_total_sum_sq", 0) or 0) + chunk_sum_sq
                call_context["_audio_total_samples"] = int(call_context.get("_audio_total_samples", 0) or 0) + len(samples)
                call_context["_audio_total_peak"] = max(int(call_context.get("_audio_total_peak", 0) or 0), chunk_peak)
                if int(call_context.get("_turn_audio_active", 0) or 0):
                    call_context["_turn_audio_chunks"] = int(call_context.get("_turn_audio_chunks", 0) or 0) + 1
                    call_context["_turn_audio_bytes"] = int(call_context.get("_turn_audio_bytes", 0) or 0) + len(pcm_16k)
                    call_context["_turn_audio_zero_rms_chunks"] = int(call_context.get("_turn_audio_zero_rms_chunks", 0) or 0) + (1 if chunk_peak == 0 else 0)
                    call_context["_turn_audio_sum_sq"] = int(call_context.get("_turn_audio_sum_sq", 0) or 0) + chunk_sum_sq
                    call_context["_turn_audio_samples"] = int(call_context.get("_turn_audio_samples", 0) or 0) + len(samples)
                    call_context["_turn_audio_peak"] = max(int(call_context.get("_turn_audio_peak", 0) or 0), chunk_peak)
            except Exception:
                pass
            if not first_chunk_logged:
                logger.info(f"[bot→VL] FIRST audio chunk: {len(pcm_16k)} bytes 16kHz PCM")
                first_chunk_logged = True
            await conn.input_audio_buffer.append(
                audio=base64.b64encode(pcm_16k).decode()
            )
            now = time.monotonic()
            if now - last_log >= 5.0:
                rms = int((rms_sum_sq / rms_samples) ** 0.5) if rms_samples else 0
                logger.info(
                    f"[bot→VL] {audio_chunks} chunks ({audio_bytes} B) in 5s; "
                    f"rms={rms} peak={rms_peak} zero_rms_chunks={zero_rms_chunks} "
                    f"(int16, max=32767); other={other_types}"
                )
                audio_chunks = 0
                audio_bytes = 0
                other_types = {}
                rms_sum_sq = 0
                rms_samples = 0
                rms_peak = 0
                zero_rms_chunks = 0
                last_log = now
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    except Exception as e:
        logger.error(f"bot→VL error: {e}")


# ── Pipeline: Voice Live → Bot ──────────────────────────────────────────────


async def _voicelive_to_bot(
    conn,
    ws: WebSocket,
    decoder: Optional[FFmpegDecoder],
    audio_decoder: Optional[FFmpegAudioDecoder],
    response_resampler: FFmpegPcmResampler,
    call_context: dict[str, str],
):
    """Handle Voice Live events and relay audio/video to EchoBot.

    In Foundry agent mode, function tool calls (e.g. ``lookup_job_requirements``)
    are executed inside the deployed Lisa agent server-side — we don't see the
    call lifecycle here, only the resulting audio/text deltas.
    """
    loop = asyncio.get_event_loop()
    seen_events: set[str] = set()
    audio_delta_count = 0
    video_delta_count = 0
    audio_send_count = 0
    lisa_speaking = False
    last_lisa_output_at = 0.0
    speaking_watchdog_task: Optional[asyncio.Task] = None
    active_response_id: Optional[str] = None
    response_shapes: dict[str, dict[str, Any]] = {}
    latency_turn = 0
    latency_speech_stopped_at: Optional[float] = None
    latency_response_created_at: Optional[float] = None
    latency_first_audio_delta_at: Optional[float] = None
    latency_first_video_delta_at: Optional[float] = None
    latency_first_audio_sent_at: Optional[float] = None
    latency_first_non_silent_audio_sent_at: Optional[float] = None
    latency_first_video_sent_at: Optional[float] = None
    latency_speech_started_at: Optional[float] = None
    latency_output_audio_chunks = 0
    latency_output_silent_prefix_chunks = 0
    leading_silence_buffer: list[bytes] = []
    leading_silence_buffer_ms = 0
    leading_silence_ms = 0
    leading_silent_chunk_count = 0
    leading_trimmed_silence_ms = 0
    leading_trimmed_chunk_count = 0
    leading_first_chunk_rms: Optional[int] = None
    leading_first_chunk_peak: Optional[int] = None
    leading_first_audible_chunk_rms: Optional[int] = None
    leading_first_audible_chunk_peak: Optional[int] = None
    leading_audio_opened = False
    leading_trim_fail_open = False

    # Phase 2: PTS pipeline. Created per-session (cumulative state). When
    # USE_PTS is False, the extractor still runs (cheap; ~1 KB state) and
    # we still emit pts/tb fields — the bot ignores them when its own
    # LISA_USE_PTS=0. This keeps sidecar behavior uniform; only the bot
    # decides whether to consume them.
    pts_extractor = Fmp4PtsExtractor()
    audio_clock = AudioPtsClock()
    audio_timeline_started = False
    pending_video_messages: list[str] = []
    video_delay_pts_logged = False
    video_preroll_logged = False
    video_pts_next = 0
    video_frame_drop_count = 0
    background_compositor = _get_background_compositor()

    def event_as_dict(event: Any) -> Optional[dict[str, Any]]:
        for attr in ("as_dict", "model_dump", "dict"):
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    value = fn()
                    if isinstance(value, dict):
                        return value
                except Exception:
                    pass
        return None

    def event_payload_json(event: Any, as_dict: Optional[dict[str, Any]] = None) -> str:
        payload = as_dict if as_dict is not None else event_as_dict(event)
        return json.dumps(payload if payload is not None else event, default=str)[:2000]

    def response_id_from_event(as_dict: Optional[dict[str, Any]]) -> Optional[str]:
        if not isinstance(as_dict, dict):
            return None
        response_id = as_dict.get("response_id")
        if response_id:
            return str(response_id)
        response = as_dict.get("response")
        if isinstance(response, dict) and response.get("id"):
            return str(response["id"])
        return None

    def summarize_shape(record: dict[str, Any]) -> str:
        flags = ",".join(
            name
            for name in (
                "output_added",
                "output_done",
                "content_added",
                "content_done",
                "audio_delta",
                "done",
                "cancelled",
                "failed",
            )
            if record.get(name)
        ) or "created_only"
        return (
            f"response_id={record.get('response_id')} status={record.get('status')} "
            f"status_details={record.get('status_details')} output_count={record.get('output_count')} "
            f"item_type={record.get('item_type')} item_status={record.get('item_status')} "
            f"tool_name={record.get('tool_name')} content_count={record.get('content_count')} "
            f"part_type={record.get('part_type')} text_len={record.get('text_len')} "
            f"transcript_len={record.get('transcript_len')} "
            f"flags={flags}"
        )

    async def response_shape_watchdog(response_id: str) -> None:
        await asyncio.sleep(RESPONSE_SHAPE_TIMEOUT_S)
        record = response_shapes.get(response_id)
        if not record or record.get("done") or record.get("cancelled") or record.get("failed"):
            return
        logger.warning(
            "[VL shape] incomplete_after_timeout_s=%s %s",
            RESPONSE_SHAPE_TIMEOUT_S,
            summarize_shape(record),
        )

    def track_response_shape(
        etype_str: str, event: Any, as_dict: Optional[dict[str, Any]]
    ) -> None:
        nonlocal active_response_id
        if not RESPONSE_SHAPE_DIAG:
            return

        response_id = response_id_from_event(as_dict) or active_response_id
        if not response_id and etype_str != "response.created":
            return

        if etype_str == "response.created":
            response = as_dict.get("response", {}) if isinstance(as_dict, dict) else {}
            if not isinstance(response, dict):
                response = {}
            response_id = str(response.get("id") or response_id or "unknown")
            active_response_id = response_id
            record = response_shapes.setdefault(response_id, {"response_id": response_id})
            record.update(
                {
                    "status": response.get("status"),
                    "status_details": response.get("status_details"),
                    "modalities": response.get("modalities"),
                    "output_count": len(response.get("output") or []),
                    "created_at": time.monotonic(),
                }
            )
            logger.info("[VL shape] created %s", summarize_shape(record))
            asyncio.create_task(response_shape_watchdog(response_id))
            return

        if not response_id:
            return

        record = response_shapes.setdefault(response_id, {"response_id": response_id})
        if etype_str == "response.output_item.added":
            item = as_dict.get("item", {}) if isinstance(as_dict, dict) else {}
            if not isinstance(item, dict):
                item = {}
            record.update(
                {
                    "output_added": True,
                    "item_type": item.get("type"),
                    "item_status": item.get("status"),
                    "tool_name": item.get("name"),
                    "role": item.get("role"),
                    "content_count": len(item.get("content") or []),
                }
            )
        elif etype_str == "response.output_item.done":
            item = as_dict.get("item", {}) if isinstance(as_dict, dict) else {}
            if not isinstance(item, dict):
                item = {}
            record.update(
                {
                    "output_done": True,
                    "item_type": item.get("type"),
                    "item_status": item.get("status"),
                    "tool_name": item.get("name"),
                    "role": item.get("role"),
                    "content_count": len(item.get("content") or []),
                }
            )
        elif etype_str == "response.content_part.added":
            part = as_dict.get("part", {}) if isinstance(as_dict, dict) else {}
            if not isinstance(part, dict):
                part = {}
            record.update(
                {
                    "content_added": True,
                    "part_type": part.get("type"),
                    "text_len": len(part.get("text") or ""),
                    "transcript_len": len(part.get("transcript") or ""),
                }
            )
        elif etype_str == "response.content_part.done":
            part = as_dict.get("part", {}) if isinstance(as_dict, dict) else {}
            if not isinstance(part, dict):
                part = {}
            record.update(
                {
                    "content_done": True,
                    "part_type": part.get("type"),
                    "text_len": len(part.get("text") or ""),
                    "transcript_len": len(part.get("transcript") or ""),
                }
            )
        elif etype_str == "response.audio.delta":
            record["audio_delta"] = True
            record["audio_delta_count"] = int(record.get("audio_delta_count") or 0) + 1
        elif etype_str in ("response.done", "response.cancelled", "response.failed"):
            response = as_dict.get("response", {}) if isinstance(as_dict, dict) else {}
            if not isinstance(response, dict):
                response = {}
            record.update(
                {
                    "done": etype_str == "response.done",
                    "cancelled": etype_str == "response.cancelled",
                    "failed": etype_str == "response.failed",
                    "status": response.get("status", record.get("status")),
                    "status_details": response.get("status_details", record.get("status_details")),
                    "output_count": len(response.get("output") or []),
                }
            )

        if etype_str.startswith("response.") and "delta" not in etype_str:
            logger.info("[VL shape] %s %s", etype_str, summarize_shape(record))

    def mark_turn_audio_start() -> None:
        call_context["_turn_audio_active"] = 1
        call_context["_turn_audio_chunks"] = 0
        call_context["_turn_audio_bytes"] = 0
        call_context["_turn_audio_zero_rms_chunks"] = 0
        call_context["_turn_audio_sum_sq"] = 0
        call_context["_turn_audio_samples"] = 0
        call_context["_turn_audio_peak"] = 0

    def turn_audio_summary() -> dict[str, int]:
        chunks = int(call_context.get("_turn_audio_chunks", 0) or 0)
        bytes_seen = int(call_context.get("_turn_audio_bytes", 0) or 0)
        zero_chunks = int(call_context.get("_turn_audio_zero_rms_chunks", 0) or 0)
        sum_sq = int(call_context.get("_turn_audio_sum_sq", 0) or 0)
        samples = int(call_context.get("_turn_audio_samples", 0) or 0)
        peak = int(call_context.get("_turn_audio_peak", 0) or 0)
        rms = int((sum_sq / samples) ** 0.5) if samples else 0
        return {
            "chunks": chunks,
            "bytes": bytes_seen,
            "zero_rms_chunks": zero_chunks,
            "rms": rms,
            "peak": peak,
        }

    def is_non_silent_energy(rms: int, peak: int) -> bool:
        return peak >= LATENCY_NON_SILENT_PEAK or rms >= LATENCY_NON_SILENT_RMS

    def reset_leading_silence_state() -> None:
        nonlocal leading_silence_buffer, leading_silence_buffer_ms
        nonlocal leading_silence_ms, leading_silent_chunk_count
        nonlocal leading_trimmed_silence_ms, leading_trimmed_chunk_count
        nonlocal leading_first_chunk_rms, leading_first_chunk_peak
        nonlocal leading_first_audible_chunk_rms, leading_first_audible_chunk_peak
        nonlocal leading_audio_opened
        nonlocal leading_trim_fail_open
        leading_silence_buffer = []
        leading_silence_buffer_ms = 0
        leading_silence_ms = 0
        leading_silent_chunk_count = 0
        leading_trimmed_silence_ms = 0
        leading_trimmed_chunk_count = 0
        leading_first_chunk_rms = None
        leading_first_chunk_peak = None
        leading_first_audible_chunk_rms = None
        leading_first_audible_chunk_peak = None
        leading_audio_opened = False
        leading_trim_fail_open = False

    def remember_leading_preroll(pcm_16k: bytes, duration_ms: int) -> None:
        nonlocal leading_silence_buffer_ms
        if LEADING_AUDIO_PREROLL_MS <= 0:
            return
        leading_silence_buffer.append(pcm_16k)
        leading_silence_buffer_ms += duration_ms
        while len(leading_silence_buffer) > 1 and leading_silence_buffer_ms > LEADING_AUDIO_PREROLL_MS:
            removed = leading_silence_buffer.pop(0)
            leading_silence_buffer_ms = max(0, leading_silence_buffer_ms - _pcm16_duration_ms(removed))

    async def flush_leading_preroll() -> None:
        nonlocal leading_silence_buffer, leading_silence_buffer_ms
        if not leading_silence_buffer:
            return
        queued = leading_silence_buffer
        leading_silence_buffer = []
        leading_silence_buffer_ms = 0
        for queued_pcm in queued:
            await send_audio_pcm_now(queued_pcm)

    async def send_audio_pcm_now(
        pcm_16k: bytes,
        energy: Optional[tuple[int, int, int, int]] = None,
    ) -> None:
        nonlocal audio_timeline_started, audio_send_count, latency_first_audio_sent_at
        nonlocal latency_first_non_silent_audio_sent_at, latency_output_audio_chunks
        nonlocal latency_output_silent_prefix_chunks
        audio_send_count += 1
        output_rms = 0
        output_peak = 0
        output_nonzero_samples = 0
        output_samples = 0
        output_is_non_silent = False
        if LATENCY_DIAG:
            if energy is None:
                energy = _pcm16_energy(pcm_16k)
            output_rms, output_peak, output_nonzero_samples, output_samples = energy
            output_is_non_silent = is_non_silent_energy(output_rms, output_peak)
        msg_d: dict[str, Any] = {
            "type": "audio",
            "data": base64.b64encode(pcm_16k).decode(),
        }
        sidecar_sent_ticks = None
        if LATENCY_DIAG:
            sidecar_sent_ticks = _dotnet_utc_ticks()
            msg_d["sidecar_sent_ticks"] = sidecar_sent_ticks
            if active_response_id:
                msg_d["vl_response_id"] = active_response_id
        pts = None
        tb_num = None
        tb_den = None
        if USE_PTS:
            tb_num, tb_den = audio_clock.time_base
            pts = audio_clock.next_pts(len(pcm_16k))
            msg_d["pts"] = pts
            msg_d["tb_num"] = tb_num
            msg_d["tb_den"] = tb_den
        msg = json.dumps(msg_d)
        if AUDIO_PIPELINE_DIAG:
            logger.info(
                "[audio-pipeline] sidecar_send_audio attempt count=%s pcm_bytes=%s json_chars=%s pts=%s tb=%s/%s",
                audio_send_count,
                len(pcm_16k),
                len(msg),
                pts,
                tb_num,
                tb_den,
            )
        try:
            await ws.send_text(msg)
        except Exception:
            logger.exception(
                "[audio-pipeline] sidecar_send_audio failed count=%s pcm_bytes=%s ws_state=%s",
                audio_send_count,
                len(pcm_16k),
                getattr(ws, "client_state", "unknown"),
            )
            raise
        if AUDIO_PIPELINE_DIAG:
            logger.info(
                "[audio-pipeline] sidecar_send_audio sent count=%s pcm_bytes=%s pts=%s",
                audio_send_count,
                len(pcm_16k),
                pts,
            )
        if LATENCY_DIAG:
            latency_output_audio_chunks += 1
            output_chunk = latency_output_audio_chunks
            sent_at = time.perf_counter()
            if latency_first_audio_sent_at is None:
                latency_first_audio_sent_at = sent_at
                logger.warning(
                    "[latency][sidecar] first_audio_sent turn=%s response_id=%s speech_stop_to_audio_sent_ms=%s response_created_to_audio_sent_ms=%s pcm_bytes=%s rms=%s peak=%s sent_ticks=%s",
                    latency_turn,
                    active_response_id or "",
                    _elapsed_ms(latency_speech_stopped_at, latency_first_audio_sent_at),
                    _elapsed_ms(latency_response_created_at, latency_first_audio_sent_at),
                    len(pcm_16k),
                    output_rms,
                    output_peak,
                    sidecar_sent_ticks,
                )
            if latency_first_non_silent_audio_sent_at is None and not output_is_non_silent:
                latency_output_silent_prefix_chunks += 1
            if output_chunk <= LATENCY_OUTPUT_AUDIO_DIAG_CHUNKS:
                logger.warning(
                    "[latency][sidecar] output_audio_chunk turn=%s response_id=%s chunk=%s pcm_bytes=%s rms=%s peak=%s nonzero_samples=%s samples=%s non_silent=%s threshold_peak=%s threshold_rms=%s sent_ticks=%s",
                    latency_turn,
                    active_response_id or "",
                    output_chunk,
                    len(pcm_16k),
                    output_rms,
                    output_peak,
                    output_nonzero_samples,
                    output_samples,
                    str(output_is_non_silent).lower(),
                    LATENCY_NON_SILENT_PEAK,
                    LATENCY_NON_SILENT_RMS,
                    sidecar_sent_ticks,
                )
            if output_is_non_silent and latency_first_non_silent_audio_sent_at is None:
                latency_first_non_silent_audio_sent_at = sent_at
                logger.warning(
                    "[latency][sidecar] first_non_silent_audio_sent turn=%s response_id=%s speech_stop_to_non_silent_audio_sent_ms=%s response_created_to_non_silent_audio_sent_ms=%s first_audio_to_non_silent_ms=%s chunk=%s silent_prefix_chunks=%s leading_silence_ms=%s leading_silent_chunk_count=%s trimmed_leading_silence_ms=%s trimmed_leading_silent_chunk_count=%s first_chunk_rms=%s first_audible_chunk_rms=%s pcm_bytes=%s rms=%s peak=%s sent_ticks=%s",
                    latency_turn,
                    active_response_id or "",
                    _elapsed_ms(latency_speech_stopped_at, latency_first_non_silent_audio_sent_at),
                    _elapsed_ms(latency_response_created_at, latency_first_non_silent_audio_sent_at),
                    _elapsed_ms(latency_first_audio_sent_at, latency_first_non_silent_audio_sent_at),
                    output_chunk,
                    latency_output_silent_prefix_chunks,
                    leading_silence_ms,
                    leading_silent_chunk_count,
                    leading_trimmed_silence_ms,
                    leading_trimmed_chunk_count,
                    leading_first_chunk_rms,
                    leading_first_audible_chunk_rms,
                    len(pcm_16k),
                    output_rms,
                    output_peak,
                    sidecar_sent_ticks,
                )
        audio_timeline_started = True

    async def send_audio_pcm(pcm_16k: bytes) -> None:
        nonlocal leading_silence_ms, leading_silent_chunk_count
        nonlocal leading_trimmed_silence_ms, leading_trimmed_chunk_count
        nonlocal leading_first_chunk_rms, leading_first_chunk_peak
        nonlocal leading_first_audible_chunk_rms, leading_first_audible_chunk_peak
        nonlocal leading_audio_opened
        nonlocal leading_trim_fail_open

        energy = _pcm16_energy(pcm_16k) if (TRIM_LEADING_AUDIO_SILENCE or LATENCY_DIAG) else None
        if energy is None:
            await send_audio_pcm_now(pcm_16k)
            return

        output_rms, output_peak, _output_nonzero_samples, _output_samples = energy
        output_non_silent = is_non_silent_energy(output_rms, output_peak)
        duration_ms = _pcm16_duration_ms(pcm_16k)

        if leading_first_chunk_rms is None:
            leading_first_chunk_rms = output_rms
            leading_first_chunk_peak = output_peak

        if not leading_audio_opened and not output_non_silent:
            leading_silent_chunk_count += 1
            leading_silence_ms += duration_ms
            if TRIM_LEADING_AUDIO_SILENCE and not leading_trim_fail_open:
                remember_leading_preroll(pcm_16k, duration_ms)
                if (
                    LEADING_AUDIO_TRIM_MAX_HOLD_MS <= 0
                    or leading_silence_ms <= LEADING_AUDIO_TRIM_MAX_HOLD_MS
                ):
                    return
                leading_trim_fail_open = True
                logger.warning(
                    "[latency][sidecar] leading_silence_trim_fail_open turn=%s response_id=%s leading_silence_ms=%s leading_silent_chunk_count=%s max_hold_ms=%s preroll_ms=%s preroll_chunk_count=%s first_chunk_rms=%s first_chunk_peak=%s",
                    latency_turn,
                    active_response_id or "",
                    leading_silence_ms,
                    leading_silent_chunk_count,
                    LEADING_AUDIO_TRIM_MAX_HOLD_MS,
                    leading_silence_buffer_ms,
                    len(leading_silence_buffer),
                    leading_first_chunk_rms,
                    leading_first_chunk_peak,
                )
                await flush_leading_preroll()
                leading_audio_opened = True
                if not audio_timeline_started:
                    await send_audio_pcm_now(pcm_16k, energy)
                return

        if output_non_silent and not leading_audio_opened:
            leading_audio_opened = True
            if leading_first_audible_chunk_rms is None:
                leading_first_audible_chunk_rms = output_rms
                leading_first_audible_chunk_peak = output_peak
            if TRIM_LEADING_AUDIO_SILENCE and leading_silence_buffer:
                preroll_ms = leading_silence_buffer_ms
                preroll_chunks = len(leading_silence_buffer)
                leading_trimmed_silence_ms = max(0, leading_silence_ms - preroll_ms)
                leading_trimmed_chunk_count = max(0, leading_silent_chunk_count - preroll_chunks)
                logger.warning(
                    "[latency][sidecar] leading_silence_trimmed turn=%s response_id=%s leading_silence_ms=%s leading_silent_chunk_count=%s trimmed_leading_silence_ms=%s trimmed_leading_silent_chunk_count=%s preroll_ms=%s preroll_chunk_count=%s first_chunk_rms=%s first_chunk_peak=%s first_audible_chunk_rms=%s first_audible_chunk_peak=%s",
                    latency_turn,
                    active_response_id or "",
                    leading_silence_ms,
                    leading_silent_chunk_count,
                    leading_trimmed_silence_ms,
                    leading_trimmed_chunk_count,
                    preroll_ms,
                    preroll_chunks,
                    leading_first_chunk_rms,
                    leading_first_chunk_peak,
                    leading_first_audible_chunk_rms,
                    leading_first_audible_chunk_peak,
                )
                await flush_leading_preroll()

        await send_audio_pcm_now(pcm_16k, energy)

    async def send_video_message(msg: str) -> None:
        nonlocal latency_first_video_sent_at
        if LATENCY_DIAG:
            try:
                msg_d = json.loads(msg)
                sidecar_sent_ticks = _dotnet_utc_ticks()
                msg_d["sidecar_sent_ticks"] = sidecar_sent_ticks
                if active_response_id:
                    msg_d["vl_response_id"] = active_response_id
                msg = json.dumps(msg_d)
                if latency_first_video_sent_at is None:
                    latency_first_video_sent_at = time.perf_counter()
                    logger.warning(
                        "[latency][sidecar] first_video_sent turn=%s response_id=%s speech_stop_to_video_sent_ms=%s response_created_to_video_sent_ms=%s sent_ticks=%s",
                        latency_turn,
                        active_response_id or "",
                        _elapsed_ms(latency_speech_stopped_at, latency_first_video_sent_at),
                        _elapsed_ms(latency_response_created_at, latency_first_video_sent_at),
                        sidecar_sent_ticks,
                    )
            except Exception:
                logger.exception("[latency][sidecar] failed to stamp video message")
        if LISA_VIDEO_DELAY_MS > 0 and not USE_PTS:
            asyncio.create_task(_delayed_ws_send(ws, msg, LISA_VIDEO_DELAY_MS / 1000.0))
        else:
            await ws.send_text(msg)

    async def flush_pending_video() -> None:
        nonlocal pending_video_messages
        if not pending_video_messages:
            return
        queued = pending_video_messages
        pending_video_messages = []
        logger.info("[VL->bot] releasing %s queued video frame(s) after first audio", len(queued))
        for pending_msg in queued:
            await send_video_message(pending_msg)

    async def queue_or_send_video(msg: str) -> None:
        nonlocal video_preroll_logged
        if USE_PTS and _FORWARD_MUXED_AUDIO and not audio_timeline_started:
            if VIDEO_PREROLL_MAX_FRAMES <= 0:
                return
            pending_video_messages.append(msg)
            if len(pending_video_messages) > VIDEO_PREROLL_MAX_FRAMES:
                pending_video_messages.pop(0)
            if not video_preroll_logged:
                video_preroll_logged = True
                logger.info(
                    "[VL->bot] queueing avatar video until muxed audio anchors PTS"
                )
            return
        await send_video_message(msg)

    async def set_lisa_speaking(value: bool) -> None:
        nonlocal lisa_speaking
        if lisa_speaking == value:
            return
        lisa_speaking = value
        try:
            await ws.send_text(json.dumps({"type": "speaking", "value": value}))
        except Exception:
            pass

    async def speaking_idle_watchdog() -> None:
        while lisa_speaking and SPEAKING_IDLE_RESET_MS > 0:
            idle_s = time.monotonic() - last_lisa_output_at
            wait_s = (SPEAKING_IDLE_RESET_MS / 1000.0) - idle_s
            if wait_s <= 0:
                logger.warning(
                    "[VL->bot] speaking idle reset after %sms without output; reopening mic",
                    SPEAKING_IDLE_RESET_MS,
                )
                await set_lisa_speaking(False)
                return
            await asyncio.sleep(min(wait_s, 0.25))

    async def mark_lisa_output() -> None:
        nonlocal last_lisa_output_at, speaking_watchdog_task
        last_lisa_output_at = time.monotonic()
        await set_lisa_speaking(True)
        if SPEAKING_IDLE_RESET_MS <= 0:
            return
        if speaking_watchdog_task is None or speaking_watchdog_task.done():
            speaking_watchdog_task = asyncio.create_task(speaking_idle_watchdog())

    try:
        while True:
            try:
                event = await conn.recv()
            except (ConnectionError, OSError) as e:
                logger.warning(f"recv parse error (continuing): {e}")
                continue
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Connection error: {e}")
                break

            etype = getattr(event, "type", "unknown")
            etype_str = getattr(etype, "value", None) or str(etype)
            shape_dict = event_as_dict(event) if RESPONSE_SHAPE_DIAG else None
            track_response_shape(etype_str, event, shape_dict)
            if LATENCY_DIAG and etype_str == "response.created":
                payload_dict = shape_dict or event_as_dict(event)
                response_id = response_id_from_event(payload_dict)
                if response_id:
                    active_response_id = response_id
                latency_response_created_at = time.perf_counter()
                logger.warning(
                    "[latency][sidecar] response_created turn=%s response_id=%s speech_stop_to_response_created_ms=%s",
                    latency_turn,
                    active_response_id or "",
                    _elapsed_ms(latency_speech_stopped_at, latency_response_created_at),
                )

            if etype == ServerEventType.RESPONSE_AUDIO_DELTA:
                if hasattr(event, "delta") and event.delta:
                    if LATENCY_DIAG and latency_first_audio_delta_at is None:
                        latency_first_audio_delta_at = time.perf_counter()
                        logger.warning(
                            "[latency][sidecar] first_audio_delta turn=%s response_id=%s speech_stop_to_audio_delta_ms=%s response_created_to_audio_delta_ms=%s",
                            latency_turn,
                            active_response_id or "",
                            _elapsed_ms(latency_speech_stopped_at, latency_first_audio_delta_at),
                            _elapsed_ms(latency_response_created_at, latency_first_audio_delta_at),
                        )
                    pcm_24k = (
                        event.delta
                        if isinstance(event.delta, bytes)
                        else base64.b64decode(event.delta)
                    )
                    # First delta of a response: tell the bot Lisa is now
                    # speaking so it gates the mic to prevent self-loop /
                    # echo feeding back into VL.
                    if audio_delta_count == 0:
                        await mark_lisa_output()
                    # When the muxed-audio path (AAC inside fMP4 video) is
                    # enabled it already produces lip-synced 16 kHz PCM with
                    # dither. Forwarding the parallel response.audio.delta
                    # stream as well causes the bot to mix two slightly
                    # offset copies of Lisa's voice => robotic / echoey
                    # timbre AND a perceived audio delay vs the avatar
                    # mouth. Skip audio here in that mode and let the
                    # video.delta branch own the audio path.
                    if not _FORWARD_MUXED_AUDIO:
                        if AUDIO_RESAMPLER_MODE == "ffmpeg":
                            pcm_chunks = await loop.run_in_executor(
                                None, response_resampler.feed, pcm_24k
                            )
                        else:
                            pcm_chunks = [resample_pcm16(pcm_24k, 24000, 16000)]
                        for pcm_16k in pcm_chunks:
                            await send_audio_pcm(pcm_16k)
                        await flush_pending_video()
                    audio_delta_count += 1
                    if audio_delta_count == 1 or audio_delta_count % 50 == 0:
                        logger.info(
                            f"[VL→bot] audio.delta count={audio_delta_count}"
                        )

            elif etype == "response.video.delta":
                if not ENABLE_AVATAR_VIDEO or decoder is None or audio_decoder is None:
                    video_delta_count += 1
                    if video_delta_count == 1 or video_delta_count % 50 == 0:
                        logger.warning(
                            "[VL->bot] dropping unexpected video.delta while avatar video is disabled count=%s",
                            video_delta_count,
                        )
                    continue
                delta = (
                    event.get("delta", "")
                    if hasattr(event, "get")
                    else getattr(event, "delta", "")
                )
                if delta:
                    try:
                        raw = base64.b64decode(delta)
                    except MemoryError:
                        logger.exception(
                            "[VL->bot] video.delta base64 decode MemoryError; delta_len=%s memory=%s",
                            len(delta),
                            _process_memory_snapshot(),
                        )
                        await asyncio.gather(
                            loop.run_in_executor(None, decoder.restart, "video_delta_decode_memory_error"),
                            loop.run_in_executor(None, audio_decoder.restart, "video_delta_decode_memory_error"),
                        )
                        continue

                    if LATENCY_DIAG and latency_first_video_delta_at is None:
                        latency_first_video_delta_at = time.perf_counter()
                        logger.warning(
                            "[latency][sidecar] first_video_delta turn=%s response_id=%s speech_stop_to_video_delta_ms=%s response_created_to_video_delta_ms=%s raw_bytes=%s",
                            latency_turn,
                            active_response_id or "",
                            _elapsed_ms(latency_speech_stopped_at, latency_first_video_delta_at),
                            _elapsed_ms(latency_response_created_at, latency_first_video_delta_at),
                            len(raw),
                        )

                    if len(raw) > MAX_FMP4_DELTA_BYTES:
                        video_delta_count += 1
                        logger.warning(
                            "[VL->bot] dropping oversized video.delta count=%s raw=%sB limit=%sB memory=%s",
                            video_delta_count,
                            len(raw),
                            MAX_FMP4_DELTA_BYTES,
                            _process_memory_snapshot(),
                        )
                        continue

                    try:
                        # Feed PTS extractor synchronously BEFORE the ffmpeg
                        # subprocess executors run — the parser is pure-Python,
                        # zero-copy on small slices, and runs in microseconds.
                        # Doing it here guarantees the per-fragment PTS values
                        # are queued before we await frames, so the 1:1 pairing
                        # below is deterministic.
                        pts_extractor.feed(raw)
                        # Feed the same fMP4 chunk to BOTH decoders (video + audio).
                        frames, pcm_chunks = await asyncio.gather(
                            loop.run_in_executor(None, decoder.feed, raw),
                            loop.run_in_executor(None, audio_decoder.feed, raw),
                        )
                    except MemoryError:
                        logger.exception(
                            "[VL->bot] video.delta decoder MemoryError; raw=%sB video_decoder={%s} audio_decoder={%s} memory=%s",
                            len(raw),
                            decoder.stats(),
                            audio_decoder.stats(),
                            _process_memory_snapshot(),
                        )
                        await asyncio.gather(
                            loop.run_in_executor(None, decoder.restart, "video_delta_memory_error"),
                            loop.run_in_executor(None, audio_decoder.restart, "video_delta_memory_error"),
                        )
                        continue
                    except Exception:
                        logger.exception(
                            "[VL->bot] video.delta decoder feed failed; raw=%sB video_decoder={%s} audio_decoder={%s} memory=%s",
                            len(raw),
                            decoder.stats(),
                            audio_decoder.stats(),
                            _process_memory_snapshot(),
                        )
                        frames = []
                        pcm_chunks = []

                    video_delta_count += 1
                    if video_delta_count == 1 or video_delta_count % 50 == 0:
                        logger.info(
                            f"[VL→bot] video.delta count={video_delta_count} "
                            f"raw={len(raw)}B nv12_frames={len(frames)} "
                            f"pcm_chunks={len(pcm_chunks)}"
                        )
                    if video_delta_count == 1 and not frames and not pcm_chunks:
                        logger.warning(
                            "[VL->bot] first video.delta produced zero decoded output; video_decoder={%s} audio_decoder={%s} memory=%s",
                            decoder.stats(),
                            audio_decoder.stats(),
                            _process_memory_snapshot(),
                        )
                    if _FORWARD_MUXED_AUDIO and pcm_chunks:
                        # The AAC track muxed in the avatar fMP4 is the audio
                        # stream naturally paired with the mouth frames. Send it
                        # before video frames so the bot's PTS resolver anchors
                        # on audio first, then suppress response.audio.delta.
                        await mark_lisa_output()
                        for pcm in pcm_chunks:
                            await send_audio_pcm(pcm)
                        await flush_pending_video()
                    if len(frames) > VIDEO_SEND_MAX_FRAMES_PER_DELTA:
                        drop_count = len(frames) - VIDEO_SEND_MAX_FRAMES_PER_DELTA
                        frames = frames[-VIDEO_SEND_MAX_FRAMES_PER_DELTA:]
                        video_frame_drop_count += drop_count
                        if USE_PTS:
                            video_pts_next += drop_count
                        if video_frame_drop_count <= 3 or video_frame_drop_count % 100 == 0:
                            logger.warning(
                                "[VL->bot] dropping decoded video frames to preserve audio cadence dropped_now=%s dropped_total=%s keep=%s",
                                drop_count,
                                video_frame_drop_count,
                                VIDEO_SEND_MAX_FRAMES_PER_DELTA,
                            )
                    for frame in frames:
                        if background_compositor is not None:
                            try:
                                frame = background_compositor.composite_nv12(
                                    frame, VIDEO_WIDTH, VIDEO_HEIGHT
                                )
                            except Exception as exc:
                                background_compositor.record_failure(exc)
                                logger.exception(
                                    "Background compositor failed; sending original avatar frame"
                                )
                        vmsg: dict[str, Any] = {
                            "type": "video",
                            "data": base64.b64encode(frame).decode(),
                            "width": VIDEO_WIDTH,
                            "height": VIDEO_HEIGHT,
                            "timestamp": 0,
                        }
                        if USE_PTS:
                            tb_num, tb_den = 1, VIDEO_FPS
                            delay_pts = _delay_ms_to_pts(
                                LISA_VIDEO_DELAY_MS, tb_num, tb_den
                            )
                            if delay_pts and not video_delay_pts_logged:
                                video_delay_pts_logged = True
                                logger.info(
                                    "[VL->bot] applying video PTS delay: %sms (%s pts at tb=%s/%s)",
                                    LISA_VIDEO_DELAY_MS,
                                    delay_pts,
                                    tb_num,
                                    tb_den,
                                )
                            vmsg["pts"] = video_pts_next + delay_pts
                            vmsg["tb_num"] = tb_num
                            vmsg["tb_den"] = tb_den
                            video_pts_next += 1
                        msg = json.dumps(vmsg)
                        await queue_or_send_video(msg)

            elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
                # User started speaking. Do NOT signal the bot here — the
                # bot's `speaking` flag means *Lisa* is speaking (used to
                # mute mic forwarding to avoid echo loop). Sending true here
                # would make the bot drop the user's own audio and VL would
                # never see the rest of the utterance.
                if LATENCY_DIAG:
                    latency_speech_started_at = time.perf_counter()
                    mark_turn_audio_start()
                    logger.warning(
                        "[latency][sidecar] speech_started turn=%s total_audio_chunks=%s",
                        latency_turn + 1,
                        call_context.get("_audio_total_chunks", 0),
                    )

            elif etype == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
                if LATENCY_DIAG:
                    latency_turn += 1
                    latency_speech_stopped_at = time.perf_counter()
                    latency_response_created_at = None
                    latency_first_audio_delta_at = None
                    latency_first_video_delta_at = None
                    latency_first_audio_sent_at = None
                    latency_first_non_silent_audio_sent_at = None
                    latency_first_video_sent_at = None
                    latency_output_audio_chunks = 0
                    latency_output_silent_prefix_chunks = 0
                    active_response_id = None
                    reset_leading_silence_state()
                    audio_summary = turn_audio_summary()
                    logger.warning(
                        "[latency][sidecar] speech_stopped turn=%s speech_started_to_stopped_ms=%s user_audio_chunks=%s user_audio_bytes=%s zero_rms_chunks=%s user_audio_rms=%s user_audio_peak=%s",
                        latency_turn,
                        _elapsed_ms(latency_speech_started_at, latency_speech_stopped_at),
                        audio_summary["chunks"],
                        audio_summary["bytes"],
                        audio_summary["zero_rms_chunks"],
                        audio_summary["rms"],
                        audio_summary["peak"],
                    )
                    call_context["_turn_audio_active"] = 0

            elif str(getattr(etype, "value", etype)) in ("response.done", "response.cancelled"):
                try:
                    payload = event_payload_json(event, shape_dict)
                    logger.info(f"[VL payload] {etype_str}: {payload}")
                except Exception:
                    logger.info(f"[VL payload-raw] {etype_str}: {event!r}")
                # Cost metering: fold this response's token usage into the meter.
                meter = call_context.get("_cost_meter")
                if meter is not None:
                    try:
                        d = shape_dict or event_as_dict(event) or {}
                        resp = d.get("response") if isinstance(d, dict) else None
                        usage = resp.get("usage") if isinstance(resp, dict) else None
                        if usage is None and isinstance(d, dict):
                            usage = d.get("usage")
                        meter.add_realtime_usage(usage)
                    except Exception:
                        logger.debug("cost: usage capture failed", exc_info=True)
                if not _FORWARD_MUXED_AUDIO and AUDIO_RESAMPLER_MODE == "ffmpeg":
                    tail_chunks = await loop.run_in_executor(None, response_resampler.finish)
                    if tail_chunks:
                        logger.info("[VL->bot] flushed %s PCM resampler tail chunks", len(tail_chunks))
                        await mark_lisa_output()
                    for pcm_16k in tail_chunks:
                        await send_audio_pcm(pcm_16k)
                # Lisa finished talking — re-open the mic gate.
                await set_lisa_speaking(False)
                if LATENCY_DIAG:
                    done_at = time.perf_counter()
                    logger.warning(
                        "[latency][sidecar] response_finished turn=%s response_id=%s event=%s speech_stop_to_done_ms=%s speech_stop_to_first_audio_delta_ms=%s speech_stop_to_first_audio_sent_ms=%s speech_stop_to_first_non_silent_audio_sent_ms=%s first_audio_to_first_non_silent_ms=%s output_audio_chunks=%s silent_prefix_chunks=%s leading_silence_ms=%s leading_silent_chunk_count=%s trimmed_leading_silence_ms=%s trimmed_leading_silent_chunk_count=%s first_chunk_rms=%s first_audible_chunk_rms=%s speech_stop_to_first_video_delta_ms=%s speech_stop_to_first_video_sent_ms=%s",
                        latency_turn,
                        active_response_id or "",
                        etype_str,
                        _elapsed_ms(latency_speech_stopped_at, done_at),
                        _elapsed_ms(latency_speech_stopped_at, latency_first_audio_delta_at),
                        _elapsed_ms(latency_speech_stopped_at, latency_first_audio_sent_at),
                        _elapsed_ms(latency_speech_stopped_at, latency_first_non_silent_audio_sent_at),
                        _elapsed_ms(latency_first_audio_sent_at, latency_first_non_silent_audio_sent_at),
                        latency_output_audio_chunks,
                        latency_output_silent_prefix_chunks,
                        leading_silence_ms,
                        leading_silent_chunk_count,
                        leading_trimmed_silence_ms,
                        leading_trimmed_chunk_count,
                        leading_first_chunk_rms,
                        leading_first_audible_chunk_rms,
                        _elapsed_ms(latency_speech_stopped_at, latency_first_video_delta_at),
                        _elapsed_ms(latency_speech_stopped_at, latency_first_video_sent_at),
                    )
                audio_delta_count = 0

            elif etype == ServerEventType.ERROR:
                logger.error(f"Voice Live error: {event}")

            else:
                # Log first occurrence of every event type so we can verify
                # the agent's response audio actually arrives (RESPONSE_AUDIO_DELTA)
                # and identify any unexpected events. Subsequent occurrences
                # are suppressed to keep logs readable.
                if etype not in seen_events:
                    seen_events.add(etype)
                    logger.info(f"[VL event first-seen] {etype}")
                # Always dump full payload for session.* and response.*
                # (except per-token deltas) so we can see what config VL
                # actually accepted and why the agent produced no audio.
                etype_str = getattr(etype, "value", None) or str(etype)
                is_session = etype_str.startswith("session.") and "avatar" not in etype_str
                is_response_meta = etype_str.startswith("response.") and "delta" not in etype_str
                if is_session or is_response_meta:
                    try:
                        # event is a pydantic-ish model; try as_dict / model_dump
                        as_dict = None
                        for attr in ("as_dict", "model_dump", "dict"):
                            fn = getattr(event, attr, None)
                            if callable(fn):
                                try:
                                    as_dict = fn()
                                    break
                                except Exception:
                                    pass
                        payload = json.dumps(as_dict if as_dict is not None else event, default=str)[:2000]
                        logger.info(f"[VL payload] {etype_str}: {payload}")
                    except Exception:
                        logger.info(f"[VL payload-raw] {etype_str}: {event!r}")

                # Persist conversation transcripts to disk so we can
                # easily verify "did Lisa actually answer?" without grepping
                # huge log files. Best-effort; failures are swallowed.
                try:
                    as_dict = None
                    for attr in ("as_dict", "model_dump", "dict"):
                        fn = getattr(event, attr, None)
                        if callable(fn):
                            try:
                                as_dict = fn()
                                break
                            except Exception:
                                pass
                    if isinstance(as_dict, dict):
                        if etype_str == "conversation.item.input_audio_transcription.completed":
                            transcript = str(as_dict.get("transcript", "") or "")
                            if LATENCY_DIAG:
                                logger.warning(
                                    "[latency][sidecar] transcription_completed turn=%s speech_stop_to_transcription_ms=%s transcript=%s",
                                    latency_turn,
                                    _elapsed_ms(latency_speech_stopped_at, time.perf_counter()),
                                    transcript[:300],
                                )
                            _write_transcript(
                                "user",
                                transcript,
                                as_dict.get("conversation_id", ""),
                                call_context,
                            )
                        elif etype_str == "response.output_item.done":
                            item = as_dict.get("item") or {}
                            for part in (item.get("content") or []):
                                t = part.get("transcript") or part.get("text")
                                if t:
                                    _write_transcript(
                                        "assistant",
                                        t,
                                        as_dict.get("conversation_id", "")
                                        or item.get("conversation_id", ""),
                                        call_context,
                                    )
                except Exception:
                    pass

    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"VL→bot error: {e}", exc_info=True)
    finally:
        if speaking_watchdog_task is not None:
            speaking_watchdog_task.cancel()


# ── Health check ────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    effective_avatar_background = "image" if AVATAR_BACKGROUND_IMAGE_URL else None
    effective_avatar_background_color = AVATAR_BACKGROUND_COLOR
    if COMPOSITE_BACKGROUND_ENABLED and not AVATAR_BACKGROUND_IMAGE_URL and not effective_avatar_background_color:
        effective_avatar_background_color = COMPOSITE_CHROMA_COLOR
    if effective_avatar_background is None and (AVATAR_BACKGROUND_COLOR or COMPOSITE_BACKGROUND_ENABLED):
        effective_avatar_background = "color"
    effective_avatar_background_color_sent = None
    if effective_avatar_background_color:
        try:
            effective_avatar_background_color_sent = _normalize_voicelive_background_color(
                effective_avatar_background_color
            )
        except Exception as exc:
            effective_avatar_background_color_sent = f"invalid:{exc}"
    background_compositing = _background_compositor_health(load_background=True)
    return {
        "status": "ok",
        "agent_mode": _agent_mode_configured(),
        "endpoint": _resolve_endpoint(AZURE_ENDPOINT, agent_mode=_agent_mode_configured()),
        "agent": (
            f"{LISA_AGENT_NAME}@{LISA_AGENT_VERSION or 'latest'} (project={LISA_PROJECT_NAME})"
            if _agent_mode_configured() else None
        ),
        "avatar": f"{AVATAR_CHARACTER}/{AVATAR_STYLE}",
        "avatar_video_enabled": ENABLE_AVATAR_VIDEO,
        "avatar_video_fps": VIDEO_FPS,
        "use_pts": USE_PTS,
        "latency_diag": LATENCY_DIAG,
        "avatar_background": effective_avatar_background,
        "avatar_background_color": effective_avatar_background_color or None,
        "avatar_background_color_sent": effective_avatar_background_color_sent,
        "avatar_background_image_url_configured": bool(AVATAR_BACKGROUND_IMAGE_URL),
        "background_compositing": background_compositing,
    }


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # ws_ping_interval=None disables uvicorn's WebSocket keepalive ping which
    # has a known AssertionError bug in websockets/legacy/protocol.py:308 that
    # half-breaks the connection (sends stall, audio stops flowing to VL).
    # ws="wsproto" switches off the buggy 'websockets' implementation entirely.
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=5001,
        ws="wsproto",
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )
