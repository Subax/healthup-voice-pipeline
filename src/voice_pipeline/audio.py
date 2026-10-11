"""오디오 프레임과 20ms 분할 (개발 포인트 문서 §4.5, §5.4)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class AudioFrame:
    turn_id: int
    seq: int          # 턴 안에서 0부터 증가
    pcm16: bytes      # 모노 16-bit
    sample_rate: int
    is_last: bool = False

    @property
    def duration_ms(self) -> float:
        return len(self.pcm16) / 2 / self.sample_rate * 1000.0


class Framer:
    """엔진이 낸 임의 길이 PCM 조각을 turn_id·seq가 붙은 고정 길이 프레임으로 자른다."""

    def __init__(self, turn_id: int, sample_rate: int, frame_ms: int = 20):
        self.turn_id = turn_id
        self.sample_rate = sample_rate
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self._buf = bytearray()
        self.seq = 0

    def push(self, pcm16: bytes) -> list[AudioFrame]:
        self._buf.extend(pcm16)
        out = []
        while len(self._buf) >= self.frame_bytes:
            out.append(self._make(bytes(self._buf[: self.frame_bytes])))
            del self._buf[: self.frame_bytes]
        return out

    def flush(self) -> list[AudioFrame]:
        """남은 조각을 마지막 프레임으로 낸다. 남은 것이 없으면 빈 마지막 프레임을 낸다."""
        frame = self._make(bytes(self._buf), is_last=True)
        self._buf.clear()
        return [frame]

    def _make(self, pcm: bytes, is_last: bool = False) -> AudioFrame:
        f = AudioFrame(self.turn_id, self.seq, pcm, self.sample_rate, is_last)
        self.seq += 1
        return f


def fade_out(pcm16: bytes, sample_rate: int, fade_ms: int = 40) -> bytes:
    """앞부분 fade_ms만 남기고 선형으로 줄인다. 정지 클릭음을 막는다 (§6.3)."""
    x = np.frombuffer(pcm16, dtype=np.int16)
    n = min(len(x), int(sample_rate * fade_ms / 1000))
    if n == 0:
        return b""
    ramp = np.linspace(1.0, 0.0, n, dtype=np.float32)
    return (x[:n].astype(np.float32) * ramp).astype(np.int16).tobytes()
