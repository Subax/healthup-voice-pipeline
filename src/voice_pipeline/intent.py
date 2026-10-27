"""음성 명령 의도 분류기 (webapp_plan 작업 C2, dev-guide §3).

규칙 기반 빠른 의도 분류:
1. 위기 표현 우선 검사: 위기/자해/극단적 선택 표현은 명령으로 보지 않고 즉시 CHAT(대화)으로 분류
2. 정규화 및 길이 상한 검사: SenseVoice 태그·문장부호 제거, 어절 상한(>6) 시 대화로 분류
3. 세션 상태별 허용 의도 제한 (idle / speaking / active / confirming)
4. 되돌릴 수 없는 명령(RESET, SESSION_END, MODE 전환) 및 법적 동의/경고(CAMERA_ON, AI 고지 숨김)는 confirm 등급 부여
5. 분류 지연 시간 P99 <= 5ms 충족 (순수 정규식/문자열 처리)
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import time
from typing import Any

log = logging.getLogger("voice_pipeline.intent")

# 의도(Intent) 상수
INTENT_SESSION_START = "SESSION_START"
INTENT_STOP_SPEAKING = "STOP_SPEAKING"
INTENT_REPEAT_BOT = "REPEAT_BOT"
INTENT_REDO_USER = "REDO_USER"
INTENT_RESET = "RESET"
INTENT_SESSION_END = "SESSION_END"
INTENT_MODE_DEV = "MODE_DEV"
INTENT_MODE_DEMO = "MODE_DEMO"
INTENT_CONFIRM_YES = "CONFIRM_YES"
INTENT_CONFIRM_NO = "CONFIRM_NO"

# 개별 기능 on/off 의도 상수
INTENT_CAMERA_ON = "CAMERA_ON"
INTENT_CAMERA_OFF = "CAMERA_OFF"
INTENT_WAKE_ON = "WAKE_ON"
INTENT_WAKE_OFF = "WAKE_OFF"
INTENT_DEV_INFO_ON = "DEV_INFO_ON"
INTENT_DEV_INFO_OFF = "DEV_INFO_OFF"
INTENT_UI_BADGE_TOGGLE = "UI_BADGE_TOGGLE"

# 비명령 / 대화 라벨 (B1 dev_event 및 web_ui_check 호환)
INTENT_TALK = "TALK"
INTENT_CHAT = "TALK"

# 등급(Grade) 상수
GRADE_IMMEDIATE = "immediate"  # 확인 없이 즉시 실행
GRADE_CONFIRM = "confirm"      # 확인 질문 후 사용자 동의 시 실행
GRADE_DIALOG = "dialog"        # 명령 아님 (일반 대화 / LLM 응답 경로)

# 되돌릴 수 없는 명령 또는 법적 경고가 필요한 명령 집합 (비명령 오판 허용 0건)
IRREVERSIBLE_OR_LEGAL_INTENTS = {
    INTENT_RESET,
    INTENT_SESSION_END,
    INTENT_MODE_DEV,
    INTENT_MODE_DEMO,
    INTENT_CAMERA_ON,
}

# 상태별 허용 의도 목록 (§3.1)
STATE_ALLOWED_INTENTS: dict[str, set[str]] = {
    "idle": {
        INTENT_SESSION_START,
        INTENT_MODE_DEV,
        INTENT_MODE_DEMO,
        INTENT_CAMERA_ON,
        INTENT_CAMERA_OFF,
        INTENT_WAKE_ON,
        INTENT_WAKE_OFF,
        INTENT_DEV_INFO_ON,
        INTENT_DEV_INFO_OFF,
        INTENT_UI_BADGE_TOGGLE,
    },
    "speaking": {
        INTENT_STOP_SPEAKING,
    },
    "confirming": {
        INTENT_CONFIRM_YES,
        INTENT_CONFIRM_NO,
    },
    "active": {
        INTENT_REPEAT_BOT,
        INTENT_REDO_USER,
        INTENT_RESET,
        INTENT_SESSION_END,
        INTENT_MODE_DEV,
        INTENT_MODE_DEMO,
        INTENT_CAMERA_ON,
        INTENT_CAMERA_OFF,
        INTENT_WAKE_ON,
        INTENT_WAKE_OFF,
        INTENT_DEV_INFO_ON,
        INTENT_DEV_INFO_OFF,
        INTENT_UI_BADGE_TOGGLE,
    },
}

# 태그 및 문장부호 제거 정규식
_TAG_RE = re.compile(r"<\|[^|]*\|>")
_PUNCT_RE = re.compile(r"[.,!?~…·、，。？！\-—'\"“”‘’()\[\]]+")

# 위기 표현 정규식 (§3.3 1단계)
_CRISIS_PATTERNS = [
    r"죽고\s*싶",
    r"죽어\s*버릴",
    r"살고\s*싶지\s*않",
    r"살아\s*있을\s*이유",
    r"(?:그만|더는)\s*살고\s*싶",
    r"목숨을\s*끊",
    r"뛰어내리",
    r"해치고\s*싶",
    r"세상에서\s*사라지",
    r"없어져야\s*(?:다들|모두)",
    r"약\s*한꺼번에",
    r"눈을\s*안\s*떴으면",
    r"정리하고\s*떠나",
    r"인생을\s*(?:여기서\s*)?멈추",
    r"숨\s*쉬는\s*것도\s*그만",
    r"끝내\s*버릴",
    r"다\s*끝내고\s*싶",
    r"끝내고\s*싶",
    r"다\s*포기하고",
    r"버티기\s*힘들어서\s*죽",
    r"자살",
    r"자해",
    r"살기\s*싫",
    r"극단적\s*선택",
    r"유서",
    r"투신",
]
_CRISIS_RE = re.compile("|".join(_CRISIS_PATTERNS), re.IGNORECASE)

# 명령 규칙 목록: (의도, 기본 등급, 규칙 식별자, 정규식 패턴)
_COMMAND_RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
    # 1. SESSION_START (대기 상태)
    (
        INTENT_SESSION_START,
        GRADE_IMMEDIATE,
        "session_start",
        re.compile(
            r"^(?:(?:지금\s*)?시작(?:하자|할게요|해\s*(?:줘|주세요|요)|해|합시다)|안녕하세요|말동무\s*해\s*(?:줘|주세요)|(?:이야기|대화|얘기)(?:[을를])?\s*(?:하고\s*싶어요|좀\s*나누고\s*싶어요|시작해\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 2. STOP_SPEAKING (말하는 중 상태)
    (
        INTENT_STOP_SPEAKING,
        GRADE_IMMEDIATE,
        "stop_speaking",
        re.compile(
            r"^(?:(?:잠깐|잠시)(?:만|만요)?(?:\s*멈춰(?:\s*봐)?)?|멈춰(?:봐|요)?|그만(?:요)?|그만\s*말해(?:\s*(?:줘|주세요))?|말\s*(?:좀\s*)?멈춰(?:\s*(?:줘|주세요|봐))?|거기까지(?:만)?(?:\s*말해(?:\s*(?:줘|주세요)))?)$",
            re.IGNORECASE,
        ),
    ),
    # 3. REPEAT_BOT (진행 상태)
    (
        INTENT_REPEAT_BOT,
        GRADE_IMMEDIATE,
        "repeat_bot",
        re.compile(
            r"^(?:(?:방금\s*한\s*말\s*)?다시\s*(?:말해\s*(?:줘|주세요)|한\s*번\s*말씀해\s*(?:줘|주세요)|해\s*주세요)|뭐라고(?:\s*하셨어요|\s*했어|요)?|(?:잘\s*)?안\s*들렸(?:어요|습니다)(?:\s*다시\s*말해\s*(?:줘|주세요))?|못\s*들었어요|한\s*번만\s*더\s*(?:말해\s*(?:줘|주세요)|말씀해\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 4. REDO_USER (진행 상태)
    (
        INTENT_REDO_USER,
        GRADE_IMMEDIATE,
        "redo_user",
        re.compile(
            r"^(?:다시\s*(?:말할게요|말씀드릴게요)|(?:내가\s*잘못\s*말했어\s*)?다시\s*할게요|(?:방금\s*(?:거|한\s*말|말은|내\s*말)|이전\s*말)\s*(?:취소(?:해\s*(?:줘|주세요)|할게요)?|지워\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 5. RESET (진행 상태, 확인 필요)
    (
        INTENT_RESET,
        GRADE_CONFIRM,
        "reset",
        re.compile(
            r"^(?:처음부터\s*다시(?:\s*(?:시작하자|할게요))?|(?:대화\s*기록\s*지우고\s*)?처음부터\s*(?:해\s*(?:줘|주세요)|다시)|(?:다른\s*사람이에요|사람이\s*바뀌었어요)|새로\s*시작할래요|초기화(?:해\s*(?:줘|주세요)|할게요)?)$",
            re.IGNORECASE,
        ),
    ),
    # 6. SESSION_END (진행 상태, 확인 필요)
    (
        INTENT_SESSION_END,
        GRADE_CONFIRM,
        "session_end",
        re.compile(
            r"^(?:그만할게요|(?:이제\s*)?끝내자|대화(?:[을를])?\s*끝낼게요|이만\s*종료할게요|그만\s*끝내\s*(?:줘|주세요)|오늘\s*대화는\s*여기까지\s*할게요|(?:이제\s*)?그만\s*마치자|종료(?:해\s*(?:줘|주세요)|할게요)?)$",
            re.IGNORECASE,
        ),
    ),
    # 7. MODE_DEV (진행·대기 상태, 확인 필요)
    (
        INTENT_MODE_DEV,
        GRADE_CONFIRM,
        "mode_dev",
        re.compile(
            r"^(?:개발(?:자)?\s*(?:모드|화면)(?:(?:으?로)\s*(?:바꿔(?:\s*(?:줘|주세요))?|전환해\s*(?:줘|주세요)|변경해\s*(?:줘|주세요)|해\s*주세요)|(?:\s*켜\s*(?:줘|주세요)|(?:\s*보여\s*(?:줘|주세요))))?)$",
            re.IGNORECASE,
        ),
    ),
    # 8. MODE_DEMO (진행·대기 상태, 확인 필요)
    (
        INTENT_MODE_DEMO,
        GRADE_CONFIRM,
        "mode_demo",
        re.compile(
            r"^(?:(?:데모|시연)\s*(?:모드|화면)(?:(?:으?로)\s*(?:바꿔(?:\s*(?:줘|주세요))?|전환해\s*(?:줘|주세요)|변경해\s*(?:줘|주세요)|해\s*주세요)|(?:\s*보여\s*(?:줘|주세요)))?)$",
            re.IGNORECASE,
        ),
    ),
    # 9. CONFIRM_YES (확인 대기 상태)
    (
        INTENT_CONFIRM_YES,
        GRADE_IMMEDIATE,
        "confirm_yes",
        re.compile(
            r"^(?:네|응|그래요|예|맞아요|그렇게\s*해\s*주세요|네\s*맞습니다|맞습니다|그러자|그럴게요|좋아요)$",
            re.IGNORECASE,
        ),
    ),
    # 10. CONFIRM_NO (확인 대기 상태)
    (
        INTENT_CONFIRM_NO,
        GRADE_IMMEDIATE,
        "confirm_no",
        re.compile(
            r"^(?:아니요|아니|아뇨|싫어요|아닙니다|취소할래요|안\s*할래요|아니\s*괜찮아요|하지\s*마세요|아니요\s*취소해\s*주세요|됐어요\s*안\s*할게요)$",
            re.IGNORECASE,
        ),
    ),
    # 11. CAMERA_ON (진행·대기 상태, 동의 확인 필요)
    (
        INTENT_CAMERA_ON,
        GRADE_CONFIRM,
        "camera_on",
        re.compile(
            r"^(?:(?:카메라|표정\s*인식|얼굴\s*감정|표정\s*분석)(?:[을를이가은는])?\s*(?:켜\s*(?:줘|주세요)|작동시켜\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 12. CAMERA_OFF (진행·대기 상태)
    (
        INTENT_CAMERA_OFF,
        GRADE_IMMEDIATE,
        "camera_off",
        re.compile(
            r"^(?:(?:카메라|표정\s*인식|얼굴\s*감정|표정\s*분석)(?:[을를이가은는])?\s*(?:꺼\s*(?:줘|주세요)|중지해\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 13. WAKE_ON (진행·대기 상태)
    (
        INTENT_WAKE_ON,
        GRADE_IMMEDIATE,
        "wake_on",
        re.compile(
            r"^(?:(?:음성(?:으로)?\s*시작(?:\s*기능)?|대기\s*(?:청취|마이크))\s*(?:켜\s*(?:줘|주세요)|활성화해\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 14. WAKE_OFF (진행·대기 상태)
    (
        INTENT_WAKE_OFF,
        GRADE_IMMEDIATE,
        "wake_off",
        re.compile(
            r"^(?:(?:음성(?:으로)?\s*시작(?:\s*기능)?|대기\s*(?:청취|마이크))\s*(?:꺼\s*(?:줘|주세요)|비활성화해\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 15. DEV_INFO_ON (진행·대기 상태)
    (
        INTENT_DEV_INFO_ON,
        GRADE_IMMEDIATE,
        "dev_info_on",
        re.compile(
            r"^(?:(?:개발\s*(?:정보|패널)|지연\s*(?:시간\s*)?표)(?:[을를이가은는])?\s*(?:보여\s*(?:줘|주세요)|켜\s*(?:줘|주세요)|표시해\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 16. DEV_INFO_OFF (진행·대기 상태)
    (
        INTENT_DEV_INFO_OFF,
        GRADE_IMMEDIATE,
        "dev_info_off",
        re.compile(
            r"^(?:(?:개발\s*(?:정보|패널)|지연\s*(?:시간\s*)?표)(?:[을를이가은는])?\s*(?:숨겨\s*(?:줘|주세요)|꺼\s*(?:줘|주세요)|가려\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
    # 17. UI_BADGE_TOGGLE (진행·대기 상태)
    (
        INTENT_UI_BADGE_TOGGLE,
        GRADE_IMMEDIATE,
        "badge_toggle",
        re.compile(
            r"^(?:(?:카메라\s*(?:표시|배지)|연결\s*상태(?:\s*표시)?|(?:AI|에이아이)\s*고지)(?:[을를이가은는])?\s*(?:숨겨\s*(?:줘|주세요)|꺼\s*(?:줘|주세요)|보여\s*(?:줘|주세요)|켜\s*(?:줘|주세요)))$",
            re.IGNORECASE,
        ),
    ),
]


@dataclass(frozen=True)
class IntentResult:
    """의도 분류 결과."""

    intent: str
    grade: str
    rule: str
    confidence: float = 1.0
    payload: dict[str, Any] = field(default_factory=dict)
    classify_ms: float = 0.0

    @property
    def is_command(self) -> bool:
        """대화가 아닌 명령으로 처리해야 하는지 여부."""
        return self.intent not in (INTENT_CHAT, "TALK", "NONE", None) and self.grade != GRADE_DIALOG

    @property
    def is_irreversible_or_legal(self) -> bool:
        """되돌릴 수 없거나 법적 고지/동의가 필요한 명령인지 여부."""
        if self.intent in IRREVERSIBLE_OR_LEGAL_INTENTS:
            return True
        if self.intent == INTENT_UI_BADGE_TOGGLE and self.payload.get("target") == "show_ai_notice" and not self.payload.get("enabled", True):
            return True
        return False


def is_crisis(text: str) -> bool:
    """위기/자해/극단적 선택 표현 여부 검사."""
    if not text:
        return False
    return bool(_CRISIS_RE.search(text))


def normalize_for_intent(text: str) -> str:
    """태그와 문장부호를 제거하고 앞뒤 공백을 정규화한다."""
    if not text:
        return ""
    t = _TAG_RE.sub(" ", text)
    t = _PUNCT_RE.sub(" ", t)
    return " ".join(t.split())


def _build_payload_and_grade(intent: str, text: str, default_grade: str) -> tuple[dict[str, Any], str]:
    """의도별 payload 및 확인 등급(confirm/immediate) 계산."""
    payload: dict[str, Any] = {}
    grade = default_grade

    if intent == INTENT_CAMERA_ON:
        payload = {"target": "face", "enabled": True}
        grade = GRADE_CONFIRM
    elif intent == INTENT_CAMERA_OFF:
        payload = {"target": "face", "enabled": False}
        grade = GRADE_IMMEDIATE
    elif intent == INTENT_WAKE_ON:
        payload = {"target": "wake", "enabled": True}
        grade = GRADE_IMMEDIATE
    elif intent == INTENT_WAKE_OFF:
        payload = {"target": "wake", "enabled": False}
        grade = GRADE_IMMEDIATE
    elif intent == INTENT_DEV_INFO_ON:
        payload = {"target": "show_dev_info", "enabled": True}
        grade = GRADE_IMMEDIATE
    elif intent == INTENT_DEV_INFO_OFF:
        payload = {"target": "show_dev_info", "enabled": False}
        grade = GRADE_IMMEDIATE
    elif intent == INTENT_UI_BADGE_TOGGLE:
        t_lower = text.lower()
        if "카메라" in text:
            target = "show_camera_badge"
        elif "연결" in text:
            target = "show_conn_status"
        elif "ai" in t_lower or "에이아이" in text or "고지" in text:
            target = "show_ai_notice"
        else:
            target = "unknown"

        # 숨김/꺼짐 vs 보여줌/켜짐
        if any(kw in text for kw in ("숨겨", "꺼", "끄", "가려", "닫아", "감춰")):
            enabled = False
        else:
            enabled = True

        payload = {"target": target, "enabled": enabled}
        # AI 고지 숨김은 법적 경고 확인 질문 필수
        if target == "show_ai_notice" and not enabled:
            grade = GRADE_CONFIRM
        else:
            grade = GRADE_IMMEDIATE

    return payload, grade


def classify_intent(text: str, state: str | None = None) -> IntentResult:
    """텍스트 전사와 세션 상태로부터 음성 명령 의도를 분류한다.

    순서 (§3.3):
    1. 위기 표현 검사 -> CHAT
    2. 정규화 및 어절/문자 길이 상한 검사 -> CHAT
    3. 세션 상태별 허용 의도 필터링
    4. 명령 정규식 매칭
    5. 매칭 시 payload 및 등급 부여, 미매칭 시 CHAT(dialog)
    """
    t0 = time.perf_counter()

    # 1. 위기 표현 우선 검사
    if is_crisis(text):
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        return IntentResult(
            intent=INTENT_CHAT,
            grade=GRADE_DIALOG,
            rule="crisis_blocked",
            confidence=1.0,
            payload={},
            classify_ms=elapsed_ms,
        )

    norm_text = normalize_for_intent(text)
    if not norm_text:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        return IntentResult(
            intent=INTENT_CHAT,
            grade=GRADE_DIALOG,
            rule="empty",
            confidence=1.0,
            payload={},
            classify_ms=elapsed_ms,
        )

    words = norm_text.split()
    # 2. 어절 수 및 공백 제외 글자 수 상한 검사
    if len(words) > 6 or len(norm_text.replace(" ", "")) > 20:
        elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
        return IntentResult(
            intent=INTENT_CHAT,
            grade=GRADE_DIALOG,
            rule="max_length_exceeded",
            confidence=1.0,
            payload={},
            classify_ms=elapsed_ms,
        )

    allowed_intents = STATE_ALLOWED_INTENTS.get(state) if state else None

    # 3. 규칙 매칭
    for rule_intent, def_grade, rule_name, pattern in _COMMAND_RULES:
        if allowed_intents is not None and rule_intent not in allowed_intents:
            continue
        if pattern.search(norm_text):
            payload, grade = _build_payload_and_grade(rule_intent, norm_text, def_grade)
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
            return IntentResult(
                intent=rule_intent,
                grade=grade,
                rule=rule_name,
                confidence=1.0,
                payload=payload,
                classify_ms=elapsed_ms,
            )

    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return IntentResult(
        intent=INTENT_CHAT,
        grade=GRADE_DIALOG,
        rule="default",
        confidence=1.0,
        payload={},
        classify_ms=elapsed_ms,
    )
