"""§32 계약을 흉내 낸 SSE 서버(모의)와 클라이언트.

와이어 형식 (임시, 정본 확인 전):
    POST /v1/counsel/stream  {"session_id", "turn_id", "messages", ...}
    응답: text/event-stream
        event: meta        data: {"risk_grade": "일반", "emotion": ..., "gesture": ...}
        event: token       data: {"text": "..."}
        event: risk_update data: {"risk_grade": "119"}
        event: done        data: {"risk_grade": "일반"}

실행 예:  python -m voice_pipeline.llm.sse --port 8090 --ttft-ms 400 --tps 40
"""

from __future__ import annotations

import argparse
import json
from typing import AsyncIterator

import aiohttp
from aiohttp import web

from .events import Done, LLMEvent, Meta, RiskUpdate, Token
from .mock import MockLLM

PATH = "/v1/counsel/stream"


def _encode(ev: LLMEvent) -> bytes:
    if isinstance(ev, Meta):
        name, data = "meta", {"risk_grade": ev.risk_grade, "emotion": ev.emotion, "gesture": ev.gesture}
    elif isinstance(ev, Token):
        name, data = "token", {"text": ev.text}
    elif isinstance(ev, RiskUpdate):
        name, data = "risk_update", {"risk_grade": ev.risk_grade}
    else:
        name, data = "done", {"risk_grade": ev.risk_grade}
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _decode(name: str, data: dict) -> LLMEvent | None:
    if name == "meta":
        return Meta(risk_grade=data.get("risk_grade", "일반"), emotion=data.get("emotion"),
                    gesture=data.get("gesture"))
    if name == "token":
        return Token(data.get("text", ""))
    if name == "risk_update":
        return RiskUpdate(data["risk_grade"])
    if name == "done":
        return Done(risk_grade=data.get("risk_grade", "일반"))
    return None


def make_app(llm: MockLLM) -> web.Application:
    async def handler(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(request)
        async for ev in llm.stream(body.get("messages", []), int(body.get("turn_id", 0))):
            await resp.write(_encode(ev))
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post(PATH, handler)
    return app


class SSELLMClient:
    """SSE로 상담 LLM을 부른다. 호출 태스크가 취소되면 연결이 닫혀 서버 생성도 끊긴다."""

    def __init__(self, base_url: str, session_id: str = "station-1", timeout_s: float = 30.0):
        self.url = base_url.rstrip("/") + PATH
        self.session_id = session_id
        self.timeout = aiohttp.ClientTimeout(total=None, sock_read=timeout_s)
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def stream(self, messages: list[dict], turn_id: int) -> AsyncIterator[LLMEvent]:
        sess = await self._sess()
        payload = {"session_id": self.session_id, "turn_id": turn_id, "messages": messages}
        async with sess.post(self.url, json=payload) as resp:
            resp.raise_for_status()
            name, data_lines = None, []
            async for raw in resp.content:
                line = raw.decode("utf-8").rstrip("\r\n")
                if line.startswith("event:"):
                    name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and name:
                    ev = _decode(name, json.loads("\n".join(data_lines) or "{}"))
                    name, data_lines = None, []
                    if ev is not None:
                        yield ev
                        if isinstance(ev, Done):
                            return


def main() -> None:
    ap = argparse.ArgumentParser(description="모의 상담 LLM SSE 서버")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--ttft-ms", type=float, default=400.0)
    ap.add_argument("--tps", type=float, default=40.0)
    ap.add_argument("--risk-at-token", type=int, default=None)
    args = ap.parse_args()
    llm = MockLLM(ttft_ms=args.ttft_ms, tokens_per_s=args.tps, risk_at_token=args.risk_at_token)
    web.run_app(make_app(llm), port=args.port)


if __name__ == "__main__":
    main()
