"""모의 상담 LLM (개발 포인트 문서 §8.4).

TTFT, 토큰 속도, risk_update 시점을 설정값으로 바꾼다.
파인튜닝 모델이 오기 전에 파이프라인 전체를 시험하는 데 쓴다.
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator, Callable

from .events import Done, LLMEvent, Meta, RiskUpdate, Token

DEFAULT_REPLY = (
    "네, 측정 결과를 같이 볼게요. 오늘 혈압은 128/82mmHg로 지난주보다 조금 높아요. "
    "체온은 36.5℃로 정상 범위입니다. 요즘 잠은 잘 주무시나요?"
)


def tokenize_ko(text: str) -> list[str]:
    """한국어 BPE를 흉내 낸 단순 분할: 공백 포함 1~2글자 단위."""
    return re.findall(r"\s?\S{1,2}", text)


class MockLLM:
    def __init__(
        self,
        reply: str | Callable[[list[dict]], str] = DEFAULT_REPLY,
        ttft_ms: float = 400.0,
        tokens_per_s: float = 40.0,
        risk_at_token: int | None = None,
        risk_grade: str = "119",
        meta: Meta | None = None,
    ):
        self.reply = reply
        self.ttft_ms = ttft_ms
        self.tokens_per_s = tokens_per_s
        self.risk_at_token = risk_at_token
        self.risk_grade = risk_grade
        self.meta = meta or Meta(risk_grade="일반", emotion="reassuring", gesture="small_nod")
        self.requests: list[tuple[int, list[dict]]] = []

    async def stream(self, messages: list[dict], turn_id: int) -> AsyncIterator[LLMEvent]:
        self.requests.append((turn_id, [dict(m) for m in messages]))
        text = self.reply(messages) if callable(self.reply) else self.reply
        yield self.meta
        await asyncio.sleep(self.ttft_ms / 1000.0)
        for i, tok in enumerate(tokenize_ko(text)):
            if i:
                await asyncio.sleep(1.0 / self.tokens_per_s)
            if self.risk_at_token is not None and i == self.risk_at_token:
                yield RiskUpdate(self.risk_grade)
                return
            yield Token(tok)
        yield Done(risk_grade=self.meta.risk_grade)
