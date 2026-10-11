#!/usr/bin/env bash
# 로컬 vLLM 서버를 docker로 띄운다 (OpenAI 호환, 127.0.0.1:8000).
# 환경변수: LLM_MODEL_PATH, LLM_GPU_UTIL(기본 0.35), LLM_MTP(기본 1, MTP 추측 디코딩. 0이면 끈다)
set -euo pipefail

NAME=healthup-llm
IMAGE=nvcr.io/nvidia/vllm:26.07-py3

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "컨테이너 $NAME 이(가) 이미 있다. 중지/삭제는 scripts/stop_llm.sh"
  exit 0
fi

ARGS=(
  "${LLM_MODEL_PATH:-nvidia/Qwen3.8-27B-NVFP4}"
  --served-model-name Qwen/Qwen3.8-27B
  --host 127.0.0.1 --port 8000
  # 통합 메모리 장비: 기본 0.9면 다른 모델이 못 뜬다. 명시적으로 둔다
  --gpu-memory-utilization "${LLM_GPU_UTIL:-0.35}"
  --max-model-len 8192
  # 텍스트 전용: 비전 인코더를 올리지 않는다 (vLLM 0.24 확인된 플래그)
  --language-model-only
)

if [[ "${LLM_MTP:-1}" == "1" ]]; then
  # MTP 추측 디코딩 (Qwen3_5MTP 등록 확인). 체크포인트에 MTP 가중치가 있어야 한다
  ARGS+=(--speculative-config '{"method":"mtp","num_speculative_tokens":2}')
fi

docker run -d --name "$NAME" \
  --gpus all --network host --ipc host \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  "$IMAGE" vllm serve "${ARGS[@]}"

echo "시작함. 로그: docker logs -f $NAME  /  확인: curl -s http://127.0.0.1:8000/v1/models"
