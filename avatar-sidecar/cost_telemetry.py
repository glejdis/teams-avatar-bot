"""VENDORED COPY of ``core/cost.py`` from the parent teams_avatar_voice_live repo.

The avatar sidecar deploys as a self-contained app (the VMSS hot-patch path
ships only ``main.py`` + sibling modules), so it cannot import the parent
repo's ``core`` package. This file is a verbatim copy — KEEP IT IN SYNC with
``core/cost.py`` when rates, the record schema, or the sink change.

----------------------------------------------------------------------------

Shared cost metering + persistence for teams-avatar-voice-live.

A single source of truth for *what a call costs* so every transport — the
``browser-fallback`` FastAPI app, the production VMSS sidecar, and (optionally)
the Foundry hosted-agent — meters usage the same way and writes the same record
shape to the same Azure store.

Three pieces:

- :func:`cost_rates` — per-unit USD rates (Voice Live Pro, avatar, vision, ACS),
  all overridable via ``COST_*`` env vars. Verified against Azure list prices
  (2026-06); see inline notes.
- :class:`CostMeter` — accumulates usage for one session and renders an
  estimated USD breakdown (the live snapshot the browser panel shows).
- :class:`CostRecord` + :func:`build_sink_from_env` / :class:`TableCostSink` —
  the persisted per-call record and the Azure Table Storage writer. The sink is
  deliberately fail-soft: if it is not configured (or the optional
  ``azure-data-tables`` dependency is missing) it degrades to a no-op so a
  storage hiccup never takes down a live call.

The VMSS sidecar (``bot/`` submodule) is expected to import :class:`CostMeter`
and :func:`build_sink_from_env` from here and call
``await sink.write(record)`` when its Graph call ends, exactly like
``browser-fallback/app.py`` does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("core.cost")

SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# Rates
# ─────────────────────────────────────────────────────────────────────────────
def cost_env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def cost_rates() -> dict[str, float]:
    """Per-unit cost rates in USD, all overridable via ``COST_*`` env vars.

    Values reflect Azure public list prices (verified 2026-06, East US 2, PAYG).
    Sources:
      - Voice Live Pro (gpt-realtime-1.5 falls in the Pro tier):
        https://azure.microsoft.com/pricing/details/cognitive-services/speech-services/
      - ACS VoIP / Teams interop:
        https://azure.microsoft.com/pricing/details/communication-services/
    The avatar per-minute rate is the one figure Azure renders dynamically and
    that we could not pin exactly; it remains a labeled estimate. Tune any of
    these per your enterprise agreement; the UI labels the figure as an estimate.
    """
    return {
        # Voice Live Pro — native speech-to-speech audio, billed per 1M tokens.
        # Confirmed: audio in $32 / out $64, text in $4 / out $16.
        "rtAudioInPerMTok": cost_env_float("COST_RT_AUDIO_INPUT_PER_MTOKEN", 32.0),
        "rtAudioOutPerMTok": cost_env_float("COST_RT_AUDIO_OUTPUT_PER_MTOKEN", 64.0),
        "rtTextInPerMTok": cost_env_float("COST_RT_TEXT_INPUT_PER_MTOKEN", 4.0),
        "rtTextOutPerMTok": cost_env_float("COST_RT_TEXT_OUTPUT_PER_MTOKEN", 16.0),
        # Optional flat per-minute add-on for the realtime/Voice Live session.
        "voiceLivePerMin": cost_env_float("COST_VOICELIVE_PER_MIN", 0.0),
        # Avatar real-time video synthesis (TTS Avatar 'interactive (real-time)'
        # SKU) — per minute of avatar streaming. ESTIMATE: Azure renders this
        # value dynamically; set COST_AVATAR_PER_MIN to your invoiced rate.
        "avatarPerMin": cost_env_float("COST_AVATAR_PER_MIN", 0.30),
        # Vision / LLM model (gpt-4.1-mini) — billed per 1M tokens ($0.40 in / $1.60 out).
        "visionInPerMTok": cost_env_float("COST_VISION_INPUT_PER_MTOKEN", 0.40),
        "visionOutPerMTok": cost_env_float("COST_VISION_OUTPUT_PER_MTOKEN", 1.60),
        # ACS VoIP / Teams interop — $0.004 per ACS-user-minute (the Teams-side
        # participant is free / covered by M365). Teams calls only.
        "acsPerMin": cost_env_float("COST_ACS_PER_MIN", 0.004),
    }


def _usage_get(obj: Any, *names: str) -> Any:
    """Read an attribute or dict key from heterogeneous SDK usage objects."""
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return obj.get(name)
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Meter
# ─────────────────────────────────────────────────────────────────────────────
class CostMeter:
    """Accumulates resource usage for a single Voice Live session and turns it
    into an estimated USD cost breakdown (Voice Live realtime model, avatar,
    vision LLM, ACS)."""

    def __init__(self, *, is_teams: bool) -> None:
        self.is_teams = is_teams
        self.started = time.monotonic()
        self.ended: Optional[float] = None
        self.rt_audio_in = 0
        self.rt_audio_out = 0
        self.rt_text_in = 0
        self.rt_text_out = 0
        self.vision_calls = 0
        self.vision_in = 0
        self.vision_out = 0

    def reset_clock(self) -> None:
        self.started = time.monotonic()

    def mark_ended(self) -> None:
        if self.ended is None:
            self.ended = time.monotonic()

    def elapsed_seconds(self) -> float:
        end = self.ended if self.ended is not None else time.monotonic()
        return max(0.0, end - self.started)

    def add_realtime_usage(self, usage: Any) -> None:
        if usage is None:
            return
        in_details = _usage_get(usage, "input_token_details", "input_tokens_details")
        out_details = _usage_get(usage, "output_token_details", "output_tokens_details")
        if in_details is not None:
            self.rt_audio_in += int(_usage_get(in_details, "audio_tokens", "audio") or 0)
            self.rt_text_in += int(_usage_get(in_details, "text_tokens", "text") or 0)
        elif _usage_get(usage, "input_tokens") is not None:
            # No per-modality split available; attribute to text input.
            self.rt_text_in += int(_usage_get(usage, "input_tokens") or 0)
        if out_details is not None:
            self.rt_audio_out += int(_usage_get(out_details, "audio_tokens", "audio") or 0)
            self.rt_text_out += int(_usage_get(out_details, "text_tokens", "text") or 0)
        elif _usage_get(usage, "output_tokens") is not None:
            self.rt_text_out += int(_usage_get(usage, "output_tokens") or 0)

    def add_vision_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.vision_calls += 1
        self.vision_in += max(0, int(prompt_tokens or 0))
        self.vision_out += max(0, int(completion_tokens or 0))

    def snapshot(self, rates: dict[str, float]) -> dict[str, Any]:
        seconds = self.elapsed_seconds()
        minutes = seconds / 60.0
        realtime_cost = (
            self.rt_audio_in / 1_000_000 * rates["rtAudioInPerMTok"]
            + self.rt_audio_out / 1_000_000 * rates["rtAudioOutPerMTok"]
            + self.rt_text_in / 1_000_000 * rates["rtTextInPerMTok"]
            + self.rt_text_out / 1_000_000 * rates["rtTextOutPerMTok"]
            + minutes * rates["voiceLivePerMin"]
        )
        avatar_cost = minutes * rates["avatarPerMin"]
        vision_cost = (
            self.vision_in / 1_000_000 * rates["visionInPerMTok"]
            + self.vision_out / 1_000_000 * rates["visionOutPerMTok"]
        )
        acs_cost = minutes * rates["acsPerMin"] if self.is_teams else 0.0
        total = realtime_cost + avatar_cost + vision_cost + acs_cost
        return {
            "channel": "teams" if self.is_teams else "page",
            "elapsedSeconds": round(seconds, 1),
            "ended": self.ended is not None,
            "totalUsd": round(total, 6),
            "components": {
                "realtime": {
                    "label": "Voice Live realtime model",
                    "costUsd": round(realtime_cost, 6),
                    "audioInTokens": self.rt_audio_in,
                    "audioOutTokens": self.rt_audio_out,
                    "textInTokens": self.rt_text_in,
                    "textOutTokens": self.rt_text_out,
                },
                "avatar": {
                    "label": "Avatar (real-time video)",
                    "costUsd": round(avatar_cost, 6),
                    "minutes": round(minutes, 3),
                },
                "vision": {
                    "label": "Vision LLM (gpt-4.1-mini)",
                    "costUsd": round(vision_cost, 6),
                    "calls": self.vision_calls,
                    "inTokens": self.vision_in,
                    "outTokens": self.vision_out,
                },
                "acs": {
                    "label": "ACS / Teams interop",
                    "costUsd": round(acs_cost, 6),
                    "minutes": round(minutes, 3) if self.is_teams else 0.0,
                    "applicable": self.is_teams,
                },
            },
        }


# ─────────────────────────────────────────────────────────────────────────────
# Persisted record
# ─────────────────────────────────────────────────────────────────────────────
def _iso(ts: Optional[datetime] = None) -> str:
    return (ts or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


@dataclass
class CostRecord:
    """One per-call cost record — the flat shape written to the Azure store and
    read back by the dashboard. Built from a :class:`CostMeter` snapshot plus
    session metadata."""

    runId: str
    schemaVersion: int = SCHEMA_VERSION
    transport: str = "browser"          # browser | vmss | hosted-agent
    channel: str = "page"               # page | teams (from the meter snapshot)
    persona: str = ""
    model: str = ""
    meetingId: str = ""
    startedAt: str = field(default_factory=_iso)
    endedAt: str = field(default_factory=_iso)
    elapsedSeconds: float = 0.0
    totalUsd: float = 0.0
    realtimeUsd: float = 0.0
    avatarUsd: float = 0.0
    visionUsd: float = 0.0
    acsUsd: float = 0.0
    rtAudioInTokens: int = 0
    rtAudioOutTokens: int = 0
    rtTextInTokens: int = 0
    rtTextOutTokens: int = 0
    avatarMinutes: float = 0.0
    visionCalls: int = 0
    visionInTokens: int = 0
    visionOutTokens: int = 0
    acsMinutes: float = 0.0
    host: str = field(default_factory=socket.gethostname)
    region: str = ""
    ratesJson: str = ""

    @classmethod
    def from_snapshot(
        cls,
        run_id: str,
        snapshot: dict[str, Any],
        *,
        transport: str = "browser",
        persona: str = "",
        model: str = "",
        meeting_id: str = "",
        started_at: Optional[str] = None,
        region: str = "",
        rates: Optional[dict[str, float]] = None,
    ) -> "CostRecord":
        comp = snapshot.get("components", {})
        rt = comp.get("realtime", {})
        av = comp.get("avatar", {})
        vis = comp.get("vision", {})
        acs = comp.get("acs", {})
        return cls(
            runId=run_id,
            transport=transport,
            channel=str(snapshot.get("channel", "page")),
            persona=persona,
            model=model,
            meetingId=meeting_id,
            startedAt=started_at or _iso(),
            endedAt=_iso(),
            elapsedSeconds=float(snapshot.get("elapsedSeconds", 0.0)),
            totalUsd=float(snapshot.get("totalUsd", 0.0)),
            realtimeUsd=float(rt.get("costUsd", 0.0)),
            avatarUsd=float(av.get("costUsd", 0.0)),
            visionUsd=float(vis.get("costUsd", 0.0)),
            acsUsd=float(acs.get("costUsd", 0.0)),
            rtAudioInTokens=int(rt.get("audioInTokens", 0)),
            rtAudioOutTokens=int(rt.get("audioOutTokens", 0)),
            rtTextInTokens=int(rt.get("textInTokens", 0)),
            rtTextOutTokens=int(rt.get("textOutTokens", 0)),
            avatarMinutes=float(av.get("minutes", 0.0)),
            visionCalls=int(vis.get("calls", 0)),
            visionInTokens=int(vis.get("inTokens", 0)),
            visionOutTokens=int(vis.get("outTokens", 0)),
            acsMinutes=float(acs.get("minutes", 0.0)),
            region=region,
            ratesJson=json.dumps(rates or {}, separators=(",", ":")),
        )

    def partition_key(self) -> str:
        """YYYY-MM bucket from startedAt; groups a month of runs together."""
        try:
            return self.startedAt[:7]
        except Exception:
            return _iso()[:7]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_entity(self) -> dict[str, Any]:
        entity = self.to_dict()
        entity["PartitionKey"] = self.partition_key()
        entity["RowKey"] = self.runId
        return entity


# ─────────────────────────────────────────────────────────────────────────────
# Sink (Azure Table Storage, fail-soft)
# ─────────────────────────────────────────────────────────────────────────────
class CostSink:
    """Base sink. The default does nothing (used when persistence is off)."""

    enabled = False

    async def write(self, record: CostRecord) -> bool:
        return False

    async def close(self) -> None:  # pragma: no cover - trivial
        return None


class NullCostSink(CostSink):
    enabled = False


class TableCostSink(CostSink):
    """Writes :class:`CostRecord` rows to Azure Table Storage.

    Auth precedence: connection string → account key → AAD (Managed Identity /
    ``DefaultAzureCredential``). The synchronous ``azure-data-tables`` client is
    invoked via :func:`asyncio.to_thread` so a slow write never blocks the call.
    """

    enabled = True

    def __init__(
        self,
        *,
        table_name: str,
        endpoint: Optional[str] = None,
        connection_string: Optional[str] = None,
        account_key: Optional[str] = None,
    ) -> None:
        self.table_name = table_name
        self.endpoint = endpoint
        self.connection_string = connection_string
        self.account_key = account_key
        self._table = None  # lazily created on first write

    def _ensure_table(self):
        if self._table is not None:
            return self._table
        from azure.data.tables import TableServiceClient  # imported lazily

        if self.connection_string:
            svc = TableServiceClient.from_connection_string(self.connection_string)
        elif self.account_key and self.endpoint:
            from azure.core.credentials import AzureNamedKeyCredential

            account_name = self.endpoint.split("//", 1)[-1].split(".", 1)[0]
            cred = AzureNamedKeyCredential(account_name, self.account_key)
            svc = TableServiceClient(endpoint=self.endpoint, credential=cred)
        else:
            from azure.identity import DefaultAzureCredential

            tenant = os.getenv("AZURE_TENANT_ID", "").strip() or None
            cred = DefaultAzureCredential(
                exclude_interactive_browser_credential=True,
                **({"interactive_browser_tenant_id": tenant} if tenant else {}),
            )
            svc = TableServiceClient(endpoint=self.endpoint, credential=cred)
        # Create-if-missing keeps first-run friction low; ignore "already exists".
        try:
            svc.create_table_if_not_exists(self.table_name)
        except Exception:  # pragma: no cover - permission/race tolerant
            logger.debug("create_table_if_not_exists(%s) skipped", self.table_name, exc_info=True)
        self._table = svc.get_table_client(self.table_name)
        return self._table

    def _write_sync(self, record: CostRecord) -> None:
        table = self._ensure_table()
        table.upsert_entity(record.to_entity())

    async def write(self, record: CostRecord) -> bool:
        try:
            await asyncio.to_thread(self._write_sync, record)
            logger.info(
                "cost record persisted run=%s transport=%s total=$%.4f",
                record.runId, record.transport, record.totalUsd,
            )
            return True
        except Exception:
            # Never let cost persistence break a call; just log and move on.
            logger.warning("cost record persist failed run=%s", record.runId, exc_info=True)
            return False


def build_sink_from_env() -> CostSink:
    """Construct a :class:`CostSink` from ``COST_STORE_*`` env vars.

    Returns a :class:`NullCostSink` (no-op) unless persistence is explicitly
    configured, so existing local runs are unaffected.

    Env:
      COST_STORE_KIND               table | none   (default: table if an account/
                                                     conn-string is present, else none)
      COST_STORE_ACCOUNT            storage account name (→ table endpoint)
      COST_STORE_ENDPOINT           explicit table endpoint (overrides ACCOUNT)
      COST_STORE_TABLE              table name (default: callcosts)
      COST_STORE_CONNECTION_STRING  full connection string (overrides AAD)
      COST_STORE_KEY                account key (used with ACCOUNT/ENDPOINT)
    """
    kind = os.getenv("COST_STORE_KIND", "").strip().lower()
    account = os.getenv("COST_STORE_ACCOUNT", "").strip()
    endpoint = os.getenv("COST_STORE_ENDPOINT", "").strip()
    conn = os.getenv("COST_STORE_CONNECTION_STRING", "").strip()
    key = os.getenv("COST_STORE_KEY", "").strip()
    table = os.getenv("COST_STORE_TABLE", "callcosts").strip() or "callcosts"

    if kind == "none":
        return NullCostSink()
    if not (account or endpoint or conn):
        # Nothing configured → stay a no-op.
        return NullCostSink()
    if endpoint == "" and account:
        endpoint = f"https://{account}.table.core.windows.net"
    try:
        return TableCostSink(
            table_name=table,
            endpoint=endpoint or None,
            connection_string=conn or None,
            account_key=key or None,
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning("cost sink construction failed; persistence disabled", exc_info=True)
        return NullCostSink()
