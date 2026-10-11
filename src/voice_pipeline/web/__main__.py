"""웹 데모 실행: python -m voice_pipeline.web --port 8080 (127.0.0.1에만 바인딩)."""

from __future__ import annotations

import argparse

from aiohttp import web

from .app import create_app
from .engines import build_engines


def main() -> None:
    ap = argparse.ArgumentParser(description="HealthUp 음성 파이프라인 웹 데모")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--llm", choices=["vllm", "mock"], default="vllm")
    ap.add_argument("--tts", choices=["cosyvoice3", "mock"], default="cosyvoice3")
    ap.add_argument("--stt", choices=["sensevoice", "mock"], default="sensevoice")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        ap.error("로컬 전용 데모다. --host는 127.0.0.1만 허용한다 (원격은 ssh -L 사용)")
    app = create_app(build_engines(a.llm, a.tts, a.stt))
    print(f"http://127.0.0.1:{a.port}/  (llm={a.llm} tts={a.tts} stt={a.stt})")
    web.run_app(app, host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
