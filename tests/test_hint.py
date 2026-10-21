from voice_pipeline.llm.hint import emotion_hint


def test_known_emotions_give_hint_with_caveat():
    for tag in ("SAD", "sad", "<|SAD|>".strip("<|>"), "ANGRY", "HAPPY", "FEARFUL"):
        h = emotion_hint(tag)
        assert h and "틀릴 수 있으니" in h


def test_neutral_or_unknown_gives_none():
    assert emotion_hint("NEUTRAL") is None
    assert emotion_hint(None) is None
    assert emotion_hint("EMO_UNKNOWN") is None


# ---------- 세션 통합: 감정 태그가 LLM 요청의 hint가 되고, 화면으로는 나가지 않는다 ----------

import asyncio
import json

import numpy as np
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.llm import MockLLM
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT

LOUD = np.full(320, 8000, dtype=np.int16).tobytes()
QUIET = bytes(640)


class RecordingLLM(MockLLM):
    def __init__(self):
        super().__init__(ttft_ms=20, tokens_per_s=400)
        self.seen: list[list[dict]] = []

    async def stream(self, messages, turn_id):
        self.seen.append([dict(m) for m in messages])
        async for ev in super().stream(messages, turn_id=turn_id):
            yield ev


async def _turn(emotion):
    llm = RecordingLLM()
    eng = Engines(llm, MockTTS(first_audio_ms=10, rtf=0.5), MockSTT("요즘 힘들어요", emotion=emotion),
                  lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"})
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await ws.receive()  # hello
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)
        raw = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), 8)
            if msg.type == WSMsgType.TEXT:
                raw.append(msg.data)
                if json.loads(msg.data)["type"] == "turn_done":
                    break
        await ws.close()
        return llm, raw
    finally:
        await c.close()


async def test_sad_emotion_becomes_hint_for_llm_only():
    llm, raw = await _turn("SAD")
    last_user = [m for m in llm.seen[-1] if m["role"] == "user"][-1]
    assert "슬프게" in last_user.get("hint", "")
    assert not any("슬프게" in r for r in raw)   # 화면(브라우저)으로는 내보내지 않는다


async def test_neutral_emotion_has_no_hint():
    llm, _ = await _turn("NEUTRAL")
    assert "hint" not in [m for m in llm.seen[-1] if m["role"] == "user"][-1]
