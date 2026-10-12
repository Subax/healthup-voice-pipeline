"""LLM 스트림 → 청커 → 정규화 → TTS → 프레임 → 싱크 (개발 포인트 문서 §1).

- 턴마다 turn_id를 발급한다. 모든 프레임에 turn_id를 붙인다.
- barge_in()은 현재 turn_id를 무효화하고 그 턴의 작업을 모두 취소한다 (§6.3).
- risk_update "119"는 같은 정지 경로를 쓰고, 안전 안내를 로컬 TTS로 낸다 (§6.5, 설계서 §31).
- 대화 기록에는 사용자가 실제로 들은 부분만 남긴다 (§6.4).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Callable

from .audio import Framer
from .chunker import SemanticChunker
from .llm.events import Done, LLMClient, Meta, RiskUpdate, Token
from .normalize import normalize_for_tts
from .sinks import AudioSink
from .trace import BACKCHANNEL, FIRST_CHUNK, LLM_FIRST_TOKEN, PLAYBACK_START, SPEECH_END, TTS_FIRST_AUDIO, TurnTrace
from .tts.base import TTSEngine
from .turns import TurnGuard

# ⚠️ 임시 문구. 정본은 정신상담 LLM 문서의 안전 응답 템플릿을 따른다.
SAFETY_TEXT_119 = "지금 많이 위험해 보여요. 화면의 안내에 따라 바로 119에 연락해 주세요. 제가 곁에서 함께 있을게요."


# 맞장구를 이미 냈을 때 LLM 첫 청크 앞의 군더더기 (구두점 필수: "아침에", "음식은"은 남는다)
_LEADING_FILLER = re.compile(r"^\s*(네|예|음|아|응)[,.…!?~]+\s*")


@dataclass
class _Chunk:
    text: str
    audio_ms: float = 0.0
    done: bool = False


@dataclass
class TurnResult:
    trace: TurnTrace
    interrupted: bool = False
    risk_grade: str = "일반"
    spoken_text: str = ""
    opener_ms: float = 0.0   # 미리 합성한 맞장구 재생 길이 (대화 기록에서 뺀다)
    chunks: list[_Chunk] = field(default_factory=list)


class VoicePipeline:
    def __init__(
        self,
        llm: LLMClient,
        tts: TTSEngine,
        sink: AudioSink,
        *,
        safety_tts: TTSEngine | None = None,
        chunker_factory: Callable[[], SemanticChunker] = SemanticChunker,
        queue_slots: int = 3,
        frame_ms: int = 20,
        fade_ms: int = 40,
        ms_per_char_estimate: float = 130.0,
    ):
        self.llm, self.tts, self.sink = llm, tts, sink
        self.safety_tts = safety_tts or tts      # 안전 안내는 항상 로컬 엔진으로 지정한다 (§4.6)
        self.chunker_factory = chunker_factory
        self.queue_slots, self.frame_ms, self.fade_ms = queue_slots, frame_ms, fade_ms
        self.ms_per_char_estimate = ms_per_char_estimate
        self.guard = TurnGuard()
        self.history: list[dict] = []
        self.traces: dict[int, TurnTrace] = {}
        self.safety_mode = False
        self._safety_task: asyncio.Task | None = None
        self.sink.playback_listener = self._on_playback

    # ---------- 공개 API ----------

    async def respond(self, user_text: str, trace: TurnTrace | None = None,
                      opener: bytes | None = None) -> TurnResult:
        """사용자 발화 하나에 응답한다. trace에 speech_end 등을 미리 찍어 넘길 수 있다.

        opener는 미리 합성한 맞장구 PCM16 (TTS 샘플레이트). 있으면 LLM 답보다 먼저 재생한다.
        """
        turn_id = self.guard.next_turn()
        trace = self._bind_trace(turn_id, trace)
        self.history.append({"role": "user", "content": user_text})
        result = TurnResult(trace=trace)
        await self.sink.avatar_state("thinking")
        await self._run_turn(turn_id, result, from_llm=True, opener=opener)
        self._commit_history(turn_id, result)
        if self._safety_task is not None:
            task, self._safety_task = self._safety_task, None
            await asyncio.shield(task)
        return result

    async def say(self, text: str, *, engine: TTSEngine | None = None, state: str = "speaking") -> TurnResult:
        """LLM 없이 고정 문장을 말한다 (안전 안내 등)."""
        turn_id = self.guard.next_turn()
        result = TurnResult(trace=self._bind_trace(turn_id, None))
        await self.sink.avatar_state(state)
        await self._run_turn(turn_id, result, from_llm=False, fixed_text=text, engine=engine)
        return result

    async def barge_in(self) -> int:
        """현재 턴을 끊는다. 무효화한 turn_id를 돌려준다."""
        dead = self.guard.invalidate()          # 1) 무효화 + 작업 취소 (동기)
        await self.sink.interrupt(dead, self.fade_ms)  # 2) 들리는 소리 정지
        await self.sink.avatar_state("listening")
        return dead

    # ---------- 내부 ----------

    def _bind_trace(self, turn_id: int, trace: TurnTrace | None) -> TurnTrace:
        trace = trace or TurnTrace(turn_id)
        trace.turn_id = turn_id
        trace.mark(SPEECH_END)
        self.traces[turn_id] = trace
        return trace

    def _on_playback(self, turn_id: int, event: str, played_ms: float) -> None:
        tr = self.traces.get(turn_id)
        if tr is not None and event == "started":
            tr.mark(PLAYBACK_START)

    async def _run_turn(self, turn_id: int, result: TurnResult, *, from_llm: bool,
                        fixed_text: str | None = None, engine: TTSEngine | None = None,
                        opener: bytes | None = None) -> None:
        q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self.queue_slots)
        if from_llm:
            producer = self._produce_llm(turn_id, q, result, strip_filler=bool(opener))
        else:
            producer = self._produce_fixed(q, fixed_text or "")
        p = self.guard.track(turn_id, asyncio.create_task(producer))
        c = self.guard.track(turn_id, asyncio.create_task(self._consume(turn_id, q, result, engine or self.tts, opener)))
        outs = await asyncio.gather(p, c, return_exceptions=True)
        for o in outs:
            if isinstance(o, Exception) and not isinstance(o, asyncio.CancelledError):
                raise o
        result.interrupted = not self.guard.is_live(turn_id)
        if not result.interrupted:
            await self.sink.avatar_state("listening")

    async def _produce_fixed(self, q: asyncio.Queue, text: str) -> None:
        await q.put(normalize_for_tts(text))
        await q.put(None)

    async def _produce_llm(self, turn_id: int, q: asyncio.Queue, result: TurnResult,
                           strip_filler: bool = False) -> None:
        chunker = self.chunker_factory()
        trace = result.trace
        first_chunk = True
        examine = strip_filler   # 첫 비어 있지 않은 청크만 검사한다

        async def put(chunk: str) -> None:
            nonlocal first_chunk, examine
            if examine and chunk.strip():
                examine = False
                chunk = _LEADING_FILLER.sub("", chunk, count=1)  # 맞장구와 겹치는 "네, " 제거
            text = normalize_for_tts(chunk)
            if not text:
                return
            if first_chunk:
                trace.mark(FIRST_CHUNK)
                first_chunk = False
            await q.put(text)  # 큐가 차면 여기서 기다린다 (역압, 설계서 §25)

        async for ev in self.llm.stream(list(self.history), turn_id=turn_id):
            if isinstance(ev, Meta):
                result.risk_grade = ev.risk_grade
                if ev.emotion or ev.gesture:
                    await self.sink.avatar_state("thinking", emotion=ev.emotion, gesture=ev.gesture)
            elif isinstance(ev, Token):
                trace.mark(LLM_FIRST_TOKEN)
                for chunk in chunker.feed(ev.text):
                    await put(chunk)
            elif isinstance(ev, RiskUpdate):
                result.risk_grade = ev.risk_grade
                if ev.risk_grade == "119":
                    # 정지는 별도 태스크에서 한다. 이 태스크도 곧 취소된다.
                    self._safety_task = asyncio.create_task(self._safety_stop(turn_id))
                    await asyncio.sleep(0)
                    return
            elif isinstance(ev, Done):
                result.risk_grade = ev.risk_grade
                break
        for chunk in chunker.close():
            await put(chunk)
        await q.put(None)

    async def _consume(self, turn_id: int, q: asyncio.Queue, result: TurnResult, engine: TTSEngine,
                       opener: bytes | None = None) -> None:
        trace = result.trace
        framer = Framer(turn_id, engine.sample_rate, self.frame_ms)
        await self.sink.begin(turn_id, engine.sample_rate)
        first_audio, speaking = True, False
        if opener:
            for frame in framer.push(opener):
                if not self.guard.is_live(turn_id):
                    return
                await self._emit(turn_id, frame)
                trace.mark(BACKCHANNEL)
                result.opener_ms += frame.duration_ms
                if not speaking:
                    speaking = True
                    await self.sink.avatar_state("speaking")
        while True:
            text = await q.get()
            if text is None:
                break
            chunk = _Chunk(text)
            result.chunks.append(chunk)
            async for pcm in engine.stream(text):
                if not self.guard.is_live(turn_id):
                    return
                if first_audio:
                    trace.mark(TTS_FIRST_AUDIO)
                    first_audio = False
                for frame in framer.push(pcm):
                    await self._emit(turn_id, frame)
                    chunk.audio_ms += frame.duration_ms
                    if not speaking:
                        speaking = True
                        await self.sink.avatar_state("speaking")
            chunk.done = True
        for frame in framer.flush():
            await self._emit(turn_id, frame)
        await self.sink.end(turn_id, framer.seq - 1)

    async def _emit(self, turn_id: int, frame) -> None:
        if self.guard.is_live(turn_id):  # 지난 턴 프레임은 버린다
            await self.sink.write(frame)

    async def _safety_stop(self, turn_id: int) -> None:
        if self.guard.is_live(turn_id):
            dead = self.guard.invalidate()
            await self.sink.interrupt(dead, self.fade_ms)
        self.safety_mode = True
        await self.say(SAFETY_TEXT_119, engine=self.safety_tts, state="safety")

    def _commit_history(self, turn_id: int, result: TurnResult) -> None:
        """사용자가 실제로 들은 부분만 assistant 메시지로 남긴다 (근사: 재생 시간 비율)."""
        if not result.interrupted:
            result.spoken_text = " ".join(c.text for c in result.chunks)
        else:
            played = max(0.0, (self.sink.played_ms(turn_id) or 0.0) - result.opener_ms)
            parts, acc = [], 0.0
            done_ms = [c.audio_ms for c in result.chunks if c.done and c.audio_ms > 0]
            per_char = (sum(done_ms) / sum(len(c.text) for c in result.chunks if c.done and c.audio_ms > 0)
                        if done_ms else self.ms_per_char_estimate)
            for c in result.chunks:
                total = c.audio_ms if c.done else max(c.audio_ms, len(c.text) * per_char)
                if acc + total <= played:
                    parts.append(c.text)
                    acc += total
                    continue
                remain = played - acc
                if remain > 0 and total > 0:
                    cut = int(len(c.text) * remain / total)
                    if cut > 0:
                        parts.append(c.text[:cut] + "…")
                break
            result.spoken_text = " ".join(parts)
        if result.spoken_text:
            msg = {"role": "assistant", "content": result.spoken_text}
            if result.interrupted:
                msg["interrupted"] = True
            self.history.append(msg)
