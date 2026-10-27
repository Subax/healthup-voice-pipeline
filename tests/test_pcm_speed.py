"""TTS 말 속도. server.py 는 torch 가 없어 함수만 꺼내 검사한다."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "services" / "cosyvoice" / "server.py"


def _load():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "SPEECH_SPEED" for t in node.targets):
            body.append(node)
        if isinstance(node, ast.ClassDef) and node.name == "StreamSpeed":
            body.append(node)
    ns: dict = {"np": np, "os": os}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVER), "exec"), ns)
    return ns["SPEECH_SPEED"], ns["StreamSpeed"]


def test_default_speed_is_in_requested_range():
    speed, _ = _load()
    assert 1.15 <= speed <= 1.25
    text = SERVER.read_text(encoding="utf-8")
    assert "StreamSpeed(SPEECH_SPEED, self.sample_rate)" in text
    assert "stream=True, speed=1.0" in text


def _peak_hz(sig: np.ndarray, sr: int) -> float:
    if sig.size < 64:
        return 0.0
    window = np.hanning(sig.size)
    spec = np.abs(np.fft.rfft(sig * window))
    return float(np.argmax(spec) * sr / sig.size)


def test_stream_speed_keeps_pitch_and_joins_chunks():
    _, StreamSpeed = _load()
    sr = 24000
    t = np.arange(sr, dtype=np.float64) / sr
    x = np.sin(2 * np.pi * 200 * t)
    whole = StreamSpeed(1.2, sr)
    y = np.concatenate([whole.push(x), whole.flush()])
    parts = StreamSpeed(1.2, sr)
    mid = sr // 2
    joined = np.concatenate([parts.push(x[:mid]), parts.push(x[mid:]), parts.flush()])
    n = min(y.size, joined.size)
    assert n > int(sr / 1.2 * 0.85)
    assert np.max(np.abs(y[:n] - joined[:n])) < 1e-4
    assert abs(y.size - x.size / 1.2) / x.size < 0.08
    assert abs(_peak_hz(y, sr) - 200) < 5
    linear = x[::]
    idx = np.arange(0, x.size - 1, 1.2)
    i0 = np.floor(idx).astype(int)
    frac = idx - i0
    raised = (1 - frac) * x[i0] + frac * x[i0 + 1]
    assert abs(_peak_hz(raised, sr) - 240) < 5
    same = StreamSpeed(1.0, sr).push(x[:240])
    assert same.shape == (240,)
    assert np.allclose(same, x[:240])


def test_calm_prompt_uses_normal_pace():
    text = SERVER.read_text(encoding="utf-8")
    calm = text[text.index('"calm"'):text.index('"comfort"')]
    assert "缓慢" not in calm
    assert "平常的语速" in calm
