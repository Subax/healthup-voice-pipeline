"""미리 합성한 안내 음성 묶음 (PromptBank).

법적 고지(AI 마음 건강 도우미, AI 기본법 §31), 시작·종료 인사, 확인 질문 5종,
액션 피드백(취소 완료, 재요청) 등을 기동 시 선합성하여 지연 없이 즉시 재생한다.
기동 시간 단축을 위해 기동 시에는 기본 목소리 1종만 선합성한다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .backchannel import apply_fades, trim_silence

if TYPE_CHECKING:
    from .tts.base import TTSEngine

log = logging.getLogger("voice_pipeline.prompts")

# 프롬프트 키 상수
PROMPT_NOTICE_AI = "notice_ai"
PROMPT_GREETING_START = "greeting_start"
PROMPT_GREETING_END = "greeting_end"
PROMPT_CONFIRM_END = "confirm_end"
PROMPT_CONFIRM_RESET = "confirm_reset"
PROMPT_CONFIRM_MODE = "confirm_mode"
PROMPT_CONFIRM_CAMERA = "confirm_camera"
PROMPT_CONFIRM_HIDE_AI = "confirm_hide_ai"
PROMPT_REDO_SUCCESS = "redo_success"
PROMPT_REDO_EMPTY = "redo_empty"
PROMPT_REPROMPT = "reprompt"

# 기본 안내 문구 목록
# 규칙 4(에코 대비): 확인 질문 문장에 "네·아니요"를 넣지 않는다.
DEFAULT_PROMPTS: dict[str, str] = {
    PROMPT_NOTICE_AI: "안녕하세요. AI 마음 건강 도우미입니다. 편하게 말씀해 주세요.",
    PROMPT_GREETING_START: "안녕하세요. 대화를 시작합니다. 편하게 말씀해 주세요.",
    PROMPT_GREETING_END: "대화를 종료합니다. 좋은 하루 보내세요.",
    PROMPT_CONFIRM_END: "대화를 마칠까요?",
    PROMPT_CONFIRM_RESET: "처음부터 다시 할까요?",
    PROMPT_CONFIRM_MODE: "모드를 변경할까요?",
    PROMPT_CONFIRM_CAMERA: "표정으로 기분을 짐작해 대답에 참고합니다. 카메라를 켜도 될까요?",
    PROMPT_CONFIRM_HIDE_AI: "AI 기본법에 따른 법적 고지입니다. 숨기시겠습니까?",
    PROMPT_REDO_SUCCESS: "네, 방금 말씀은 지웠어요. 다시 말씀해 주세요.",
    PROMPT_REDO_EMPTY: "아직 주고받은 대화가 없어요. 편하게 말씀해 주세요.",
    PROMPT_REPROMPT: "잘 듣지 못했어요. 다시 말씀해 주세요.",
}

# 별칭 매핑 (의도 이름이나 단축 키)
KEY_ALIASES: dict[str, str] = {
    "notice": PROMPT_NOTICE_AI,
    "notice_ai": PROMPT_NOTICE_AI,
    "ai_notice": PROMPT_NOTICE_AI,
    "greeting_start": PROMPT_GREETING_START,
    "greeting_end": PROMPT_GREETING_END,
    "start": PROMPT_GREETING_START,
    "end": PROMPT_GREETING_END,
    "SESSION_START": PROMPT_GREETING_START,
    "SESSION_END": PROMPT_CONFIRM_END,
    "confirm_end": PROMPT_CONFIRM_END,
    "confirm_session_end": PROMPT_CONFIRM_END,
    "RESET": PROMPT_CONFIRM_RESET,
    "confirm_reset": PROMPT_CONFIRM_RESET,
    "MODE_DEV": PROMPT_CONFIRM_MODE,
    "MODE_DEMO": PROMPT_CONFIRM_MODE,
    "confirm_mode": PROMPT_CONFIRM_MODE,
    "confirm_mode_switch": PROMPT_CONFIRM_MODE,
    "CAMERA_ON": PROMPT_CONFIRM_CAMERA,
    "confirm_camera": PROMPT_CONFIRM_CAMERA,
    "confirm_camera_on": PROMPT_CONFIRM_CAMERA,
    "HIDE_AI_NOTICE": PROMPT_CONFIRM_HIDE_AI,
    "confirm_hide_ai": PROMPT_CONFIRM_HIDE_AI,
    "REDO_USER": PROMPT_REDO_SUCCESS,
    "redo_success": PROMPT_REDO_SUCCESS,
    "redo_empty": PROMPT_REDO_EMPTY,
    "reprompt": PROMPT_REPROMPT,
}

MIN_PROMPT_MS = 100.0
MAX_PROMPT_MS = 20000.0


class PromptBank:
    """미리 합성한 안내 음성 묶음.

    기동 시 기본 목소리 1종만 선합성하여 기동 시간을 단축하고,
    세션 중 즉각적인 음성 안내를 재생할 수 있도록 한다.
    """

    def __init__(
        self,
        prompts: dict[str, str] | None = None,
        *,
        sample_rate: int = 24000,
        min_ms: float = MIN_PROMPT_MS,
        max_ms: float = MAX_PROMPT_MS,
    ):
        self.prompts = dict(prompts or DEFAULT_PROMPTS)
        self.sample_rate = sample_rate
        self.min_ms = min_ms
        self.max_ms = max_ms
        self._cache: dict[tuple[str | None, str], bytes] = {}
        self.discarded_count: int = 0
        self.discarded_items: list[tuple[str | None, str, float]] = []

    def canonical_key(self, key: str) -> str:
        return KEY_ALIASES.get(key, key)

    def get_text(self, key: str) -> str | None:
        canon = self.canonical_key(key)
        return self.prompts.get(canon)

    def has_audio(self, key: str, voice: str | None = None) -> bool:
        canon = self.canonical_key(key)
        if (voice, canon) in self._cache:
            return True
        if (None, canon) in self._cache:
            return True
        return any(k[1] == canon for k in self._cache)

    def get_audio(self, key: str, voice: str | None = None) -> bytes | None:
        canon = self.canonical_key(key)
        if (voice, canon) in self._cache:
            return self._cache[(voice, canon)]
        if (None, canon) in self._cache:
            return self._cache[(None, canon)]
        # fallback to any cached voice for this prompt
        for (v, k), pcm in self._cache.items():
            if k == canon:
                return pcm
        return None

    async def prepare(self, tts: TTSEngine, voices: list[str | None]) -> None:
        """지정된 voices에 대해 모든 프롬프트를 합성하여 캐시한다.

        기동 시에는 기본 목소리 1종만 선합성하여 시간을 단축한다.
        """
        self.sample_rate = tts.sample_rate
        has_voice = hasattr(tts, "voice")
        orig_voice = getattr(tts, "voice", None)
        try:
            for voice in voices:
                if has_voice and voice is not None:
                    tts.voice = voice
                vkey = voice if has_voice else None
                for key, text in self.prompts.items():
                    try:
                        pcm = b"".join([c async for c in tts.stream(text)])
                        pcm = apply_fades(trim_silence(pcm, tts.sample_rate), tts.sample_rate)
                    except Exception:
                        log.exception("안내 음성 합성 실패: voice=%s key=%s text=%r", voice, key, text)
                        continue
                    ms = len(pcm) // 2 * 1000 / tts.sample_rate
                    if not (self.min_ms <= ms <= self.max_ms):
                        self.discarded_count += 1
                        self.discarded_items.append((voice, key, ms))
                        log.warning("안내 음성 길이 필터로 버림: voice=%s key=%s %.0fms", voice, key, ms)
                        continue
                    self._cache[(vkey, key)] = pcm
        finally:
            if has_voice:
                tts.voice = orig_voice
