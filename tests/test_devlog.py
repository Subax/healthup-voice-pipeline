"""B2 개발 로그 파일, 판단 로그 게이트, 보존 주기 삭제, gitignore 검증."""

import asyncio
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import subprocess

import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.bridge.protocol import unpack_frame
from voice_pipeline.llm import MockLLM
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import create_app
from voice_pipeline.web.devlog import DevLogger, purge_old_logs
from voice_pipeline.web.engines import Engines, build_engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT
from voice_pipeline.web.__main__ import build_parser
from voice_pipeline.endpoint.turn import TurnEndpointer, DEFAULT_LOG_DIR


REQUIRED_FIELDS = {
    "ts", "session_id", "utt_id", "turn_id", "mode",
    "transcript", "stt_id", "stt_ms", "emotion", "event",
    "face", "hint_rule", "intent", "llm_text", "spoken_text",
    "interrupted", "trace", "turn_eval_reason", "barge",
}


async def _recv(ws, timeout=5.0):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type == WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type == WSMsgType.BINARY:
        return unpack_frame(msg.data)
    raise AssertionError(f"unexpected msg type: {msg.type}")


async def _until(ws, pred, out):
    while True:
        m = await _recv(ws)
        out.append(m)
        if pred(m):
            return m


def _is_type(t):
    return lambda m: isinstance(m, dict) and m.get("type") == t


@pytest.mark.asyncio
async def test_dev_mode_turn_logging(tmp_path):
    dev_dir = tmp_path / "dev_logs"
    eng = Engines(
        MockLLM(ttft_ms=20, tokens_per_s=400),
        MockTTS(first_audio_ms=10, rtf=0.5),
        MockSTT("안녕하세요"),
        lambda: EnergyVAD(min_silence_ms=100),
        {"llm": "mock", "tts": "mock", "stt": "mock"},
        default_mode="dev",
        dev_log_dir=dev_dir,
    )
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        hello = await _recv(ws)
        assert hello["type"] == "hello"
        assert hello["mode"] == "dev"

        await ws.send_str(json.dumps({"type": "text_input", "text": "안녕하세요"}))
        msgs = []
        done = await _until(ws, _is_type("turn_done"), msgs)
        assert done["type"] == "turn_done"

        # Check logs/dev/YYYYMMDD/<session_id>.jsonl
        date_dirs = list(dev_dir.glob("20*"))
        assert len(date_dirs) == 1, f"Expected 1 date dir in {dev_dir}, got {date_dirs}"
        log_files = list(date_dirs[0].glob("*.jsonl"))
        assert len(log_files) == 1, f"Expected 1 log file, got {log_files}"

        lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])

        # Verify all 19 fields
        assert REQUIRED_FIELDS.issubset(rec.keys()), f"Missing keys: {REQUIRED_FIELDS - set(rec.keys())}"
        assert rec["mode"] == "dev"
        assert rec["transcript"] == "안녕하세요"
        assert isinstance(rec["intent"], dict)
        assert {"intent", "grade", "rule", "classify_ms"}.issubset(rec["intent"].keys())
        assert rec["interrupted"] is False
        assert rec["spoken_text"] != ""
        assert rec["llm_text"] != ""
        assert isinstance(rec["trace"], dict)
        assert isinstance(rec["barge"], list)

        # Prohibited items check: no audio, no video, no raw hint text, no auth tokens
        raw_text = lines[0]
        assert "이번 대답의 참고" not in raw_text
        assert "audio" not in rec or rec["audio"] is None
        assert "video" not in rec or rec["video"] is None
        assert "token" not in rec or rec.get("token") is None
        assert "auth" not in rec

        await ws.close()
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_demo_mode_writes_zero_files(tmp_path):
    dev_dir = tmp_path / "dev_logs"
    eng = Engines(
        MockLLM(ttft_ms=20, tokens_per_s=400),
        MockTTS(first_audio_ms=10, rtf=0.5),
        MockSTT("안녕하세요"),
        lambda: EnergyVAD(min_silence_ms=100),
        {"llm": "mock", "tts": "mock", "stt": "mock"},
        default_mode="demo",
        dev_log_dir=dev_dir,
    )
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        hello = await _recv(ws)
        assert hello["type"] == "hello"
        assert hello["mode"] == "demo"

        # Simulate UI toggling show_dev_info: True
        await ws.send_str(json.dumps({"type": "config", "show_dev_info": True}))
        await ws.send_str(json.dumps({"type": "text_input", "text": "안녕하세요"}))
        msgs = []
        done = await _until(ws, _is_type("turn_done"), msgs)
        assert done["type"] == "turn_done"

        # Check that no files or directories were created
        if dev_dir.exists():
            files = list(dev_dir.rglob("*.jsonl"))
            assert len(files) == 0, f"Expected 0 files in demo mode, found: {files}"

        # Test DevLogger directly with demo mode
        logger = DevLogger(base_dir=dev_dir)
        res = logger.write_turn({"mode": "demo", "session_id": "test_demo", "transcript": "hi"})
        assert res is None
        if dev_dir.exists():
            assert len(list(dev_dir.rglob("*.jsonl"))) == 0

        await ws.close()
    finally:
        await c.close()


def test_endpoint_turn_log_gating(tmp_path):
    """endpoint/turn.py 판단 로그 게이트 검증.
    1. session_mode == 'demo' and log_dir is None -> 파일 쓰지 않음
    2. log_dir 명시 주입(단위 시험 호환) -> session_mode와 무관하게 명시 디렉터리에 씀
    """
    ev = {
        "type": "turn_eval",
        "stage": "confirmed",
        "utt_id": 1,
        "stt_id": "fake",
        "reason": "test",
    }

    # 1. demo mode with default log_dir (None) -> 0 writes
    ep_demo = TurnEndpointer(
        stt=None, scorer=None, emit=lambda ev: None, on_confirm=lambda t: None,
        session_mode="demo", log_dir=None,
    )
    # Check that calling _write_log does not raise and does not write
    before_files = set(DEFAULT_LOG_DIR.glob("*.jsonl")) if DEFAULT_LOG_DIR.exists() else set()
    ep_demo._write_log(ev)
    after_files = set(DEFAULT_LOG_DIR.glob("*.jsonl")) if DEFAULT_LOG_DIR.exists() else set()
    assert before_files == after_files

    # 2. explicit log_dir injected (e.g. in unit tests) -> writes to injected dir regardless
    explicit_dir = tmp_path / "explicit_ep_log"
    ep_explicit = TurnEndpointer(
        stt=None, scorer=None, emit=lambda ev: None, on_confirm=lambda t: None,
        session_mode="demo", log_dir=explicit_dir,
    )
    ep_explicit._write_log(ev)
    written = list(explicit_dir.glob("*.jsonl"))
    assert len(written) == 1
    content = json.loads(written[0].read_text(encoding="utf-8").strip())
    assert content["reason"] == "test"
    assert content["mode"] == "smart"


def test_purge_old_logs(tmp_path):
    base = tmp_path / "dev_purge"
    base.mkdir(parents=True)

    # 1. Old date directory (8 days ago)
    d_old = base / "20261015"
    d_old.mkdir()
    f_old1 = d_old / "sess1.jsonl"
    f_old1.write_text("old1\n", encoding="utf-8")

    # 2. Another old date directory (10 days ago)
    d_older = base / "20261013"
    d_older.mkdir()
    f_old2 = d_older / "sess2.jsonl"
    f_old2.write_text("old2\n", encoding="utf-8")

    # 3. Recent date directory (today)
    d_recent = base / "20261023"
    d_recent.mkdir()
    f_recent = d_recent / "sess3.jsonl"
    f_recent.write_text("recent\n", encoding="utf-8")

    # 4. Folder with mtime older than 7 days
    d_custom = base / "custom"
    d_custom.mkdir()
    f_mtime_old = d_custom / "mtime_old.jsonl"
    f_mtime_old.write_text("mtime_old\n", encoding="utf-8")
    past_ts = time_val = datetime(2026, 10, 10, 12, 0, 0).timestamp()
    os.utime(f_mtime_old, (past_ts, past_ts))

    f_mtime_new = d_custom / "mtime_new.jsonl"
    f_mtime_new.write_text("mtime_new\n", encoding="utf-8")

    now = datetime(2026, 10, 23, 12, 0, 0)
    deleted = purge_old_logs(base, retention_days=7, now=now)

    assert deleted == 3  # f_old1, f_old2, f_mtime_old

    # Old date directories should have been removed (since they became empty)
    assert not d_old.exists()
    assert not d_older.exists()

    # Recent files and non-empty dirs preserved
    assert f_recent.exists()
    assert d_recent.exists()
    assert f_mtime_new.exists()
    assert not f_mtime_old.exists()


def test_cli_dev_log_days_and_engines():
    parser = build_parser()
    args = parser.parse_args(["--dev-log-days", "14"])
    assert args.dev_log_days == 14

    default_args = parser.parse_args([])
    assert default_args.dev_log_days == 7

    eng = build_engines("mock", "mock", "mock", dev_log_days=10)
    assert eng.dev_log_days == 10


def test_git_check_ignore():
    res = subprocess.run(
        ["git", "check-ignore", "logs/dev/test_sess.jsonl"],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "logs/" in res.stdout or "logs/dev" in res.stdout
