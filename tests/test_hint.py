from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.endpoint.turn import ConfirmedTurn
from voice_pipeline.llm import MockLLM
from voice_pipeline.llm.hint import emotion_hint, fused_hint, is_valid_face_summary
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT, MockSTTResult
from voice_pipeline.web.session import DemoSession

LOUD = np.full(320, 8000, dtype=np.int16).tobytes()
QUIET = bytes(640)


# ==============================================================================
# 1. 단위 시험: 기존 음성 감정 hint 검증
# ==============================================================================

def test_known_emotions_give_hint_with_caveat():
    for tag in ("SAD", "sad", "<|SAD|>".strip("<|>"), "ANGRY", "HAPPY", "FEARFUL"):
        h = emotion_hint(tag)
        assert h and "틀릴 수 있으니" in h


def test_neutral_or_unknown_gives_none():
    assert emotion_hint("NEUTRAL") is None
    assert emotion_hint(None) is None
    assert emotion_hint("EMO_UNKNOWN") is None


# ==============================================================================
# 2. 단위 시험: §4.6 음성 감정 + 얼굴 표정 요약 합침(fused_hint) 표 전체 조합 검증
# ==============================================================================

def test_fused_hint_r0_invalid_or_disabled_face():
    """R0: 얼굴 표정 기능 꺼짐 또는 요약이 유효하지 않을 때 기존 음성 감정 hint 유지."""
    valid_face = {"label": "SAD", "conf": 0.9, "coverage": 0.8, "n_frames": 10}

    # (a) face_enabled = False -> 얼굴 요약이 있어도 무시되고 음성 hint (R0)
    h, rule = fused_hint("SAD", valid_face, face_enabled=False)
    assert rule == "R0"
    assert h == emotion_hint("SAD")
    assert "목소리가 가라앉고" in h

    # (b) face_summary is None -> R0
    h, rule = fused_hint("ANGRY", None, face_enabled=True)
    assert rule == "R0"
    assert h == emotion_hint("ANGRY")

    # (c) coverage 미달 (< 0.4) -> R0
    low_cov = {"label": "SAD", "conf": 0.9, "coverage": 0.2, "n_frames": 10}
    h, rule = fused_hint("SAD", low_cov, face_enabled=True)
    assert rule == "R0"
    assert h == emotion_hint("SAD")

    # (d) conf 미달 (< 0.4) -> R0
    low_conf = {"label": "SAD", "conf": 0.2, "coverage": 0.8, "n_frames": 10}
    h, rule = fused_hint("SAD", low_conf, face_enabled=True)
    assert rule == "R0"
    assert h == emotion_hint("SAD")

    # (e) 음성 중립 + 요약 없음 -> hint=None, R0
    h, rule = fused_hint("NEUTRAL", None, face_enabled=True)
    assert rule == "R0"
    assert h is None


def test_fused_hint_r1_same_negative():
    """R1: 음성 부정 + 표정 같은 쪽 부정 -> '목소리와 표정이 모두' 문형."""
    # SAD + SAD
    face_sad = {"label": "SAD", "conf": 0.85, "coverage": 0.75, "n_frames": 12}
    h, rule = fused_hint("SAD", face_sad, face_enabled=True)
    assert rule == "R1"
    assert "목소리와 표정이 모두" in h
    assert "가라앉고 슬퍼" in h
    assert "틀릴 수 있으니" in h

    # ANGRY + FROWN (정규화)
    face_angry = {"label": "frown", "conf": 80, "coverage": 70, "n_frames": 8}
    h, rule = fused_hint("ANGRY", face_angry, face_enabled=True)
    assert rule == "R1"
    assert "목소리와 표정이 모두" in h
    assert "화나 보입니다" in h

    # FEARFUL + FEAR
    face_fear = {"label": "FEAR", "conf": 0.9, "coverage": 0.8, "n_frames": 10}
    h, rule = fused_hint("FEARFUL", face_fear, face_enabled=True)
    assert rule == "R1"
    assert "목소리와 표정이 모두" in h
    assert "불안해 보입니다" in h

    # DISGUSTED + DISGUST
    face_disgust = {"label": "DISGUST", "conf": 0.9, "coverage": 0.8, "n_frames": 10}
    h, rule = fused_hint("DISGUSTED", face_disgust, face_enabled=True)
    assert rule == "R1"
    assert "목소리와 표정이 모두" in h
    assert "불쾌해 보입니다" in h


def test_fused_hint_r2_different_negative():
    """R2: 음성 부정 + 표정 다른 쪽 부정 -> 음성 우선 hint."""
    face_angry = {"label": "ANGRY", "conf": 0.88, "coverage": 0.7, "n_frames": 10}
    h, rule = fused_hint("SAD", face_angry, face_enabled=True)
    assert rule == "R2"
    assert "목소리가 가라앉고 슬프게 들립니다" in h
    assert "목소리와 표정이 모두" not in h

    face_sad = {"label": "SAD", "conf": 0.9, "coverage": 0.8, "n_frames": 10}
    h, rule = fused_hint("ANGRY", face_sad, face_enabled=True)
    assert rule == "R2"
    assert "목소리가 화나 보입니다" in h


def test_fused_hint_r3_neutral_voice_negative_face():
    """R3: 음성 중립·모름 + 표정 부정 -> '표정이 ~해 보일 수 있습니다' 약한 문형."""
    for voice_tag in ("NEUTRAL", "EMO_UNKNOWN", None, ""):
        # SAD
        face_sad = {"label": "SAD", "conf": 0.85, "coverage": 0.75, "n_frames": 10}
        h, rule = fused_hint(voice_tag, face_sad, face_enabled=True)
        assert rule == "R3"
        assert "표정이 조금 가라앉아 보일 수 있습니다" in h
        assert "틀릴 수 있으니" in h

        # ANGRY
        face_angry = {"label": "ANGRY", "conf": 0.85, "coverage": 0.75, "n_frames": 10}
        h, rule = fused_hint(voice_tag, face_angry, face_enabled=True)
        assert rule == "R3"
        assert "표정이 다소 굳어 보일 수 있습니다" in h

        # FEARFUL
        face_fear = {"label": "FEARFUL", "conf": 0.85, "coverage": 0.75, "n_frames": 10}
        h, rule = fused_hint(voice_tag, face_fear, face_enabled=True)
        assert rule == "R3"
        assert "표정이 다소 불안해 보일 수 있습니다" in h

        # DISGUSTED
        face_disgust = {"label": "DISGUSTED", "conf": 0.85, "coverage": 0.75, "n_frames": 10}
        h, rule = fused_hint(voice_tag, face_disgust, face_enabled=True)
        assert rule == "R3"
        assert "표정이 다소 불편해 보일 수 있습니다" in h


def test_fused_hint_r4_neutral_voice_positive_or_surprise_face():
    """R4: 음성 중립·모름 + 표정 긍정·놀람 -> hint 없음 (밝은 어조를 얼굴만으로 지시하지 않음)."""
    face_happy = {"label": "HAPPY", "conf": 0.95, "coverage": 0.9, "n_frames": 15}
    face_surprise = {"label": "SURPRISE", "conf": 0.8, "coverage": 0.7, "n_frames": 10}

    for voice_tag in ("NEUTRAL", "EMO_UNKNOWN", None, ""):
        h, rule = fused_hint(voice_tag, face_happy, face_enabled=True)
        assert rule == "R4"
        assert h is None

        h, rule = fused_hint(voice_tag, face_surprise, face_enabled=True)
        assert rule == "R4"
        assert h is None


def test_fused_hint_r5_positive_negative_conflict():
    """R5: 부정 ↔ 긍정 충돌 -> 감정 이름 없는 지시('기분을 단정하지 말고... 차분하게')."""
    face_happy = {"label": "HAPPY", "conf": 0.9, "coverage": 0.8, "n_frames": 10}
    face_sad = {"label": "SAD", "conf": 0.9, "coverage": 0.8, "n_frames": 10}

    # 음성 부정 + 표정 긍정
    h, rule = fused_hint("SAD", face_happy, face_enabled=True)
    assert rule == "R5"
    assert "기분을 단정하지 말고 말씀 내용에 맞춰 차분하게" in h
    assert "슬프" not in h and "기뻐" not in h

    # 음성 긍정 + 표정 부정
    h, rule = fused_hint("HAPPY", face_sad, face_enabled=True)
    assert rule == "R5"
    assert "기분을 단정하지 말고 말씀 내용에 맞춰 차분하게" in h


def test_fused_hint_r6_both_positive():
    """R6: 음성 긍정 + 표정 긍정 -> 음성 감정 hint."""
    face_happy = {"label": "smile", "conf": 90, "coverage": 80, "n_frames": 10}
    h, rule = fused_hint("HAPPY", face_happy, face_enabled=True)
    assert rule == "R6"
    assert "목소리가 밝게 들립니다" in h
    assert "틀릴 수 있으니" in h


def test_coverage_denominator_total_speech_frames():
    """F2 리뷰 #3: coverage 분모가 발화 전체 프레임(total_frames) 기준인지 검증."""
    # 발화 전체 프레임 20개 중 얼굴이 2개 프레임에서만 포착된 경우:
    # 비록 face-detected 프레임 기준 비율이 1.0(2/2)이라도,
    # 발화 전체 기준 coverage(2/20 = 0.1) < 0.4이므로 유효하지 않음 (False -> R0)
    summary_low_coverage = {
        "label": "SAD",
        "conf": 0.85,
        "coverage": 1.0,
        "n_frames": 2,
        "total_frames": 20,
    }
    assert is_valid_face_summary(summary_low_coverage) is False
    _, rule = fused_hint("SAD", summary_low_coverage, face_enabled=True)
    assert rule == "R0"

    # 발화 전체 프레임 20개 중 12개 프레임에서 얼굴이 포착된 경우 (12/20 = 0.6 >= 0.4): 유효
    summary_valid_coverage = {
        "label": "SAD",
        "conf": 0.85,
        "coverage": 0.6,
        "n_frames": 12,
        "total_frames": 20,
    }
    assert is_valid_face_summary(summary_valid_coverage) is True
    _, rule = fused_hint("SAD", summary_valid_coverage, face_enabled=True)
    assert rule == "R1"


def test_fused_hint_voice_surprised():
    """F2 리뷰 #4: webapp_plan.md §4.6 표에 따른 음성 SURPRISED 및 표정 SURPRISE 처리 검증."""
    face_surprised = {"label": "SURPRISE", "conf": 0.85, "coverage": 0.75, "n_frames": 10}
    face_happy = {"label": "HAPPY", "conf": 0.85, "coverage": 0.75, "n_frames": 10}
    face_sad = {"label": "SAD", "conf": 0.85, "coverage": 0.75, "n_frames": 10}
    face_neutral = {"label": "NEUTRAL", "conf": 0.85, "coverage": 0.75, "n_frames": 10}

    # 1. 음성 SURPRISED + 표정 SURPRISED -> R6 (긍정/놀람 동의)
    h, rule = fused_hint("SURPRISED", face_surprised, face_enabled=True)
    assert rule == "R6"
    assert "이용자가 놀란 듯합니다" in h

    # 2. 음성 SURPRISED + 표정 HAPPY -> R6 (긍정/놀람 동의)
    h, rule = fused_hint("SURPRISED", face_happy, face_enabled=True)
    assert rule == "R6"
    assert "이용자가 놀란 듯합니다" in h

    # 3. 음성 SURPRISED + 표정 SAD -> R5 (긍정/놀람 ↔ 부정 충돌)
    h, rule = fused_hint("SURPRISED", face_sad, face_enabled=True)
    assert rule == "R5"
    assert "기분을 단정하지 말고 말씀 내용에 맞춰 차분하게" in h

    # 4. 음성 SURPRISED + 표정 NEUTRAL -> R0 (음성 전용)
    h, rule = fused_hint("SURPRISED", face_neutral, face_enabled=True)
    assert rule == "R0"
    assert "이용자가 놀란 듯합니다" in h

    # 5. 음성 SAD + 표정 SURPRISED -> R5 (부정 ↔ 긍정/놀람 충돌)
    h, rule = fused_hint("SAD", face_surprised, face_enabled=True)
    assert rule == "R5"
    assert "기분을 단정하지 말고 말씀 내용에 맞춰 차분하게" in h

    # 6. 음성 NEUTRAL + 표정 SURPRISED -> R4 (hint 없음)
    h, rule = fused_hint("NEUTRAL", face_surprised, face_enabled=True)
    assert rule == "R4"
    assert h is None


# ==============================================================================
# 3. 통합 시험: 세션 WebSocket + face_summary + LLM hint + dev_event + dev_record
# ==============================================================================

class RecordingLLM(MockLLM):
    def __init__(self):
        super().__init__(ttft_ms=20, tokens_per_s=400)
        self.seen: list[list[dict]] = []

    async def stream(self, messages, turn_id):
        self.seen.append([dict(m) for m in messages])
        async for ev in super().stream(messages, turn_id=turn_id):
            yield ev


async def _turn_with_face(emotion: str | None, face_msg: dict | None, face_pref: bool = True):
    llm = RecordingLLM()
    eng = Engines(
        llm=llm,
        tts=MockTTS(first_audio_ms=10, rtf=0.5),
        stt_initial=MockSTT("요즘 많이 힘들어요", emotion=emotion),
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        default_mode="dev",
    )
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await ws.receive()  # hello

        # face 환경설정 전송
        await ws.send_str(json.dumps({"type": "config", "face": face_pref}))
        await asyncio.sleep(0.05)

        # 표정 요약 전송
        if face_msg:
            await ws.send_str(json.dumps(face_msg))
            await asyncio.sleep(0.05)

        # 마이크 발화 주입
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)

        raw_msgs = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT:
                parsed = json.loads(msg.data)
                raw_msgs.append(parsed)
                if parsed.get("type") == "turn_done":
                    break
        await ws.close()
        return llm, raw_msgs, c
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_sad_emotion_becomes_hint_for_llm_only():
    """기존 시험 호환: SAD 음성 -> LLM 어조 지시 hint가 생성되고 브라우저로는 나가지 않음 검증."""
    llm, raw_msgs, _ = await _turn_with_face("SAD", face_msg=None, face_pref=False)
    last_user = [m for m in llm.seen[-1] if m["role"] == "user"][-1]
    assert "슬프게" in last_user.get("hint", "")
    assert not any("슬프게" in json.dumps(m, ensure_ascii=False) for m in raw_msgs if m.get("type") != "dev_event")


@pytest.mark.asyncio
async def test_neutral_emotion_has_no_hint():
    """기존 시험 호환: 중립 음성 + 표정 없음 -> hint 없음."""
    llm, _, _ = await _turn_with_face("NEUTRAL", face_msg=None, face_pref=False)
    assert "hint" not in [m for m in llm.seen[-1] if m["role"] == "user"][-1]


@pytest.mark.asyncio
async def test_session_face_summary_r1_integration():
    """F2 세션 통합: SAD 음성 + SAD 표정 -> R1 합침 hint ('목소리와 표정이 모두') 및 dev_event(hint) 전송 검증."""
    face_summary = {
        "type": "face_summary",
        "label": "SAD",
        "conf": 0.88,
        "coverage": 0.85,
        "n_frames": 10,
    }
    llm, raw_msgs, _ = await _turn_with_face("SAD", face_msg=face_summary, face_pref=True)

    # 1. LLM에 R1 hint ("목소리와 표정이 모두")가 전달되었는지 확인
    last_user = [m for m in llm.seen[-1] if m["role"] == "user"][-1]
    hint = last_user.get("hint", "")
    assert "목소리와 표정이 모두" in hint
    assert "가라앉고 슬퍼" in hint
    assert "틀릴 수 있으니" in hint

    # 2. WebSocket dev_event(event=hint)에 rule="R1", face="SAD"가 수신되었는지 확인
    hint_events = [m for m in raw_msgs if m.get("type") == "dev_event" and m.get("event") == "hint"]
    assert len(hint_events) == 1
    ev = hint_events[0]
    assert ev.get("rule") == "R1"
    assert ev.get("emotion") == "SAD"
    assert ev.get("face") == "SAD"


@pytest.mark.asyncio
async def test_session_face_summary_ignored_when_face_disabled():
    """F2 세션 통합: face=false 일 때는 face_summary가 도착해도 무시되어 R0(음성 전용) 처리 검증."""
    face_summary = {
        "type": "face_summary",
        "label": "SAD",
        "conf": 0.9,
        "coverage": 0.85,
        "n_frames": 10,
    }
    llm, raw_msgs, _ = await _turn_with_face("SAD", face_msg=face_summary, face_pref=False)

    last_user = [m for m in llm.seen[-1] if m["role"] == "user"][-1]
    hint = last_user.get("hint", "")
    # R1 문구가 아니라 기존 R0 문구
    assert "목소리가 가라앉고" in hint
    assert "목소리와 표정이 모두" not in hint

    hint_events = [m for m in raw_msgs if m.get("type") == "dev_event" and m.get("event") == "hint"]
    assert len(hint_events) == 1
    assert hint_events[0].get("rule") == "R0"


@pytest.mark.asyncio
async def test_session_face_summary_not_leaked_to_subsequent_turn():
    """F2 세션 수명주기: 턴 1의 face_summary가 다음 턴 2로 누출되지 않고 매 턴 독립 격리되는지 검증."""
    llm = RecordingLLM()
    eng = Engines(
        llm=llm,
        tts=MockTTS(first_audio_ms=10, rtf=0.5),
        stt_initial=MockSTT("요즘 많이 힘들어요", emotion="SAD"),
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        default_mode="dev",
    )
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await ws.receive()  # hello
        await ws.send_str(json.dumps({"type": "config", "face": True}))
        await asyncio.sleep(0.05)

        # 턴 1: SAD 표정 요약 전송 후 발화
        face_summary = {"type": "face_summary", "label": "SAD", "conf": 0.88, "coverage": 0.85, "n_frames": 10}
        await ws.send_str(json.dumps(face_summary))
        await asyncio.sleep(0.05)
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)

        # 턴 1 완료 대기
        raw_msgs_1 = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT:
                parsed = json.loads(msg.data)
                raw_msgs_1.append(parsed)
                if parsed.get("type") == "turn_done":
                    break

        # 턴 1은 R1 (목소리와 표정 모두 SAD)
        hint_1 = [m for m in llm.seen[0] if m["role"] == "user"][-1].get("hint", "")
        assert "목소리와 표정이 모두" in hint_1
        ev_1 = [m for m in raw_msgs_1 if m.get("type") == "dev_event" and m.get("event") == "hint"][0]
        assert ev_1.get("rule") == "R1"
        assert ev_1.get("face") == "SAD"

        # 턴 2: 표정 요약 전송 없이 두 번째 발화 (SAD 음성만 발생)
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)

        raw_msgs_2 = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT:
                parsed = json.loads(msg.data)
                raw_msgs_2.append(parsed)
                if parsed.get("type") == "turn_done":
                    break

        # 턴 2는 턴 1의 SAD 표정을 재사용하지 않고 R0 (음성 단독 SAD)로 격리되어야 함
        hint_2 = [m for m in llm.seen[1] if m["role"] == "user"][-1].get("hint", "")
        assert "목소리와 표정이 모두" not in hint_2
        assert "목소리가 가라앉고" in hint_2
        ev_2 = [m for m in raw_msgs_2 if m.get("type") == "dev_event" and m.get("event") == "hint"][0]
        assert ev_2.get("rule") == "R0"
        assert ev_2.get("face") is None

        await ws.close()
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_voice_camera_consent_stores_and_turns_on_camera():
    """F2 리뷰 #1: 음성 '카메라 켜 줘' -> '네' 확인 시 config.face=True, consent=True 메시지 전송 검증."""
    stt = MockSTT("카메라 켜 줘")
    llm = RecordingLLM()
    eng = Engines(
        llm=llm,
        tts=MockTTS(first_audio_ms=10, rtf=0.5),
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        default_mode="dev",
    )
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await ws.receive()  # hello
        await asyncio.sleep(0.05)

        # 턴 1: "카메라 켜 줘" -> confirming 상태 진입
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)

        confirm_seen = False
        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT:
                parsed = json.loads(msg.data)
                if parsed.get("type") == "session_state" and parsed.get("state") == "confirming":
                    confirm_seen = True
                if parsed.get("type") == "turn_done":
                    break
        assert confirm_seen, "confirming 상태 진입 실패"

        # 턴 2: STT를 "네"로 전환하여 발화 -> 동의 확정 및 카메라 켜기
        stt.text = "네"
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)

        camera_config_msg = None
        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT:
                parsed = json.loads(msg.data)
                if parsed.get("type") == "config" and parsed.get("face") is True:
                    camera_config_msg = parsed
                if parsed.get("type") == "turn_done":
                    break

        assert camera_config_msg is not None, "config face=True 메시지 미수신"
        assert camera_config_msg.get("consent") is True, "consent=True 플래그 누락"

        await ws.close()
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_face_summary_discarded_after_turn_ends():
    """F2 리뷰 #2: 턴 종료 후 도착한 요약은 버려지고 다음 턴에 누출되지 않으며, 확정 시 계산된 hint가 commit과 dev_event에 일치 검증."""
    stt = MockSTT("처음 발화입니다", emotion="NEUTRAL")
    llm = RecordingLLM()
    eng = Engines(
        llm=llm,
        tts=MockTTS(first_audio_ms=10, rtf=0.5),
        stt_initial=stt,
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
        default_mode="dev",
    )
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await ws.receive()  # hello
        await ws.send_str(json.dumps({"type": "config", "face": True}))
        await asyncio.sleep(0.05)

        # 턴 1 발화: 정상 완료
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)

        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "turn_done":
                break

        # 턴 1 종료 후 (idle) 늦게 도착한 표정 요약 전송 -> 수신 거부되어 버려져야 함
        late_face = {"type": "face_summary", "label": "SAD", "conf": 0.9, "coverage": 0.8, "n_frames": 10}
        await ws.send_str(json.dumps(late_face))
        await asyncio.sleep(0.05)

        # 턴 2 발화: HAPPY 음성 (새 표정 요약 없음)
        stt.text = "두 번째 발화입니다"
        stt.emotion = "HAPPY"
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)

        raw_msgs_2 = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT:
                parsed = json.loads(msg.data)
                raw_msgs_2.append(parsed)
                if parsed.get("type") == "turn_done":
                    break

        # 늦게 도착한 SAD 표정이 버려졌으므로 HAPPY 음성과 충돌(R5)하지 않고 R0(음성 전용) 처리
        hint_ev = [m for m in raw_msgs_2 if m.get("type") == "dev_event" and m.get("event") == "hint"][0]
        assert hint_ev.get("rule") == "R0"
        assert hint_ev.get("face") is None

        # LLM history (commit)에도 확정 때의 hint(R0 - HAPPY 음성 hint)가 정확히 반영됨
        turn2_user = [m for m in llm.seen[-1] if m["role"] == "user"][-1]
        assert "목소리가 밝게 들립니다" in turn2_user.get("hint", "")
        assert "차분하게" not in turn2_user.get("hint", "")

        await ws.close()
    finally:
        await c.close()


# ==============================================================================
# 4. 단위 시험: cgrok 리뷰 (선행 생성 hint와 확정 hint 불일치 시 취소 및 재응답)
# ==============================================================================

class FakeWS:
    closed = False

    def __init__(self):
        self.sent: list = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))

    async def send_bytes(self, b):
        self.sent.append(b)


@pytest.mark.asyncio
async def test_spec_aborted_and_restarted_when_confirmed_hint_differs():
    """cgrok 리뷰 #2: 선행 생성 시점 hint와 확정 시점 hint가 다르면 선행 생성을 버리고 확정 hint로 재응답 검증."""
    llm = RecordingLLM()
    eng = Engines(
        llm=llm,
        tts=MockTTS(first_audio_ms=10, rtf=0.05),
        stt_initial=MockSTT("힘들어요"),
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
    )
    ws = FakeWS()
    sess = DemoSession(ws, eng)
    sess.feature_prefs["face"] = True

    # 1. 표정 요약 없이 선행 생성 시작 (SAD 음성 -> R0 hint)
    await sess._on_speculate("힘들어요", utt_id=1, last=MockSTTResult("힘들어요", emotion="SAD"), pcm16=b"")
    await asyncio.sleep(0.05)
    assert sess._spec is not None
    assert sess._spec.hint_rule == "R0"
    assert len(llm.seen) == 1
    spec_call_hint = [m for m in llm.seen[0] if m["role"] == "user"][-1].get("hint", "")
    assert "목소리가 가라앉고" in spec_call_hint
    assert "목소리와 표정이 모두" not in spec_call_hint

    # 2. 확정 전 표정 요약 도착 (SAD 표정 -> 확정 hint는 R1으로 변경)
    sess._last_face_summary = {"label": "SAD", "conf": 0.9, "coverage": 0.8, "n_frames": 10}

    # 3. 턴 확정
    now = time.monotonic()
    ct = ConfirmedTurn(1, "힘들어요", b"", MockSTTResult("힘들어요", emotion="SAD"), now - 0.2, now - 0.1, now, 1.0, "mock", "t", "EF")
    await sess._on_turn_confirmed(ct)
    await asyncio.wait_for(sess._task, 5)

    # 4. 검증: 선행 생성(R0)은 abort되고, 확정 hint(R1)로 LLM 재호출이 일어남
    assert len(llm.seen) == 2, "선행 생성이 취소되고 확정 hint로 다시 호출되어야 함"
    actual_call_hint = [m for m in llm.seen[1] if m["role"] == "user"][-1].get("hint", "")
    assert "목소리와 표정이 모두" in actual_call_hint

    # 5. 개발 패널(dev_event)과 기록에는 LLM에 실제로 보낸 hint (R1)가 기록됨
    hint_evs = [m for m in ws.sent if isinstance(m, dict) and m.get("type") == "dev_event" and m.get("event") == "hint"]
    assert len(hint_evs) == 1
    assert hint_evs[0]["rule"] == "R1"
    assert hint_evs[0]["face"] == "SAD"


@pytest.mark.asyncio
async def test_spec_kept_and_committed_when_confirmed_hint_matches():
    """cgrok 리뷰 #2: 선행 생성 시점 hint와 확정 시점 hint가 일치하면 선행 생성을 그대로 commit 검증."""
    llm = RecordingLLM()
    eng = Engines(
        llm=llm,
        tts=MockTTS(first_audio_ms=10, rtf=0.05),
        stt_initial=MockSTT("힘들어요"),
        make_vad=lambda: EnergyVAD(min_silence_ms=100),
        labels={"llm": "mock", "tts": "mock", "stt": "mock"},
    )
    ws = FakeWS()
    sess = DemoSession(ws, eng)
    sess.feature_prefs["face"] = True

    # 1. 선행 생성 전에 이미 SAD 표정 요약이 존재 (SAD 음성 + SAD 표정 -> R1 hint로 선행 생성)
    sess._last_face_summary = {"label": "SAD", "conf": 0.9, "coverage": 0.8, "n_frames": 10}
    await sess._on_speculate("힘들어요", utt_id=1, last=MockSTTResult("힘들어요", emotion="SAD"), pcm16=b"")
    await asyncio.sleep(0.05)
    assert sess._spec is not None
    assert sess._spec.hint_rule == "R1"
    assert len(llm.seen) == 1
    spec_call_hint = [m for m in llm.seen[0] if m["role"] == "user"][-1].get("hint", "")
    assert "목소리와 표정이 모두" in spec_call_hint

    # 2. 확정 시점에도 동일한 표정 요약 유지
    sess._last_face_summary = {"label": "SAD", "conf": 0.9, "coverage": 0.8, "n_frames": 10}

    # 3. 턴 확정
    now = time.monotonic()
    ct = ConfirmedTurn(1, "힘들어요", b"", MockSTTResult("힘들어요", emotion="SAD"), now - 0.2, now - 0.1, now, 1.0, "mock", "t", "EF")
    await sess._on_turn_confirmed(ct)
    await asyncio.wait_for(sess._task, 5)

    # 4. 검증: hint가 일치하므로 선행 생성을 버리지 않고 그대로 commit (LLM 추가 호출 없음)
    assert len(llm.seen) == 1, "선행 생성을 그대로 재사용하여 추가 LLM 호출이 없어야 함"

    # 5. 개발 패널(dev_event)에는 LLM에 실제로 보낸 hint (R1)가 기록됨
    hint_evs = [m for m in ws.sent if isinstance(m, dict) and m.get("type") == "dev_event" and m.get("event") == "hint"]
    assert len(hint_evs) == 1
    assert hint_evs[0]["rule"] == "R1"
    assert hint_evs[0]["face"] == "SAD"
