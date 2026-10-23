"""C4 대기 청취(--wake 및 config.wake) 단위 및 통합 시험 (webapp_plan_review.md §15-3).

합격 기준:
1) 대기 중 비명령 5발화 → LLM 호출 0회, transcript 메시지 0개, 개발 모드 JSONL 로그 0줄, wake_ignored_count == 5
2) 대기 중 상한(2000ms) 초과 긴 발화 → STT transcribe 호출 0회
3) 대기 중 "시작하자" 발화 → active 전이 + AI 고지 audio_begin 재생
4) config.wake=false 전환 시 대기 중 발화 → STT transcribe 호출 0회
5) 첫 터치 화면은 페이지당 1회만 표시 (세션 종료 후 idle 복귀 시 재표시 없음)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time
from typing import Any

import numpy as np
import pytest
import pytest_asyncio
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

# Ensure repo root is in python path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_pipeline.bridge.protocol import unpack_frame
from voice_pipeline.llm.mock import MockLLM
from voice_pipeline.prompts import PromptBank
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import SESSIONS, create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.lifecycle import SessionState
from voice_pipeline.web.mocks import EnergyVAD, MockSTTResult


class TrackingMockLLM(MockLLM):
    """호출 횟수를 추적하는 모의 LLM."""

    def __init__(self, reply: str = "챗봇 기본 답변입니다.", ttft_ms: float = 10.0, tokens_per_s: float = 400.0):
        super().__init__(reply=reply, ttft_ms=ttft_ms, tokens_per_s=tokens_per_s)
        self.call_count = 0
        self.last_messages: list[dict] = []

    async def stream(self, messages: list[dict], turn_id: int):
        self.call_count += 1
        self.last_messages = [dict(m) for m in messages]
        async for ev in super().stream(messages, turn_id=turn_id):
            yield ev


class TrackingMockSTT:
    """transcribe 호출 횟수 및 발화 길이를 추적하는 STT."""

    sample_rate = 16000

    def __init__(self, text: str = "오늘 날씨 어때요"):
        self.text = text
        self.transcribe_call_count = 0
        self.transcribe_durations: list[float] = []

    async def transcribe(self, pcm16: bytes) -> MockSTTResult:
        self.transcribe_call_count += 1
        dur = len(pcm16) / 32.0
        self.transcribe_durations.append(dur)
        return MockSTTResult(text=self.text, elapsed_ms=5.0)


async def _recv_json_or_frame(ws, timeout=5.0):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type == WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type == WSMsgType.BINARY:
        return unpack_frame(msg.data)
    raise AssertionError(f"unexpected {msg.type}")


async def _drain_turn(ws, timeout=5.0) -> list[Any]:
    msgs: list[Any] = []
    while True:
        m = await _recv_json_or_frame(ws, timeout=timeout)
        msgs.append(m)
        if isinstance(m, dict) and m.get("type") == "turn_done":
            break
    return msgs


def _get_session(client: TestClient):
    sessions = client.app.get(SESSIONS)
    if sessions:
        return next(iter(sessions))
    raise RuntimeError("세션을 찾을 수 없습니다")


LOUD_FRAME = np.full(320, 8000, dtype=np.int16).tobytes()
QUIET_FRAME = np.zeros(320, dtype=np.int16).tobytes()


async def _send_audio_utterance(ws, loud_frames: int = 30, quiet_frames: int = 6):
    """EnergyVAD에 발화 구간을 주입한다 (1프레임 = 20ms)."""
    for _ in range(loud_frames):
        await ws.send_bytes(LOUD_FRAME)
    for _ in range(quiet_frames):
        await ws.send_bytes(QUIET_FRAME)


@pytest.mark.asyncio
async def test_idle_non_command_discarded_zero_llm_zero_transcript_zero_log(tmp_path):
    """1) 대기 중 비명령 5발화 → LLM 호출 0회, transcript 메시지 0개, 개발 모드 JSONL 로그 0줄, wake_ignored_count == 5."""
    llm = TrackingMockLLM()
    tts = MockTTS(first_audio_ms=5, rtf=0.01)
    stt = TrackingMockSTT(text="오늘 날씨 어때요")  # 일반 대화문 (TALK)
    prompt_bank = PromptBank()

    eng = Engines(
        llm=llm,
        tts=tts,
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        prompt_bank=prompt_bank,
        default_mode="dev",
        dev_log_dir=tmp_path,
        intent=True,
        wake=True,
    )
    await eng.warmup()
    app = create_app(eng)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        ws = await client.ws_connect("/ws")
        hello = await _recv_json_or_frame(ws)
        assert hello["type"] == "hello"
        assert hello.get("wake") is True

        session_obj = _get_session(client)
        assert session_obj.lifecycle.is_idle
        assert session_obj.wake_ignored_count == 0

        received_msgs: list[Any] = []

        # 백그라운드로 수신 루프 실행
        async def recv_loop():
            try:
                while True:
                    m = await _recv_json_or_frame(ws, timeout=0.5)
                    received_msgs.append(m)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        recv_task = asyncio.create_task(recv_loop())

        # 5회 비명령 발화 주입 (각 발화 30 프레임 = 600ms <= 2000ms 상한)
        for i in range(5):
            await _send_audio_utterance(ws, loud_frames=30, quiet_frames=6)
            await asyncio.sleep(0.1)

        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

        # 1. LLM 호출 0회 검증
        assert llm.call_count == 0, f"대기 중 비명령 발화에서 LLM이 호출됨: {llm.call_count}"

        # 2. 브라우저로 transcript 메시지 전송 0개 검증
        transcripts = [m for m in received_msgs if isinstance(m, dict) and m.get("type") == "transcript"]
        assert len(transcripts) == 0, f"대기 중 비명령 발화의 transcript가 전송됨: {transcripts}"

        # 2-1. dev_event 메시지 0개 검증
        dev_events = [m for m in received_msgs if isinstance(m, dict) and m.get("type") == "dev_event"]
        assert len(dev_events) == 0, f"대기 중 비명령 발화에서 dev_event가 전송됨: {dev_events}"

        # 2-2. _last_pcm 메모리 누출 차단 및 retranscribe("다시 인식") 에러 검증
        assert session_obj._last_pcm is None, "대기 중 비명령 발화의 PCM 바이트가 _last_pcm에 잔류함"
        await ws.send_str(json.dumps({"type": "retranscribe"}))
        retrans_resp = await _recv_json_or_frame(ws, timeout=2.0)
        assert isinstance(retrans_resp, dict) and retrans_resp.get("type") == "error", f"retranscribe 응답 불일치: {retrans_resp}"
        assert "다시 인식할 마이크 발화가 없습니다" in retrans_resp.get("message", ""), f"에러 메시지 불일치: {retrans_resp}"
        assert not any(isinstance(m, dict) and m.get("type") == "retranscript" for m in received_msgs)

        # 3. 개발 모드 JSONL 로그 0줄 검증
        jsonl_files = list(tmp_path.glob("**/*.jsonl"))
        total_lines = 0
        for f in jsonl_files:
            total_lines += len(f.read_text(encoding="utf-8").strip().splitlines())
        assert total_lines == 0, f"대기 중 비명령 발화가 개발 로그에 기록됨 ({total_lines}줄): {jsonl_files}"

        # 4. 세션 무시 카운터 5회 검증 및 상태 idle 유지
        assert session_obj.wake_ignored_count == 5, f"wake_ignored_count 불일치: {session_obj.wake_ignored_count}"
        assert session_obj.lifecycle.is_idle, f"세션 상태가 idle이 아님: {session_obj.lifecycle.state}"

        # 5. 대기(idle) 상태에서 확인 등급 명령("개발 모드로 바꿔 줘") 후 "아니요" 응답 시 active로 새지 않고 idle 복귀 검증
        stt.text = "개발 모드로 바꿔 줘"
        await _send_audio_utterance(ws, loud_frames=30, quiet_frames=6)
        confirm_msgs = await _drain_turn(ws)
        assert session_obj.lifecycle.is_confirming, f"확인 대기 상태로 전이되지 않음: {session_obj.lifecycle.state}"

        # "아니요" 취소 응답 주입
        stt.text = "아니요"
        await _send_audio_utterance(ws, loud_frames=20, quiet_frames=6)
        cancel_msgs = await _drain_turn(ws)

        assert session_obj.lifecycle.is_idle, f"취소 후 idle로 복귀하지 않고 {session_obj.lifecycle.state} 상태로 샜음"
        assert session_obj.lifecycle.state == "idle"
        assert llm.call_count == 0, f"확인 명령 취소 과정에서 LLM이 호출됨: {llm.call_count}"

        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_idle_duration_cap_exceeded_zero_stt_calls():
    """2) 대기 중 상한(2000ms) 초과 긴 발화 → STT transcribe 호출 0회."""
    llm = TrackingMockLLM()
    tts = MockTTS(first_audio_ms=5, rtf=0.01)
    stt = TrackingMockSTT(text="시작하자")
    prompt_bank = PromptBank()

    eng = Engines(
        llm=llm,
        tts=tts,
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        prompt_bank=prompt_bank,
        intent=True,
        wake=True,
    )
    await eng.warmup()
    app = create_app(eng)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        ws = await client.ws_connect("/ws")
        await _recv_json_or_frame(ws)  # hello

        session_obj = _get_session(client)
        assert session_obj.lifecycle.is_idle

        # 2000ms 초과 긴 발화 주입:
        # 135 loud frames (2700ms) + 6 quiet frames (120ms) = 2820ms 총 구간
        # dur_ms = 2820ms - 300ms(PRE_ROLL_MS) = 2520ms > 2000ms(WAKE_MAX_SPEECH_MS)
        await _send_audio_utterance(ws, loud_frames=135, quiet_frames=6)
        await asyncio.sleep(0.15)

        # STT transcribe가 전혀 호출되지 않아야 함
        assert stt.transcribe_call_count == 0, f"2000ms 초과 발화에서 STT가 호출됨: {stt.transcribe_call_count}"
        assert llm.call_count == 0
        assert session_obj.lifecycle.is_idle

        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_idle_session_start_speech_starts_session_and_plays_notice():
    """3) 대기 중 "시작하자" 발화 → active 전이 + AI 고지 audio_begin 재생."""
    llm = TrackingMockLLM()
    tts = MockTTS(first_audio_ms=5, rtf=0.01)
    stt = TrackingMockSTT(text="시작하자")
    prompt_bank = PromptBank()

    eng = Engines(
        llm=llm,
        tts=tts,
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        prompt_bank=prompt_bank,
        intent=True,
        wake=True,
    )
    await eng.warmup()
    app = create_app(eng)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        ws = await client.ws_connect("/ws")
        await _recv_json_or_frame(ws)  # hello

        session_obj = _get_session(client)
        assert session_obj.lifecycle.is_idle

        # 대기 상태에서 "시작하자" 발화 주입 (30 frames = 600ms <= 2000ms)
        await _send_audio_utterance(ws, loud_frames=30, quiet_frames=6)
        msgs = await _drain_turn(ws)

        # 1. active 상태 전이 확인
        states = [m["state"] for m in msgs if isinstance(m, dict) and m.get("type") == "session_state"]
        assert "active" in states, f"active 전이 누락: {states}"
        assert session_obj.lifecycle.is_active

        # 2. AI 고지 audio_begin 재생 확인
        audio_begins = [m for m in msgs if isinstance(m, dict) and m.get("type") == "audio_begin"]
        assert len(audio_begins) >= 1, "AI 고지 audio_begin이 재생되지 않음"

        # 3. transcript 및 turn_done 확인
        transcripts = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript"]
        assert len(transcripts) >= 1
        assert transcripts[0]["text"] == "시작하자"

        turn_dones = [m for m in msgs if isinstance(m, dict) and m.get("type") == "turn_done"]
        assert len(turn_dones) >= 1
        assert "도우미" in turn_dones[0]["spoken_text"]

        # 4. LLM 호출 0회 확인 (AI 고지는 선합성 음성에서 즉시 송출)
        assert llm.call_count == 0, f"세션 시작 고지에서 LLM이 호출됨: {llm.call_count}"

        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_idle_wake_disabled_zero_stt():
    """4) config.wake=false 전환 시 대기 중 발화 → STT transcribe 호출 0회."""
    llm = TrackingMockLLM()
    tts = MockTTS(first_audio_ms=5, rtf=0.01)
    stt = TrackingMockSTT(text="시작하자")
    prompt_bank = PromptBank()

    eng = Engines(
        llm=llm,
        tts=tts,
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        prompt_bank=prompt_bank,
        intent=True,
        wake=True,
    )
    await eng.warmup()
    app = create_app(eng)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        ws = await client.ws_connect("/ws")
        await _recv_json_or_frame(ws)  # hello

        session_obj = _get_session(client)
        assert session_obj.feature_prefs["wake"] is True

        # config로 wake 비활성화
        await ws.send_str(json.dumps({"type": "config", "wake": False}))
        await asyncio.sleep(0.05)
        assert session_obj.feature_prefs["wake"] is False

        # 대기 상태에서 짧은 발화 주입
        await _send_audio_utterance(ws, loud_frames=30, quiet_frames=6)
        await asyncio.sleep(0.1)

        # wake=False이므로 마이크 오디오에 대해 STT가 0회 호출되어야 함
        assert stt.transcribe_call_count == 0, f"wake=False 대기 상태에서 STT가 호출됨: {stt.transcribe_call_count}"
        assert session_obj.lifecycle.is_idle

        await ws.close()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_first_touch_single_prompt_after_session_end():
    """5) 첫 터치 화면은 페이지당 1회만 표시 (세션 종료 후 idle 복귀 시 재표시 없음 및 wake 대기 연결 검증)."""
    # 1. app.js 내 idle 복귀 시 첫 터치 재요구 방지 및 대기 안내 설정 로직 정적 검증
    app_js_path = Path(__file__).resolve().parents[1] / "src" / "voice_pipeline" / "web" / "static" / "app.js"
    app_js = app_js_path.read_text(encoding="utf-8")

    # AudioContext 동작 중(ctx.state === "running") idle 복귀 시 avatarHint 설정 확인
    assert "마이크 대기 중 ('시작하자'라고 말씀해 주세요)" in app_js, "app.js에 대기 중 마이크 안내 문구가 누락됨"
    assert 'case "session_state":' in app_js
    assert 'currentSessionState === "idle"' in app_js or 'm.state === "idle"' in app_js

    # 2. WebSocket 수명주기 E2E 흐름 검증: 시작 -> 종료 -> idle 복귀 -> 다시 시작하자 음성으로 재시작
    llm = TrackingMockLLM(reply="종료 전 답변입니다.")
    tts = MockTTS(first_audio_ms=5, rtf=0.01)
    stt = TrackingMockSTT(text="시작하자")
    prompt_bank = PromptBank()

    eng = Engines(
        llm=llm,
        tts=tts,
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        prompt_bank=prompt_bank,
        intent=True,
        wake=True,
    )
    await eng.warmup()
    app = create_app(eng)
    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        ws = await client.ws_connect("/ws")
        await _recv_json_or_frame(ws)  # hello

        session_obj = _get_session(client)
        assert session_obj.lifecycle.is_idle

        # 1차 세션 시작 (대기 중 "시작하자")
        await _send_audio_utterance(ws, loud_frames=30, quiet_frames=6)
        msgs1 = await _drain_turn(ws)
        assert any(isinstance(m, dict) and m.get("type") == "session_state" and m.get("state") == "active" for m in msgs1)
        assert session_obj.lifecycle.is_active

        # 세션 종료 명령 전송
        await ws.send_str(json.dumps({"type": "session_end"}))
        msgs_closing: list[Any] = []
        while True:
            m = await _recv_json_or_frame(ws)
            msgs_closing.append(m)
            if isinstance(m, dict) and m.get("type") == "session_state" and m.get("state") == "idle":
                break

        # idle 복귀 확인
        assert session_obj.lifecycle.is_idle

        # 세션 종료 후 첫 터치를 다시 요구하지 않고, 대기 상태에서 바로 2차 "시작하자" 음성으로 재시작
        await _send_audio_utterance(ws, loud_frames=30, quiet_frames=6)
        msgs2 = await _drain_turn(ws)
        assert any(isinstance(m, dict) and m.get("type") == "session_state" and m.get("state") == "active" for m in msgs2)
        assert session_obj.lifecycle.is_active

        await ws.close()
    finally:
        await client.close()
