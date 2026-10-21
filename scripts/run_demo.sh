#!/usr/bin/env bash
# 웹 데모 실행. 인자는 그대로 넘긴다. 예: scripts/run_demo.sh --llm mock --tts mock --stt mock
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8080
prev=""
for a in "$@"; do [ "$prev" = "--port" ] && PORT="$a"; prev="$a"; done
# --host 기본값은 127.0.0.1. 루프백이 아니면 --tls-cert --tls-key --auth-token이 필요하다
echo "열기:  http://localhost:${PORT}/  (TLS를 켰다면 https)"
echo "태블릿(LAN): scripts/gen_cert.sh <서버IP> → run_demo.sh --host 0.0.0.0 --tls-cert certs/server.pem --tls-key certs/server.key --auth-token <토큰>"
echo "            태블릿 주소: https://<서버IP>:${PORT}/?t=<토큰>"
echo "(getUserMedia는 보안 컨텍스트가 필요하다. http://localhost와 HTTPS는 되고 http://LAN IP는 안 된다)"
exec .venv/bin/python -m voice_pipeline.web --port "$PORT" "$@"
