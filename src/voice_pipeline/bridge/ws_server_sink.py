"""서버 쪽 WebSocket(aiohttp web.WebSocketResponse) 위의 AudioSink.

UnrealBridgeSink와 같은 메시지를 보낸다. 브라우저가 Unreal 역할(재생 주체)을 한다.
들어오는 메시지의 분기는 세션이 한다 (같은 WS로 마이크 오디오도 들어온다).
세션은 playback JSON을 handle_playback()에 넘긴다.
"""

from __future__ import annotations

import asyncio
import collections
import json

from aiohttp import web

from ..audio import AudioFrame
from ..sinks import PlaybackListener
from .protocol import pack_frame


class WebSocketServerSink:
    def __init__(self, ws: web.WebSocketResponse):
        self.ws = ws
        self.playback_listener: PlaybackListener | None = None
        self.active_turn: int | None = None  # begin 이후 아직 재생이 끝나지 않은 턴
        self._played: dict[int, float] = collections.defaultdict(float)
        self._lock = asyncio.Lock()  # 동시 전송 보호

    @property
    def active(self) -> bool:
        return self.active_turn is not None

    async def _send(self, data: str | bytes) -> None:
        if self.ws.closed:
            return
        async with self._lock:
            try:
                if isinstance(data, bytes):
                    await self.ws.send_bytes(data)
                else:
                    await self.ws.send_str(data)
            except (ConnectionResetError, RuntimeError):
                pass  # 브라우저가 이미 닫혔다

    async def send_json(self, obj: dict) -> None:
        """UI용 메시지를 보낸다."""
        await self._send(json.dumps(obj, ensure_ascii=False))

    def handle_playback(self, msg: dict) -> None:
        """브라우저의 playback 메시지를 반영한다."""
        tid, ev = int(msg["turn_id"]), str(msg["event"])
        played = float(msg.get("played_ms", 0.0))
        if played >= 0:
            self._played[tid] = max(self._played[tid], played)
        if ev in ("finished", "stopped") and self.active_turn == tid:
            self.active_turn = None
        if self.playback_listener:
            self.playback_listener(tid, ev, played)

    async def begin(self, turn_id: int, sample_rate: int) -> None:
        self.active_turn = turn_id
        await self.send_json({"type": "audio_begin", "turn_id": turn_id,
                              "sample_rate": sample_rate, "channels": 1})

    async def write(self, frame: AudioFrame) -> None:
        await self._send(pack_frame(frame.turn_id, frame.seq, frame.pcm16, frame.is_last))

    async def end(self, turn_id: int, last_seq: int) -> None:
        await self.send_json({"type": "audio_end", "turn_id": turn_id, "last_seq": last_seq})

    async def interrupt(self, turn_id: int, fade_ms: int = 40) -> None:
        if self.active_turn == turn_id:
            self.active_turn = None
        await self.send_json({"type": "interrupt", "turn_id": turn_id, "fade_ms": fade_ms})

    async def avatar_state(self, state: str, **fields) -> None:
        await self.send_json({"type": "avatar_state", "state": state,
                              **{k: v for k, v in fields.items() if v is not None}})

    def played_ms(self, turn_id: int) -> float | None:
        return self._played.get(turn_id)
