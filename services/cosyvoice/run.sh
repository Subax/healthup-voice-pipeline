#!/usr/bin/env bash
# CosyVoice3 서버 컨테이너 실행 (127.0.0.1:8091). 모델은 저장소 models/ 를 읽기 전용 마운트.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
docker rm -f healthup-cosyvoice >/dev/null 2>&1 || true
CVENV=(); for v in $(env | grep -o "^CV_[A-Z0-9_]*"); do CVENV+=(-e "$v=${!v}"); done
exec docker run -d --name healthup-cosyvoice --gpus all --network host --ipc=host "${CVENV[@]}" \
  -v "$ROOT/models:/models:ro" \
  -v "$ROOT/services/cosyvoice/server.py:/opt/server.py:ro" \
  healthup-cosyvoice:dev python /opt/server.py --host 127.0.0.1 --port "${PORT:-8091}" "$@"
