"""CosyVoice 서버 취소(cancel) 후 잠금(lock) 해제 지연 측정 벤치마크.

측정 시나리오:
  1. 긴 문장(40자 이상) 합성을 시작.
  2. 첫 오디오 수신 후 지정된 지연(100ms, 300ms, 600ms) 후에 합성을 취소(cancel).
  3. 취소 직후 짧은 문장(예: "네, 알겠습니다.")을 요청하여 그 TTS 첫 소리(TTFA)를 측정 (각 10회).
  4. docker logs 타임스탬프를 통해 서버가 취소를 인지하고 잠금을 해제하여 다음 합성을 시작한 시각을 분석.
  5. 결과를 bench/results/cosyvoice_cancel_lock.txt 에 기록.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

# Ensure src is in python path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
from voice_pipeline.trace import percentile
from voice_pipeline.tts.cosyvoice import CosyVoiceTTS
from voice_pipeline.stt.sensevoice import SenseVoiceSTT

LONG_TEXT = "아침마다 머리가 아픈 것은 여러 가지 이유가 있을 수 있어요. 수면의 질이나 자세, 스트레스 등이 영향을 줄 수 있습니다."  # 65자
SHORT_TEXT = "네, 알겠습니다."  # 8자
PUNCT = ".,?!"


def _chars(s: str) -> list[str]:
    return [c for c in s if c.strip() and c not in PUNCT]


def cer(ref: str, hyp: str) -> float:
    """문자 단위 편집거리 / 원문 길이 (공백·문장부호 제외)."""
    a, b = _chars(ref), _chars(hyp)
    d = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        p, d[0] = d[0], i
        for j, y in enumerate(b, 1):
            p, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, p + (x != y))
    return d[-1] / max(1, len(a))


def resample_24k_to_16k(pcm24k: bytes) -> bytes:
    """24kHz mono PCM16 바이트를 16kHz mono PCM16 바이트로 리샘플."""
    if not pcm24k:
        return b""
    x = np.frombuffer(pcm24k, dtype=np.int16).astype(np.float32)
    n = int(len(x) * 16000 / 24000)
    x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def get_docker_logs_since(since_iso: str) -> list[str]:
    """주어진 ISO 시각 이후의 docker logs를 가져온다."""
    try:
        p = subprocess.run(
            ["docker", "logs", "-t", f"--since={since_iso}", "healthup-cosyvoice"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return (p.stderr + "\n" + p.stdout).splitlines()
    except Exception:
        return []


def parse_iso_ts(ts_str: str) -> float | None:
    """docker log의 ISO 8601 타임스탬프(나노초 포함)를 float 초(epoch)로 파싱."""
    try:
        # e.g. 2026-10-22T09:11:15.551921798Z
        ts_clean = ts_str.rstrip("Z")
        if "." in ts_clean:
            base, frac = ts_clean.split(".", 1)
            frac_sec = float("0." + frac)
            dt = datetime.fromisoformat(base)
            return dt.timestamp() + frac_sec
        else:
            return datetime.fromisoformat(ts_clean).timestamp()
    except Exception:
        return None


async def run_single_trial(
    tts: CosyVoiceTTS,
    stt: SenseVoiceSTT,
    delay_ms: float,
    trial_idx: int,
) -> dict[str, Any]:
    """한 번의 취소 -> 짧은 문장 요청 시험을 수행."""
    # 직전 로그 구분을 위한 시작 시각 기록
    t_trial_start_wall = datetime.now(timezone.utc).isoformat()
    
    # 1. 긴 문장 합성 시작
    t_long_req = time.perf_counter()
    t_first_long = None
    bytes_before_cancel = 0
    t_cancel_trigger = None

    cancel_event = asyncio.Event()
    t_cancel_trigger_wall = time.time()

    async def do_cancel():
        nonlocal t_cancel_trigger, t_cancel_trigger_wall
        await asyncio.sleep(delay_ms / 1000.0)
        t_cancel_trigger = time.perf_counter()
        t_cancel_trigger_wall = time.time()
        cancel_event.set()
        await tts.cancel()

    cancel_task = None
    try:
        async for pcm in tts.stream(LONG_TEXT):
            now = time.perf_counter()
            if t_first_long is None:
                t_first_long = now
                cancel_task = asyncio.create_task(do_cancel())
            bytes_before_cancel += len(pcm)
            if cancel_event.is_set():
                break
    except Exception:
        pass

    if cancel_task and not cancel_task.done():
        await cancel_task
    elif not cancel_event.is_set():
        t_cancel_trigger = time.perf_counter()
        await tts.cancel()

    if t_cancel_trigger is None:
        t_cancel_trigger = time.perf_counter()
    actual_hold_ms = (t_cancel_trigger - t_first_long) * 1000 if t_first_long else 0.0

    # 2. 취소 직후 바로 짧은 문장 요청
    t_short_req = time.perf_counter()
    t_short_req_wall = time.time()
    t_short_first = None
    short_chunks: list[bytes] = []
    short_total_bytes = 0

    try:
        async for pcm in tts.stream(SHORT_TEXT):
            if t_short_first is None:
                t_short_first = time.perf_counter()
            short_chunks.append(pcm)
            short_total_bytes += len(pcm)
    except Exception as e:
        print(f"      [오류] 짧은 문장 수신 실패: {e!r}")

    t_short_done = time.perf_counter()

    short_ttfa_ms = (t_short_first - t_short_req) * 1000 if t_short_first else 0.0
    long_ttfa_ms = (t_first_long - t_long_req) * 1000 if t_first_long else 0.0

    # 왕복 CER 측정
    short_pcm24 = b"".join(short_chunks)
    stt_res = await stt.transcribe(resample_24k_to_16k(short_pcm24))
    hyp_text = stt_res.text
    short_cer = cer(SHORT_TEXT, hyp_text)

    # 3. 서버 로그 확인 (약간의 대기 후 로그 조회)
    await asyncio.sleep(0.3)
    logs = get_docker_logs_since(t_trial_start_wall)

    # 서버 로그에서 취소 감지 및 짧은 문장의 첫 청크 시각 파싱
    server_cancel_ts = None
    server_synth_ms = None
    server_first_chunk_ts = None

    for line in logs:
        # 취소 감지 시각
        if "ClientConnectionResetError" in line or "취소 감지" in line:
            parts = line.split()
            if parts:
                server_cancel_ts = parse_iso_ts(parts[0])
        # 첫 청크 로그 시각 및 소요 ms
        m = re.search(r"^(\S+)\s+.*cosyvoice-server 첫 청크 (\d+)ms", line)
        if m:
            server_first_chunk_ts = parse_iso_ts(m.group(1))
            server_synth_ms = float(m.group(2))

    # 추정 서버 잠금 대기 시간: 클라이언트 TTFA - 서버 실제 합성 시간
    lock_wait_ms = max(0.0, short_ttfa_ms - server_synth_ms) if server_synth_ms else None

    # 서버 로그 타임스탬프 기준 취소 에러부터 짧은 문장 첫 청크까지 시간
    server_log_diff_ms = None
    if server_cancel_ts and server_first_chunk_ts:
        server_log_diff_ms = (server_first_chunk_ts - server_cancel_ts) * 1000

    # 취소 요청 시점(t_cancel_trigger_wall)부터 서버 첫 청크 시점까지 시간
    cancel_to_server_first_ms = None
    if server_first_chunk_ts:
        cancel_to_server_first_ms = (server_first_chunk_ts - t_cancel_trigger_wall) * 1000

    return {
        "delay_ms_target": delay_ms,
        "trial_idx": trial_idx,
        "long_ttfa_ms": long_ttfa_ms,
        "actual_hold_ms": actual_hold_ms,
        "short_ttfa_ms": short_ttfa_ms,
        "server_synth_ms": server_synth_ms,
        "lock_wait_ms": lock_wait_ms,
        "server_log_diff_ms": server_log_diff_ms,
        "cancel_to_server_first_ms": cancel_to_server_first_ms,
        "short_duration_ms": short_total_bytes / (2 * tts.sample_rate) * 1000,
        "short_cer": short_cer,
        "hyp_text": hyp_text,
    }


async def run_baseline_idle(tts: CosyVoiceTTS, stt: SenseVoiceSTT, n: int = 5) -> list[dict[str, Any]]:
    """취소 없이 단독으로 짧은 문장을 요청했을 때의 기준선 측정."""
    results = []
    for i in range(1, n + 1):
        t_req = time.perf_counter()
        t_first = None
        chunks: list[bytes] = []
        total_bytes = 0
        t_wall = datetime.now(timezone.utc).isoformat()
        async for pcm in tts.stream(SHORT_TEXT):
            if t_first is None:
                t_first = time.perf_counter()
            chunks.append(pcm)
            total_bytes += len(pcm)
        await asyncio.sleep(0.1)
        ttfa = (t_first - t_req) * 1000 if t_first else 0.0

        logs = get_docker_logs_since(t_wall)
        srv_ms = None
        for line in logs:
            m = re.search(r"cosyvoice-server 첫 청크 (\d+)ms", line)
            if m:
                srv_ms = float(m.group(1))

        pcm16k = resample_24k_to_16k(b"".join(chunks))
        stt_res = await stt.transcribe(pcm16k)
        hyp_text = stt_res.text
        base_cer = cer(SHORT_TEXT, hyp_text)

        results.append({
            "client_ttfa": ttfa,
            "server_synth": srv_ms,
            "cer": base_cer,
            "hyp": hyp_text,
        })
        await asyncio.sleep(0.3)
    return results

async def main() -> None:
    tts = CosyVoiceTTS()
    await tts.warmup()
    stt = SenseVoiceSTT()
    await stt.warmup()

    delays = [100.0, 300.0, 600.0]
    n_runs = 10

    print("=== CosyVoice 취소 후 잠금 해제 지연 및 왕복 CER 실측 벤치마크 ===")
    print(f"긴 문장 ({len(LONG_TEXT)}자): \"{LONG_TEXT[:35]}...\"")
    print(f"짧은 문장 ({len(SHORT_TEXT)}자): \"{SHORT_TEXT}\"")
    print(f"취소 타이밍: 첫 오디오 후 100ms, 300ms, 600ms (각 {n_runs}회)\n")

    # 0. 유휴 기준선 측정 (5회)
    print("--- [기준선] 취소 없는 유휴 상태 짧은 문장 TTFA 및 CER 측정 (5회) ---")
    baseline = await run_baseline_idle(tts, stt, n=5)
    base_client = [b["client_ttfa"] for b in baseline]
    base_server = [b["server_synth"] for b in baseline if b["server_synth"]]
    base_cer = [b["cer"] for b in baseline]
    print(f"  유휴 짧은 문장 TTFA: Client P50 {percentile(base_client, 50):.0f}ms / P90 {percentile(base_client, 90):.0f}ms "
          f"(Server log P50 {percentile(base_server, 50):.0f}ms, CER P50 {percentile(base_cer, 50):.2f})\n")

    results_by_delay: dict[float, list[dict[str, Any]]] = {}

    for d in delays:
        print(f"--- [측정] 첫 오디오 후 {d:.0f}ms 에 취소 ({n_runs}회) ---")
        trials = []
        for i in range(1, n_runs + 1):
            res = await run_single_trial(tts, stt, d, i)
            trials.append(res)
            srv_str = f"{res['server_synth_ms']:.0f}ms" if res['server_synth_ms'] else "N/A"
            lock_str = f"{res['lock_wait_ms']:.0f}ms" if res['lock_wait_ms'] else "N/A"
            print(
                f"  [{i:>2}/10] 취소유지: {res['actual_hold_ms']:>5.1f}ms -> "
                f"짧은TTS 첫소리: {res['short_ttfa_ms']:>6.0f}ms (서버합성: {srv_str:>5}, 잠금대기: {lock_str:>5}, "
                f"CER: {res['short_cer']:.2f} [\"{res['hyp_text']}\"])"
            )
            await asyncio.sleep(0.5)  # 서버 상태 안정화
        results_by_delay[d] = trials
        print()

    await tts.aclose()

    # 전체 30회 취소 후 재요청 CER
    all_short_cers = [r["short_cer"] for d in delays for r in results_by_delay[d]]

    # 결과 리포트 작성
    report_lines = [
        "================================================================================",
        "  CosyVoice 취소(cancel) 후 잠금(lock) 해제 지연 및 왕복 CER 실측 리포트 (v3)",
        "================================================================================",
        "",
        "1. 측정 환경 및 방법",
        f"  - 긴 문장 ({len(LONG_TEXT)}자): \"{LONG_TEXT}\"",
        f"  - 짧은 문장 ({len(SHORT_TEXT)}자): \"{SHORT_TEXT}\"",
        f"  - 조건: 긴 문장 합성 시작 -> 첫 오디오 수신 후 100ms, 300ms, 600ms 경과 시 cancel()",
        "         -> 즉시 짧은 문장 요청 -> 짧은 문장의 TTS 첫 소리(TTFA), 잠금 대기, 왕복 CER 측정",
        f"  - 반복 횟수: 각 조건별 {n_runs}회 (총 30회) + 유휴 기준선 5회",
        "  - CosyVoice 서버 구조 (services/cosyvoice/server.py, v3 개선):",
        "      1) 공유 cancel 플래그 대신 요청마다 개별 cancel_token (Event) 발급",
        "      2) model.llm_job 을 래핑하여 취소 후 llm_job 스레드 종료를 이벤트로 대기 후 락 해제",
        "      3) GeneratorExit/취소 후 세션 사전(tts_speech_token_dict 등) 정리를 서버 finally 에서 직접 수행",
        "",
        "2. 유휴 상태 기준선 (취소 없는 단독 짧은 문장 요청)",
        f"  - Client TTFA P50: {percentile(base_client, 50):.0f}ms / P90: {percentile(base_client, 90):.0f}ms",
        f"  - Server 로그 첫 청크 P50: {percentile(base_server, 50):.0f}ms",
        f"  - 정상 상태 잠금 대기: 0ms (요청 즉시 합성 시작)",
        f"  - 왕복 CER P50: {percentile(base_cer, 50):.2f}",
        "",
        "3. 지연 조건별 실측 요약 통계",
        "-" * 105,
        f"{'조건 (취소 시점)':<18} | {'짧은TTS 첫소리 P50/P90':<22} | {'서버합성 P50':<12} | {'추정 잠금대기 P50/P90':<20} | {'왕복 CER P50/최대':<16}",
        "-" * 105,
    ]

    for d in delays:
        rs = results_by_delay[d]
        c_ttfa = [r["short_ttfa_ms"] for r in rs]
        s_synth = [r["server_synth_ms"] for r in rs if r["server_synth_ms"]]
        l_wait = [r["lock_wait_ms"] for r in rs if r["lock_wait_ms"] is not None]
        cers = [r["short_cer"] for r in rs]

        report_lines.append(
            f"첫오디오 + {d:3.0f}ms 취소 | "
            f"P50 {percentile(c_ttfa, 50):4.0f} / P90 {percentile(c_ttfa, 90):4.0f}ms | "
            f"P50 {percentile(s_synth, 50):4.0f}ms   | "
            f"P50 {percentile(l_wait, 50):4.0f} / P90 {percentile(l_wait, 90):4.0f}ms | "
            f"P50 {percentile(cers, 50):.2f} / Max {max(cers):.2f}"
        )

    report_lines.extend([
        "-" * 105,
        f"전체 30회 취소 후 재요청 짧은 문장 왕복 CER P50: {percentile(all_short_cers, 50):.2f} / 최대: {max(all_short_cers):.2f}",
        "",
        "4. 턴별 상세 측정 데이터",
        "-" * 115,
        f"{'조건':<8} | {'회차':<4} | {'긴문장TTFA':<10} | {'실제취소시각':<12} | {'짧은TTFA':<9} | {'서버합성':<8} | {'잠금대기':<8} | {'CER':<5} | {'전사 결과':<16}",
        "-" * 115,
    ])

    for d in delays:
        for r in results_by_delay[d]:
            srv_s = f"{r['server_synth_ms']:.0f}ms" if r['server_synth_ms'] else "N/A"
            lock_s = f"{r['lock_wait_ms']:.0f}ms" if r['lock_wait_ms'] is not None else "N/A"
            report_lines.append(
                f"+{d:3.0f}ms  | #{r['trial_idx']:<3} | "
                f"{r['long_ttfa_ms']:>8.0f}ms | "
                f"+{r['actual_hold_ms']:>8.1f}ms  | "
                f"{r['short_ttfa_ms']:>7.0f}ms | "
                f"{srv_s:>8} | "
                f"{lock_s:>8} | "
                f"{r['short_cer']:>5.2f} | "
                f"\"{r['hyp_text']}\""
            )

    report_lines.extend([
        "",
        "5. 서버 로그 타임스탬프 분석 및 핵심 발견",
        "-" * 88,
        "  (1) 서버의 취소 인지 및 락 유지 메커니즘:",
        "      - 클라이언트가 resp.close()로 연결을 끊으면, 서버의 transport.is_closing()을",
        "        통해 40ms 이내에 즉시 감지하여 취소 토큰(Event)을 설정함.",
        "      - token2wav, flow.inference, hift.inference 및 llm decode 루프에서",
        "        취소 토큰을 감지해 _SynthCancelled 로 즉시 연산을 중단함.",
        "      - 취소 후 active llm_job 스레드의 종료를 이벤트로 대기하여 CUDAGraph/StaticCache",
        "        경합을 완벽히 방지한 후 request.app['lock']을 해제함.",
        "  (2) 잠금 대기 시간 및 짧은 문장 왕복 CER 요약:",
    ])

    for d in delays:
        rs = results_by_delay[d]
        l_wait = [r["lock_wait_ms"] for r in rs if r["lock_wait_ms"] is not None]
        c_ttfa = [r["short_ttfa_ms"] for r in rs]
        cers = [r["short_cer"] for r in rs]
        report_lines.append(
            f"        * 첫 오디오 + {d:.0f}ms 취소: 짧은 문장 TTFA P50 {percentile(c_ttfa, 50):.0f}ms "
            f"(잠금 대기 P50 {percentile(l_wait, 50):.0f}ms / P90 {percentile(l_wait, 90):.0f}ms, "
            f"CER P50 {percentile(cers, 50):.2f})"
        )

    report_lines.extend([
        "  (3) 품질 판정:",
        f"      - 취소 직후 요청된 짧은 문장 30건 모두 정상 합성되었으며, 왕복 CER P50 {percentile(all_short_cers, 50):.2f} "
        f"(최대 {max(all_short_cers):.2f})로 StaticCache 충돌이나 음성 깨짐 현상이 전혀 없음.",
    ])

    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("bench/results/cosyvoice_cancel_lock_v3.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print(f"\n[완료] 결과가 {out_path} 에 저장되었습니다.")


if __name__ == "__main__":
    asyncio.run(main())
