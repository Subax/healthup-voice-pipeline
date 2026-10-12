"""TurnEndpointer 테스트: 가짜 STT(지연 조절)·가짜 스코어러. 모델 파일이 필요 없다."""

import asyncio
import json
import time

import pytest

from voice_pipeline.endpoint.rules import MAX_SILENCE_MS
from voice_pipeline.endpoint.smart_turn import MockEndpointScorer
from voice_pipeline.endpoint.turn import ConfirmedTurn, TurnEndpointer
from voice_pipeline.stt.base import STTResult

pytest.importorskip("kiwipiepy")


class FakeSTT:
    """호출마다 texts를 차례로 돌려준다. delay 동안 기다린다. 호출 수·동시 실행 수를 센다."""

    def __init__(self, texts, delay=0.0):
        self.texts, self.delay = list(texts), delay
        self.calls: list[bytes] = []
        self.running = self.max_running = 0

    async def transcribe(self, pcm16):
        self.calls.append(pcm16)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            await asyncio.sleep(self.delay)
            text = self.texts.pop(0) if self.texts else ""
            return STTResult(text, "ko", emotion="NEUTRAL", elapsed_ms=self.delay * 1000)
        finally:
            self.running -= 1


class Harness:
    def __init__(self, stt, scorer=None, **kw):
        self.events: list[dict] = []
        self.confirmed: list[ConfirmedTurn] = []
        self.ep = TurnEndpointer(stt, scorer, self._emit, self._confirm, stt_id="fake", **kw)

    async def _emit(self, ev):
        self.events.append(ev)

    async def _confirm(self, turn):
        self.confirmed.append(turn)

    def stages(self):
        return [e["stage"] for e in self.events]


async def test_ef_high_confirms_after_200ms():
    h = Harness(FakeSTT(["힘들어요"]), MockEndpointScorer(0.9))
    t0 = time.monotonic()
    await h.ep.on_pause(b"\x01\x00" * 800, t0)
    await asyncio.sleep(0.5)
    assert h.stages() == ["waiting", "confirmed"]
    turn = h.confirmed[0]
    assert turn.text == "힘들어요" and turn.utt_id == 1
    assert 0.18 <= turn.confirm_time - t0 < 0.35
    w = h.events[0]
    assert w["wait_ms"] == 200 and w["rule_id"] == "EF_HIGH" and w["smart_turn_p"] == 0.9
    assert w["ending"]["category"] == "EF" and w["stt_id"] == "fake" and w["utt_id"] == 1


async def test_ec_resumed_then_joined_text():
    h = Harness(FakeSTT(["그랬는데", "힘들었어요"]), MockEndpointScorer(0.9))
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.15)
    assert h.stages() == ["waiting"] and h.events[0]["wait_ms"] == 650
    await h.ep.on_speech_start()
    assert h.stages() == ["waiting", "resumed"] and h.events[1]["reason"] == "대기 중 다시 말함"
    await asyncio.sleep(0.7)
    assert not h.confirmed  # 대기 타이머가 취소됐다
    await h.ep.on_pause(b"\x02\x00" * 100, time.monotonic())
    await asyncio.sleep(0.5)
    assert h.stages() == ["waiting", "resumed", "waiting", "confirmed"]
    turn = h.confirmed[0]
    assert turn.text == "그랬는데 힘들었어요" and len(turn.segments) == 2
    assert turn.pcm16 == b"\x01\x00" * 100 + b"\x02\x00" * 100 and turn.last.emotion == "NEUTRAL"


async def test_stt_is_sequential_and_merges_pending():
    stt = FakeSTT(["가", "나 다"], delay=0.15)
    h = Harness(stt, MockEndpointScorer(0.9))
    now = time.monotonic()
    await h.ep.on_pause(b"A" * 10, now)
    await asyncio.sleep(0.03)
    await h.ep.on_pause(b"B" * 10, now + 0.03)   # STT가 도는 중
    await h.ep.on_pause(b"C" * 10, now + 0.04)
    await asyncio.sleep(0.8)
    assert stt.max_running == 1 and len(stt.calls) == 2
    assert stt.calls[1] == b"B" * 10 + b"C" * 10     # 밀린 오디오는 한 요청
    assert h.confirmed and h.confirmed[0].text == "가 나 다"
    assert h.stages().count("waiting") == 1           # 첫 전사 뒤에는 판단하지 않았다


async def test_late_stt_confirms_immediately_when_deadline_passed():
    h = Harness(FakeSTT(["힘들어요"], delay=0.4), MockEndpointScorer(0.9))
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.7)
    assert h.stages() == ["confirmed"]  # 시한(200ms)이 STT 중에 지났다. waiting 없이 확정
    assert h.events[0]["rule_id"] == "EF_HIGH"


async def test_max_silence_cap_reason_when_stt_slow():
    h = Harness(FakeSTT(["저는"], delay=1.1), MockEndpointScorer(0.9))
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(1.4)
    assert h.stages() == ["confirmed"]
    assert h.events[0]["rule_id"] == "MAX_SILENCE" and str(MAX_SILENCE_MS) in h.events[0]["reason"]


async def test_hung_stt_hard_timeout_drops_empty_turn():
    h = Harness(FakeSTT(["x"], delay=30), None, stt_hard_timeout_ms=300)
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.6)
    assert h.stages() == ["dropped"] and not h.confirmed


async def test_hung_stt_keeps_earlier_text():
    stt = FakeSTT(["그랬는데"], delay=0.02)
    h = Harness(stt, MockEndpointScorer(0.9), stt_hard_timeout_ms=300)
    await h.ep.on_pause(b"A" * 10, time.monotonic())
    await asyncio.sleep(0.1)
    await h.ep.on_speech_start()
    stt.delay = 30
    await h.ep.on_pause(b"B" * 10, time.monotonic())
    await asyncio.sleep(0.6)
    assert h.stages()[-1] == "confirmed" and h.events[-1]["rule_id"] == "STT_TIMEOUT"
    assert h.confirmed[0].text == "그랬는데"


async def test_empty_transcript_dropped():
    h = Harness(FakeSTT([""]), MockEndpointScorer(0.9))
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(1.2)
    assert h.stages() == ["waiting", "dropped"] and not h.confirmed


async def test_scorer_timeout_gives_none():
    class Slow:
        def score(self, pcm):
            time.sleep(0.5)
            return 0.99

    h = Harness(FakeSTT(["힘들어요"]), Slow(), score_timeout_s=0.05)
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.6)
    w = h.events[0]
    assert w["smart_turn_p"] is None and w["rule_id"] == "EF_LOW" and w["wait_ms"] == 300


async def test_speech_during_stt_emits_resumed_not_waiting():
    h = Harness(FakeSTT(["그랬는데"], delay=0.2), MockEndpointScorer(0.9))
    await h.ep.on_pause(b"A" * 10, time.monotonic())
    await h.ep.on_speech_start()
    await asyncio.sleep(0.5)
    assert h.stages() == ["resumed"] and not h.confirmed


async def test_jsonl_written(tmp_path):
    h = Harness(FakeSTT(["힘들어요"]), MockEndpointScorer(0.9), log_dir=tmp_path / "log")
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.5)
    files = list((tmp_path / "log").glob("*.jsonl"))
    assert len(files) == 1
    rows = [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]
    assert [r["stage"] for r in rows] == ["waiting", "confirmed"]
    r = rows[0]
    assert r["ts"] and r["mode"] == "smart" and r["stt_engine"] == "fake" and r["text"] == "힘들어요"
    assert {"ending", "smart_turn_p", "wait_ms", "rule_id", "reason", "stt_ms", "since_pause_ms"} <= set(r)


async def test_voice_onset_suspends_timer_no_confirm():
    h = Harness(FakeSTT(["그랬는데", "힘들었어요"]), MockEndpointScorer(0.9))
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.15)
    assert h.stages() == ["waiting"]
    await h.ep.on_voice_onset()
    assert h.stages() == ["waiting", "resumed"]
    await asyncio.sleep(0.3)
    await h.ep.on_speech_start()            # 진짜 말이었다
    await asyncio.sleep(0.8)                # 원래 시한(650ms)이 한참 지났다
    assert not h.confirmed
    await h.ep.on_pause(b"\x02\x00" * 100, time.monotonic())
    await asyncio.sleep(0.5)
    assert h.confirmed[0].text == "그랬는데 힘들었어요"


async def test_voice_onset_noise_resumes_remaining_wait(monkeypatch):
    import voice_pipeline.endpoint.turn as tm
    monkeypatch.setattr(tm, "ONSET_NOISE_MS", 100)
    h = Harness(FakeSTT(["그랬는데"]), MockEndpointScorer(0.9))
    t0 = time.monotonic()
    await h.ep.on_pause(b"\x01\x00" * 100, t0)
    await asyncio.sleep(0.15)               # 남은 대기 약 500ms
    await h.ep.on_voice_onset()
    await asyncio.sleep(0.3)                # 잡음 판정(100ms) 뒤 타이머가 다시 켜졌다
    assert h.stages() == ["waiting", "resumed", "waiting"] and not h.confirmed
    assert 400 <= h.events[2]["wait_ms"] - h.events[2]["since_pause_ms"] <= 520
    await asyncio.sleep(0.7)
    assert h.stages()[-1] == "confirmed"
    assert h.confirmed[0].confirm_time - t0 > 0.72  # 정지한 만큼 늦게 확정 (원래 시한 0.65)


async def test_reopen_then_next_pause_joins_text():
    h = Harness(FakeSTT(["그랬는데", "힘들었어요"]), MockEndpointScorer(0.9))
    await h.ep.on_pause(b"\x01\x00" * 100, time.monotonic())
    await asyncio.sleep(0.9)
    first = h.confirmed[0]
    await h.ep.reopen(first)
    assert h.stages()[-1] == "reopened" and h.events[-1]["utt_id"] == first.utt_id
    await h.ep.on_speech_start()
    await h.ep.on_pause(b"\x02\x00" * 100, time.monotonic())
    await asyncio.sleep(0.5)
    second = h.confirmed[1]
    assert second.utt_id == first.utt_id and second.text == "그랬는데 힘들었어요"
    assert second.pcm16 == first.pcm16 + b"\x02\x00" * 100
