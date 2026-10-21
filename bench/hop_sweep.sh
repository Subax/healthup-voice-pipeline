#!/usr/bin/env bash
# CV_HOP(첫 스트리밍 토큰 묶음 크기)별로 TTS 서버를 다시 띄우고 단독 TTFA·RTF, 왕복 CER, LLM 동시 첫 소리를 잰다.
# 사용: bench/hop_sweep.sh 25 15 10 [-- first_audio_bench 인자]   (LLM 서버가 떠 있어야 동시 측정이 된다)
# 결과: bench/results/latency_2610/hop<N>/ (wav, tts_bench.txt, roundtrip.txt, firstaudio.txt)
set -uo pipefail
cd "$(dirname "$0")/.."
OUT=bench/results/latency_2610
for H in "$@"; do
  D="$OUT/hop$H"; mkdir -p "$D"
  echo "=== CV_HOP=$H: TTS 서버 재시작"
  CV_HOP="$H" services/cosyvoice/run.sh >/dev/null
  for _ in $(seq 1 90); do curl -s -m 2 http://127.0.0.1:8091/info >/dev/null && break; sleep 5; done
  .venv/bin/python bench/tts_bench.py --engine cosyvoice3 --save-wav "$D" > "$D/tts_bench.txt" 2>&1
  tail -1 "$D/tts_bench.txt"
  .venv/bin/python bench/tts_roundtrip.py "$D" > "$D/roundtrip.txt" 2>&1
  tail -3 "$D/roundtrip.txt"
  .venv/bin/python bench/first_audio_bench.py --first-min 2 -n 10 > "$D/firstaudio.txt" 2>&1
  grep -A5 "== new" "$D/firstaudio.txt" | head -6
done
