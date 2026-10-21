"""턴 종료 상태기계 (dev-guide §3.1, 2차).

VAD가 짧은 멈춤(약 200ms)을 알리면 멈춤마다 다음을 한다.
1. 새 오디오를 STT로 전사한다 (세션당 한 번에 하나, 밀리면 합쳐서 한 번에)
2. 턴 전체 오디오로 Smart Turn 점수 p를 낸다
3. Kiwi로 전사 끝 어미를 분류하고 `decide()`로 대기 시간을 정한다
4. 대기 중 말이 다시 시작되면 취소하고 계속 쌓는다. 대기가 끝나면 턴을 확정한다

기준 시각은 `pause_start` (실제 발화 끝 = VAD SpeechEnd 시각 - VAD 최소 무음)이다.
확정 시한 = pause_start + wait_ms. 시한이 이미 지났으면(STT가 느렸다) 바로 확정한다.
대기는 `MAX_SILENCE_MS`를 넘지 않으므로 이 상한은 `decide()`가 지킨다.

말 재개 신호는 둘이다. VoiceOnset(약 100ms 만에 옴, 잡음일 수 있음)은 대기 타이머를 멈춘다.
SpeechStart(약 250ms 필요, 확실함)는 재개를 확정한다. VoiceOnset 뒤 `ONSET_NOISE_MS` 안에
SpeechStart·SpeechEnd가 없으면 잡음으로 보고, 멈춘 시점에 남았던 대기 시간만큼을 그 시각부터 다시 기다린다.

STT가 멈춘 경우: 마지막 멈춤으로부터 `STT_HARD_TIMEOUT_MS`(3초)까지는 STT를 기다린다
(상한 1초를 넘겨도 전사가 곧 오면 그것을 쓰기 위해서다). 3초가 지나도 응답이 없으면
STT 호출을 취소하고, 그때까지 얻은 전사로 확정한다. 전사가 하나도 없으면 턴을 버린다.
취소해도 엔진 안의 스레드 작업은 멈추지 않을 수 있다 (확인하지 않았다).

선행 생성 훅 (선택): `on_speculate(text, utt_id, last_result, pcm16)`는 대기에 들어간 직후,
남은 대기가 `SPECULATE_MIN_REMAINING_MS` 이상이고 판단 규칙이 `SPECULATE_RULES`(말이 끝났을 가능성이 높은 규칙)일 때만 부른다.
`on_speculate_cancel()`은 재개·reset·새 멈춤으로 텍스트가 달라질 때·dropped일 때 한 번만 부른다.
확정(`_confirm`)으로 넘어가면 취소를 부르지 않는다. 확정 콜백이 선행 결과를 쓰거나 버린다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable

from .ending import Ending, classify_ending
from .rules import MAX_SILENCE_MS, decide

log = logging.getLogger("voice_pipeline.endpoint")

DEFAULT_LOG_DIR = Path(__file__).resolve().parents[3] / "bench" / "results" / "endpoint_log"
SCORE_TIMEOUT_S = 0.3        # Smart Turn 점수 제한 시간. 넘으면 p=None
STT_HARD_TIMEOUT_MS = 3000   # 멈춤 뒤 STT 응답을 기다리는 최대 시간
RESUMED_REASON = "대기 중 다시 말함"
REOPEN_REASON = "확정 직후 다시 말함 → 같은 턴으로 이어 붙임"
ONSET_NOISE_MS = 400         # VoiceOnset 뒤 SpeechStart·SpeechEnd가 없으면 잡음으로 본다
SPECULATE_MIN_REMAINING_MS = 60   # 남은 대기가 이보다 짧으면 선행하지 않는다
# 말이 끝났을 가능성이 높은 규칙만 선행한다. 연결 어미·조사·머뭇거림(EC, NOUN, JX_JK, IC)은 말이 이어지기 쉽다.
SPECULATE_RULES = frozenset({"EF_HIGH", "EF_LOW", "EC_YO_HIGH", "EC_YO_LOW", "NONE_HIGH"})


@dataclass
class SegmentInfo:
    """STT 요청 하나의 결과. 밀린 멈춤은 한 요청으로 합쳐지므로 n_pauses가 1보다 클 수 있다."""

    text: str
    stt_ms: float
    n_pauses: int
    pcm_bytes: int
    emotion: str | None = None
    event: str | None = None


@dataclass
class ConfirmedTurn:
    utt_id: int
    text: str
    pcm16: bytes                 # 턴 전체 오디오 (구간 순서대로)
    last: Any                    # 마지막 STTResult (감정·이벤트용). 없으면 None
    pause_start: float           # 마지막 멈춤의 실제 발화 끝 (단조 시각)
    stt_done: float              # 마지막 전사가 끝난 시각
    confirm_time: float
    stt_ms: float                # 마지막 전사 시간
    stt_id: str
    reason: str
    rule_id: str
    segments: list[SegmentInfo] = field(default_factory=list)


def _resolve(x: Any, attr: str | None = None) -> Any:
    """값 또는 인자 없는 함수를 받아 값을 돌려준다. attr 메서드가 있으면 객체로 본다."""
    if callable(x) and not (attr and hasattr(x, attr)):
        return x()
    return x


class TurnEndpointer:
    def __init__(self, stt: Any, scorer: Any, emit: Callable[[dict], Awaitable[None]],
                 on_confirm: Callable[[ConfirmedTurn], Awaitable[None]], *,
                 stt_id: Any = "", mode: Any = "smart", log_dir: Path | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 utt_id_factory: Callable[[], int] | None = None,
                 score_timeout_s: float = SCORE_TIMEOUT_S,
                 stt_hard_timeout_ms: int = STT_HARD_TIMEOUT_MS,
                 on_speculate: Callable[[str, int, Any, bytes], Awaitable[None]] | None = None,
                 on_speculate_cancel: Callable[[], Awaitable[None]] | None = None) -> None:
        self._on_spec, self._on_spec_cancel = on_speculate, on_speculate_cancel
        self._spec_text: str | None = None   # 지금 선행 중인 텍스트. None이면 선행 없음
        self._spec_tasks: set[asyncio.Task] = set()
        self._stt, self._scorer = stt, scorer
        self._emit_cb, self._on_confirm = emit, on_confirm
        self._stt_id, self._mode, self._log_dir = stt_id, mode, log_dir
        self._clock = clock
        self._seq_utt = 0
        self._utt_factory = utt_id_factory or self._default_utt
        self._score_timeout = score_timeout_s
        self._hard_ms = stt_hard_timeout_ms
        self._speaking = False
        self._timer: asyncio.Task | None = None
        self._cap: asyncio.Task | None = None
        self._worker: asyncio.Task | None = None
        self._onset: asyncio.Task | None = None
        self._reset_turn()

    def _default_utt(self) -> int:
        self._seq_utt += 1
        return self._seq_utt

    def _reset_turn(self) -> None:
        self._utt: int | None = None
        self._audio: list[bytes] = []        # 턴 전체 구간
        self._pending: list[bytes] = []      # 아직 전사하지 않은 구간
        self._segs: list[SegmentInfo] = []
        self._last_result: Any = None
        self._stt_done: float | None = None
        self._pause_start: float | None = None
        self._seq = 0                        # 멈춤 번호 (낡은 평가를 버리는 데 쓴다)
        self._eval: dict | None = None
        self._eval_seq = -1
        self._deadline: float | None = None   # 대기 타이머의 확정 시한
        self._susp_left = 0.0                 # VoiceOnset으로 멈췄을 때 남은 대기(초)
        self._onset_only = False              # VoiceOnset만 받았고 SpeechStart는 아직이다

    # ---------- 입력 ----------

    async def on_pause(self, segment_pcm: bytes, pause_start: float) -> None:
        """VAD가 구간 하나를 끝냈다. 바로 돌아온다 (전사·판단은 백그라운드)."""
        self._speaking = False
        if self._utt is None:
            self._utt = self._utt_factory()
        self._audio.append(segment_pcm)
        self._pending.append(segment_pcm)
        self._pause_start = pause_start
        self._seq += 1
        self._eval = None
        self._cancel(self._timer)
        self._timer = None
        self._clear_onset()
        self._arm_cap()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.get_running_loop().create_task(self._run())

    async def on_speech_start(self) -> None:
        """말이 다시 시작됐다. 대기 중이면 취소하고 계속 쌓는다."""
        self._speaking = True
        self._clear_onset()
        if self._utt is None:
            return
        self._cancel(self._cap)
        self._cap = None
        if self._timer is not None and not self._timer.done():
            self._cancel(self._timer)
            self._timer = None
            await self._emit(self._event("resumed", reason=RESUMED_REASON, rule_id="RESUMED"))
        await self._spec_cancel()

    async def on_voice_onset(self) -> None:
        """말소리가 막 시작된 것 같다 (SpeechStart보다 빠른 신호, 잡음일 수 있다).

        대기 타이머를 멈춘다. ONSET_NOISE_MS 안에 SpeechStart·SpeechEnd가 없으면 잡음으로 보고
        남은 대기 시간으로 타이머를 다시 켠다.
        """
        if self._utt is None:
            self._speaking = True
            return
        if self._onset_only:  # 이미 의심 중이다
            return
        self._speaking = True
        self._onset_only = True
        self._cancel(self._cap)
        self._cap = None
        if self._timer is not None and not self._timer.done():
            self._susp_left = max(0.0, (self._deadline or self._clock()) - self._clock())
            self._cancel(self._timer)
            self._timer = None
            await self._emit(self._event("resumed", reason=RESUMED_REASON, rule_id="RESUMED"))
        await self._spec_cancel()
        self._cancel(self._onset)
        self._onset = asyncio.get_running_loop().create_task(self._onset_watch(self._seq))

    def _clear_onset(self) -> None:
        self._cancel(self._onset)
        self._onset = None
        self._onset_only = False

    async def _onset_watch(self, seq: int) -> None:
        await asyncio.sleep(ONSET_NOISE_MS / 1000)
        if seq != self._seq or not self._onset_only or self._utt is None:
            return
        self._onset_only = False
        self._speaking = False
        self._onset = None
        if self._pending or (self._worker is not None and not self._worker.done()):
            self._arm_cap()  # STT 작업자가 곧 평가한다
            return
        if self._eval is not None and self._eval_seq == seq:
            left = self._susp_left or self._eval.get("wait_ms", 0) / 1000
            self._deadline = self._clock() + left
            wait_ms = self._eval.get("wait_ms", 0)
            await self._emit(self._event("waiting", since=max(0.0, wait_ms - left * 1000)))
            self._cancel(self._timer)
            self._timer = asyncio.get_running_loop().create_task(self._wait_then_confirm(self._deadline, seq))
            await self._maybe_speculate(self._eval.get("rule_id", ""))
        else:  # 재개한 턴(reopen)은 평가가 없다. 다시 평가한다
            stt_ms = self._segs[-1].stt_ms if self._segs else 0.0
            self._worker = asyncio.get_running_loop().create_task(self._evaluate(self._utt, seq, stt_ms))

    async def reopen(self, turn: ConfirmedTurn, reason: str = REOPEN_REASON) -> None:
        """방금 확정한 턴을 되살린다. 구간·전사·오디오를 그대로 두고 다음 멈춤에서 다시 판단한다."""
        self.reset()
        self._utt, self._audio = turn.utt_id, [turn.pcm16]
        self._segs, self._last_result = list(turn.segments), turn.last
        self._stt_done, self._pause_start = turn.stt_done, turn.pause_start
        self._seq = 1
        self._speaking = True
        await self._emit(self._event("reopened", reason=reason, rule_id="REOPENED"))

    def reset(self) -> None:
        """진행 중인 턴을 버린다 (모드 전환·세션 종료)."""
        for t in (self._timer, self._cap, self._worker, self._onset):
            self._cancel(t)
        self._timer = self._cap = self._worker = self._onset = None
        self._spec_cancel_nowait()
        self._reset_turn()

    # ---------- 선행 생성 ----------

    async def _spec_cancel(self) -> None:
        if self._spec_text is None:
            return
        self._spec_text = None
        if self._on_spec_cancel is not None:
            try:
                await self._on_spec_cancel()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("선행 취소 콜백 오류")

    def _spec_cancel_nowait(self) -> None:
        """동기 문맥(reset)에서 취소한다. 취소 콜백은 백그라운드 작업으로 돈다."""
        if self._spec_text is None:
            return
        try:
            t = asyncio.get_running_loop().create_task(self._spec_cancel())
        except RuntimeError:
            self._spec_text = None
            return
        self._spec_tasks.add(t)
        t.add_done_callback(self._spec_tasks.discard)

    async def _maybe_speculate(self, rule_id: str) -> None:
        """대기에 들어간 직후 부른다. 조건이 맞으면 on_speculate, 달라졌으면 이전 선행을 취소한다."""
        text = self._text()
        remain_ms = ((self._deadline or self._clock()) - self._clock()) * 1000
        ok = (self._on_spec is not None and bool(text) and rule_id in SPECULATE_RULES
              and remain_ms >= SPECULATE_MIN_REMAINING_MS)
        if self._spec_text is not None and (not ok or self._spec_text != text):
            await self._spec_cancel()
        if not ok or self._spec_text is not None:
            return
        self._spec_text = text
        try:
            await self._on_spec(text, self._utt or 0, self._last_result, b"".join(self._audio))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("선행 콜백 오류")
            self._spec_text = None

    # ---------- 내부 ----------

    @staticmethod
    def _cancel(t: asyncio.Task | None) -> None:
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()

    def _text(self) -> str:
        return " ".join(s.text for s in self._segs if s.text)

    def _arm_cap(self) -> None:
        """STT가 멈춘 경우를 위한 시한 타이머."""
        self._cancel(self._cap)
        delay = max(0.0, self._pause_start + self._hard_ms / 1000 - self._clock())
        self._cap = asyncio.get_running_loop().create_task(self._hard_timeout(self._seq, delay))

    async def _hard_timeout(self, seq: int, delay: float) -> None:
        await asyncio.sleep(delay)
        if seq != self._seq or self._eval_seq == seq or self._speaking or self._utt is None:
            return
        log.warning("STT 응답 없음 %dms: 지금까지의 전사로 확정", self._hard_ms)
        self._cancel(self._worker)
        await self._confirm(f"STT 응답 없음 {self._hard_ms}ms 도달", "STT_TIMEOUT")

    async def _run(self) -> None:
        """STT 작업자: 세션당 하나. 밀린 구간은 합쳐서 한 번에 전사한다."""
        while self._pending:
            audio = b"".join(self._pending)
            n = len(self._pending)
            self._pending.clear()
            utt, seq = self._utt, self._seq
            t0 = self._clock()
            try:
                stt = _resolve(self._stt, "transcribe")
                r = await stt.transcribe(audio)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("STT 오류")
                r = None
            if utt != self._utt:  # 그 사이 턴이 바뀌었다
                return
            ms = float(getattr(r, "elapsed_ms", 0.0) or 0.0) or (self._clock() - t0) * 1000
            text = ((getattr(r, "text", "") or "").strip()) if r is not None else ""
            self._segs.append(SegmentInfo(text, ms, n, len(audio), getattr(r, "emotion", None),
                                          getattr(r, "event", None)))
            if r is not None:
                self._last_result = r
            self._stt_done = self._clock()
            if self._pending:  # 더 새 멈춤이 있다. 그 전사까지 끝난 뒤에 판단한다
                continue
            await self._evaluate(utt, seq, ms)

    async def _score(self, audio: bytes) -> float | None:
        if self._scorer is None:
            return None
        try:
            p = await asyncio.wait_for(asyncio.to_thread(self._scorer.score, audio), self._score_timeout)
            return float(p)
        except asyncio.TimeoutError:
            log.warning("Smart Turn 시간 초과(%.0fms): p=None", self._score_timeout * 1000)
        except Exception:
            log.exception("Smart Turn 오류: p=None")
        return None

    async def _evaluate(self, utt: int | None, seq: int, stt_ms: float) -> None:
        text = self._text()
        p = await self._score(b"".join(self._audio))
        ending = await asyncio.to_thread(classify_ending, text)
        if utt != self._utt or seq != self._seq or self._pending:
            return  # 평가 도중 새 멈춤이 왔다. 다음 평가가 대신한다
        d = decide(ending, p)
        self._eval = {"ending": ending, "p": p, "wait_ms": d.wait_ms, "rule_id": d.rule_id,
                      "reason": d.reason_ko, "stt_ms": round(stt_ms, 1)}
        self._eval_seq = seq
        if self._speaking:  # 판단이 끝나기 전에 이미 다시 말하고 있다
            self._susp_left = d.wait_ms / 1000
            await self._emit(self._event("resumed", reason=RESUMED_REASON, rule_id="RESUMED"))
            await self._spec_cancel()
            return
        now = self._clock()
        deadline = self._pause_start + d.wait_ms / 1000
        if now >= deadline:  # STT가 느려 시한이 지났다
            if (now - self._pause_start) * 1000 >= MAX_SILENCE_MS:
                await self._confirm(f"최대 무음 {MAX_SILENCE_MS}ms 도달", "MAX_SILENCE")
            else:
                await self._confirm(d.reason_ko, d.rule_id)
            return
        self._deadline = deadline
        await self._emit(self._event("waiting"))
        self._cancel(self._timer)
        self._timer = asyncio.get_running_loop().create_task(self._wait_then_confirm(deadline, seq))
        await self._maybe_speculate(d.rule_id)

    async def _wait_then_confirm(self, deadline: float, seq: int) -> None:
        await asyncio.sleep(max(0.0, deadline - self._clock()))
        if seq != self._seq or self._speaking or self._utt is None:
            return
        e = self._eval or {}
        await self._confirm(e.get("reason", ""), e.get("rule_id", ""))

    async def _confirm(self, reason: str, rule_id: str) -> None:
        if self._utt is None:
            return
        text = self._text()
        now = self._clock()
        last = self._last_result
        turn = ConfirmedTurn(
            self._utt, text, b"".join(self._audio), last, self._pause_start or now,
            self._stt_done or now, now, self._segs[-1].stt_ms if self._segs else 0.0,
            str(_resolve(self._stt_id)), reason, rule_id, list(self._segs))
        if text:
            ev = self._event("confirmed", reason=reason, rule_id=rule_id)
        else:
            ev = self._event("dropped", reason="전사 없음 → 턴 버림", rule_id="EMPTY")
        for t in (self._timer, self._cap, self._onset):
            self._cancel(t)
        self._timer = self._cap = self._onset = None
        if text:
            self._spec_text = None   # 취소를 부르지 않는다. 확정 콜백이 선행 결과를 쓰거나 버린다
        else:
            await self._spec_cancel()
        self._reset_turn()  # 콜백을 기다리는 동안 들어오는 멈춤은 새 턴이다
        await self._emit(ev)
        if text:
            await self._on_confirm(turn)

    # ---------- 이벤트·로그 ----------

    def _event(self, stage: str, *, reason: str | None = None, rule_id: str | None = None,
               since: float | None = None) -> dict:
        e = self._eval or {}
        ending: Ending = e.get("ending") or Ending("NONE")
        if since is None:
            since = (self._clock() - self._pause_start) * 1000 if self._pause_start is not None else 0.0
        return {"type": "turn_eval", "utt_id": self._utt, "stage": stage, "text": self._text(),
                "ending": {"category": ending.category, "morph": ending.morph,
                           "tags": [list(t) for t in ending.tags]},
                "stt_punct": ending.stt_punct, "smart_turn_p": e.get("p"),
                "wait_ms": e.get("wait_ms", 0), "rule_id": rule_id if rule_id is not None else e.get("rule_id", ""),
                "reason": reason if reason is not None else e.get("reason", ""),
                "stt_ms": e.get("stt_ms", self._segs[-1].stt_ms if self._segs else 0.0),
                "stt_id": str(_resolve(self._stt_id)), "since_pause_ms": round(since, 1)}

    async def _emit(self, ev: dict) -> None:
        self._write_log(ev)
        try:
            await self._emit_cb(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("turn_eval 전송 오류")

    def _write_log(self, ev: dict) -> None:
        """평가 이벤트를 하루 단위 JSONL로 남긴다. 규칙 조정에 쓴다."""
        try:
            d = Path(self._log_dir) if self._log_dir is not None else DEFAULT_LOG_DIR
            d.mkdir(parents=True, exist_ok=True)
            now = datetime.now()
            rec = {"ts": now.isoformat(timespec="milliseconds"), **ev, "stt_engine": ev["stt_id"],
                   "mode": str(_resolve(self._mode))}
            with open(d / f"{now:%Y%m%d}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("endpoint 로그 쓰기 실패")
