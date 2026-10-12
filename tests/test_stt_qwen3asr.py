"""Qwen3-ASR 어댑터 테스트 (가짜 aiohttp 서버)."""

from __future__ import annotations

import io
import wave

import pytest
from aiohttp import web

from voice_pipeline.stt.qwen3asr import Qwen3ASRSTT, parse_output, to_wav
from voice_pipeline.stt.registry import STT_ENGINES


def test_parse_output_strips_prefix_and_tags():
    assert parse_output("language Korean<asr_text>안녕하세요.") == "안녕하세요."
    assert parse_output("  <asr_text>네</asr_text> ") == "네"
    assert parse_output("") == ""


def test_to_wav_roundtrip():
    with wave.open(io.BytesIO(to_wav(bytes(3200))), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getnframes()) == (16000, 1, 1600)


@pytest.fixture
async def fake_url(aiohttp_unused=None):
    seen: dict = {}

    async def handler(req: web.Request) -> web.Response:
        form = await req.post()
        seen["language"] = form.get("language")
        seen["wav"] = form["file"].file.read()
        return web.json_response({"text": "language Korean<asr_text>嗯 안녕 하세요。"})

    app = web.Application()
    app.router.add_post("/v1/audio/transcriptions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", seen
    await runner.cleanup()


async def test_transcribe_korean_only(fake_url):
    url, seen = fake_url
    stt = Qwen3ASRSTT(url=url)
    r = await stt.transcribe(bytes(3200))
    await stt.close()
    assert r.text == "안녕 하세요."
    assert r.lang == "ko" and r.emotion is None and r.event is None
    assert r.elapsed_ms > 0
    assert seen["language"] == "ko"
    assert seen["wav"][:4] == b"RIFF"


def test_registry_entry_and_unavailable_when_down(monkeypatch):
    spec = STT_ENGINES["qwen3asr"]
    assert spec.label == "Qwen3-ASR-1.7B (GPU)"
    monkeypatch.setenv("QWEN3ASR_URL", "http://127.0.0.1:1")
    assert spec.available() is False
    assert spec.factory().name == "qwen3-asr-1.7b"
