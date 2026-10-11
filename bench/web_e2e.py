"""웹 데모 전 구간 검사: 헤드리스 Chromium + 가짜 마이크(wav 파일).

브라우저 쪽 JS(마이크 worklet, 재생, playback 회신)까지 실제로 돈다.
준비: 별도 venv에 `pip install playwright && playwright install chromium`
실행: <venv>/bin/python bench/web_e2e.py <16kHz 모노 wav> [기대 턴 수]
데모 서버(scripts/run_demo.sh)가 먼저 떠 있어야 한다. 결과는 화면의 대화·지연 표다.
"""
import asyncio, sys
from playwright.async_api import async_playwright
async def main(wav):
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                                          f"--use-file-for-fake-audio-capture={wav}%noloop", "--autoplay-policy=no-user-gesture-required"])
        pg = await b.new_page()
        pg.on("console", lambda m: print("console:", m.type, m.text))
        pg.on("pageerror", lambda e: print("pageerror:", e))
        sent = []
        pg.on("websocket", lambda ws: ws.on("framesent", lambda f: sent.append(f) if isinstance(f, str) and "playback" in f else None))
        await pg.goto("http://127.0.0.1:8080/")
        await pg.click("#start")
        want = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        for _ in range(300):
            await asyncio.sleep(0.5)
            if sum(("finished" in s) or ("stopped" in s) for s in sent) >= want: break
        print("LOG:", await pg.inner_text("#log"))
        print("LAT:", await pg.inner_text("#lat"))
        print("ERR:", await pg.inner_text("#err"))
        print("BADGE:", await pg.inner_text("#badge"))
        print("playback msgs:", len(sent), sent[:2], [s for s in sent if "finished" in s or "stopped" in s][:2])
        await b.close()
asyncio.run(main(sys.argv[1]))
