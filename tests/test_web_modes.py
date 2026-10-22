"""웹 데모 화면 모드(demo/dev) 상태 및 전환 테스트 (webapp_plan 작업 A1).

시험 3개:
1. test_default_mode_demo: 기본값 demo 확인 (--default-mode dev 설정 시 dev 확인 포함)
2. test_mode_switch: demo <-> dev 모드 전환 및 mode_ready 응답 확인
3. test_invalid_mode_ignored: 잘못된 모드 값 무시 및 상태 유지 확인
"""

import asyncio
import json
import pytest
from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.llm import MockLLM
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import Engines, build_engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT


def _make_engines(default_mode: str = "demo") -> Engines:
    return Engines(
        MockLLM(ttft_ms=20, tokens_per_s=400),
        MockTTS(first_audio_ms=10, rtf=0.5),
        MockSTT("마이크 테스트"),
        lambda: EnergyVAD(min_silence_ms=100),
        {"llm": "mock", "tts": "mock", "stt": "mock"},
        default_mode=default_mode,
    )


async def _recv_json(ws, timeout: float = 3.0) -> dict:
    msg = await asyncio.wait_for(ws.receive(), timeout)
    if msg.type == WSMsgType.TEXT:
        return json.loads(msg.data)
    raise AssertionError(f"unexpected message type: {msg.type}")


async def _until_type(ws, msg_type: str, timeout: float = 3.0) -> dict:
    t0 = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t0 < timeout:
        m = await _recv_json(ws, timeout=timeout)
        if m.get("type") == msg_type:
            return m
    raise TimeoutError(f"timed out waiting for {msg_type}")


@pytest.mark.asyncio
async def test_default_mode_demo():
    """1. 기본값 demo 확인: 인자 없는 기본 상태에서 hello.mode == 'demo', dev 지정 시 'dev'."""
    # (1) 기본 설정 (demo)
    eng_demo = _make_engines()
    assert eng_demo.default_mode == "demo"
    c_demo = TestClient(TestServer(create_app(eng_demo)))
    await c_demo.start_server()
    try:
        ws = await c_demo.ws_connect("/ws")
        hello = await _recv_json(ws)
        assert hello["type"] == "hello"
        assert hello["mode"] == "demo"
        await ws.close()
    finally:
        await c_demo.close()

    # (2) default_mode='dev' 명시 설정
    eng_dev = _make_engines(default_mode="dev")
    assert eng_dev.default_mode == "dev"
    c_dev = TestClient(TestServer(create_app(eng_dev)))
    await c_dev.start_server()
    try:
        ws = await c_dev.ws_connect("/ws")
        hello = await _recv_json(ws)
        assert hello["type"] == "hello"
        assert hello["mode"] == "dev"
        await ws.close()
    finally:
        await c_dev.close()

    # (3) build_engines 기본값 및 CLI 파서 기본값 확인
    eng_b = build_engines("mock", "mock", "mock")
    assert eng_b.default_mode == "demo"
    eng_b_dev = build_engines("mock", "mock", "mock", default_mode="dev")
    assert eng_b_dev.default_mode == "dev"

    from voice_pipeline.web.__main__ import build_parser
    parser = build_parser()
    assert parser.parse_args([]).default_mode == "demo"
    assert parser.parse_args(["--default-mode", "dev"]).default_mode == "dev"


@pytest.mark.asyncio
async def test_mode_switch():
    """2. 모드 전환: config.mode로 demo -> dev -> demo 전환 시 mode_ready 수신 확인."""
    c = TestClient(TestServer(create_app(_make_engines(default_mode="demo"))))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        hello = await _recv_json(ws)
        assert hello["mode"] == "demo"

        # demo -> dev 전환
        await ws.send_json({"type": "config", "mode": "dev"})
        m1 = await _until_type(ws, "mode_ready")
        assert m1["mode"] == "dev"

        # dev -> demo 전환
        await ws.send_json({"type": "config", "mode": "demo"})
        m2 = await _until_type(ws, "mode_ready")
        assert m2["mode"] == "demo"

        await ws.close()
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_invalid_mode_ignored():
    """3. 잘못된 값 무시: 유효하지 않은 mode는 무시하고 상태를 유지하며 mode_ready를 보내지 않음."""
    c = TestClient(TestServer(create_app(_make_engines(default_mode="demo"))))
    await c.start_server()
    try:
        ws = await c.ws_connect("/ws")
        hello = await _recv_json(ws)
        assert hello["mode"] == "demo"

        # 1) 잘못된 모드 전송 ('invalid')
        await ws.send_json({"type": "config", "mode": "invalid"})

        # ping/pong을 통해 FIFO 큐에서 mode_ready가 오지 않고 바로 pong이 오는지 확인
        await ws.send_json({"type": "ping", "t": 1001})
        pong = await _recv_json(ws)
        assert pong == {"type": "pong", "t": 1001}

        # 2) dev로 전환하여 정상 변경
        await ws.send_json({"type": "config", "mode": "dev"})
        m_dev = await _until_type(ws, "mode_ready")
        assert m_dev["mode"] == "dev"

        # 3) dev 상태에서 숫자가 들어오거나 빈 문자열, null 등 유효하지 않은 모드 전송
        for bad in ["unknown", 123, None, "", "DEMO"]:
            await ws.send_json({"type": "config", "mode": bad})
            await ws.send_json({"type": "ping", "t": 2002})
            pong = await _recv_json(ws)
            assert pong == {"type": "pong", "t": 2002}

        # 상태가 여전히 dev인지 확인: demo로 전환 요청 시 mode_ready(demo) 수신
        await ws.send_json({"type": "config", "mode": "demo"})
        m_demo = await _until_type(ws, "mode_ready")
        assert m_demo["mode"] == "demo"

        await ws.close()
    finally:
        await c.close()
