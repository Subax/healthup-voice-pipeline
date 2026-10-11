"""상담 LLM 스트리밍 벤치: TTFT, 토큰/초, 글자/초, 총 시간 (실행 중인 vLLM 서버 필요).

예:  python bench/llm_bench.py -n 10
"""

from __future__ import annotations

import argparse
import asyncio
import time

from voice_pipeline.llm.events import Token
from voice_pipeline.llm.openai_stream import OpenAIStreamLLM
from voice_pipeline.trace import percentile

PROMPTS = [
    "요즘 잠을 잘 못 자요.",
    "혈압이 좀 높게 나왔는데 괜찮을까요?",
    "요즘 계속 기운이 없고 우울해요.",
    "회사 일 때문에 스트레스를 너무 많이 받아요.",
    "아침마다 머리가 아픈데 왜 그럴까요?",
    "운동을 시작하고 싶은데 뭐부터 하면 좋을까요?",
    "밥맛이 없어서 며칠째 잘 못 먹고 있어요.",
    "가슴이 두근거릴 때가 자주 있어요.",
    "요즘 사람 만나는 게 귀찮고 외로워요.",
    "체중이 갑자기 줄었는데 걱정돼요.",
]


async def run_one(llm: OpenAIStreamLLM, prompt: str) -> dict:
    t0 = time.perf_counter()
    t_first = None
    n_tok, n_chars = 0, 0
    async for ev in llm.stream([{"role": "user", "content": prompt}], turn_id=0):
        if isinstance(ev, Token):
            if t_first is None:
                t_first = time.perf_counter()
            n_tok += 1
            n_chars += len(ev.text)
    t_end = time.perf_counter()
    if t_first is None:
        raise RuntimeError("토큰이 없다")
    gen_s = max(t_end - t_first, 1e-6)
    # 첫 토큰 이후 구간 기준 (첫 토큰 제외)
    return {
        "ttft_ms": (t_first - t0) * 1000,
        "tps": (n_tok - 1) / gen_s,
        "cps": (n_chars) / gen_s,
        "total_ms": (t_end - t0) * 1000,
        "chars": n_chars,
    }


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-n", type=int, default=10, help="프롬프트 수")
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--model", default=None)
    ap.add_argument("--max-tokens", type=int, default=256)
    args = ap.parse_args()

    llm = OpenAIStreamLLM(base_url=args.base_url, model=args.model, max_tokens=args.max_tokens)
    try:
        print(f"서버 {llm.base_url}  모델 {llm.model}  워밍업 중...")
        await llm.warmup()
        rows = []
        for i in range(args.n):
            p = PROMPTS[i % len(PROMPTS)]
            r = await run_one(llm, p)
            rows.append(r)
            print(f"[{i + 1:2d}] TTFT {r['ttft_ms']:6.0f}ms  {r['tps']:5.1f} tok/s  "
                  f"{r['cps']:5.1f} 글자/s  총 {r['total_ms']:6.0f}ms  {r['chars']}자  | {p}")
    finally:
        await llm.close()

    for key, label in (("ttft_ms", "TTFT ms"), ("tps", "tok/s"), ("cps", "글자/s"), ("total_ms", "총 ms")):
        vals = [r[key] for r in rows]
        print(f"{label:8s} P50 {percentile(vals, 50):8.1f}  P90 {percentile(vals, 90):8.1f}")
    print("주의: 개발 장비(ATOM) 수치이며 최종 수치가 아니다.")


if __name__ == "__main__":
    asyncio.run(main())
