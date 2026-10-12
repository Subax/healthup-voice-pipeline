"""모델 없이 데모·테스트를 돌리는 가짜 STT·VAD (에너지 임계값)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FRAME_BYTES = 640  # 16 kHz, 20 ms, PCM16


@dataclass
class SpeechStart:
    t_ms: float


@dataclass
class SpeechEnd:
    t_ms: float
    pcm16: bytes


@dataclass
class MockSTTResult:
    text: str
    lang: str = "ko"
    emotion: str | None = None
    event: str | None = None
    elapsed_ms: float = 0.0


class MockSTT:
    sample_rate = 16000

    def __init__(self, text: str = "안녕하세요, 오늘 혈압 결과 알려주세요.", emotion: str | None = None):
        self.text, self.emotion = text, emotion

    async def warmup(self) -> None:
        return None

    async def transcribe(self, pcm16: bytes) -> MockSTTResult:
        return MockSTTResult(self.text, emotion=self.emotion, elapsed_ms=1.0)


class EnergyVAD:
    """RMS가 threshold를 넘는 프레임이 이어지면 시작, 조용한 프레임이 이어지면 끝."""

    def __init__(self, threshold: float = 1000.0, min_speech_frames: int = 2, min_silence_ms: int = 300):
        self.threshold, self.min_speech = threshold, min_speech_frames
        self.sil_frames = max(1, min_silence_ms // 20)
        self.reset()

    def reset(self) -> None:
        self._buf = bytearray()
        self._pending: list[bytes] = []
        self._seg = bytearray()
        self._speaking, self._sil, self._n = False, 0, 0

    def feed(self, pcm16: bytes) -> list:
        self._buf += pcm16
        events: list = []
        while len(self._buf) >= FRAME_BYTES:
            f = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            self._n += 1
            t = self._n * 20.0
            x = np.frombuffer(f, dtype=np.int16).astype(np.float32)
            loud = float(np.sqrt(np.mean(x * x))) >= self.threshold
            if not self._speaking:
                if loud:
                    self._pending.append(f)
                    if len(self._pending) >= self.min_speech:
                        self._speaking, self._sil = True, 0
                        self._seg = bytearray(b"".join(self._pending))
                        self._pending.clear()
                        events.append(SpeechStart(t))
                else:
                    self._pending.clear()
            else:
                self._seg += f
                self._sil = 0 if loud else self._sil + 1
                if self._sil >= self.sil_frames:
                    events.append(SpeechEnd(t, bytes(self._seg)))
                    self._speaking, self._seg = False, bytearray()
        return events
