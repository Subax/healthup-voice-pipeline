"""aiohttp 앱: 정적 페이지 + /ws (탭 하나 = DemoSession 하나)."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import socket
from pathlib import Path

from aiohttp import WSMsgType, web

from .devlog import DEFAULT_DEV_LOG_DIR, daily_purge_loop, purge_old_logs
from .engines import Engines
from .session import DemoSession

ENGINES = web.AppKey("engines", Engines)
AUTH_TOKEN = web.AppKey("auth_token", str)
SESSIONS = web.AppKey("sessions", set)
STATIC = Path(__file__).parent / "static"
log = logging.getLogger("voice_pipeline.web")

# 페이지가 부르는 정적 파일. 쿼리의 수정 시각이 바뀌면 터널 캐시를 피한다.
INDEX_ASSETS = (
    "/static/app.css",
    "/static/vendor/face-api/face-api.js",
    "/static/face-hud.js",
    "/static/app.js",
)


def asset_version(path: Path) -> str:
    """파일 수정 시각(초). 파일이 없으면 0."""
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return "0"


def stamp_index_html(html: str, static_dir: Path = STATIC) -> str:
    """index.html의 정적 주소에 ?v=수정시각 을 붙인다."""
    for url in sorted(INDEX_ASSETS, key=len, reverse=True):
        rel = url.removeprefix("/static/")
        ver = asset_version(static_dir / rel)
        html = html.replace(f'"{url}"', f'"{url}?v={ver}"', 1)
    return html


@web.middleware
async def _no_store_static(request: web.Request, handler):
    """페이지와 /static/ 은 저장하지 않는다."""
    response = await handler(request)
    if request.path == "/" or request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


async def index(request: web.Request) -> web.Response:
    raw = (STATIC / "index.html").read_text(encoding="utf-8")
    body = stamp_index_html(raw)
    return web.Response(
        text=body,
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


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
    request.app[SESSIONS].add(session)
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
        request.app[SESSIONS].discard(session)
        await session.close()
    return ws


def create_app(engines: Engines, *, warmup: bool = True, auth_token: str | None = None) -> web.Application:
    app = web.Application(middlewares=[_no_store_static])
    app[ENGINES] = engines
    app[SESSIONS] = set()
    if auth_token:
        app[AUTH_TOKEN] = auth_token

    purge_task: asyncio.Task | None = None

    async def on_startup(app: web.Application) -> None:
        nonlocal purge_task
        base_dir = engines.dev_log_dir or DEFAULT_DEV_LOG_DIR
        purge_old_logs(base_dir, engines.dev_log_days)
        purge_task = asyncio.create_task(daily_purge_loop(base_dir, engines.dev_log_days))
        if warmup:
            await engines.warmup()

    async def on_cleanup(app: web.Application) -> None:
        nonlocal purge_task
        if purge_task is not None and not purge_task.done():
            purge_task.cancel()
        await engines.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC, append_version=False)
    return app
