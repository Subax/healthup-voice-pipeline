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
    "당신은 건강 스테이션에서 일하는 건강 상담사입니다. 차분하고 따뜻하고 안정된 말투로 말합니다. "
    "들뜬 말투와 느낌표는 쓰지 마세요. "
    "답은 반드시 아주 짧은 공감 한 문장으로 시작합니다. 그 문장은 네 글자에서 열두 글자 정도로 합니다. "
    "예: \"네, 그러셨군요.\" \"많이 힘드셨겠어요.\" \"걱정되셨겠어요.\" "
    "그다음 짧은 문장 한두 개로 쉽게 답합니다. 문장마다 짧게 끊어 쓰세요. "
    "소리 내어 읽어 주는 말이므로 마크다운, 목록, 이모지, 영어는 쓰지 마세요. "
    "숫자는 소리 내어 읽을 수 있게 한글로 풀어 쓰세요."
)

INTERRUPTED_SUFFIX = " (사용자가 중간에 끊음)"
_KEEP_KEYS = ("role", "content")


def clean_messages(messages: list[dict]) -> list[dict]:
    """표준 키(role, content)만 남긴다. interrupted 표시는 내용 끝에 붙인다."""
    out = []
    for m in messages:
        msg = {k: m[k] for k in _KEEP_KEYS if k in m}
        if m.get("interrupted") and m.get("role") == "assistant":
            msg["content"] = str(msg.get("content", "")) + INTERRUPTED_SUFFIX
        out.append(msg)
    return out


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

    def build_body(self, messages: list[dict], stream: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": self.system_prompt}, *clean_messages(messages)],
            "stream": stream,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            # Qwen3.x 생각 모드 끄기: 지연에 결정적이다
            "chat_template_kwargs": {"enable_thinking": False},
        }
        body.update(self.extra_body)
        return body

    async def stream(self, messages: list[dict], turn_id: int) -> AsyncIterator[LLMEvent]:
        # 실제 모델은 위험도 메타를 만들지 않는다. §32 risk 필드는 상담 LLM 팀 서버에서 온다.
        yield Meta(risk_grade="일반")
        sess = await self._sess()
        async with sess.post(f"{self.base_url}/chat/completions", json=self.build_body(messages)) as resp:
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
