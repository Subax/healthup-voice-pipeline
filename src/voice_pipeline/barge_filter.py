"""barge-in 맞장구 판별 (dev-guide §6.2).

말하는 중에 사용자의 짧은 "네", "음"은 끼어들기가 아니다. 이런 발화로 답을 끊지 않는다.
판단 순서: ① 소리가 짧다 ② 전사가 맞장구 말뭉치뿐이거나 비어 있다 (기침·웃음·알아듣지 못한 소리).
말뭉치 밖의 말이 하나라도 있으면 끼어들기다. 기준값은 가설이다. 녹음으로 조정한다.
"""

from __future__ import annotations

import re

BACKCHANNEL_MAX_MS = 900   # 이보다 긴 발화는 맞장구가 아니다 (전사를 보지 않고 끼어들기로 본다)
PROBE_WINDOW_MS = 600      # 사용자 소리가 시작된 뒤 이 시간 안에 안 끝나면 맞장구가 아니다. 끼어들기로 보고 끊는다
MAX_WORDS = 4              # 어절 수 상한
MAX_CHARS = 12             # 공백 뺀 글자 수 상한

# 맞장구 어절. STT 표기 변형(음/으음/흠, 네/넵/예)을 함께 둔다. 끝의 반복("네네")은 허용한다.
_WORDS = (
    "네", "넵", "예", "응", "으응", "어", "으음", "음", "흠", "아", "오", "아하", "아휴", "와",
    "그래", "그래요", "그렇죠", "그렇군요", "그렇구나", "그렇네요", "그렇습니다", "그럼요", "맞아요", "맞네요", "맞아",
    "알겠어요", "알겠습니다", "알았어요", "알았어", "좋아요", "좋네요", "글쎄요", "하하", "허", "아이고",
)
_WORD_RE = re.compile(r"^(?:%s)+$" % "|".join(sorted(map(re.escape, _WORDS), key=len, reverse=True)))
_TAG_RE = re.compile(r"<\|[^|]*\|>")                 # <|Cough|> 같은 SenseVoice 태그 조각
_PUNCT_RE = re.compile(r"[.,!?~…·、，。？！\-—'\"“”‘’()\[\]]+")


def normalize(text: str) -> list[str]:
    """태그·문장부호를 뗀 어절 목록."""
    t = _PUNCT_RE.sub(" ", _TAG_RE.sub(" ", text or ""))
    return t.split()


def is_backchannel(text: str, duration_ms: float) -> bool:
    """이 발화가 맞장구(또는 잡음)라 답을 끊지 않아도 되는가. duration_ms는 사용자가 말한 길이."""
    if duration_ms > BACKCHANNEL_MAX_MS:
        return False
    words = normalize(text)
    if not words:  # 전사가 비었다: 기침·웃음·숨소리거나 알아듣지 못한 짧은 소리. 답을 끊지 않는다
        return True
    if len(words) > MAX_WORDS or sum(len(w) for w in words) > MAX_CHARS:
        return False
    return all(_WORD_RE.match(w) for w in words)
