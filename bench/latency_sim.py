"""응답 지연 시뮬레이션 (개발 포인트 문서 §2.3).

모의 LLM·TTS의 설정값으로 '발화 종료 → 재생 시작' 지연의 P50·P90을 본다.
턴 종료 감지 대기(--eot-ms)와 선행 생성 겹침(--preemptive-ms)은 앞에 더하거나 뺀다.
실측값이 생기면 이 숫자들을 실측값으로 바꿔 예산을 다시 맞춘다.

실행 예:
    python bench/latency_sim.py --ttft-ms 400 --tps 40 --tts-first-ms 100
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from voice_pipeline.llm import MockLLM  # noqa: E402
from voice_pipeline.pipeline import VoicePipeline  # noqa: E402
from voice_pipeline.sinks import MemorySink  # noqa: E402
from voice_pipeline.trace import FIRST_CHUNK, LLM_FIRST_TOKEN, PLAYBACK_START, TTS_FIRST_AUDIO, summarize  # noqa: E402
from voice_pipeline.tts import MockTTS  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ttft-ms", type=float, default=400)
    ap.add_argument("--tps", type=float, default=40)
    ap.add_argument("--tts-first-ms", type=float, default=100)
    ap.add_argument("--rtf", type=float, default=0.2)
    ap.add_argument("--first-min", type=int, default=8)
    ap.add_argument("--eot-ms", type=float, default=250, help="턴 종료 감지 대기 (§3.1)")
    ap.add_argument("--stt-ms", type=float, default=100, help="STT 최종 전사 지연")
    ap.add_argument("--preemptive-ms", type=float, default=200, help="선행 생성으로 겹치는 시간 (§3.2)")
    ap.add_argument("--turns", type=int, default=10)
    args = ap.parse_args()

    from voice_pipeline.chunker import SemanticChunker

    llm = MockLLM(ttft_ms=args.ttft_ms, tokens_per_s=args.tps)
    tts = MockTTS(first_audio_ms=args.tts_first_ms, rtf=args.rtf)
    traces = []
    for _ in range(args.turns):
        p = VoicePipeline(llm, tts, MemorySink(), chunker_factory=lambda: SemanticChunker(first_min=args.first_min))
        traces.append((await p.respond("결과 알려줘")).trace)

    print("구간 (LLM 요청 시점 기준)        P50      P90")
    for name in (LLM_FIRST_TOKEN, FIRST_CHUNK, TTS_FIRST_AUDIO, PLAYBACK_START):
        s = summarize(traces, name)
        print(f"  {name:<28} {s['p50']:7.1f}  {s['p90']:7.1f}")
    s = summarize(traces, PLAYBACK_START)
    pre = args.eot_ms + args.stt_ms - args.preemptive_ms
    print(f"\n발화 종료 → 재생 시작 (턴 감지 {args.eot_ms:.0f} + STT {args.stt_ms:.0f} "
          f"- 겹침 {args.preemptive_ms:.0f} 반영): P50 {s['p50'] + pre:.0f}ms / P90 {s['p90'] + pre:.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())
