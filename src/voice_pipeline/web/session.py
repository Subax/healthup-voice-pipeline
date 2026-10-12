"""브라우저 탭 하나의 대화 세션: 마이크 → VAD → STT → VoicePipeline → 브라우저 재생.

SPEECH_END 시각은 VAD가 발화 끝을 확정한 순간(SpeechEnd 수신)에 세션이 먼저 찍는다.
TurnTrace.mark는 같은 이름의 첫 시각만 남기므로, 파이프라인이 나중에 다시 찍어도 이 시각이 이긴다.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from aiohttp import web

from ..bridge.ws_server_sink import WebSocketServerSink
from ..llm.events import LLMClient, LLMEvent, Token
from ..chunker import SemanticChunker
from ..pipeline import VoicePipeline
from ..trace import (BACKCHANNEL, FIRST_CHUNK, LLM_FIRST_TOKEN, PLAYBACK_START, SPEECH_END, STT_FINAL,
                     TTS_FIRST_AUDIO, TurnTrace)
from .engines import Engines

log = logging.getLogger("voice_pipeline.web")

TRACE_MARKS = {"stt_final": STT_FINAL, "llm_first_token": LLM_FIRST_TOKEN, "first_chunk": FIRST_CHUNK,
               "backchannel": BACKCHANNEL, "tts_first_audio": TTS_FIRST_AUDIO, "playback_start": PLAYBACK_START}


class TeeLLM:
    """LLM 이벤트를 그대로 넘기면서 Token 텍스트를 UI로 복사한다."""

    def __init__(self, inner: LLMClient, on_token):
        self.inner, self.on_token = inner, on_token

    async def stream(self, messages: list[dict], turn_id: int) -> AsyncIterator[LLMEvent]:
        async for ev in self.inner.stream(messages, turn_id=turn_id):
            if isinstance(ev, Token):
                await self.on_token(turn_id, ev.text)
            yield ev


class DemoSession:
    def __init__(self, ws: web.WebSocketResponse, engines: Engines):
        self.engines = engines
        self.sink = WebSocketServerSink(ws)
        first_min = engines.first_min
        self.pipeline = VoicePipeline(TeeLLM(engines.llm, self._on_token), engines.tts, self.sink,
                                      chunker_factory=lambda: SemanticChunker(first_min=first_min))
        self._pipeline_listener = self.sink.playback_listener
        self.sink.playback_listener = self._on_playback
        self.vad = engines.make_vad()
        self.barge_in_enabled = True
        self.backchannel_enabled = engines.backchannel is not None
        self._task: asyncio.Task | None = None
        self._stage: str | None = None      # "stt" | "respond" | None
        self._bg: set[asyncio.Task] = set()
        self._muted = False                 # 반이중 중 마이크 무시 상태
        self._closed = False

    # ---------- 전송 ----------

    async def send(self, obj: dict) -> None:
        await self.sink.send_json(obj)

    async def send_hello(self) -> None:
        tts = self.engines.tts
        voices = list(getattr(tts, "voices", None) or [])  # 목소리 목록은 TTS 어댑터가 warmup 뒤에 채운다
        await self.send({"type": "hello", "engines": self.engines.labels, "barge_in": self.barge_in_enabled,
                         "backchannel": self.backchannel_enabled,
                         "first_min": self.engines.first_min, "vad_silence_ms": self.engines.vad_silence_ms,
                         "voices": voices, "voice": getattr(tts, "voice", None)})

    async def _on_token(self, turn_id: int, text: str) -> None:
        await self.send({"type": "assistant_delta", "turn_id": turn_id, "text": text})

    def _on_playback(self, turn_id: int, event: str, played_ms: float) -> None:
        if self._pipeline_listener:
            self._pipeline_listener(turn_id, event, played_ms)  # 먼저 trace에 playback_start를 찍는다
        if event == "started":
            tr = self.pipeline.traces.get(turn_id)
            if tr is not None:
                self._spawn(self.send({"type": "trace_update", "turn_id": turn_id,
                                       "trace": self._trace_ms(tr)}))

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    @staticmethod
    def _trace_ms(tr: TurnTrace) -> dict[str, Any]:
        out = {k: (None if (v := tr.since(m)) is None else round(v, 1)) for k, m in TRACE_MARKS.items()}
        return out

    # ---------- 입력 ----------

    def _busy(self) -> bool:
        return self._stage == "respond" or self.sink.active

    async def on_mic(self, data: bytes) -> None:
        """마이크 PCM16 16 kHz 모노를 VAD에 넣는다."""
        if self._closed:
            return
        if self._busy() and not self.barge_in_enabled:
            if not self._muted:  # 반이중: 말하는 동안 마이크를 무시한다
                self._muted = True
                self.vad.reset()
            return
        self._muted = False
        for ev in self.vad.feed(data):
            kind = type(ev).__name__
            if kind == "SpeechStart":
                await self.send({"type": "vad", "speaking": True})
                # 최소 구현. TODO: 맞장구(네, 음) 필터는 범위 밖이다. 지금은 어떤 발화든 끊는다.
                if self._busy():
                    await self._stop_current()
            elif kind == "SpeechEnd":
                await self.send({"type": "vad", "speaking": False})
                trace = TurnTrace(0)
                trace.mark(SPEECH_END)
                await self._start_turn(trace, pcm16=ev.pcm16)

    async def on_json(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        kind = msg.get("type")
        if kind == "playback":
            self.sink.handle_playback(msg)
        elif kind == "text_input":
            text = str(msg.get("text", "")).strip()
            if text:
                trace = TurnTrace(0)
                trace.mark(SPEECH_END)
                await self._start_turn(trace, text=text)
        elif kind == "config":
            if "barge_in" in msg:
                self.barge_in_enabled = bool(msg["barge_in"])
            if "backchannel" in msg:
                self.backchannel_enabled = bool(msg["backchannel"]) and self.engines.backchannel is not None
            voice = msg.get("voice")
            if isinstance(voice, str) and voice:
                # 엔진은 공유다. 탭 하나만 쓴다는 전제 (README 참고)
                ids = {v.get("id") for v in (getattr(self.engines.tts, "voices", None) or [])}
                if voice in ids:
                    self.engines.tts.voice = voice
        elif kind == "reset":
            await self._stop_current()
            self.pipeline.history.clear()
            await self.send({"type": "reset_done"})

    # ---------- 턴 ----------

    async def _stop_current(self) -> None:
        """진행 중 STT/응답/재생을 멈춘다."""
        t = self._task
        running = t is not None and not t.done()
        if running and self._stage == "stt":
            t.cancel()
        if (running and self._stage == "respond") or self.sink.active:
            await self.pipeline.barge_in()
        if running:
            await asyncio.wait({t})

    async def _start_turn(self, trace: TurnTrace, *, pcm16: bytes | None = None, text: str | None = None) -> None:
        await self._stop_current()
        if self._closed:
            return
        self._task = asyncio.create_task(self._run_turn(trace, pcm16, text))

    async def _run_turn(self, trace: TurnTrace, pcm16: bytes | None, text: str | None) -> None:
        stt_ms, emotion, event = 0.0, None, None
        try:
            self._stage = "stt"
            if pcm16 is not None:
                r = await self.engines.stt.transcribe(pcm16)
                text, stt_ms = (r.text or "").strip(), float(r.elapsed_ms)
                emotion, event = getattr(r, "emotion", None), getattr(r, "event", None)
            trace.mark(STT_FINAL)
            if not text:
                return
            await self.send({"type": "transcript", "text": text, "stt_ms": round(stt_ms, 1),
                             "emotion": emotion, "event": event})
            self._stage = "respond"
            opener = None
            bank = self.engines.backchannel
            if self.backchannel_enabled and bank is not None:
                picked = bank.pick(getattr(self.engines.tts, "voice", None))
                opener = picked[1] if picked else None
            res = await self.pipeline.respond(text, trace=trace, opener=opener)
            await self.send({"type": "turn_done", "turn_id": res.trace.turn_id,
                             "interrupted": res.interrupted, "spoken_text": res.spoken_text,
                             "trace": {"stt_ms": round(stt_ms, 1), **self._trace_ms(res.trace)}})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("턴 처리 오류")
            await self.send({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self._stage = None

    async def close(self) -> None:
        """WS가 닫혔다. 진행 중 작업을 정리한다."""
        self._closed = True
        try:
            await self._stop_current()
        finally:
            for t in list(self._bg):
                t.cancel()
