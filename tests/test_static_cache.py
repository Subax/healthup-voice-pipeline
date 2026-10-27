"""페이지 정적 파일은 수정 시각 쿼리와 Cache-Control: no-store 로 준다."""

from __future__ import annotations

import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from voice_pipeline.llm import MockLLM
from voice_pipeline.tts.mock import MockTTS
from voice_pipeline.web.app import INDEX_ASSETS, STATIC, asset_version, create_app, stamp_index_html
from voice_pipeline.web.engines import Engines
from voice_pipeline.web.mocks import EnergyVAD, MockSTT


def test_stamp_index_html_uses_mtime():
    raw = (STATIC / "index.html").read_text(encoding="utf-8")
    stamped = stamp_index_html(raw)
    for url in INDEX_ASSETS:
        rel = url.removeprefix("/static/")
        ver = asset_version(STATIC / rel)
        assert f'"{url}?v={ver}"' in stamped
        assert f'"{url}"' not in stamped


def test_missing_asset_version_is_zero(tmp_path):
    assert asset_version(tmp_path / "없는파일.js") == "0"


@pytest_asyncio.fixture
async def client():
    eng = Engines(
        MockLLM(),
        MockTTS(),
        MockSTT("안녕"),
        lambda: EnergyVAD(),
        {"llm": "mock", "tts": "mock", "stt": "mock"},
    )
    c = TestClient(TestServer(create_app(eng, warmup=False)))
    await c.start_server()
    yield c
    await c.close()


async def test_index_and_static_send_no_store(client):
    page = await client.get("/")
    assert page.status == 200
    assert page.headers["Cache-Control"] == "no-store"
    text = await page.text()
    for url in INDEX_ASSETS:
        rel = url.removeprefix("/static/")
        ver = asset_version(STATIC / rel)
        assert f"{url}?v={ver}" in text

    for url in INDEX_ASSETS:
        resp = await client.get(url)
        assert resp.status == 200, url
        assert resp.headers["Cache-Control"] == "no-store"
