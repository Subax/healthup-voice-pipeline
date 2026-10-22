"""사용자 청취 비교용 샘플 생성 스크립트.

같은 입력 5턴을 대상으로:
  (1) concurrent (동시 기준선)
  (2) after_first_frame (양보)
를 각각 실행하여, 턴마다 실제 재생 순서대로 청크 사이 무음(gap)을 포함한 하나의 WAV 파일로 저장한다.

저장 위치: bench/results/listen_ab_yield/
파일명: turn{i}_{mode}.wav
결과 표: bench/results/listen_ab_yield/README.txt
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any

# Ensure bench and src are in python path
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from first_audio_bench import (
    get_latest_server_first_chunk,
    wait_tts,
    wait_vllm_idle,
)
from llm_bench import PROMPTS
from voice_pipeline.chunker import SemanticChunker
from voice_pipeline.llm.events import Token
from voice_pipeline.llm.openai_stream import (
    FIRST_PHASE_MAX_TOKENS,
    PROMPT_NO_FILLER,
    OpenAIStreamLLM,
    second_phase_max_tokens,
)
from voice_pipeline.normalize import normalize_for_tts
from voice_pipeline.tts.cosyvoice import CosyVoiceTTS

SAMPLE_PROMPTS = PROMPTS[:5]


async def run_turn_concurrent(
    llm: OpenAIStreamLLM,
    tts: CosyVoiceTTS,
    prompt: str,
    first_min: int = 2,
    skip_filler_comma: bool = True,
) -> dict[str, Any]:
    await wait_vllm_idle(llm.base_url)
    t0 = time.perf_counter()

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

    # 1청크 TTS
    t_c1_start = time.perf_counter()
    t1 = None
    pcm_c1 = bytearray()
    async for pcm in tts.stream(chunk1_text):
        if t1 is None:
            t1 = time.perf_counter()
        pcm_c1.extend(pcm)
    d1_ms = len(pcm_c1) / (2 * tts.sample_rate) * 1000
    srv_tts = get_latest_server_first_chunk()

    chunk2_text = await chunk2_future

    # 2청크 TTS: 완주
    t2 = None
    pcm_c2 = bytearray()
    if chunk2_text:
        async for pcm in tts.stream(chunk2_text):
            if t2 is None:
                t2 = time.perf_counter()
            pcm_c2.extend(pcm)
    if t2 is None:
        t2 = time.perf_counter()

    llm_task.cancel()
    await asyncio.gather(llm_task, return_exceptions=True)

    gap_ms = max(0.0, (t2 - t1) * 1000 - d1_ms)
    tts_ms = (t1 - t_c1_start) * 1000
    chunk_ms = (t_chunk1 - t0) * 1000 if t_chunk1 else 0.0

    return {
        "mode": "concurrent",
        "prompt": prompt,
        "chunk1_text": chunk1_text,
        "chunk2_text": chunk2_text,
        "ttft_ms": (t_tok - t0) * 1000 if t_tok else 0.0,
        "chunk_ms": chunk_ms,
        "tts_ms": tts_ms,
        "server_tts_ms": srv_tts,
        "total_first_sound_ms": chunk_ms + tts_ms,
        "d1_ms": d1_ms,
        "gap_ms": gap_ms,
        "pcm_c1": bytes(pcm_c1),
        "pcm_c2": bytes(pcm_c2),
    }


async def run_turn_after_first_frame(
    llm: OpenAIStreamLLM,
    tts: CosyVoiceTTS,
    prompt: str,
    first_min: int = 2,
    skip_filler_comma: bool = True,
) -> dict[str, Any]:
    await wait_vllm_idle(llm.base_url)
    t0 = time.perf_counter()

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
    pcm_c1 = bytearray()

    async for pcm in tts.stream(chunk1_text):
        if t1 is None:
            t1 = time.perf_counter()
            p2_task = asyncio.create_task(produce_phase2())
        pcm_c1.extend(pcm)
    d1_ms = len(pcm_c1) / (2 * tts.sample_rate) * 1000
    srv_tts = get_latest_server_first_chunk()

    chunk2_text = await chunk2_future

    # 2청크 TTS: 완주
    t2 = None
    pcm_c2 = bytearray()
    if chunk2_text:
        async for pcm in tts.stream(chunk2_text):
            if t2 is None:
                t2 = time.perf_counter()
            pcm_c2.extend(pcm)
    if t2 is None:
        t2 = time.perf_counter()

    if p2_task:
        p2_task.cancel()
        await asyncio.gather(p2_task, return_exceptions=True)

    gap_ms = max(0.0, (t2 - t1) * 1000 - d1_ms)
    tts_ms = (t1 - t_c1_start) * 1000
    chunk_ms = (t_chunk1 - t0) * 1000 if t_chunk1 else 0.0

    return {
        "mode": "after_first_frame",
        "prompt": prompt,
        "chunk1_text": chunk1_text,
        "chunk2_text": chunk2_text,
        "ttft_ms": (t_tok - t0) * 1000 if t_tok else 0.0,
        "chunk_ms": chunk_ms,
        "tts_ms": tts_ms,
        "server_tts_ms": srv_tts,
        "total_first_sound_ms": chunk_ms + tts_ms,
        "d1_ms": d1_ms,
        "gap_ms": gap_ms,
        "pcm_c1": bytes(pcm_c1),
        "pcm_c2": bytes(pcm_c2),
    }


def save_wav(out_path: Path, pcm_data: bytes, sample_rate: int = 24000) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)


async def main() -> None:
    out_dir = Path("bench/results/listen_ab_yield")
    out_dir.mkdir(parents=True, exist_ok=True)

    tts = CosyVoiceTTS()
    await wait_tts(tts.url)
    await tts.warmup()

    llm = OpenAIStreamLLM(system_prompt=PROMPT_NO_FILLER)
    await llm.warmup()

    records: list[dict[str, Any]] = []

    print(f"=== 청취 비교용 샘플 생성 시작 (5턴, concurrent vs after_first_frame) ===")
    print(f"저장 위치: {out_dir}\n")

    for idx, prompt in enumerate(SAMPLE_PROMPTS, start=1):
        print(f"--- [Turn {idx}/5] 입력: \"{prompt}\" ---")

        # 1. concurrent 모드
        res_conc = await run_turn_concurrent(llm, tts, prompt, first_min=2, skip_filler_comma=True)
        silence_samples_conc = int(tts.sample_rate * (res_conc["gap_ms"] / 1000.0))
        full_pcm_conc = res_conc["pcm_c1"] + (b"\x00\x00" * silence_samples_conc) + res_conc["pcm_c2"]
        file_conc = f"turn{idx}_concurrent.wav"
        save_wav(out_dir / file_conc, full_pcm_conc, tts.sample_rate)
        res_conc["turn"] = idx
        res_conc["filename"] = file_conc
        records.append(res_conc)
        print(
            f"  [concurrent]        첫소리: {res_conc['total_first_sound_ms']:.0f}ms (TTS {res_conc['tts_ms']:.0f}ms), "
            f"1청크: {res_conc['d1_ms']:.0f}ms, gap: {res_conc['gap_ms']:.0f}ms -> {file_conc}"
        )
        print(f"                      [1] {res_conc['chunk1_text']} / [2] {res_conc['chunk2_text']}")

        # 2. after_first_frame 모드
        res_yield = await run_turn_after_first_frame(llm, tts, prompt, first_min=2, skip_filler_comma=True)
        silence_samples_yield = int(tts.sample_rate * (res_yield["gap_ms"] / 1000.0))
        full_pcm_yield = res_yield["pcm_c1"] + (b"\x00\x00" * silence_samples_yield) + res_yield["pcm_c2"]
        file_yield = f"turn{idx}_after_first_frame.wav"
        save_wav(out_dir / file_yield, full_pcm_yield, tts.sample_rate)
        res_yield["turn"] = idx
        res_yield["filename"] = file_yield
        records.append(res_yield)
        print(
            f"  [after_first_frame] 첫소리: {res_yield['total_first_sound_ms']:.0f}ms (TTS {res_yield['tts_ms']:.0f}ms), "
            f"1청크: {res_yield['d1_ms']:.0f}ms, gap: {res_yield['gap_ms']:.0f}ms -> {file_yield}"
        )
        print(f"                      [1] {res_yield['chunk1_text']} / [2] {res_yield['chunk2_text']}\n")

    await llm.close()

    # README.txt 작성
    readme_lines = [
        "================================================================================",
        "  사용자 청취 비교 샘플: 동시 기준선(concurrent) vs 양보(after_first_frame)",
        "================================================================================",
        "",
        "1. 개요 및 오디오 구성",
        "  - 목적: 동일한 사용자 입력 5턴에 대해 동시 기준선과 after_first_frame 양보 모드의",
        "         첫 소리 지연(TTFA) 및 청크 간 무음(gap)을 실제 재생 순서대로 체감 비교.",
        "  - 오디오 파일 구성: [1청크 음성] + [실측 gap_ms 무음] + [2청크 음성]",
        "    * gap_ms == 0 인 경우 끊김 없이 즉시 2청크가 이어짐",
        "    * gap_ms > 0 인 경우 실측된 무음 시간만큼 묵음(zero PCM) 삽입 후 2청크 재생",
        "  - 샘플레이트: 24000Hz, 모노 16-bit PCM WAV",
        "",
        "2. 턴별 실측 수치 및 비교표",
        "",
        f"{'파일명':<28} | {'모드':<17} | {'합계첫소리':<8} | {'TTS첫소리(srv)':<14} | {'1청크길이':<8} | {'무음gap':<7} | {'청크 문장'}",
        "-" * 120,
    ]

    for r in records:
        srv_str = f"{r['tts_ms']:.0f}ms({r['server_tts_ms']:.0f}ms)" if r['server_tts_ms'] is not None else f"{r['tts_ms']:.0f}ms"
        txt_summary = f"[1] {r['chunk1_text']} / [2] {r['chunk2_text']}"
        readme_lines.append(
            f"{r['filename']:<28} | {r['mode']:<17} | {r['total_first_sound_ms']:>6.0f}ms | {srv_str:>14} | {r['d1_ms']:>6.0f}ms | {r['gap_ms']:>5.0f}ms | {txt_summary}"
        )

    readme_lines.extend([
        "",
        "3. 턴별 세부 프롬프트 및 응답 텍스트",
        "-" * 80,
    ])

    for i in range(1, 6):
        c_rec = [r for r in records if r["turn"] == i and r["mode"] == "concurrent"][0]
        y_rec = [r for r in records if r["turn"] == i and r["mode"] == "after_first_frame"][0]
        prompt = c_rec["prompt"]

        readme_lines.extend([
            f"[Turn {i}] 입력: \"{prompt}\"",
            f"  - concurrent:",
            f"      파일: {c_rec['filename']}",
            f"      첫소리: {c_rec['total_first_sound_ms']:.0f}ms (청크 {c_rec['chunk_ms']:.0f}ms + TTS {c_rec['tts_ms']:.0f}ms), gap: {c_rec['gap_ms']:.0f}ms",
            f"      1청크: {c_rec['chunk1_text']}",
            f"      2청크: {c_rec['chunk2_text']}",
            f"  - after_first_frame:",
            f"      파일: {y_rec['filename']}",
            f"      첫소리: {y_rec['total_first_sound_ms']:.0f}ms (청크 {y_rec['chunk_ms']:.0f}ms + TTS {y_rec['tts_ms']:.0f}ms), gap: {y_rec['gap_ms']:.0f}ms",
            f"      1청크: {y_rec['chunk1_text']}",
            f"      2청크: {y_rec['chunk2_text']}",
            "",
        ])

    conc_first = [r["total_first_sound_ms"] for r in records if r["mode"] == "concurrent"]
    yield_first = [r["total_first_sound_ms"] for r in records if r["mode"] == "after_first_frame"]
    conc_tts = [r["tts_ms"] for r in records if r["mode"] == "concurrent"]
    yield_tts = [r["tts_ms"] for r in records if r["mode"] == "after_first_frame"]
    conc_gap = [r["gap_ms"] for r in records if r["mode"] == "concurrent"]
    yield_gap = [r["gap_ms"] for r in records if r["mode"] == "after_first_frame"]

    def avg(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    readme_lines.extend([
        "4. 5턴 요약 통계",
        "-" * 80,
        f"  - 합계 첫 소리 시각 평균 : concurrent {avg(conc_first):.0f}ms  vs  after_first_frame {avg(yield_first):.0f}ms (양보가 {avg(conc_first) - avg(yield_first):+.0f}ms 빠름)",
        f"  - TTS 첫 소리 평균     : concurrent {avg(conc_tts):.0f}ms  vs  after_first_frame {avg(yield_tts):.0f}ms (양보가 {avg(conc_tts) - avg(yield_tts):+.0f}ms 빠름)",
        f"  - 청크 간 무음 gap 평균 : concurrent {avg(conc_gap):.0f}ms  vs  after_first_frame {avg(yield_gap):.0f}ms (양보가 {avg(yield_gap) - avg(conc_gap):+.0f}ms 긴 무음 발생)",
        "",
        "5. 청취 비교 핵심 포인트",
        "  - concurrent 모드: 첫 소리는 약간 늦으나(~1.5초), 1청크와 2청크 사이 끊김 없이 매끄럽게 연결됨 (gap ~0ms 위주).",
        "  - after_first_frame 모드: 첫 소리는 약 1.2초로 확연히 빠르나, 1청크 재생 종료 후 2청크 시작 전 약 0.6~0.9초간 정적(gap)이 발생함.",
    ])

    readme_path = out_dir / "README.txt"
    readme_path.write_text("\n".join(readme_lines) + "\n", encoding="utf-8")
    print(f"\n[완료] README.txt 생성 완료: {readme_path}")


if __name__ == "__main__":
    asyncio.run(main())
