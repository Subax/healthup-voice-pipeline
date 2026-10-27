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
    assert "StreamSpeed(SPEECH_SPEED)" in text
    assert "stream=True, speed=1.0" in text


def test_stream_speed_keeps_rate_and_joins_chunks():
    _, StreamSpeed = _load()
    x = np.sin(np.linspace(0, 8, 240, dtype=np.float64))
    whole = StreamSpeed(1.2).push(x)
    parts = StreamSpeed(1.2)
    joined = np.concatenate([parts.push(x[:100]), parts.push(x[100:])])
    n = min(whole.size, joined.size)
    assert n >= int(240 / 1.2) - 2
    assert np.max(np.abs(whole[:n] - joined[:n])) < 1e-6
    same = StreamSpeed(1.0).push(x)
    assert same.shape == x.shape
    assert np.allclose(same, x)
