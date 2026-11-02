# F1 표정 모델 성능·언더런 측정 사용자 가이드 (`webapp_plan.md` §4.8 · §5 F1 · §7)

> **목적**: 개발 PC 웹캠과 태블릿 카메라에서 브라우저 로컬 표정 모델 후보 3종의 **로드 시간, 추론 P50·P90, 실제 분석 주기(FPS), 오디오 재생 언더런 수**를 측정하여 `bench/results/face_probe_20261027.txt`에 기록한다.
> **개인정보·안전 원칙 (§4.4)**: 동의 버튼을 누르기 전에는 `getUserMedia`가 호출되지 않으며, 카메라 영상 프레임은 브라우저 메모리에서만 처리되고 서버로 0바이트 전송된다.

---

## 1. 서버 띄우는 정확한 명령 (HTTPS 필요 여부·인증서·포트·토큰)

`src/voice_pipeline/web/__main__.py`(`57~63행`)와 브라우저 보안 정책(`Secure Context`)에 따라 **접속 기기별 조건**이 다르다. F1 페이지는 정적 파일(`/static/face-probe.html`)만 쓰므로 LLM·TTS·STT는 모두 `mock`으로 띄우면 GPU 대기 없이 즉시 기동된다.

### 경우 A. 개발 PC 웹캠만 먼저 측정할 때 (루프백 `127.0.0.1` — HTTP 허용, 인증서·토큰 불필요)

브라우저는 `http://127.0.0.1`을 보안 컨텍스트로 인정하므로 HTTP에서도 카메라(`getUserMedia`)가 바로 켜진다.

```bash
cd /home/maisc05/Desktop/healthup/healthup-voice-pipeline
.venv/bin/python -m voice_pipeline.web --host 127.0.0.1 --port 8080 --llm mock --tts mock --stt mock
```

### 경우 B. 태블릿 카메라(같은 LAN)와 개발 PC를 함께 측정할 때 (HTTPS + 토큰 필수)

- **HTTPS 필요 이유 2가지**:
  1. **브라우저 제약**: 루프백이 아닌 LAN IP(`http://192.168.x.x`)에서는 브라우저가 `navigator.mediaDevices.getUserMedia` 호출을 차단한다 (`https://` 필수).
  2. **서버 기동 제약 (`web/__main__.py:60~62`)**: `--host`가 `127.0.0.1`/`localhost`가 아니면 `--tls-cert`, `--tls-key`, `--auth-token`(또는 환경변수 `DEMO_AUTH_TOKEN`) 3가지가 모두 있어야 서버가 기동된다.

#### 1) 로컬 CA 및 서버 인증서 생성 (`scripts/gen_cert.sh`)

```bash
cd /home/maisc05/Desktop/healthup/healthup-voice-pipeline

# 1. 개발 PC(서버)의 LAN IP 확인 (예: 192.168.0.10)
hostname -I

# 2. 인증서 생성 (이미 certs/가 있고 IP가 바뀌었으면 rm -rf certs 후 실행)
scripts/gen_cert.sh <서버_LAN_IP>
```

- 생성 파일: `certs/ca.pem` (태블릿 설치용 공개 CA), `certs/server.pem` (서버 인증서), `certs/server.key` (서버 개인키 — 외부 반출 금지)
- **태블릿에 `certs/ca.pem` 설치 방법** (`scripts/gen_cert.sh:36~39`):
  - **Android**: `설정 → 보안 → 암호화 및 사용자 인증정보 → 인증서 설치 → CA 인증서`에서 `ca.pem` 선택
  - **iPadOS / iOS**: `ca.pem`을 열어 프로파일 설치 → `설정 → 일반 → 정보 → 인증서 신뢰 설정`에서 `HealthUp Local CA` 활성화

#### 2) HTTPS 서버 기동 명령 (포트 `8080`, 토큰 `healthup-f1`)

```bash
cd /home/maisc05/Desktop/healthup/healthup-voice-pipeline
.venv/bin/python -m voice_pipeline.web \
  --host 0.0.0.0 --port 8080 \
  --llm mock --tts mock --stt mock \
  --tls-cert certs/server.pem --tls-key certs/server.key \
  --auth-token healthup-f1
```

---

## 2. 개발 PC·태블릿에서 열 `face-probe` 주소 형식

`src/voice_pipeline/web/app.py:102`에서 `src/voice_pipeline/web/static/` 폴더를 `/static/` 경로로 서빙하므로 정확한 주소는 아래와 같다 (`/static/` 접두어 필수):

| 기기 | 서버 기동 방식 | 브라우저 주소창에 입력할 정확한 주소 |
|---|---|---|
| **개발 PC** | 경우 A (HTTP 루프백) | `http://127.0.0.1:8080/static/face-probe.html` |
| **개발 PC** | 경우 B (HTTPS) | `https://127.0.0.1:8080/static/face-probe.html` |
| **태블릿** | 경우 B (HTTPS, 같은 Wi-Fi) | `https://<서버_LAN_IP>:8080/static/face-probe.html` |

*(참고: `/static/face-probe.html`은 웹소켓 `/ws`를 쓰지 않아 `?t=` 쿼리 없이 열리며, 상단 링크로 메인 데모 `/`에 이동할 때만 `https://<서버_LAN_IP>:8080/?t=healthup-f1`을 쓴다.)*

> [!IMPORTANT]
> **실측 모드(`[실제]`) vs 모의 모드(`[모의]`) 확인**:
> 현재 저장소에는 `src/voice_pipeline/web/static/models/` 및 `vendor/` 아래에 모델 가중치 파일이 아직 배치되어 있지 않다.
> - 파일 없이 바로 실행하면 카메라 프레임 캡처(`640x480` → 오프스크린 캔버스 전처리)와 Web Audio 스케줄러 언더런을 검사하는 **`[모의] 모의 측정 (모델 미배치)`** 모드로 동작한다.
> - 후보 3종의 **실제 추론 시간(`[실제]`)**을 측정하려면 측정 전에 아래 로컬 경로(`face-probe.js:80~110`)에 모델 및 런타임 파일을 배치하고 해당 런타임 `<script>`를 로드해야 한다:
>   - 후보 1: `src/voice_pipeline/web/static/models/face_landmarker.task`, `src/voice_pipeline/web/static/vendor/tasks-vision/vision_wasm_internal.wasm`
>   - 후보 2: `src/voice_pipeline/web/static/models/version-RFB-320.onnx`, `emotion-ferplus-8.onnx`, `src/voice_pipeline/web/static/vendor/onnxruntime-web/ort-wasm-simd.wasm`
>   - 후보 3: `src/voice_pipeline/web/static/models/face-api/tiny_face_detector_model-shard1`, `face_expression_model-shard1`, `src/voice_pipeline/web/static/vendor/face-api/face-api.js`

---

## 3. 사용자가 할 일 (5단계)

개발 PC와 태블릿에서 각각 아래 5단계를 똑같이 수행한다 (기기당 약 30초 소요).

1. **페이지 열기**: 브라우저에서 위 §2의 `/static/face-probe.html` 주소를 연다. (동의 전 상태에서는 `카메라 꺼짐` 배지가 표시되며 카메라 불이 켜지지 않는다.)
2. **동의 후 카메라 켜기**: 상단 파란 배너의 **`[✅ 동의하고 카메라 켜기]`** 버튼을 누르고 브라우저 카메라 권한을 허용한다. 화면에 본인 얼굴 프리뷰가 나오고 우측 카드에 `오디오 컨텍스트 상태: running`, `네트워크 영상 전송 0 바이트 (안전)`이 표시되는지 확인한다.
3. **후보 3종 일괄 측정 실행**: 기본 설정(`측정 프레임 수: 50 프레임`, `목표 분석 주기: 100 ms (10 FPS)`, `백그라운드 오디오 스케줄러 활성화` 체크 유지) 그대로 **`[▶ 후보 3종 일괄 순차 측정]`** 버튼을 누르고 약 20초 동안 카메라를 바라본다.
4. **결과 텍스트 내려받기**: 하단 표의 1·2·3번 행이 모두 채워지면 우측의 **`[📥 결과 텍스트 내려받기 (.txt)]`** 버튼을 눌러 결과 파일(`face_probe_YYYYMMDDHHMMSS.txt`)을 내려받는다.
5. **카메라 끄기**: 상단의 **`[❌ 카메라 끄기]`** 버튼을 눌러 카메라 스트림을 종료한다.

---

## 4. 보고받을 항목 및 결과 파일(`bench/results/face_probe_20261027.txt`) 저장 방법

### 4-1. 측정 후 확인할 항목 (내려받은 `.txt` 보고서에 모두 자동 포함됨)

| 구분 | 보고 항목 |
|---|---|
| **기기·브라우저 환경** | 기기 구분(개발 PC / 태블릿 모델명), OS 버전, 브라우저(`User-Agent`), 코어 수(`hardwareConcurrency`), 카메라 해상도(`640x480`), `WebGL` 및 `WASM SIMD` 지원 여부 |
| **후보 3종 계측 지표** | `1. MediaPipe Face Landmarker` / `2. Face Detector + FER+ ONNX` / `3. face-api.js` 각각의:<br>• **모델 로드 시간 (`ms`)**<br>• **추론 지연 시간 `P50 (ms)` / `P90 (ms)` / 평균 (`ms`)**<br>• **실제 분석 주기 (`ms`) 및 실효 `FPS`**<br>• **오디오 언더런 발생 횟수 (`회`, 합격 기준: `0회`)**<br>• **파이프라인 모드 및 판정 (`[실제]` vs `[모의]`)** |

### 4-2. `bench/results/face_probe_20261027.txt` 저장 방법

- **개발 PC 단독 저장 시**:
  ```bash
  mkdir -p /home/maisc05/Desktop/healthup/healthup-voice-pipeline/bench/results
  cp ~/Downloads/face_probe_*.txt /home/maisc05/Desktop/healthup/healthup-voice-pipeline/bench/results/face_probe_20261027.txt
  ```
- **개발 PC 결과 파일과 태블릿 결과 파일을 하나로 합쳐 저장할 때 (권장)**:
  태블릿에서 내려받은 `.txt` 파일을 개발 PC로 옮긴 뒤 두 파일을 이어 붙여 `bench/results/face_probe_20261027.txt` 하나로 저장한다:
  ```bash
  mkdir -p /home/maisc05/Desktop/healthup/healthup-voice-pipeline/bench/results
  cat <개발PC_결과.txt> <태블릿_결과.txt> > /home/maisc05/Desktop/healthup/healthup-voice-pipeline/bench/results/face_probe_20261027.txt
  ```
