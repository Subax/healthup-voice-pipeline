"""미리 합성한 맞장구 (네, 음 ...). 턴이 확정되면 LLM/TTS 답보다 먼저 바로 재생한다.

- 시작할 때 목소리별로 한 번 합성해 두고, 턴마다 하나를 고른다.
- 중립적인 문구만 쓴다. 공감·판단이 들어간 말은 LLM 답에 맡긴다.
"""

from __future__ import annotations

import logging
import random
import wave
from pathlib import Path

import numpy as np

from .tts.base import TTSEngine

log = logging.getLogger("voice_pipeline.backchannel")

DEFAULT_PHRASES = ("네.", "아, 네.", "네, 네.")

SILENCE_THRESHOLD = 0.02   # 최대 진폭 대비 비율. 이보다 작으면 무음으로 본다
KEEP_PAD_MS = 60           # 앞뒤에 남기는 여유
FADE_IN_MS = 15            # 시작 클릭 방지
FADE_OUT_MS = 30           # 끝 클릭 방지
MIN_MS, MAX_MS = 250, 1200  # 이 밖의 길이는 맞장구로 쓰지 않는다 (너무 짧으면 클릭음, 길면 답변)


def trim_silence(pcm: bytes, sample_rate: int, *, threshold: float = SILENCE_THRESHOLD,
                 pad_ms: int = KEEP_PAD_MS) -> bytes:
    """앞뒤 무음을 자른다. 전부 무음이면 빈 bytes. 결과 길이는 항상 짝수."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16)
    loud = np.flatnonzero(np.abs(x.astype(np.int32)) > threshold * 32767)
    if loud.size == 0:
        return b""
    pad = sample_rate * pad_ms // 1000
    lo, hi = max(0, int(loud[0]) - pad), min(len(x), int(loud[-1]) + 1 + pad)
    return x[lo:hi].tobytes()


def apply_fades(pcm: bytes, sample_rate: int, *, fade_in_ms: int = FADE_IN_MS,
                fade_out_ms: int = FADE_OUT_MS) -> bytes:
    """앞에 선형 페이드인, 뒤에 선형 페이드아웃을 건다 (int16)."""
    x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    n_in, n_out = min(len(x), sample_rate * fade_in_ms // 1000), min(len(x), sample_rate * fade_out_ms // 1000)
    if n_in:
        x[:n_in] *= np.arange(n_in, dtype=np.float32) / n_in
    if n_out:
        x[len(x) - n_out:] *= np.arange(n_out, 0, -1, dtype=np.float32) / n_out
    return np.round(x).astype(np.int16).tobytes()


class BackchannelBank:
    """목소리 키별로 (문구, PCM16) 목록을 들고 있다가 하나씩 골라 준다."""

    def __init__(self, phrases: tuple[str, ...] = DEFAULT_PHRASES, seed: int | None = None):
        self.phrases = tuple(phrases)
        self.sample_rate = 24000
        self._items: dict[str | None, list[tuple[str, bytes]]] = {}
        self._last: dict[str | None, str] = {}
        self._rng = random.Random(seed)

    async def prepare(self, tts: TTSEngine, voices: list[str | None]) -> None:
        """voices 각각으로 모든 문구를 합성해 둔다. 실패한 문구는 로그만 남기고 건너뛴다."""
        self.sample_rate = tts.sample_rate
        has_voice = hasattr(tts, "voice")
        original = getattr(tts, "voice", None)
        try:
            for voice in voices:
                if has_voice and voice is not None:
                    tts.voice = voice
                key = voice if has_voice else None
                items: list[tuple[str, bytes]] = []
                for text in self.phrases:
                    try:
                        pcm = b"".join([c async for c in tts.stream(text)])
                        pcm = apply_fades(trim_silence(pcm, tts.sample_rate), tts.sample_rate)
                    except Exception:
                        log.exception("맞장구 합성 실패: voice=%s text=%r", voice, text)
                        continue
                    ms = len(pcm) // 2 * 1000 / tts.sample_rate
                    if not (MIN_MS <= ms <= MAX_MS):
                        log.warning("맞장구 길이 범위 밖, 버림: voice=%s text=%r %.0fms", voice, text, ms)
                        continue
                    items.append((text, pcm))
                if items:
                    self._items[key] = items
        finally:
            if has_voice:
                tts.voice = original

    def pick(self, voice: str | None) -> tuple[str, bytes] | None:
        """해당 목소리의 맞장구 하나. 직전과 같은 문구는 피한다. 없으면 None."""
        items = self._items.get(voice)
        if not items:
            return None
        pool = [it for it in items if it[0] != self._last.get(voice)] if len(items) > 1 else items
        choice = self._rng.choice(pool)
        self._last[voice] = choice[0]
        return choice

    def save_wavs(self, out_dir: str | Path, sample_rate: int | None = None) -> list[Path]:
        """사람이 들어 볼 수 있게 wav로 저장한다. 파일명 {voice}_{i}.wav."""
        rate = sample_rate or self.sample_rate
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        paths = []
        for voice, items in self._items.items():
            for i, (_, pcm) in enumerate(items):
                p = out / f"{voice or 'default'}_{i}.wav"
                with wave.open(str(p), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(rate)
                    w.writeframes(pcm)
                paths.append(p)
        return paths
