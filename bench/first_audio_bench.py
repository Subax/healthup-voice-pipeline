"""첫 소리까지 지연 벤치: 실제 LLM → SemanticChunker(first_min=N) → 정규화 → 실제 CosyVoice.

서버 필요: vLLM 127.0.0.1:8000, CosyVoice 127.0.0.1:8091.

사용법:
  # 기본 동시 실행:
  python bench/first_audio_bench.py --first-min 2 4 6 8
  # (1a) 프롬프트·청커 단독 비교 (ABAB 교차):
  python bench/first_audio_bench.py --first-min 2 -n 20 --compare-prompt-chunker
  # (1b) 1단계 양보 비교 (유휴·동시·after_chunk1_tts·after_first_frame ABAB 교차):
  python bench/first_audio_bench.py --first-min 2 -n 20 --compare-yield
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from llm_bench import PROMPTS  # noqa: E402

import aiohttp  # noqa: E402

from voice_pipeline.chunker import SemanticChunker  # noqa: E402
from voice_pipeline.llm.events import Token  # noqa: E402
from voice_pipeline.llm.openai_stream import (  # noqa: E402
    DEFAULT_SYSTEM_PROMPT,
    FIRST_PHASE_MAX_TOKENS,
    PROMPT_NO_FILLER,
    OpenAIStreamLLM,
    second_phase_max_tokens,
)
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


def is_filler_chunk(text: str) -> bool:
    """첫 청크가 군더더기('네', '아', '음', '예', '응') 단독인지 판별."""
    s = text.strip().rstrip(",.?!~ ").strip()
    return s in ("네", "아", "음", "예", "응")


def get_latest_server_first_chunk() -> float | None:
    """CosyVoice 컨테이너 로그에서 가장 최근 '첫 청크 Xms' 기록을 가져온다."""
    try:
        p = subprocess.run(
            ["docker", "logs", "--tail", "25", "healthup-cosyvoice"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        lines = [l for l in (p.stderr + "\n" + p.stdout).splitlines() if "첫 청크" in l]
        if lines:
            m = re.search(r"첫 청크 (\d+)ms", lines[-1])
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None


async def wait_vllm_idle(base_url: str = "http://127.0.0.1:8000", timeout: float = 5.0) -> None:
    """vLLM 서버의 실행 중 요청 수가 0이 될 때까지 기다린다."""
    t0 = time.monotonic()
    async with aiohttp.ClientSession() as s:
        while time.monotonic() - t0 < timeout:
            try:
                async with s.get(f"{base_url}/metrics", timeout=aiohttp.ClientTimeout(total=1)) as r:
                    if r.status == 200:
                        txt = await r.text()
                        for line in txt.splitlines():
                            if line.startswith("vllm:num_requests_running"):
                                val = float(line.split()[-1])
                                if val == 0.0:
                                    return
            except Exception:
                pass
            await asyncio.sleep(0.05)


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


async def run_one(
    llm: OpenAIStreamLLM,
    tts: CosyVoiceTTS,
    prompt: str,
    first_min: int,
    skip_filler_comma: bool = False,
) -> dict[str, Any]:
    """기존 방식: LLM 생성 중에 TTS를 호출(경합 재현). 첫 청크 첫 소리만 측정."""
    chunker = SemanticChunker(first_min=first_min, skip_filler_comma=skip_filler_comma)
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
            if text:
                t_chunk = time.perf_counter()
                tts_task = asyncio.create_task(tts_first_audio(tts, text))
                break
    ms = await tts_task if tts_task is not None else 0.0
    ttft = (t_tok - t0) * 1000 if t_tok else 0.0
    chunk_ms = (t_chunk - t0) * 1000 if t_chunk else 0.0
    return {
        "ttft": ttft,
        "chunk": chunk_ms,
        "text": text,
        "len": len(text),
        "is_filler": is_filler_chunk(text),
        "tts": ms,
        "audio": chunk_ms + ms,
        "gap_ms": 0.0,
    }


async def run_yield_turn(
    mode: str,
    llm: OpenAIStreamLLM,
    tts: CosyVoiceTTS,
    prompt: str,
    first_min: int,
    skip_filler_comma: bool,
) -> dict[str, Any]:
    """1b 양보 비교용: 1청크 및 2청크 TTS를 수행하여 gap_ms와 TTFA를 측정한다.

    2청크 TTS는 취소하지 않고 끝까지 수신하여 서버 잠금(lock) 대기 문제를 방지한다.
    """
    await wait_vllm_idle(llm.base_url)
    t0 = time.perf_counter()

    if mode == "idle":
        # 1. LLM으로 청크 2개까지 먼저 생성 (TTS 시작 전 LLM 완료)
        chunker = SemanticChunker(first_min=first_min, skip_filler_comma=skip_filler_comma)
        t_tok = t_chunk1 = None
        chunks: list[str] = []
        stream_iter = llm.stream([{"role": "user", "content": prompt}], turn_id=0)
        try:
            async for ev in stream_iter:
                if not isinstance(ev, Token):
                    continue
                if t_tok is None:
                    t_tok = time.perf_counter()
                for c in chunker.feed(ev.text):
                    t = normalize_for_tts(c)
                    if t:
                        if not chunks:
                            t_chunk1 = time.perf_counter()
                        chunks.append(t)
                        if len(chunks) == 2:
                            break
                if len(chunks) == 2:
                    break
            if len(chunks) < 2:
                for c in chunker.close():
                    t = normalize_for_tts(c)
                    if t:
                        if not chunks:
                            t_chunk1 = time.perf_counter()
                        chunks.append(t)
                        if len(chunks) == 2:
                            break
        finally:
            await stream_iter.aclose()

        chunk1_text = chunks[0] if chunks else ""
        chunk2_text = chunks[1] if len(chunks) > 1 else ""

        # 2. LLM 유휴 상태에서 1청크 TTS
        t_c1_start = time.perf_counter()
        t1 = None
        total_bytes = 0
        async for pcm in tts.stream(chunk1_text):
            if t1 is None:
                t1 = time.perf_counter()
            total_bytes += len(pcm)
        d1_ms = total_bytes / (2 * tts.sample_rate) * 1000
        srv_tts = get_latest_server_first_chunk()

        # 3. 2청크 TTS: 취소하지 않고 끝까지 수신하여 서버 잠금을 해제한다
        t2 = None
        if chunk2_text:
            async for _ in tts.stream(chunk2_text):
                if t2 is None:
                    t2 = time.perf_counter()
        if t2 is None:
            t2 = time.perf_counter()

        gap_ms = max(0.0, (t2 - t1) * 1000 - d1_ms)
        tts_ms = (t1 - t_c1_start) * 1000
        chunk_ms = (t_chunk1 - t0) * 1000 if t_chunk1 else 0.0
        return {
            "ttft": (t_tok - t0) * 1000 if t_tok else 0.0,
            "chunk": chunk_ms,
            "text": chunk1_text,
            "chunk2_text": chunk2_text,
            "len": len(chunk1_text),
            "is_filler": is_filler_chunk(chunk1_text),
            "tts": tts_ms,
            "server_tts": srv_tts,
            "audio": chunk_ms + tts_ms,
            "d1_ms": d1_ms,
            "gap_ms": gap_ms,
        }

    elif mode == "concurrent":
        chunker = SemanticChunker(first_min=first_min, skip_filler_comma=skip_filler_comma)
        t_tok = t_chunk1 = None
        chunk1_text = ""
        chunk2_future: asyncio.Future[str] = asyncio.Future()
        chunk1_ready = asyncio.Event()

        async def consume_llm() -> None:
            nonlocal t_tok, t_chunk1, chunk1_text
            c1_done = False
            stream_iter = llm.stream([{"role": "user", "content": prompt}], turn_id=0)
            try:
                async for ev in stream_iter:
                    if not isinstance(ev, Token):
                        continue
                    if t_tok is None:
                        t_tok = time.perf_counter()
                    for c in chunker.feed(ev.text):
                        t = normalize_for_tts(c)
                        if not t:
                            continue
                        if not c1_done:
                            chunk1_text = t
                            t_chunk1 = time.perf_counter()
                            c1_done = True
                            chunk1_ready.set()
                        elif not chunk2_future.done():
                            chunk2_future.set_result(t)
                            return
                if not c1_done:
                    for c in chunker.close():
                        t = normalize_for_tts(c)
                        if t:
                            chunk1_text = t
                            t_chunk1 = time.perf_counter()
                            c1_done = True
                            chunk1_ready.set()
                            break
                if not chunk2_future.done():
                    for c in chunker.close():
                        t = normalize_for_tts(c)
                        if t:
                            chunk2_future.set_result(t)
                            return
                    chunk2_future.set_result("")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                if not chunk2_future.done():
                    chunk2_future.set_exception(e)
                if not chunk1_ready.is_set():
                    chunk1_ready.set()
            finally:
                await stream_iter.aclose()

        llm_task = asyncio.create_task(consume_llm())
        await chunk1_ready.wait()

        # 1청크 TTS (LLM 계속 생성 중)
        t_c1_start = time.perf_counter()
        t1 = None
        total_bytes = 0
        async for pcm in tts.stream(chunk1_text):
            if t1 is None:
                t1 = time.perf_counter()
            total_bytes += len(pcm)
        d1_ms = total_bytes / (2 * tts.sample_rate) * 1000
        srv_tts = get_latest_server_first_chunk()

        chunk2_text = await chunk2_future

        # 2청크 TTS: 취소하지 않고 끝까지 수신
        t2 = None
        if chunk2_text:
            async for _ in tts.stream(chunk2_text):
                if t2 is None:
                    t2 = time.perf_counter()
        if t2 is None:
            t2 = time.perf_counter()

        llm_task.cancel()
        await asyncio.gather(llm_task, return_exceptions=True)

        gap_ms = max(0.0, (t2 - t1) * 1000 - d1_ms)
        tts_ms = (t1 - t_c1_start) * 1000
        chunk_ms = (t_chunk1 - t0) * 1000 if t_chunk1 else 0.0
        return {
            "ttft": (t_tok - t0) * 1000 if t_tok else 0.0,
            "chunk": chunk_ms,
            "text": chunk1_text,
            "chunk2_text": chunk2_text,
            "len": len(chunk1_text),
            "is_filler": is_filler_chunk(chunk1_text),
            "tts": tts_ms,
            "server_tts": srv_tts,
            "audio": chunk_ms + tts_ms,
            "d1_ms": d1_ms,
            "gap_ms": gap_ms,
        }

    elif mode in ("after_chunk1_tts", "after_first_frame"):
        # Phase 1: 1차 요청 (max_tokens=64)
        chunker = SemanticChunker(first_min=first_min, skip_filler_comma=skip_filler_comma)
        t_tok = t_chunk1 = None
        chunk1_raw = ""
        tok_count = 0

        llm_iter = llm.stream([{"role": "user", "content": prompt}], turn_id=0, max_tokens=FIRST_PHASE_MAX_TOKENS)
        try:
            async for ev in llm_iter:
                if not isinstance(ev, Token):
                    continue
                if t_tok is None:
                    t_tok = time.perf_counter()
                tok_count += 1
                for c in chunker.feed(ev.text):
                    chunk1_raw = c
                    t_chunk1 = time.perf_counter()
                    break
                if chunk1_raw:
                    break
            if not chunk1_raw:
                for c in chunker.close():
                    chunk1_raw = c
                    t_chunk1 = time.perf_counter()
                    break
        finally:
            await llm_iter.aclose()

        chunk1_text = normalize_for_tts(chunk1_raw)
        chunk2_future: asyncio.Future[str] = asyncio.Future()

        async def produce_phase2() -> None:
            c2 = SemanticChunker(first_min=first_min, skip_filler_comma=skip_filler_comma)
            c2.first = False  # 2청크는 문장 끝 기준
            p2_iter = llm.stream(
                [{"role": "user", "content": prompt}, {"role": "assistant", "content": chunk1_raw}],
                turn_id=0,
                continue_final_message=True,
                max_tokens=second_phase_max_tokens(tok_count),
            )
            try:
                async for ev in p2_iter:
                    if not isinstance(ev, Token):
                        continue
                    for c in c2.feed(ev.text):
                        t = normalize_for_tts(c)
                        if t:
                            chunk2_future.set_result(t)
                            return
                if not chunk2_future.done():
                    for c in c2.close():
                        t = normalize_for_tts(c)
                        if t:
                            chunk2_future.set_result(t)
                            return
                if not chunk2_future.done():
                    chunk2_future.set_result("")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                if not chunk2_future.done():
                    chunk2_future.set_exception(e)
            finally:
                await p2_iter.aclose()

        p2_task: asyncio.Task | None = None
        t_c1_start = time.perf_counter()
        t1 = None
        total_bytes = 0

        async for pcm in tts.stream(chunk1_text):
            if t1 is None:
                t1 = time.perf_counter()
                if mode == "after_first_frame":
                    p2_task = asyncio.create_task(produce_phase2())
            total_bytes += len(pcm)
        d1_ms = total_bytes / (2 * tts.sample_rate) * 1000
        srv_tts = get_latest_server_first_chunk()

        if mode == "after_chunk1_tts":
            p2_task = asyncio.create_task(produce_phase2())

        chunk2_text = await chunk2_future

        # 2청크 TTS: 취소하지 않고 끝까지 수신
        t2 = None
        if chunk2_text:
            async for _ in tts.stream(chunk2_text):
                if t2 is None:
                    t2 = time.perf_counter()
        if t2 is None:
            t2 = time.perf_counter()

        if p2_task:
            p2_task.cancel()
            await asyncio.gather(p2_task, return_exceptions=True)

        gap_ms = max(0.0, (t2 - t1) * 1000 - d1_ms)
        tts_ms = (t1 - t_c1_start) * 1000
        chunk_ms = (t_chunk1 - t0) * 1000 if t_chunk1 else 0.0
        return {
            "ttft": (t_tok - t0) * 1000 if t_tok else 0.0,
            "chunk": chunk_ms,
            "text": chunk1_text,
            "chunk2_text": chunk2_text,
            "len": len(chunk1_text),
            "is_filler": is_filler_chunk(chunk1_text),
            "tts": tts_ms,
            "server_tts": srv_tts,
            "audio": chunk_ms + tts_ms,
            "d1_ms": d1_ms,
            "gap_ms": gap_ms,
        }

    else:
        raise ValueError(f"알 수 없는 모드: {mode}")


def stat(xs: list[float]) -> str:
    if not xs:
        return "N/A"
    return f"P50 {percentile(xs, 50):6.0f}  P90 {percentile(xs, 90):6.0f}"


def format_report_block(label: str, rs: list[dict[str, Any]], show_gap: bool = False) -> str:
    lines = [f"\n== {label}  (n={len(rs)})"]
    lines.append(f"  LLM 첫 토큰         {stat([r['ttft'] for r in rs])}")
    lines.append(f"  첫 청크             {stat([r['chunk'] for r in rs])}")

    server_ttses = [r["server_tts"] for r in rs if r.get("server_tts") is not None]
    srv_stat = f" (서버 로그: {stat(server_ttses)})" if server_ttses else ""
    lines.append(f"  TTS 첫 소리         {stat([r['tts'] for r in rs])}  (요청 시작 기준{srv_stat})")
    lines.append(f"  합계(청크+TTS)      {stat([r['audio'] for r in rs])}")

    lengths = [r["len"] for r in rs]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    lines.append(f"  첫 청크 길이(글자)  P50 {percentile(lengths, 50):4.0f}  P90 {percentile(lengths, 90):4.0f}  (평균 {avg_len:4.1f})")

    filler_count = sum(1 for r in rs if r.get("is_filler"))
    lines.append(f"  군더더기 단독 첫 청크 {filler_count}/{len(rs)} ({filler_count / len(rs) * 100:.1f}%)")

    if show_gap:
        gaps = [r["gap_ms"] for r in rs]
        pos_gaps = sum(1 for g in gaps if g > 0.0)
        lines.append(f"  청크 사이 무음(gap) P50 {percentile(gaps, 50):4.0f}  P90 {percentile(gaps, 90):4.0f}  (gap > 0: {pos_gaps}/{len(rs)})")

    lines.append("    chunk    tts(srv)   audio" + ("    gap" if show_gap else "") + "   len  text [1] / [2]")
    for r in rs:
        gap_part = f" {r['gap_ms']:6.0f}" if show_gap else ""
        srv_str = f"{r.get('server_tts', 0):.0f}" if r.get("server_tts") is not None else "   -"
        c2 = f" / {r['chunk2_text']}" if r.get("chunk2_text") else ""
        lines.append(f"   {r['chunk']:6.0f} {r['tts']:4.0f}({srv_str:>4s}) {r['audio']:6.0f}{gap_part}   {r['len']:3d}  {r['text']}{c2}")

    return "\n".join(lines)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--first-min", type=int, nargs="+", default=[2, 4, 6, 8])
    ap.add_argument("-n", type=int, default=10, help="프롬프트 수 (예: 20)")
    ap.add_argument("--prompt", choices=["current", "fixed"], default=None, help="시스템 프롬프트 (current 또는 fixed)")
    ap.add_argument("--chunker", choices=["current", "fixed"], default=None, help="청커 옵션 (current 또는 fixed)")
    ap.add_argument("--compare-prompt-chunker", action="store_true", help="(1a) current vs fixed 교차 측정")
    ap.add_argument("--compare-yield", action="store_true", help="(1b) 유휴·동시·after_chunk1_tts·after_first_frame 교차 측정")
    ap.add_argument("--old-prompt", action="store_true", help="이전 시스템 프롬프트 (OLD_PROMPT 기준선)")
    ap.add_argument("--with-old", action="store_true", help="이전 프롬프트(first_min=8)를 기준선으로 같이 돌린다")
    ap.add_argument("--tts-url", default="http://127.0.0.1:8091")
    ap.add_argument("--rounds", type=int, default=1, help="프롬프트 세트 반복 횟수")
    ap.add_argument("--out", type=str, default=None, help="결과 저장 파일 경로")
    a = ap.parse_args()

    await wait_tts(a.tts_url)
    tts = CosyVoiceTTS(url=a.tts_url)
    await tts.warmup()
    await tts_first_audio(tts, "안녕하세요.")  # TTS 워밍업

    # 프롬프트 목록 n개 구성 (부족하면 반복)
    prompts = [PROMPTS[i % len(PROMPTS)] for i in range(a.n)]
    output_lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text)
        output_lines.append(text)

    # (1a) 프롬프트·청커 단독 비교 모드
    if a.compare_prompt_chunker:
        first_min = a.first_min[0]
        llm_current = OpenAIStreamLLM(system_prompt=DEFAULT_SYSTEM_PROMPT)
        llm_fixed = OpenAIStreamLLM(system_prompt=PROMPT_NO_FILLER)
        await llm_current.warmup()
        await llm_fixed.warmup()

        configs = [
            ("current (--prompt current --chunker current)", llm_current, False),
            ("fixed (--prompt fixed --chunker fixed)", llm_fixed, True),
        ]
        results: dict[str, list[dict]] = {c[0]: [] for c in configs}

        emit(f"=== (1a) 프롬프트·청커 단독 비교 (first_min={first_min}, n={a.n}, ABAB 교차) ===")
        for idx, p in enumerate(prompts):
            for label, l, skip_comma in configs:
                for _try in range(3):
                    try:
                        res = await run_one(l, tts, p, first_min, skip_filler_comma=skip_comma)
                        results[label].append(res)
                        break
                    except Exception as e:
                        print(f"  재시도({e!r})")
                        await wait_tts(a.tts_url)

        for label, rs in results.items():
            emit(format_report_block(label, rs, show_gap=False))

        # (1a) 합격 판정
        rs_cur = results["current (--prompt current --chunker current)"]
        rs_fix = results["fixed (--prompt fixed --chunker fixed)"]
        filler_fix = sum(1 for r in rs_fix if r["is_filler"])
        p50_chunk_cur = percentile([r["chunk"] for r in rs_cur], 50)
        p50_chunk_fix = percentile([r["chunk"] for r in rs_fix], 50)
        chunk_diff = p50_chunk_fix - p50_chunk_cur

        pass_filler = filler_fix == 0
        pass_chunk = chunk_diff <= 100.0
        passed_1a = pass_filler and pass_chunk

        emit("\n=== (1a) 합격 판정 ===")
        emit(f"  1) 군더더기 단독 첫 청크: fixed={filler_fix}/{len(rs_fix)} (기준: 0/20) -> {'✅ 합격' if pass_filler else '❌ 미달'}")
        emit(f"  2) 첫 청크 시각 P50: current={p50_chunk_cur:.0f}ms, fixed={p50_chunk_fix:.0f}ms (차이 {chunk_diff:+.0f}ms, 기준 <= +100ms) -> {'✅ 합격' if pass_chunk else '❌ 미달'}")
        emit(f"  => (1a) 최종 판정: {'✅ 전체 합격' if passed_1a else '❌ 미달'}")

        await llm_current.close()
        await llm_fixed.close()

    # (1b) 양보 비교 모드
    elif a.compare_yield:
        first_min = a.first_min[0]
        # (1a) 통과 후 기본 또는 명시 설정: 미지정 시 (1a) 합격 상태이므로 fixed 기본
        prompt_mode = a.prompt or "fixed"
        chunker_mode = a.chunker or "fixed"
        system_prompt = DEFAULT_SYSTEM_PROMPT if prompt_mode == "current" else PROMPT_NO_FILLER
        skip_comma = chunker_mode == "fixed"

        llm = OpenAIStreamLLM(system_prompt=system_prompt)
        await llm.warmup()

        modes = ["idle", "concurrent", "after_chunk1_tts", "after_first_frame"]
        results: dict[str, list[dict]] = {m: [] for m in modes}

        emit(f"=== (1b) 1단계 양보 교차 비교 (first_min={first_min}, n={a.n}, prompt={prompt_mode}, chunker={chunker_mode}) ===")
        for idx, p in enumerate(prompts):
            for m in modes:
                for _try in range(3):
                    try:
                        res = await run_yield_turn(m, llm, tts, p, first_min, skip_comma)
                        results[m].append(res)
                        break
                    except Exception as e:
                        print(f"  재시도({m}, {e!r})")
                        await wait_tts(a.tts_url)

        labels = {
            "idle": "idle (LLM 유휴)",
            "concurrent": "concurrent (LLM 동시)",
            "after_chunk1_tts": "after_chunk1_tts (양보: 1청크 TTS 완료 후 2차 요청)",
            "after_first_frame": "after_first_frame (양보: 1청크 첫 프레임 시각 2차 요청)",
        }
        for m in modes:
            emit(format_report_block(labels[m], results[m], show_gap=True))

        # (1b) 합격 판정
        tts_con = percentile([r["tts"] for r in results["concurrent"]], 50)
        audio_con = percentile([r["audio"] for r in results["concurrent"]], 50)

        emit("\n=== (1b) 합격 판정 ===")
        emit(f"  [동시 기준선] TTS 첫 소리 P50: {tts_con:.0f}ms, 합계(첫 청크+TTS) P50: {audio_con:.0f}ms")

        for m in ("after_chunk1_tts", "after_first_frame"):
            rs_m = results[m]
            tts_m = percentile([r["tts"] for r in rs_m], 50)
            srv_list = [r["server_tts"] for r in rs_m if r.get("server_tts") is not None]
            srv_p50_str = f", 서버 로그 P50: {percentile(srv_list, 50):.0f}ms" if srv_list else ""
            audio_m = percentile([r["audio"] for r in rs_m], 50)
            reduction = audio_con - audio_m
            pos_gaps = sum(1 for r in rs_m if r["gap_ms"] > 0.0)

            pass_tts = tts_m <= 600.0
            pass_abs = audio_m <= 1350.0
            pass_red = reduction >= 300.0
            pass_gap = pos_gaps == 0
            all_pass = pass_tts and pass_abs and pass_red and pass_gap

            emit(f"\n  [{labels[m]}]")
            emit(f"    - TTS 첫 소리 P50: {tts_m:.0f}ms{srv_p50_str} (기준 <= 600ms) -> {'✅ 만족' if pass_tts else '❌ 미달'}")
            emit(f"    - 합계 P50 절대값: {audio_m:.0f}ms (기준 <= 1350ms) -> {'✅ 만족' if pass_abs else '❌ 미달'}")
            emit(f"    - 합계 P50 단축 폭: {reduction:+.0f}ms (동시 {audio_con:.0f}ms -> {audio_m:.0f}ms, 기준 >= 300ms) -> {'✅ 만족' if pass_red else '❌ 미달'}")
            emit(f"    - gap_ms > 0 턴 수: {pos_gaps}/{len(rs_m)} (기준 0/20) -> {'✅ 만족' if pass_gap else '❌ 미달'}")
            emit(f"    => 최종 판정: {'✅ 합격' if all_pass else '❌ 미달'}")

        await llm.close()

    # 기존 실행 모드
    else:
        prompt_mode = a.prompt or "current"
        chunker_mode = a.chunker or "current"
        sys_prompt = OLD_PROMPT if a.old_prompt else (PROMPT_NO_FILLER if prompt_mode == "fixed" else DEFAULT_SYSTEM_PROMPT)
        skip_comma = chunker_mode == "fixed"
        llm = OpenAIStreamLLM(system_prompt=sys_prompt)
        llm_old = OpenAIStreamLLM(system_prompt=OLD_PROMPT) if a.with_old else None
        await llm.warmup()
        if llm_old:
            await llm_old.warmup()

        configs = [(f"{'old' if a.old_prompt else prompt_mode} first_min={n}", llm, n, skip_comma) for n in a.first_min]
        if llm_old:
            configs.append(("old first_min=8", llm_old, 8, False))

        results: dict[str, list[dict]] = {c[0]: [] for c in configs}
        for _ in range(a.rounds):
            for p in prompts:
                for label, l, n, sc in configs:
                    for _try in range(3):
                        try:
                            results[label].append(await run_one(l, tts, p, n, skip_filler_comma=sc))
                            break
                        except Exception as e:
                            print(f"  재시도({e!r})")
                            await wait_tts(a.tts_url)

        for label, rs in results.items():
            emit(format_report_block(label, rs, show_gap=False))

        await llm.close()
        if llm_old:
            await llm_old.close()

    await tts.aclose()

    # 결과 저장
    out_path: Path | None = None
    if a.out:
        out_path = Path(a.out)
    elif a.compare_prompt_chunker:
        out_path = Path(__file__).parent / "results" / "next_01_1a_prompt_chunker.txt"
    elif a.compare_yield:
        out_path = Path(__file__).parent / "results" / "next_01_1b_yield_v2.txt"

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
        print(f"\n[저장 완료] 실측 결과가 {out_path} 에 저장되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
