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
from pathlib import Path
import sys
import time
from typing import NamedTuple

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


def main() -> None:
    parser = argparse.ArgumentParser(description="HealthUp 의도 분류기 평가 (C2)")
    parser.add_argument("tsv", nargs="?", default="bench/intent_set_ko.tsv", help="평가 TSV 파일 경로")
    parser.add_argument("--sentences", default="bench/sentences_ko.txt", help="기존 문장 텍스트 파일 경로")
    parser.add_argument("-v", "--verbose", action="store_true", help="상세 실패 로그 출력")
    args = parser.parse_args()

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
