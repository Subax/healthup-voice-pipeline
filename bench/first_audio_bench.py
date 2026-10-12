"""첫 소리까지 지연 벤치: 실제 LLM → SemanticChunker(first_min=N) → 정규화 → 실제 CosyVoice (첫 청크만).

LLM이 계속 생성하는 중에 TTS를 요청한다 (같은 GPU 경합 재현).
서버 필요: vLLM 127.0.0.1:8000, CosyVoice 127.0.0.1:8091.

예:  python bench/first_audio_bench.py --first-min 2 4 6 8
     python bench/first_audio_bench.py --first-min 8 --old-prompt   # 이전 프롬프트 기준선
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from llm_bench import PROMPTS  # noqa: E402

import aiohttp  # noqa: E402

from voice_pipeline.chunker import SemanticChunker  # noqa: E402
from voice_pipeline.llm.events import Token  # noqa: E402
from voice_pipeline.llm.openai_stream import OpenAIStreamLLM  # noqa: E402
from voice_pipeline.normalize import normalize_for_tts  # noqa: E402
from voice_pipeline.trace import percentile  # noqa: E402
from voice_pipeline.tts.cosyvoice import CosyVoiceTTS  # noqa: E402

OLD_PROMPT = (
    "당신은 건강 스테이션에서 일하는 따뜻한 건강 상담사입니다. "
    "사용자의 말을 먼저 공감하고, 짧고 쉬운 말로 답하세요. "
    "답은 말로 읽어 주는 두세 문장으로 합니다. "
    "마크다운, 목록, 이모지, 영어는 쓰지 마세요. "
    "숫자는 소리 내어 읽을 수 있게 한글로 풀어 쓰세요."
)


async def wait_tts(url: str, tries: int = 60) -> None:
    async with aiohttp.ClientSession() as s:
        for _ in range(tries):
            try:
                async with s.get(f"{url}/info", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            await asyncio.sleep(2)
    raise RuntimeError("TTS 서버 응답 없음")


async def tts_first_audio(tts: CosyVoiceTTS, text: str) -> float:
    t0 = time.perf_counter()
    async for _ in tts.stream(text):
        ms = (time.perf_counter() - t0) * 1000
        await tts.cancel()
        return ms
    raise RuntimeError("오디오 없음")


async def run_one(llm: OpenAIStreamLLM, tts: CosyVoiceTTS, prompt: str, first_min: int) -> dict:
    chunker = SemanticChunker(first_min=first_min)
    t0 = time.perf_counter()
    t_tok = t_chunk = None
    text, tts_task = "", None
    async for ev in llm.stream([{"role": "user", "content": prompt}], turn_id=0):
        if not isinstance(ev, Token):
            continue
        if t_tok is None:
            t_tok = time.perf_counter()
        if tts_task is None:
            for c in chunker.feed(ev.text):
                text = normalize_for_tts(c)
                if text:
                    t_chunk = time.perf_counter()
                    tts_task = asyncio.create_task(tts_first_audio(tts, text))
                    break
    if tts_task is None:  # 스트림 끝까지 청크가 없으면 close()로 마지막 조각
        for c in chunker.close():
            text = normalize_for_tts(c)
            t_chunk = time.perf_counter()
            tts_task = asyncio.create_task(tts_first_audio(tts, text))
            break
    # LLM은 이미 끝났다. TTS 요청은 LLM 생성 중에 시작했다 (경합 재현)
    ms = await tts_task
    return {"ttft": (t_tok - t0) * 1000, "chunk": (t_chunk - t0) * 1000, "text": text, "tts": ms,
            "audio": (t_chunk - t0) * 1000 + ms}


def stat(xs: list[float]) -> str:
    return f"P50 {percentile(xs, 50):6.0f}  P90 {percentile(xs, 90):6.0f}"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--first-min", type=int, nargs="+", default=[2, 4, 6, 8])
    ap.add_argument("-n", type=int, default=10)
    ap.add_argument("--old-prompt", action="store_true", help="이전 시스템 프롬프트 (기준선)")
    ap.add_argument("--with-old", action="store_true", help="이전 프롬프트(first_min=8)를 기준선으로 같이 돌린다")
    ap.add_argument("--tts-url", default="http://127.0.0.1:8091")
    ap.add_argument("--rounds", type=int, default=1, help="프롬프트 세트 반복 횟수")
    a = ap.parse_args()
    await wait_tts(a.tts_url)
    llm = OpenAIStreamLLM(**({"system_prompt": OLD_PROMPT} if a.old_prompt else {}))
    llm_old = OpenAIStreamLLM(system_prompt=OLD_PROMPT) if a.with_old else None
    tts = CosyVoiceTTS(url=a.tts_url)
    await tts.warmup()
    await llm.warmup()
    await tts_first_audio(tts, "안녕하세요.")  # 워밍업
    # 설정을 프롬프트마다 번갈아 돌려, 다른 프로세스의 GPU 부하 변동이 설정 간에 고르게 섞이게 한다
    configs = [(f"{'old' if a.old_prompt else 'new'} first_min={n}", llm, n) for n in a.first_min]
    if llm_old:
        configs.append(("old first_min=8", llm_old, 8))
    rows: dict[str, list[dict]] = {c[0]: [] for c in configs}
    for _ in range(a.rounds):
        for p in PROMPTS[: a.n]:
            for label, l, n in configs:
                for _try in range(3):
                    try:
                        rows[label].append(await run_one(l, tts, p, n)); break
                    except Exception as e:
                        print(f"  재시도({e!r})"); await wait_tts(a.tts_url)
    for label, rs in rows.items():
        print(f"\n== {label}  (n={len(rs)})")
        print("  LLM 첫 토큰  ", stat([r["ttft"] for r in rs]))
        print("  첫 청크      ", stat([r["chunk"] for r in rs]))
        print("  TTS 첫 소리  ", stat([r["tts"] for r in rs]), " (요청 시작 기준)")
        print("  합계(청크+TTS)", stat([r["audio"] for r in rs]))
        for r in rs:
            print(f"    {r['chunk']:6.0f} {r['tts']:6.0f}  {r['text']}")
    await tts.aclose()
    await llm.close()
    if llm_old:
        await llm_old.close()


if __name__ == "__main__":
    asyncio.run(main())
