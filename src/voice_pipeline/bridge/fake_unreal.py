"""가짜 Unreal 서버. 실제 Unreal 없이 브리지 프로토콜을 시험한다.

프레임을 실시간 속도로 "재생"하고 started / progress / stopped / finished를 회신한다.
Unreal 담당이 구현할 동작의 참고 구현이기도 하다.

실행 예:  python -m voice_pipeline.bridge.fake_unreal --port 8765 --solver-delay-ms 80
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

from aiohttp import WSMsgType, web

from .protocol import unpack_frame


class FakeUnreal:
    def __init__(self, solver_delay_ms: float = 0.0, progress_ms: float = 100.0):
        self.solver_delay_ms = solver_delay_ms  # 립싱크 처리 지연만큼 재생을 늦춘다 (§7.4)
        self.progress_ms = progress_ms
        self.log: list[tuple[float, dict]] = []

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        queues: dict[int, asyncio.Queue] = {}
        players: dict[int, asyncio.Task] = {}
        rates: dict[int, int] = {}

        async def play(tid: int) -> None:
            q, played, last_report, started = queues[tid], 0.0, 0.0, False
            while True:
                pcm, is_last = await q.get()
                if pcm is None:
                    break
                if not started:
                    started = True
                    # 첫 프레임을 받은 뒤 립싱크 처리 지연만큼 기다렸다가 재생한다
                    await asyncio.sleep(self.solver_delay_ms / 1000.0)
                    await ws.send_str(json.dumps({"type": "playback", "turn_id": tid,
                                                  "event": "started", "played_ms": 0.0}))
                dur = len(pcm) / 2 / rates.get(tid, 24000) * 1000.0
                await asyncio.sleep(dur / 1000.0)
                played += dur
                if played - last_report >= self.progress_ms:
                    last_report = played
                    await ws.send_str(json.dumps({"type": "playback", "turn_id": tid,
                                                  "event": "progress", "played_ms": played}))
                if is_last:
                    break
            await ws.send_str(json.dumps({"type": "playback", "turn_id": tid,
                                          "event": "finished", "played_ms": played}))

        async for msg in ws:
            now = time.monotonic()
            if msg.type == WSMsgType.BINARY:
                tid, seq, is_last, pcm = unpack_frame(msg.data)
                if tid in queues:
                    queues[tid].put_nowait((pcm, is_last))
            elif msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                self.log.append((now, data))
                kind = data.get("type")
                if kind == "audio_begin":
                    tid = int(data["turn_id"])
                    rates[tid] = int(data.get("sample_rate", 24000))
                    queues[tid] = asyncio.Queue()
                    players[tid] = asyncio.create_task(play(tid))
                elif kind == "interrupt":
                    tid = int(data["turn_id"])
                    task = players.pop(tid, None)
                    if task:
                        task.cancel()
                    queues.pop(tid, None)
                    await ws.send_str(json.dumps({"type": "playback", "turn_id": tid,
                                                  "event": "stopped", "played_ms": -1}))
        return ws

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/ws", self.handle)
        return app


def main() -> None:
    ap = argparse.ArgumentParser(description="가짜 Unreal 재생 서버")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--solver-delay-ms", type=float, default=0.0)
    args = ap.parse_args()
    web.run_app(FakeUnreal(args.solver_delay_ms).app(), port=args.port)


if __name__ == "__main__":
    main()
