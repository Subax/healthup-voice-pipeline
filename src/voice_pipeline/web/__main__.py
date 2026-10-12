"""웹 데모 실행: python -m voice_pipeline.web --port 8080 (127.0.0.1에만 바인딩)."""

from __future__ import annotations

import argparse

from aiohttp import web

from ..stt.registry import STT_ENGINES
from .app import create_app
from .engines import build_engines


def main() -> None:
    ap = argparse.ArgumentParser(description="HealthUp 음성 파이프라인 웹 데모")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--llm", choices=["vllm", "mock"], default="vllm")
    ap.add_argument("--tts", choices=["cosyvoice3", "mock"], default="cosyvoice3")
    ap.add_argument("--stt", choices=list(STT_ENGINES), default="sensevoice")
    ap.add_argument("--first-min", type=int, default=2, help="첫 청크 최소 글자 수 (SemanticChunker.first_min)")
    ap.add_argument("--vad-silence-ms", type=int, default=500, help="발화 끝 판정 무음 길이")
    ap.add_argument("--no-backchannel", action="store_true", help="미리 합성한 맞장구(네, 음)를 끈다")
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        ap.error("로컬 전용 데모다. --host는 127.0.0.1만 허용한다 (원격은 ssh -L 사용)")
    app = create_app(build_engines(a.llm, a.tts, a.stt, first_min=a.first_min, vad_silence_ms=a.vad_silence_ms,
                                 backchannel=not a.no_backchannel))
    print(f"http://127.0.0.1:{a.port}/  (llm={a.llm} tts={a.tts} stt={a.stt} first_min={a.first_min} vad_silence_ms={a.vad_silence_ms} backchannel={not a.no_backchannel})")
    web.run_app(app, host=a.host, port=a.port, print=None)


if __name__ == "__main__":
    main()
