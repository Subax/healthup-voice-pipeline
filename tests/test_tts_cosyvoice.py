"""CosyVoiceTTS 어댑터 테스트. GPU 없이 가짜 aiohttp 서버로 검증한다."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from voice_pipeline.tts.base import TTSEngine
from voice_pipeline.tts.cosyvoice import CosyVoiceTTS

PCM = bytes(range(256)) * 8  # 2048 바이트
# 홀수 길이로 나눠 보낸다 (샘플 경계가 어긋나는 경우)
SPLITS = [1, 300, 701, 3, 1043]
assert sum(SPLITS) == len(PCM)


def make_app(forever: bool = False, voices: bool = True) -> web.Application:
    state = {"closed": asyncio.Event(), "bodies": []}

    async def info(_):
        return web.json_response({"sample_rate": 24000, "model": "fake"})

    async def tts(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        assert body["text"]
        state["bodies"].append(body)
        resp = web.StreamResponse()
        await resp.prepare(request)
        try:
            if forever:
                while True:
                    await resp.write(b"\x01\x00" * 480)
                    await asyncio.sleep(0.01)
            pos = 0
            for n in SPLITS:
                await resp.write(PCM[pos:pos + n])
                pos += n
                await asyncio.sleep(0.01)
            await resp.write_eof()
        finally:
            state["closed"].set()
        return resp

    async def voices_h(_):
        return web.json_response({"default": "calm", "voices": [{"id": "calm", "label": "차분하게"}]})

    app = web.Application()
    app["state"] = state
    if voices:
        app.router.add_get("/voices", voices_h)
    app.router.add_get("/info", info)
    app.router.add_post("/tts", tts)
    return app


async def test_warmup_sets_sample_rate_and_protocol():
    async with TestServer(make_app()) as server:
        tts = CosyVoiceTTS(url=str(server.make_url("")))
        assert isinstance(tts, TTSEngine)
        await tts.warmup()
        assert tts.sample_rate == 24000
        await tts.aclose()


async def test_stream_even_chunks_and_total_bytes():
    async with TestServer(make_app()) as server:
        tts = CosyVoiceTTS(url=str(server.make_url("")))
        await tts.warmup()
        chunks = [c async for c in tts.stream("안녕하세요")]
        assert chunks and all(len(c) % 2 == 0 for c in chunks)
        assert b"".join(chunks) == PCM
        await tts.aclose()


async def test_cancel_stops_stream():
    app = make_app(forever=True)
    async with TestServer(app) as server:
        tts = CosyVoiceTTS(url=str(server.make_url("")))
        await tts.warmup()
        n = 0
        async for _ in tts.stream("긴 문장"):
            n += 1
            if n == 3:
                await tts.cancel()
        assert n < 100  # 무한 스트림이 끊겼다
        await asyncio.wait_for(app["state"]["closed"].wait(), 2.0)  # 서버도 끊김을 알아챘다
        await tts.aclose()


async def test_voice_passed_and_voices_parsed():
    app = make_app()
    async with TestServer(app) as server:
        tts = CosyVoiceTTS(url=str(server.make_url("")))
        tts.voice = "calm2"
        await tts.warmup()
        assert tts.voices == [{"id": "calm", "label": "차분하게"}]
        _ = [c async for c in tts.stream("안녕하세요")]
        assert app["state"]["bodies"][-1] == {"text": "안녕하세요", "voice": "calm2"}
        tts.voice = None
        _ = [c async for c in tts.stream("안녕하세요")]
        assert "voice" not in app["state"]["bodies"][-1]
        await tts.aclose()


async def test_warmup_tolerates_missing_voices_endpoint():
    async with TestServer(make_app(voices=False)) as server:
        tts = CosyVoiceTTS(url=str(server.make_url("")))
        await tts.warmup()
        assert tts.voices == []
        await tts.aclose()
