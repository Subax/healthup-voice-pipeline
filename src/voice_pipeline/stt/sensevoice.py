"""SenseVoice-Small INT8 (sherpa-onnx, CPU). 구간 단위 오프라인 인식."""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

import numpy as np

from .base import STTResult

DEFAULT_MODEL_DIR = Path(__file__).resolve().parents[3] / "models" / "sense-voice-int8"  # 2024-07-17 INT8 (2025-09-09는 한국어 오인식)
_TAG = re.compile(r"<\|[^|>]*\|>")
_NONE_TAGS = {"", "nospeech", "unk", "withitn", "woitn"}


def _clean_tag(value: str | None) -> str | None:
    """'<|HAPPY|>' → 'HAPPY'. 비어 있으면 None."""
    if not value:
        return None
    v = value.strip().strip("<|>").strip()
    return None if v.lower() in _NONE_TAGS else v


class SenseVoiceSTT:
    def __init__(
        self,
        model_dir: str | Path = DEFAULT_MODEL_DIR,
        language: str = "ko",
        use_itn: bool = True,
        num_threads: int = 4,
        name: str = "sensevoice-int8",
    ):
        self.name = name
        self.sample_rate = 16000
        self.model_dir = Path(model_dir)
        self.language = language
        self.use_itn = use_itn
        self.num_threads = num_threads
        self._rec = None

    def _load(self):
        if self._rec is None:
            import sherpa_onnx

            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(self.model_dir / "model.int8.onnx"),
                tokens=str(self.model_dir / "tokens.txt"),
                language=self.language,
                use_itn=self.use_itn,
                num_threads=self.num_threads,
                debug=False,
            )
        return self._rec

    def _run(self, pcm16: bytes) -> STTResult:
        rec = self._load()
        t0 = time.perf_counter()
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
        stream = rec.create_stream()
        stream.accept_waveform(self.sample_rate, samples)
        rec.decode_stream(stream)
        r = stream.result
        elapsed = (time.perf_counter() - t0) * 1000.0
        raw = r.text or ""
        # sherpa가 태그를 필드로 주면 쓰고, 아니면 텍스트 안의 태그에서 찾는다
        tags = _TAG.findall(raw)
        lang = _clean_tag(getattr(r, "lang", None))
        emotion = _clean_tag(getattr(r, "emotion", None))
        event = _clean_tag(getattr(r, "event", None))
        if lang is None and tags:
            lang = _clean_tag(tags[0])
        return STTResult(
            text=_TAG.sub("", raw).strip(),
            lang=lang,
            emotion=emotion,
            event=event,
            elapsed_ms=elapsed,
        )

    async def warmup(self) -> None:
        def _w() -> None:
            self._load()
            self._run(np.zeros(self.sample_rate // 2, dtype=np.int16).tobytes())

        await asyncio.get_running_loop().run_in_executor(None, _w)

    async def transcribe(self, pcm16: bytes) -> STTResult:
        return await asyncio.get_running_loop().run_in_executor(None, self._run, pcm16)
