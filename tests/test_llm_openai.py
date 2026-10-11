"""OpenAIStreamLLM: 가짜 OpenAI 호환 SSE 서버로 이벤트 순서·요청 본문·취소를 확인한다."""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from voice_pipeline.llm.events import Done, Meta, Token
from voice_pipeline.llm.openai_stream import OpenAIStreamLLM


def _chunk(delta: dict, finish: str | None = None) -> bytes:
    obj = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode()


async def _start(handler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/v1"


async def test_stream_events_and_request_body():
    bodies: list[dict] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        bodies.append(await request.json())
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(_chunk({"role": "assistant", "content": ""}))
        await resp.write(_chunk({"reasoning_content": "생각 중"}))
        await resp.write(_chunk({"content": "네, "}))
        await resp.write(_chunk({"content": "괜찮아요."}))
        await resp.write(_chunk({}, finish="stop"))
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    runner, url = await _start(handler)
    llm = OpenAIStreamLLM(base_url=url, model="m")
    try:
        msgs = [
            {"role": "user", "content": "잠을 못 자요."},
            {"role": "assistant", "content": "그러셨군요", "interrupted": True},
            {"role": "user", "content": "네"},
        ]
        events = [ev async for ev in llm.stream(msgs, turn_id=1)]
    finally:
        await llm.close()
        await runner.cleanup()

    assert isinstance(events[0], Meta) and events[0].risk_grade == "일반"
    assert isinstance(events[-1], Done)
    toks = [e for e in events[1:-1]]
    assert all(isinstance(e, Token) for e in toks)
    assert "".join(t.text for t in toks) == "네, 괜찮아요."

    body = bodies[0]
    assert body["stream"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["role"] == "system"
    assert all("interrupted" not in m for m in body["messages"])
    assert body["messages"][2]["content"].startswith("그러셨군요")


async def test_cancel_closes_connection():
    closed = asyncio.Event()
    started = asyncio.Event()

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        try:
            for _ in range(200):
                await resp.write(_chunk({"content": "가"}))
                started.set()
                await asyncio.sleep(0.05)
        except (ConnectionResetError, asyncio.CancelledError):
            closed.set()
            raise
        return resp

    runner, url = await _start(handler)
    llm = OpenAIStreamLLM(base_url=url, model="m")
    got: list[object] = []

    async def consume() -> None:
        async for ev in llm.stream([{"role": "user", "content": "안녕"}], turn_id=1):
            got.append(ev)

    try:
        task = asyncio.create_task(consume())
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.wait([task], timeout=1.0)
        assert task.done()
        await asyncio.wait_for(closed.wait(), 3)  # 서버가 끊김을 본다
    finally:
        await llm.close()
        await runner.cleanup()
    assert any(isinstance(e, Token) for e in got)
