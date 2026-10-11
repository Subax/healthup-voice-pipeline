#!/usr/bin/env bash
# 웹 데모 실행. 인자는 그대로 넘긴다. 예: scripts/run_demo.sh --llm mock --tts mock --stt mock
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8080
prev=""
for a in "$@"; do [ "$prev" = "--port" ] && PORT="$a"; prev="$a"; done
echo "열기:  http://localhost:${PORT}/"
echo "원격 장비라면 내 PC에서:  ssh -L ${PORT}:localhost:${PORT} <host>"
echo "(getUserMedia는 보안 컨텍스트가 필요하다. http://localhost는 되고 LAN IP는 안 된다)"
exec .venv/bin/python -m voice_pipeline.web --host 127.0.0.1 --port "$PORT" "$@"
