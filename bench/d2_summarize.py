#!/usr/bin/env python3
"""D2 지연 회귀 벤치마크 결과 집계 및 판정기 (webapp_plan 작업 D2).

사용법:
  # 1) 두 파일 직접 비교:
  python bench/d2_summarize.py bench/results/latency_2610/e2e_on1.txt bench/results/latency_2610/e2e_off1.txt

  # 2) 파일 목록 명시 (A/B 모드):
  python bench/d2_summarize.py --mode-a e2e_A1.txt e2e_A2.txt --mode-b e2e_B1.txt e2e_B2.txt

  # 3) 디렉터리 내 파일 자동 분류 (on/A vs off/B):
  python bench/d2_summarize.py bench/results/latency_2610/

합격 기준 (§1.6 & §5 D2):
  1. 본문 첫 오디오 P50 차이 (Mode A - Mode B) <= +50ms
  2. 턴 확정 P50 차이 (Mode A - Mode B) <= +20ms
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Add src to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

try:
    from voice_pipeline.trace import percentile
except ImportError:
    def percentile(values: list[float], p: float) -> float:
        """선형 보간 백분위수 fallback."""
        if not values:
            raise ValueError("empty")
        xs = sorted(values)
        if len(xs) == 1:
            return xs[0]
        k = (len(xs) - 1) * p / 100.0
        lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


@dataclass
class LatencyRow:
    turn: str
    stt_ms: float
    turn_confirmed: float
    llm_first_token: float
    first_chunk: float
    backchannel: float
    tts_first_audio: float
    playback_start: float


@dataclass
class ModeSummary:
    name: str
    n_turns: int
    confirmed_p50: float
    confirmed_p90: float
    first_audio_p50: float
    first_audio_p90: float
    stt_p50: float
    llm_first_token_p50: float
    first_chunk_p50: float
    playback_start_p50: float


@dataclass
class D2Result:
    summary_a: ModeSummary
    summary_b: ModeSummary
    diff_confirmed: float
    diff_first_audio: float
    pass_confirmed: bool
    pass_first_audio: bool
    passed: bool


def parse_lat_text(text: str) -> list[LatencyRow]:
    """e2e 출력 텍스트에서 LAT: 테이블 행을 파싱한다."""
    rows: list[LatencyRow] = []
    in_lat = False

    for line in text.splitlines():
        if line.startswith("LAT:"):
            in_lat = True
            content = line[4:].strip()
            if content:
                row = _parse_row(content)
                if row:
                    rows.append(row)
            continue

        if in_lat:
            # LAT 블록 종료 조건 (ERR:, BADGE:, playback msgs:, 빈 줄 이후 다른 섹션 등)
            if any(line.startswith(prefix) for prefix in ("ERR:", "BADGE:", "playback msgs:", "LOG:")):
                break
            line_str = line.strip()
            if not line_str:
                continue
            if line_str.startswith("#"):
                row = _parse_row(line_str)
                if row:
                    rows.append(row)

    return rows


def _parse_row(line: str) -> LatencyRow | None:
    parts = line.split()
    if len(parts) >= 8 and parts[0].startswith("#"):
        try:
            return LatencyRow(
                turn=parts[0],
                stt_ms=float(parts[1]),
                turn_confirmed=float(parts[2]),
                llm_first_token=float(parts[3]),
                first_chunk=float(parts[4]),
                backchannel=float(parts[5]),
                tts_first_audio=float(parts[6]),
                playback_start=float(parts[7]),
            )
        except ValueError:
            return None
    return None


def parse_lat_files(paths: list[Path | str]) -> list[LatencyRow]:
    """여러 e2e 파일에서 지연 측정 행을 수집한다."""
    all_rows: list[LatencyRow] = []
    for p in paths:
        path = Path(p)
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        rows = parse_lat_text(text)
        all_rows.extend(rows)
    return all_rows


def summarize_rows(rows: list[LatencyRow], name: str = "Mode") -> ModeSummary:
    """행 목록의 지연 메트릭 통계(P50, P90)를 계산한다."""
    if not rows:
        raise ValueError(f"{name}에 유효한 턴 데이터가 없습니다.")

    confs = [r.turn_confirmed for r in rows]
    audios = [r.tts_first_audio for r in rows]
    stts = [r.stt_ms for r in rows]
    llm_tokens = [r.llm_first_token for r in rows]
    chunks = [r.first_chunk for r in rows]
    plays = [r.playback_start for r in rows]

    return ModeSummary(
        name=name,
        n_turns=len(rows),
        confirmed_p50=percentile(confs, 50),
        confirmed_p90=percentile(confs, 90),
        first_audio_p50=percentile(audios, 50),
        first_audio_p90=percentile(audios, 90),
        stt_p50=percentile(stts, 50),
        llm_first_token_p50=percentile(llm_tokens, 50),
        first_chunk_p50=percentile(chunks, 50),
        playback_start_p50=percentile(plays, 50),
    )


def evaluate_d2(rows_a: list[LatencyRow], rows_b: list[LatencyRow],
                name_a: str = "Mode A (새 기능 켬)",
                name_b: str = "Mode B (새 기능 끔)") -> D2Result:
    """Mode A와 Mode B의 결과를 비교하여 합격 여부를 판정한다."""
    sum_a = summarize_rows(rows_a, name=name_a)
    sum_b = summarize_rows(rows_b, name=name_b)

    diff_confirmed = sum_a.confirmed_p50 - sum_b.confirmed_p50
    diff_first_audio = sum_a.first_audio_p50 - sum_b.first_audio_p50

    pass_confirmed = diff_confirmed <= 20.0
    pass_first_audio = diff_first_audio <= 50.0
    passed = pass_confirmed and pass_first_audio

    return D2Result(
        summary_a=sum_a,
        summary_b=sum_b,
        diff_confirmed=diff_confirmed,
        diff_first_audio=diff_first_audio,
        pass_confirmed=pass_confirmed,
        pass_first_audio=pass_first_audio,
        passed=passed,
    )


def format_report(result: D2Result) -> str:
    """사람이 읽기 편한 판정 보고서 문자열을 생성한다."""
    sa = result.summary_a
    sb = result.summary_b

    status_conf = "✅ 합격" if result.pass_confirmed else "❌ 불합격"
    status_audio = "✅ 합격" if result.pass_first_audio else "❌ 불합격"
    final_status = "🎉 최종 합격 (PASS)" if result.passed else "⚠️ 최종 불합격 (FAIL)"

    lines = [
        "================================================================================",
        "                     D2 지연 회귀 벤치마크 평가 결과",
        "================================================================================",
        f"1. 대상 모드 및 턴 수:",
        f"   - {sa.name}: 총 {sa.n_turns}턴",
        f"   - {sb.name}: 총 {sb.n_turns}턴",
        "",
        "2. 주요 구간 지연 백분위수 (ms, 실제 발화 끝 기준):",
        f"   {'메트릭':<24} | {sa.name + ' (P50/P90)':<25} | {sb.name + ' (P50/P90)':<25}",
        f"   {'-'*24}-|-{'-'*25}-|-{'-'*25}",
        f"   {'턴 확정 (turn_confirmed)':<24} | {sa.confirmed_p50:6.1f} / {sa.confirmed_p90:6.1f} ms          | {sb.confirmed_p50:6.1f} / {sb.confirmed_p90:6.1f} ms",
        f"   {'본문 첫 오디오 (tts_first)':<24} | {sa.first_audio_p50:6.1f} / {sa.first_audio_p90:6.1f} ms          | {sb.first_audio_p50:6.1f} / {sb.first_audio_p90:6.1f} ms",
        f"   {'STT 지연':<24} | {sa.stt_p50:6.1f} ms                    | {sb.stt_p50:6.1f} ms",
        f"   {'LLM 첫 토큰':<24} | {sa.llm_first_token_p50:6.1f} ms                    | {sb.llm_first_token_p50:6.1f} ms",
        f"   {'첫 청크':<24} | {sa.first_chunk_p50:6.1f} ms                    | {sb.first_chunk_p50:6.1f} ms",
        f"   {'브라우저 재생 시작':<24} | {sa.playback_start_p50:6.1f} ms                    | {sb.playback_start_p50:6.1f} ms",
        "",
        "3. D2 합격 기준 판정 (§1.6 & §5 D2):",
        f"   (1) 턴 확정 P50 차이:      {result.diff_confirmed:+6.1f} ms (기준: <= +20.0 ms) -> {status_conf}",
        f"   (2) 본문 첫 오디오 P50 차이: {result.diff_first_audio:+6.1f} ms (기준: <= +50.0 ms) -> {status_audio}",
        "",
        f"4. 종합 판정: {final_status}",
        "================================================================================",
    ]
    return "\n".join(lines)


def auto_categorize_files(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """파일명 패턴에 따라 Mode A와 Mode B로 자동 분류한다."""
    mode_a: list[Path] = []
    mode_b: list[Path] = []

    for f in sorted(files):
        stem = f.stem.lower()
        if any(token in stem for token in ("_on", "_a", "on1", "on2", "mode_a", "e2e_a")):
            mode_a.append(f)
        elif any(token in stem for token in ("_off", "_b", "off1", "off2", "mode_b", "e2e_b")):
            mode_b.append(f)

    return mode_a, mode_b


def main() -> int:
    parser = argparse.ArgumentParser(description="D2 지연 회귀 벤치마크 결과 집계기")
    parser.add_argument("paths", nargs="*", help="e2e txt 파일 또는 디렉터리 경로")
    parser.add_argument("--mode-a", nargs="+", help="Mode A (새 기능 켬) 파일 목록")
    parser.add_argument("--mode-b", nargs="+", help="Mode B (새 기능 끔) 파일 목록")
    args = parser.parse_args()

    files_a: list[Path] = []
    files_b: list[Path] = []

    if args.mode_a or args.mode_b:
        if not (args.mode_a and args.mode_b):
            parser.error("--mode-a와 --mode-b는 함께 지정해야 합니다.")
        files_a = [Path(p) for p in args.mode_a]
        files_b = [Path(p) for p in args.mode_b]
    elif len(args.paths) == 2 and all(Path(p).is_file() for p in args.paths):
        # 2개 파일이 직접 주어진 경우 [0]=A, [1]=B
        files_a = [Path(args.paths[0])]
        files_b = [Path(args.paths[1])]
    elif args.paths:
        target_files: list[Path] = []
        for p_str in args.paths:
            p = Path(p_str)
            if p.is_dir():
                target_files.extend(list(p.glob("*.txt")))
            elif p.is_file():
                target_files.append(p)
        files_a, files_b = auto_categorize_files(target_files)
    else:
        # 기본 디렉터리 탐색
        def_dir = Path("bench/results/latency_2610")
        if def_dir.exists():
            files_a, files_b = auto_categorize_files(list(def_dir.glob("*.txt")))

    if not files_a or not files_b:
        print("오류: Mode A 또는 Mode B에 해당하는 e2e 파일이 지정되지 않았거나 찾을 수 없습니다.", file=sys.stderr)
        print("예시: python bench/d2_summarize.py bench/results/latency_2610/e2e_on1.txt bench/results/latency_2610/e2e_off1.txt", file=sys.stderr)
        return 2

    print(f"Mode A 파일 ({len(files_a)}개): {[f.name for f in files_a]}")
    print(f"Mode B 파일 ({len(files_b)}개): {[f.name for f in files_b]}")

    rows_a = parse_lat_files(files_a)
    rows_b = parse_lat_files(files_b)

    if not rows_a or not rows_b:
        print(f"오류: LAT 데이터를 파싱할 수 없습니다 (A: {len(rows_a)}행, B: {len(rows_b)}행)", file=sys.stderr)
        return 2

    res = evaluate_d2(rows_a, rows_b)
    print("\n" + format_report(res))

    return 0 if res.passed else 1


if __name__ == "__main__":
    sys.exit(main())
