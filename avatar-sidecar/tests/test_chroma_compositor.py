from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Optional

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
SIDECAR = os.path.dirname(HERE)
if SIDECAR not in sys.path:
    sys.path.insert(0, SIDECAR)


def _load_chroma_symbols():
    src_path = os.path.join(SIDECAR, "main.py")
    with open(src_path, "r", encoding="utf-8") as handle:
        text = handle.read()
    start = text.index("# ── Chroma-key background compositing")
    end = text.index("# ── FFmpeg video decoder", start)
    snippet = text[start:end]
    ns = {
        "__name__": "chroma_test_ns",
        "logger": logging.getLogger("chroma_test"),
        "np": np,
        "Optional": Optional,
        "subprocess": subprocess,
    }
    exec(snippet, ns)
    return ns


def test_chroma_compositor_replaces_green_background_and_preserves_subject():
    symbols = _load_chroma_symbols()
    rgb_to_nv12 = symbols["_rgb_to_nv12"]
    nv12_to_rgb = symbols["_nv12_to_rgb"]
    Compositor = symbols["ChromaKeyBackgroundCompositor"]

    width = 8
    height = 8
    chroma = np.array([0, 255, 0], dtype=np.uint8)
    subject = np.array([220, 40, 30], dtype=np.uint8)
    background_color = np.array([20, 80, 210], dtype=np.uint8)

    avatar_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    avatar_rgb[:, :] = chroma
    avatar_rgb[2:6, 2:6] = subject

    background_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    background_rgb[:, :] = background_color

    frame = rgb_to_nv12(avatar_rgb)
    compositor = Compositor(
        enabled=True,
        image_path="",
        chroma_color="#00ff00",
        tolerance=70,
        background_rgb=background_rgb,
    )

    composited = compositor.composite_nv12(frame, width, height)
    assert len(composited) == width * height * 3 // 2

    out_rgb = nv12_to_rgb(composited, width, height)
    np.testing.assert_allclose(out_rgb[0, 0], background_color, atol=12)
    assert out_rgb[3, 3, 0] > 170
    assert out_rgb[3, 3, 1] < 90
    assert out_rgb[3, 3, 2] < 90


def test_chroma_compositor_preserves_pale_blue_clothing():
    symbols = _load_chroma_symbols()
    rgb_to_nv12 = symbols["_rgb_to_nv12"]
    nv12_to_rgb = symbols["_nv12_to_rgb"]
    Compositor = symbols["ChromaKeyBackgroundCompositor"]

    width = 8
    height = 8
    chroma = np.array([0, 255, 0], dtype=np.uint8)
    shirt = np.array([190, 225, 245], dtype=np.uint8)
    background_color = np.array([20, 80, 210], dtype=np.uint8)

    avatar_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    avatar_rgb[:, :] = chroma
    avatar_rgb[2:6, 2:6] = shirt

    background_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    background_rgb[:, :] = background_color

    frame = rgb_to_nv12(avatar_rgb)
    compositor = Compositor(
        enabled=True,
        image_path="",
        chroma_color="#00ff00",
        tolerance=45,
        background_rgb=background_rgb,
    )

    composited = compositor.composite_nv12(frame, width, height)
    out_rgb = nv12_to_rgb(composited, width, height)

    np.testing.assert_allclose(out_rgb[0, 0], background_color, atol=12)
    np.testing.assert_allclose(out_rgb[3, 3], shirt, atol=35)


def test_chroma_compositor_removes_green_avatar_edge_halo():
    symbols = _load_chroma_symbols()
    rgb_to_nv12 = symbols["_rgb_to_nv12"]
    nv12_to_rgb = symbols["_nv12_to_rgb"]
    Compositor = symbols["ChromaKeyBackgroundCompositor"]

    width = 8
    height = 8
    chroma = np.array([0, 255, 0], dtype=np.uint8)
    hair = np.array([35, 28, 24], dtype=np.uint8)
    green_halo = np.array([35, 170, 35], dtype=np.uint8)
    background_color = np.array([20, 80, 210], dtype=np.uint8)

    avatar_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    avatar_rgb[:, :] = chroma
    avatar_rgb[2:6, 2:6] = hair
    avatar_rgb[1, 2:6] = green_halo
    avatar_rgb[6, 2:6] = green_halo
    avatar_rgb[2:6, 1] = green_halo
    avatar_rgb[2:6, 6] = green_halo

    background_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    background_rgb[:, :] = background_color

    frame = rgb_to_nv12(avatar_rgb)
    compositor = Compositor(
        enabled=True,
        image_path="",
        chroma_color="#00ff00",
        tolerance=45,
        green_margin=35,
        edge_softness=55,
        background_rgb=background_rgb,
    )

    composited = compositor.composite_nv12(frame, width, height)
    out_rgb = nv12_to_rgb(composited, width, height)

    assert out_rgb[1, 3, 1] < 120
    assert out_rgb[1, 3, 2] > 140
    assert out_rgb[3, 3, 1] < 70
    assert out_rgb[3, 3, 2] < 70


def test_chroma_compositor_matte_erode_shrinks_foreground_mask():
    symbols = _load_chroma_symbols()
    rgb_to_nv12 = symbols["_rgb_to_nv12"]
    nv12_to_rgb = symbols["_nv12_to_rgb"]
    Compositor = symbols["ChromaKeyBackgroundCompositor"]

    width = 10
    height = 10
    chroma = np.array([0, 255, 0], dtype=np.uint8)
    subject = np.array([220, 40, 30], dtype=np.uint8)
    background_color = np.array([20, 80, 210], dtype=np.uint8)

    avatar_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    avatar_rgb[:, :] = chroma
    avatar_rgb[2:8, 2:8] = subject

    background_rgb = np.zeros((height, width, 3), dtype=np.uint8)
    background_rgb[:, :] = background_color

    frame = rgb_to_nv12(avatar_rgb)
    no_erode = Compositor(
        enabled=True,
        image_path="",
        chroma_color="#00ff00",
        tolerance=70,
        matte_erode_px=0,
        background_rgb=background_rgb,
    )
    compositor = Compositor(
        enabled=True,
        image_path="",
        chroma_color="#00ff00",
        tolerance=70,
        matte_erode_px=1,
        background_rgb=background_rgb,
    )

    no_erode_rgb = nv12_to_rgb(no_erode.composite_nv12(frame, width, height), width, height)
    composited = compositor.composite_nv12(frame, width, height)
    out_rgb = nv12_to_rgb(composited, width, height)

    assert out_rgb[2, 4, 0] < no_erode_rgb[2, 4, 0]
    assert out_rgb[2, 4, 2] > no_erode_rgb[2, 4, 2]
    assert out_rgb[4, 4, 0] > 170
    assert out_rgb[4, 4, 1] < 90
    assert out_rgb[4, 4, 2] < 90
    assert compositor.stats()["matte_erode_px"] == 1


def test_chroma_compositor_disabled_returns_original_frame():
    symbols = _load_chroma_symbols()
    rgb_to_nv12 = symbols["_rgb_to_nv12"]
    Compositor = symbols["ChromaKeyBackgroundCompositor"]

    avatar_rgb = np.full((4, 4, 3), [0, 255, 0], dtype=np.uint8)
    frame = rgb_to_nv12(avatar_rgb)
    compositor = Compositor(
        enabled=False,
        image_path="",
        chroma_color="#00ff00",
        tolerance=70,
        background_rgb=np.full((4, 4, 3), [10, 20, 30], dtype=np.uint8),
    )

    assert compositor.composite_nv12(frame, 4, 4) == frame


def test_chroma_compositor_bypasses_frame_when_no_key_color_matches():
    symbols = _load_chroma_symbols()
    rgb_to_nv12 = symbols["_rgb_to_nv12"]
    Compositor = symbols["ChromaKeyBackgroundCompositor"]

    avatar_rgb = np.full((4, 4, 3), [12, 24, 48], dtype=np.uint8)
    frame = rgb_to_nv12(avatar_rgb)
    compositor = Compositor(
        enabled=True,
        image_path="",
        chroma_color="#00ff00",
        tolerance=70,
        background_rgb=np.full((4, 4, 3), [10, 20, 30], dtype=np.uint8),
    )

    assert compositor.composite_nv12(frame, 4, 4) == frame
    assert compositor.frames_bypassed_no_chroma == 1
    assert compositor.frames_composited == 0
    assert compositor.last_composited is False


def test_chroma_color_is_normalized_for_voicelive_rgba():
    symbols = _load_chroma_symbols()

    normalize = symbols["_normalize_voicelive_background_color"]
    parse_rgb = symbols["_parse_hex_rgb"]

    assert normalize("#00ff00") == "#00FF00FF"
    assert normalize("00ff0080") == "#00FF0080"
    assert parse_rgb("#00FF00FF") == (0, 255, 0)
