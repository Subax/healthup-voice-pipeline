#!/usr/bin/env bash
# 로컬 ASR 컨테이너를 멈추고 지운다.
set -euo pipefail
docker rm -f healthup-asr >/dev/null 2>&1 && echo "healthup-asr 삭제함" || echo "healthup-asr 없음"
