"""Smoke test for the in-process fMP4 PTS extractor (Phase 2).

Synthesizes a minimal but spec-valid ISO BMFF byte stream:

  ftyp                               (skipped by parser)
  moov
    trak
      tkhd          track_id=1
      mdia
        mdhd        timescale=90000
        hdlr        handler_type='vide'
  moof  (fragment 1)
    traf
      tfhd          track_id=1
      tfdt          base_media_decode_time=0
      trun          3 samples, sample_duration=3000 each
  moof  (fragment 2)
    traf
      tfhd          track_id=1
      tfdt          base_media_decode_time=9000
      trun          2 samples, sample_duration=3000 each, last sample CTO=1500

Then validates:
  - timescale parsed (1/90000)
  - 5 PTS values extracted in [0, 3000, 6000, 9000, 13500] order
  - B-frame canary triggered by the non-zero CTO on sample 5
  - monotonic + negative-drop counters at 0
  - empty-pop canary triggers when popping past the last queued PTS

Run from repo root:
    python -m pytest my-echobot-repo/avatar-sidecar/tests/test_fmp4_pts.py
or standalone:
    python my-echobot-repo/avatar-sidecar/tests/test_fmp4_pts.py
"""
from __future__ import annotations

import os
import struct
import sys

# Allow `python tests/test_fmp4_pts.py` from the avatar-sidecar dir.
HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.dirname(HERE)
if SIDECAR not in sys.path:
    sys.path.insert(0, SIDECAR)

# Avoid importing the whole FastAPI app on a test run. We import the parser
# directly via a tiny exec trick: read main.py, execute only the module-top
# imports + the two parser classes. The classes are self-contained.
def _load_extractor():
    src_path = os.path.join(SIDECAR, "main.py")
    with open(src_path, "r", encoding="utf-8") as f:
        text = f.read()
    # Slice from the parser banner to (just before) the next banner.
    start = text.index("# ── Phase 2: PTS extraction")
    end = text.index("# ── Session config builders", start)
    snippet = text[start:end]
    # Provide the symbols the snippet uses.
    import logging
    import queue as _queue
    from typing import Optional
    ns: dict = {
        "__name__": "fmp4_test_ns",
        "logger": logging.getLogger("fmp4_test"),
        "queue": _queue,
        "Optional": Optional,
        "FMP4_PTS_BUFFER_MAX_BYTES": 4 * 1024 * 1024,
    }
    exec(snippet, ns)
    return ns["Fmp4PtsExtractor"], ns["AudioPtsClock"]


# ── ISO BMFF synth helpers ──────────────────────────────────────────────────

def _box(btype: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), btype) + payload


def _full(version: int, flags: int) -> bytes:
    return struct.pack(">I", (version << 24) | (flags & 0xFFFFFF))


def _mvhd_skip() -> bytes:
    # We don't parse mvhd — just need the parser to skip it. Use a stub box.
    return b""


def _tkhd(track_id: int) -> bytes:
    # version 0: ts(4) mtime(4) track_id(4) reserved(4) duration(4) ...
    payload = _full(0, 0x000007)
    payload += b"\x00" * 8                     # creation/modification time
    payload += struct.pack(">I", track_id)     # track_id
    payload += b"\x00" * 4                     # reserved
    payload += b"\x00" * 4                     # duration
    payload += b"\x00" * (2 * 4 + 2 + 2 + 2 + 2)  # reserved + layer + altgroup + volume + reserved
    payload += b"\x00\x01\x00\x00" + b"\x00" * 4 + b"\x00" * 4  # matrix row 1
    payload += b"\x00" * 4 + b"\x00\x01\x00\x00" + b"\x00" * 4  # row 2
    payload += b"\x00" * 4 + b"\x00" * 4 + b"\x40\x00\x00\x00"  # row 3
    payload += b"\x00" * 8                     # width + height
    return _box(b"tkhd", payload)


def _mdhd(timescale: int) -> bytes:
    payload = _full(0, 0)
    payload += b"\x00" * 8                     # creation/modification
    payload += struct.pack(">I", timescale)
    payload += b"\x00" * 4                     # duration
    payload += b"\x55\xc4"                     # language ('und')
    payload += b"\x00\x00"                     # pre-defined
    return _box(b"mdhd", payload)


def _hdlr(handler_type: bytes) -> bytes:
    payload = _full(0, 0)
    payload += b"\x00" * 4                     # pre-defined
    payload += handler_type                    # handler_type ('vide')
    payload += b"\x00" * 12                    # reserved
    payload += b"\x00"                         # name (empty c-string)
    return _box(b"hdlr", payload)


def _trak_video(track_id: int, timescale: int) -> bytes:
    mdia = _box(b"mdia", _mdhd(timescale) + _hdlr(b"vide"))
    return _box(b"trak", _tkhd(track_id) + mdia)


def _moov(track_id: int, timescale: int) -> bytes:
    return _box(b"moov", _trak_video(track_id, timescale))


def _tfhd(track_id: int) -> bytes:
    # flags=0 (no defaults set) — duration comes from trun per-sample.
    return _box(b"tfhd", _full(0, 0) + struct.pack(">I", track_id))


def _tfdt(base: int) -> bytes:
    # version 1 → 64-bit base_media_decode_time
    return _box(b"tfdt", _full(1, 0) + struct.pack(">Q", base))


def _trun(samples: list[tuple[int, int]]) -> bytes:
    """samples = [(duration, cto), ...]. Sets flags 0x100 (sample_duration)
    and 0x800 (composition_time_offset).
    """
    flags = 0x000100 | 0x000800
    body = _full(0, flags)
    body += struct.pack(">I", len(samples))
    for dur, cto in samples:
        body += struct.pack(">I", dur)
        body += struct.pack(">i", cto)
    return _box(b"trun", body)


def _moof(track_id: int, base: int, samples: list[tuple[int, int]]) -> bytes:
    traf = _box(b"traf", _tfhd(track_id) + _tfdt(base) + _trun(samples))
    return _box(b"moof", traf)


# ── Test ────────────────────────────────────────────────────────────────────

def main() -> int:
    Fmp4PtsExtractor, AudioPtsClock = _load_extractor()

    stream = b""
    stream += _box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2avc1mp41")
    stream += _moov(track_id=1, timescale=90000)
    stream += _moof(1, 0, [(3000, 0), (3000, 0), (3000, 0)])
    stream += _moof(1, 9000, [(3000, 0), (3000, 1500)])  # last sample has CTO

    ext = Fmp4PtsExtractor()
    ext.feed(stream)

    tb_num, tb_den = ext.video_time_base
    assert (tb_num, tb_den) == (1, 90000), f"time_base={tb_num}/{tb_den}"

    pts_list: list[int] = []
    while True:
        v = ext.pop_video_pts()
        if v is None:
            break
        pts_list.append(v)

    expected = [0, 3000, 6000, 9000, 13500]  # 12000 + 1500 CTO on sample 5
    assert pts_list == expected, f"pts_list={pts_list} expected={expected}"

    assert ext._b_frame_warned, "B-frame canary should have fired (CTO=1500)"
    assert ext._monotonic_clamps == 0
    assert ext._negative_drops == 0
    assert ext._extracted_count == 5
    assert ext._popped_count == 5

    # The while-loop above already did one empty pop (the loop terminator).
    # That counts as one §9.5 canary tick. Pop once more → second tick.
    assert ext._empty_pops == 1, f"after drain: empty_pops={ext._empty_pops}"
    none_v = ext.pop_video_pts()
    assert none_v is None
    assert ext._empty_pops == 2

    # AudioPtsClock sanity.
    clk = AudioPtsClock()
    assert clk.time_base == (1, 16000)
    assert clk.next_pts(640) == 0      # first chunk: starts at 0
    assert clk.next_pts(640) == 320    # second: 640 bytes = 320 samples later
    assert clk.next_pts(0) == 640      # zero-byte chunk doesn't advance
    assert clk.next_pts(640) == 640

    print("OK: fMP4 PTS extractor + AudioPtsClock smoke test passed")
    print(f"     video pts: {pts_list}")
    print(f"     b_frame_canary={ext._b_frame_warned} empty_pops={ext._empty_pops}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
