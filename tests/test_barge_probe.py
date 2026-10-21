"""barge-in 맞장구 필터 통합 시험: 말하는 중 짧은 "네"는 무시하고, 다른 말·긴 발화는 끊는다."""

import asyncio
import json

import numpy as np
import pytest
import pytest_asyncio
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.bridge.protocol import unpack_frame
from voice_pipeline.llm import MockLLM
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT

LOUD = (np.full(320, 8000, dtype=np.int16)).tobytes()   # 20 ms
QUIET = bytes(640)


def make_client(stt_text: str):
    eng = Engines(MockLLM(ttft_ms=20, tokens_per_s=400), MockTTS(first_audio_ms=10, rtf=0.5),
                  MockSTT(stt_text), lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"})
    return TestClient(TestServer(create_app(eng)))


async def recv(ws, timeout=5.0):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type == WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type == WSMsgType.BINARY:
        return unpack_frame(msg.data)
    raise AssertionError(f"unexpected {msg.type}")


async def collect(ws, seconds: float) -> list:
    out: list = []
    end = asyncio.get_running_loop().time() + seconds
    while (left := end - asyncio.get_running_loop().time()) > 0:
        try:
            out.append(await recv(ws, timeout=left))
        except asyncio.TimeoutError:
            break
    return out


def types(msgs) -> list[str]:
    return [m["type"] for m in msgs if isinstance(m, dict)]


async def start_answer(ws):
    """긴 답을 시작하고 첫 오디오 프레임이 올 때까지 기다린다."""
    await recv(ws)  # hello
    await ws.send_str(json.dumps({"type": "text_input", "text": "길게 말해줘"}))
    while not isinstance(await recv(ws), tuple):
        pass


async def short_utterance(ws):
    for _ in range(3):
        await ws.send_bytes(LOUD)
    for _ in range(8):
        await ws.send_bytes(QUIET)


@pytest.mark.parametrize("fixed", [False, True])
async def test_short_backchannel_is_ignored_and_ducked(fixed):
    c = make_client("네")
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await start_answer(ws)
        if fixed:
            await ws.send_json({"type": "config", "endpoint": "fixed"})
        await short_utterance(ws)
        msgs = await collect(ws, 1.5)
        t = types(msgs)
        ducks = [m["on"] for m in msgs if isinstance(m, dict) and m["type"] == "duck"]
        assert ducks[:2] == [True, False]
        assert "backchannel_ignored" in t
        assert "interrupt" not in t
        await ws.close()
    finally:
        await c.close()


async def test_real_interruption_after_short_utterance_stops_answer():
    c = make_client("잠깐만요")
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await start_answer(ws)
        await short_utterance(ws)
        msgs = await collect(ws, 2.0)
        t = types(msgs)
        assert "interrupt" in t and "backchannel_ignored" not in t
        assert "transcript" in t   # 끼어든 말이 새 턴으로 이어진다
        await ws.close()
    finally:
        await c.close()


async def test_long_speech_interrupts_without_waiting_for_end():
    c = make_client("네")   # 전사가 맞장구여도 길게 이어지면 끊는다
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await start_answer(ws)
        for _ in range(60):   # 약 1.2초 계속 말한다
            await ws.send_bytes(LOUD)
            await asyncio.sleep(0.02)
        msgs = await collect(ws, 0.5)
        assert "interrupt" in types(msgs)
        await ws.close()
    finally:
        await c.close()


async def test_filter_off_interrupts_immediately():
    c = make_client("네")
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await start_answer(ws)
        await ws.send_json({"type": "config", "barge_filter": False})
        for _ in range(3):
            await ws.send_bytes(LOUD)
        msgs = await collect(ws, 0.5)
        t = types(msgs)
        assert "interrupt" in t and "duck" not in t
        await ws.close()
    finally:
        await c.close()


async def test_speech_while_idle_is_a_normal_turn_even_if_backchannel_word():
    """답이 없을 때 "네"는 질문에 대한 대답일 수 있다. 무시하지 않고 새 턴으로 처리한다."""
    c = make_client("네")
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await recv(ws)
        await short_utterance(ws)
        msgs = await collect(ws, 2.0)
        t = types(msgs)
        assert "transcript" in t and "backchannel_ignored" not in t
        await ws.close()
    finally:
        await c.close()
