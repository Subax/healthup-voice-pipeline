"""데모에 쓰는 엔진 묶음. 실제 엔진은 선택했을 때만 import한다."""

from __future__ import annotations

import inspect
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from ..backchannel import BackchannelBank
from ..llm.events import LLMClient
from ..stt.registry import STT_ENGINES
from ..tts.base import TTSEngine
from .mocks import EnergyVAD


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
    _stt_cache: dict[str, Any] = field(default_factory=dict)
    _stt_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.stt_id = self.stt_id or self.labels.get("stt", "mock")
        self._stt_cache[self.stt_id] = self.stt_initial

    @property
    def stt(self) -> Any:
        """기본(현재) STT 엔진."""
        return self._stt_cache[self.stt_id]

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
        for e in (self.stt, self.tts, self.llm):
            fn = getattr(e, "warmup", None)
            if fn:
                r = fn()
                if inspect.isawaitable(r):
                    await r
            if e is self.tts and self.backchannel is not None:
                voices = [v["id"] for v in getattr(self.tts, "voices", None) or []] or [getattr(self.tts, "voice", None)]
                await self.backchannel.prepare(self.tts, voices)  # 목소리 목록은 warmup 뒤에 채워진다

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
                  first_min: int = 2, vad_silence_ms: int = 500, backchannel: bool = True) -> Engines:
    labels = {"llm": llm, "tts": tts, "stt": stt}
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
        make_vad = lambda: VADSegmenter(min_silence_ms=vad_silence_ms)
    return Engines(llm_o, tts_o, stt_o, make_vad, labels, first_min, vad_silence_ms,
                   BackchannelBank() if backchannel else None)
