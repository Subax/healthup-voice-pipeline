"""개발 모드 얼굴 상태 문구. 브라우저는 띄우지 않고 face-hud.js만 검사한다."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUD = ROOT / "src" / "voice_pipeline" / "web" / "static" / "face-hud.js"
APP = ROOT / "src" / "voice_pipeline" / "web" / "static" / "app.js"
HTML = ROOT / "src" / "voice_pipeline" / "web" / "static" / "index.html"


def _node(body: str) -> str:
    return subprocess.check_output(["node", "-e", body], text=True)


def _lines(state: dict) -> dict:
    script = (
        "const hud = require(" + json.dumps(str(HUD)) + ");\n"
        "const state = " + json.dumps(state) + ";\n"
        "process.stdout.write(JSON.stringify(hud.lines(state)));\n"
    )
    return json.loads(_node(script))


def test_face_hud_visible_only_in_dev_mode():
    script = (
        "const hud = require(" + json.dumps(str(HUD)) + ");\n"
        "process.stdout.write(JSON.stringify({"
        "demo: hud.faceHudVisible('demo'),"
        "dev: hud.faceHudVisible('dev'),"
        "other: hud.faceHudVisible('show_dev_info')"
        "}));\n"
    )
    out = json.loads(_node(script))
    assert out == {"demo": False, "dev": True, "other": False}


def test_face_hud_lines_top3_and_summary_text():
    view = _lines({
        "detected": True,
        "score": 0.812,
        "faceCount": 2,
        "emotions": [
            {"label": "SAD", "score": 0.2},
            {"label": "HAPPY", "score": 0.7},
            {"label": "ANGRY", "score": 0.05},
            {"label": "FEARFUL", "score": 0.04},
        ],
        "fps": 8.24,
        "lastSentAt": "05:10:03",
        "lastSent": {
            "label": "HAPPY",
            "conf": 0.7,
            "coverage": 0.5,
            "n_frames": 4,
            "total_frames": 8,
        },
        "engine": "face-api",
        "camera": "허용",
    })
    assert view["detected"] == "있음"
    assert view["score"] == "0.81"
    assert view["count"] == "2"
    assert view["emotions"] == "HAPPY 0.70, SAD 0.20, ANGRY 0.05"
    assert "FEARFUL" not in view["emotions"]
    assert view["fps"] == "8.2 fps"
    assert view["lastSent"].startswith("05:10:03 HAPPY")
    assert "프레임 4/8" in view["lastSent"]
    assert view["engine"] == "face-api"
    assert view["camera"] == "허용"
    blob = json.dumps(view, ensure_ascii=False)
    for bad in ("\u2014", "\u2013", "\u00b7", "data:image", "jpeg"):
        assert bad not in blob


def test_face_hud_empty_state_and_camera_error():
    view = _lines({"detected": False, "camera": "거부", "cameraError": "권한 없음"})
    assert view["detected"] == "없음"
    assert view["score"] == "없음"
    assert view["emotions"] == "없음"
    assert view["lastSent"] == "아직 없음"
    assert view["fps"] == "없음"
    assert view["engine"] == "없음"
    assert view["camera"] == "거부: 권한 없음"


def test_face_hud_markup_stays_hidden_and_sends_no_pixels():
    html = HTML.read_text(encoding="utf-8")
    assert 'id="faceHud" hidden' in html
    assert 'id="faceVideo"' in html
    assert 'id="faceOverlay"' in html
    app = APP.read_text(encoding="utf-8")
    assert "faceHudVisible(currentMode)" in app
    assert "toDataURL" not in app
    assert "MediaRecorder" not in app
    assert 'type: "face_summary"' in app
    send_at = app.index('type: "face_summary"')
    chunk = app[send_at:send_at + 500]
    for banned in ("image", "jpeg", "pixels", "data:"):
        assert banned not in chunk
