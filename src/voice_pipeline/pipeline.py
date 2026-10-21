"""LLM 스트림 → 청커 → 정규화 → TTS → 프레임 → 싱크 (개발 포인트 문서 §1).

- 턴마다 turn_id를 발급한다. 모든 프레임에 turn_id를 붙인다.
- barge_in()은 현재 turn_id를 무효화하고 그 턴의 작업을 모두 취소한다 (§6.3).
- risk_update "119"는 같은 정지 경로를 쓰고, 안전 안내를 로컬 TTS로 낸다 (§6.5, 설계서 §31).
- 대화 기록에는 사용자가 실제로 들은 부분만 남긴다 (§6.4).
- 선행 생성: prepare()는 LLM·청커·TTS를 먼저 돌리되 싱크로 가는 모든 호출을 대기열(_Outbox)에 쌓는다.
  commit()이 대기열을 순서대로 비우고, abort()는 아무것도 보내지 않고 버린다.
  선행 중 RiskUpdate("119")는 보수적으로 commit 시점까지 미룬다 (소리를 미리 내지 않기 위해).
  ⚠️ 팀 결정 대기: 안전 안내를 확정 전에 즉시 내야 하는지는 아직 정해지지 않았다.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
from dataclasses import dataclass, field
from functools import partial
from typing import Awaitable, Callable

from .audio import Framer
from .chunker import SemanticChunker
from .llm.events import Done, LLMClient, Meta, RiskUpdate, Token
from .normalize import normalize_for_tts
from .sinks import AudioSink
from .trace import (BACKCHANNEL, FIRST_CHUNK, LLM_FIRST_TOKEN, PLAYBACK_START, SPEC_COMMIT, SPEC_START, SPEECH_END,
                    TTS_FIRST_AUDIO, TurnTrace)
from .tts.base import TTSEngine
from .turns import TurnGuard

log = logging.getLogger("voice_pipeline.pipeline")

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


class _Outbox:
    """싱크로 가는 호출의 순서 있는 대기열. released 전에는 쌓고, 후에는 바로 보낸다.

    release()는 대기열이 빈 것을 확인한 직후 await 없이 released를 켠다.
    비우는 동안 쌓인 항목도 같은 반복에서 순서대로 나가므로 순서가 뒤바뀌지 않는다.
    """

    def __init__(self, guard: TurnGuard, released: bool = True) -> None:
        self._guard = guard
        self.released = released
        self.frozen = False   # 선행 중 119: 이후 호출은 버린다
        self._q: collections.deque[tuple[int, Callable[[], Awaitable[None]]]] = collections.deque()

    async def call(self, turn_id: int, fn: Callable[[], Awaitable[None]]) -> None:
        if self.frozen:
            return
        if self.released:
            if self._guard.is_live(turn_id):  # 지난 턴 호출은 버린다
                await fn()
            return
        self._q.append((turn_id, fn))

    def freeze_with(self, turn_id: int, fn: Callable[[], Awaitable[None]]) -> None:
        """쌓인 소리·상태를 모두 버리고 fn(안전 정지 시작)만 남긴다. commit 때 실행된다."""
        self._q.clear()
        self._q.append((turn_id, fn))
        self.frozen = True

    async def release(self) -> None:
        while self._q:
            turn_id, fn = self._q.popleft()
            if self._guard.is_live(turn_id):
                await fn()
        self.released = True   # 빈 것을 확인한 뒤 await 없이 켠다


class PendingTurn:
    """선행 생성 중인 턴. commit()하기 전에는 싱크로 아무것도 나가지 않는다."""

    def __init__(self, pipe: "VoicePipeline", turn_id: int, text: str, result: "TurnResult",
                 out: _Outbox, hist_len: int) -> None:
        self._pipe, self.turn_id, self.text = pipe, turn_id, text
        self.result, self._out, self._hist_len = result, out, hist_len
        self._task: asyncio.Task | None = None
        self.state = "pending"   # pending | committed | aborted

    @property
    def hist_len(self) -> int:
        """prepare 이전 history 길이 (user 메시지를 지울 때 쓴다)."""
        return self._hist_len

    async def commit(self) -> "TurnResult":
        """턴을 확정한다. 대기열을 비우고, 턴이 끝날 때까지 기다려 respond()와 같은 결과를 돌려준다."""
        if self.state == "aborted":
            raise RuntimeError("abort된 PendingTurn")
        pipe = self._pipe
        if self.state == "pending":
            self.state = "committed"
            if pipe._pending is self:
                pipe._pending = None
            self.result.trace.mark(SPEC_COMMIT)
            await self._out.release()
        assert self._task is not None
        await self._task
        await pipe._finish(self.turn_id, self.result)
        return self.result

    async def abort(self) -> None:
        """선행 결과를 버린다. 싱크로는 아무것도 보내지 않는다 (interrupt도 없다)."""
        if self.state != "pending":
            return
        self.state = "aborted"
        pipe = self._pipe
        if pipe._pending is self:
            pipe._pending = None
        if pipe.guard.is_live(self.turn_id):
            pipe.guard.invalidate()   # 생산·소비 작업 취소
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._task is not None:
            await asyncio.wait({self._task})
            if not self._task.cancelled():
                self._task.exception()   # 미회수 예외 경고를 막는다
        try:
            await pipe.tts.cancel()
        except Exception:
            log.exception("선행 abort 중 TTS cancel 오류")
        del pipe.history[self._hist_len:]   # prepare 이전 길이로 되돌린다
        pipe.traces.pop(self.turn_id, None)


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
        self._pending: PendingTurn | None = None   # 선행 생성 중인 턴 (최대 하나)
        self.sink.playback_listener = self._on_playback

    # ---------- 공개 API ----------

    async def respond(self, user_text: str, trace: TurnTrace | None = None,
                      opener: bytes | None = None, hint: str | None = None) -> TurnResult:
        """사용자 발화 하나에 응답한다. trace에 speech_end 등을 미리 찍어 넘길 수 있다.

        opener는 미리 합성한 맞장구 PCM16 (TTS 샘플레이트). 있으면 LLM 답보다 먼저 재생한다.
        hint는 이번 턴의 어조 지시. LLM 요청에만 들어가고 화면·로그에는 나가지 않는다.
        """
        await self._abort_pending()
        turn_id = self.guard.next_turn()
        trace = self._bind_trace(turn_id, trace)
        self.history.append(self._user_message(user_text, hint))
        result = TurnResult(trace=trace)
        out = _Outbox(self.guard, released=True)
        await out.call(turn_id, partial(self.sink.avatar_state, "thinking"))
        await self._run_turn(turn_id, result, from_llm=True, opener=opener, out=out)
        await self._finish(turn_id, result)
        return result

    async def prepare(self, user_text: str, trace: TurnTrace | None = None,
                      opener: bytes | None = None, hint: str | None = None) -> PendingTurn:
        """선행 생성: LLM·청커·TTS를 지금 시작하고, 싱크로 가는 호출은 모두 대기열에 쌓는다.

        commit() 전에는 오디오·audio_begin/end·avatar_state·interrupt를 싱크로 보내지 않는다.
        선행 중 RiskUpdate("119")는 보수적으로 commit 시점까지 미룬다 (소리를 미리 내지 않기 위해).
        ⚠️ 팀 결정 대기. 새 prepare()와 barge_in()은 대기 중인 PendingTurn을 abort한다.
        """
        await self._abort_pending()
        turn_id = self.guard.next_turn()
        trace = self._bind_trace(turn_id, trace)
        trace.mark(SPEC_START)
        hist_len = len(self.history)
        self.history.append(self._user_message(user_text, hint))
        result = TurnResult(trace=trace)
        out = _Outbox(self.guard, released=False)
        await out.call(turn_id, partial(self.sink.avatar_state, "thinking"))
        pt = PendingTurn(self, turn_id, user_text, result, out, hist_len)
        pt._task = asyncio.create_task(self._run_turn(turn_id, result, from_llm=True, opener=opener, out=out))
        self._pending = pt
        return pt

    @staticmethod
    def _user_message(text: str, hint: str | None) -> dict:
        msg = {"role": "user", "content": text}
        if hint:
            msg["hint"] = hint
        return msg

    @property
    def pending(self) -> PendingTurn | None:
        return self._pending

    async def _abort_pending(self) -> None:
        if self._pending is not None:
            await self._pending.abort()

    async def _finish(self, turn_id: int, result: TurnResult) -> None:
        self._commit_history(turn_id, result)
        if self._safety_task is not None:
            task, self._safety_task = self._safety_task, None
            await asyncio.shield(task)

    async def say(self, text: str, *, engine: TTSEngine | None = None, state: str = "speaking") -> TurnResult:
        """LLM 없이 고정 문장을 말한다 (안전 안내 등)."""
        await self._abort_pending()
        turn_id = self.guard.next_turn()
        result = TurnResult(trace=self._bind_trace(turn_id, None))
        await self.sink.avatar_state(state)
        await self._run_turn(turn_id, result, from_llm=False, fixed_text=text, engine=engine)
        return result

    async def barge_in(self) -> int:
        """현재 턴을 끊는다. 무효화한 turn_id를 돌려준다.

        선행 중인 PendingTurn이 있으면 그것만 abort한다 (브라우저는 본 적이 없으므로 interrupt를 보내지 않는다).
        """
        if self._pending is not None:
            pt = self._pending
            await pt.abort()
            return pt.turn_id
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
                        opener: bytes | None = None, out: _Outbox | None = None) -> None:
        out = out or _Outbox(self.guard, released=True)
        q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self.queue_slots)
        if from_llm:
            producer = self._produce_llm(turn_id, q, result, out, strip_filler=bool(opener))
        else:
            producer = self._produce_fixed(q, fixed_text or "")
        p = self.guard.track(turn_id, asyncio.create_task(producer))
        c = self.guard.track(turn_id, asyncio.create_task(self._consume(turn_id, q, result, engine or self.tts, opener, out)))
        outs = await asyncio.gather(p, c, return_exceptions=True)
        for o in outs:
            if isinstance(o, Exception) and not isinstance(o, asyncio.CancelledError):
                raise o
        result.interrupted = not self.guard.is_live(turn_id)
        if not result.interrupted:
            await out.call(turn_id, partial(self.sink.avatar_state, "listening"))

    async def _produce_fixed(self, q: asyncio.Queue, text: str) -> None:
        await q.put(normalize_for_tts(text))
        await q.put(None)

    async def _produce_llm(self, turn_id: int, q: asyncio.Queue, result: TurnResult, out: _Outbox,
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
                    await out.call(turn_id, partial(self.sink.avatar_state, "thinking",
                                                    emotion=ev.emotion, gesture=ev.gesture))
            elif isinstance(ev, Token):
                trace.mark(LLM_FIRST_TOKEN)
                for chunk in chunker.feed(ev.text):
                    await put(chunk)
            elif isinstance(ev, RiskUpdate):
                result.risk_grade = ev.risk_grade
                if ev.risk_grade == "119":
                    # 정지는 별도 태스크에서 한다. 이 태스크도 곧 취소된다.
                    if out.released:
                        self._start_safety(turn_id)
                    else:  # 선행 중: 쌓인 소리를 버리고 commit 때 정지·안내를 시작한다
                        out.freeze_with(turn_id, partial(self._start_safety_async, turn_id))
                    await asyncio.sleep(0)
                    return
            elif isinstance(ev, Done):
                result.risk_grade = ev.risk_grade
                break
        for chunk in chunker.close():
            await put(chunk)
        await q.put(None)

    def _start_safety(self, turn_id: int) -> None:
        self._safety_task = asyncio.create_task(self._safety_stop(turn_id))

    async def _start_safety_async(self, turn_id: int) -> None:
        self._start_safety(turn_id)

    async def _consume(self, turn_id: int, q: asyncio.Queue, result: TurnResult, engine: TTSEngine,
                       opener: bytes | None = None, out: _Outbox | None = None) -> None:
        out = out or _Outbox(self.guard, released=True)
        trace = result.trace
        framer = Framer(turn_id, engine.sample_rate, self.frame_ms)
        await out.call(turn_id, partial(self.sink.begin, turn_id, engine.sample_rate))
        first_audio, speaking = True, False
        if opener:
            for frame in framer.push(opener):
                if not self.guard.is_live(turn_id):
                    return
                # BACKCHANNEL은 실제로 싱크에 쓴 시각에 찍는다 (선행이면 commit 뒤)
                await out.call(turn_id, partial(self._write_opener, frame, trace))
                result.opener_ms += frame.duration_ms
                if not speaking:
                    speaking = True
                    await out.call(turn_id, partial(self.sink.avatar_state, "speaking"))
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
                    await out.call(turn_id, partial(self.sink.write, frame))  # 지난 턴 프레임은 버린다
                    chunk.audio_ms += frame.duration_ms
                    if not speaking:
                        speaking = True
                        await out.call(turn_id, partial(self.sink.avatar_state, "speaking"))
            chunk.done = True
        for frame in framer.flush():
            await out.call(turn_id, partial(self.sink.write, frame))
        await out.call(turn_id, partial(self.sink.end, turn_id, framer.seq - 1))

    async def _write_opener(self, frame, trace: TurnTrace) -> None:
        await self.sink.write(frame)
        trace.mark(BACKCHANNEL)

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
