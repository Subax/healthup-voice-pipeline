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
        assert [v["id"] for v in hello["voices"]] == ["auto", "calm", "warm"] and hello["voice"] == "calm"  # 자동은 선택해야 켜진다
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


async def test_stt_options_unavailable_and_switch(monkeypatch):
    from voice_pipeline.stt.registry import STT_ENGINES, STTSpec
    monkeypatch.setitem(STT_ENGINES, "alt", STTSpec("alt", "대체", lambda: MockSTT("대체 인식"), lambda: True))
    monkeypatch.setitem(STT_ENGINES, "gone", STTSpec("gone", "없음", lambda: MockSTT("x"), lambda: False))
    eng = Engines(MockLLM(ttft_ms=20, tokens_per_s=400), MockTTS(first_audio_ms=10, rtf=0.5),
                  MockSTT("마이크 테스트"), lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"})
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        hello = await recv(ws)
        opts = {o["id"]: o["available"] for o in hello["stt_options"]}
        assert hello["stt"] == "mock" and opts["mock"] and opts["alt"] and not opts["gone"]
        await ws.send_json({"type": "config", "stt": "gone"})
        assert (await until(ws, is_type("error"), []))["message"]
        await ws.send_json({"type": "config", "stt": "nope"})
        assert (await until(ws, is_type("error"), []))["message"]
        await ws.send_json({"type": "config", "stt": "alt"})
        assert (await until(ws, is_type("stt_ready"), []))["stt"] == "alt"
        for _ in range(3):
            await ws.send_bytes(LOUD)
        for _ in range(10):
            await ws.send_bytes(QUIET)
        t = await until(ws, is_type("transcript"), [])
        assert t["text"] == "대체 인식" and t["stt"] == "alt" and "stt_ms" in t
        # 다시 인식: 새 턴 없이 결과만
        await until(ws, is_type("turn_done"), [])
        await ws.send_json({"type": "config", "stt": "mock"})
        await until(ws, is_type("stt_ready"), [])
        await ws.send_json({"type": "retranscribe"})
        msgs: list = []
        r = await until(ws, is_type("retranscript"), msgs)
        assert r["stt"] == "mock" and r["text"] == "마이크 테스트"
        try:
            while True:
                msgs.append(await recv(ws, timeout=0.4))
        except asyncio.TimeoutError:
            pass
        assert not any(isinstance(m, dict) and m["type"] in ("turn_done", "audio_begin", "transcript") for m in msgs)
        await ws.close()
    finally:
        await c.close()


async def test_retranscribe_without_mic_errors(client):
    ws = await client.ws_connect("/ws")
    await recv(ws)
    await ws.send_json({"type": "retranscribe"})
    assert (await until(ws, is_type("error"), []))["message"]
    await ws.close()


async def test_stop_interrupts_and_returns_to_listening(client):
    ws = await client.ws_connect("/ws")
    await recv(ws)
    await ws.send_json({"type": "text_input", "text": "길게 말해줘"})
    msgs: list = []
    await until(ws, lambda m: isinstance(m, tuple), msgs)
    await ws.send_json({"type": "stop"})
    done = await until(ws, is_type("turn_done"), msgs)
    assert done["interrupted"]
    assert any(isinstance(m, dict) and m["type"] == "interrupt" for m in msgs)
    st = await until(ws, lambda m: isinstance(m, dict) and m["type"] == "avatar_state" and m["state"] == "listening", msgs)
    assert st["state"] == "listening"
    await ws.close()


class RecordingTTS(MockTTS):
    """합성할 때 쓴 voice 를 기록한다."""

    def __init__(self):
        super().__init__(first_audio_ms=10, rtf=0.5)
        self.voices = [{"id": v, "label": v} for v in ("calm", "comfort", "warm", "steady")]
        self.voice = "calm"
        self.used: list[str | None] = []

    async def stream(self, text):
        self.used.append(self.voice)
        async for c in super().stream(text):
            yield c


async def _tone_client(emotion):
    tts = RecordingTTS()
    eng = Engines(MockLLM(ttft_ms=20, tokens_per_s=400), tts, MockSTT("마이크 테스트", emotion=emotion),
                  lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"})
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    return c, tts


async def _mic_turn(ws):
    for _ in range(5):
        await ws.send_bytes(LOUD * 2)
    for _ in range(10):
        await ws.send_bytes(QUIET)
    msgs: list = []
    await until(ws, is_type("turn_done"), msgs)
    return next(m for m in msgs if isinstance(m, dict) and m["type"] == "transcript")


async def test_auto_voice_picks_tone_from_emotion():
    c, tts = await _tone_client("SAD")
    try:
        ws = await c.ws_connect("/ws")
        hello = await recv(ws)
        assert hello["voice"] != "auto" and hello["voices"][0]["id"] == "auto"
        assert "auto" not in [v["id"] for v in tts.voices]  # 서버 목록에는 auto 가 없다
        await ws.send_str(json.dumps({"type": "config", "voice": "auto"}))   # 자동 톤은 선택했을 때만 켜진다
        tr = await _mic_turn(ws)
        assert tr["emotion"] == "SAD" and tr["tone"] == "comfort"
        assert tts.used and set(tts.used) == {"comfort"}
        # 글자 입력은 감정이 없으므로 calm
        await ws.send_str(json.dumps({"type": "text_input", "text": "안녕"}))
        msgs: list = []
        await until(ws, is_type("turn_done"), msgs)
        assert next(m for m in msgs if isinstance(m, dict) and m["type"] == "transcript")["tone"] == "calm"
        assert tts.used[-1] == "calm" and "auto" not in tts.used
        await ws.close()
    finally:
        await c.close()


async def test_fixed_voice_ignores_emotion():
    c, tts = await _tone_client("ANGRY")
    try:
        ws = await c.ws_connect("/ws")
        await recv(ws)
        await ws.send_str(json.dumps({"type": "config", "voice": "warm"}))
        tr = await _mic_turn(ws)
        assert tr["tone"] is None and set(tts.used) == {"warm"}
        await ws.send_str(json.dumps({"type": "config", "voice": "auto"}))
        tr = await _mic_turn(ws)
        assert tr["tone"] == "steady" and tts.used[-1] == "steady"
        await ws.close()
    finally:
        await c.close()


async def _smart_client(tmp_path, stt_text="힘들어요", scorer_p=0.9):
    from voice_pipeline.endpoint.smart_turn import MockEndpointScorer
    eng = Engines(MockLLM(ttft_ms=20, tokens_per_s=400), MockTTS(first_audio_ms=10, rtf=0.5),
                  MockSTT(stt_text), lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"},
                  endpoint_log_dir=tmp_path)
    eng.endpoint_scorer = MockEndpointScorer(scorer_p)
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    return c


async def test_smart_mode_end_to_end(tmp_path):
    c = await _smart_client(tmp_path, "그랬는데")
    try:
        ws = await c.ws_connect("/ws")
        hello = await recv(ws)
        assert hello["endpoint"] == "smart" and [o["id"] for o in hello["endpoint_options"]] == ["smart", "fixed"]
        msgs: list = []
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)
        done = await until(ws, is_type("turn_done"), msgs)
        ds = [m for m in msgs if isinstance(m, dict)]
        evals = [m for m in ds if m["type"] == "turn_eval"]
        assert [e["stage"] for e in evals] == ["waiting", "confirmed"] and evals[0]["rule_id"] == "EC" and evals[0]["wait_ms"] == 650
        tr = next(m for m in ds if m["type"] == "transcript")
        assert tr["utt_id"] == evals[0]["utt_id"] == done["utt_id"] and tr["text"] == "그랬는데"
        assert tr["turn_id"] == done["turn_id"]
        assert [m["type"] for m in ds].index("turn_eval") < [m["type"] for m in ds].index("transcript")
        t = done["trace"]
        assert t["turn_confirmed"] is not None and t["turn_confirmed"] >= 600 and t["stt_final"] <= t["turn_confirmed"]
        assert list(tmp_path.glob("*.jsonl"))
        await ws.close()
    finally:
        await c.close()


async def test_endpoint_config_switch_to_fixed(tmp_path):
    c = await _smart_client(tmp_path)
    try:
        ws = await c.ws_connect("/ws")
        await recv(ws)
        await ws.send_json({"type": "config", "endpoint": "fixed"})
        assert (await until(ws, is_type("endpoint_ready"), []))["endpoint"] == "fixed"
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)
        msgs: list = []
        done = await until(ws, is_type("turn_done"), msgs)
        assert not any(isinstance(m, dict) and m["type"] == "turn_eval" for m in msgs)
        assert done["utt_id"] == 1 and done["trace"]["turn_confirmed"] is not None
        await ws.close()
    finally:
        await c.close()


# ---------- 확정 직후 다시 말함 (reopen) ----------

class SeqSTT:
    """호출마다 texts를 차례로 돌려준다."""

    sample_rate = 16000

    def __init__(self, texts):
        self.texts = list(texts)

    async def warmup(self):
        return None

    async def transcribe(self, pcm16):
        from voice_pipeline.web.mocks import MockSTTResult
        return MockSTTResult(self.texts.pop(0) if self.texts else "", elapsed_ms=1.0)


class RecLLM(MockLLM):
    """LLM이 받은 messages를 기록한다."""

    def __init__(self):
        super().__init__(ttft_ms=20, tokens_per_s=400)
        self.calls: list[list[dict]] = []

    async def stream(self, messages, turn_id):
        self.calls.append([dict(m) for m in messages])
        async for ev in super().stream(messages, turn_id=turn_id):
            yield ev


async def _reopen_client(tmp_path, tts_first_ms):
    from voice_pipeline.endpoint.smart_turn import MockEndpointScorer
    llm = RecLLM()
    eng = Engines(llm, MockTTS(first_audio_ms=tts_first_ms, rtf=0.5), SeqSTT(["그랬는데", "힘들었어요"]),
                  lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"},
                  endpoint_log_dir=tmp_path)
    eng.endpoint_scorer = MockEndpointScorer(0.9)
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    return c, llm


async def _speak_and_pause(ws):
    for _ in range(5):
        await ws.send_bytes(LOUD * 2)
    for _ in range(10):
        await ws.send_bytes(QUIET)


async def test_reopen_after_confirm_before_tts_audio(tmp_path):
    c, llm = await _reopen_client(tmp_path, tts_first_ms=3000)
    try:
        ws = await c.ws_connect("/ws")
        await recv(ws)
        await _speak_and_pause(ws)
        msgs: list = []
        await until(ws, lambda m: isinstance(m, dict) and m["type"] == "turn_eval" and m["stage"] == "confirmed", msgs)
        await asyncio.sleep(0.2)               # 본 응답 소리는 아직 없다 (TTS 3초)
        for _ in range(3):
            await ws.send_bytes(LOUD * 2)      # 다시 말한다
        re = await until(ws, lambda m: isinstance(m, dict) and m["type"] == "turn_eval" and m["stage"] == "reopened", msgs)
        assert "이어 붙임" in re["reason"] and re["text"] == "그랬는데"
        for _ in range(5):
            await ws.send_bytes(LOUD * 2)
        for _ in range(10):
            await ws.send_bytes(QUIET)
        done = await until(ws, lambda m: isinstance(m, dict) and m["type"] == "turn_done" and not m["interrupted"], msgs)
        tr = [m for m in msgs if isinstance(m, dict) and m["type"] == "transcript"]
        assert tr[-1]["text"] == "그랬는데 힘들었어요" and tr[-1]["utt_id"] == re["utt_id"] == done["utt_id"]
        users = [m for m in llm.calls[-1] if m["role"] == "user"]
        assert [m["content"] for m in users] == ["그랬는데 힘들었어요"]   # 고아 user 메시지 없음
        assert not any(m["role"] == "assistant" for m in llm.calls[-1])
        log = "".join(f.read_text(encoding="utf-8") for f in tmp_path.glob("*.jsonl"))
        assert '"stage": "reopened"' in log
        await ws.close()
    finally:
        await c.close()


async def test_no_reopen_after_tts_audio_started(tmp_path):
    c, _ = await _reopen_client(tmp_path, tts_first_ms=10)
    try:
        ws = await c.ws_connect("/ws")
        await recv(ws)
        await _speak_and_pause(ws)
        msgs: list = []
        await until(ws, lambda m: isinstance(m, tuple), msgs)   # 본 응답 첫 프레임
        for _ in range(3):
            await ws.send_bytes(LOUD * 2)
        await until(ws, is_type("interrupt"), msgs)
        assert not any(isinstance(m, dict) and m.get("stage") == "reopened" for m in msgs)
        await ws.close()
    finally:
        await c.close()
