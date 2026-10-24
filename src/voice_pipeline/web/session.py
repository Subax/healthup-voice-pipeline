"""브라우저 탭 하나의 대화 세션: 마이크 → VAD → STT → VoicePipeline → 브라우저 재생.

턴 종료 방식 (세션마다 `endpoint_mode`):
- smart: VAD 멈춤(200ms)마다 TurnEndpointer가 STT·Smart Turn·Kiwi 어미로 대기 시간을 정한다.
- fixed: VAD 무음 vad_silence_ms(500) 뒤 바로 턴을 확정한다 (이전 동작).

SPEECH_END 시각은 두 방식 모두 "실제 발화 끝"이다.
smart는 마지막 멈춤의 시작 시각, fixed는 VAD 방출 시각 - vad_silence_ms 이다.
(이전에는 fixed의 SPEECH_END가 VAD 방출 시각이라 약 500ms 늦었다. 두 방식의 지연을 같은 기준으로 비교하려고 맞췄다.)
TURN_CONFIRMED는 턴이 확정된 시각이다. fixed에서는 VAD 방출 시각이다.
TurnTrace.mark는 같은 이름의 첫 시각만 남기므로, 파이프라인이 나중에 다시 찍어도 이 시각이 이긴다.
id: 사용자 발화마다 utt_id (세션 카운터). 파이프라인 turn_id는 respond()에서 나중에 정해지므로
transcript와 turn_done에 둘 다 싣는다 (transcript의 turn_id는 곧 할당될 값).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from aiohttp import web

from ..audio import Framer
from ..bridge.ws_server_sink import WebSocketServerSink
from ..llm.events import LLMClient, LLMEvent, Token
from ..barge_filter import PROBE_WINDOW_MS, is_backchannel
from ..chunker import SemanticChunker
from ..intent import (
    GRADE_CONFIRM,
    GRADE_DIALOG,
    GRADE_IMMEDIATE,
    INTENT_CAMERA_OFF,
    INTENT_CAMERA_ON,
    INTENT_CHAT,
    INTENT_CONFIRM_NO,
    INTENT_CONFIRM_YES,
    INTENT_DEV_INFO_OFF,
    INTENT_DEV_INFO_ON,
    INTENT_MODE_DEMO,
    INTENT_MODE_DEV,
    INTENT_REDO_USER,
    INTENT_REPEAT_BOT,
    INTENT_RESET,
    INTENT_SESSION_END,
    INTENT_SESSION_START,
    INTENT_STOP_SPEAKING,
    INTENT_UI_BADGE_TOGGLE,
    INTENT_WAKE_OFF,
    INTENT_WAKE_ON,
    STATE_ALLOWED_INTENTS,
    classify_intent,
)
from ..pipeline import PendingTurn, VoicePipeline
from ..prompts import (
    PROMPT_CONFIRM_CAMERA,
    PROMPT_CONFIRM_END,
    PROMPT_CONFIRM_HIDE_AI,
    PROMPT_CONFIRM_MODE,
    PROMPT_CONFIRM_RESET,
    PROMPT_GREETING_END,
    PROMPT_GREETING_START,
    PROMPT_NOTICE_AI,
    PROMPT_REDO_EMPTY,
    PROMPT_REDO_SUCCESS,
    PROMPT_REPROMPT,
    PromptBank,
)
from ..trace import (BACKCHANNEL, FIRST_CHUNK, LLM_FIRST_TOKEN, PLAYBACK_START, SPEECH_END, STT_FINAL,
                     TTS_FIRST_AUDIO, TURN_CONFIRMED, TurnTrace)
from ..endpoint.turn import ConfirmedTurn, TurnEndpointer
from ..llm.hint import emotion_hint, fused_hint
from ..tone import AUTO, CALM, extract_features, pick_tone
from .devlog import DevLogger
from .engines import Engines
from .lifecycle import SessionLifecycle, SessionState

log = logging.getLogger("voice_pipeline.web")

AUTO_LABEL = "자동 (사용자 말투에 맞춤)"

PAUSE_SILENCE_MS = 200   # smart 모드 VAD 최소 무음 (짧은 멈춤까지 알린다)
RESUME_MERGE_MS = 1500   # 확정 직후 이 시간 안에 다시 말하면(본 응답 소리 전) 같은 턴으로 이어 붙인다
PRE_ROLL_MS = 300        # VAD 구간 앞에 붙는 pre-roll. 말한 길이 = 구간 길이 - 이 값
WAKE_MAX_SPEECH_MS = 2000  # 대기 상태에서 STT로 넘길 최대 발화 길이 (초과 시 무시)
ENDPOINT_OPTIONS = [{"id": "smart", "label": "어미+운율 (Smart Turn·Kiwi)"},
                    {"id": "fixed", "label": "고정 무음 500ms"}]

TRACE_MARKS = {"stt_final": STT_FINAL, "turn_confirmed": TURN_CONFIRMED, "llm_first_token": LLM_FIRST_TOKEN, "first_chunk": FIRST_CHUNK,
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


@dataclass
class _Spec:
    """선행 생성 중인 턴과 확정 전까지 보관하는 UI 메시지 (assistant_delta)."""

    pt: PendingTurn
    text: str
    trace: TurnTrace
    tone: str | None
    emotion: str | None
    event: str | None
    hint_text: str | None = None
    hint_rule: str = "R0"
    deltas: list[tuple[int, str]] = field(default_factory=list)
    held: bool = True         # True인 동안 assistant_delta를 보관한다
    claimed: bool = False     # 확정되어 응답 작업이 넘겨받았다


class DemoSession:
    def __init__(self, ws: web.WebSocketResponse, engines: Engines, session_id: str | None = None):
        self.engines = engines
        self.sink = WebSocketServerSink(ws)
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.dev_logger = DevLogger(base_dir=engines.dev_log_dir, retention_days=engines.dev_log_days)
        self._turn_tokens: dict[int, str] = {}
        self._barge_events: list[dict] = []
        self._last_turn_eval_reason: str | None = None
        self._last_face_summary: dict | None = None
        self._accepting_face_summary: bool = True
        first_min = engines.first_min
        self.pipeline = VoicePipeline(TeeLLM(engines.llm, self._on_token), engines.tts, self.sink,
                                      chunker_factory=lambda: SemanticChunker(first_min=first_min))
        self._pipeline_listener = self.sink.playback_listener
        self.sink.playback_listener = self._on_playback
        self._utt_seq = 0                   # 사용자 발화 번호
        self.speculate = bool(getattr(engines, "speculate", True))  # 선행 생성 (대기 중 LLM·TTS 먼저)
        self._spec: _Spec | None = None
        self.endpoint_mode = engines.endpoint_default if engines.endpoint_default in ("smart", "fixed") else "smart"
        self.mode = getattr(engines, "default_mode", "demo")
        if self.mode not in ("demo", "dev"):
            self.mode = "demo"
        self.vad = self._new_vad()
        self.endpointer = TurnEndpointer(
            lambda: self._stt(), engines.endpoint_scorer, self._emit_turn_eval, self._on_turn_confirmed,
            stt_id=lambda: self.stt_id, mode=lambda: self.endpoint_mode, log_dir=engines.endpoint_log_dir,
            session_mode=lambda: self.mode,
            utt_id_factory=self._next_utt,
            on_speculate=self._on_speculate if self.speculate else None,
            on_speculate_cancel=self._on_speculate_cancel if self.speculate else None)
        self.barge_in_enabled = True
        self.barge_filter = bool(getattr(engines, "barge_filter", True))  # 말하는 중 짧은 맞장구는 끼어들기로 보지 않는다
        self._probe: dict | None = None     # 맞장구 판별 중: {"timer": Task}
        self.backchannel_enabled = engines.backchannel is not None
        self._task: asyncio.Task | None = None
        self._stage: str | None = None      # "stt" | "respond" | None
        self._bg: set[asyncio.Task] = set()
        self._last_confirm: dict | None = None  # smart 확정 직후 상태: turn, t, trace, hl(history 길이)
        self._muted = False                 # 반이중 중 마이크 무시 상태
        self._closed = False
        self.stt_id = engines.stt_id        # STT 선택은 세션마다
        self._stt_switch: asyncio.Task | None = None
        self._last_pcm: bytes | None = None  # 마지막 마이크 발화 (다시 인식용)
        self.voice_auto = False             # 목소리 선택이 auto 인가. 기본은 끔: 감정 맞춤은 LLM 어조 지시(hint)로 한다
        self.emotion_hint = bool(getattr(engines, "emotion_hint", True))
        self.intent_enabled = bool(getattr(engines, "intent", True))
        self.feature_prefs: dict[str, bool] = {
            "wake": bool(getattr(engines, "wake", True)),
            "face": False,
            "show_ai_notice": True,
            "show_camera_badge": True,
            "show_conn_status": True,
            "show_dev_info": False,
        }
        self.wake_ignored_count: int = 0
        self._last_bot_text: str = ""
        self._speech_start_time: float | None = None
        self.prompt_bank = getattr(engines, "prompt_bank", None) or PromptBank()
        initial_lifecycle = (
            SessionState.IDLE
            if (getattr(engines, "prompt_bank", None) is not None and self.feature_prefs["wake"] and self.intent_enabled)
            else SessionState.ACTIVE
        )
        self.lifecycle = SessionLifecycle(initial_state=initial_lifecycle, on_state_change=self._on_lifecycle_change)
        self._notice_played: bool = False

    def _next_utt(self) -> int:
        self._utt_seq += 1
        return self._utt_seq

    def _new_vad(self) -> Any:
        """모드에 맞는 VAD. smart는 짧은 멈춤(200ms)까지 알린다."""
        return self.engines.new_vad(PAUSE_SILENCE_MS if self.endpoint_mode == "smart" else None)

    async def _emit_turn_eval(self, ev: dict) -> None:
        self._last_turn_eval_reason = ev.get("reason")
        await self.send(ev)

    def _voice_ids(self) -> set[str]:
        return {v.get("id") for v in (getattr(self.engines.tts, "voices", None) or [])}

    def _auto_active(self) -> bool:
        return self.voice_auto and bool(self._voice_ids())

    def _stt(self) -> Any:
        return self.engines._stt_cache[self.stt_id]

    # ---------- 전송 ----------

    async def send(self, obj: dict) -> None:
        await self.sink.send_json(obj)

    async def send_hello(self) -> None:
        tts = self.engines.tts
        voices = list(getattr(tts, "voices", None) or [])  # 목소리 목록은 TTS 어댑터가 warmup 뒤에 채운다
        voice = getattr(tts, "voice", None)
        if voices:  # auto 는 세션이 UI 용으로만 붙인다 (서버 목록에는 없다). 기본 선택은 서버 목소리다
            voices = [{"id": AUTO, "label": AUTO_LABEL}, *voices]
            if self.voice_auto:
                voice = AUTO
        await self.send({"type": "hello", "mode": self.mode, "engines": self.engines.labels, "barge_in": self.barge_in_enabled,
                         "backchannel": self.backchannel_enabled,
                         "first_min": self.engines.first_min, "vad_silence_ms": self.engines.vad_silence_ms,
                         "voices": voices, "voice": voice,
                         "stt_options": self.engines.stt_options(), "stt": self.stt_id,
                         "endpoint": self.endpoint_mode, "endpoint_options": ENDPOINT_OPTIONS,
                         "wake": self.feature_prefs.get("wake", True),
                         "session_state": self.lifecycle.state})

    async def _on_lifecycle_change(self, old_state: str, new_state: str) -> None:
        await self.send({"type": "session_state", "state": new_state})

    async def play_prompt(self, key: str, *, voice: str | None = None, state: str = "speaking") -> int:
        """PromptBank의 안내 음성을 즉시 재생한다."""
        await self._stop_current()
        turn_id = self.pipeline.guard.next_turn()
        self.pipeline.traces[turn_id] = TurnTrace(turn_id)
        selected_voice = voice or getattr(self.engines.tts, "voice", None)
        pcm = self.prompt_bank.get_audio(key, selected_voice)
        sample_rate = self.prompt_bank.sample_rate or getattr(self.engines.tts, "sample_rate", 24000)

        await self.sink.avatar_state(state)
        await self.sink.begin(turn_id, sample_rate)

        if pcm:
            framer = Framer(turn_id, sample_rate, self.pipeline.frame_ms)
            for frame in framer.push(pcm):
                if not self.pipeline.guard.is_live(turn_id):
                    return turn_id
                await self.sink.write(frame)
            for frame in framer.flush():
                if not self.pipeline.guard.is_live(turn_id):
                    return turn_id
                await self.sink.write(frame)
            await self.sink.end(turn_id, framer.seq - 1)
        else:
            text = self.prompt_bank.get_text(key)
            if text:
                tts = self.engines.tts
                framer = Framer(turn_id, tts.sample_rate, self.pipeline.frame_ms)
                async for chunk in tts.stream(text):
                    if not self.pipeline.guard.is_live(turn_id):
                        return turn_id
                    for frame in framer.push(chunk):
                        await self.sink.write(frame)
                for frame in framer.flush():
                    await self.sink.write(frame)
                await self.sink.end(turn_id, framer.seq - 1)

        await self.sink.avatar_state("listening")
        return turn_id

    async def start_session(self, *, play_notice: bool = True) -> None:
        """세션을 시작하고 필요 시 AI 법적 고지 음성을 재생한다."""
        await self.lifecycle.start()
        if play_notice and not self._notice_played:
            self._notice_played = True
            await self.play_prompt(PROMPT_NOTICE_AI)

    async def end_session(self, *, play_greeting: bool = True) -> None:
        """세션을 종료하고 기록을 삭제한 뒤 대기(idle) 상태로 복귀한다."""
        await self._stop_current()
        await self.lifecycle.transition_to(SessionState.CLOSING)
        if play_greeting:
            await self.play_prompt(PROMPT_GREETING_END)
        self.pipeline.history.clear()
        self._turn_tokens.clear()
        self._barge_events.clear()
        self._notice_played = False
        self._last_face_summary = None
        await self.lifecycle.transition_to(SessionState.IDLE)

    async def _on_token(self, turn_id: int, text: str) -> None:
        self._turn_tokens[turn_id] = self._turn_tokens.get(turn_id, "") + text
        sp = self._spec
        if sp is not None and sp.held and sp.pt.turn_id == turn_id:  # 확정 전에는 UI로 보내지 않는다
            sp.deltas.append((turn_id, text))
            return
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
                self._speech_start_time = time.monotonic()
                self._accepting_face_summary = True
                await self.send({"type": "vad", "speaking": True})
                if self.lifecycle.is_idle:
                    continue
                if self.endpoint_mode == "smart":
                    await self._maybe_reopen()
                if self._busy():
                    if self.barge_filter and self.barge_in_enabled:
                        await self._probe_begin()   # 소리를 낮추고 맞장구인지 본다. 아직 끊지 않는다
                        continue
                    await self._stop_current()
                if self.endpoint_mode == "smart":
                    await self.endpointer.on_speech_start()
            elif kind == "VoiceOnset":  # SpeechStart보다 빠른 신호: 대기 타이머만 멈춘다 (barge-in 아님)
                self._accepting_face_summary = True
                if self.lifecycle.is_idle:
                    continue
                if self.endpoint_mode == "smart":
                    await self._maybe_reopen()
                    await self.endpointer.on_voice_onset()
            elif kind == "SpeechEnd":
                await self.send({"type": "vad", "speaking": False})
                now = time.monotonic()
                if self.lifecycle.is_idle:
                    if not self.feature_prefs.get("wake", True):
                        continue
                    dur_ms = max(0.0, len(ev.pcm16) / 32.0 - PRE_ROLL_MS)
                    if dur_ms > WAKE_MAX_SPEECH_MS:
                        log.debug("대기 중 발화 길이 초과로 무시: dur_ms=%.1f > %d", dur_ms, WAKE_MAX_SPEECH_MS)
                        continue
                    trace = TurnTrace(0)
                    trace.mark(SPEECH_END, now)
                    trace.mark(TURN_CONFIRMED, now)
                    await self._start_turn(trace, pcm16=ev.pcm16)
                    continue
                if self._probe is not None:
                    if self._busy():
                        self._probe_end(ev.pcm16, now)   # STT로 맞장구인지 가른다 (백그라운드)
                        continue
                    self._clear_probe()   # 답이 이미 끝났다: 이 발화는 맞장구가 아니라 새 턴이다
                    await self._duck(False)
                if self.endpoint_mode == "smart":  # 판단·확정은 TurnEndpointer가 한다
                    await self.endpointer.on_pause(ev.pcm16, now - PAUSE_SILENCE_MS / 1000)
                    continue
                trace = TurnTrace(0)
                trace.mark(SPEECH_END, now - self.engines.vad_silence_ms / 1000)  # 실제 발화 끝
                trace.mark(TURN_CONFIRMED, now)
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
                self._last_confirm = None
                trace = TurnTrace(0)
                trace.mark(SPEECH_END)
                trace.mark(TURN_CONFIRMED)
                await self._start_turn(trace, text=text)
        elif kind == "config":
            if "barge_in" in msg:
                self.barge_in_enabled = bool(msg["barge_in"])
            if "barge_filter" in msg:
                self.barge_filter = bool(msg["barge_filter"])
                if not self.barge_filter:
                    self._clear_probe()
            if "backchannel" in msg:
                self.backchannel_enabled = bool(msg["backchannel"]) and self.engines.backchannel is not None
            if "intent" in msg:
                self.intent_enabled = bool(msg["intent"])
            for k in ("wake", "face", "show_ai_notice", "show_camera_badge", "show_conn_status", "show_dev_info"):
                if k in msg:
                    self.feature_prefs[k] = bool(msg[k])
            mode = msg.get("mode")
            if mode in ("demo", "dev"):
                self.mode = mode
                await self.send({"type": "mode_ready", "mode": self.mode})
            voice = msg.get("voice")
            if isinstance(voice, str) and voice:
                # 엔진은 공유다. 탭 하나만 쓴다는 전제 (README 참고)
                if voice == AUTO and self._voice_ids():
                    self.voice_auto = True
                elif voice in self._voice_ids():
                    self.voice_auto = False
                    self.engines.tts.voice = voice
            ep = msg.get("endpoint")
            if ep in ("smart", "fixed") and ep != self.endpoint_mode:
                self.endpointer.reset()  # 대기 중이던 턴은 버린다
                await self._abort_spec()
                self._last_confirm = None
                self.endpoint_mode = ep
                self.vad = self._new_vad()
                await self.send({"type": "endpoint_ready", "endpoint": ep})
            stt_id = msg.get("stt")
            if isinstance(stt_id, str) and stt_id:
                await self._switch_stt(stt_id)
        elif kind == "retranscribe":
            self._spawn(self._retranscribe())
        elif kind == "stop":
            await self._stop_current()
            await self.send({"type": "avatar_state", "state": "listening"})
        elif kind in ("session_start", "start"):
            play_notice = bool(msg.get("notice", True))
            await self.start_session(play_notice=play_notice)
        elif kind in ("session_end", "end"):
            await self.end_session()
        elif kind == "face_summary":
            if not self.feature_prefs.get("face", False):
                return
            if not self._accepting_face_summary or self._stage == "respond":
                log.debug("턴 종료 후(또는 응답 중) 도착한 표정 요약 버림")
                return
            self._last_face_summary = {
                "label": str(msg.get("label", "")),
                "conf": float(msg.get("conf", 0.0)),
                "coverage": float(msg.get("coverage", 0.0)),
                "n_frames": int(msg.get("n_frames", 0)),
                "total_frames": int(msg.get("total_frames", 0)),
            }
        elif kind == "reset":
            await self._stop_current()
            self.pipeline.history.clear()
            self._turn_tokens.clear()
            self._barge_events.clear()
            self._notice_played = False
            self._last_face_summary = None
            self._accepting_face_summary = True
            await self.lifecycle.reset()
            await self.send({"type": "reset_done"})

    # ---------- STT 선택 ----------

    async def _switch_stt(self, stt_id: str) -> None:
        """STT를 바꾼다. 로드하는 동안은 이전 엔진을 그대로 쓴다."""
        if not self.engines.stt_usable(stt_id):
            await self.send({"type": "error", "message": f"쓸 수 없는 STT: {stt_id}"})
            return
        if self._stt_switch is not None and not self._stt_switch.done():
            self._stt_switch.cancel()
        self._stt_switch = asyncio.get_running_loop().create_task(self._load_stt(stt_id))
        self._bg.add(self._stt_switch)
        self._stt_switch.add_done_callback(self._bg.discard)

    async def _load_stt(self, stt_id: str) -> None:
        try:
            await self.engines.get_stt(stt_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("STT 로드 실패: %s", stt_id)
            await self.send({"type": "error", "message": f"STT 로드 실패({stt_id}): {type(e).__name__}: {e}"})
            return
        self.stt_id = stt_id
        await self.send({"type": "stt_ready", "stt": stt_id})

    async def _retranscribe(self) -> None:
        """마지막 마이크 발화를 현재 STT로 다시 인식한다. 새 턴은 만들지 않는다."""
        if self._last_pcm is None:
            await self.send({"type": "error", "message": "다시 인식할 마이크 발화가 없습니다"})
            return
        stt_id = self.stt_id
        try:
            r = await self._stt().transcribe(self._last_pcm)
        except Exception as e:
            log.exception("다시 인식 오류")
            await self.send({"type": "error", "message": f"{type(e).__name__}: {e}"})
            return
        await self.send({"type": "retranscript", "stt": stt_id, "text": (r.text or "").strip(),
                         "stt_ms": round(float(r.elapsed_ms), 1)})

    # ---------- 턴 ----------

    # ---------- 선행 생성 ----------

    def _hint(self, emotion: str | None) -> tuple[str | None, str]:
        if not self.emotion_hint:
            return None, "R0"
        face_enabled = bool(self.feature_prefs.get("face", False))
        return fused_hint(emotion, self._last_face_summary, face_enabled=face_enabled)

    def _pick_opener(self) -> bytes | None:
        bank = self.engines.backchannel
        if self.backchannel_enabled and bank is not None:
            picked = bank.pick(getattr(self.engines.tts, "voice", None))
            return picked[1] if picked else None
        return None

    async def _on_speculate(self, text: str, utt_id: int, last: Any, pcm16: bytes) -> None:
        """대기 중 이미 아는 전사로 LLM·TTS를 먼저 돌린다. 확정 전에는 브라우저로 아무것도 보내지 않는다.

        응답·재생이 진행 중이면 선행하지 않는다 (barge-in 경로를 건드리지 않는다).
        """
        if self._closed or self.endpoint_mode != "smart":
            return
        if self.lifecycle.is_idle:
            return
        if self._busy() or (self._task is not None and not self._task.done()):
            return
        if self.intent_enabled:
            ires = classify_intent(text, state=self.lifecycle.state)
            if ires.is_command:
                log.debug("선행 생성 건너뜀 (명령 후보): text=%r intent=%s", text, ires.intent)
                return
        await self._abort_spec()
        emotion, event = getattr(last, "emotion", None), getattr(last, "event", None)
        tone = self._apply_tone(emotion, pcm16, text) if self._auto_active() else None
        trace = TurnTrace(0)
        hint_text, hint_rule = self._hint(emotion)
        pt = await self.pipeline.prepare(text, trace=trace, opener=self._pick_opener(), hint=hint_text)
        self._spec = _Spec(pt, text, trace, tone, emotion, event, hint_text=hint_text, hint_rule=hint_rule)  # prepare 직후, 첫 토큰보다 먼저 보관한다
        log.debug("선행 생성 시작 utt=%s turn=%s text=%r", utt_id, pt.turn_id, text)

    async def _on_speculate_cancel(self) -> None:
        sp = self._spec
        if sp is not None and not sp.claimed:
            log.debug("선행 생성 취소 turn=%s", sp.pt.turn_id)
            await self._abort_spec()

    async def _abort_spec(self) -> None:
        """보관 중인 선행 턴을 버린다. 싱크로는 아무것도 가지 않고 history는 prepare 이전으로 돌아간다."""
        sp, self._spec = self._spec, None
        if sp is not None:
            sp.held = False
            sp.deltas.clear()
            await sp.pt.abort()

    async def _release_spec_ui(self, sp: _Spec) -> None:
        """보관했던 assistant_delta를 순서대로 보낸다. 비는 것을 확인한 직후 await 없이 보관을 끈다."""
        while sp.deltas:
            tid, text = sp.deltas.pop(0)
            await self.send({"type": "assistant_delta", "turn_id": tid, "text": text})
        sp.held = False

    # ---------- barge-in 맞장구 필터 (dev-guide §6.2) ----------

    async def _duck(self, on: bool) -> None:
        now = time.monotonic()
        duck_lat = round((now - self._speech_start_time) * 1000.0, 1) if (on and self._speech_start_time is not None) else None
        ev = {
            "type": "dev_event",
            "event": "duck",
            "on": on,
            "duck_latency_ms": duck_lat,
        }
        self._barge_events.append(ev)
        await self.send({"type": "duck", "on": on})
        await self.send(ev)

    async def _probe_begin(self) -> None:
        """말하는 중에 사용자 소리가 시작됐다. 소리를 낮추고, 짧은 맞장구인지 끝까지 본다.

        PROBE_WINDOW_MS 안에 발화가 안 끝나면 끼어들기로 보고 끊는다.
        """
        if self._probe is not None:
            return
        self._probe = {"timer": asyncio.get_running_loop().create_task(self._probe_timeout())}
        await self._duck(True)

    def _clear_probe(self) -> None:
        pr, self._probe = self._probe, None
        if pr is not None:
            t = pr.get("timer")
            if t is not None and not t.done() and t is not asyncio.current_task():
                t.cancel()

    async def _probe_timeout(self) -> None:
        await asyncio.sleep(PROBE_WINDOW_MS / 1000)
        if self._probe is None or self._closed:
            return
        self._probe = None       # 길게 이어진다: 맞장구가 아니다. 이후 SpeechEnd는 보통 턴으로 처리된다
        ev = {
            "type": "dev_event",
            "event": "barge_in",
            "result": "timeout_interrupt",
            "timeout_ms": PROBE_WINDOW_MS,
        }
        self._barge_events.append(ev)
        await self.send(ev)
        await self._stop_current()
        await self._duck(False)
        if self.endpoint_mode == "smart":
            await self.endpointer.on_speech_start()

    def _probe_end(self, pcm: bytes, now: float) -> None:
        """짧게 끝난 발화: 전사해서 맞장구면 무시하고, 아니면 끼어들기로 처리한다."""
        pr = self._probe
        if pr is not None and pr.get("timer") is not None and pr["timer"] is not asyncio.current_task():
            pr["timer"].cancel()
        self._probe = None
        self._spawn(self._probe_classify(pcm, now))

    async def _probe_classify(self, pcm: bytes, now: float) -> None:
        dur_ms = max(0.0, len(pcm) / 32.0 - PRE_ROLL_MS)
        text = None
        try:
            r = await self._stt().transcribe(pcm)
            text = (r.text or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("맞장구 판별 STT 오류: 끼어들기로 본다")

        t_stt_done = time.monotonic()

        # 확인 대기 상태: 맞장구 필터보다 확인 응답 판별을 먼저 한다 (§3.4-5)
        if self.lifecycle.is_confirming:
            await self._stop_current()
            await self._duck(False)
            trace = TurnTrace(0)
            trace.mark(SPEECH_END, now)
            trace.mark(TURN_CONFIRMED, now)
            await self._start_turn(trace, pcm16=pcm, text=text)
            return

        # 말하는 중 정지 명령 (STOP_SPEAKING): 끊기만 하고 새 턴을 만들지 않는다 (§3.3-5)
        if self.intent_enabled and text:
            ires = classify_intent(text, state="speaking")
            if ires.intent == INTENT_STOP_SPEAKING:
                await self._stop_current()
                await self._duck(False)
                int_latency_ms = (time.monotonic() - t_stt_done) * 1000.0
                log.debug("정지 명령 감지: text=%r, interrupt latency=%.2fms", text, int_latency_ms)
                ev = {
                    "type": "dev_event",
                    "event": "barge_in",
                    "result": "stop_command",
                    "text": text,
                    "dur_ms": round(dur_ms, 1),
                    "int_latency_ms": round(int_latency_ms, 2),
                }
                self._barge_events.append(ev)
                await self.send(ev)
                return

        if text is not None and is_backchannel(text, dur_ms):
            log.debug("맞장구 무시: %r %.0fms", text, dur_ms)
            await self._duck(False)
            await self.send({"type": "backchannel_ignored", "text": text})
            ev = {
                "type": "dev_event",
                "event": "barge_in",
                "result": "backchannel_ignored",
                "text": text,
                "dur_ms": round(dur_ms, 1),
            }
            self._barge_events.append(ev)
            await self.send(ev)
            return
        ev = {
            "type": "dev_event",
            "event": "barge_in",
            "result": "interrupt",
            "text": text,
            "dur_ms": round(dur_ms, 1),
        }
        self._barge_events.append(ev)
        await self.send(ev)
        await self._stop_current()
        await self._duck(False)
        if self.endpoint_mode == "smart":
            await self.endpointer.on_speech_start()
            await self.endpointer.on_pause(pcm, now - PAUSE_SILENCE_MS / 1000)
        else:
            trace = TurnTrace(0)
            trace.mark(SPEECH_END, now - self.engines.vad_silence_ms / 1000)
            trace.mark(TURN_CONFIRMED, now)
            await self._start_turn(trace, pcm16=pcm)

    # ---------- 턴 중단 ----------

    async def _stop_current(self, keep_spec: _Spec | None = None) -> None:
        """진행 중 STT/응답/재생을 멈춘다. 보관 중인 선행 턴도 버린다 (keep_spec 제외)."""
        self._clear_probe()
        if self._spec is not None and self._spec is not keep_spec:
            await self._abort_spec()
        curr = asyncio.current_task()
        t = self._task
        running = t is not None and not t.done() and t is not curr
        if running and self._stage == "stt":
            t.cancel()
        if (running and self._stage == "respond") or self.sink.active:
            dead_tid = self.sink.active_turn
            played = self.sink.played_ms(dead_tid) if dead_tid is not None else 0.0
            dead = await self.pipeline.barge_in()
            actual_tid = dead or dead_tid
            if actual_tid:
                actual_played = self.sink.played_ms(actual_tid) or played or 0.0
                ev = {
                    "type": "dev_event",
                    "event": "interrupt",
                    "turn_id": actual_tid,
                    "played_ms": round(actual_played, 1),
                }
                self._barge_events.append(ev)
                await self.send(ev)
        if running:
            await asyncio.wait({t})

    async def _on_turn_confirmed(self, turn: ConfirmedTurn) -> None:
        """smart: 턴이 확정됐다. 이미 아는 전사로 응답 경로를 시작한다 (STT 생략)."""
        sp = self._spec
        if sp is not None and sp.pt.state == "pending" and sp.text == turn.text and not self._closed:
            sp.claimed = True      # 선행 결과를 쓴다
            trace = sp.trace       # 선행 시작 때 찍은 마크는 두고, 확정 기준 마크는 덮어쓴다
            trace.marks[SPEECH_END] = turn.pause_start
            trace.marks[STT_FINAL] = turn.stt_done
            trace.marks[TURN_CONFIRMED] = turn.confirm_time
            hl = sp.pt.hist_len    # user 메시지는 이미 history에 있다. 되돌릴 길이는 prepare 이전
        else:
            await self._abort_spec()   # 텍스트가 다르거나 선행이 없다. 지금 경로로 새로 시작한다
            sp, hl = None, None
            trace = TurnTrace(0)
            trace.mark(SPEECH_END, turn.pause_start)
            trace.mark(STT_FINAL, turn.stt_done)
            trace.mark(TURN_CONFIRMED, turn.confirm_time)
        self._last_pcm = turn.pcm16
        self._last_confirm = {"turn": turn, "t": time.monotonic(), "trace": trace, "hl": hl}
        await self._start_turn(trace, pcm16=turn.pcm16, text=turn.text, known=turn, spec=sp)

    async def _maybe_reopen(self) -> bool:
        """확정 직후(RESUME_MERGE_MS 안) 본 응답 소리 전에 다시 말하면 판단을 버리고 턴을 이어 붙인다."""
        lc = self._last_confirm
        if lc is None:
            return False
        if (time.monotonic() - lc["t"]) * 1000 > RESUME_MERGE_MS or TTS_FIRST_AUDIO in lc["trace"].marks:
            self._last_confirm = None  # 늦었다. 일반 barge-in 경로
            return False
        self._last_confirm = None
        await asyncio.sleep(0)  # 응답 작업이 시작할 기회를 준다 (history 길이를 기록한다)
        await self._stop_current()
        hist, hl, turn = self.pipeline.history, lc["hl"], lc["turn"]
        if hl is not None:
            del hist[hl:]  # 이 턴의 user 메시지와 부분 assistant 메시지를 지운다
        elif hist and hist[-1].get("role") == "user" and hist[-1].get("content") == turn.text:
            hist.pop()
        await self.send({"type": "avatar_state", "state": "listening"})
        await self.endpointer.reopen(turn)
        return True

    async def _start_turn(self, trace: TurnTrace, *, pcm16: bytes | None = None, text: str | None = None,
                          known: ConfirmedTurn | None = None, spec: _Spec | None = None) -> None:
        await self._stop_current(keep_spec=spec)
        if self._closed:
            await self._abort_spec()
            return
        utt_id = known.utt_id if known is not None else self._next_utt()
        self._task = asyncio.create_task(self._run_turn(trace, pcm16, text, utt_id, known, spec))

    def _apply_tone(self, emotion: str | None, pcm16: bytes | None, text: str) -> str:
        """auto: 감정·음향 특징으로 톤을 골라 이번 턴의 TTS 목소리로 쓴다. 서버에 없는 톤이면 calm."""
        tone = pick_tone(emotion, extract_features(pcm16, text))
        ids = self._voice_ids()
        if tone not in ids:
            tone = CALM if CALM in ids else getattr(self.engines.tts, "voice", None)
        self.engines.tts.voice = tone  # 엔진은 공유다. 탭 하나만 쓴다는 전제
        return tone

    async def _play_confirm_prompt(self, intent: str, payload: dict[str, Any]) -> str:
        if intent == INTENT_SESSION_END:
            key = PROMPT_CONFIRM_END
        elif intent == INTENT_RESET:
            key = PROMPT_CONFIRM_RESET
        elif intent in (INTENT_MODE_DEV, INTENT_MODE_DEMO):
            key = PROMPT_CONFIRM_MODE
        elif intent == INTENT_CAMERA_ON:
            key = PROMPT_CONFIRM_CAMERA
        elif intent == INTENT_UI_BADGE_TOGGLE and payload.get("target") == "show_ai_notice":
            key = PROMPT_CONFIRM_HIDE_AI
        else:
            key = PROMPT_CONFIRM_MODE
        await self.play_prompt(key)
        return self.prompt_bank.get_text(key) or ""

    async def _execute_confirmed_command(self, intent: str | None, payload: dict[str, Any], turn_id: int) -> str:
        if not intent:
            return ""
        if intent == INTENT_SESSION_END:
            await self.end_session(play_greeting=True)
            return self.prompt_bank.get_text(PROMPT_GREETING_END) or ""
        elif intent == INTENT_RESET:
            self.pipeline.history.clear()
            self._last_bot_text = ""
            await self.lifecycle.reset()
            await self.start_session(play_notice=True)
            return self.prompt_bank.get_text(PROMPT_NOTICE_AI) or ""
        elif intent == INTENT_MODE_DEV:
            self.mode = "dev"
            await self.send({"type": "mode_ready", "mode": self.mode})
            return ""
        elif intent == INTENT_MODE_DEMO:
            self.mode = "demo"
            await self.send({"type": "mode_ready", "mode": self.mode})
            return ""
        elif intent == INTENT_CAMERA_ON:
            self.feature_prefs["face"] = True
            await self.send({"type": "config", "face": True, "consent": True})
            return ""
        elif intent == INTENT_UI_BADGE_TOGGLE:
            target = payload.get("target")
            enabled = payload.get("enabled", True)
            if target:
                self.feature_prefs[target] = enabled
                await self.send({"type": "config", target: enabled})
            return ""
        return ""

    async def _execute_immediate_command(self, intent: str, payload: dict[str, Any], turn_id: int) -> str:
        if intent == INTENT_STOP_SPEAKING:
            await self._stop_current()
            return ""
        elif intent == INTENT_REPEAT_BOT:
            if self._last_bot_text:
                await self.pipeline.say(self._last_bot_text)
                return self._last_bot_text
            else:
                await self.play_prompt(PROMPT_REDO_EMPTY)
                return self.prompt_bank.get_text(PROMPT_REDO_EMPTY) or ""
        elif intent == INTENT_REDO_USER:
            if len(self.pipeline.history) >= 2:
                self.pipeline.history.pop()  # assistant
                self.pipeline.history.pop()  # user
                self._last_bot_text = ""
                await self.play_prompt(PROMPT_REDO_SUCCESS)
                return self.prompt_bank.get_text(PROMPT_REDO_SUCCESS) or ""
            else:
                await self.play_prompt(PROMPT_REDO_EMPTY)
                return self.prompt_bank.get_text(PROMPT_REDO_EMPTY) or ""
        elif intent == INTENT_CAMERA_OFF:
            self.feature_prefs["face"] = False
            await self.send({"type": "config", "face": False})
            return ""
        elif intent == INTENT_WAKE_ON:
            self.feature_prefs["wake"] = True
            await self.send({"type": "config", "wake": True})
            return ""
        elif intent == INTENT_WAKE_OFF:
            self.feature_prefs["wake"] = False
            await self.send({"type": "config", "wake": False})
            return ""
        elif intent == INTENT_DEV_INFO_ON:
            self.feature_prefs["show_dev_info"] = True
            await self.send({"type": "config", "show_dev_info": True})
            return ""
        elif intent == INTENT_DEV_INFO_OFF:
            self.feature_prefs["show_dev_info"] = False
            await self.send({"type": "config", "show_dev_info": False})
            return ""
        elif intent == INTENT_UI_BADGE_TOGGLE:
            target = payload.get("target")
            enabled = payload.get("enabled", True)
            if target:
                self.feature_prefs[target] = enabled
                await self.send({"type": "config", target: enabled})
            return ""
        elif intent == INTENT_SESSION_START:
            await self.start_session(play_notice=True)
            return self.prompt_bank.get_text(PROMPT_NOTICE_AI) or ""
        return ""

    def _write_dev_record(self, utt_id: int, turn_id: int, text: str, stt_id: str, stt_ms: float,
                          emotion: str | None, event: str | None, hint_rule: str,
                          intent: str, grade: str, rule: str, classify_ms: float,
                          llm_text: str, spoken_text: str, interrupted: bool, turn_trace: dict,
                          face: dict | None = None) -> None:
        if self.mode != "dev":
            return
        barge_list = list(self._barge_events)
        self._barge_events.clear()
        dev_record = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "session_id": self.session_id,
            "utt_id": utt_id,
            "turn_id": turn_id,
            "mode": self.mode,
            "transcript": text,
            "stt_id": stt_id,
            "stt_ms": round(stt_ms, 1),
            "emotion": emotion,
            "event": event,
            "face": face,
            "hint_rule": hint_rule,
            "intent": {
                "intent": intent,
                "grade": grade,
                "rule": rule,
                "classify_ms": classify_ms,
            },
            "llm_text": llm_text,
            "spoken_text": spoken_text,
            "interrupted": interrupted,
            "trace": turn_trace,
            "turn_eval_reason": self._last_turn_eval_reason,
            "barge": barge_list,
        }
        self.dev_logger.write_turn(dev_record)

    async def _run_turn(self, trace: TurnTrace, pcm16: bytes | None, text: str | None, utt_id: int,
                        known: ConfirmedTurn | None = None, spec: _Spec | None = None) -> None:
        stt_ms, emotion, event = 0.0, None, None
        stt_id = self.stt_id
        try:
            self._stage = "stt"
            if known is not None:  # 전사는 TurnEndpointer가 끝냈다
                stt_id, stt_ms = known.stt_id, known.stt_ms
                emotion, event = getattr(known.last, "emotion", None), getattr(known.last, "event", None)
            elif pcm16 is not None:
                r = await self._stt().transcribe(pcm16)
                text, stt_ms = (r.text or "").strip(), float(r.elapsed_ms)
                emotion, event = getattr(r, "emotion", None), getattr(r, "event", None)
            trace.mark(STT_FINAL)
            if not text:
                return

            t0_intent = time.monotonic()
            if self.intent_enabled:
                ires = classify_intent(text, state=self.lifecycle.state)
                intent = ires.intent
                grade = ires.grade
                rule = ires.rule
                payload = ires.payload
            else:
                intent = "TALK"
                grade = GRADE_DIALOG
                rule = "disabled"
                payload = {}
            classify_ms = round((time.monotonic() - t0_intent) * 1000.0, 2)

            # 대기(idle) 상태 마이크 발화 게이트: 허용된 명령이 아니면 즉시 무기록 폐기 (통신비밀보호법 보호 장치)
            if self.lifecycle.is_idle:
                is_allowed_idle_command = (self.intent_enabled and ires.is_command and intent in STATE_ALLOWED_INTENTS["idle"])
                if pcm16 is not None and not is_allowed_idle_command:
                    self.wake_ignored_count += 1
                    log.debug("대기 중 비명령 발화 무시 (무기록 폐기): 총 %d회", self.wake_ignored_count)
                    return

            if pcm16 is not None:
                self._last_pcm = pcm16

            hint_text, hint_rule = self._hint(emotion)
            turn_face = self._last_face_summary
            face_label = turn_face.get("label") if turn_face else None
            self._last_face_summary = None  # 확정 시점에 소비 (§4.2-5, 리뷰 #2)
            self._accepting_face_summary = False  # 이번 턴 종료까지 새 표정 요약 수신 차단

            # 확정 시점 hint가 선행 생성에 쓴 hint와 다르면 선행 생성을 버리고 확정 hint로 다시 생성 (리뷰 #2 보완)
            if spec is not None and spec.hint_text != hint_text:
                log.debug("선행 hint 불일치 (spec=%r, confirmed=%r) -> 선행 생성 취소 후 확정 hint로 재응답",
                          spec.hint_text, hint_text)
                old_tid = spec.pt.turn_id
                await spec.pt.abort()
                self._turn_tokens.pop(old_tid, None)
                if self._spec is spec:
                    self._spec = None
                spec = None

            actual_hint_rule = spec.hint_rule if spec is not None else hint_rule

            if spec is not None:   # 톤·맞장구는 선행 시작 때 이미 골랐다
                tone, turn_id = spec.tone, spec.pt.turn_id
            else:
                tone = self._apply_tone(emotion, pcm16, text) if self._auto_active() else None
                if known is not None and self._last_confirm is not None and self._last_confirm["turn"] is known:
                    self._last_confirm["hl"] = len(self.pipeline.history)
                turn_id = self.pipeline.guard.current + 1
            await self.send({"type": "transcript", "utt_id": utt_id, "turn_id": turn_id,
                             "text": text, "stt": stt_id, "stt_ms": round(stt_ms, 1),
                             "emotion": emotion, "event": event, "tone": tone})
            await self.send({
                "type": "dev_event",
                "event": "intent",
                "intent": intent,
                "grade": grade,
                "rule": rule,
                "classify_ms": classify_ms,
            })
            await self.send({
                "type": "dev_event",
                "event": "hint",
                "rule": actual_hint_rule,
                "emotion": emotion,
                "face": face_label,
            })

            # 1. 확인 대기 (confirming) 상태 처리
            if self.lifecycle.is_confirming:
                if intent == INTENT_CONFIRM_YES:
                    pending_cmd, pld = await self.lifecycle.resolve_confirm(confirmed=True)
                    spoken_text = await self._execute_confirmed_command(pending_cmd, pld, turn_id)
                    if spoken_text:
                        self._last_bot_text = spoken_text
                    await self.send({"type": "session_state", "state": self.lifecycle.state})
                    await self.send({"type": "turn_done", "utt_id": utt_id, "turn_id": turn_id,
                                     "interrupted": False, "spoken_text": spoken_text or "", "trace": {"stt_ms": round(stt_ms, 1)}})
                    self._write_dev_record(utt_id, turn_id, text, stt_id, stt_ms, emotion, event, actual_hint_rule,
                                           intent, grade, rule, classify_ms, spoken_text or "", spoken_text or "", False, {"stt_ms": round(stt_ms, 1)}, face=turn_face)
                    return
                elif intent == INTENT_CONFIRM_NO:
                    await self.lifecycle.resolve_confirm(confirmed=False)
                    await self.send({"type": "session_state", "state": self.lifecycle.state})
                    await self.send({"type": "turn_done", "utt_id": utt_id, "turn_id": turn_id,
                                     "interrupted": False, "spoken_text": "", "trace": {"stt_ms": round(stt_ms, 1)}})
                    self._write_dev_record(utt_id, turn_id, text, stt_id, stt_ms, emotion, event, actual_hint_rule,
                                           intent, grade, rule, classify_ms, "", "", False, {"stt_ms": round(stt_ms, 1)}, face=turn_face)
                    return
                else:
                    await self.lifecycle.resolve_confirm(confirmed=False)
                    await self.send({"type": "session_state", "state": self.lifecycle.state})

            # 2. 대기 (idle) 상태 처리
            if self.lifecycle.is_idle:
                if intent == INTENT_SESSION_START:
                    await self.start_session(play_notice=True)
                    prompt_text = self.prompt_bank.get_text(PROMPT_NOTICE_AI) or ""
                    self._last_bot_text = prompt_text
                    await self.send({"type": "turn_done", "utt_id": utt_id, "turn_id": turn_id,
                                     "interrupted": False, "spoken_text": prompt_text, "trace": {"stt_ms": round(stt_ms, 1)}})
                    self._write_dev_record(utt_id, turn_id, text, stt_id, stt_ms, emotion, event, actual_hint_rule,
                                           intent, grade, rule, classify_ms, prompt_text, prompt_text, False, {"stt_ms": round(stt_ms, 1)}, face=turn_face)
                    return
                elif pcm16 is None:
                    # direct text_input 하위 호환
                    await self.lifecycle.start()
                    await self.send({"type": "session_state", "state": self.lifecycle.state})
                else:
                    # 기타 대기 중 허용된 명령은 아래 명령 처리 블록으로 이동
                    pass
            else:
                await self.send({"type": "session_state", "state": self.lifecycle.state})

            # 3. 명령 처리 (is_command)
            if self.intent_enabled and ires.is_command:
                if grade == GRADE_CONFIRM:
                    await self.lifecycle.request_confirm(intent, payload=payload)
                    await self.send({"type": "session_state", "state": "confirming"})
                    prompt_text = await self._play_confirm_prompt(intent, payload)
                    self._last_bot_text = prompt_text
                    await self.send({"type": "turn_done", "utt_id": utt_id, "turn_id": turn_id,
                                     "interrupted": False, "spoken_text": prompt_text, "trace": {"stt_ms": round(stt_ms, 1)}})
                    self._write_dev_record(utt_id, turn_id, text, stt_id, stt_ms, emotion, event, actual_hint_rule,
                                           intent, grade, rule, classify_ms, prompt_text, prompt_text, False, {"stt_ms": round(stt_ms, 1)}, face=turn_face)
                    return
                elif grade == GRADE_IMMEDIATE:
                    spoken_text = await self._execute_immediate_command(intent, payload, turn_id)
                    if spoken_text:
                        self._last_bot_text = spoken_text
                    await self.send({"type": "turn_done", "utt_id": utt_id, "turn_id": turn_id,
                                     "interrupted": False, "spoken_text": spoken_text, "trace": {"stt_ms": round(stt_ms, 1)}})
                    self._write_dev_record(utt_id, turn_id, text, stt_id, stt_ms, emotion, event, actual_hint_rule,
                                           intent, grade, rule, classify_ms, spoken_text, spoken_text, False, {"stt_ms": round(stt_ms, 1)}, face=turn_face)
                    return

            # 4. 일반 대화 (LLM 경로)
            if spec is not None:
                await self._release_spec_ui(spec)
                self._stage = "respond"
                self._accepting_face_summary = False
                if spec.pt.state == "aborted":
                    res = None
                else:
                    # 확정 때 계산한 hint를 commit과 일치시킴 (§4.6, 리뷰 #2)
                    if self.pipeline.history:
                        for m in reversed(self.pipeline.history):
                            if m.get("role") == "user":
                                if hint_text:
                                    m["hint"] = hint_text
                                else:
                                    m.pop("hint", None)
                                break
                    log.debug("선행 결과 commit turn=%s", turn_id)
                    res = await spec.pt.commit()
                if res is None:
                    done_turn_id = turn_id
                    interrupted = True
                    spoken_text = ""
                    turn_trace = {"stt_ms": round(stt_ms, 1), **self._trace_ms(trace)}
                else:
                    done_turn_id = res.trace.turn_id
                    interrupted = res.interrupted
                    spoken_text = res.spoken_text
                    turn_trace = {"stt_ms": round(stt_ms, 1), **self._trace_ms(res.trace)}
            else:
                self._stage = "respond"
                self._accepting_face_summary = False
                res = await self.pipeline.respond(text, trace=trace, opener=self._pick_opener(), hint=hint_text)
                done_turn_id = res.trace.turn_id
                interrupted = res.interrupted
                spoken_text = res.spoken_text
                turn_trace = {"stt_ms": round(stt_ms, 1), **self._trace_ms(res.trace)}

            await self.send({"type": "turn_done", "utt_id": utt_id, "turn_id": done_turn_id,
                             "interrupted": interrupted, "spoken_text": spoken_text,
                             "trace": turn_trace})

            llm_text = self._turn_tokens.pop(done_turn_id, "")
            if not llm_text and spoken_text:
                llm_text = spoken_text
            if llm_text:
                self._last_bot_text = llm_text
            elif spoken_text:
                self._last_bot_text = spoken_text

            self._write_dev_record(utt_id, done_turn_id, text, stt_id, stt_ms, emotion, event, actual_hint_rule,
                                   intent, grade, rule, classify_ms, llm_text, spoken_text, interrupted, turn_trace, face=turn_face)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("턴 처리 오류")
            await self.send({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            self._stage = None
            self._accepting_face_summary = False
            self._last_face_summary = None  # 턴 완료 후 이전 턴 표정 요약 정리 (§4.2-5, §4.4-3)
            if spec is not None:
                if self._spec is spec:
                    self._spec = None
                await spec.pt.abort()   # commit하지 못한 채 끝났으면 버린다 (이미 commit됐으면 아무 일 없음)

    async def close(self) -> None:
        """WS가 닫혔다. 진행 중 작업을 정리한다."""
        self._closed = True
        self.endpointer.reset()
        try:
            await self._stop_current()
        finally:
            for t in list(self._bg):
                t.cancel()
