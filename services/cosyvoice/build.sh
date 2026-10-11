#!/usr/bin/env bash
# CosyVoice 소스를 받고 이미지를 빌드한다.
set -euo pipefail
cd "$(dirname "$0")"
# 검증한 커밋 (2026-10-11, GB10). 바꾸면 tts_roundtrip CER을 다시 잰다
REV=074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
[ -d CosyVoice ] || git clone --recursive https://github.com/FunAudioLLM/CosyVoice CosyVoice
git -C CosyVoice checkout -q "$REV" && git -C CosyVoice submodule update -q --init --recursive
docker build -t healthup-cosyvoice:dev .
