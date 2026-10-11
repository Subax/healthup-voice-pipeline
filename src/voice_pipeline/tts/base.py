"""TTS 어댑터 인터페이스 (개발 포인트 문서 §4.5).

엔진은 PCM16 모노 조각만 낸다. turn_id·seq·20ms 분할은 파이프라인이 붙인다.
엔진을 바꿔도 청커·큐·재생 코드는 바꾸지 않는다.

구현 예정 (⏭️): OmniVoiceTTS, CosyVoiceTTS, AzureTTS, ClovaTTS, QwenTTS
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable


@runtime_checkable
class TTSEngine(Protocol):
    name: str
    sample_rate: int

    async def warmup(self) -> None:
        """모델 로드, 연결 미리 열기. 첫 턴 전에 한 번 부른다."""

    def stream(self, text: str) -> AsyncIterator[bytes]:
        """text를 합성해 PCM16 모노 조각을 차례로 낸다. 길이는 자유."""

    async def cancel(self) -> None:
        """진행 중 합성을 중단한다. 지원하지 않는 엔진은 아무것도 하지 않는다."""
