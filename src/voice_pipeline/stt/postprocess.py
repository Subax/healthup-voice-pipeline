"""STT 후처리: 한국어 외 문자 제거, Kiwi 띄어쓰기 교정."""

from __future__ import annotations

import logging
import re
import threading

log = logging.getLogger(__name__)

# 한자(CJK 통합·확장 A·호환), 히라가나, 가타카나
_FOREIGN = re.compile(r"[一-鿿㐀-䶿豈-﫿぀-ゟ゠-ヿ]")
_FULLWIDTH = str.maketrans({"。": ".", "，": ",", "？": "?", "！": "!"})
_WS = re.compile(r"\s+")

_kiwi = None
_kiwi_failed = False
_lock = threading.Lock()


def korean_only(text: str) -> str:
    """한자·가나 제거, 전각 문장부호를 ASCII로, 공백 정리. 한글·숫자·라틴은 둔다."""
    text = text.translate(_FULLWIDTH)
    text = _FOREIGN.sub("", text)
    text = re.sub(r"\s+([.,?!])", r"\1", text)  # 제거 후 남은 문장부호 앞 공백
    text = re.sub(r"^[.,?!\s]+", "", text)  # 한자만 있던 자리의 문장부호
    return _WS.sub(" ", text).strip()


def _get_kiwi():
    """Kiwi 인스턴스를 처음 쓸 때 한 번만 만든다. 없으면 None."""
    global _kiwi, _kiwi_failed
    if _kiwi is not None or _kiwi_failed:
        return _kiwi
    with _lock:
        if _kiwi is None and not _kiwi_failed:
            try:
                from kiwipiepy import Kiwi

                _kiwi = Kiwi()
            except Exception as e:  # ImportError 포함
                _kiwi_failed = True
                log.warning("kiwipiepy 사용 불가, 띄어쓰기 교정 생략: %s", e)
    return _kiwi


def fix_spacing(text: str) -> str:
    """Kiwi로 띄어쓰기를 다시 잡는다 ('조 금만 생각 을' → '조금만 생각을')."""
    if not text.strip():
        return text
    kiwi = _get_kiwi()
    if kiwi is None:
        return text
    return kiwi.space(text, reset_whitespace=True)
