"""재생 선버퍼와 늦은 프레임 시각. 브라우저는 띄우지 않는다."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src" / "voice_pipeline" / "web" / "static" / "app.js"


def _plan() -> dict:
    text = APP.read_text(encoding="utf-8")
    start = text.index("function shouldHold")
    end = text.index("function applyEdgeFade")
    src = text[start:end]
    pre = re.search(r"const PREBUFFER_SEC = ([0-9.]+)", text).group(1)
    fade = re.search(r"const EDGE_FADE_SEC = ([0-9.]+)", text).group(1)
    script = f"const PREBUFFER_SEC = {pre};\nconst EDGE_FADE_SEC = {fade};\n" + src + """
process.stdout.write(JSON.stringify({
  pre: PREBUFFER_SEC,
  fade: EDGE_FADE_SEC,
  holdShort: shouldHold(0.10, false),
  holdReady: shouldHold(0.20, false),
  holdEnd: shouldHold(0.05, true),
  first: planFrameStart({primed:false, cursor:0}, 0.02, 10),
  ontime: planFrameStart({primed:true, cursor:10}, 0.02, 1),
  late: planFrameStart({primed:true, cursor:5}, 0.02, 5.1)
}));
"""
    out = subprocess.check_output(["node", "-e", script], text=True)
    return json.loads(out)


def test_prebuffer_is_150_to_250ms_and_late_frame_has_no_gap():
    got = _plan()
    assert 0.15 <= got["pre"] <= 0.25
    assert got["holdShort"] is True
    assert got["holdReady"] is False
    assert got["holdEnd"] is False
    assert got["first"]["startAt"] == 10.02
    assert got["first"]["fadeOut"] is True
    assert got["ontime"]["startAt"] == 10 - got["fade"]
    assert got["ontime"]["startAt"] < 10
    assert got["ontime"]["fadeIn"] is True
    assert got["late"]["startAt"] == 5.1
    assert got["late"]["fadeIn"] is True
    assert got["late"]["startAt"] != 5.1 + 0.02
    body = APP.read_text(encoding="utf-8")
    assert "else if (t < now + 0.005)" not in body
    assert "flushQueue(cur, false)" in body
    assert "flushQueue(t, true)" in body
