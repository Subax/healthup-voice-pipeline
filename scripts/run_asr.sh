#!/usr/bin/env bash
# 로컬 Qwen3-ASR-1.7B 서버를 docker(vLLM)로 띄운다 (OpenAI 호환, 127.0.0.1:8092).
# 환경변수: ASR_GPU_UTIL(기본 0.08, 전체 메모리 비율), ASR_MODEL_PATH
set -euo pipefail

# FP8 가중치 양자화 (기본). 디코드가 메모리 대역폭에 묶여 지연이 약 절반이 된다. 끄려면 ASR_QUANT= (빈 값)
ASR_QUANT="${ASR_QUANT-fp8}"
# 모델: ASR_MODEL_PATH=Qwen/Qwen3-ASR-0.6B 로 바꿀 수 있다 (served-model-name 은 그대로)

NAME=healthup-asr
IMAGE=nvcr.io/nvidia/vllm:26.07-py3

if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "컨테이너 $NAME 이(가) 이미 있다. 중지/삭제는 scripts/stop_asr.sh"
  exit 0
fi

# 통합 메모리 장비: LLM(0.35)·CosyVoice 옆에 올라가도록 작게 잡는다
docker run -d --name "$NAME" \
  --gpus all --network host --ipc host \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  "$IMAGE" vllm serve "${ASR_MODEL_PATH:-Qwen/Qwen3-ASR-1.7B}" \
  --served-model-name Qwen/Qwen3-ASR-1.7B \
  --host 127.0.0.1 --port 8092 \
  --gpu-memory-utilization "${ASR_GPU_UTIL:-0.08}" \
  --max-model-len 4096 \
  ${ASR_QUANT:+--quantization "$ASR_QUANT"}

echo "시작함. 로그: docker logs -f $NAME  /  확인: curl -s http://127.0.0.1:8092/v1/models"
