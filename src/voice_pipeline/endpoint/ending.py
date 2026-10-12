"""Kiwi로 전사 끝의 어미·조사를 분류한다 (dev-guide §3.1).

분류:
- EF: 종결 어미 (-어요, -다, -까, -죠)
- EC_YO: 연결 어미 + 요 (-는데요, -고요, -거든요). Kiwi가 EC+JX("요")로 내든
  EF("거든요")로 내든 같은 범주로 모은다
- EC: 연결 어미·관형형 (-고, -서, -는데, -면, -는)
- JX_JK: 조사로 끝남 (-는, -를, -가)
- IC: 감탄사·머뭇거림·접속부사·부사 (음, 어, 그, 그러니까)
- NOUN: 명사·대명사로 끝남 ("혈압"). 조사가 빠진 말끝이라 할 말이 더 있을 수 있어
  규칙표에서 EC와 같은 대기를 쓴다
- NONE: 비었거나 분류할 수 없음 (Smart Turn 단독 규칙)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from voice_pipeline.stt.postprocess import _get_kiwi

_TRAIL = re.compile(r"[\s.,?!~…。？！]+$")
_PUNCT_END = re.compile(r"[.?!。？！…]\s*$")

# 연결 어미 + 요가 한 형태소로 나오는 경우 (EF로 분석됨)
_EC_YO_EF = re.compile(r"(거든요|[는은ᆫ]?데요|고요|면요|니까요|서요|는걸요|은걸요)$")
_FILLERS = {"음", "어", "그", "저", "아", "으음", "어어", "에", "뭐", "그게", "이제", "저기", "그니까"}

_NOUN_TAGS = {"NNG", "NNP", "NNB", "NP", "NR", "XSN", "SL", "SN", "SH"}
_JOSA_PREFIX = "J"  # JKS JKC JKG JKO JKB JKV JKQ JX JC
_IC_TAGS = {"IC", "MAJ", "MAG", "MM"}
_EC_TAGS = {"EC", "ETM"}


@dataclass(frozen=True)
class Ending:
    category: str  # EF | EC_YO | EC | JX_JK | IC | NOUN | NONE
    morph: str = ""  # 판단에 쓴 형태소 표면형 (예: "는데")
    tags: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # 끝 형태소 몇 개
    stt_punct: bool = False  # 전사 끝에 . ? ! 가 있었다 (분류에는 쓰지 않음)


def classify_ending(text: str) -> Ending:
    """전사 끝 어절의 형태소로 어미 범주를 정한다."""
    stt_punct = bool(_PUNCT_END.search(text or ""))
    body = _TRAIL.sub("", (text or "").strip())
    if not body:
        return Ending("NONE", stt_punct=stt_punct)
    kiwi = _get_kiwi()
    if kiwi is None:
        return Ending("NONE", stt_punct=stt_punct)
    toks = [(t.form, t.tag) for t in kiwi.tokenize(body) if not t.tag.startswith("S") or t.tag in ("SL", "SN", "SH")]
    if not toks:
        return Ending("NONE", stt_punct=stt_punct)
    tags = tuple(toks[-4:])
    cat, morph = _categorize(toks, body)
    return Ending(cat, morph, tags, stt_punct)


def _categorize(toks: list[tuple[str, str]], body: str) -> tuple[str, str]:
    last_eojeol = body.split()[-1]
    if last_eojeol in _FILLERS:
        return "IC", last_eojeol
    form, tag = toks[-1]
    if tag == "EF":
        if _EC_YO_EF.search(form):
            return "EC_YO", form
        if form == "요" and len(toks) >= 2 and toks[-2][1] in ("EC", "EF"):
            return ("EC_YO", toks[-2][0]) if toks[-2][1] == "EC" else ("EF", toks[-2][0] + form)
        return "EF", form
    if tag.startswith(_JOSA_PREFIX):
        # EC 뒤 "요"(JX)는 연결 어미 + 요
        if form == "요" and len(toks) >= 2 and toks[-2][1] == "EC":
            return "EC_YO", toks[-2][0]
        return "JX_JK", form
    if tag in _EC_TAGS:
        return "EC", form
    if tag in _IC_TAGS:
        return "IC", form
    if tag in _NOUN_TAGS:
        return "NOUN", form
    return "NONE", form
