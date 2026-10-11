# STATE — 개발 현황 (세션마다 갱신)

> 갱신 규칙
> - 세션 시작: 「1. 현재 상태」와 「2. 다음 할 일」을 읽고 이번 세션 작업을 정한다.
> - 세션 끝: 「1」을 덮어써서 최신으로 만들고, 「2」를 다시 정렬하고, 「3. 세션 로그」 맨 위에 항목을 추가한다.
> - 직접 확인한 사실만 쓴다. 확인하지 않은 것은 "확인하지 않았다"로 쓴다.
> - 날짜는 절대 날짜(YYYY-MM-DD)로 쓴다.
> - 방향은 `OBJECTIVE.md`를 따른다. 이 문서는 목표를 바꾸지 않는다.

---

## 1. 현재 상태 (최종 갱신: 2026-10-11)

**결론**: 웹 데모(localhost)로 전 구간이 동작한다. 응답 지연은 목표의 약 3배다.
개발 장비에서 VAD 발화 끝 확정 → 재생 시작은 2.6~3.1초다 (목표 P50 1.0초).

**지금 할 일**: 첫 청크 대기와 GPU 경합을 줄인다 (「2」의 1번).

**단계**: 1주차 — TTS·STT 1순위 후보 동작 확인, 웹 데모 완료

**저장소**

- git `main` @ 이 STATE 커밋 (직전 코드 커밋 `17bbdfa`)
- 테스트: 96개 통과 (`.venv/bin/python -m pytest`, 약 17초)

**구현 완료**

- STT: `stt/` — `SenseVoiceSTT`(sherpa-onnx, CPU, INT8 2024-07-17), `VADSegmenter`(Silero, 무음 500ms 종료)
- LLM: `llm/openai_stream.py` — vLLM 스트리밍, 생각 모드 끔. `scripts/run_llm.sh` (MTP 기본 켬)
- TTS: `services/cosyvoice/` 서버 이미지 + `tts/cosyvoice.py` 어댑터. 스트리밍·연결 끊김 시 생성 중단
- 정규화: 숫자·날짜·시각·전화번호·약어 한국어 읽기 (`normalize.py`)
- 웹 데모: `web/` — 브라우저가 재생 주체. 브리지 프로토콜(`audio_begin`·프레임·`interrupt`·`playback`)을 그대로 쓴다
- 검사 도구: `bench/tts_roundtrip.py` (TTS 출력 → SenseVoice 전사 → CER), `stt_bench.py`, `llm_bench.py`
- 문장 세트 40개 (`bench/sentences_ko.txt`)

**미구현**

- Smart Turn·Kiwi 턴 종료, 선행 생성, 맞장구 필터, AEC 결정, OmniVoice 비교
- 미리 합성한 안전 안내 WAV (OBJECTIVE §3-9). 지금은 같은 TTS 엔진으로 합성한다
- Unreal 실제 연결, 립싱크

**실행 중인 서비스 (2026-10-11 세션 끝 기준)**

- `healthup-llm` (vLLM, 127.0.0.1:8000), `healthup-cosyvoice` (127.0.0.1:8091), 웹 데모 (`scripts/run_demo.sh`, 127.0.0.1:8080)
- 메모리: 사용 69GB, 가용 52GB

**실측 (개발 장비 AI TOP ATOM, GB10, 2026-10-11). 기능 확인용이며 최종 수치가 아니다**

| 구간 | 조건 | 결과 |
|---|---|---|
| STT | SenseVoice INT8, CPU 4스레드, ko.wav 4.6초 | 51ms, RTF 0.011 |
| LLM | Qwen3.8-27B NVFP4, 생각 끔, MTP 없음, 10문장 | TTFT P50 192ms / P90 198ms, 24 글자/초 |
| LLM | 같은 조건 + MTP(2토큰) | TTFT P50 224ms / P90 253ms, 32.6 글자/초 |
| TTS | CosyVoice3, 40문장, 단독 | TTFA P50 723ms / P90 737ms, RTF P50 0.37 / P90 0.41 |
| TTS | 같은 조건, LLM 1개 동시 생성 (MTP 켬) | TTFA P50 1426ms / P90 1485ms, RTF P50 0.76 / P90 0.92 |
| TTS 발음 | 왕복 CER, 40문장 | P50 0.07 / P90 0.37 |
| 전 구간 | 헤드리스 Chromium + 가짜 마이크(ko.wav), 3턴 | 재생 시작 2562 / 3068 / 2987ms |

- 전 구간 수치의 기준점은 VAD가 발화 끝을 확정한 시각이다. 실제 발화 끝은 약 500ms 더 앞이다.
- 전 구간 분해 (3턴): LLM 첫 토큰 174~233ms, 첫 청크 1074~1589ms, TTS 첫 오디오 2500~3006ms.
- 첫 청크 대기가 약 0.9~1.4초다. LLM 첫 문장이 끝날 때까지 청커가 기다린다.
- TTS RTF는 LLM과 동시 실행 시 P90 0.92다. 긴 문장에서 재생 끊김이 생길 수 있다. 청취로 확인하지 않았다.
- `tts_bench.py`의 RTF = 전체 소요 시간(TTFA 포함) ÷ 오디오 길이다.

**확인한 사실 (환경)**

- vLLM 26.07 이미지(vLLM 0.24)는 `Qwen3_5ForConditionalGeneration`을 지원한다. `--language-model-only`로 비전 인코더를 끈다.
- NVFP4 체크포인트에 MTP 가중치가 있다 (`mtp.*` 15개).
- CosyVoice3는 transformers 5.6.1에서 무의미한 소리를 냈다 (CER 1.0). 4.51.3으로 고정해 해결했다.
- CosyVoice 이미지의 cuDNN은 끈다. HiFT f0 예측기의 float64 conv가 GB10 cuDNN에서 실패한다.
- CosyVoice의 vLLM 가속은 쓸 수 없다. vLLM 0.24는 transformers 5.x가 필요하다.
- SenseVoice 2025-09-09 INT8 모델은 ko.wav를 한자 섞인 결과(lang=yue)로 냈다. 2024-07-17 모델은 정확했다.
- `nvidia-smi`는 GB10 메모리를 `[N/A]`로 보인다. 메모리는 `free -g`로 확인한다.
- NVFP4는 RTX 4090(Ada)에서 하드웨어 가속이 없다. 운영 PC용 양자화본은 따로 정한다.

**알려진 문제**

- 데모 경로의 STT 결과에 음절 사이 공백이 생긴다 ("조 금만 생각 을"). 전체 wav 전사에서는 생기지 않았다. 원인은 확인하지 않았다.
- CosyVoice3 출력은 fp16 LLM·flow 5스텝이다. 품질 저하 여부는 청취로 확인하지 않았다. 되돌리기: `CV_LLM_DTYPE=fp32 CV_STEPS=10`.
- 참조 음성은 공식 예제(중국어 화자)다. 한국어 화자 음성은 동의를 받은 뒤 바꾼다.
- 브라우저 AEC 효과는 실제 스피커·마이크로 확인하지 않았다.

**문서 불일치**: README를 dev-guide v2에 맞췄다. 남은 불일치는 없다.

**막힌 것 / 팀 결정 대기**: OBJECTIVE.md §8의 8건 모두 미결정

---

## 2. 다음 할 일 (우선순위 순)

1. **첫 청크 대기 줄이기** — 청커 첫 청크 규칙(§2.4, §5.2)을 실측으로 조정한다. 지금 0.9~1.4초다
2. **GPU 경합 줄이기** — LLM 동시 실행 시 TTS RTF P90 0.92다 (§5.5). 후보: 응답 길이 제한, TTS 우선순위, CosyVoice 첫 hop 조정(CER 함께 확인)
3. **사람 청취 확인** — `bench/results/`의 CosyVoice3 wav와 데모 소리를 팀원이 듣는다 (1주차 §9)
4. **OmniVoice 어댑터** — 같은 40문장으로 TTFA·RTF·CER을 비교한다 (§4.5)
5. **Smart Turn v3.2 + Kiwi 턴 종료, 선행 생성** — VAD 무음 500ms 대기를 줄인다 (§3.1, §3.2)
6. **STT 녹음 세트·Qwen3-ASR 기준점** — 팀원 녹음으로 CER을 잰다 (§3.3, §8.3)
7. 안전 안내 WAV 미리 합성, Windows 동작 확인

---

## 3. 세션 로그 (최신이 위)

### 2026-10-11 (2) — 웹 데모까지 구현

- 한 일: STT·LLM·TTS 실제 엔진을 붙이고 localhost 웹 데모를 만들었다
- 작업 방식: 코딩은 Sonnet 5.5(medium) 에이전트가 했다. 주 에이전트가 diff·테스트·실측으로 검증했다
- 확인: 테스트 96개 통과. 헤드리스 Chromium + 가짜 마이크로 3턴 대화와 barge-in을 확인했다 (`bench/web_e2e.py`)
- barge-in: 재생 중 두 번째 발화가 턴 1을 2457ms 지점에서 멈췄다 (`playback stopped`)
- 사용자 결정: LLM은 Qwen3.8-27B(Apache-2.0). 입력은 브라우저 마이크. 참조 음성은 공식 예제. 단계마다 커밋
- 결정: LLM은 메모리 대역폭 때문에 `nvidia/Qwen3.8-27B-NVFP4`를 쓴다. bf16 원본은 대비책이다
- 범위: 실제 LLM 결합은 OBJECTIVE §7의 3주차 항목이다. 사용자 요청으로 앞당겼다
- 코드 변경: `77628c6`..`17bbdfa` (8커밋). CLAUDE.md에 §2.1 작업 방식, §2.2 글쓰기 규칙을 추가했다
- 다음: 「2」의 1번


### 2026-10-11

- 한 일: `docs/dev-guide.md` v2를 읽고 `Claude/docs/OBJECTIVE.md`(정본), `Claude/docs/STATE.md`, 루트 `CLAUDE.md`를 만들었다
- 확인: 테스트 22개 통과, git `main` @ `5a012f9`, 장비 aarch64·GB10
- 코드 변경: 없음
- 환경: 가상환경을 상위 폴더에서 저장소 안 `.venv/`로 옮겼다 (같은 패키지 버전으로 다시 만들고 기존 것은 삭제). 테스트 22개 통과
- 다음: CosyVoice3 어댑터 (위 「2」의 1번)
