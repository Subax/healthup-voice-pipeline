"""aiohttp 앱: 정적 페이지 + /ws (탭 하나 = DemoSession 하나)."""

from __future__ import annotations

import hmac
import json
import logging
import socket
from pathlib import Path

from aiohttp import WSMsgType, web

from .engines import Engines
from .session import DemoSession

ENGINES = web.AppKey("engines", Engines)
AUTH_TOKEN = web.AppKey("auth_token", str)
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("voice_pipeline.web")


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def _pong(ws: web.WebSocketResponse, raw: str) -> bool:
    """RTT 측정용 ping에 바로 답한다. 세션에는 넘기지 않는다."""
    if '"ping"' not in raw:
        return False
    try:
        m = json.loads(raw)
    except ValueError:
        return False
    if not isinstance(m, dict) or m.get("type") != "ping":
        return False
    await ws.send_str(json.dumps({"type": "pong", "t": m.get("t")}))
    return True


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    token = request.app.get(AUTH_TOKEN)
    if token is not None and not hmac.compare_digest(request.query.get("t", "").encode(), token.encode()):
        raise web.HTTPUnauthorized(text="인증 실패")
    # PCM은 압축이 안 되고 deflate는 CPU·지연만 더한다
    ws = web.WebSocketResponse(heartbeat=20, compress=False)
    await ws.prepare(request)
    sock = request.transport.get_extra_info("socket") if request.transport else None
    if sock is not None:  # aiohttp가 이미 켜 두지만 명시한다
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
    session = DemoSession(ws, request.app[ENGINES])
    await session.send_hello()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await session.on_mic(msg.data)
            elif msg.type == WSMsgType.TEXT:
                if await _pong(ws, msg.data):
                    continue
                await session.on_json(msg.data)
    except Exception:
        log.exception("세션 오류")
    finally:
        await session.close()
    return ws


def create_app(engines: Engines, *, warmup: bool = True, auth_token: str | None = None) -> web.Application:
    app = web.Application()
    app[ENGINES] = engines
    if auth_token:
        app[AUTH_TOKEN] = auth_token

    async def on_startup(app: web.Application) -> None:
        if warmup:
            await engines.warmup()

    async def on_cleanup(app: web.Application) -> None:
        await engines.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC, append_version=False)
    return app
