"""Unreal(재생 주체)로 PCM 스트림을 보내는 WebSocket 싱크."""

from __future__ import annotations

import asyncio
import collections
import json

import aiohttp

from ..audio import AudioFrame
from ..sinks import PlaybackListener
from .protocol import pack_frame


class UnrealBridgeSink:
    def __init__(self, url: str):
        self.url = url
        self.playback_listener: PlaybackListener | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._played: dict[int, float] = collections.defaultdict(float)

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self.url, heartbeat=10)
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    async def _read_loop(self) -> None:
        assert self._ws is not None
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if data.get("type") == "playback":
                tid, ev = int(data["turn_id"]), data["event"]
                played = float(data.get("played_ms", 0.0))
                self._played[tid] = max(self._played[tid], played)
                if self.playback_listener:
                    self.playback_listener(tid, ev, played)

    async def _send_json(self, obj: dict) -> None:
        assert self._ws is not None, "connect() 먼저 호출"
        await self._ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def begin(self, turn_id: int, sample_rate: int) -> None:
        await self._send_json({"type": "audio_begin", "turn_id": turn_id,
                               "sample_rate": sample_rate, "channels": 1})

    async def write(self, frame: AudioFrame) -> None:
        assert self._ws is not None
        await self._ws.send_bytes(pack_frame(frame.turn_id, frame.seq, frame.pcm16, frame.is_last))

    async def end(self, turn_id: int, last_seq: int) -> None:
        await self._send_json({"type": "audio_end", "turn_id": turn_id, "last_seq": last_seq})

    async def interrupt(self, turn_id: int, fade_ms: int = 40) -> None:
        await self._send_json({"type": "interrupt", "turn_id": turn_id, "fade_ms": fade_ms})

    async def avatar_state(self, state: str, **fields) -> None:
        await self._send_json({"type": "avatar_state", "state": state,
                               **{k: v for k, v in fields.items() if v is not None}})

    def played_ms(self, turn_id: int) -> float | None:
        return self._played.get(turn_id)
