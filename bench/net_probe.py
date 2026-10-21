"""데모 서버 네트워크 점검: ping RTT, text_input → 첫 오디오 프레임, 프레임 도착 간격.

서버는 `--llm mock --tts mock --stt mock`로 띄운다.
예: .venv/bin/python bench/net_probe.py --url https://192.168.0.10:8080/ws --token T --insecure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import statistics
import time

import aiohttp


def pct(v: list[float], p: float) -> float:
    s = sorted(v)
    return s[min(len(s) - 1, int(len(s) * p))] if s else float("nan")


async def main(a: argparse.Namespace) -> None:
    url = a.url + (("&" if "?" in a.url else "?") + "t=" + a.token if a.token else "")
    ssl_arg = False if a.insecure else None
    if a.cacert:
        ssl_arg = ssl.create_default_context(cafile=a.cacert)
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(url, ssl=ssl_arg, compress=0) as ws:
            while (await ws.receive(timeout=10)).type != aiohttp.WSMsgType.TEXT:
                pass  # hello
            rtts = []
            for _ in range(a.pings):
                t0 = time.perf_counter()
                await ws.send_str(json.dumps({"type": "ping", "t": t0}))
                while True:
                    m = await ws.receive(timeout=5)
                    if m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("type") == "pong":
                        break
                rtts.append((time.perf_counter() - t0) * 1000)
                await asyncio.sleep(0.05)
            print(f"ping RTT ({len(rtts)}회): P50 {pct(rtts, .5):.2f}ms  P90 {pct(rtts, .9):.2f}ms  최대 {max(rtts):.2f}ms")

            t0 = time.perf_counter()
            await ws.send_str(json.dumps({"type": "text_input", "text": a.text}))
            arrivals: list[float] = []
            while True:
                m = await ws.receive(timeout=30)
                now = time.perf_counter()
                if m.type == aiohttp.WSMsgType.BINARY:
                    arrivals.append(now)
                elif m.type == aiohttp.WSMsgType.TEXT and json.loads(m.data).get("type") == "turn_done":
                    break
                elif m.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    raise SystemExit("ws가 닫혔다")
    if not arrivals:
        raise SystemExit("오디오 프레임이 없다")
    gaps = [(b - c) * 1000 for c, b in zip(arrivals, arrivals[1:])]
    print(f"text_input → 첫 오디오 프레임: {(arrivals[0] - t0) * 1000:.1f}ms (프레임 {len(arrivals)}개)")
    if gaps:
        print(f"프레임 도착 간격: P50 {pct(gaps, .5):.2f}ms  P90 {pct(gaps, .9):.2f}ms  최대 {max(gaps):.2f}ms  평균 {statistics.fmean(gaps):.2f}ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="데모 서버 네트워크 점검")
    ap.add_argument("--url", default="ws://127.0.0.1:8080/ws")
    ap.add_argument("--token", default=None)
    ap.add_argument("--insecure", action="store_true", help="인증서 검증을 끈다")
    ap.add_argument("--cacert", default=None, help="CA 인증서(ca.pem)")
    ap.add_argument("--pings", type=int, default=20)
    ap.add_argument("--text", default="혈압이 높을 때 어떻게 해야 하나요")
    asyncio.run(main(ap.parse_args()))
