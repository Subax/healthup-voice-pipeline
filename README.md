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

## 웹 애플리케이션 사용법

웹 데모 서버(`src/voice_pipeline/web/`)를 실행하면 브라우저에서 대화 파이프라인 및 화면 기능을 시험할 수 있습니다.

### 1. 화면 모드 (데모 모드 vs 개발 모드)

화면 우측 상단의 `⚙️ 설정` 패널에서 화면 모드를 선택할 수 있습니다.

- **데모 모드 (`demo`)**:
  - 기본 사용자 화면입니다.
  - 가상 아바타 자리표시(Avatar Placeholder) 화면과 대화 말풍선 위주로 간소화되어 있습니다.
  - VAD 레벨 미터, 어미·운율 지연 바, 텍스트 직접 입력창 등의 개발용 카드는 숨겨집니다.
  - 대화 내용이 파일로 저장되지 않습니다 (개인정보보호 기본값).
- **개발 모드 (`dev`)**:
  - 엔지니어링 및 파이프라인 진단 화면입니다.
  - **Smart Turn 턴 종료 바**: 어미 확률(`epEnd`), Smart Turn 점수(`epP`), STT 전사 및 침묵 대기 시간 시각화.
  - **마이크 진단 카드**: 실시간 음량 레벨, Silero VAD 음성 감지 상태(초록 점), 맞장구/barge-in 개별 토글, 대화 초기화 및 다시 인식 버튼.
  - **텍스트 입력 카드**: 마이크 없이 텍스트를 전송하여 LLM 및 TTS 파이프라인 단독 시험.
  - **개발 로그 자동 저장**: 전사 텍스트, LLM 응답, 융합 감정 태그, 지연 메트릭이 세션별 로그(`logs/dev/YYYYMMDD/<session_id>.jsonl`)로 서버에 기록되며, `--dev-log-days`(기본 7일) 경과 시 자동 파기됩니다.
- **모드 전환 방법**:
  - ⚙️ 설정 패널에서 라디오 버튼(`modeDemo` / `modeDev`) 클릭.
  - 음성 명령: "개발 모드로 바꿔 줘" 또는 "데모 모드로 바꿔 줘" 발화 시, 봇의 확인 질문("모드를 변경할까요?")에 "네"라고 답하면 즉시 전환됩니다.

### 2. ⚙️ 설정 패널 토글 6종

화면 상단 `⚙️ 설정` 버튼을 클릭하면 접이식 설정 패널이 열리며, 다음 6가지 기능을 독립적으로 켜고 끌 수 있습니다:

1. **음성으로 시작 (대기 청취, `wake`)**:
   - 세션 시작 전 대기(`idle`) 상태에서도 마이크를 열어두고 음성 명령("시작하자", "안녕하세요" 등)을 감지합니다.
2. **얼굴 표정 감정 반영 (`face`)**:
   - 웹캠을 통해 사용자의 실시간 표정을 분석하고, 음성 감정과 융합(R0~R6)하여 LLM 어조 지시 힌트로 제공합니다 (최초 켤 때 동의 팝업 필수).
3. **AI 고지 표시 (`show_ai_notice`)**:
   - AI 기본법 제31조 준수를 위해 상단에 "AI 마음 건강 도우미" 상시 고지 배너를 노출합니다. 끄려고 시도할 경우 법적 고지 확인 경고창이 나타납니다.
4. **카메라/마이크 상태 배지 표시 (`show_camera_badge`)**:
   - 상단 헤더에 카메라 켜짐 상태 배지(`카메라 켬`) 및 마이크 상태 배지를 표시합니다.
5. **연결 상태 표시 (`show_conn_status`)**:
   - 상단 헤더에 웹소켓 연결 상태(`대기`, `연결됨`, `재연결 중`) 및 서버 RTT(`RTT n ms`)를 표시합니다.
6. **개발 정보 패널 표시 (`show_dev_info`)**:
   - 데모 모드 상태에서도 Smart Turn 어미/운율 판단 바 등 개발 정보 카드를 보조로 띄울 수 있습니다.

### 3. 대기 청취 (Wake Listening)

- **동작 방식**: 세션이 시작되지 않은 대기(`idle`) 상태에서도 브라우저 오디오 컨텍스트가 마이크를 활성화하여 로컬 VAD 및 STT로 시작 명령을 실시간 감시합니다.
- **화면 표시**: 화면 상단에 `대기` 배지가 표시되며, 아바타 힌트에 "화면을 터치하거나 '시작하자'라고 말씀하세요" 안내가 노출됩니다.
- **제어**: 서버 기동 시 `--wake`(기본값) 또는 `--no-wake` 옵션으로 기본 활성화 여부를 지정할 수 있으며, 실행 중에는 ⚙️ 설정 패널 토글이나 음성 명령("대기 청취 켜 줘" / "대기 청취 꺼 줘")으로 즉시 제어할 수 있습니다.

### 4. 표정 감정 분석 동의 절차 (Face Emotion Consent)

- **원칙 (개인정보보호 및 의료법 가이드 준수)**:
  - 웹캠 영상은 브라우저 메모리(face-api.js의 tinyFaceDetector + faceExpressionNet, `/static/models/face-api`) 내에서 100% 로컬 처리됩니다.
  - 영상 프레임이나 이미지 파일은 **서버로 전혀 전송되지 않으며 어디에도 저장되지 않습니다**.
  - 동의하기 전에는 웹캠 장치(`getUserMedia`)를 호출하지 않습니다.
- **동의 흐름**:
  1. ⚙️ 설정 패널에서 `얼굴 표정 감정 반영`을 체크하거나, 음성으로 "카메라 켜 줘"라고 명령합니다.
  2. 동의 이력이 없는 경우, 화면에 **카메라 및 얼굴 표정 분석 동의 모달**이 표시되고 음성 안내("표정으로 기분을 짐작해 대답에 참고합니다. 카메라를 켜도 될까요?")가 재생됩니다.
  3. 사용자가 모달의 **「동의하고 카메라 켜기」** 버튼을 클릭하거나 음성으로 **"네" / "좋아요"**라고 답변하면 동의(`sessionStorage`)가 저장되고 웹캠이 켜집니다.
  4. **「동의 안 함」** 버튼, `ESC` 키, 모달 바깥 클릭, 또는 음성 **"아니요"** 답변 시 카메라는 켜지지 않고 순수 음성 대화만 진행됩니다.
- **감정 융합 반영**:
  - 카메라가 켜지면 사용자의 발화 구간 동안 프레임별 표정을 추론합니다.
  - 발화 전체 프레임 대비 얼굴 검출률(coverage)과 가중 다수결(R0~R6)을 통해 음성 감정(SenseVoice)과 얼굴 감정을 융합한 힌트(예: `[화자 표정·어조: 슬픔(표정 80%)]`)를 LLM에 전달합니다.
  - 언제든 ⚙️ 설정 패널 체크 해제 또는 음성 명령("카메라 꺼 줘")으로 카메라를 끌 수 있습니다.

### 5. 음성 명령 목록 (`src/voice_pipeline/intent.py` 기준)

파이프라인에는 위기 발화 우선 보호 및 초고속(P99 <= 5ms) 규칙 기반 의도 분류기가 탑재되어 있습니다. 위기/자해/극단적 선택 표현은 명령으로 오판하지 않고 즉시 위기 대응 일반 상담(`TALK`)으로 넘깁니다.

#### 즉시 실행 명령 (Immediate — 확인 질문 없이 즉시 처리)
- **대기 상태 (`idle`)**:
  - `SESSION_START`: "시작하자", "시작할게요", "안녕하세요", "말동무 해 줘", "대화 시작해 줘", "이야기하고 싶어요"
- **봇 발화 중 (`speaking`)**:
  - `STOP_SPEAKING`: "잠깐만", "멈춰", "그만", "그만 말해 줘", "말 좀 멈춰", "거기까지 말해 줘" (TTS 재생 즉시 중단)
- **대화 진행 중 (`active`)**:
  - `REPEAT_BOT`: "다시 말해 줘", "뭐라고 하셨어요", "안 들렸어요", "못 들었어요", "한 번만 더 말해 줘" (직전 봇 답변 다시 재생)
  - `REDO_USER`: "다시 말할게요", "방금 한 말 취소해 줘", "이전 말 지워 줘", "내가 잘못 말했어 다시 할게요" (직전 사용자 발화 취소)
- **기능 즉시 제어 (`idle` 또는 `active`)**:
  - `CAMERA_OFF`: "카메라 꺼 줘", "표정 인식 중지해 줘"
  - `WAKE_ON` / `WAKE_OFF`: "대기 청취 켜 줘", "음성으로 시작 켜 줘" / "대기 청취 꺼 줘", "음성으로 시작 꺼 줘"
  - `DEV_INFO_ON` / `DEV_INFO_OFF`: "개발 정보 보여 줘", "개발 패널 켜 줘" / "개발 정보 숨겨 줘", "개발 패널 꺼 줘"
  - `UI_BADGE_TOGGLE`: "카메라 배지 켜/꺼 줘", "연결 상태 켜/꺼 줘", "AI 고지 보여 줘"

#### 확인 필요 명령 (Confirm — 오작동 방지용 확인 질문 후 실행)
- `RESET`: "처음부터 다시", "초기화해 줘", "사람이 바뀌었어요"
  - 확인 질문: *"처음부터 다시 할까요?"* → "네" 답변 시 대화 기록 초기화 및 첫인사 재생.
- `SESSION_END`: "그만할게요", "대화 끝낼게요", "오늘 대화는 여기까지 할게요", "종료할게요"
  - 확인 질문: *"대화를 마칠까요?"* → "네" 답변 시 세션 종료 및 작별 인사 재생.
- `MODE_DEV`: "개발 모드로 바꿔 줘", "개발자 화면 켜 줘"
  - 확인 질문: *"모드를 변경할까요?"* → "네" 답변 시 개발 모드로 전환.
- `MODE_DEMO`: "데모 모드로 바꿔 줘", "시연 모드로 전환해 줘"
  - 확인 질문: *"모드를 변경할까요?"* → "네" 답변 시 데모 모드로 전환.
- `CAMERA_ON`: "카메라 켜 줘", "표정 분석 켜 줘"
  - 확인 질문: *"표정으로 기분을 짐작해 대답에 참고합니다. 카메라를 켜도 될까요?"* → "네" 답변 시 동의 저장 및 카메라 켜짐.
- `AI 고지 숨김`: "AI 고지 숨겨 줘", "에이아이 고지 꺼 줘"
  - 확인 질문: *"AI 기본법에 따른 법적 고지입니다. 숨기시겠습니까?"* → "네" 답변 시 숨김.

#### 확인 질문 응답 (`confirming` 상태)
- `CONFIRM_YES`: "네", "응", "그래요", "예", "맞아요", "좋아요", "그렇게 해 주세요"
- `CONFIRM_NO`: "아니요", "아니", "취소할래요", "하지 마세요"

### 6. 서버 실행 인자 (`src/voice_pipeline/web/__main__.py`)

`python -m voice_pipeline.web [옵션]` 또는 `scripts/run_demo.sh [옵션]`으로 서버를 기동합니다.

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--host HOST` | `127.0.0.1` | 서버 바인드 호스트 (루프백 외 LAN 공개 시 TLS와 인증 토큰 필수) |
| `--port PORT` | `8080` | 서버 HTTP/WebSocket 포트 |
| `--llm {vllm,mock}` | `vllm` | 상담 LLM 백엔드 엔진 선택 |
| `--tts {cosyvoice3,mock}` | `cosyvoice3` | 음성 합성 TTS 백엔드 엔진 선택 |
| `--stt {sensevoice,qwen3asr,mock}` | `sensevoice` | 음성 인식 STT 백엔드 엔진 선택 |
| `--first-min N` | `2` | 첫 청크 최소 글자 수 (`SemanticChunker.first_min`) |
| `--vad-silence-ms MS` | `500` | 발화 끝 판정 무음 길이(ms) |
| `--endpoint {smart,fixed}` | `smart` | 턴 종료 방식: `smart`(Kiwi 어미 + Smart Turn 운율) \| `fixed`(고정 무음 `--vad-silence-ms`) |
| `--default-mode {demo,dev}` | `demo` | 웹 화면 기본 모드: `demo`(간소화 데모) \| `dev`(개발 패널 및 로그 활성화) |
| `--dev-log-days DAYS` | `7` | 개발 모드 로그 파일 보존 일수 (경과 시 자동 파기) |
| `--wake` / `--no-wake` | `--wake` | 대기 청취(음성으로 시작 감지) 기본 활성화 여부 |
| `--no-backchannel` | (꺼짐) | 미리 합성한 맞장구("네", "음" 등)를 끕니다 |
| `--no-speculate` | (꺼짐) | 턴 확정 대기 중 LLM·TTS 선행 생성을 끕니다 |
| `--no-barge-filter` | (꺼짐) | 맞장구 판별 필터를 끄고 발화 중 어떤 소리든 즉시 끼어들기(barge-in)로 답을 끊습니다 |
| `--no-emotion-hint` | (꺼짐) | STT 및 표정 감정 태그를 LLM 어조 지시 프롬프트로 쓰지 않습니다 |
| `--no-intent` | (꺼짐) | 음성 명령 의도 분류를 끄고 모든 입력을 대화로 처리합니다 |
| `--tls-cert PATH` | `None` | TLS 인증서(PEM) 파일 경로 (`--tls-key`와 함께 사용) |
| `--tls-key PATH` | `None` | TLS 개인키(PEM) 파일 경로 |
| `--auth-token TOKEN` | 환경변수 `DEMO_AUTH_TOKEN` | `/ws` 접속 인증 토큰 (외부 접속 시 필수) |

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
