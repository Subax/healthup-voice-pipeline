"""Qwen3-ASR-1.7B (로컬 vLLM 서버, GPU). 구간 단위 오프라인 인식.

서버는 `scripts/run_asr.sh`로 띄운다 (127.0.0.1:8092). 음성은 이 PC 밖으로 나가지 않는다.
"""

from __future__ import annotations

import io
import os
import re
import time
import wave

import aiohttp

from .base import STTResult
from .postprocess import fix_spacing, korean_only

DEFAULT_URL = "http://127.0.0.1:8092"
MODEL = "Qwen/Qwen3-ASR-1.7B"
# 'language Korean<asr_text>안녕' 형태 접두어. 언어를 강제해도 남을 수 있다
_PREFIX = re.compile(r"^\s*language\s+\w*\s*<asr_text>", re.IGNORECASE)
_TAG = re.compile(r"<asr_text>|</asr_text>|<\|[^|>]*\|>")


def to_wav(pcm16: bytes, sample_rate: int = 16000) -> bytes:
    """PCM16 모노를 WAV 바이트로 감싼다."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm16)
    return buf.getvalue()


def parse_output(raw: str) -> str:
    """모델 출력에서 언어 접두어·태그를 떼고 본문만 남긴다."""
    text = _PREFIX.sub("", raw or "")
    return _TAG.sub("", text).strip()


class Qwen3ASRSTT:
    def __init__(
        self,
        url: str | None = None,
        language: str = "Korean",
        name: str = "qwen3-asr-1.7b",
        spacing: bool = False,
        korean_only: bool = True,
        timeout_s: float = 30.0,
    ):
        """language는 'Korean' 고정이다 (요청에 language=ko를 실어 서버가 언어를 강제한다)."""
        self.name = name
        self.sample_rate = 16000
        self.url = (url or os.environ.get("QWEN3ASR_URL") or DEFAULT_URL).rstrip("/")
        self.language = language
        self.spacing = spacing
        self.korean_only = korean_only
        self.timeout_s = timeout_s
        self._session: aiohttp.ClientSession | None = None

    def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout_s))
        return self._session

    async def _request(self, pcm16: bytes) -> str:
        form = aiohttp.FormData()
        form.add_field("file", to_wav(pcm16, self.sample_rate), filename="a.wav", content_type="audio/wav")
        form.add_field("model", MODEL)
        form.add_field("language", "ko")
        form.add_field("temperature", "0")
        async with self._sess().post(f"{self.url}/v1/audio/transcriptions", data=form) as r:
            r.raise_for_status()
            return (await r.json()).get("text", "")

    async def warmup(self) -> None:
        await self._request(bytes(self.sample_rate))  # 무음 1초

    async def transcribe(self, pcm16: bytes) -> STTResult:
        t0 = time.perf_counter()
        text = parse_output(await self._request(pcm16))
        if self.korean_only:
            text = korean_only(text)
        if self.spacing:
            text = fix_spacing(text)
        return STTResult(text=text, lang="ko", elapsed_ms=(time.perf_counter() - t0) * 1000.0)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
