"""aiohttp 앱: 정적 페이지 + /ws (탭 하나 = DemoSession 하나)."""

from __future__ import annotations

import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from .engines import Engines
from .session import DemoSession

ENGINES = web.AppKey("engines", Engines)
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("voice_pipeline.web")


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    session = DemoSession(ws, request.app[ENGINES])
    await session.send_hello()
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await session.on_mic(msg.data)
            elif msg.type == WSMsgType.TEXT:
                await session.on_json(msg.data)
    except Exception:
        log.exception("세션 오류")
    finally:
        await session.close()
    return ws


def create_app(engines: Engines, *, warmup: bool = True) -> web.Application:
    app = web.Application()
    app[ENGINES] = engines

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
