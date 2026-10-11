"""TTS 입력용 텍스트 정규화 (개발 포인트 문서 §5.3).

기호와 단위는 항상 말로 바꾼다. 읽기 규칙(예: 혈압 "120에 80")은
조교·상담 자문과 맞춰 수정한다.

마지막 단계(_spell_numbers)가 아라비아 숫자와 영문 약어를 한글로 풀어 쓴다.
CosyVoice3가 숫자·약어를 잘못 읽기 때문이다(왕복 CER 0.27~0.54).
- 기본은 한자어 수(128 → 백이십팔). 소수는 "삼십육 점 오"
- 1~99 + 단위(개 번 명 살 시 시간 잔 병 가지 마리 달)는 고유어 수(3번 → 세 번)
- 전화번호·5자리 이상 숫자·112/119/1393은 숫자별로 읽는다(119 → 일일구)
- 수와 단위 사이에 공백을 넣는다(고유어 단위, 회 분 초 일 월 년 등). 읽기에 영향이 없어 일관성을 택했다
- 영문 약어 사전에 없는 영단어는 그대로 둔다
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
    (re.compile(r"(?<=\d)\s*kg(?![A-Za-z])", re.I), " 킬로그램"),
    (re.compile(r"(?<=\d)\s*mL(?![A-Za-z])"), " 밀리리터"),
    (re.compile(r"(?<=\d)\s*cm(?![A-Za-z])", re.I), " 센티미터"),
    (re.compile(r"(?<=\d)\s*bpm(?![A-Za-z])", re.I), " 회"),
    (re.compile(r"(?<=\d)\s*kcal(?![A-Za-z])", re.I), " 킬로칼로리"),
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


# ---- 숫자·약어 한글화 ----

_ACRONYMS = {
    "HbA1c": "당화혈색소", "BMI": "비엠아이", "LDL": "엘디엘", "HDL": "에이치디엘",
    "BPM": "비피엠", "AI": "에이아이", "TV": "티비",
}
_ACRONYM = re.compile(
    r"(?<![A-Za-z])(HbA1c|BMI|LDL|HDL|AI|TV|bpm|BPM)(?![A-Za-z])"
)

_DIGIT = "일이삼사오육칠팔구"
_NATIVE_ONES = ["", "한", "두", "세", "네", "다섯", "여섯", "일곱", "여덟", "아홉"]
_NATIVE_TENS = ["", "열", "스물", "서른", "마흔", "쉰", "예순", "일흔", "여든", "아흔"]
_NATIVE_UNITS = "시간|시|개(?!월)|번|명|살|잔|병|가지|마리|달"
_SINO_UNITS = "개월|걸음|회|분|초|일|월|년|주|층|세|호|원|대|단계"
_EMERGENCY = "112|119|1393"
# 숫자 뒤에 이 단위가 오면 5자리 이상이라도 수량으로 읽는다(10000걸음)
_QUANTITY = "원|걸음|보|명|개|번|회|칼로리|킬로칼로리"
# 112 mg/dL 같은 측정값과 구분: 뒤에 단위가 오면 긴급번호가 아니다
_NOT_EMERGENCY = r"(?!\.\d|,\d|에 \d|\s*(?:밀리|퍼센트|킬로|센티|회|칼로리|점))"


def _sino(n: int) -> str:
    """정수를 한자어 수로. 10**16 이상은 숫자별로 읽는다."""
    if n == 0:
        return "영"
    if n >= 10**16:
        return _digits(str(n))
    out = []
    groups = []
    while n:
        groups.append(n % 10000)
        n //= 10000
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g == 0:
            continue
        part = ""
        for pos, unit in enumerate(["천", "백", "십", ""]):
            d = (g // 10 ** (3 - pos)) % 10
            if d == 0:
                continue
            part += ("" if d == 1 and unit else _DIGIT[d - 1]) + unit
        if g == 1 and i == 1:  # 일만 → 만
            part = ""
        out.append(part + ["", "만", "억", "조"][i])
    return "".join(out)


def _native(n: int) -> str:
    """1~99를 단위 앞 고유어 수로 (20 → 스무, 3 → 세)."""
    if n == 20:
        return "스무"
    t, o = divmod(n, 10)
    return _NATIVE_TENS[t] + _NATIVE_ONES[o]


def _digits(s: str, zero: str = "공") -> str:
    return "".join(zero if c == "0" else _DIGIT[int(c) - 1] for c in s)


def _number_words(tok: str) -> str:
    tok = tok.replace(",", "")
    whole, _, frac = tok.partition(".")
    words = _sino(int(whole))
    if frac:
        words += " 점 " + _digits(frac, "영")
    return words


_DATE = re.compile(r"(?<![\d-])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d-])")
_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_PHONE = re.compile(r"(?<![\d-])\d+(?:-\d+)+(?![\d-])")
_EMERGENCY_RE = re.compile(rf"(?<![\d.,])({_EMERGENCY})(?!\d){_NOT_EMERGENCY}")
_LONG = re.compile(rf"(?<![\d.,])\d{{5,}}(?!\d|\.\d)(?!\s?(?:{_QUANTITY}))")
_NATIVE_COUNT = re.compile(rf"(?<![\d.])(\d{{1,2}})(?!\d) ?({_NATIVE_UNITS})")
_NUMBER = re.compile(
    rf"(?<![\d.])(\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?|\d+(?:\.\d+)?)(?:[ ]?({_SINO_UNITS}))?"
)


def _native_sub(m: re.Match[str]) -> str:
    n, unit = int(m.group(1)), m.group(2)
    if n == 0:
        return _sino(0) + " " + unit
    if n == 1 and unit == "번" and m.string.startswith("째", m.end()):
        return "첫 번"
    return f"{_native(n)} {unit}"


def _number_sub(m: re.Match[str]) -> str:
    tok, unit = m.group(1), m.group(2)
    if unit == "월" and tok in ("6", "10"):  # 유월·시월
        return {"6": "유월", "10": "시월"}[tok]
    words = _number_words(tok)
    return f"{words} {unit}" if unit else words


def _spell_numbers(text: str) -> str:
    text = _ACRONYM.sub(lambda m: _ACRONYMS.get(m[1]) or _ACRONYMS[m[1].upper()], text)
    text = _DATE.sub(lambda m: f"{m[1]}년 {m[2]}월 {m[3]}일", text)
    text = _TIME.sub(lambda m: f"{int(m[1])}시" + ("" if m[2] == "00" else f" {m[2]}분"), text)
    text = _PHONE.sub(lambda m: " ".join(_digits(g) for g in m.group().split("-")), text)
    text = _EMERGENCY_RE.sub(lambda m: _digits(m.group()), text)
    text = _LONG.sub(lambda m: _digits(m.group()), text)
    text = _NATIVE_COUNT.sub(_native_sub, text)
    return _NUMBER.sub(_number_sub, text)


def normalize_for_tts(text: str) -> str:
    text = _EMOJI.sub("", text)
    for pattern, repl in _RULES:
        text = pattern.sub(repl, text)
    return _spell_numbers(text).strip()
