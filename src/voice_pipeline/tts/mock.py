"""모의 TTS. 첫 오디오 지연과 RTF를 설정값으로 흉내 낸다 (테스트·파이프라인 지연 시뮬레이션용)."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import numpy as np


class MockTTS:
    def __init__(
        self,
        first_audio_ms: float = 80.0,
        rtf: float = 0.1,
        ms_per_char: float = 130.0,
        sample_rate: int = 24000,
        piece_ms: int = 100,
        name: str = "mock",
    ):
        self.name = name
        self.first_audio_ms = first_audio_ms
        self.rtf = rtf
        self.ms_per_char = ms_per_char
        self.sample_rate = sample_rate
        self.piece_ms = piece_ms

    async def warmup(self) -> None:
        return None

    async def cancel(self) -> None:
        return None

    def audio_ms(self, text: str) -> float:
        return max(1, len(text.replace(" ", ""))) * self.ms_per_char

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        total_ms = self.audio_ms(text)
        await asyncio.sleep(self.first_audio_ms / 1000.0)
        n_total = int(self.sample_rate * total_ms / 1000.0)
        n_piece = int(self.sample_rate * self.piece_ms / 1000.0)
        t = 0
        first = True
        while t < n_total:
            n = min(n_piece, n_total - t)
            if not first:
                await asyncio.sleep(n / self.sample_rate * self.rtf)
            first = False
            idx = np.arange(t, t + n)
            wave = 0.2 * np.sin(2 * np.pi * 220.0 * idx / self.sample_rate)
            yield (wave * 32767).astype(np.int16).tobytes()
            t += n
