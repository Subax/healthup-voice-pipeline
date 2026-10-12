"""미리 합성한 맞장구: 은행(BackchannelBank)과 파이프라인 연동."""

import asyncio

import numpy as np

from voice_pipeline.backchannel import BackchannelBank
from voice_pipeline.llm import MockLLM
from voice_pipeline.pipeline import VoicePipeline
from voice_pipeline.sinks import MemorySink
from voice_pipeline.trace import BACKCHANNEL, TTS_FIRST_AUDIO
from voice_pipeline.tts.mock import MockTTS

RATE = 24000
TONE = 7200   # 300 ms


class PaddedTTS:
    """무음 + 톤 + 무음을 내는 가짜 TTS."""
    sample_rate = RATE
    voice = "orig"

    def __init__(self):
        self.seen: list[str] = []

    async def stream(self, text):
        self.seen.append(self.voice)
        tone = (0.5 * np.sin(np.arange(TONE) / 5) * 32767).astype(np.int16)
        yield np.zeros(RATE // 2, dtype=np.int16).tobytes()
        yield tone.tobytes()
        yield np.zeros(RATE // 2, dtype=np.int16).tobytes()


async def test_bank_pick_even_and_no_repeat(fast_tts):
    bank = BackchannelBank(seed=1)
    await bank.prepare(fast_tts, [None])
    last = None
    for _ in range(20):
        text, pcm = bank.pick(None)
        assert len(pcm) % 2 == 0 and pcm
        assert text != last
        last = text
    assert bank.pick("없는목소리") is None


async def test_bank_trims_silence_and_restores_voice(tmp_path):
    tts = PaddedTTS()
    bank = BackchannelBank(phrases=("네.",))
    await bank.prepare(tts, ["a", "b"])
    assert tts.voice == "orig" and tts.seen == ["a", "b"]
    _, pcm = bank.pick("a")
    n = len(pcm) // 2
    pad = RATE * 60 // 1000
    assert len(pcm) % 2 == 0 and TONE <= n <= TONE + 2 * pad
    paths = bank.save_wavs(tmp_path, RATE)
    assert sorted(p.name for p in paths) == ["a_0.wav", "b_0.wav"]


async def test_bank_skips_failed_phrase():
    class Flaky(PaddedTTS):
        async def stream(self, text):
            if text == "아, 네.":
                raise RuntimeError("fail")
            async for c in super().stream(text):
                yield c

    bank = BackchannelBank()
    await bank.prepare(Flaky(), ["x"])
    assert {bank.pick("x")[0] for _ in range(20)} <= {"네.", "네, 네."}


async def test_fades_applied_to_clip():
    bank = BackchannelBank(phrases=("네.",))
    await bank.prepare(PaddedTTS(), [None])
    x = np.frombuffer(bank.pick(None)[1], dtype=np.int16)
    assert abs(int(x[0])) <= 2 and abs(int(x[-1])) <= 2
    assert np.abs(x).max() > 5000


async def test_duration_filter_drops_short_and_long():
    class Dur(PaddedTTS):
        def __init__(self, n):
            self.n = n

        async def stream(self, text):
            yield (0.5 * np.sin(np.arange(self.n) / 5) * 32767).astype(np.int16).tobytes()

    for n in (RATE * 100 // 1000, RATE * 1500 // 1000):   # 100 ms, 1.5 s
        bank = BackchannelBank(phrases=("네.",))
        await bank.prepare(Dur(n), [None])
        assert bank.pick(None) is None
    bank = BackchannelBank(phrases=("네.",))
    await bank.prepare(Dur(RATE * 500 // 1000), [None])
    assert bank.pick(None) is not None


def _opener(ms=200):
    return (0.3 * np.sin(np.arange(RATE * ms // 1000) / 5) * 32767).astype(np.int16).tobytes()


class _Reply:
    def __init__(self, text):
        self.text = text

    def __call__(self, messages):
        return self.text


async def test_opener_plays_first_with_contiguous_seq(fast_tts):
    sink = MemorySink()
    llm = MockLLM(reply=_Reply("힘드셨겠어요. 조금 쉬어 보세요."), ttft_ms=40, tokens_per_s=400)
    p = VoicePipeline(llm, fast_tts, sink)
    r = await p.respond("피곤해", opener=_opener(200))
    frames = sink.frames_of(r.trace.turn_id)
    assert [f.seq for f in frames] == list(range(len(frames)))
    assert all(f.turn_id == r.trace.turn_id for f in frames)
    assert r.opener_ms == 200
    tr = r.trace
    assert tr.since(BACKCHANNEL) is not None and tr.since(BACKCHANNEL) < tr.since(TTS_FIRST_AUDIO)
    assert "speaking" in [d["state"] for _, k, d in sink.events if k == "avatar_state"]


async def test_no_opener_leaves_marks_clean(fast_tts):
    p = VoicePipeline(MockLLM(ttft_ms=20, tokens_per_s=400), fast_tts, MemorySink())
    r = await p.respond("안녕")
    assert r.trace.since(BACKCHANNEL) is None and r.opener_ms == 0.0


async def _first_chunk_text(reply, opener, fast_tts):
    p = VoicePipeline(MockLLM(reply=_Reply(reply), ttft_ms=20, tokens_per_s=400), fast_tts, MemorySink())
    r = await p.respond("안녕", opener=opener)
    return r.chunks[0].text


async def test_leading_filler_stripped_only_with_opener(fast_tts):
    reply = "네, 힘드셨겠어요. 조금 쉬어 보세요."
    with_op = await _first_chunk_text(reply, _opener(), fast_tts)
    without = await _first_chunk_text(reply, None, fast_tts)
    assert not with_op.startswith("네") and "힘드셨겠어요" in with_op
    assert without.startswith("네")


async def test_words_starting_with_filler_survive(fast_tts):
    for word in ("아침에 일어나서 어떠셨어요?", "음식은 어떠셨어요?"):
        text = await _first_chunk_text(word, _opener(), fast_tts)
        assert text[:2] == word[:2]


async def test_barge_in_history_subtracts_opener():
    tts = MockTTS(first_audio_ms=10, rtf=0.02)
    sink = MemorySink()
    llm = MockLLM(reply=_Reply("힘드셨겠어요. 조금 쉬어 보세요. 물도 드세요."), ttft_ms=20, tokens_per_s=400)
    p = VoicePipeline(llm, tts, sink)
    task = asyncio.create_task(p.respond("피곤해", opener=_opener(300)))
    while not sink.frames:
        await asyncio.sleep(0.005)
    dead = await p.barge_in()
    r = await task
    assert r.interrupted and r.opener_ms > 0
    # 재생 길이가 맞장구 안에서 끝났다면 기록에 assistant 내용이 남지 않는다
    sink.played_ms = lambda tid: r.opener_ms
    p.history.clear()
    p._commit_history(dead, r)
    assert not p.history
