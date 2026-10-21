"""원격(LAN) 접속 테스트: 호스트 규칙, 토큰 인증, ping/pong, 압축 끔."""

import asyncio
import json
import sys

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.llm import MockLLM
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web import __main__ as web_main
from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT

TOKEN = "s3cret-token"


def make_engines() -> Engines:
    return Engines(MockLLM(ttft_ms=20, tokens_per_s=400), MockTTS(first_audio_ms=10, rtf=0.5),
                   MockSTT("마이크 테스트"), lambda: EnergyVAD(min_silence_ms=100), {"llm": "mock", "tts": "mock", "stt": "mock"})


@pytest_asyncio.fixture
async def client():
    c = TestClient(TestServer(create_app(make_engines(), auth_token=TOKEN)))
    await c.start_server()
    yield c
    await c.close()


@pytest.mark.parametrize("extra", [[], ["--tls-cert", "a.pem", "--tls-key", "a.key"], ["--auth-token", "x"]])
def test_non_loopback_needs_tls_and_token(monkeypatch, capsys, extra):
    monkeypatch.delenv("DEMO_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", ["web", "--host", "0.0.0.0", *extra])
    with pytest.raises(SystemExit) as e:
        web_main.main()
    assert e.value.code == 2
    assert "TLS" in capsys.readouterr().err


def test_tls_cert_without_key_rejected(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["web", "--tls-cert", "a.pem"])
    with pytest.raises(SystemExit):
        web_main.main()


def test_is_loopback():
    assert all(web_main.is_loopback(h) for h in ("127.0.0.1", "localhost", "::1"))
    assert not any(web_main.is_loopback(h) for h in ("0.0.0.0", "192.168.0.10"))


async def test_wrong_token_401(client):
    for path in ("/ws", "/ws?t=nope"):
        with pytest.raises(WSServerHandshakeError) as e:
            await client.ws_connect(path)
        assert e.value.status == 401


async def test_right_token_connects_and_static_open(client):
    ws = await client.ws_connect(f"/ws?t={TOKEN}")
    assert json.loads((await ws.receive()).data)["type"] == "hello"
    await ws.close()
    assert (await client.get("/")).status == 200


async def test_ping_pong(client):
    ws = await client.ws_connect(f"/ws?t={TOKEN}")
    await ws.receive()  # hello
    await ws.send_str(json.dumps({"type": "ping", "t": 123.5}))
    msg = await asyncio.wait_for(ws.receive(), 5)
    assert msg.type == WSMsgType.TEXT and json.loads(msg.data) == {"type": "pong", "t": 123.5}
    await ws.close()


async def test_no_auth_when_token_unset():
    c = TestClient(TestServer(create_app(make_engines())))
    await c.start_server()
    ws = await c.ws_connect("/ws")
    assert json.loads((await ws.receive()).data)["type"] == "hello"
    await ws.close()
    await c.close()


async def test_compress_disabled(client):
    ws = await client.ws_connect(f"/ws?t={TOKEN}")
    await ws.receive()
    assert "permessage-deflate" not in ws._response.headers.get("Sec-WebSocket-Extensions", "")
    await ws.close()
