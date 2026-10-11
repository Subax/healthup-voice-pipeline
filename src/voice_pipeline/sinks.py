"""오디오 출력 대상 (재생 주체).

운영에서는 Unreal이 재생한다 (개발 포인트 문서 §7.4, bridge/unreal.py).
Linux 개발 중에는 MemorySink(테스트)와 LocalSpeakerSink(로컬 스피커)로 같은 인터페이스를 시험한다.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Callable, Protocol

from .audio import AudioFrame, fade_out

# (turn_id, event, played_ms) — event: "started" | "progress" | "stopped" | "finished"
PlaybackListener = Callable[[int, str, float], None]


class AudioSink(Protocol):
    playback_listener: PlaybackListener | None

    async def begin(self, turn_id: int, sample_rate: int) -> None: ...
    async def write(self, frame: AudioFrame) -> None: ...
    async def end(self, turn_id: int, last_seq: int) -> None: ...
    async def interrupt(self, turn_id: int, fade_ms: int = 40) -> None: ...
    async def avatar_state(self, state: str, **fields) -> None: ...
    def played_ms(self, turn_id: int) -> float | None: ...


class MemorySink:
    """프레임과 메시지를 기록만 한다. 첫 프레임을 받은 시점을 재생 시작으로 본다."""

    def __init__(self) -> None:
        self.playback_listener: PlaybackListener | None = None
        self.frames: list[AudioFrame] = []
        self.events: list[tuple[float, str, dict]] = []
        self._written_ms: dict[int, float] = collections.defaultdict(float)
        self._interrupted: set[int] = set()

    def _log(self, kind: str, **data) -> None:
        self.events.append((time.monotonic(), kind, data))

    async def begin(self, turn_id: int, sample_rate: int) -> None:
        self._log("audio_begin", turn_id=turn_id, sample_rate=sample_rate)

    async def write(self, frame: AudioFrame) -> None:
        if frame.turn_id in self._interrupted:
            return
        first = frame.turn_id not in self._written_ms
        self.frames.append(frame)
        self._written_ms[frame.turn_id] += frame.duration_ms
        if first and self.playback_listener:
            self.playback_listener(frame.turn_id, "started", 0.0)

    async def end(self, turn_id: int, last_seq: int) -> None:
        self._log("audio_end", turn_id=turn_id, last_seq=last_seq)
        if self.playback_listener and turn_id not in self._interrupted:
            self.playback_listener(turn_id, "finished", self._written_ms.get(turn_id, 0.0))

    async def interrupt(self, turn_id: int, fade_ms: int = 40) -> None:
        self._interrupted.add(turn_id)
        self._log("interrupt", turn_id=turn_id, fade_ms=fade_ms)
        if self.playback_listener:
            self.playback_listener(turn_id, "stopped", self._written_ms.get(turn_id, 0.0))

    async def avatar_state(self, state: str, **fields) -> None:
        self._log("avatar_state", state=state, **fields)

    def played_ms(self, turn_id: int) -> float | None:
        return self._written_ms.get(turn_id)

    def frames_of(self, turn_id: int) -> list[AudioFrame]:
        return [f for f in self.frames if f.turn_id == turn_id]


class LocalSpeakerSink(MemorySink):
    """sounddevice로 로컬 스피커에 실시간 재생한다 (Linux 개발용).

    ⚠️ 오디오 장치가 없는 환경에서는 실행을 확인하지 않았다. `pip install sounddevice`가 필요하다.
    """

    def __init__(self, sample_rate: int = 24000, blocksize: int = 480, prebuffer_ms: int = 40):
        super().__init__()
        import sounddevice as sd  # 선택 의존성

        self.sample_rate = sample_rate
        self.prebuffer_bytes = int(sample_rate * prebuffer_ms / 1000) * 2
        self._q: collections.deque[tuple[int, bytes]] = collections.deque()
        self._lock = threading.Lock()
        self._played: dict[int, float] = collections.defaultdict(float)
        self._started: set[int] = set()
        self.underruns = 0
        self._stream = sd.RawOutputStream(
            samplerate=sample_rate, channels=1, dtype="int16",
            blocksize=blocksize, latency="low", callback=self._callback,
        )
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status) -> None:  # 오디오 스레드
        need = frames * 2
        out = bytearray()
        with self._lock:
            buffered = sum(len(b) for _, b in self._q)
            ready = buffered >= self.prebuffer_bytes or any(
                tid in self._started for tid, _ in self._q)
            while ready and len(out) < need and self._q:
                tid, b = self._q[0]
                take = b[: need - len(out)]
                out.extend(take)
                self._played[tid] += len(take) / 2 / self.sample_rate * 1000.0
                if tid not in self._started:
                    self._started.add(tid)
                    if self.playback_listener:
                        self.playback_listener(tid, "started", 0.0)
                if len(take) == len(b):
                    self._q.popleft()
                else:
                    self._q[0] = (tid, b[len(take):])
        if 0 < len(out) < need:
            self.underruns += 1
        outdata[:] = bytes(out) + b"\x00" * (need - len(out))

    async def write(self, frame: AudioFrame) -> None:
        if frame.turn_id in self._interrupted:
            return
        self.frames.append(frame)
        with self._lock:
            self._q.append((frame.turn_id, frame.pcm16))

    async def interrupt(self, turn_id: int, fade_ms: int = 40) -> None:
        with self._lock:
            rest = b"".join(b for tid, b in self._q if tid == turn_id)
            self._q = collections.deque((tid, b) for tid, b in self._q if tid != turn_id)
            tail = fade_out(rest, self.sample_rate, fade_ms)
            if tail:
                self._q.appendleft((turn_id, tail))
        await super().interrupt(turn_id, fade_ms)

    def played_ms(self, turn_id: int) -> float | None:
        return self._played.get(turn_id)

    def close(self) -> None:
        self._stream.stop()
        self._stream.close()
