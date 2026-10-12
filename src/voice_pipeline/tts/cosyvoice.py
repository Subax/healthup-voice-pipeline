"""CosyVoice3 어댑터. services/cosyvoice 의 HTTP 서버(PCM16 청크 스트림)를 부르는 얇은 클라이언트."""

from __future__ import annotations

import os
from typing import AsyncIterator

import aiohttp


class CosyVoiceTTS:
    name = "cosyvoice3"

    def __init__(self, url: str | None = None, sample_rate: int = 24000):
        self.url = (url or os.environ.get("COSYVOICE_URL") or "http://127.0.0.1:8091").rstrip("/")
        self.sample_rate = sample_rate
        self.voice: str | None = os.environ.get("COSYVOICE_VOICE") or None  # 서버 프리셋 id (None 이면 서버 기본)
        self.voices: list[dict] = []  # warmup() 이 /voices 에서 채운다
        self._session: aiohttp.ClientSession | None = None
        self._resp: aiohttp.ClientResponse | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=60))
        return self._session

    async def warmup(self) -> None:
        """/info 로 샘플레이트를 받고 연결을 연다."""
        session = await self._ensure_session()
        async with session.get(f"{self.url}/info") as r:
            r.raise_for_status()
            info = await r.json()
        self.sample_rate = int(info["sample_rate"])
        try:
            async with session.get(f"{self.url}/voices") as r:
                self.voices = (await r.json())["voices"] if r.status == 200 else []
        except (aiohttp.ClientError, KeyError, ValueError):
            self.voices = []  # 구버전 서버

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        session = await self._ensure_session()
        carry = b""
        body: dict = {"text": text}
        if self.voice:
            body["voice"] = self.voice
        async with session.post(f"{self.url}/tts", json=body) as resp:
            resp.raise_for_status()
            self._resp = resp
            try:
                async for chunk in resp.content.iter_any():
                    data = carry + chunk
                    cut = len(data) - (len(data) % 2)  # 샘플(2바이트) 경계 유지
                    carry = data[cut:]
                    if cut:
                        yield data[:cut]
            except (aiohttp.ClientError, ConnectionResetError):
                if self._resp is not None:  # cancel()로 끊긴 경우는 정상 종료
                    raise
            finally:
                self._resp = None

    async def cancel(self) -> None:
        """진행 중 응답을 닫는다. 서버는 연결이 끊기면 생성을 멈춘다."""
        resp, self._resp = self._resp, None
        if resp is not None:
            resp.close()

    async def aclose(self) -> None:
        await self.cancel()
        if self._session is not None:
            await self._session.close()
            self._session = None
