"""F1 표정 모델 후보 3종 headless Playwright 실측 검증 스크립트.

가짜 카메라(--use-fake-device-for-media-stream) 환경에서
로컬 모델 및 벤더 파일 기반으로 [실제] 모드로 로드되는지 확인하고
결과를 bench/results/face_probe_fake_cam.txt에 저장한다.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import time

from aiohttp import web
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from voice_pipeline.web.app import create_app
from voice_pipeline.web.engines import build_engines


async def run_probe_server(port: int = 8796) -> tuple[web.AppRunner, web.TCPSite]:
    engines = build_engines("mock", "mock", "mock", default_mode="demo")
    app = create_app(engines)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, site


async def main():
    port = 8796
    base_url = f"http://127.0.0.1:{port}/static/face-probe.html"
    print("=" * 70)
    print("  F1 Face Probe Headless Playwright 실측 검사 시작")
    print(f"  서버 주소: {base_url}")
    print("=" * 70)

    runner, site = await run_probe_server(port)

    console_logs: list[str] = []
    page_errors: list[str] = []

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=os.environ.get("CHROME_PATH") or None,
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            context = await browser.new_context(permissions=["camera"])
            page = await context.new_page()

            page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            print("1. 페이지 로딩 중...")
            await page.goto(base_url)
            await page.wait_for_selector("#btnConsentOn")

            # 벤더 런타임 객체 확인
            print("2. 벤더 런타임 객체 전역 노출 확인...")
            runtimes = await page.evaluate("""() => {
                return {
                    hasFilesetResolver: typeof window.FilesetResolver !== 'undefined',
                    hasFaceLandmarker: typeof window.FaceLandmarker !== 'undefined',
                    hasOrt: typeof window.ort !== 'undefined',
                    hasFaceapi: typeof window.faceapi !== 'undefined'
                };
            }""")
            print(f"  런타임 상태: {runtimes}")

            # 카메라 동의 및 켜기
            print("3. 카메라 동의 및 활성화...")
            await page.click("#btnConsentOn")
            await asyncio.sleep(1.0)

            cam_badge = await page.text_content("#cameraStatusBadge")
            print(f"  카메라 배지 텍스트: {cam_badge.strip()}")

            # 측정 프레임 수를 30프레임으로 설정 (옵션: 30, 50, 100)
            await page.select_option("#selFrameCount", "30")

            # 후보 3종 일괄 측정 실행
            print("4. 후보 3종 일괄 측정 시작 (#btnRunAll 클릭)...")
            await page.click("#btnRunAll")

            # 벤치마크 완료 대기 (최대 120초)
            t0 = time.time()
            done = False
            last_p = ""
            await asyncio.sleep(1.0)
            while time.time() - t0 < 120:
                is_disabled = await page.is_disabled("#btnRunAll")
                progress_text = await page.text_content("#benchProgressText")
                percent_text = await page.text_content("#benchPercentText")
                cur_p = f"{percent_text} | {progress_text}"
                if cur_p != last_p:
                    print(f"  진척: {cur_p}")
                    last_p = cur_p
                if not is_disabled and time.time() - t0 > 3.0:
                    done = True
                    break
                await asyncio.sleep(0.5)

            print()
            if not done:
                print("⚠️ 벤치마크 완료 대기 시간 초과!")

            # 콘솔 로그 출력
            print("\n--- 브라우저 콘솔 로그 요약 ---")
            for log in console_logs[-30:]:
                print(log)

            # 결과 객체 확인
            results = await page.evaluate("() => window.benchmarkResults || {}")
            print("\n5. 측정 결과 요약:")
            for cid in ["c1", "c2", "c3"]:
                res = results.get(cid)
                if res:
                    real_str = "[실제]" if res.get("isReal") else "[모의]"
                    print(f"  - {cid} ({res.get('name')}): {real_str} 로드 {res.get('loadTimeMs')}ms | P50 {res.get('latStats', {}).get('p50')}ms | P90 {res.get('latStats', {}).get('p90')}ms | 언더런 {res.get('underruns')}회 | 판정 {res.get('rating')}")
                else:
                    print(f"  - {cid}: 결과 없음")

            # 전체 보고서 텍스트 생성
            report_text = await page.evaluate("() => typeof window.generateReportText === 'function' ? window.generateReportText() : ''")

            # bench/results/face_probe_fake_cam.txt에 저장
            out_path = Path(__file__).resolve().parents[1] / "bench" / "results" / "face_probe_fake_cam.txt"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report_text, encoding="utf-8")
            print(f"\n6. 결과 보고서 저장 완료 -> {out_path}")

            if page_errors:
                print(f"⚠️ 페이지 오류 발생 ({len(page_errors)}건):")
                for err in page_errors[:10]:
                    print(f"   {err}")

            await browser.close()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
