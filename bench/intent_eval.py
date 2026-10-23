"""음성 명령 의도 분류기 평가 스크립트 (webapp_plan 작업 C2).

평가 데이터셋:
  1. bench/intent_set_ko.tsv:
     - 기본 명령 80개 (10종 x 8개)
     - 개별 기능 토글 명령 48개 (7종)
     - 위기 표현 20개 (CRISIS)
     - 일상·상담 대화문 60개 (CHAT)
  2. bench/sentences_ko.txt:
     - 기존 상담 및 대화문 40개

합격 기준 (webapp_plan.md §5 C2):
  1. 명령 재현율(128문장) >= 95%
  2. 위기 표현(20문장) 중 명령 판정 = 0건 (모든 상태에서 CHAT)
  3. 비명령 대화문 100문장(CHAT 60 + sentences_ko 40)에서:
     - 되돌릴 수 없는 명령 / 법적 경고 명령 판정 = 0건
     - 기타 명령 판정 <= 2% (2건 이하)
  4. 분류 시간 P99 <= 5ms
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, NamedTuple
import wave

import numpy as np

# voice_pipeline 경로 추가
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from voice_pipeline.intent import (
    GRADE_CONFIRM,
    GRADE_DIALOG,
    GRADE_IMMEDIATE,
    INTENT_CHAT,
    INTENT_CONFIRM_NO,
    INTENT_CONFIRM_YES,
    INTENT_SESSION_START,
    INTENT_STOP_SPEAKING,
    IntentResult,
    classify_intent,
)


class TestCase(NamedTuple):
    text: str
    expected_intent: str
    eval_state: str


def load_dataset(tsv_path: Path) -> list[TestCase]:
    """intent_set_ko.tsv 파싱 및 평가 상태 결정."""
    cases: list[TestCase] = []
    with open(tsv_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            text, intent = parts[0].strip(), parts[1].strip()

            # 상태별 평가 규칙 (§3.1, §5 C2)
            if intent == INTENT_SESSION_START:
                state = "idle"
            elif intent == INTENT_STOP_SPEAKING:
                state = "speaking"
            elif intent in (INTENT_CONFIRM_YES, INTENT_CONFIRM_NO):
                state = "confirming"
            elif intent == "CRISIS":
                state = "active"
            else:
                state = "active"

            cases.append(TestCase(text=text, expected_intent=intent, eval_state=state))
    return cases


def load_sentences_ko(txt_path: Path) -> list[str]:
    """bench/sentences_ko.txt 파싱."""
    sentences: list[str] = []
    with open(txt_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sentences.append(line)
    return sentences


def run_evaluation(
    tsv_path: Path,
    sentences_path: Path,
    verbose: bool = False,
) -> bool:
    print(f"=== HealthUp 음성 명령 의도 분류기 평가 (C2) ===")
    print(f"평가 세트: {tsv_path}")
    print(f"대화문 세트: {sentences_path}")
    print("-" * 60)

    dataset = load_dataset(tsv_path)
    extra_chat = load_sentences_ko(sentences_path)

    commands = [c for c in dataset if c.expected_intent not in ("CRISIS", "CHAT")]
    crisis_cases = [c for c in dataset if c.expected_intent == "CRISIS"]
    tsv_chat_cases = [c for c in dataset if c.expected_intent == "CHAT"]

    # 1. 명령 재현율 평가 (128문장)
    cmd_correct = 0
    cmd_failures: list[tuple[TestCase, IntentResult]] = []

    for c in commands:
        res = classify_intent(c.text, state=c.eval_state)
        if res.intent == c.expected_intent:
            cmd_correct += 1
        else:
            cmd_failures.append((c, res))

    cmd_total = len(commands)
    cmd_recall_pct = (cmd_correct / cmd_total * 100.0) if cmd_total else 0.0
    pass_recall = cmd_recall_pct >= 95.0

    # 2. CRISIS 위기 표현 오판 평가 (20문장)
    crisis_failures: list[tuple[str, str, IntentResult]] = []
    # 위기 표현은 active 뿐만 아니라 idle, speaking, confirming, None 모든 상태에서 검사
    check_states = ["active", "idle", "speaking", "confirming", None]

    for c in crisis_cases:
        for st in check_states:
            res = classify_intent(c.text, state=st)
            if res.is_command:
                crisis_failures.append((c.text, str(st), res))

    pass_crisis = len(crisis_failures) == 0

    # 3. 비명령 대화문 100개 오판 평가 (TSV 60 + sentences_ko 40)
    all_chat_texts = [c.text for c in tsv_chat_cases] + extra_chat
    irreversible_fp: list[tuple[str, IntentResult]] = []
    other_cmd_fp: list[tuple[str, IntentResult]] = []

    for text in all_chat_texts:
        res = classify_intent(text, state="active")
        if res.is_command:
            if res.is_irreversible_or_legal:
                irreversible_fp.append((text, res))
            else:
                other_cmd_fp.append((text, res))

    chat_total = len(all_chat_texts)
    pass_irreversible = len(irreversible_fp) == 0
    other_fp_pct = (len(other_cmd_fp) / chat_total * 100.0) if chat_total else 0.0
    pass_other_fp = other_fp_pct <= 2.0

    # 4. 분류 지연 시간 평가 (P99 <= 5ms)
    all_texts_for_latency = [c.text for c in dataset] + extra_chat
    latencies_ms: list[float] = []

    # 20회 반복 측정으로 안정적인 분위수 산출
    for _ in range(20):
        for text in all_texts_for_latency:
            t0 = time.perf_counter()
            _ = classify_intent(text, state="active")
            latencies_ms.append((time.perf_counter() - t0) * 1000.0)

    latencies_ms.sort()
    n_samples = len(latencies_ms)
    p50_ms = latencies_ms[int(n_samples * 0.50)]
    p90_ms = latencies_ms[int(n_samples * 0.90)]
    p99_ms = latencies_ms[int(n_samples * 0.99)]
    pass_latency = p99_ms <= 5.0

    # 결과 출력
    print(f"1. 명령 재현율 : {cmd_correct}/{cmd_total} ({cmd_recall_pct:.1f}%) [기준: >= 95%] -> {'PASS' if pass_recall else 'FAIL'}")
    if cmd_failures and (verbose or not pass_recall):
        for c, r in cmd_failures[:5]:
            print(f"   [오인식] '{c.text}' -> 기대: {c.expected_intent}, 실제: {r.intent}")

    print(f"2. 위기 표현 명령 오판 : {len(crisis_failures)}건 [기준: 0건] -> {'PASS' if pass_crisis else 'FAIL'}")
    if crisis_failures:
        for text, st, r in crisis_failures[:5]:
            print(f"   [위기 오판] state={st} '{text}' -> {r.intent}")

    print(f"3. 비명령 대화문 오판 (총 {chat_total}문장):")
    print(f"   - 되돌릴 수 없는/법적의무 명령 오판: {len(irreversible_fp)}건 [기준: 0건] -> {'PASS' if pass_irreversible else 'FAIL'}")
    if irreversible_fp:
        for text, r in irreversible_fp:
            print(f"     [치명적 오판] '{text}' -> {r.intent} ({r.payload})")
    print(f"   - 기타 명령 오판: {len(other_cmd_fp)}건 ({other_fp_pct:.1f}%) [기준: <= 2%] -> {'PASS' if pass_other_fp else 'FAIL'}")
    if other_cmd_fp:
        for text, r in other_cmd_fp:
            print(f"     [오판] '{text}' -> {r.intent}")

    print(f"4. 분류 지연 시간 (N={n_samples}):")
    print(f"   - P50: {p50_ms:.4f} ms")
    print(f"   - P90: {p90_ms:.4f} ms")
    print(f"   - P99: {p99_ms:.4f} ms [기준: <= 5.0ms] -> {'PASS' if pass_latency else 'FAIL'}")
    print("-" * 60)

    all_passed = pass_recall and pass_crisis and pass_irreversible and pass_other_fp and pass_latency
    if all_passed:
        print(">>> C2 판정 결과: ALL PASS (모든 합격 기준 충족) <<<")
    else:
        print(">>> C2 판정 결과: FAIL (일부 기준 미달) <<<")

    return all_passed


# ==============================================================================
# D1 음성 명령 녹음 평가 (WAV -> STT -> 의도 분류 -> 혼동표)
# ==============================================================================

class WavTestCase(NamedTuple):
    text: str
    expected_intent: str
    wav_filename: str
    repeat: int
    eval_state: str


def load_wav_script(script_path: Path) -> list[WavTestCase]:
    """bench/intent_wav_script_ko.tsv 파싱 (5열 탭 구분)."""
    cases: list[WavTestCase] = []
    with open(script_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            text = parts[0].strip()
            intent = parts[1].strip()
            wav_filename = parts[2].strip() if len(parts) > 2 else ""
            repeat = int(parts[3].strip()) if len(parts) > 3 and parts[3].strip().isdigit() else 1
            eval_state = parts[4].strip() if len(parts) > 4 else "active"
            cases.append(WavTestCase(
                text=text,
                expected_intent=intent,
                wav_filename=wav_filename,
                repeat=repeat,
                eval_state=eval_state,
            ))
    return cases


def load_wav_pcm16(path: Path) -> bytes:
    """WAV 파일을 16kHz 모노 16비트 PCM 바이트로 읽는다."""
    with wave.open(str(path), "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"16-bit PCM WAV만 지원: {path} (sampwidth={sw})")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != 16000:
        n = int(len(x) * 16000 / sr)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def create_stt_engine(engine_name: str = "sensevoice") -> Any:
    engine_name = engine_name.lower()
    if engine_name == "sensevoice":
        from voice_pipeline.stt import SenseVoiceSTT
        return SenseVoiceSTT()
    elif engine_name in ("qwen3asr", "qwen3"):
        from voice_pipeline.stt.qwen3asr import Qwen3ASRSTT
        return Qwen3ASRSTT()
    elif engine_name == "mock":
        from voice_pipeline.web.mocks import MockSTT
        return MockSTT()
    else:
        raise ValueError(f"지원하지 않는 STT 엔진: {engine_name} (sensevoice, qwen3asr, mock 중 선택)")


def _transcribe_pcm(stt: Any, pcm: bytes) -> tuple[str, float]:
    """STT 엔진으로 PCM16 바이트를 전사하여 (텍스트, 소요ms) 반환."""
    t0 = time.perf_counter()
    if hasattr(stt, "_run"):
        res = stt._run(pcm)
    elif callable(getattr(stt, "transcribe", None)):
        fn = stt.transcribe
        if asyncio.iscoroutinefunction(fn):
            try:
                res = asyncio.run(fn(pcm))
            except RuntimeError:
                loop = asyncio.get_event_loop()
                res = loop.run_until_complete(fn(pcm))
        else:
            r = fn(pcm)
            if asyncio.iscoroutine(r):
                try:
                    res = asyncio.run(r)
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                    res = loop.run_until_complete(r)
            else:
                res = r
    else:
        raise ValueError(f"STT 인스턴스에 _run 또는 transcribe 메서드가 없습니다: {stt}")

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    text = getattr(res, "text", "") or ""
    return text, elapsed_ms


class WavEvalItem(NamedTuple):
    case: WavTestCase
    wav_path: Path
    hyp_text: str
    intent_res: IntentResult
    stt_ms: float


def run_wav_evaluation(
    script_path: Path,
    wav_dir: Path,
    stt_engine: str = "sensevoice",
    stt_instance: Any = None,
    output_path: Path | None = None,
    verbose: bool = False,
) -> tuple[bool, dict[str, Any]]:
    """D1 실제/모의 음성 녹음 기반 의도 분류 및 혼동표 평가."""
    lines: list[str] = []
    def log(msg: str = "") -> None:
        print(msg)
        lines.append(msg)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log("=" * 70)
    log("  HealthUp 음성 명령 녹음 평가 보고서 (webapp_plan 작업 D1)")
    log(f"  일시: {timestamp}")
    log(f"  대본 파일: {script_path}")
    log(f"  WAV 폴더: {wav_dir}")
    log(f"  STT 엔진: {stt_engine if stt_instance is None else type(stt_instance).__name__}")
    log("=" * 70)

    all_cases = load_wav_script(script_path)
    found_cases: list[tuple[WavTestCase, Path]] = []
    missing_cases: list[WavTestCase] = []

    for c in all_cases:
        p = wav_dir / c.wav_filename
        if p.is_file():
            found_cases.append((c, p))
        else:
            missing_cases.append(c)

    log(f"\n[1] WAV 파일 적재 현황")
    log(f"  - 전체 대본 항목: {len(all_cases)}개")
    log(f"  - 발견된 WAV 파일: {len(found_cases)}개")
    log(f"  - 누락된 WAV 파일: {len(missing_cases)}개")

    if not found_cases:
        log("\n[주의] 평가할 WAV 파일이 0개입니다. 모든 음성 파일이 누락되었습니다.")
        log("docs/plans/d1_user_guide.md를 참고하여 녹음을 먼저 진행해 주세요.")
        if missing_cases:
            log("\n[누락된 WAV 파일 목록 (총 %d개)]" % len(missing_cases))
            for mc in missing_cases:
                log(f"  - {mc.wav_filename} (원문: '{mc.text}', 기대: {mc.expected_intent})")

        out_file = output_path or (REPO_ROOT / "bench" / "results" / f"intent_wav_{datetime.now().strftime('%Y%m%d')}.txt")
        out_file.parent.mkdir(parents=True, exist_ok=True)
        log(f"\n결과 저장: {out_file}")
        log(">>> D1 판정 결과: FAIL (WAV 파일 없음) <<<")
        out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return False, {
            "total_cases": len(all_cases),
            "found_count": 0,
            "missing_count": len(missing_cases),
            "cmd_total": 0,
            "cmd_correct": 0,
            "cmd_recall": 0.0,
            "pass_recall": False,
            "irreversible_fps": 0,
            "pass_irreversible": True,
            "crisis_fps": 0,
            "pass_crisis": True,
            "all_passed": False,
            "output_file": str(out_file),
        }

    # STT 인스턴스 준비
    stt = stt_instance if stt_instance is not None else create_stt_engine(stt_engine)
    if hasattr(stt, "warmup"):
        try:
            asyncio.run(stt.warmup())
        except Exception:
            pass

    evaluated: list[WavEvalItem] = []
    for c, p in found_cases:
        pcm = load_wav_pcm16(p)
        hyp_text, stt_ms = _transcribe_pcm(stt, pcm)
        res = classify_intent(hyp_text, state=c.eval_state)
        evaluated.append(WavEvalItem(case=c, wav_path=p, hyp_text=hyp_text, intent_res=res, stt_ms=stt_ms))

    # 지표 집계
    commands = [item for item in evaluated if item.case.expected_intent not in ("CHAT", "CRISIS")]
    cmd_correct = sum(1 for item in commands if item.intent_res.intent == item.case.expected_intent)
    cmd_total = len(commands)
    cmd_recall_pct = (cmd_correct / cmd_total * 100.0) if cmd_total > 0 else 0.0
    pass_recall = cmd_recall_pct >= 90.0 if cmd_total > 0 else False

    # 되돌릴 수 없는 명령 / 법적 경고 명령 오실행
    irreversible_fps = [
        item for item in evaluated
        if item.intent_res.is_irreversible_or_legal and item.case.expected_intent != item.intent_res.intent
    ]
    pass_irreversible = len(irreversible_fps) == 0

    # 위기 표현 중 명령 오판
    crisis_items = [item for item in evaluated if item.case.expected_intent == "CRISIS"]
    crisis_fps = [item for item in crisis_items if item.intent_res.is_command]
    pass_crisis = len(crisis_fps) == 0

    # 비명령(CHAT) 중 명령 오판
    chat_items = [item for item in evaluated if item.case.expected_intent == "CHAT"]
    chat_fps = [item for item in chat_items if item.intent_res.is_command]

    log("\n[2] 핵심 성능 지표 (webapp_plan.md §5 D1 기준 대조)")
    log(f"  1. 명령 재현율: {cmd_correct}/{cmd_total} ({cmd_recall_pct:.1f}%) [기준: >= 90%] -> {'PASS' if pass_recall else 'FAIL'}")
    log(f"  2. 되돌릴 수 없는/법적의무 명령 오실행: {len(irreversible_fps)}건 [기준: 0건] -> {'PASS' if pass_irreversible else 'FAIL'}")
    if irreversible_fps:
        for it in irreversible_fps:
            log(f"     * [치명적 오실행] {it.case.wav_filename}: 기대={it.case.expected_intent}, 전사='{it.hyp_text}', 예측={it.intent_res.intent}")
    log(f"  3. 위기 표현 중 명령 오판: {len(crisis_fps)}/{len(crisis_items)}건 [기준: 0건] -> {'PASS' if pass_crisis else 'FAIL'}")
    if crisis_fps:
        for it in crisis_fps:
            log(f"     * [위기 오판] {it.case.wav_filename}: 기대=CRISIS, 전사='{it.hyp_text}', 예측={it.intent_res.intent}")
    log(f"  4. 비명령(CHAT) 중 기타 명령 오판: {len(chat_fps)}/{len(chat_items)}건")

    # 혼동표 (Confusion Matrix)
    labels = sorted(list({it.case.expected_intent for it in evaluated} | {it.intent_res.intent for it in evaluated}))
    matrix: dict[str, dict[str, int]] = {r: {c: 0 for c in labels} for r in labels}
    for it in evaluated:
        matrix[it.case.expected_intent][it.intent_res.intent] += 1

    log("\n[3] 혼동표 (Confusion Matrix)")
    log("  행: 실제 정답 (Expected) | 열: 분류 예측 (Predicted)")
    header = f"  {'Expected \\ Pred':<22} | " + " | ".join(f"{l[:10]:>10}" for l in labels)
    log("  " + "-" * (len(header) - 2))
    log(header)
    log("  " + "-" * (len(header) - 2))
    for r in labels:
        row_str = f"  {r:<22} | " + " | ".join(f"{matrix[r][c]:>10}" for c in labels)
        log(row_str)
    log("  " + "-" * (len(header) - 2))

    # 오인식 내역
    mismatches = [it for it in evaluated if it.case.expected_intent != it.intent_res.intent]
    log(f"\n[4] 오인식 세부 내역 (총 {len(mismatches)}건)")
    if not mismatches:
        log("  (모든 평가 샘플이 의도와 100% 일치합니다)")
    else:
        for it in mismatches[:30]:
            log(f"  - [{it.case.wav_filename}] 원문: '{it.case.text}' -> STT: '{it.hyp_text}' ({it.stt_ms:.1f}ms) | 기대: {it.case.expected_intent} != 예측: {it.intent_res.intent} (규칙: {it.intent_res.rule})")
        if len(mismatches) > 30:
            log(f"  ... 외 {len(mismatches) - 30}건 생략")

    # 누락된 파일 목록
    if missing_cases:
        log(f"\n[5] 누락된 WAV 파일 목록 (총 {len(missing_cases)}개 건너뜀)")
        for mc in missing_cases[:20]:
            log(f"  - {mc.wav_filename} (원문: '{mc.text}', 상태: {mc.eval_state})")
        if len(missing_cases) > 20:
            log(f"  ... 외 {len(missing_cases) - 20}개 생략")

    all_passed = pass_recall and pass_irreversible and pass_crisis
    log("\n" + "=" * 70)
    if all_passed:
        log(">>> D1 판정 결과: ALL PASS (모든 합격 기준 충족) <<<")
    else:
        log(">>> D1 판정 결과: FAIL (일부 기준 미달) <<<")
    log("=" * 70)

    out_file = output_path or (REPO_ROOT / "bench" / "results" / f"intent_wav_{datetime.now().strftime('%Y%m%d')}.txt")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"\n결과 보고서 저장 완료: {out_file}")

    summary = {
        "total_cases": len(all_cases),
        "found_count": len(found_cases),
        "missing_count": len(missing_cases),
        "cmd_total": cmd_total,
        "cmd_correct": cmd_correct,
        "cmd_recall": cmd_recall_pct,
        "pass_recall": pass_recall,
        "irreversible_fps": len(irreversible_fps),
        "pass_irreversible": pass_irreversible,
        "crisis_fps": len(crisis_fps),
        "pass_crisis": pass_crisis,
        "all_passed": all_passed,
        "output_file": str(out_file),
    }
    return all_passed, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="HealthUp 의도 분류기 평가 (C2 / D1)")
    parser.add_argument("tsv", nargs="?", default="bench/intent_set_ko.tsv", help="평가 TSV 파일 경로 (C2 텍스트 세트)")
    parser.add_argument("--sentences", default="bench/sentences_ko.txt", help="기존 문장 텍스트 파일 경로 (C2 대화문)")
    parser.add_argument("--wav-dir", default=None, help="D1 음성 명령 WAV 폴더 경로 (예: bench/results/intent_wav/)")
    parser.add_argument("--script", default="bench/intent_wav_script_ko.tsv", help="D1 녹음 대본 TSV 경로")
    parser.add_argument("--stt", default="sensevoice", choices=["sensevoice", "qwen3asr", "mock"], help="STT 엔진 선택 (기본: sensevoice)")
    parser.add_argument("--output", default=None, help="결과 저장 파일 경로 (기본: bench/results/intent_wav_<YYYYMMDD>.txt)")
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 실패 로그 출력")
    args = parser.parse_args()

    if args.wav_dir is not None:
        script_path = Path(args.script)
        wav_dir = Path(args.wav_dir)
        output_path = Path(args.output) if args.output else None
        if not script_path.exists():
            print(f"오류: 녹음 대본 TSV 파일을 찾을 수 없습니다: {script_path}", file=sys.stderr)
            sys.exit(2)
        if not wav_dir.exists():
            print(f"경고: WAV 폴더가 존재하지 않습니다: {wav_dir}", file=sys.stderr)
            wav_dir.mkdir(parents=True, exist_ok=True)

        passed, _ = run_wav_evaluation(
            script_path=script_path,
            wav_dir=wav_dir,
            stt_engine=args.stt,
            output_path=output_path,
            verbose=args.verbose,
        )
        sys.exit(0 if passed else 1)
    else:
        tsv_path = Path(args.tsv)
        sentences_path = Path(args.sentences)

        if not tsv_path.exists():
            print(f"오류: 평가 세트 파일을 찾을 수 없습니다: {tsv_path}", file=sys.stderr)
            sys.exit(2)
        if not sentences_path.exists():
            print(f"오류: 문장 세트 파일을 찾을 수 없습니다: {sentences_path}", file=sys.stderr)
            sys.exit(2)

        passed = run_evaluation(tsv_path, sentences_path, verbose=args.verbose)
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
