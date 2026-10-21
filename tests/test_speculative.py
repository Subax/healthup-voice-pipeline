"""선행 생성(speculative generation) 테스트: mock LLM/TTS/MemorySink, 가짜 STT·WS. 모델 파일이 필요 없다."""

import asyncio
import json
import time

import pytest

from voice_pipeline.endpoint.smart_turn import MockEndpointScorer
from voice_pipeline.endpoint.turn import ConfirmedTurn, TurnEndpointer
from voice_pipeline.llm import MockLLM
from voice_pipeline.llm.events import Token
from voice_pipeline.pipeline import SAFETY_TEXT_119, VoicePipeline
from voice_pipeline.sinks import MemorySink
from voice_pipeline.stt.base import STTResult
from voice_pipeline.trace import PLAYBACK_START, SPEC_COMMIT, SPEC_START, TurnTrace
from voice_pipeline.tts import MockTTS
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT
from voice_pipeline.web.session import DemoSession

OPENER = b"\x01\x00" * 4800   # 24 kHz 200 ms


class CountTTS(MockTTS):
    """cancel() 호출 수를 센다."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.cancels = 0

    async def cancel(self) -> None:
        self.cancels += 1


def kinds(sink):
    return [k for _, k, _ in sink.events]


# ---------- 파이프라인 ----------

async def test_nothing_reaches_sink_before_commit(fast_llm, fast_tts):
    sink = MemorySink()
    p = VoicePipeline(fast_llm, fast_tts, sink)
    pt = await p.prepare("오늘 결과 알려줘", opener=OPENER)
    await asyncio.sleep(0.4)   # 합성이 끝날 만큼 기다려도
    assert sink.events == [] and sink.frames == []
    r = await pt.commit()
    assert not r.interrupted and r.spoken_text


async def test_commit_order_and_turn_id(fast_llm, fast_tts):
    sink = MemorySink()
    p = VoicePipeline(fast_llm, fast_tts, sink)
    pt = await p.prepare("오늘 결과 알려줘", opener=OPENER)
    await asyncio.sleep(0.3)
    r = await pt.commit()
    tid = pt.turn_id
    assert r.trace.turn_id == tid and p.history[-1] == {"role": "assistant", "content": r.spoken_text}
    ev = [(k, d.get("turn_id")) for _, k, d in sink.events]
    ib = ev.index(("audio_begin", tid))
    assert all(k == "avatar_state" for k, _ in ev[:ib]) and ev[-2] == ("audio_end", tid)
    assert ev.index(("audio_end", tid)) > ib
    assert sink.events[0][2]["state"] == "thinking" and sink.events[-1][2]["state"] == "listening"
    frames = sink.frames_of(tid)
    assert frames and [f.seq for f in frames] == list(range(len(frames))) and frames[-1].is_last
    assert all(f.turn_id == tid for f in sink.frames)
    # 맞장구(200ms = 10프레임)가 가장 먼저 나간다
    assert r.opener_ms == pytest.approx(200, abs=1)
    first = frames[0].pcm16
    assert first[:2] == b"\x01\x00" and frames[9].pcm16[:2] == b"\x01\x00" and frames[10].pcm16 != frames[9].pcm16
    m = r.trace.marks
    assert SPEC_START in m and SPEC_COMMIT in m and m[SPEC_START] < m[SPEC_COMMIT]
    # 재생 시작·맞장구 마크는 commit 뒤 실제로 쓴 시각이다
    assert m[PLAYBACK_START] >= m[SPEC_COMMIT]


async def test_abort_sends_nothing_and_restores_history(fast_llm):
    tts = CountTTS(first_audio_ms=10, rtf=0.02)
    sink = MemorySink()
    p = VoicePipeline(fast_llm, tts, sink)
    p.history.append({"role": "user", "content": "앞선 말"})
    p.history.append({"role": "assistant", "content": "앞선 답"})
    before = list(p.history)
    pt = await p.prepare("지금 말", opener=OPENER)
    assert p.history[-1] == {"role": "user", "content": "지금 말"}
    await asyncio.sleep(0.15)
    await pt.abort()
    await asyncio.sleep(0.1)
    assert sink.events == [] and sink.frames == []   # interrupt도 없다
    assert p.history == before and tts.cancels == 1 and p.pending is None
    with pytest.raises(RuntimeError):
        await pt.commit()
    # 이어서 정상 턴
    r = await p.respond("다음 말")
    assert not r.interrupted and sink.frames_of(r.trace.turn_id)


async def test_new_prepare_and_barge_in_abort_pending(fast_llm, fast_tts):
    sink = MemorySink()
    p = VoicePipeline(fast_llm, fast_tts, sink)
    a = await p.prepare("첫째")
    b = await p.prepare("둘째")
    assert a.state == "aborted" and p.pending is b
    assert [m["content"] for m in p.history] == ["둘째"]
    await p.barge_in()
    assert b.state == "aborted" and p.pending is None and p.history == []
    assert sink.events == []


async def test_risk_119_during_speculation_waits_for_commit(fast_tts):
    llm = MockLLM(ttft_ms=20, tokens_per_s=400, risk_at_token=12)
    sink = MemorySink()
    p = VoicePipeline(llm, fast_tts, sink)
    pt = await p.prepare("너무 힘들어요")
    await asyncio.sleep(0.4)   # RiskUpdate가 이미 나왔다
    assert sink.events == [] and sink.frames == [] and not p.safety_mode
    r = await pt.commit()
    assert r.risk_grade == "119" and r.interrupted and p.safety_mode
    states = [d["state"] for _, k, d in sink.events if k == "avatar_state"]
    assert "safety" in states
    assert pt.turn_id in [d["turn_id"] for _, k, d in sink.events if k == "interrupt"]
    assert not sink.frames_of(pt.turn_id)   # 선행 중 만든 소리는 나가지 않았다
    safety_turn = max(f.turn_id for f in sink.frames)
    assert safety_turn > pt.turn_id and sink.frames_of(safety_turn)[-1].is_last
    assert SAFETY_TEXT_119


async def test_respond_unchanged_without_speculation(fast_llm, fast_tts):
    sink = MemorySink()
    p = VoicePipeline(fast_llm, fast_tts, sink)
    r = await p.respond("오늘 결과 알려줘")
    assert SPEC_START not in r.trace.marks and SPEC_COMMIT not in r.trace.marks
    assert kinds(sink)[0] == "avatar_state" and "audio_begin" in kinds(sink) and kinds(sink)[-1] == "avatar_state"


async def _first_frame_after(sink, t0):
    while not sink.frames:
        await asyncio.sleep(0.002)
    return time.monotonic() - t0


async def test_speculation_cuts_confirm_to_first_frame_latency():
    """LLM 첫 토큰 100ms + TTS 첫 청크 100ms, 확정 대기 300ms."""
    def make():
        sink = MemorySink()
        stamps = []
        sink.playback_listener = lambda tid, ev, ms: stamps.append(time.monotonic()) if ev == "started" else None
        llm = MockLLM(ttft_ms=100, tokens_per_s=400)
        return VoicePipeline(llm, MockTTS(first_audio_ms=100, rtf=0.02), sink), sink, stamps

    wait = 0.3
    # OFF: 대기 뒤 시작
    p, sink, stamps = make()
    await asyncio.sleep(wait)
    t0 = time.monotonic()
    task = asyncio.create_task(p.respond("혈압 알려줘"))
    off = await _first_frame_after(sink, t0)
    await task
    # ON: 대기 중에 선행
    p, sink, stamps = make()
    pt = await p.prepare("혈압 알려줘")
    await asyncio.sleep(wait)
    t0 = time.monotonic()
    task = asyncio.create_task(pt.commit())
    on = await _first_frame_after(sink, t0)
    await task
    print(f"확정 -> 첫 프레임: 선행 OFF {off * 1000:.0f}ms, 선행 ON {on * 1000:.0f}ms")
    assert off >= 0.19 and on < 0.06 and on < off / 3


# ---------- TurnEndpointer 콜백 ----------

class FakeSTT:
    def __init__(self, texts):
        self.texts = list(texts)

    async def transcribe(self, pcm16):
        return STTResult(self.texts.pop(0) if self.texts else "", "ko", emotion="NEUTRAL", elapsed_ms=1.0)


class EpHarness:
    def __init__(self, texts, p=0.9, **kw):
        self.spec, self.cancels, self.confirmed = [], 0, []
        self.ep = TurnEndpointer(FakeSTT(texts), MockEndpointScorer(p) if p is not None else None, self._emit, self._confirm,
                                 stt_id="fake", on_speculate=self._on_spec, on_speculate_cancel=self._on_cancel, **kw)

    async def _emit(self, ev):
        pass

    async def _confirm(self, turn):
        self.confirmed.append(turn)

    async def _on_spec(self, text, utt_id, last, pcm):
        self.spec.append((text, utt_id, last, pcm))

    async def _on_cancel(self):
        self.cancels += 1


pytest.importorskip("kiwipiepy")
from voice_pipeline.endpoint.ending import classify_ending  # noqa: E402

classify_ending("힘들어요")   # Kiwi 첫 로드가 대기 시간을 먹지 않게 미리 데운다


async def test_endpointer_speculates_on_ef_and_not_cancelled_by_confirm():
    h = EpHarness(["힘들어요"])
    await h.ep.on_pause(b"\x01\x00" * 800, time.monotonic())
    await asyncio.sleep(0.1)
    assert len(h.spec) == 1 and h.spec[0][:2] == ("힘들어요", 1) and h.spec[0][2].emotion == "NEUTRAL"
    assert h.spec[0][3] == b"\x01\x00" * 800
    await asyncio.sleep(0.3)
    assert h.confirmed and h.cancels == 0 and len(h.spec) == 1


async def test_endpointer_no_speculation_for_ec_and_empty():
    h = EpHarness(["그랬는데"])   # EC: 650ms 대기, 말이 이어질 가능성
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.1)
    assert h.spec == []
    h2 = EpHarness([""])   # 전사 없음 (NONE_HIGH지만 텍스트가 비었다)
    await h2.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.1)
    assert h2.spec == []


async def test_endpointer_min_remaining_wait():
    h = EpHarness(["힘들어요"])   # 대기 200ms 중 이미 170ms가 지났다 -> 남은 30ms < 60ms
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic() - 0.17)
    await asyncio.sleep(0.1)
    assert h.spec == []


async def test_endpointer_cancel_on_resume_once():
    h = EpHarness(["힘들어요"], p=None)   # EF_LOW 300ms
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.08)
    assert len(h.spec) == 1
    await h.ep.on_voice_onset()
    assert h.cancels == 1
    await h.ep.on_speech_start()
    assert h.cancels == 1   # 같은 선행 상태에서 중복 호출하지 않는다
    await asyncio.sleep(0.4)
    assert not h.confirmed


async def test_endpointer_cancel_on_text_change_and_reset():
    h = EpHarness(["힘들어요", "그랬는데"], p=None)
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.08)
    assert len(h.spec) == 1
    await h.ep.on_pause(b"\x02\x00" * 100, time.monotonic())   # 재발화 신호 없이 새 멈춤: 텍스트가 달라졌다
    await asyncio.sleep(0.15)
    assert h.cancels == 1 and len(h.spec) == 1   # 합친 텍스트는 EC라 새 선행도 없다
    h3 = EpHarness(["힘들어요"], p=None)
    await h3.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.08)
    h3.ep.reset()
    await asyncio.sleep(0.02)
    assert h3.cancels == 1
    h3.ep.reset()
    await asyncio.sleep(0.02)
    assert h3.cancels == 1


async def test_endpointer_without_callbacks_unchanged():
    from voice_pipeline.endpoint.turn import TurnEndpointer as TE
    got = []

    async def emit(ev):
        got.append(ev["stage"])

    async def conf(t):
        got.append("c")

    ep = TE(FakeSTT(["힘들어요"]), MockEndpointScorer(0.9), emit, conf, stt_id="x")
    await ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.4)
    assert got == ["waiting", "confirmed", "c"]


# ---------- DemoSession ----------

class FakeWS:
    closed = False

    def __init__(self):
        self.sent: list = []

    async def send_str(self, s):
        self.sent.append(json.loads(s))

    async def send_bytes(self, b):
        self.sent.append(b)

    def types(self):
        return [m["type"] if isinstance(m, dict) else "frame" for m in self.sent]


class RecLLM(MockLLM):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    async def stream(self, messages, turn_id):
        self.calls.append((time.monotonic(), [dict(m) for m in messages]))
        async for ev in super().stream(messages, turn_id=turn_id):
            yield ev


def make_session(tmp_path, speculate=True, ttft_ms=20, tts_first_ms=10):
    llm = RecLLM(ttft_ms=ttft_ms, tokens_per_s=400)
    eng = Engines(llm, CountTTS(first_audio_ms=tts_first_ms, rtf=0.05), MockSTT("x"), lambda: EnergyVAD(min_silence_ms=100),
                  {"llm": "mock", "tts": "mock", "stt": "mock"}, endpoint_log_dir=tmp_path)
    eng.speculate = speculate
    ws = FakeWS()
    return DemoSession(ws, eng), ws, llm


def confirmed(text, utt_id=1):
    now = time.monotonic()
    return ConfirmedTurn(utt_id, text, b"\x01\x00" * 100, STTResult(text, "ko", emotion="NEUTRAL", elapsed_ms=1.0),
                         now - 0.2, now - 0.19, now, 1.0, "mock", "t", "EF_HIGH")


async def test_session_holds_ui_until_commit_and_uses_spec(tmp_path):
    s, ws, llm = make_session(tmp_path)
    await s._on_speculate("힘들어요", 1, None, b"\x01\x00" * 100)
    await asyncio.sleep(0.4)
    assert ws.sent == [] and len(llm.calls) == 1
    await s._on_turn_confirmed(confirmed("힘들어요"))
    await asyncio.wait_for(s._task, 5)
    types = ws.types()
    assert len(llm.calls) == 1   # 선행 결과를 썼다. LLM을 다시 부르지 않았다
    assert types[0] == "transcript" and types.index("assistant_delta") > 0
    assert types.index("audio_begin") > max(i for i, t in enumerate(types) if t == "assistant_delta")
    assert types[-1] == "turn_done" and ws.sent[-1]["interrupted"] is False
    tid = ws.sent[0]["turn_id"]
    assert all(m["turn_id"] == tid for m in ws.sent if isinstance(m, dict) and "turn_id" in m)
    assert s.pipeline.history[0] == {"role": "user", "content": "힘들어요"} and len(s.pipeline.history) == 2
    assert s._spec is None


async def test_session_text_mismatch_restarts(tmp_path):
    s, ws, llm = make_session(tmp_path)
    await s._on_speculate("힘들어요", 1, None, b"\x01\x00" * 100)
    await asyncio.sleep(0.1)
    await s._on_turn_confirmed(confirmed("힘들어요 정말"))
    await asyncio.wait_for(s._task, 5)
    assert len(llm.calls) == 2
    users = [m["content"] for m in s.pipeline.history if m["role"] == "user"]
    assert users == ["힘들어요 정말"]   # 선행 user 메시지가 남지 않았다
    assert ws.sent[0]["type"] == "transcript" and ws.sent[0]["text"] == "힘들어요 정말"
    assert ws.sent[-1]["type"] == "turn_done" and not ws.sent[-1]["interrupted"]
    assert s.engines.tts.cancels >= 1


async def test_session_cancel_callback_and_stop_abort_spec(tmp_path):
    s, ws, llm = make_session(tmp_path)
    await s._on_speculate("힘들어요", 1, None, b"")
    await s._on_speculate_cancel()
    assert s._spec is None and s.pipeline.history == [] and ws.sent == []
    await s._on_speculate("힘들어요", 2, None, b"")
    await s.on_json(json.dumps({"type": "stop"}))
    assert s._spec is None and s.pipeline.history == []
    assert [m["type"] for m in ws.sent] == ["avatar_state"]   # 스톱 메시지의 listening 하나뿐
    await s._on_speculate("힘들어요", 3, None, b"")
    await s.on_json(json.dumps({"type": "reset"}))
    assert s._spec is None and s.pipeline.history == []
    await s._on_speculate("힘들어요", 4, None, b"")
    await s.close()
    assert s._spec is None and s.pipeline.history == []


async def test_session_speculate_off_is_unchanged(tmp_path):
    s, ws, llm = make_session(tmp_path, speculate=False)
    assert s.endpointer._on_spec is None and s.endpointer._on_spec_cancel is None
    await s._on_turn_confirmed(confirmed("힘들어요"))
    await asyncio.wait_for(s._task, 5)
    types = ws.types()
    assert types[0] == "transcript" and types[-1] == "turn_done" and len(llm.calls) == 1


async def test_session_no_speculation_while_responding(tmp_path):
    s, ws, llm = make_session(tmp_path, ttft_ms=300)
    await s._on_turn_confirmed(confirmed("첫째"))
    await asyncio.sleep(0.05)
    await s._on_speculate("둘째", 2, None, b"")
    assert s._spec is None and len(llm.calls) == 1
    await s._stop_current()


async def test_session_reopen_discards_claimed_spec(tmp_path):
    """확정 직후(commit 전후) 다시 말해 이어 붙이면 history에 고아 user 메시지가 남지 않는다."""
    s, ws, llm = make_session(tmp_path, tts_first_ms=3000)   # 본 응답 소리는 아직 없다
    await s._on_speculate("힘들어요", 1, None, b"")
    await asyncio.sleep(0.05)
    turn = confirmed("힘들어요")
    await s._on_turn_confirmed(turn)
    assert s._last_confirm["hl"] == 0
    assert await s._maybe_reopen() is True
    await asyncio.sleep(0.2)
    assert s.pipeline.history == [] and s._spec is None


async def test_web_end_to_end_speculation(tmp_path, monkeypatch):
    """마이크 -> 가짜 VAD/STT -> 대기 중 선행 -> 확정. 확정(confirmed) 전에는 UI 메시지가 없다."""
    import numpy as np
    from aiohttp import WSMsgType
    from aiohttp.test_utils import TestClient, TestServer
    from voice_pipeline.web.app import create_app

    llm = RecLLM(ttft_ms=20, tokens_per_s=400)
    eng = Engines(llm, MockTTS(first_audio_ms=10, rtf=0.5), MockSTT("힘들어요"), lambda: EnergyVAD(min_silence_ms=100),
                  {"llm": "mock", "tts": "mock", "stt": "mock"}, endpoint_log_dir=tmp_path)
    eng.endpoint_scorer = MockEndpointScorer(0.1)   # EF_LOW: 300ms 대기 (VAD 무음 200ms를 빼고 약 100ms 남는다)
    seen = []
    orig = DemoSession._on_turn_confirmed

    async def spy(self, turn):
        seen.append(time.monotonic())
        await orig(self, turn)

    monkeypatch.setattr(DemoSession, "_on_turn_confirmed", spy)
    c = TestClient(TestServer(create_app(eng)))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        await ws.receive()   # hello
        loud, quiet = np.full(320, 8000, dtype=np.int16).tobytes(), bytes(640)
        for _ in range(5):
            await ws.send_bytes(loud * 2)
        for _ in range(10):
            await ws.send_bytes(quiet)
        types = []
        while True:
            m = await asyncio.wait_for(ws.receive(), 5)
            if m.type == WSMsgType.BINARY:
                types.append("frame")
                continue
            d = json.loads(m.data)
            types.append(d["type"] + (":" + d["stage"] if d["type"] == "turn_eval" else ""))
            if d["type"] == "turn_done":
                break
        ci = types.index("turn_eval:confirmed")
        for t in ("transcript", "assistant_delta", "audio_begin", "frame"):
            assert t not in types[:ci], types
        assert types.index("transcript") < types.index("assistant_delta") < types.index("audio_begin")
        assert len(llm.calls) == 1 and "turn_eval:waiting" in types
        assert llm.calls[0][0] < seen[0] - 0.03   # LLM은 확정보다 먼저 시작했다
        await ws.close()
    finally:
        await c.close()
