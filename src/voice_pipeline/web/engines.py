"""데모에 쓰는 엔진 묶음. 실제 엔진은 선택했을 때만 import한다."""

from __future__ import annotations

import inspect
import logging
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..backchannel import BackchannelBank
from ..prompts import PromptBank
from ..llm.events import LLMClient
from ..stt.registry import STT_ENGINES
from ..tts.base import TTSEngine
from .mocks import EnergyVAD


log = logging.getLogger("voice_pipeline.web")

SMART_TURN_MODEL = Path("models/smart-turn/smart-turn-v3.2-cpu.onnx")


def _default_scorer() -> Any:
    """Smart Turn 모델 파일이 있으면 지연 로드 스코어러를 만든다. 없으면 None."""
    if not SMART_TURN_MODEL.exists():
        return None
    from ..endpoint.smart_turn import SmartTurnScorer
    return SmartTurnScorer(SMART_TURN_MODEL)


@dataclass
class Engines:
    llm: LLMClient
    tts: TTSEngine
    stt_initial: Any                 # 처음 쓰는 STT. transcribe(pcm16) -> STTResult
    make_vad: Callable[[], Any]      # 세션마다 새 VAD (상태가 있다)
    labels: dict[str, str]
    first_min: int = 2               # 첫 청크 최소 글자 수
    vad_silence_ms: int = 500        # 발화 끝으로 보는 무음 길이
    backchannel: BackchannelBank | None = None  # 미리 합성한 맞장구
    stt_id: str = ""                 # 기본 STT id. 비우면 labels["stt"]
    endpoint_default: str = "smart"  # 세션 기본 턴 종료 방식: smart | fixed
    endpoint_log_dir: Path | None = None  # 턴 종료 판단 JSONL 위치. None이면 bench/results/endpoint_log
    endpoint_scorer: Any = None      # EndpointScorer (Smart Turn). None이면 p=None 경로
    speculate: bool = True           # 선행 생성: 턴 확정 대기 중 LLM·TTS를 먼저 돌린다
    barge_filter: bool = True        # 말하는 중 짧은 맞장구("네")로 답을 끊지 않는다
    emotion_hint: bool = True        # 감정 태그를 LLM 어조 지시(hint)로 쓴다
    default_mode: str = "demo"       # 세션 기본 화면 모드: demo | dev
    dev_log_dir: Path | None = None  # 개발 로그 저장 디렉터리 (None이면 logs/dev)
    dev_log_days: int = 7            # 개발 로그 보존 일수 (기본 7일)
    prompt_bank: PromptBank | None = None  # 미리 합성한 안내 음성 묶음
    _stt_cache: dict[str, Any] = field(default_factory=dict)
    _stt_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.stt_id = self.stt_id or self.labels.get("stt", "mock")
        self._stt_cache[self.stt_id] = self.stt_initial
        if self.default_mode not in ("demo", "dev"):
            self.default_mode = "demo"

    @property
    def stt(self) -> Any:
        """기본(현재) STT 엔진."""
        return self._stt_cache[self.stt_id]

    def new_vad(self, min_silence_ms: int | None = None) -> Any:
        """세션용 새 VAD. min_silence_ms를 받는 팩토리면 그 값으로, 아니면 기본값으로 만든다."""
        if min_silence_ms is not None:
            try:
                params = inspect.signature(self.make_vad).parameters
            except (TypeError, ValueError):
                params = {}
            if "min_silence_ms" in params or any(p.kind is p.VAR_KEYWORD for p in params.values()):
                return self.make_vad(min_silence_ms=min_silence_ms)
        return self.make_vad()

    def stt_options(self) -> list[dict[str, Any]]:
        """UI 목록. 등록부 엔진 + 이미 주입된 엔진."""
        out = [{"id": s.id, "label": s.label, "available": s.id in self._stt_cache or bool(s.available())}
               for s in STT_ENGINES.values()]
        if self.stt_id not in STT_ENGINES:
            out.insert(0, {"id": self.stt_id, "label": self.stt_id, "available": True})
        return out

    def stt_usable(self, stt_id: str) -> bool:
        return any(o["id"] == stt_id and o["available"] for o in self.stt_options())

    async def get_stt(self, stt_id: str) -> Any:
        """STT 엔진을 처음 쓸 때 만들고 warmup한다. 블로킹 로드는 이벤트 루프 밖에서 돈다."""
        if stt_id in self._stt_cache:
            return self._stt_cache[stt_id]
        lock = self._stt_locks.setdefault(stt_id, asyncio.Lock())
        async with lock:
            if stt_id not in self._stt_cache:
                eng = await asyncio.to_thread(STT_ENGINES[stt_id].factory)
                fn = getattr(eng, "warmup", None)
                if fn:
                    r = fn()
                    if inspect.isawaitable(r):
                        await r
                self._stt_cache[stt_id] = eng
        return self._stt_cache[stt_id]

    async def warmup(self) -> None:
        sc = self.endpoint_scorer
        if sc is not None and hasattr(sc, "warmup"):
            try:
                await asyncio.to_thread(sc.warmup)  # 모델 로드는 블로킹이다
            except Exception:
                log.exception("Smart Turn warmup 실패: p=None으로 동작")
                self.endpoint_scorer = None
        for e in (self.stt, self.tts, self.llm):
            fn = getattr(e, "warmup", None)
            if fn:
                r = fn()
                if inspect.isawaitable(r):
                    await r
            if e is self.tts and self.backchannel is not None:
                voices = [v["id"] for v in getattr(self.tts, "voices", None) or []] or [getattr(self.tts, "voice", None)]
                await self.backchannel.prepare(self.tts, voices)  # 목소리 목록은 warmup 뒤에 채워진다
            if e is self.tts and self.prompt_bank is not None:
                # 기동 시 기본 목소리 1종만 선합성 (시간 단축)
                default_voice = getattr(self.tts, "voice", None)
                await self.prompt_bank.prepare(self.tts, [default_voice])

    async def close(self) -> None:
        fn = getattr(self.llm, "close", None)
        if fn:
            r = fn()
            if inspect.isawaitable(r):
                await r


def _mock_reply(messages: list[dict]) -> str:
    last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return (f"네, \"{last[:30]}\" 라고 말씀하셨군요. 측정 결과를 같이 볼게요. "
            "오늘 혈압은 128/82mmHg로 지난주보다 조금 높아요. 요즘 잠은 잘 주무시나요?")


def build_engines(llm: str = "mock", tts: str = "mock", stt: str = "mock", *,
                  first_min: int = 2, vad_silence_ms: int = 500, backchannel: bool = True,
                  endpoint: str = "smart", speculate: bool = True, barge_filter: bool = True,
                  emotion_hint: bool = True, default_mode: str = "demo",
                  dev_log_dir: Path | None = None, dev_log_days: int = 7,
                  prompt_bank: PromptBank | None = None) -> Engines:
    labels = {"llm": llm, "tts": tts, "stt": stt}
    if prompt_bank is None:
        prompt_bank = PromptBank()
    if llm == "vllm":
        from ..llm.openai_stream import OpenAIStreamLLM
        llm_o = OpenAIStreamLLM()
    else:
        from ..llm import MockLLM
        llm_o = MockLLM(reply=_mock_reply, ttft_ms=300, tokens_per_s=40)
    if tts == "cosyvoice3":
        from ..tts.cosyvoice import CosyVoiceTTS
        tts_o = CosyVoiceTTS()
    else:
        from ..tts.mock import MockTTS
        tts_o = MockTTS()
    if stt not in STT_ENGINES:
        raise ValueError(f"알 수 없는 STT: {stt} (가능: {', '.join(STT_ENGINES)})")
    stt_o = STT_ENGINES[stt].factory()
    if stt == "mock":
        make_vad = EnergyVAD
    else:
        from ..stt import VADSegmenter
        make_vad = lambda min_silence_ms=None: VADSegmenter(min_silence_ms=min_silence_ms or vad_silence_ms)
    eng = Engines(llm_o, tts_o, stt_o, make_vad, labels, first_min, vad_silence_ms,
                  BackchannelBank() if backchannel else None, endpoint_default=endpoint,
                  speculate=speculate, barge_filter=barge_filter, emotion_hint=emotion_hint,
                  default_mode=default_mode, dev_log_dir=dev_log_dir, dev_log_days=dev_log_days,
                  prompt_bank=prompt_bank)
    if stt == "mock":
        from ..endpoint.smart_turn import MockEndpointScorer
        eng.endpoint_scorer = MockEndpointScorer(0.8)
    else:
        eng.endpoint_scorer = _default_scorer()
    return eng
