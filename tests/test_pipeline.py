import asyncio

from voice_pipeline.llm import MockLLM
from voice_pipeline.pipeline import SAFETY_TEXT_119, VoicePipeline
from voice_pipeline.sinks import MemorySink
from voice_pipeline.trace import FIRST_CHUNK, LLM_FIRST_TOKEN, PLAYBACK_START, TTS_FIRST_AUDIO


def states(sink):
    return [d["state"] for _, k, d in sink.events if k == "avatar_state"]


async def test_normal_turn_marks_and_frames(fast_llm, fast_tts):
    sink = MemorySink()
    p = VoicePipeline(fast_llm, fast_tts, sink)
    r = await p.respond("오늘 결과 알려줘")

    tr = r.trace
    order = [tr.since(m) for m in (LLM_FIRST_TOKEN, FIRST_CHUNK, TTS_FIRST_AUDIO, PLAYBACK_START)]
    assert all(v is not None for v in order) and order == sorted(order)
    assert tr.since(LLM_FIRST_TOKEN) >= 40  # TTFT 설정값

    frames = sink.frames_of(tr.turn_id)
    assert [f.seq for f in frames] == list(range(len(frames)))
    assert frames[-1].is_last
    assert not r.interrupted
    assert "백이십팔에 팔십이" in r.spoken_text and "삼십육 점 오도" in r.spoken_text  # 정규화 + 소수점 보호
    assert p.history[-1] == {"role": "assistant", "content": r.spoken_text}
    assert states(sink)[0] == "thinking" and "speaking" in states(sink) and states(sink)[-1] == "listening"


async def test_barge_in_drops_stale_audio_and_truncates_history(fast_tts):
    llm = MockLLM(ttft_ms=20, tokens_per_s=400)
    sink = MemorySink()
    p = VoicePipeline(llm, fast_tts, sink)

    task = asyncio.create_task(p.respond("결과 알려줘"))
    while not sink.frames:                    # 첫 오디오가 나올 때까지
        await asyncio.sleep(0.005)
    await asyncio.sleep(0.02)
    dead = await p.barge_in()
    n_at_interrupt = len(sink.frames_of(dead))
    r = await task
    await asyncio.sleep(0.05)                 # 늦게 끝난 합성이 있어도

    assert r.interrupted
    assert len(sink.frames_of(dead)) == n_at_interrupt   # 정지 뒤 지난 턴 프레임 0개
    assert p.history[-1]["interrupted"] is True
    full = " ".join(c.text for c in r.chunks)
    assert len(p.history[-1]["content"]) < len(full)

    # 다음 턴은 seq 0부터 정상 재생
    r2 = await p.respond("다시 말해줘")
    frames2 = sink.frames_of(r2.trace.turn_id)
    assert frames2 and frames2[0].seq == 0 and not r2.interrupted
    # 다음 LLM 요청에는 잘린 기록이 들어간다
    sent = llm.requests[-1][1]
    assert any(m.get("interrupted") for m in sent)


async def test_risk_119_stops_and_plays_safety_text(fast_tts):
    llm = MockLLM(ttft_ms=20, tokens_per_s=400, risk_at_token=12)
    sink = MemorySink()
    p = VoicePipeline(llm, fast_tts, sink)
    r = await p.respond("너무 힘들어요")

    assert r.risk_grade == "119" and r.interrupted and p.safety_mode
    assert "safety" in states(sink)
    interrupts = [d["turn_id"] for _, k, d in sink.events if k == "interrupt"]
    assert r.trace.turn_id in interrupts
    safety_turn = max(f.turn_id for f in sink.frames)
    assert safety_turn > r.trace.turn_id and sink.frames_of(safety_turn)[-1].is_last
    assert SAFETY_TEXT_119  # 임시 문구 존재
