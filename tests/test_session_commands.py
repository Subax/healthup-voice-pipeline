"""C3 음성 명령 통합 시험 (webapp_plan.md §5 C3).

통합 시험 9개 통과 기준:
① 명령 턴 LLM 호출 0회 (test_command_turn_llm_zero_calls)
② 말하는 중 "멈춰" → interrupt 1회·새 턴 0 (test_speaking_stop_command_interrupts_and_zero_new_turn)
③ 확인 "아니요" → 세션 유지 (test_confirm_no_maintains_session)
④ "다시 말해 줘" → 직전 답 원문 재생·기록 오염 없음 (test_repeat_bot_replays_last_bot_text_without_history_pollution)
⑤ 위기 표현 → LLM 경로 (test_crisis_expression_goes_to_llm)
⑥ 명령 후보 전사로 선행 생성 0회 (test_speculation_skipped_for_command_candidates)
⑦ 확인 대기 중 "네"를 맞장구로 버리지 않음 (test_confirming_state_preserves_short_yes_from_backchannel)
⑧ 음성으로 개별 기능("개발 정보 보여 줘", "대기 청취 꺼 줘") 즉시 전환 및 "AI 고지 숨겨 줘" 확인 대기 진입 (test_voice_individual_feature_toggles)
⑨ mock에서 정지 명령 STT 완료 → interrupt 전송 <= 50ms (test_mock_stop_command_stt_to_interrupt_latency)
+ 보조 시험: --no-intent 옵션 시 명령 분류 비활성화 및 LLM 호출 검증 (test_no_intent_flag)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.bridge.protocol import unpack_frame
from voice_pipeline.llm.mock import MockLLM
from voice_pipeline.prompts import (
    PROMPT_CONFIRM_END,
    PROMPT_CONFIRM_HIDE_AI,
    PromptBank,
)
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import SESSIONS, create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.lifecycle import SessionState
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


async def _recv_json_or_frame(ws, timeout=5.0):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type == WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type == WSMsgType.BINARY:
        return unpack_frame(msg.data)
    raise AssertionError(f"unexpected {msg.type}")


async def _drain_turn(ws, timeout=5.0) -> list[Any]:
    """turn_done이 수신될 때까지 모든 메시지/프레임을 수집하여 반환."""
    msgs: list[Any] = []
    while True:
        m = await _recv_json_or_frame(ws, timeout=timeout)
        msgs.append(m)
        if isinstance(m, dict) and m.get("type") == "turn_done":
            break
    return msgs


@pytest_asyncio.fixture
async def session_client():
    llm = TrackingMockLLM(reply="챗봇 기본 답변입니다.")
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
        intent=True,
    )
    await eng.warmup()
    app = create_app(eng)
    c = TestClient(TestServer(app))
    await c.start_server()
    yield c, llm, eng
    await c.close()


def _get_session(client: TestClient):
    sessions = client.app.get(SESSIONS)
    if sessions:
        return next(iter(sessions))
    raise RuntimeError("세션을 찾을 수 없습니다")


@pytest.mark.asyncio
async def test_command_turn_llm_zero_calls(session_client):
    """① 명령 턴 LLM 호출 0회 검증: 즉시 명령 실행 시 LLM을 부르지 않는다."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    initial_llm_calls = llm.call_count

    # 즉시 실행 명령: "개발 정보 보여 줘"
    await ws.send_str(json.dumps({"type": "text_input", "text": "개발 정보 보여 줘"}))
    msgs = await _drain_turn(ws)

    # 1) LLM 호출 횟수가 증가하지 않음
    assert llm.call_count == initial_llm_calls, f"명령 턴에서 LLM이 호출됨: {llm.call_count}"

    # 2) dev_event intent 확인
    dev_events = [m for m in msgs if isinstance(m, dict) and m.get("event") == "intent"]
    assert len(dev_events) >= 1
    assert dev_events[0]["intent"] == "DEV_INFO_ON"
    assert dev_events[0]["grade"] == "immediate"

    # 3) UI/클라이언트로 config 전송 확인
    configs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "config"]
    assert any(c.get("show_dev_info") is True for c in configs)

    await ws.close()


@pytest.mark.asyncio
async def test_speaking_stop_command_interrupts_and_zero_new_turn(session_client):
    """② 말하는 중 '멈춰' → interrupt 1회·새 턴 0 검증."""
    client, llm, eng = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    session_obj = _get_session(client)

    # 1) 일반 발화 시작
    await ws.send_str(json.dumps({"type": "text_input", "text": "오늘 날씨 어때?"}))
    # audio_begin까지 대기
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m.get("type") == "audio_begin":
            break

    # 2) 말하는 중에 STT로 '멈춰'가 들어왔을 때 _probe_classify 호출
    session_obj.engines._stt_cache[session_obj.stt_id] = MockSTT("멈춰")
    task_before = session_obj._task

    # probe_classify 호출 (재생 중 '멈춰' 발화)
    pcm_dummy = b"\x00" * 640  # 20ms
    await session_obj._probe_classify(pcm_dummy, time.monotonic())

    # 새 턴(_task)이 시작되지 않았는지 검증
    assert session_obj._task == task_before or (session_obj._task is not None and session_obj._task.done())

    # 메시지 수집: interrupt 1회 수신 및 새 transcript 0개 검증
    msgs = []
    for _ in range(15):
        try:
            m = await _recv_json_or_frame(ws, timeout=0.3)
            msgs.append(m)
        except asyncio.TimeoutError:
            break

    interrupts = [m for m in msgs if isinstance(m, dict) and m.get("type") == "interrupt"]
    assert len(interrupts) == 1, f"Expected exactly 1 interrupt message, got {interrupts}"

    transcripts = [m for m in msgs if isinstance(m, dict) and m.get("type") == "transcript" and m.get("text") == "멈춰"]
    assert len(transcripts) == 0, f"Expected 0 transcript for stop command, got {transcripts}"

    # dev_event barge_in의 result == 'stop_command' 수신 확인
    stop_barge = [m for m in msgs if isinstance(m, dict) and m.get("event") == "barge_in" and m.get("result") == "stop_command"]
    assert len(stop_barge) >= 1, "Expected barge_in with result='stop_command'"

    await ws.close()


@pytest.mark.asyncio
async def test_confirm_no_maintains_session(session_client):
    """③ 확인 '아니요' → 세션 유지 및 명령 취소 검증."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    # 세션 시작하여 active 상태로 만들기
    await ws.send_str(json.dumps({"type": "session_start"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m.get("type") == "audio_end":
            break

    # 1) active 상태에서 종료 확인 요청 명령: "대화 끝낼게요"
    await ws.send_str(json.dumps({"type": "text_input", "text": "대화 끝낼게요"}))
    msgs = await _drain_turn(ws)

    # confirming 상태 진입 확인
    states = [m["state"] for m in msgs if isinstance(m, dict) and m.get("type") == "session_state"]
    assert "confirming" in states

    # 2) 취소 응답: "아니요"
    llm_calls_before = llm.call_count
    await ws.send_str(json.dumps({"type": "text_input", "text": "아니요"}))
    msgs2 = await _drain_turn(ws)

    # active 상태로 복귀 확인 (세션 유지)
    states2 = [m["state"] for m in msgs2 if isinstance(m, dict) and m.get("type") == "session_state"]
    assert "active" in states2

    # LLM은 호출되지 않음
    assert llm.call_count == llm_calls_before
    await ws.close()


@pytest.mark.asyncio
async def test_repeat_bot_replays_last_bot_text_without_history_pollution(session_client):
    """④ '다시 말해 줘' → 직전 답 원문 재생·기록 오염 없음 검증."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    # 1턴: 일반 질문 발화
    await ws.send_str(json.dumps({"type": "text_input", "text": "오늘 혈압 어때요?"}))
    await _drain_turn(ws)
    assert llm.call_count == 1

    # 2턴: '다시 말해 줘' (REPEAT_BOT)
    await ws.send_str(json.dumps({"type": "text_input", "text": "다시 말해 줘"}))
    msgs = await _drain_turn(ws)

    # 1) LLM 호출 횟수가 늘지 않음
    assert llm.call_count == 1, "REPEAT_BOT 명령에서 LLM이 다시 호출되면 안 됨"

    # 2) 오디오 출력이 재생됨 (audio_begin 및 audio_end 확인)
    begin_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "audio_begin"]
    assert len(begin_msgs) >= 1

    # 3) 대화 기록 오염 없음 검증: 다음 턴에서 이전 '다시 말해 줘'가 history에 없어야 함
    await ws.send_str(json.dumps({"type": "text_input", "text": "다음 질문입니다."}))
    await _drain_turn(ws)
    assert llm.call_count == 2
    # last_messages에 '다시 말해 줘'가 들어가지 않았는지 확인
    assert not any(m.get("content") == "다시 말해 줘" for m in llm.last_messages)

    await ws.close()


@pytest.mark.asyncio
async def test_crisis_expression_goes_to_llm(session_client):
    """⑤ 위기 표현 → LLM 경로 정상 진입 검증."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    llm_calls_before = llm.call_count

    # 위기 표현: "다 끝내고 싶어"
    await ws.send_str(json.dumps({"type": "text_input", "text": "다 끝내고 싶어"}))
    msgs = await _drain_turn(ws)

    # 1) LLM이 반드시 호출되어야 함
    assert llm.call_count == llm_calls_before + 1, "위기 표현은 LLM 경로로 응답을 생성해야 함"

    # 2) 의도 판정이 CHAT, dialog, crisis_blocked여야 함
    intent_events = [m for m in msgs if isinstance(m, dict) and m.get("event") == "intent"]
    assert len(intent_events) >= 1
    assert intent_events[0]["intent"] in ("TALK", "CHAT")
    assert intent_events[0]["grade"] == "dialog"
    assert intent_events[0]["rule"] == "crisis_blocked"

    await ws.close()


@pytest.mark.asyncio
async def test_speculation_skipped_for_command_candidates(session_client):
    """⑥ 명령 후보 전사로 선행 생성 0회 검증."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    session_obj = _get_session(client)
    # active 상태로 만들기
    await session_obj.lifecycle.start()

    # 1) active 상태에서 명령 후보 문장으로 _on_speculate 호출
    dummy_pcm = b"\x00" * 320
    await session_obj._on_speculate("개발 모드로 바꿔", utt_id=1, last=None, pcm16=dummy_pcm)
    assert session_obj._spec is None, "MODE_DEV 명령 후보 전사는 선행 생성을 시작하면 안 됨"

    await session_obj._on_speculate("그만할게요", utt_id=2, last=None, pcm16=dummy_pcm)
    assert session_obj._spec is None, "SESSION_END 명령 후보 전사는 선행 생성을 시작하면 안 됨"

    # idle 상태에서는 SESSION_START 후보 전사 선행 생성 건너뜀 검증
    await session_obj.lifecycle.reset()
    assert session_obj.lifecycle.is_idle
    await session_obj._on_speculate("시작하자", utt_id=3, last=None, pcm16=dummy_pcm)
    assert session_obj._spec is None, "idle 상태에서 SESSION_START 전사는 선행 생성을 시작하면 안 됨"

    # 2) 반면 active 상태에서 일반 대화 문장으로는 선행 생성이 정상 시작됨
    await session_obj.lifecycle.start()
    await session_obj._on_speculate("오늘 날씨가 참 좋네요", utt_id=4, last=None, pcm16=dummy_pcm)
    assert session_obj._spec is not None, "일반 대화 전사는 선행 생성이 동작해야 함"

    await ws.close()


@pytest.mark.asyncio
async def test_confirming_state_preserves_short_yes_from_backchannel(session_client):
    """⑦ 확인 대기 중 '네'를 맞장구로 버리지 않고 CONFIRM_YES로 처리 검증."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    session_obj = _get_session(client)

    # 1) 세션 시작 후 active 상태에서 확인 대기 상태 진입
    await ws.send_str(json.dumps({"type": "session_start"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m.get("type") == "audio_end":
            break

    await ws.send_str(json.dumps({"type": "text_input", "text": "대화 끝낼게요"}))
    await _drain_turn(ws)
    assert session_obj.lifecycle.is_confirming

    # 2) 확인 응답: 짧은 "네" (barge_filter의 맞장구 단어)
    # text_input 대신 _probe_classify 경로를 호출하여 is_confirming 분기가 작동하는지 검증
    session_obj.engines._stt_cache[session_obj.stt_id] = MockSTT("네")
    await session_obj._probe_classify(b"\x00" * 640, time.monotonic())

    msgs = await _drain_turn(ws)

    # backchannel_ignored 메시지가 발생하지 않고, 정상적으로 closing 및 idle로 종료되었는지 확인
    assert not any(isinstance(m, dict) and m.get("type") == "backchannel_ignored" for m in msgs)
    states = [m["state"] for m in msgs if isinstance(m, dict) and m.get("type") == "session_state"]
    assert "idle" in states

    await ws.close()


@pytest.mark.asyncio
async def test_voice_individual_feature_toggles(session_client):
    """⑧ 음성으로 개별 기능 즉시 전환 및 AI 고지 숨김 확인 대기 진입 검증."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    # 1) "개발 정보 보여 줘" -> 즉시 config 전송
    await ws.send_str(json.dumps({"type": "text_input", "text": "개발 정보 보여 줘"}))
    msgs1 = await _drain_turn(ws)
    configs1 = [m for m in msgs1 if isinstance(m, dict) and m.get("type") == "config"]
    assert any(c.get("show_dev_info") is True for c in configs1)

    # 2) "대기 청취 꺼 줘" -> 즉시 config 전송
    await ws.send_str(json.dumps({"type": "text_input", "text": "대기 청취 꺼 줘"}))
    msgs2 = await _drain_turn(ws)
    configs2 = [m for m in msgs2 if isinstance(m, dict) and m.get("type") == "config"]
    assert any(c.get("wake") is False for c in configs2)

    # 3) "AI 고지 숨겨 줘" -> 법적 경고 확인 대기 진입 (confirming)
    await ws.send_str(json.dumps({"type": "text_input", "text": "AI 고지 숨겨 줘"}))
    msgs3 = await _drain_turn(ws)
    states3 = [m["state"] for m in msgs3 if isinstance(m, dict) and m.get("type") == "session_state"]
    assert "confirming" in states3

    # 4) 확인 응답 "네" -> 그제서야 show_ai_notice: False 전송
    await ws.send_str(json.dumps({"type": "text_input", "text": "네"}))
    msgs4 = await _drain_turn(ws)
    configs4 = [m for m in msgs4 if isinstance(m, dict) and m.get("type") == "config"]
    assert any(c.get("show_ai_notice") is False for c in configs4)

    await ws.close()


@pytest.mark.asyncio
async def test_mock_stop_command_stt_to_interrupt_latency(session_client):
    """⑨ mock에서 정지 명령 STT 완료 → interrupt 전송 <= 50ms 예산 검증."""
    client, llm, _ = session_client
    ws = await client.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    session_obj = _get_session(client)

    # 1) 일반 발화 시작하여 audio_begin까지 대기 (sink.active == True 상태 만들기)
    await ws.send_str(json.dumps({"type": "text_input", "text": "오늘 날씨 어때?"}))
    while True:
        m = await _recv_json_or_frame(ws)
        if isinstance(m, dict) and m.get("type") == "audio_begin":
            break

    assert session_obj.sink.active

    # 2) 정지 명령 MockSTT 설정 및 _probe_classify 실행 시간 측정
    session_obj.engines._stt_cache[session_obj.stt_id] = MockSTT("멈춰")

    t_start = time.perf_counter()
    await session_obj._probe_classify(b"\x00" * 320, time.monotonic())
    elapsed_ms = (time.perf_counter() - t_start) * 1000.0

    # 3) interrupt 메시지가 실제로 수신되는지 확인
    msgs = []
    for _ in range(15):
        try:
            m = await _recv_json_or_frame(ws, timeout=0.3)
            msgs.append(m)
            if isinstance(m, dict) and m.get("type") == "interrupt":
                break
        except asyncio.TimeoutError:
            break

    interrupt_msgs = [m for m in msgs if isinstance(m, dict) and m.get("type") == "interrupt"]
    assert len(interrupt_msgs) == 1, f"Expected 1 interrupt message, got {interrupt_msgs}"

    # 전체 처리 시간이 50ms 이내여야 함 (§4.7, webapp_plan.md §5 C3)
    assert elapsed_ms <= 50.0, f"정지 명령 interrupt 지연 시간 초과: {elapsed_ms:.2f}ms > 50ms"

    await ws.close()


@pytest.mark.asyncio
async def test_no_intent_flag():
    """--no-intent 인자 시 명령 분류가 꺼지고 일반 대화(LLM)로 전달되는지 검증."""
    llm = TrackingMockLLM(reply="명령 대신 일반 대화 응답")
    eng = Engines(
        llm=llm,
        tts=MockTTS(),
        stt_initial=MockSTT("개발 정보 보여 줘"),
        make_vad=lambda: EnergyVAD(),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        intent=False,  # 의도 분류 끔
    )
    app = create_app(eng)
    c = TestClient(TestServer(app))
    await c.start_server()

    ws = await c.ws_connect("/ws")
    await _recv_json_or_frame(ws)  # hello

    # 명령 문형을 보냈으나 intent=False이므로 LLM이 호출되어야 함
    await ws.send_str(json.dumps({"type": "text_input", "text": "개발 정보 보여 줘"}))
    msgs = await _drain_turn(ws)

    assert llm.call_count == 1, "intent=False 상태에서는 명령 문형도 LLM을 호출해야 함"
    intent_events = [m for m in msgs if isinstance(m, dict) and m.get("event") == "intent"]
    assert intent_events[0]["intent"] == "TALK"
    assert intent_events[0]["grade"] == "dialog"

    await ws.close()
    await c.close()
