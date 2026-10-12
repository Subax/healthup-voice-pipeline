"""데모에 쓰는 엔진 묶음. 실제 엔진은 선택했을 때만 import한다."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Callable

from ..llm.events import LLMClient
from ..tts.base import TTSEngine
from .mocks import EnergyVAD, MockSTT


@dataclass
class Engines:
    llm: LLMClient
    tts: TTSEngine
    stt: Any                         # transcribe(pcm16) -> STTResult
    make_vad: Callable[[], Any]      # 세션마다 새 VAD (상태가 있다)
    labels: dict[str, str]
    first_min: int = 2               # 첫 청크 최소 글자 수
    vad_silence_ms: int = 500        # 발화 끝으로 보는 무음 길이

    async def warmup(self) -> None:
        for e in (self.stt, self.tts, self.llm):
            fn = getattr(e, "warmup", None)
            if fn:
                r = fn()
                if inspect.isawaitable(r):
                    await r

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
                  first_min: int = 2, vad_silence_ms: int = 500) -> Engines:
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
    if stt == "sensevoice":
        from ..stt import SenseVoiceSTT, VADSegmenter
        stt_o, make_vad = SenseVoiceSTT(), (lambda: VADSegmenter(min_silence_ms=vad_silence_ms))
    else:
        stt_o, make_vad = MockSTT(), EnergyVAD
    return Engines(llm_o, tts_o, stt_o, make_vad, labels, first_min, vad_silence_ms)
