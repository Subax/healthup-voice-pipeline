"""스트리밍 VAD 끝점 검출 (sherpa-onnx Silero VAD, CPU).

사용한 API: sherpa_onnx.VoiceActivityDetector
- is_speech_detected(): 음성이 감지되는 동안 True. False→True 전환 순간을 SpeechStart로 쓴다
  (barge-in용, 구간이 끝나기를 기다리지 않는다).
- empty()/front/pop(): min_silence_ms 무음 뒤 구간이 끝나면 큐에 쌓인다 → SpeechEnd.
시각은 지금까지 feed한 오디오 길이(ms) 기준이다.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000


@dataclass
class SpeechStart:
    t_ms: float


@dataclass
class SpeechEnd:
    t_ms: float
    pcm16: bytes


VADEvent = SpeechStart | SpeechEnd


class VADSegmenter:
    def __init__(
        self,
        model_path: str = "models/silero_vad.onnx",
        threshold: float = 0.5,
        min_speech_ms: int = 250,
        min_silence_ms: int = 500,
        max_speech_s: float = 20.0,
        pre_roll_ms: int = 300,
        window: int = 512,
        num_threads: int = 1,
    ):
        import sherpa_onnx

        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = model_path
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_speech_duration = min_speech_ms / 1000.0
        cfg.silero_vad.min_silence_duration = min_silence_ms / 1000.0
        cfg.silero_vad.max_speech_duration = max_speech_s
        cfg.silero_vad.window_size = window
        cfg.sample_rate = SAMPLE_RATE
        cfg.num_threads = num_threads
        self._cfg = cfg
        self._sherpa = sherpa_onnx
        self.window = window
        self.pre_roll = SAMPLE_RATE * pre_roll_ms // 1000
        self._vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=max_speech_s + 10)
        self.reset()

    def reset(self) -> None:
        self._vad.reset()
        self._tail = b""  # 윈도 크기에 못 미친 나머지 바이트
        self._fed = 0  # feed한 총 샘플 수
        self._buf = np.zeros(0, dtype=np.int16)  # 최근 오디오 (pre-roll용)
        self._buf_start = 0  # _buf[0]의 절대 샘플 위치
        self._speaking = False

    def _ms(self, samples: int) -> float:
        return samples * 1000.0 / SAMPLE_RATE

    def feed(self, pcm16: bytes) -> list[VADEvent]:
        """PCM16 16 kHz 조각을 넣고 발생한 이벤트를 돌려준다."""
        events: list[VADEvent] = []
        data = self._tail + pcm16
        n = (len(data) // 2 // self.window) * self.window
        self._tail = data[n * 2 :]
        if n == 0:
            return events
        pcm = np.frombuffer(data[: n * 2], dtype=np.int16)
        f32 = pcm.astype(np.float32) / 32768.0
        # 윈도마다 넣어야 SpeechStart 시각이 윈도 단위로 정확하다
        for i in range(0, n, self.window):
            self._vad.accept_waveform(f32[i : i + self.window])
            self._fed += self.window
            det = self._vad.is_speech_detected()
            if det and not self._speaking:
                self._speaking = True
                events.append(SpeechStart(self._ms(self._fed)))
            while not self._vad.empty():
                seg = self._vad.front
                start, samples = seg.start, np.asarray(seg.samples, dtype=np.float32)
                self._vad.pop()
                events.append(self._end_event(start, len(samples), pcm, i))
                self._speaking = False
        self._append(pcm)
        return events

    def _append(self, pcm: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, pcm])
        # 20초 구간 + 여유만 남기고 버린다
        keep = SAMPLE_RATE * 32
        if len(self._buf) > keep:
            cut = len(self._buf) - keep
            self._buf = self._buf[cut:]
            self._buf_start += cut

    def _end_event(self, start: int, length: int, cur: np.ndarray, cur_i: int) -> SpeechEnd:
        # 현재 조각 중 cur_i+window까지를 버퍼에 합쳐 놓고 자른다
        full = np.concatenate([self._buf, cur[: cur_i + self.window]])
        full_start = self._buf_start
        a = max(start - self.pre_roll, full_start)
        b = min(start + length, full_start + len(full))
        seg = full[a - full_start : b - full_start]
        return SpeechEnd(self._ms(self._fed), seg.tobytes())
