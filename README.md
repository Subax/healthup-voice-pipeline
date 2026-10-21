# healthup-voice-pipeline

HealthUp Station 버츄얼휴먼의 음성 파이프라인이다. 웹 데모(localhost)로 전 구간을 실행할 수 있다.
마이크 → STT → 상담 LLM → TTS → 브라우저 재생 경로가 모두 로컬에서 동작한다.

**지금 할 일**: `Claude/docs/STATE.md`의 「2. 다음 할 일」 맨 위 항목을 한다.

설계 근거는 [docs/dev-guide.md](docs/dev-guide.md)에 있다. 아키텍처 정본은 `10-ai26-2-ai.md` (v0.3)이다.

## 진행 상태

- ✅ turn_id 기반 취소, 청커, 텍스트 정규화(숫자·약어 한국어 읽기 포함)
- ✅ 파이프라인: 청크 큐, barge-in, 119 정지 후 안전 안내, 들은 부분만 대화 기록
- ✅ STT: SenseVoice-Small INT8 (sherpa-onnx, CPU), Silero VAD 구간 분할
- ✅ LLM: 로컬 vLLM (Qwen3.8-27B NVFP4) 스트리밍 클라이언트
- ✅ TTS: CosyVoice3 (Fun-CosyVoice3-0.5B-2512) 서버(docker)와 어댑터
- ✅ 웹 데모: 브라우저가 Unreal 역할(재생 주체)을 한다. 같은 브리지 프로토콜을 쓴다
- ⏭️ Smart Turn·Kiwi 턴 종료, 선행 생성, 맞장구 필터, OmniVoice 비교, Unreal 연결

## 구조

```text
src/voice_pipeline/
  pipeline.py        LLM 스트림 → 청커 → 정규화 → TTS → 20ms 프레임 → 싱크
  turns.py           TurnGuard: 정지 = 현재 turn_id 무효화
  chunker.py         SemanticChunker
  normalize.py       기호·단위·숫자·약어 → 읽을 수 있는 말
  audio.py, trace.py, sinks.py
  stt/               STTEngine 인터페이스, SenseVoiceSTT, Qwen3ASRSTT, VADSegmenter
  llm/               §32 이벤트(임시 스키마), MockLLM, SSE, OpenAIStreamLLM(vLLM)
  tts/               TTSEngine 인터페이스, MockTTS, CosyVoiceTTS
  bridge/            Unreal WebSocket 싱크, 프레임 형식, 서버 쪽 싱크, 가짜 Unreal 서버
  web/               웹 데모 서버(aiohttp)와 페이지(static/)
services/cosyvoice/  CosyVoice3 서버 이미지 (GB10 aarch64용)
scripts/             vLLM·데모 실행 스크립트
bench/               TTS·STT·LLM 벤치, TTS 왕복 CER 검사, 문장 세트(40개)
tests/
```

## 웹 데모 실행 (개발 장비: AI TOP ATOM, GB10)

> ⚠️ 주의: 세 모델이 통합 메모리를 함께 쓴다. 시작 전에 `free -g`로 가용 메모리 60GB 이상을 확인한다.

모델 받기 (처음 한 번, 저장소 루트에서):

1. vLLM 이미지로 LLM 가중치를 받는다: `docker run --rm -v ~/.cache/huggingface:/root/.cache/huggingface nvcr.io/nvidia/vllm:26.07-py3 hf download nvidia/Qwen3.8-27B-NVFP4`
2. TTS 모델을 받는다: `docker run --rm -v $PWD/models:/models nvcr.io/nvidia/vllm:26.07-py3 hf download FunAudioLLM/Fun-CosyVoice3-0.5B-2512 --local-dir /models/Fun-CosyVoice3-0.5B-2512`
3. STT 모델을 받는다: `csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17`의 `model.int8.onnx`, `tokens.txt`를 `models/sense-voice-int8/`에 둔다
4. VAD 모델을 받는다: sherpa-onnx 릴리스 `asr-models`의 `silero_vad.onnx`를 `models/`에 둔다
5. (선택) Qwen3-ASR 가중치를 받는다: `docker run --rm -v ~/.cache/huggingface:/root/.cache/huggingface nvcr.io/nvidia/vllm:26.07-py3 hf download Qwen/Qwen3-ASR-1.7B`
6. 턴 종료 모델을 받는다: `pipecat-ai/smart-turn-v3`의 `smart-turn-v3.2-cpu.onnx`를 `models/smart-turn/`에 둔다 (`curl -L -o models/smart-turn/smart-turn-v3.2-cpu.onnx https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx`)

> ⚠️ 주의: SenseVoice는 2024-07-17 모델을 쓴다. 2025-09-09 INT8 모델은 한국어를 한자 섞인 결과로 냈다.

실행:

1. 의존성을 설치한다: `.venv/bin/pip install -e ".[dev]"`
2. TTS 이미지를 빌드한다: `services/cosyvoice/build.sh`
3. TTS 서버를 띄운다: `services/cosyvoice/run.sh`
4. LLM 서버를 띄운다: `scripts/run_llm.sh` (준비까지 약 6분)
5. LLM 준비를 확인한다: `curl -s http://127.0.0.1:8000/v1/models`
6. (선택) ASR 서버를 띄운다: `scripts/run_asr.sh` (127.0.0.1:8092, GPU 메모리 약 10GB, 준비까지 첫 실행 약 7분)
7. ASR 준비를 확인한다: `curl -s http://127.0.0.1:8092/v1/models`
8. 데모 서버를 띄운다: `scripts/run_demo.sh`
9. 브라우저에서 `http://localhost:8080/`을 열고 「시작」을 누른다

- 다른 PC에서 접속하면 그 PC에서 `ssh -L 8080:localhost:8080 <장비>`를 먼저 실행한다. 마이크(getUserMedia)는 localhost나 HTTPS에서만 동작한다.
- 같은 LAN의 태블릿에서 접속하려면 HTTPS와 토큰이 필요하다 (둘 중 하나라도 없으면 서버가 시작하지 않는다):
  1. 인증서를 만든다: `scripts/gen_cert.sh <서버IP> [호스트이름]` (`certs/ca.pem`, `certs/server.pem`, `certs/server.key`)
  2. 태블릿에 `certs/ca.pem`을 설치한다 (Android: 설정 → 보안 → 인증서 설치, iOS: 프로파일 설치 후 신뢰 켜기)
  3. 서버를 띄운다: `scripts/run_demo.sh --host 0.0.0.0 --tls-cert certs/server.pem --tls-key certs/server.key --auth-token <토큰>`
  4. 태블릿 브라우저에서 `https://<서버IP>:8080/?t=<토큰>`을 열고 「시작」을 누른다
  5. 화면 상단의 「RTT n ms」로 서버까지 왕복 시간을 본다. 서버 쪽 점검은 `bench/net_probe.py --url wss://<서버IP>:8080/ws --token <토큰> --cacert certs/ca.pem`
- 토큰은 환경변수 `DEMO_AUTH_TOKEN`으로도 준다. 연결이 끊기면 화면이 「재연결 중」으로 바뀌고 자동으로 다시 연결한다.
- 스피커 소리가 마이크로 들어가 barge-in이 잘못 걸리면 헤드폰을 쓴다. 또는 화면의 barge-in 선택을 끈다.
- 화면의 「목소리」에서 말투를 고른다: 자동(기본), 차분하게, 위로하는, 따뜻하게, 차분하고 단단하게, 밝게 (기존). 모두 같은 여성 화자(CosyVoice 공식 예제 음성)이고 지시문만 다르다. 「자동」은 사용자 발화의 감정 태그(SenseVoice)와 음량·말 속도로 톤을 고른다 (`src/voice_pipeline/tone.py`, 임계값은 미튜닝 추측값). 고른 톤은 말풍선에 `[톤 …]`으로 나온다.
- 화면의 「맞장구」를 켜면 답 앞에 미리 합성한 "네." 등이 바로 나온다. 끄려면 선택을 풀거나 `--no-backchannel`을 쓴다.
- 비교·문제 확인용 끄개: `--no-speculate`(선행 생성: 턴 확정 대기 중 LLM·TTS를 먼저 돌림), `--no-barge-filter`(말하는 중 짧은 "네"로 답을 끊지 않는 판별), `--no-emotion-hint`(감정 태그를 LLM 어조 지시로 쓰기).
- TTS 서버의 첫 오디오 묶음 크기는 `CV_HOP`(기본 10)이다. 값별 비교는 `bench/hop_sweep.sh 25 10`.
- 설정: `scripts/run_demo.sh --first-min 2 --vad-silence-ms 500`. `--first-min`은 첫 청크 최소 글자 수, `--vad-silence-ms`는 발화 끝 판정 무음 길이다.
- 브라우저 탭은 하나만 연다. TTS 엔진을 탭끼리 공유하므로 두 탭이 동시에 말하면 충돌한다.
- GPU 없이 화면만 시험하려면: `scripts/run_demo.sh --llm mock --tts mock --stt mock`
- STT 엔진은 화면에서 고른다. 「Qwen3-ASR-1.7B (GPU)」는 ASR 서버가 떠 있을 때만 선택된다. 주소는 환경변수 `QWEN3ASR_URL`로 바꾼다.
- 정지: `scripts/stop_llm.sh`, `scripts/stop_asr.sh`, `docker rm -f healthup-cosyvoice`

## 벤치·검사 (저장소 루트에서)

1. 테스트: `.venv/bin/python -m pytest`
2. TTS 지연: `.venv/bin/python bench/tts_bench.py --engine cosyvoice3 --save-wav bench/results/cv3`
3. TTS 발음 검사(왕복 CER): `.venv/bin/python bench/tts_roundtrip.py bench/results/cv3`
4. STT: `.venv/bin/python bench/stt_bench.py`
5. STT 엔진 비교(SenseVoice vs Qwen3-ASR, ASR 서버 필요): `.venv/bin/python bench/stt_compare.py --both-spacing`
6. LLM: `.venv/bin/python bench/llm_bench.py`
7. 지연 시뮬레이션: `.venv/bin/python bench/latency_sim.py --ttft-ms 400 --tps 40`

## 새 TTS 엔진 붙이기

1. `src/voice_pipeline/tts/`에 `TTSEngine`을 따르는 클래스를 만든다.
2. `stream(text)`에서 PCM16 모노 조각을 차례로 낸다.
3. `bench/tts_bench.py`의 `ENGINES`에 등록한다.
4. 같은 문장 세트로 TTFA·RTF를 잰다.
5. `bench/tts_roundtrip.py`로 CER을 잰다.

## 주의

- 개발 장비 수치는 기능 확인용이다. 최종 수치는 운영 PC(Windows 11 + RTX 4090 ×2)에서 확정한다.
- NVFP4 LLM은 RTX 4090(Ada)에서 하드웨어 가속이 없다. 운영 PC에서는 다른 양자화본을 쓴다.
- §32 이벤트 필드 이름은 임시다. 정본을 받으면 `llm/events.py`, `llm/sse.py`, `llm/openai_stream.py`만 고친다.
- `SAFETY_TEXT_119`는 임시 문구다. 정본 안전 응답 템플릿으로 바꾼다.
- 클라우드 TTS는 조교·지도교수 승인 전까지 쓰지 않는다 (dev-guide §10).
