"""C2 음성 명령 의도 분류기 및 평가 세트 단위 테스트.

테스트 항목:
1. 기본 명령 10종 인식 및 세션 상태별 게이트 검증
2. 개별 기능 on/off 토글 명령 7종 및 페이로드·등급(confirm/immediate) 검증
3. AI 고지 숨김 시 법적 경고 확인(confirm) 등급 검증
4. 위기 표현(CRISIS) 전 상태 차단 및 CHAT(dialog) 판정 검증
5. 비명령 대화문(부분 일치 문장) 오판 방지 및 어절 상한 검증
6. SenseVoice 태그 및 특수문자 정규화 검증
7. 분류 지연 시간 P99 <= 5ms 예산 검증
8. 전체 평가 세트(bench/intent_set_ko.tsv) 합격선 검증
"""

from __future__ import annotations

import sys
from pathlib import Path
import time
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from voice_pipeline.intent import (
    GRADE_CONFIRM,
    GRADE_DIALOG,
    GRADE_IMMEDIATE,
    INTENT_CAMERA_OFF,
    INTENT_CAMERA_ON,
    INTENT_CHAT,
    INTENT_CONFIRM_NO,
    INTENT_CONFIRM_YES,
    INTENT_DEV_INFO_OFF,
    INTENT_DEV_INFO_ON,
    INTENT_MODE_DEMO,
    INTENT_MODE_DEV,
    INTENT_REDO_USER,
    INTENT_REPEAT_BOT,
    INTENT_RESET,
    INTENT_SESSION_END,
    INTENT_SESSION_START,
    INTENT_STOP_SPEAKING,
    INTENT_UI_BADGE_TOGGLE,
    INTENT_WAKE_OFF,
    INTENT_WAKE_ON,
    classify_intent,
    is_crisis,
    normalize_for_intent,
)


def test_basic_commands_and_states():
    """기본 명령 10종 및 상태 게이트 검증."""
    # 1. SESSION_START: idle 상태에서만 동작
    res = classify_intent("시작하자", state="idle")
    assert res.intent == INTENT_SESSION_START
    assert res.grade == GRADE_IMMEDIATE
    assert res.is_command

    res_active = classify_intent("시작하자", state="active")
    assert res_active.intent == INTENT_CHAT  # active에서는 시작 명령 무시

    # 2. STOP_SPEAKING: speaking 상태에서만 동작
    res = classify_intent("그만", state="speaking")
    assert res.intent == INTENT_STOP_SPEAKING
    assert res.grade == GRADE_IMMEDIATE

    res_idle = classify_intent("그만", state="idle")
    assert res_idle.intent == INTENT_CHAT

    # 3. REPEAT_BOT: active 상태
    res = classify_intent("다시 말해 줘", state="active")
    assert res.intent == INTENT_REPEAT_BOT
    assert res.grade == GRADE_IMMEDIATE

    # 4. REDO_USER: active 상태
    res = classify_intent("방금 거 취소", state="active")
    assert res.intent == INTENT_REDO_USER
    assert res.grade == GRADE_IMMEDIATE

    # 5. RESET: active 상태 (확인 질문 필요)
    res = classify_intent("처음부터 다시", state="active")
    assert res.intent == INTENT_RESET
    assert res.grade == GRADE_CONFIRM
    assert res.is_irreversible_or_legal

    # 6. SESSION_END: active 상태 (확인 질문 필요)
    res = classify_intent("그만할게요", state="active")
    assert res.intent == INTENT_SESSION_END
    assert res.grade == GRADE_CONFIRM
    assert res.is_irreversible_or_legal

    # 7. MODE_DEV / MODE_DEMO: active 또는 idle 상태 (확인 필요)
    res = classify_intent("개발 모드로 바꿔", state="active")
    assert res.intent == INTENT_MODE_DEV
    assert res.grade == GRADE_CONFIRM
    assert res.is_irreversible_or_legal

    res = classify_intent("데모 모드로 바꿔 줘", state="idle")
    assert res.intent == INTENT_MODE_DEMO
    assert res.grade == GRADE_CONFIRM
    assert res.is_irreversible_or_legal

    # 8. CONFIRM_YES / NO: confirming 상태에서만 동작
    res = classify_intent("네", state="confirming")
    assert res.intent == INTENT_CONFIRM_YES
    assert res.grade == GRADE_IMMEDIATE

    res = classify_intent("아니요", state="confirming")
    assert res.intent == INTENT_CONFIRM_NO
    assert res.grade == GRADE_IMMEDIATE

    # active 상태에서 '네', '아니요'는 확인 명령이 아님
    assert classify_intent("네", state="active").intent == INTENT_CHAT
    assert classify_intent("아니요", state="active").intent == INTENT_CHAT


def test_feature_toggle_commands_and_payload():
    """개별 기능 on/off 토글 명령 7종 및 페이로드 검증."""
    # CAMERA_ON: 동의 확인 질문 필요 (confirm)
    res = classify_intent("카메라 켜 줘", state="active")
    assert res.intent == INTENT_CAMERA_ON
    assert res.grade == GRADE_CONFIRM
    assert res.payload == {"target": "face", "enabled": True}
    assert res.is_irreversible_or_legal

    # CAMERA_OFF: 즉시 정지 (immediate)
    res = classify_intent("카메라 꺼 줘", state="active")
    assert res.intent == INTENT_CAMERA_OFF
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "face", "enabled": False}

    # WAKE_ON / OFF
    res = classify_intent("대기 청취 켜 줘", state="active")
    assert res.intent == INTENT_WAKE_ON
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "wake", "enabled": True}

    res = classify_intent("음성 시작 꺼 줘", state="active")
    assert res.intent == INTENT_WAKE_OFF
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "wake", "enabled": False}

    # DEV_INFO_ON / OFF
    res = classify_intent("개발 정보 보여 줘", state="active")
    assert res.intent == INTENT_DEV_INFO_ON
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "show_dev_info", "enabled": True}

    res = classify_intent("지연 표 숨겨 줘", state="active")
    assert res.intent == INTENT_DEV_INFO_OFF
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "show_dev_info", "enabled": False}


def test_ui_badge_toggle_and_legal_notice():
    """UI 배지 토글 및 AI 고지 숨김 시 법적 경고 확인 검증."""
    # 카메라 배지
    res = classify_intent("카메라 표시 숨겨 줘", state="active")
    assert res.intent == INTENT_UI_BADGE_TOGGLE
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "show_camera_badge", "enabled": False}

    res = classify_intent("카메라 배지 켜 줘", state="active")
    assert res.intent == INTENT_UI_BADGE_TOGGLE
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "show_camera_badge", "enabled": True}

    # 연결 상태 배지
    res = classify_intent("연결 상태 표시 꺼 줘", state="active")
    assert res.intent == INTENT_UI_BADGE_TOGGLE
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "show_conn_status", "enabled": False}

    # AI 고지 켜기 (immediate)
    res = classify_intent("AI 고지 보여 줘", state="active")
    assert res.intent == INTENT_UI_BADGE_TOGGLE
    assert res.grade == GRADE_IMMEDIATE
    assert res.payload == {"target": "show_ai_notice", "enabled": True}
    assert not res.is_irreversible_or_legal

    # AI 고지 숨기기 (법적 필수 고지이므로 confirm 등급 필수!)
    res = classify_intent("AI 고지 숨겨 줘", state="active")
    assert res.intent == INTENT_UI_BADGE_TOGGLE
    assert res.grade == GRADE_CONFIRM
    assert res.payload == {"target": "show_ai_notice", "enabled": False}
    assert res.is_irreversible_or_legal

    # 에이아이 한글 표기 변형
    res = classify_intent("에이아이 고지 숨겨 주세요", state="active")
    assert res.intent == INTENT_UI_BADGE_TOGGLE
    assert res.grade == GRADE_CONFIRM
    assert res.payload == {"target": "show_ai_notice", "enabled": False}
    assert res.is_irreversible_or_legal


def test_crisis_expressions_suppressed():
    """위기 표현은 모든 상태에서 절대 명령으로 분류되지 않고 CHAT으로 처리."""
    crisis_examples = [
        "다 끝내고 싶어",
        "그만 살고 싶어",
        "이제 다 그만하고 죽고 싶어요",
        "인생을 여기서 멈추고 싶어요",
        "스스로 목숨을 끊고 싶은 생각이 들어요",
        "사는 게 너무 고통스러워서 끝내고 싶어요",
    ]
    for expr in crisis_examples:
        assert is_crisis(expr)
        for st in ["active", "idle", "speaking", "confirming", None]:
            res = classify_intent(expr, state=st)
            assert not res.is_command, f"위기 표현 '{expr}'가 state={st}에서 명령으로 분류됨: {res.intent}"
            assert res.intent == INTENT_CHAT
            assert res.grade == GRADE_DIALOG
            assert res.rule == "crisis_blocked"


def test_chat_dialog_safety():
    """명령어가 부분 포함된 일상 대화문이 명령으로 오판되지 않아야 함."""
    chat_examples = [
        "그만 좀 하라고 했는데 아들이 계속 잔소리를 해요",
        "운동을 처음부터 다시 시작하려니까 무릎이 시큰거려요",
        "어제 손주가 카메라로 사진을 찍어 줬는데 참 잘 나왔어요",
        "잠깐만 걸어도 숨이 차고 다리가 후들거려요",
        "아니요라고 말하고 싶었는데 며느리 눈치가 보여서 그냥 참았어요",
        "텔레비전을 켜 놔도 재미있는 게 하나도 없어요",
        "친구한테 먼저 전화해 볼까 하다가 바쁠까 봐 그만뒀어요",
    ]
    for expr in chat_examples:
        res = classify_intent(expr, state="active")
        assert not res.is_command, f"대화문 '{expr}'가 명령으로 오판됨: {res.intent}"
        assert res.intent == INTENT_CHAT
        assert res.grade == GRADE_DIALOG


def test_normalization_and_tags():
    """SenseVoice 태그 및 구두점 정규화 검증."""
    raw = "<|HAPPY|> <|Speech|> 시작하자!?? .."
    assert normalize_for_intent(raw) == "시작하자"
    res = classify_intent(raw, state="idle")
    assert res.intent == INTENT_SESSION_START

    # 빈 문자열
    res_empty = classify_intent("", state="active")
    assert res_empty.intent == INTENT_CHAT
    assert res_empty.rule == "empty"


def test_max_length_filter():
    """어절 수 상한(>6) 초과 시 명령 검사 없이 CHAT 처리."""
    long_msg = "하나 둘 셋 넷 다섯 여섯 일곱 여덟 시작하자"
    res = classify_intent(long_msg, state="idle")
    assert res.intent == INTENT_CHAT
    assert res.rule == "max_length_exceeded"


def test_latency_budget():
    """분류 시간 P99 <= 5ms 예산 검증."""
    samples = [
        "시작하자",
        "잠깐만",
        "카메라 켜 줘",
        "AI 고지 숨겨 줘",
        "다 끝내고 싶어",
        "그만 좀 하라고 했는데 아들이 계속 잔소리를 해요",
    ]
    times_ms: list[float] = []
    for _ in range(200):
        for s in samples:
            t0 = time.perf_counter()
            _ = classify_intent(s, state="active")
            times_ms.append((time.perf_counter() - t0) * 1000.0)

    times_ms.sort()
    p99 = times_ms[int(len(times_ms) * 0.99)]
    assert p99 <= 5.0, f"P99 지연 시간 초과: {p99:.4f}ms > 5.0ms"


def test_intent_eval_bench_suite():
    """bench/intent_eval.py 실행 결과가 ALL PASS인지 검증."""
    from bench.intent_eval import run_evaluation

    tsv_file = Path("bench/intent_set_ko.tsv")
    sent_file = Path("bench/sentences_ko.txt")
    assert tsv_file.exists()
    assert sent_file.exists()

    passed = run_evaluation(tsv_file, sent_file, verbose=False)
    assert passed, "bench/intent_eval.py 평가 세트 검증 실패"
