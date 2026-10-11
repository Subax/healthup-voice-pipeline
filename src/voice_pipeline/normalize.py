"""TTS 입력용 텍스트 정규화 (개발 포인트 문서 §5.3).

기호와 단위는 항상 말로 바꾼다. 숫자 읽기는 엔진별로 시험한 뒤 정한다.
읽기 규칙(예: 혈압 "120에 80")은 조교·상담 자문과 맞춰 수정한다.
"""

from __future__ import annotations

import re

_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF️‍]+"
)

_RULES: list[tuple[re.Pattern[str], str]] = [
    # 마크다운
    (re.compile(r"(\*\*|__|`)"), ""),
    (re.compile(r"^\s*(#{1,6}|[-*•])\s+", re.M), ""),
    # 혈압 120/80mmHg → 120에 80
    (re.compile(r"(\d{2,3})\s*/\s*(\d{2,3})\s*(mmHg)?", re.I), r"\1에 \2"),
    (re.compile(r"\s*mmHg", re.I), " 밀리미터 수은주"),
    # 단위
    (re.compile(r"\s*mg\s*/\s*dL", re.I), " 밀리그램 퍼 데시리터"),
    (re.compile(r"\s*mmol\s*/\s*L", re.I), " 밀리몰 퍼 리터"),
    (re.compile(r"(?<=\d)\s*kg\b", re.I), " 킬로그램"),
    (re.compile(r"(?<=\d)\s*cm\b", re.I), " 센티미터"),
    (re.compile(r"(?<=\d)\s*bpm\b", re.I), " 회"),
    # 기호
    (re.compile(r"\s*(℃|°C)"), "도"),
    (re.compile(r"\s*%"), " 퍼센트"),
    (re.compile(r"(?<=\d)\s*[~～]\s*(?=\d)"), "에서 "),
    # 괄호: CLOVA는 괄호 안 텍스트를 읽지 않는다 → 쉼표로 바꾼다
    (re.compile(r"\s*[(（]\s*"), ", "),
    (re.compile(r"\s*[)）]\s*"), ", "),
    (re.compile(r",\s*([.?!])"), r"\1"),
    # 공백 정리
    (re.compile(r"[ \t]{2,}"), " "),
]


def normalize_for_tts(text: str) -> str:
    text = _EMOJI.sub("", text)
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return text.strip()
