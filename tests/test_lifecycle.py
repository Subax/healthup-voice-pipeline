"""C1 세션 수명주기 및 PromptBank 테스트.

통과 기준:
1. 시작 → 고지 audio_begin이 첫 LLM 호출보다 먼저
2. 종료 → history == [], 상태 대기 (idle)
3. 기동 시 기본 목소리 1종만 선합성 (시간 단축)
4. 길이 필터로 버리는 문장 0개
5. 기존 test_web_demo.py 하위 호환 유지
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import pytest
import pytest_asyncio
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.audio import Framer
from voice_pipeline.bridge.protocol import unpack_frame
from voice_pipeline.llm.mock import MockLLM
from voice_pipeline.prompts import (
    DEFAULT_PROMPTS,
    PROMPT_CONFIRM_CAMERA,
    PROMPT_CONFIRM_END,
    PROMPT_CONFIRM_HIDE_AI,
    PROMPT_CONFIRM_MODE,
    PROMPT_CONFIRM_RESET,
    PROMPT_GREETING_END,
    PROMPT_GREETING_START,
    PROMPT_NOTICE_AI,
    PROMPT_REDO_EMPTY,
    PROMPT_REDO_SUCCESS,
    PROMPT_REPROMPT,
    PromptBank,
)
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.lifecycle import SessionLifecycle, SessionState
from voice_pipeline.web.mocks import EnergyVAD, MockSTT


class TrackingMockLLM(MockLLM):
    """호출 횟수를 추적하는 모의 LLM."""

    def __init__(self, reply: str = "네, 말씀 잘 들었습니다.", ttft_ms: float = 10.0, tokens_per_s: float = 400.0):
        super().__init__(reply=reply, ttft_ms=ttft_ms, tokens_per_s=tokens_per_s)
        self.call_count = 0
        self.last_messages: list[dict] = []

    async def stream(self, messages: list[dict], turn_id: int):
        self.call_count += 1
        self.last_messages = [dict(m) for m in messages]
        async for ev in super().stream(messages, turn_id=turn_id):
            yield ev


@pytest.mark.asyncio
async def test_lifecycle_state_transitions():
    """세션 상태 기계의 전이 및 확인 대기 흐름 테스트."""
    history_events: list[tuple[str, str]] = []

    async def on_change(old_state: str, new_state: str):
        history_events.append((old_state, new_state))

    lc = SessionLifecycle(initial_state=SessionState.IDLE, on_state_change=on_change)
    assert lc.is_idle
    assert lc.state == "idle"

    # idle -> active
    await lc.start()
    assert lc.is_active
    assert lc.state == "active"
    assert history_events[-1] == ("idle", "active")

    # active -> confirming (종료 확인 질문)
    await lc.request_confirm("SESSION_END", {"intent": "SESSION_END"})
    assert lc.is_confirming
    assert lc.pending_intent == "SESSION_END"
    assert history_events[-1] == ("active", "confirming")

    # 확인 취소 (아니요) -> active로 복귀
    intent, payload = await lc.resolve_confirm(False)
    assert intent is None
    assert payload == {}
    assert lc.is_active
    assert history_events[-1] == ("confirming", "active")

    # active -> confirming (초기화 확인)
    await lc.request_confirm("RESET")
    assert lc.is_confirming

    # 확인 수락 (네) -> 실행 의도 반환
    intent, payload = await lc.resolve_confirm(True)
    assert intent == "RESET"
    assert lc.is_active

    # close -> closing -> idle
    await lc.close()
    assert lc.is_idle
    assert history_events[-2] == ("active", "closing")
    assert history_events[-1] == ("closing", "idle")

    # 잘못된 상태 전이 예외
    with pytest.raises(ValueError):
        await lc.transition_to("invalid_state")


@pytest.mark.asyncio
async def test_prompt_bank_coverage_and_zero_discarded():
    """PromptBank 필수 11종 문구 존재 및 길이 필터 버림 0개 검증."""
    # 1) 필수 키 존재 확인
    expected_keys = [
        PROMPT_NOTICE_AI,
        PROMPT_GREETING_START,
        PROMPT_GREETING_END,
        PROMPT_CONFIRM_END,
        PROMPT_CONFIRM_RESET,
        PROMPT_CONFIRM_MODE,
        PROMPT_CONFIRM_CAMERA,
        PROMPT_CONFIRM_HIDE_AI,
        PROMPT_REDO_SUCCESS,
        PROMPT_REDO_EMPTY,
        PROMPT_REPROMPT,
    ]
    for key in expected_keys:
        assert key in DEFAULT_PROMPTS
        assert len(DEFAULT_PROMPTS[key]) > 0

    # 2) AI 고지 문구에 법적 명칭 "AI 마음 건강 도우미" 포함 확인
    assert "AI 마음 건강 도우미" in DEFAULT_PROMPTS[PROMPT_NOTICE_AI]

    # 3) MockTTS로 선합성 진행
    tts = MockTTS(first_audio_ms=10, rtf=0.01)
    bank = PromptBank()
    await bank.prepare(tts, voices=[None])

    # 4) 길이 필터로 버려진 문장 0개 검증
    assert bank.discarded_count == 0
    assert len(bank.discarded_items) == 0

    # 5) 모든 프롬프트의 캐시 생성 확인
    for key in expected_keys:
        assert bank.has_audio(key), f"{key}에 대한 선합성 오디오가 없습니다"
        audio = bank.get_audio(key)
        assert audio is not None and len(audio) > 0


@pytest.mark.asyncio
async def test_confirmation_5_kinds_no_yes_no():
    """확인 질문 5종에 에코 대비 원칙(네/아니요 불포함) 검증."""
    confirm_keys = [
        PROMPT_CONFIRM_END,
        PROMPT_CONFIRM_RESET,
        PROMPT_CONFIRM_MODE,
        PROMPT_CONFIRM_CAMERA,
        PROMPT_CONFIRM_HIDE_AI,
    ]
    for key in confirm_keys:
        text = DEFAULT_PROMPTS[key]
        assert "네" not in text and "아니요" not in text, f"{key}에 네/아니요가 포함됨: {text}"
        assert text.endswith("?") or text.endswith("까?") or text.endswith("요?")


@pytest.mark.asyncio
async def test_warmup_synthesizes_only_one_default_voice():
    """기동 시 기본 목소리 1종만 선합성하여 기동 시간을 단축하는지 검증."""
    tts = MockTTS(first_audio_ms=5, rtf=0.01)
    tts.voices = [
        {"id": "voice_default", "label": "기본 목소리"},
        {"id": "voice_warm", "label": "따뜻한 목소리"},
        {"id": "voice_calm", "label": "차분한 목소리"},
    ]
    tts.voice = "voice_default"

    prompt_bank = PromptBank()
    llm = TrackingMockLLM()
    eng = Engines(
        llm=llm,
        tts=tts,
        stt_initial=MockSTT(""),
        make_vad=lambda: EnergyVAD(),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        prompt_bank=prompt_bank,
    )

    # warmup 실행
    await eng.warmup()

    # 캐시된 목소리 키 집합 확인
    cached_voices = {v for (v, k) in prompt_bank._cache.keys()}
    assert cached_voices == {"voice_default"}, f"오직 1종의 기본 목소리만 선합성되어야 함: {cached_voices}"
    assert "voice_warm" not in cached_voices
    assert "voice_calm" not in cached_voices


async def _recv_json_or_frame(ws, timeout=5.0):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type == WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type == WSMsgType.BINARY:
        return unpack_frame(msg.data)
    raise AssertionError(f"unexpected {msg.type}")


@pytest_asyncio.fixture
async def lifecycle_client():
    llm = TrackingMockLLM()
    tts = MockTTS(first_audio_ms=5, rtf=0.01)
    stt = MockSTT("테스트 발화")
    prompt_bank = PromptBank()
    eng = Engines(
        llm=llm,
        tts=tts,
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        prompt_bank=prompt_bank,
    )
    # warmup에서 선합성 1회 수행
    await eng.warmup()
    app = create_app(eng)
    c = TestClient(TestServer(app))
    await c.start_server()
    yield c, llm, prompt_bank
    await c.close()


@pytest.mark.asyncio
async def test_session_start_notice_before_first_llm_call(lifecycle_client):
    """시작 시 법적 고지 audio_begin이 첫 LLM 호출보다 먼저 재생되는지 검증."""
    client, llm, prompt_bank = lifecycle_client
    ws = await client.ws_connect("/ws")
    hello = await _recv_json_or_frame(ws)
    assert hello["type"] == "hello"

    # 세션 시작 메시지 전송
    await ws.send_str(json.dumps({"type": "session_start"}))

    msgs: list = []
    # 고지 오디오 끝날 때까지 수신
    while True:
        m = await _recv_json_or_frame(ws)
        msgs.append(m)
        if isinstance(m, dict) and m["type"] == "audio_end":
            break

    # 1) 세션 상태가 active로 전이되었는지 확인
    state_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "session_state"]
    assert any(m["state"] == "active" for m in state_msgs)

    # 2) 고지 audio_begin 수신 확인
    begin_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "audio_begin"]
    assert len(begin_msgs) >= 1
    notice_tid = begin_msgs[0]["turn_id"]

    # 3) 고지 오디오 프레임 수신 확인
    frames = [m for m in msgs if isinstance(m, tuple) and m[0] == notice_tid]
    assert len(frames) > 0
    assert frames[-1][2] is True  # 마지막 프레임 is_last

    # 4) 이 시점까지 LLM은 전혀 호출되지 않았어야 함
    assert llm.call_count == 0, f"고지 재생 중 LLM이 호출되면 안 됨: {llm.call_count}"

    # 5) 이후 첫 대화 턴 전송 시 LLM이 비로소 호출됨
    await ws.send_str(json.dumps({"type": "text_input", "text": "안녕하세요"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m["type"] == "turn_done":
            break

    assert llm.call_count == 1, f"대화 턴 후 LLM 호출 수가 1이어야 함: {llm.call_count}"
    await ws.close()


@pytest.mark.asyncio
async def test_session_end_clears_history_and_returns_to_idle(lifecycle_client):
    """종료 시 종료 안내 재생, history 비우기, 상태 대기(idle) 복귀 검증."""
    client, llm, prompt_bank = lifecycle_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    # 세션 시작 및 1턴 대화
    await ws.send_str(json.dumps({"type": "text_input", "text": "기록용 발화"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m["type"] == "turn_done":
            break

    # 세션 종료 요청
    await ws.send_str(json.dumps({"type": "session_end"}))

    msgs: list = []
    while True:
        m = await _recv_json_or_frame(ws)
        msgs.append(m)
        if isinstance(m, dict) and m.get("type") == "session_state" and m.get("state") == "idle":
            break

    # 1) closing 거쳐서 idle로 복귀 확인
    state_seq = [m["state"] for m in msgs if isinstance(m, dict) and m.get("type") == "session_state"]
    assert "closing" in state_seq
    assert state_seq[-1] == "idle"

    # 2) 종료 안내 음성(audio_begin/audio_end)이 재생되었는지 확인
    assert any(isinstance(m, dict) and m.get("type") == "audio_begin" for m in msgs)
    assert any(isinstance(m, dict) and m.get("type") == "audio_end" for m in msgs)

    # 3) 이전 대화 기록이 새 세션 턴에 전달되지 않음 (history == [] 확인)
    await ws.send_str(json.dumps({"type": "text_input", "text": "새 발화"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m["type"] == "turn_done":
            break
    assert not any(m.get("content") == "기록용 발화" for m in llm.last_messages)
    assert any(m.get("content") == "새 발화" for m in llm.last_messages)

    await ws.close()


@pytest.mark.asyncio
async def test_reset_clears_history_and_resets_lifecycle(lifecycle_client):
    """reset 메시지 수신 시 history 정리 및 lifecycle idle 복귀 검증."""
    client, llm, prompt_bank = lifecycle_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    await ws.send_str(json.dumps({"type": "text_input", "text": "대화 내용"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m["type"] == "turn_done":
            break

    await ws.send_str(json.dumps({"type": "reset"}))
    msgs: list = []
    while True:
        m = await _recv_json_or_frame(ws)
        msgs.append(m)
        if isinstance(m, dict) and m.get("type") == "reset_done":
            break

    # reset_done 수신 확인
    assert any(isinstance(m, dict) and m.get("type") == "reset_done" for m in msgs)
    # idle 상태 전이 확인
    assert any(isinstance(m, dict) and m.get("type") == "session_state" and m.get("state") == "idle" for m in msgs)

    # 리셋 후 이전 발화가 새 세션에 전달되지 않음 (history == [] 확인)
    await ws.send_str(json.dumps({"type": "text_input", "text": "리셋 후 새 발화"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m["type"] == "turn_done":
            break
    assert not any(m.get("content") == "대화 내용" for m in llm.last_messages)
    assert any(m.get("content") == "리셋 후 새 발화" for m in llm.last_messages)

    await ws.close()


@pytest.mark.asyncio
async def test_action_feedback_and_reprompt_prompts():
    """REDO_USER 취소 피드백 2종 및 재요청 안내 문구 검증."""
    assert "지웠어요" in DEFAULT_PROMPTS[PROMPT_REDO_SUCCESS]
    assert "없어요" in DEFAULT_PROMPTS[PROMPT_REDO_EMPTY]
    assert "다시" in DEFAULT_PROMPTS[PROMPT_REPROMPT]


@pytest.mark.asyncio
async def test_direct_text_input_backward_compatibility(lifecycle_client):
    """기존 test_web_demo.py 호환: direct text_input 시 불필요한 고지 없이 바로 턴 처리."""
    client, llm, _ = lifecycle_client
    ws = await client.ws_connect("/ws")
    hello = await _recv_json_or_frame(ws)
    assert hello["type"] == "hello"

    await ws.send_str(json.dumps({"type": "text_input", "text": "혈압 알려줘"}))
    msgs: list = []
    while True:
        m = await _recv_json_or_frame(ws)
        msgs.append(m)
        if isinstance(m, dict) and m["type"] == "turn_done":
            break

    types = [m["type"] for m in msgs if isinstance(m, dict)]
    # 기존 순서: transcript < audio_begin < audio_end < turn_done
    assert types.index("transcript") < types.index("audio_begin") < types.index("audio_end") < types.index("turn_done")
    await ws.close()

