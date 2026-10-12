"""웹 데모 테스트: 가짜 STT·VAD + MockLLM/MockTTS, aiohttp 테스트 클라이언트."""

import asyncio
import json

import numpy as np
import pytest_asyncio
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.bridge.protocol import unpack_frame
from voice_pipeline.bridge.ws_server_sink import WebSocketServerSink
from voice_pipeline.audio import AudioFrame
from voice_pipeline.llm import MockLLM
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT

LOUD = (np.full(320, 8000, dtype=np.int16)).tobytes()   # 20 ms
QUIET = bytes(640)


@pytest_asyncio.fixture
async def client():
    eng = Engines(MockLLM(ttft_ms=20, tokens_per_s=400), MockTTS(first_audio_ms=10, rtf=0.5),
                  MockSTT("마이크 테스트"), lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"})
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    yield c
    await c.close()


async def recv(ws, timeout=5.0):
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type == WSMsgType.TEXT:
        return json.loads(msg.data)
    if msg.type == WSMsgType.BINARY:
        return unpack_frame(msg.data)
    raise AssertionError(f"unexpected {msg.type}")


async def until(ws, pred, out):
    while True:
        m = await recv(ws)
        out.append(m)
        if pred(m):
            return m


def is_type(t):
    return lambda m: isinstance(m, dict) and m["type"] == t


async def test_text_input_full_turn(client):
    ws = await client.ws_connect("/ws")
    assert (await recv(ws))["type"] == "hello"
    await ws.send_str(json.dumps({"type": "text_input", "text": "혈압 알려줘"}))
    msgs: list = []
    done = await until(ws, is_type("turn_done"), msgs)
    types = [m["type"] for m in msgs if isinstance(m, dict)]
    assert types.index("transcript") < types.index("audio_begin") < types.index("audio_end") < types.index("turn_done")
    assert "assistant_delta" in types
    tr = next(m for m in msgs if isinstance(m, dict) and m["type"] == "transcript")
    assert tr["text"] == "혈압 알려줘" and tr["stt_ms"] == 0
    begin = next(m for m in msgs if isinstance(m, dict) and m["type"] == "audio_begin")
    frames = [m for m in msgs if isinstance(m, tuple)]
    assert frames and all(f[0] == begin["turn_id"] for f in frames)
    assert [f[1] for f in frames] == list(range(len(frames))) and frames[-1][2]
    assert done["turn_id"] == begin["turn_id"] and not done["interrupted"] and done["spoken_text"]
    assert done["trace"]["llm_first_token"] > 0 and done["trace"]["tts_first_audio"] >= done["trace"]["first_chunk"]
    await ws.close()


async def test_playback_started_reaches_trace(client):
    ws = await client.ws_connect("/ws")
    await recv(ws)
    await ws.send_str(json.dumps({"type": "text_input", "text": "안녕"}))
    msgs: list = []
    b = await until(ws, is_type("audio_begin"), msgs)
    await ws.send_str(json.dumps({"type": "playback", "turn_id": b["turn_id"], "event": "started", "played_ms": 0}))
    upd = await until(ws, is_type("trace_update"), msgs)
    assert upd["turn_id"] == b["turn_id"] and upd["trace"]["playback_start"] is not None
    done = await until(ws, is_type("turn_done"), msgs)
    assert done["trace"]["playback_start"] is not None
    await ws.close()


async def test_barge_in_from_mic(client):
    ws = await client.ws_connect("/ws")
    await recv(ws)
    await ws.send_str(json.dumps({"type": "text_input", "text": "길게 말해줘"}))
    msgs: list = []
    first = await until(ws, lambda m: isinstance(m, tuple), msgs)
    tid = first[0]
    for _ in range(3):                      # 말소리 -> 가짜 VAD가 SpeechStart
        await ws.send_bytes(LOUD)
    intr = await until(ws, is_type("interrupt"), msgs)
    assert intr["turn_id"] == tid
    n_before = len([m for m in msgs if isinstance(m, tuple)])
    await ws.send_json({"type": "playback", "turn_id": tid, "event": "stopped", "played_ms": 50})
    after: list = []
    try:                                    # 이후 같은 turn_id 프레임은 없어야 한다
        while True:
            m = await recv(ws, timeout=0.4)
            after.append(m)
    except asyncio.TimeoutError:
        pass
    assert not any(isinstance(m, tuple) and m[0] == tid for m in after)
    assert n_before >= 1
    # 이어서 말이 끝나면 (조용한 프레임) 새 턴이 STT 문장으로 시작한다
    for _ in range(10):
        await ws.send_bytes(QUIET)
    msgs2: list = []
    t = await until(ws, is_type("transcript"), msgs2)
    assert t["text"] == "마이크 테스트"
    done = await until(ws, is_type("turn_done"), msgs2)
    assert done["turn_id"] > tid
    await ws.close()


async def test_sink_message_formats():
    class FakeWS:
        closed = False

        def __init__(self):
            self.sent = []

        async def send_str(self, s):
            self.sent.append(json.loads(s))

        async def send_bytes(self, b):
            self.sent.append(b)

    ws = FakeWS()
    sink = WebSocketServerSink(ws)
    events = []
    sink.playback_listener = lambda *a: events.append(a)
    await sink.begin(3, 24000)
    assert sink.active
    await sink.write(AudioFrame(3, 0, b"\x01\x00" * 480, 24000, False))
    await sink.write(AudioFrame(3, 1, b"\x02\x00" * 480, 24000, True))
    await sink.end(3, 1)
    await sink.avatar_state("speaking", emotion=None, gesture="nod")
    await sink.interrupt(3, 40)
    assert ws.sent[0] == {"type": "audio_begin", "turn_id": 3, "sample_rate": 24000, "channels": 1}
    assert unpack_frame(ws.sent[1])[:3] == (3, 0, False) and unpack_frame(ws.sent[2])[:3] == (3, 1, True)
    assert unpack_frame(ws.sent[2])[3] == b"\x02\x00" * 480
    assert ws.sent[3] == {"type": "audio_end", "turn_id": 3, "last_seq": 1}
    assert ws.sent[4] == {"type": "avatar_state", "state": "speaking", "gesture": "nod"}
    assert ws.sent[5] == {"type": "interrupt", "turn_id": 3, "fade_ms": 40} and not sink.active
    sink.handle_playback({"type": "playback", "turn_id": 3, "event": "progress", "played_ms": 120})
    sink.handle_playback({"type": "playback", "turn_id": 3, "event": "stopped", "played_ms": -1})
    assert sink.played_ms(3) == 120 and events[-1] == (3, "stopped", -1.0)


async def test_hello_and_voice_config():
    tts = MockTTS(first_audio_ms=10, rtf=0.5)
    tts.voices = [{"id": "calm", "label": "차분"}, {"id": "warm", "label": "따뜻"}]
    tts.voice = "calm"
    eng = Engines(MockLLM(ttft_ms=20, tokens_per_s=400), tts, MockSTT("x"), lambda: EnergyVAD(min_silence_ms=100),
                  {"llm": "mock", "tts": "mock", "stt": "mock"}, first_min=5, vad_silence_ms=400)
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        hello = await recv(ws)
        assert hello["first_min"] == 5 and hello["vad_silence_ms"] == 400
        assert [v["id"] for v in hello["voices"]] == ["calm", "warm"] and hello["voice"] == "calm"
        await ws.send_str(json.dumps({"type": "config", "voice": "warm"}))
        await ws.send_str(json.dumps({"type": "config", "voice": "nope"}))
        await ws.send_str(json.dumps({"type": "reset"}))
        await until(ws, is_type("reset_done"), [])
        assert tts.voice == "warm"
        await ws.close()
    finally:
        await c.close()


async def _bc_client(enabled=True):
    from voice_pipeline.backchannel import BackchannelBank
    eng = Engines(MockLLM(ttft_ms=20, tokens_per_s=400), MockTTS(first_audio_ms=10, rtf=0.1),
                  MockSTT("마이크 테스트"), lambda: EnergyVAD(min_silence_ms=100),
                  {"llm": "mock", "tts": "mock", "stt": "mock"},
                  backchannel=BackchannelBank(seed=0) if enabled else None)
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    return c


async def test_backchannel_hello_and_trace():
    c = await _bc_client()
    try:
        ws = await c.ws_connect("/ws")
        assert (await recv(ws))["backchannel"] is True
        await ws.send_str(json.dumps({"type": "text_input", "text": "안녕"}))
        done = await until(ws, is_type("turn_done"), [])
        tr = done["trace"]
        assert tr["backchannel"] is not None and tr["backchannel"] < tr["tts_first_audio"]
        await ws.close()
    finally:
        await c.close()


async def test_backchannel_config_off_has_no_opener():
    c = await _bc_client()
    try:
        ws = await c.ws_connect("/ws")
        await recv(ws)
        await ws.send_str(json.dumps({"type": "config", "backchannel": False}))
        await ws.send_str(json.dumps({"type": "text_input", "text": "안녕"}))
        done = await until(ws, is_type("turn_done"), [])
        assert done["trace"]["backchannel"] is None
        await ws.close()
    finally:
        await c.close()


async def test_backchannel_absent_hello_false():
    c = await _bc_client(enabled=False)
    try:
        ws = await c.ws_connect("/ws")
        assert (await recv(ws))["backchannel"] is False
        await ws.close()
    finally:
        await c.close()
