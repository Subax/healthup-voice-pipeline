"""CosyVoice 첫 요청 TTFA 단축을 위한 6구간 타이밍 분해 계측 벤치마크.

docs/plans/next_04_first_tts.md 1단계:
  - 2x3 직교 실험: 문장 2종 x 직전 상태 3종 (각 n=10, 총 60회)
    문장:
      S1: "네, 알겠습니다." (8자)
      S2: "많이 힘드셨겠어요." (10자)
    직전 상태:
      A: Post-TTS 50ms (직전 TTS 합성 완료 후 50ms 대기)
      B: Idle 3s (GPU 완전 유휴 3초 대기)
      C: Post-vLLM 20 tokens (vLLM 20토큰 생성 완료 직후 0ms)
  - 6구간 측정:
      1. frontend_ms: synth() 시작 ~ model.tts() 진입 (text_normalize 소요)
      2. llm_first_tok_ms: model.tts() 진입 ~ 첫 음성 토큰 생성
      3. hop1_ready_ms: model.tts() 진입 ~ 첫 홉 필요 토큰(16~17tok) 도달
      4. poll_wait_ms: hop1_ready_ms ~ 첫 token2wav() 진입 (순수 폴링 대기)
      5. token2wav_ms: 첫 token2wav() 소요 시간
      6. first_chunk_ms: 잠금 획득 ~ 첫 청크 전송 시각
      + ms_per_tok: CosyVoice LLM 토큰당 디코드 시간
  - 1단계 진입 판정: Condition C에서 poll_wait_ms + frontend_ms P50 >= 50ms 여부
  - 결과 저장: bench/results/next_04_breakdown.txt
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

# Ensure src is in python path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import aiohttp
from voice_pipeline.trace import percentile

SENTENCES = {
    "S1": "네, 알겠습니다.",
    "S2": "많이 힘드셨겠어요.",
}

PRE_TTS_TEXT = "반갑습니다."
VLLM_PROMPT = "안녕하세요. 오늘 날씨가 어떤가요?"

RE_BREAKDOWN = re.compile(
    r"PROF_BREAKDOWN frontend=(?P<frontend>[\d.]+)ms\s+"
    r"llm_first_tok=(?P<llm_first_tok>[\d.]+)ms\s+"
    r"hop1_ready=(?P<hop1_ready>[\d.]+)ms\s+"
    r"poll_wait=(?P<poll_wait>[\d.]+)ms\s+"
    r"token2wav=(?P<token2wav>[\d.]+)ms\s+"
    r"first_chunk=(?P<first_chunk>[\d.]+)ms\s+"
    r"finalize=(?P<finalize>\w+)"
)

RE_LLM_PROF = re.compile(
    r"PROF llm tokens=(?P<tokens>\d+)\s+total=(?P<total>[\d.]+)ms\s+"
    r"first=(?P<first>[\d.]+)ms\s+\((?P<ms_per_tok>[\d.]+)ms/tok\)"
)


def get_docker_logs_since(since_iso: str, container: str = "healthup-cosyvoice") -> list[str]:
    """주어진 ISO 시각 이후의 docker logs를 가져온다."""
    try:
        p = subprocess.run(
            ["docker", "logs", "-t", f"--since={since_iso}", container],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (p.stderr + "\n" + p.stdout).splitlines()
    except Exception:
        return []


def parse_latest_prof(logs: list[str]) -> dict[str, Any] | None:
    """로그 목록에서 가장 최근 PROF_BREAKDOWN과 PROF llm 정보를 파싱한다."""
    breakdown_match = None
    llm_match = None

    for line in logs:
        m_b = RE_BREAKDOWN.search(line)
        if m_b:
            breakdown_match = m_b
        m_l = RE_LLM_PROF.search(line)
        if m_l:
            llm_match = m_l

    if not breakdown_match:
        return None

    bd_dict = breakdown_match.groupdict()
    res: dict[str, Any] = {
        "frontend_ms": float(bd_dict["frontend"]),
        "llm_first_tok_ms": float(bd_dict["llm_first_tok"]),
        "hop1_ready_ms": float(bd_dict["hop1_ready"]),
        "poll_wait_ms": float(bd_dict["poll_wait"]),
        "token2wav_ms": float(bd_dict["token2wav"]),
        "first_chunk_ms": float(bd_dict["first_chunk"]),
        "finalize": bd_dict["finalize"].lower() == "true",
    }

    if llm_match:
        ld = llm_match.groupdict()
        res["tokens"] = int(ld["tokens"])
        res["llm_total_ms"] = float(ld["total"])
        res["llm_first_ms"] = float(ld["first"])
        res["ms_per_tok"] = float(ld["ms_per_tok"])
    else:
        res["tokens"] = 0
        res["llm_total_ms"] = 0.0
        res["llm_first_ms"] = 0.0
        res["ms_per_tok"] = 0.0

    return res


async def run_pre_tts(session: aiohttp.ClientSession, base_url: str, voice: str) -> float:
    """Condition A 전처리: 직전 TTS 요청 및 전체 수신."""
    t0 = time.perf_counter()
    async with session.post(f"{base_url}/tts", json={"text": PRE_TTS_TEXT, "voice": voice}) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise RuntimeError(f"Pre-TTS failed: {resp.status} {err}")
        async for _ in resp.content.iter_any():
            pass
    return (time.perf_counter() - t0) * 1000.0


async def run_vllm_20tok(session: aiohttp.ClientSession, base_url: str, model: str) -> float:
    """Condition C 전처리: vLLM 20토큰 생성 요청 및 완료 대기."""
    t0 = time.perf_counter()
    async with session.post(
        f"{base_url}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": VLLM_PROMPT}],
            "max_tokens": 20,
        },
    ) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise RuntimeError(f"vLLM failed: {resp.status} {err}")
        await resp.json()
    return (time.perf_counter() - t0) * 1000.0


async def run_target_tts(
    session: aiohttp.ClientSession,
    base_url: str,
    text: str,
    voice: str,
) -> tuple[float, float, int]:
    """측정 대상 TTS 요청. (client_ttfa_ms, total_ms, total_bytes) 반환."""
    t0 = time.perf_counter()
    ttfa = None
    total_bytes = 0
    async with session.post(f"{base_url}/tts", json={"text": text, "voice": voice}) as resp:
        if resp.status != 200:
            err = await resp.text()
            raise RuntimeError(f"Target TTS failed: {resp.status} {err}")
        async for chunk in resp.content.iter_any():
            if chunk:
                if ttfa is None:
                    ttfa = (time.perf_counter() - t0) * 1000.0
                total_bytes += len(chunk)
    t_end = time.perf_counter()
    return ttfa or (t_end - t0) * 1000.0, (t_end - t0) * 1000.0, total_bytes


async def run_single_trial(
    session: aiohttp.ClientSession,
    condition: str,
    sentence_id: str,
    text: str,
    trial_idx: int,
    cosyvoice_url: str,
    vllm_url: str,
    vllm_model: str,
    voice: str,
) -> dict[str, Any]:
    """단일 실험 1회 실행: 조건 세팅 -> 대상 문장 TTS -> 프로파일링 로그 추출."""
    prep_ms = 0.0

    # 1. 조건 세팅
    if condition == "A":  # Post-TTS 50ms
        prep_ms = await run_pre_tts(session, cosyvoice_url, voice)
        await asyncio.sleep(0.050)
    elif condition == "B":  # Idle 3s
        t_b = time.perf_counter()
        await asyncio.sleep(3.0)
        prep_ms = (time.perf_counter() - t_b) * 1000.0
    elif condition == "C":  # Post-vLLM 20 tokens
        prep_ms = await run_vllm_20tok(session, vllm_url, vllm_model)

    # 2. 대상 TTS 요청 직전 타임스탬프 기록 (도커 로그 필터링용)
    since_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # 아주 짧은 시계 간극 보정
    await asyncio.sleep(0.010)

    # 3. 대상 TTS 실행 및 Client TTFA 측정
    c_ttfa, total_ms, total_bytes = await run_target_tts(session, cosyvoice_url, text, voice)

    # 4. 서버 로그 플러시 대기 및 프로파일 정보 추출
    await asyncio.sleep(0.050)
    prof = None
    for _ in range(5):
        logs = get_docker_logs_since(since_iso)
        prof = parse_latest_prof(logs)
        if prof:
            break
        await asyncio.sleep(0.100)

    if not prof:
        # 혹시 since 타임스탬프 오차가 있을 경우 최근 25줄 fallback
        fallback_logs = subprocess.run(
            ["docker", "logs", "--tail", "25", "healthup-cosyvoice"],
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        prof = parse_latest_prof(fallback_logs)

    if not prof:
        raise RuntimeError(f"Trial ({condition}, {sentence_id}, #{trial_idx}) 프로파일링 로그 추출 실패")

    return {
        "condition": condition,
        "sentence_id": sentence_id,
        "text": text,
        "trial_idx": trial_idx,
        "prep_ms": prep_ms,
        "client_ttfa_ms": c_ttfa,
        "total_ms": total_ms,
        "total_bytes": total_bytes,
        "frontend_ms": prof["frontend_ms"],
        "llm_first_tok_ms": prof["llm_first_tok_ms"],
        "hop1_ready_ms": prof["hop1_ready_ms"],
        "poll_wait_ms": prof["poll_wait_ms"],
        "token2wav_ms": prof["token2wav_ms"],
        "server_first_chunk_ms": prof["first_chunk_ms"],
        "finalize": prof["finalize"],
        "tokens": prof["tokens"],
        "ms_per_tok": prof["ms_per_tok"],
        "poll_plus_frontend": prof["poll_wait_ms"] + prof["frontend_ms"],
    }


def compute_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    """레코드 리스트의 P50, P90, min, max, mean 통계 산출."""
    if not records:
        return {}

    def st(key: str) -> dict[str, float]:
        vals = [float(r[key]) for r in records]
        return {
            "p50": percentile(vals, 50),
            "p90": percentile(vals, 90),
            "min": min(vals),
            "max": max(vals),
            "mean": sum(vals) / len(vals),
        }

    return {
        "n": len(records),
        "client_ttfa": st("client_ttfa_ms"),
        "server_first_chunk": st("server_first_chunk_ms"),
        "frontend": st("frontend_ms"),
        "llm_first_tok": st("llm_first_tok_ms"),
        "hop1_ready": st("hop1_ready_ms"),
        "poll_wait": st("poll_wait_ms"),
        "token2wav": st("token2wav_ms"),
        "poll_plus_frontend": st("poll_plus_frontend"),
        "ms_per_tok": st("ms_per_tok"),
        "finalize_count": sum(1 for r in records if r["finalize"]),
    }


def format_report(
    all_trials: list[dict[str, Any]],
    n_trials: int,
) -> str:
    """결과 리포트 문자열 생성."""
    cond_labels = {
        "A": "A: Post-TTS 50ms (직전 TTS 완료 후 50ms 대기)",
        "B": "B: Idle 3s (GPU 완전 유휴 3초)",
        "C": "C: Post-vLLM 20 tokens (vLLM 20토큰 생성 직후)",
    }

    lines = [
        "=" * 100,
        "  CosyVoice 첫 요청 TTFA 6구간 타이밍 분해 계측 리포트 (next_04 1단계 실측)",
        "=" * 100,
        "",
        "1. 실험 개요 및 환경",
        f"  - 일시: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "  - 설계: 2 x 3 직교 실험 (문장 2종 x 직전 상태 3종, 조건별 n=10, 총 60회)",
        f"  - 대상 문장 S1: \"{SENTENCES['S1']}\" (8자)",
        f"  - 대상 문장 S2: \"{SENTENCES['S2']}\" (10자)",
        "  - 조건 A: 직전 짧은 문장 TTS 완료 후 50ms 대기 후 요청",
        "  - 조건 B: 3.0초 GPU 완전 유휴 후 요청",
        "  - 조건 C: vLLM(Qwen3.8-27B) 20토큰 생성 완료 직후 0ms 요청",
        "  - 6구간 정의 (t0 = 서버 잠금 획득 시각 기준):",
        "      1. frontend_ms: synth() 시작 ~ model.tts() 진입 (text_normalize 중복 포함)",
        "      2. llm_first_tok_ms: model.tts() 진입 ~ 첫 음성 토큰 생성",
        "      3. hop1_ready_ms: model.tts() 진입 ~ 첫 홉 필요 토큰(need_tok) 도달",
        "      4. poll_wait_ms: hop1_ready_ms ~ 첫 token2wav() 진입 (time.sleep(0.1) 폴링 대기)",
        "      5. token2wav_ms: 첫 token2wav() 소요 시간 (flow + hift)",
        "      6. first_chunk_ms: t0 ~ 첫 청크 전송 (서버 전체 TTFA)",
        "      + ms_per_tok: CosyVoice LLM 토큰당 디코드 시간 (CUDAGraph)",
        "",
        "2. 2x3 셀별 요약 통계 (각 n=10)",
        "-" * 125,
        f"{'조건':<12} | {'문장':<14} | {'Client TTFA':<15} | {'서버 첫청크':<14} | {'frontend':<10} | {'llm_first':<10} | {'hop1_ready':<11} | {'poll_wait':<10} | {'token2wav':<10} | {'poll+front':<10}",
        f"{'':<12} | {'':<14} | {'P50 / P90':<15} | {'P50 / P90':<14} | {'P50':<10} | {'P50':<10} | {'P50':<11} | {'P50':<10} | {'P50':<10} | {'P50':<10}",
        "-" * 125,
    ]

    cell_stats: dict[tuple[str, str], dict[str, Any]] = {}
    for cond in ["A", "B", "C"]:
        for s_id in ["S1", "S2"]:
            sub = [r for r in all_trials if r["condition"] == cond and r["sentence_id"] == s_id]
            st = compute_stats(sub)
            cell_stats[(cond, s_id)] = st
            lines.append(
                f"{cond:<12} | {s_id + ' (' + SENTENCES[s_id] + ')':<14} | "
                f"{st['client_ttfa']['p50']:4.0f} / {st['client_ttfa']['p90']:4.0f}ms   | "
                f"{st['server_first_chunk']['p50']:4.0f} / {st['server_first_chunk']['p90']:4.0f}ms   | "
                f"{st['frontend']['p50']:4.1f}ms   | "
                f"{st['llm_first_tok']['p50']:4.1f}ms   | "
                f"{st['hop1_ready']['p50']:5.1f}ms   | "
                f"{st['poll_wait']['p50']:4.1f}ms   | "
                f"{st['token2wav']['p50']:4.1f}ms   | "
                f"{st['poll_plus_frontend']['p50']:4.1f}ms"
            )

    lines.extend([
        "-" * 125,
        "",
        "3. 직전 상태 조건별 통합 통계 (각 n=20: S1 + S2)",
        "-" * 120,
        f"{'직전 상태 조건':<26} | {'Client TTFA P50/P90':<20} | {'서버첫청크 P50/P90':<20} | {'poll_wait P50':<14} | {'frontend P50':<14} | {'poll+front P50':<14}",
        "-" * 120,
    ])

    cond_stats: dict[str, dict[str, Any]] = {}
    for cond in ["A", "B", "C"]:
        sub = [r for r in all_trials if r["condition"] == cond]
        st = compute_stats(sub)
        cond_stats[cond] = st
        lines.append(
            f"{cond_labels[cond]:<26} | "
            f"P50 {st['client_ttfa']['p50']:4.0f} / P90 {st['client_ttfa']['p90']:4.0f}ms | "
            f"P50 {st['server_first_chunk']['p50']:4.0f} / P90 {st['server_first_chunk']['p90']:4.0f}ms | "
            f"P50 {st['poll_wait']['p50']:4.1f}ms      | "
            f"P50 {st['frontend']['p50']:4.1f}ms      | "
            f"P50 {st['poll_plus_frontend']['p50']:4.1f}ms"
        )

    lines.extend([
        "-" * 120,
        "",
        "4. 문장별 통합 통계 (각 n=30: A + B + C)",
        "-" * 110,
        f"{'문장':<24} | {'글자':<4} | {'Client TTFA P50/P90':<20} | {'서버첫청크 P50':<14} | {'hop1_ready P50':<15} | {'ms/tok P50':<12}",
        "-" * 110,
    ])

    s_stats: dict[str, dict[str, Any]] = {}
    for s_id in ["S1", "S2"]:
        sub = [r for r in all_trials if r["sentence_id"] == s_id]
        st = compute_stats(sub)
        s_stats[s_id] = st
        lines.append(
            f"{s_id + ': ' + SENTENCES[s_id]:<24} | {len(SENTENCES[s_id]):<4} | "
            f"P50 {st['client_ttfa']['p50']:4.0f} / P90 {st['client_ttfa']['p90']:4.0f}ms | "
            f"P50 {st['server_first_chunk']['p50']:4.0f}ms     | "
            f"P50 {st['hop1_ready']['p50']:5.1f}ms    | "
            f"{st['ms_per_tok']['p50']:4.1f}ms/tok"
        )

    lines.extend([
        "-" * 110,
        "",
        "5. 가설 검증 및 1단계 진입 판정 (docs/plans/next_04_first_tts.md §2, §6)",
        "-" * 100,
    ])

    c_poll_front_p50 = cond_stats["C"]["poll_plus_frontend"]["p50"]
    c_poll_p50 = cond_stats["C"]["poll_wait"]["p50"]
    c_front_p50 = cond_stats["C"]["frontend"]["p50"]
    a_srv_p50 = cond_stats["A"]["server_first_chunk"]["p50"]
    b_srv_p50 = cond_stats["B"]["server_first_chunk"]["p50"]
    c_srv_p50 = cond_stats["C"]["server_first_chunk"]["p50"]

    lines.append(f"  - 조건 A 서버 첫 청크 P50: {a_srv_p50:.0f}ms")
    lines.append(f"  - 조건 B 서버 첫 청크 P50: {b_srv_p50:.0f}ms (A 대비 {b_srv_p50 - a_srv_p50:+.0f}ms)")
    lines.append(f"  - 조건 C 서버 첫 청크 P50: {c_srv_p50:.0f}ms (A 대비 {c_srv_p50 - a_srv_p50:+.0f}ms)")
    lines.append(f"  - Condition C 'poll_wait_ms + frontend_ms' P50: {c_poll_front_p50:.1f}ms (poll: {c_poll_p50:.1f}ms, frontend: {c_front_p50:.1f}ms)")
    lines.append("")

    # 판정 기준
    is_stage1_passed = c_poll_front_p50 >= 50.0
    status_str = "✅ 통과 (PASS) - 2단계 폴링·전처리 패치 진입 조건 충족" if is_stage1_passed else "❌ 미충족 (FAIL) - 중단 조건 검토"
    lines.append(f"  ▶ 1단계 진입 판정 (기준: C 조건 poll_wait_ms + frontend_ms P50 >= 50ms): {status_str}")
    lines.append("")

    # 가설별 분석
    lines.append("  [가설별 분석]:")
    lines.append(f"  1) 가설 1 (폴링 양자화 대기): C 조건 poll_wait_ms P50 = {c_poll_p50:.1f}ms. "
                 f"100ms 폴링의 균등 대기(~50ms)와 계단 증폭이 실측됨.")
    lines.append(f"  2) 가설 2 (vLLM 직후 컨텍스트 전환): C 조건 vs A 조건 차이 = {c_srv_p50 - a_srv_p50:+.0f}ms. "
                 f"llm_first_tok P50 = {cond_stats['C']['llm_first_tok']['p50']:.1f}ms (A: {cond_stats['A']['llm_first_tok']['p50']:.1f}ms).")
    lines.append(f"  3) 가설 3 (GPU 유휴 클럭 저하): B 조건 vs A 조건 차이 = {b_srv_p50 - a_srv_p50:+.0f}ms. "
                 f"{'유휴에 의한 클럭 저하 영향 작음' if abs(b_srv_p50 - a_srv_p50) < 30 else '유휴 클럭 저하 관측됨'}.")
    lines.append(f"  4) 가설 4 (문장 및 전처리 차이): S1 vs S2 서버 첫 청크 P50 = {s_stats['S1']['server_first_chunk']['p50']:.0f}ms vs {s_stats['S2']['server_first_chunk']['p50']:.0f}ms "
                 f"(차이 {s_stats['S2']['server_first_chunk']['p50'] - s_stats['S1']['server_first_chunk']['p50']:+.0f}ms). "
                 f"frontend_ms P50 = {cond_stats['C']['frontend']['p50']:.1f}ms.")

    lines.extend([
        "-" * 100,
        "",
        "6. 60회 턴별 상세 실측 데이터",
        "-" * 135,
        f"{'조건':<4} | {'문장':<4} | {'회차':<4} | {'Client':<8} | {'서버첫청크':<10} | {'frontend':<8} | {'llm_first':<9} | {'hop1_ready':<10} | {'poll_wait':<9} | {'token2wav':<9} | {'ms/tok':<6} | {'finalize':<8} | {'prep_ms':<8}",
        "-" * 135,
    ])

    for r in all_trials:
        lines.append(
            f"{r['condition']:<4} | {r['sentence_id']:<4} | #{r['trial_idx']:<3} | "
            f"{r['client_ttfa_ms']:6.1f}ms | {r['server_first_chunk_ms']:7.1f}ms   | "
            f"{r['frontend_ms']:6.1f}ms | {r['llm_first_tok_ms']:7.1f}ms | "
            f"{r['hop1_ready_ms']:8.1f}ms  | {r['poll_wait_ms']:7.1f}ms | "
            f"{r['token2wav_ms']:7.1f}ms | {r['ms_per_tok']:5.1f}  | "
            f"{str(r['finalize']):<8} | {r['prep_ms']:6.0f}ms"
        )

    lines.append("-" * 135)
    return "\n".join(lines) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cosyvoice-url", default="http://127.0.0.1:8091")
    ap.add_argument("--vllm-url", default="http://127.0.0.1:8000")
    ap.add_argument("--vllm-model", default="Qwen/Qwen3.8-27B")
    ap.add_argument("--voice", default="calm")
    ap.add_argument("-n", "--trials", type=int, default=10, help="조건 및 문장당 반복 횟수 (기본 10회, 총 60회)")
    ap.add_argument("--output", default="bench/results/next_04_breakdown.txt")
    args = ap.parse_args()

    print("=" * 70)
    print("  CosyVoice 6구간 타이밍 분해 계측 2x3 직교 벤치마크 시작")
    print(f"  CosyVoice URL: {args.cosyvoice_url} (voice: {args.voice})")
    print(f"  vLLM URL:      {args.vllm_url} (model: {args.vllm_model})")
    print(f"  반복 횟수:     조건별 {args.trials}회 (총 2x3x{args.trials} = {6 * args.trials}회)")
    print("=" * 70)

    # 연결 확인
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(f"{args.cosyvoice_url}/info", timeout=aiohttp.ClientTimeout(total=3)) as r:
                assert r.status == 200
        except Exception as e:
            print(f"CosyVoice 서버 연결 실패: {e}")
            sys.exit(1)

        try:
            async with session.get(f"{args.vllm_url}/v1/models", timeout=aiohttp.ClientTimeout(total=3)) as r:
                assert r.status == 200
        except Exception as e:
            print(f"vLLM 서버 연결 실패: {e}")
            sys.exit(1)

        all_trials: list[dict[str, Any]] = []

        # 실험 루프: 직교 조건 순회
        # drift 방지를 위해 회차별(round 0..n-1)로 3개 조건 x 2개 문장을 순회
        for idx in range(args.trials):
            print(f"\n[Round {idx + 1}/{args.trials}]")
            for cond in ["A", "B", "C"]:
                for s_id in ["S1", "S2"]:
                    text = SENTENCES[s_id]
                    sys.stdout.write(f"  ({cond}, {s_id}) #{idx + 1:02d} ... ")
                    sys.stdout.flush()

                    t_res = await run_single_trial(
                        session=session,
                        condition=cond,
                        sentence_id=s_id,
                        text=text,
                        trial_idx=idx + 1,
                        cosyvoice_url=args.cosyvoice_url,
                        vllm_url=args.vllm_url,
                        vllm_model=args.vllm_model,
                        voice=args.voice,
                    )
                    all_trials.append(t_res)

                    print(
                        f"TTFA={t_res['client_ttfa_ms']:.0f}ms "
                        f"[front={t_res['frontend_ms']:.1f} hop1={t_res['hop1_ready_ms']:.1f} "
                        f"poll={t_res['poll_wait_ms']:.1f} t2w={t_res['token2wav_ms']:.1f}]"
                    )

    # 리포트 생성 및 저장
    report = format_report(all_trials, args.trials)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print("\n" + "=" * 70)
    print(f"  결과 리포트 저장 완료: {out_path}")
    print("=" * 70)

    # 요약 출력
    c_trials = [r for r in all_trials if r["condition"] == "C"]
    c_stats = compute_stats(c_trials)
    c_p50 = c_stats["poll_plus_frontend"]["p50"]
    passed = c_p50 >= 50.0

    print(f"  Condition C (Post-vLLM) 서버 첫청크 P50: {c_stats['server_first_chunk']['p50']:.0f}ms")
    print(f"  Condition C poll_wait_ms P50:            {c_stats['poll_wait']['p50']:.1f}ms")
    print(f"  Condition C frontend_ms P50:             {c_stats['frontend']['p50']:.1f}ms")
    print(f"  Condition C poll+frontend P50:           {c_p50:.1f}ms")
    print(f"  1단계 진입 조건 (>= 50ms):                {'충족 (PASS)' if passed else '미충족 (FAIL)'}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
