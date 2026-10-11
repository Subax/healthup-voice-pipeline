#!/usr/bin/env bash
# CosyVoice 소스를 받고 이미지를 빌드한다.
set -euo pipefail
cd "$(dirname "$0")"
[ -d CosyVoice ] || git clone --recursive https://github.com/FunAudioLLM/CosyVoice CosyVoice
docker build -t healthup-cosyvoice:dev .
