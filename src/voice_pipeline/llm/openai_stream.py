"""로컬 vLLM(OpenAI 호환) 스트리밍 클라이언트.

LLMClient 프로토콜을 구현하고 events.py의 이벤트를 낸다. pipeline.py는 바꾸지 않는다.
외부 클라우드로 보내지 않는다: base_url은 로컬 서버를 가리킨다.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import aiohttp

from .events import Done, LLMEvent, Meta, Token

DEFAULT_BASE_URL = "http://127.0.0.1:8000/v1"
DEFAULT_MODEL = "Qwen/Qwen3.8-27B"

DEFAULT_SYSTEM_PROMPT = (
    "당신은 고령의 이용자와 말동무가 되어 주는 마음 건강 도우미입니다. 따뜻하고 차분한 존댓말을 씁니다. "
    "이용자의 말을 끝까지 듣고, 판단하거나 가르치려 하지 않습니다. 먼저 감정을 알아주고, 이용자가 한 말을 짧게 되짚어 줍니다. "
    # 소리 내어 읽는 말이므로 형식을 엄격히 정한다
    "답은 반드시 아주 짧은 공감 한 문장으로 시작합니다. 그 문장은 네 글자에서 열두 글자 정도로 합니다. "
    "예: \"네, 그러셨군요.\" \"많이 힘드셨겠어요.\" \"걱정되셨겠어요.\" "
    "그다음 짧은 문장 한두 개로 쉽게 말합니다. 전체는 세 문장 안으로 하고, 어려운 낱말은 쓰지 않습니다. "
    "질문은 한 번에 하나만 합니다. "
    "소리 내어 읽어 주는 말이므로 마크다운, 목록, 이모지, 느낌표, 영어는 쓰지 마세요. 숫자는 한글로 풀어 씁니다. "
    # 의료·진단 경계 (docs/LEGAL_REVIEW.md §5)
    "당신은 의사나 상담사가 아닙니다. 병명을 짐작해 말하거나, 약을 권하거나, 치료한다고 말하지 마세요. "
    "몸이나 마음이 오래 힘들다고 하면 가까운 병원이나 상담센터에 가 보시기를 부드럽게 권합니다. "
    "이용자가 죽고 싶다거나 스스로를 해치고 싶다고 하면, 말을 끊지 말고 먼저 마음을 알아준 뒤 "
    "지금 안전한지 묻고, 자살예방상담전화 일공구번에 전화할 수 있다고 알려 주세요. 당장 위험하면 일일구번을 알려 줍니다. "
    "사람이냐고 물으면 인공지능 도우미라고 솔직하게 말합니다."
)

PROMPT_NO_FILLER = (
    "당신은 고령의 이용자와 말동무가 되어 주는 마음 건강 도우미입니다. 따뜻하고 차분한 존댓말을 씁니다. "
    "이용자의 말을 끝까지 듣고, 판단하거나 가르치려 하지 않습니다. 먼저 감정을 알아주고, 이용자가 한 말을 짧게 되짚어 줍니다. "
    # 소리 내어 읽는 말이므로 형식을 엄격히 정한다
    "답은 반드시 아주 짧은 공감 한 문장으로 시작합니다. 그 문장은 네 글자에서 열두 글자 정도로 합니다. "
    "첫 문장은 \"네\", \"아\", \"음\", \"예\", \"응\" 같은 군더더기 없이 시작하세요. "
    "예: \"많이 힘드셨겠어요.\" \"걱정되셨겠어요.\" \"그러셨군요.\" "
    "그다음 짧은 문장 한두 개로 쉽게 말합니다. 전체는 세 문장 안으로 하고, 어려운 낱말은 쓰지 않습니다. "
    "질문은 한 번에 하나만 합니다. "
    "소리 내어 읽어 주는 말이므로 마크다운, 목록, 이모지, 느낌표, 영어는 쓰지 마세요. 숫자는 한글로 풀어 씁니다. "
    # 의료·진단 경계 (docs/LEGAL_REVIEW.md §5)
    "당신은 의사나 상담사가 아닙니다. 병명을 짐작해 말하거나, 약을 권하거나, 치료한다고 말하지 마세요. "
    "몸이나 마음이 오래 힘들다고 하면 가까운 병원이나 상담센터에 가 보시기를 부드럽게 권합니다. "
    "이용자가 죽고 싶다거나 스스로를 해치고 싶다고 하면, 말을 끊지 말고 먼저 마음을 알아준 뒤 "
    "지금 안전한지 묻고, 자살예방상담전화 일공구번에 전화할 수 있다고 알려 주세요. 당장 위험하면 일일구번을 알려 줍니다. "
    "사람이냐고 물으면 인공지능 도우미라고 솔직하게 말합니다."
)

DEFAULT_MAX_TOKENS = 256
FIRST_PHASE_MAX_TOKENS = 64
MIN_SECOND_PHASE_MAX_TOKENS = 192


def second_phase_max_tokens(
    first_phase_tokens: int,
    total_tokens: int = DEFAULT_MAX_TOKENS,
    min_tokens: int = MIN_SECOND_PHASE_MAX_TOKENS,
) -> int:
    """2차 요청 max_tokens: total(256) - 1차 토큰 수, 최소 192."""
    return max(min_tokens, total_tokens - first_phase_tokens)

INTERRUPTED_SUFFIX = " (사용자가 중간에 끊음)"
_KEEP_KEYS = ("role", "content")
HINT_KEY = "hint"   # 마지막 user 메시지에 달린 이번 턴 참고 (어조 지시 등). 시스템 프롬프트 뒤에 붙는다


def clean_messages(messages: list[dict]) -> list[dict]:
    """표준 키(role, content)만 남긴다. interrupted 표시는 내용 끝에 붙인다."""
    out = []
    for m in messages:
        msg = {k: m[k] for k in _KEEP_KEYS if k in m}
        if m.get("interrupted") and m.get("role") == "assistant":
            msg["content"] = str(msg.get("content", "")) + INTERRUPTED_SUFFIX
        out.append(msg)
    return out


def turn_hint(messages: list[dict]) -> str:
    """마지막 user 메시지의 이번 턴 참고(hint). 없으면 빈 문자열."""
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get(HINT_KEY) or "").strip()
    return ""


class OpenAIStreamLLM:
    """vLLM /chat/completions 스트림을 §32 이벤트로 바꾼다.

    호출 태스크가 취소되면 async with가 응답을 닫아 서버 생성도 끊긴다.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        max_tokens: int = 256,
        temperature: float = 0.7,
        extra_body: dict | None = None,
        timeout_s: float = 30.0,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.extra_body = extra_body or {}
        self.timeout = aiohttp.ClientTimeout(total=None, sock_connect=5.0, sock_read=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _system(self, messages: list[dict]) -> str:
        hint = turn_hint(messages)
        return f"{self.system_prompt} 이번 대답의 참고: {hint}" if hint else self.system_prompt

    def build_body(
        self,
        messages: list[dict],
        stream: bool = True,
        continue_final_message: bool = False,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": self._system(messages)}, *clean_messages(messages)],
            "stream": stream,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "temperature": self.temperature,
            # Qwen3.x 생각 모드 끄기: 지연에 결정적이다
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if continue_final_message:
            body["continue_final_message"] = True
            body["add_generation_prompt"] = False
        if self.extra_body:
            for k, v in self.extra_body.items():
                if k == "chat_template_kwargs" and isinstance(v, dict):
                    body["chat_template_kwargs"].update(v)
                else:
                    body[k] = v
        return body

    async def stream(
        self,
        messages: list[dict],
        turn_id: int,
        continue_final_message: bool = False,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMEvent]:
        # 실제 모델은 위험도 메타를 만들지 않는다. §32 risk 필드는 상담 LLM 팀 서버에서 온다.
        if not continue_final_message:
            yield Meta(risk_grade="일반")
        sess = await self._sess()
        body = self.build_body(
            messages,
            stream=True,
            continue_final_message=continue_final_message,
            max_tokens=max_tokens,
        )
        async with sess.post(f"{self.base_url}/chat/completions", json=body) as resp:
            if resp.status != 200:
                raise RuntimeError(f"LLM HTTP {resp.status}: {(await resp.text())[:200]}")
            async for raw in resp.content:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    choices = json.loads(data).get("choices") or []
                except json.JSONDecodeError:
                    continue
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content")  # reasoning_content는 무시
                if text:
                    yield Token(text)
        yield Done("일반")

    async def warmup(self) -> None:
        """1토큰 요청으로 첫 호출 지연(캐시·그래프 초기화)을 미리 치른다."""
        sess = await self._sess()
        body = self.build_body([{"role": "user", "content": "안녕하세요"}], stream=False)
        body["max_tokens"] = 1
        async with sess.post(f"{self.base_url}/chat/completions", json=body) as resp:
            await resp.read()
