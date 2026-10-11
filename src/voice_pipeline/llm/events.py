"""상담 LLM 스트리밍 이벤트 (설계서 §32: meta → token → done, risk_update).

⚠️ 임시 스키마다. 정본(정신상담 LLM 문서 §7)의 필드 이름은 확인하지 않았다.
정본을 받으면 이 파일과 sse.py의 파싱만 고친다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol, Union

RISK_GRADES = ("일반", "주의", "고위험", "119")


@dataclass
class Meta:
    risk_grade: str = "일반"
    emotion: str | None = None      # 제안: 감정·제스처는 토큰 앞 meta에 둔다 (§10 결정 사항 4)
    gesture: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Token:
    text: str


@dataclass
class RiskUpdate:
    risk_grade: str


@dataclass
class Done:
    risk_grade: str = "일반"


LLMEvent = Union[Meta, Token, RiskUpdate, Done]


class LLMClient(Protocol):
    def stream(self, messages: list[dict], turn_id: int) -> AsyncIterator[LLMEvent]:
        """이벤트를 차례로 낸다. 호출 쪽 태스크가 취소되면 요청도 끊는다."""
