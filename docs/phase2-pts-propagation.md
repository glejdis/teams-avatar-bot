# Phase 2 — PTS Propagation (Lipsync)

**Status:** LOCKED v1.2 (2026-05-06).
**Scope:** Carry Voice Live encoder timestamps end-to-end so the .NET bot's
`AudioVideoFramePlayer` can present audio + video on a single timeline.

> **Note on doc lineage:** v1.0/v1.1 lived in conversation threads only. v1.2 is
> the first on-disk version and folds in (a) review feedback that produced v1.1
> and (b) the implementation-time deviation in §4.

---

## Changelog

| Version | Date       | Change |
| ------- | ---------- | ------ |
| v1.0    | 2026-05-05 | Initial design (mpegts side-channel). |
| v1.1    | 2026-05-06 | §3 fallback chain hardened (no-honor-zero); §4 audio-master-clock anchor (Z4); §9.3 first-AV-offset canary; clarified MediaPlatform.GetCurrentTimestamp accessibility limitation (use `DateTime.UtcNow.Ticks`). |
| **v1.2**| **2026-05-06** | **§4 mechanism revised: mpegts infeasible (cannot carry raw NV12 / raw PCM — only encoded elementary streams). Replaced with in-process fMP4 box parser + audio sample clock. Contract unchanged: per-frame PTS in stream-relative units with `tb_num`/`tb_den`. See `Fmp4PtsExtractor` / `AudioPtsClock` in `avatar-sidecar/main.py`.** |

---

## §1 Problem

Sidecar emits `{"type":"video", …, "timestamp":0}` and `{"type":"audio", …}`
with no timestamp. The bot wraps each in an `AudioMediaBuffer` /
`VideoSendBuffer` with `Timestamp = DateTime.Now.Ticks` at receive time.
That destroys encoder-time alignment between audio and video — causing the
robotic-voice / lipsync drift symptoms.

## §2 Goal

Per-buffer `Timestamp` values that share a single timeline, so
`AudioVideoFramePlayer` strategies 1 & 3 (use audio TS as reference; drop
video if ahead of audio) work as documented in
`Microsoft.Skype.Bots.Media.xml`.

## §3 Fallback chain (bot side)

`ResolveTimestamp` in `SpeechService.cs`, applied to incoming audio + video JSON:

1. `pts` field present (any int incl. 0) → convert via `tb_num`/`tb_den`.
2. `timestamp` field present **and non-zero** → use as-is (legacy compat).
3. Else `DateTime.UtcNow.Ticks` + ONE-SHOT canary warning per session.

**Hardened in v1.1:** explicitly **does not** honor `timestamp:0`. The current
sidecar hardcodes `timestamp:0` on video; we treat that as "missing" rather
than "year 1 AD".

## §4 Mechanism (REVISED v1.2)

**Original v1.0/v1.1:** Approach A — pipe a side-channel ffmpeg subprocess in
mpegts mode and parse PTS from the muxed PES.

**Why revised:** mpegts cannot carry raw NV12 video frames or raw PCM audio
samples. Mpegts requires encoded elementary streams (H.264/AAC/MP3/etc.)
in PES packets. Discovered at implementation time.

**v1.2 mechanism:**

- **Video PTS:** in-process minimal ISO BMFF parser
  (`Fmp4PtsExtractor`, ~250 LoC, zero deps). Walks `moov→trak→{tkhd,mdia/{mdhd,hdlr}}`
  to find the video track + timescale; per `moof→traf` decodes
  `tfhd`+`tfdt`+`trun` to enumerate per-sample PTS in presentation order.
  Pushes PTS values to a bounded queue; bot pops one per emitted NV12 frame.
- **Audio PTS:** self-clocked at PCM rate (`AudioPtsClock`, tb=1/16000,
  +320 samples per 640-byte 20 ms chunk). PCM is fixed-rate by definition;
  no muxed-source PTS extraction is needed for **rate** correctness.
  *Anchoring* is discussed in §10 below.

**Why this is better than the original mpegts approach:**

- Zero new Python deps (no PyAV, no extra ffmpeg subprocess).
- ffmpeg subprocess invocations untouched → AAC dither hack at
  `main.py:489` (`aresample=dither_method=triangular_hp`) preserved verbatim.
- Both consumers (extractor + decoder) see the same byte stream in the same
  order → deterministic 1:1 pairing.
- No subprocess crash to handle (parser is pure Python).

## §5 PTS-to-ticks conversion

`ConvertPtsToTicks(pts, tb_num, tb_den)` in `SpeechService.cs`. Uses
`decimal` arithmetic to avoid Int64 overflow (`pts * 10_000_000` overflows
at ~3 hours for tb=1/90000).

## §6 Audio-master-clock anchor (Z4)

The first **audio** buffer that yields a usable PTS sets the wall-clock
origin: `anchor = NOW - ticks(first_audio_pts)`. All subsequent buffers
(audio AND video) compute their `Timestamp` as
`anchor + (this_pts_ticks - first_audio_pts_ticks)`. This matches the
SDK's documented behavior (`VideoPlayerQueue.DequeueAndProcessAsync`
strategy 1 uses audio TS as reference).

Video frames that arrive before any audio fall back to wall-clock NOW —
player strategy 2 paces them at fps cadence; this is fine for the
typically-tiny pre-roll window.

## §7 Wire format additions

Both `audio` and `video` JSON messages now carry (when sidecar's
`LISA_USE_PTS=1`):

```jsonc
{ "type":"video", "data":"...", "width":W, "height":H,
  "timestamp": 0,            // legacy field, retained
  "pts": 1234567,            // stream-relative, signed int64
  "tb_num": 1, "tb_den": 90000 }
```

`pts` value is in units of `tb_num/tb_den` seconds. Audio is always
`tb_num=1, tb_den=16000`. Video tb is `1/<mvhd timescale>` (typically
`1/90000` for Voice Live).

## §8 Feature flag

`LISA_USE_PTS` env var on **both** sidecar (controls emit) and bot (controls
consume). Default `0` = no behavior change (sidecar omits fields; bot
ignores them if present). Flip to `1` only after Phase 1 production
validation. Flag flip requires service restart on each side.

Side effects of asymmetric flag state:

- sidecar=1, bot=0 → bot ignores fields, runs legacy path. Harmless.
- sidecar=0, bot=1 → bot triggers the §3 fallback canary on every buffer.
  Detectable within 1 turn.

## §9 Diagnostics

### §9.1 Per-stream first-10-PTS log
Sidecar logs `fMP4 PTS: video sample[N] pts=…` for first 5 samples and
`audio PTS: chunk[N] pts=…` for first 5 chunks per session. Bot logs
`audio pts[N]=… tb=…/… → ticks=…` (and matching video) for first 10 of each.

### §9.2 Anchor log
Bot logs once per session: `PTS anchor set on first audio.
anchor_ticks=… first_audio_pts_ticks=…`.

### §9.3 First-AV-offset canary
On the first video frame after both anchors are established, the bot logs
`first video frame offset vs wall-clock-NOW = N ms`. **If `|N| > 100ms`
the bot escalates to a WARNING.** This is the fastest signal that
PTS conversion or tb is wrong — surfaces before
`LowOnFrames(Video)` from strategy-3 video drops would.

### §9.4 PTS-fallback canary
ONE-SHOT warning per session if §3 falls through to wall-clock for any
buffer. Detects silent regression (e.g. flag drift, sidecar deploy with
old code).

### §9.5 Desync canary (v1.2)
Sidecar logs warnings when:
- Video PTS queue empty on `pop` → more decoder output than fMP4 samples
  parsed (parser desync or moov not yet seen).
- Bounded queue overflows / drops oldest → bot not consuming frames fast
  enough OR more samples parsed than decoded.
- Non-zero `composition_time_offset` observed in `trun` → indicates
  B-frames (decode order ≠ presentation order). Voice Live's encoder is
  expected to be baseline/main without B-frames; if this fires, the 1:1
  pairing assumption needs revisiting (see §11 risk).

## §10 Audio anchor assumption (open)

`AudioPtsClock` starts at 0. This assumes the first PCM sample emitted by
ffmpeg corresponds to the same encoder-time-zero as the first video frame
PTS — i.e. AAC priming has been stripped by ffmpeg upstream of our PCM
output. **Not verified.**

If wrong, symptom is a constant lipsync offset (audio leading video
by ~88-132 ms — AAC's standard 2112-sample priming), no drift over
session length.

**Decision:** ship v1.2 as-is. The §9.3 canary will fire consistently
across calls if this is the cause. Three remediation options if it is:

- **(a)** Add `LISA_AUDIO_PTS_OFFSET_SAMPLES` env var, tune empirically.
- **(b)** Extend `Fmp4PtsExtractor` to also parse the audio track's
  `traf` and seed `AudioPtsClock` with the first audio sample's PTS
  (converted to 1/16000 base). Cleanest fix.
- **(c)** Defer; revisit if production canary fires.

Plan: monitor canary in Phase 2 production validation; pick (a) or (b)
in a Phase 2.1 patch if needed.

## §11 Known risks

- **B-frames** would break 1:1 index pairing (§9.5 detects). Voice Live
  is expected to use baseline/main H.264; risk is low. Mitigation if
  triggered: pair by an explicit per-frame PTS field instead of by index
  (would require ffmpeg `-show_frames` or a dec-order queue).
- **ffmpeg dropping a frame** (decoder error on corrupted input) also
  desyncs the index. §9.5 first-pop-empty warning catches this within
  one frame; recovery would be to flush both queues at the next keyframe.
- **AAC priming** — see §10.

## §12 Tests

- C# unit test for `ConvertPtsToTicks` (boundary cases, overflow path).
  *Not yet written; tracked as Phase 2 follow-up.*
- Python smoke test for `Fmp4PtsExtractor` box parsing
  (`avatar-sidecar/tests/test_fmp4_pts.py` — synthetic moof bytes,
  validates pts emission order + monotonic clamp + negative-drop).
