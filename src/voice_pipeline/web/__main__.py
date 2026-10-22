"""웹 데모 실행: python -m voice_pipeline.web --port 8080 (기본 127.0.0.1. LAN 공개는 TLS+토큰 필요)."""

from __future__ import annotations

import argparse
import ipaddress
import os
import ssl

from aiohttp import web

from ..stt.registry import STT_ENGINES
from .app import create_app
from .engines import build_engines


def is_loopback(host: str) -> bool:
    """루프백 주소인지 본다 (localhost 포함)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="HealthUp 음성 파이프라인 웹 데모")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--llm", choices=["vllm", "mock"], default="vllm")
    ap.add_argument("--tts", choices=["cosyvoice3", "mock"], default="cosyvoice3")
    ap.add_argument("--stt", choices=list(STT_ENGINES), default="sensevoice")
    ap.add_argument("--first-min", type=int, default=2, help="첫 청크 최소 글자 수 (SemanticChunker.first_min)")
    ap.add_argument("--vad-silence-ms", type=int, default=500, help="발화 끝 판정 무음 길이")
    ap.add_argument("--endpoint", choices=["smart", "fixed"], default="smart",
                    help="턴 종료 방식: smart(어미+운율) | fixed(고정 무음 --vad-silence-ms)")
    ap.add_argument("--default-mode", choices=["demo", "dev"], default="demo",
                    help="기본 화면 모드: demo(간소화) | dev(개발 패널·로그)")
    ap.add_argument("--dev-log-days", type=int, default=7,
                    help="개발 로그 보존 일수 (기본 7일)")
    ap.add_argument("--no-backchannel", action="store_true", help="미리 합성한 맞장구(네, 음)를 끈다")
    ap.add_argument("--no-speculate", action="store_true", help="선행 생성(확정 대기 중 LLM·TTS 먼저)을 끈다")
    ap.add_argument("--no-barge-filter", action="store_true", help="말하는 중 맞장구 판별을 끄고 어떤 소리든 끊는다")
    ap.add_argument("--no-emotion-hint", action="store_true", help="감정 태그를 LLM 어조 지시로 쓰지 않는다")
    ap.add_argument("--tls-cert", help="TLS 인증서(PEM). --tls-key와 함께 쓴다")
    ap.add_argument("--tls-key", help="TLS 개인키(PEM)")
    ap.add_argument("--auth-token", default=os.environ.get("DEMO_AUTH_TOKEN"),
                    help="/ws 접속 토큰 (환경변수 DEMO_AUTH_TOKEN도 된다)")
    return ap


def main() -> None:
    ap = build_parser()
    a = ap.parse_args()
    if bool(a.tls_cert) != bool(a.tls_key):
        ap.error("--tls-cert와 --tls-key는 함께 줘야 한다")
    tls = bool(a.tls_cert)
    if not is_loopback(a.host) and not (tls and a.auth_token):
        ap.error("루프백이 아닌 --host는 TLS(--tls-cert, --tls-key)와 --auth-token(또는 DEMO_AUTH_TOKEN)이 "
                 "모두 있어야 한다. 마이크는 HTTPS에서만 켜지고, 상담 음성이 인증 없이 열리면 안 된다")
    ssl_ctx = None
    if tls:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_cert_chain(a.tls_cert, a.tls_key)
    app = create_app(build_engines(a.llm, a.tts, a.stt, first_min=a.first_min, vad_silence_ms=a.vad_silence_ms,
                                 backchannel=not a.no_backchannel, endpoint=a.endpoint,
                                 speculate=not a.no_speculate, barge_filter=not a.no_barge_filter,
                                 emotion_hint=not a.no_emotion_hint, default_mode=a.default_mode,
                                 dev_log_days=a.dev_log_days),
                     auth_token=a.auth_token)
    scheme = "https" if tls else "http"
    print(f"{scheme}://{a.host}:{a.port}/  (auth={'on' if a.auth_token else 'off'} llm={a.llm} tts={a.tts} stt={a.stt} default_mode={a.default_mode} first_min={a.first_min} vad_silence_ms={a.vad_silence_ms} backchannel={not a.no_backchannel} endpoint={a.endpoint} speculate={not a.no_speculate} barge_filter={not a.no_barge_filter} emotion_hint={not a.no_emotion_hint})")
    web.run_app(app, host=a.host, port=a.port, ssl_context=ssl_ctx, print=None, access_log=None)  # 접속 로그를 끈다: 쿼리의 토큰이 남지 않게


if __name__ == "__main__":
    main()
