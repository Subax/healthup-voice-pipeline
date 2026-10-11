#!/usr/bin/env bash
# 로컬 vLLM 컨테이너를 멈추고 지운다.
set -euo pipefail
docker rm -f healthup-llm >/dev/null 2>&1 && echo "healthup-llm 삭제함" || echo "healthup-llm 없음"
