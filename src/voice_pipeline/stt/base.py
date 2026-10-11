"""STT 어댑터 인터페이스.

VAD가 끊어 준 발화 구간(PCM16 16 kHz 모노) 하나를 받아 텍스트로 바꾼다.
엔진을 바꿔도 VAD·파이프라인 코드는 바꾸지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class STTResult:
    text: str
    lang: str | None = None
    emotion: str | None = None
    event: str | None = None
    elapsed_ms: float = 0.0


@runtime_checkable
class STTEngine(Protocol):
    name: str
    sample_rate: int  # 16000

    async def warmup(self) -> None:
        """모델 로드·더미 추론. 첫 턴 전에 한 번 부른다."""

    async def transcribe(self, pcm16: bytes) -> STTResult:
        """발화 구간 하나를 텍스트로 바꾼다. 이벤트 루프를 막지 않는다."""
