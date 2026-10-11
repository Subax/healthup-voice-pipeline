import asyncio

from aiohttp.test_utils import TestServer

from voice_pipeline.bridge import UnrealBridgeSink, pack_frame, unpack_frame
from voice_pipeline.bridge.fake_unreal import FakeUnreal
from voice_pipeline.llm import Done, Meta, MockLLM, Token
from voice_pipeline.llm.sse import SSELLMClient, make_app
from voice_pipeline.pipeline import VoicePipeline
from voice_pipeline.trace import PLAYBACK_START, TTS_FIRST_AUDIO


def test_frame_roundtrip():
    data = pack_frame(7, 42, b"\x01\x02" * 10, is_last=True)
    assert unpack_frame(data) == (7, 42, True, b"\x01\x02" * 10)


async def test_sse_mock_server_and_client():
    server = TestServer(make_app(MockLLM(reply="네, 좋아요.", ttft_ms=10, tokens_per_s=500)))
    await server.start_server()
    client = SSELLMClient(str(server.make_url("")))
    try:
        evs = [ev async for ev in client.stream([{"role": "user", "content": "안녕"}], turn_id=1)]
    finally:
        await client.close()
        await server.close()
    assert isinstance(evs[0], Meta) and isinstance(evs[-1], Done)
    assert "".join(e.text for e in evs if isinstance(e, Token)) == "네, 좋아요."


async def test_pipeline_through_fake_unreal(fast_tts):
    fake = FakeUnreal(solver_delay_ms=30)
    server = TestServer(fake.app())
    await server.start_server()
    sink = UnrealBridgeSink(str(server.make_url("/ws")))
    await sink.connect()
    try:
        llm = MockLLM(reply="네, 확인했어요. 오늘 혈압은 정상이에요.", ttft_ms=20, tokens_per_s=400)
        p = VoicePipeline(llm, fast_tts, sink)
        r = await p.respond("결과")
        for _ in range(400):  # 가짜 Unreal이 실시간으로 다 "재생"할 때까지
            if any(d.get("type") == "audio_end" for _, d in fake.log) and sink.played_ms(r.trace.turn_id):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)
    finally:
        await sink.close()
        await server.close()
    tr = r.trace
    assert tr.since(PLAYBACK_START) is not None
    assert tr.since(PLAYBACK_START) >= tr.since(TTS_FIRST_AUDIO) + 25  # 솔버 지연이 재생 시작에 반영
    kinds = [d["type"] for _, d in fake.log]
    assert kinds[0] == "avatar_state" and "audio_begin" in kinds and "audio_end" in kinds
