# STATE — 개발 현황 (세션마다 갱신)

> 갱신 규칙
> - 세션 시작: 「1. 현재 상태」와 「2. 다음 할 일」을 읽고 이번 세션 작업을 정한다.
> - 세션 끝: 「1」을 덮어써서 최신으로 만들고, 「2」를 다시 정렬하고, 「3. 세션 로그」 맨 위에 항목을 추가한다.
> - 직접 확인한 사실만 쓴다. 확인하지 않은 것은 "확인하지 않았다"로 쓴다.
> - 날짜는 절대 날짜(YYYY-MM-DD)로 쓴다.
> - 방향은 `OBJECTIVE.md`를 따른다. 이 문서는 목표를 바꾸지 않는다.

---

## 1. 현재 상태 (최종 갱신: 2026-10-27)

이번 세션: TTS 스트리밍 말 속도를 1.2로 올렸다. `model.tts(stream=True)`는 speed 인자를 쓰지 않아 파형 길이를 줄인다. CosyVoice 컨테이너는 재시작하지 않았다. vLLM은 기본 모델 `nvidia/Qwen3.8-27B-NVFP4`이다. LoRA는 없다.

**결론**: 지연 개선 코드를 모두 넣었다. 본문 첫 오디오는 P50 1.8초(목표 1.0초)로 아직 미달이다. 남은 병목은 LLM과 같은 GPU를 쓰는 TTS 첫 오디오(약 1.0초)다. 맞장구는 발화 끝 0.35초에 난다.
남은 일은 대부분 **녹음·청취·태블릿 실기**가 필요하다 (`docs/PLAN.md` §3).

**목표 변경 (사용자 결정 2026-10-21)**: 데모 마감 2026-11-27. 산업용 태블릿(브라우저 클라이언트, 연산은 서버)에서 STT·TTS 지연을 줄인다. LLM은 프롬프트 수준 조정만 한다. `OBJECTIVE.md`(11/08, Unreal 재생)와 다르다. 사용자 요청 때만 고친다.

**지금 할 일**: D1 음성 명령 녹음. 그 뒤에 D1b 문형을 녹음으로 다시 잰다. D2 실측, 태블릿, 청취는 그다음이다.

**단계**: 지연 개선 코드 완료. 녹음·청취·태블릿 시험 대기

**저장소**

- git `main`. 직전 문서 커밋은 `8c5f76a`. 푸시하지 않았다
- 테스트: 337 passed (`.venv/bin/python -m pytest`, 176.89초). 경고 5건은 기존 `test_tts_cosyvoice.py`의 `NotAppKeyWarning`
- 서비스: 이 세션에서 웹 서버와 컨테이너를 재시작하거나 멈추지 않았다. 실행 여부는 확인하지 않았다.

**구현 완료**

- (2026-10-21) 태블릿 원격 접속: `--tls-cert/--tls-key/--auth-token`, 비루프백은 TLS+토큰 필수, ws 압축 끔, 자동 재연결, RTT 표시, `scripts/gen_cert.sh`, `bench/net_probe.py`. 접속 로그를 꺼 토큰이 남지 않게 했다
- (2026-10-21) 선행 생성: `PendingTurn`(prepare/commit/abort)과 `_Outbox`. 확정 전에는 싱크·브라우저로 아무것도 보내지 않는다. 선행 중 119는 확정 시점까지 미룬다 (팀 결정 대기)
- (2026-10-21) barge-in 맞장구 필터: `barge_filter.py`. 말하는 중 소리가 시작되면 음량을 낮추고(덕킹) 600ms 안에 끝나면 전사해 "네"류는 무시, 아니면 끊는다
- (2026-10-21) 감정 hint: `llm/hint.py`. 감정 태그를 LLM 요청에만 넣는 어조 지시로 바꾼다. 자동 톤(TTS 프리셋)은 선택했을 때만 켜진다
- (2026-10-21) 청커: 토큰 중간의 문장 끝에서도 자른다. CV_HOP 기본 10. LLM 시스템 프롬프트를 고령자 심리상담용으로 바꿨다
- 끄개: `--no-speculate`, `--no-barge-filter`, `--no-emotion-hint`

- STT: `stt/` — `SenseVoiceSTT`(sherpa-onnx, CPU, INT8 2024-07-17), `VADSegmenter`(Silero, 무음 500ms 종료)
- LLM: `llm/openai_stream.py` — vLLM 스트리밍, 생각 모드 끔. `scripts/run_llm.sh` (MTP 기본 켬)
- TTS: `services/cosyvoice/` 서버 이미지 + `tts/cosyvoice.py` 어댑터. 스트리밍·연결 끊김 시 생성 중단
- 목소리 프리셋 (`GET /voices`): 한국어 여성 참조 음성(`models/voices/ko_female_a.wav`, 사용자 제공 `koreanFemale.mp3`에서 자름). `calm`(기본), `comfort`, `warm`, `steady`(instruct, 지시문은 중국어·영어 그대로), `ko_a`·`ko_b`(zero_shot, 참조 운율 복제), `zh_calm`(옛 중국어 참조, 비교용). `bright`는 삭제했다
  - 한국어 지시문은 쓸 수 없다. 모델이 지시문을 본문처럼 읽는다 (40문장 CER P50 1.0 이상)
- 자동 톤 (`tone.py`): 화면 기본값 「자동」. SenseVoice 감정 태그와 음량·말 속도로 턴마다 톤을 고른다. 임계값은 미튜닝 추측값이다
- STT 엔진 2개: `sensevoice`(CPU, 기본), `qwen3asr`(Qwen3-ASR-1.7B FP8 기본, vLLM 컨테이너 `healthup-asr` 127.0.0.1:8092, `scripts/run_asr.sh`, `ASR_MODEL_PATH`로 0.6B 선택)
- 턴 종료 (`endpoint/`): VAD 멈춤 200ms마다 STT + Smart Turn v3.2(ONNX CPU) + Kiwi 끝 어절 → `rules.py` 대기표 → 확정. 재발화 시 판단 버림
  - 빠른 재발화 감지: `VoiceOnset`(에너지 휴리스틱, 약 96ms). 확정 1.5초 안 재발화 + 본문 소리 전이면 같은 턴으로 이어 붙임
  - 화면: 배지 「듣는 중 (사유: …)」·「말 이어짐」·「턴 확정」, 남은 대기 막대, 「턴 종료」 선택(어미+운율 / 고정 500ms), 판단 로그 JSONL
- STT 후처리 (`stt/postprocess.py`): 언어 ko 고정, 한자·가나 제거, Kiwi 띄어쓰기 교정. VAD pre-roll 300ms
- 데모 설정: `--first-min`(기본 2), `--vad-silence-ms`(기본 500), 화면의 목소리 선택
- LLM 프롬프트: 첫 문장을 짧은 공감 문장으로, 차분한 말투, 느낌표 금지
- 맞장구: `backchannel.py` — "네.", "아, 네.", "네, 네."를 (250~1200ms 밖은 버림, 앞뒤 페이드) 목소리별로 시작 시 합성한다. STT 결과가 있으면 턴 맨 앞에 재생한다. 직전 문구는 반복하지 않는다. LLM 첫 청크의 "네,"를 지운다. 화면 「맞장구」 선택, `--no-backchannel`
- STT 선택: `stt/registry.py`(`STT_ENGINES`). 화면 「STT」 선택, 「다시 인식」(마지막 발화를 현재 STT로 재전사). 새 엔진 = 어댑터 파일 1개 + 등록 1줄
- 화면 버튼: 「대화 중지」(마이크·재생·연결 정지), 「답변 멈춤」(현재 답만 정지)
- 정규화: 숫자·날짜·시각·전화번호·약어 한국어 읽기 (`normalize.py`)
- 웹 데모: `web/` — 브라우저가 재생 주체. 브리지 프로토콜(`audio_begin`·프레임·`interrupt`·`playback`)을 그대로 쓴다
- 검사 도구: `bench/tts_roundtrip.py` (TTS 출력 → SenseVoice 전사 → CER), `stt_bench.py`, `llm_bench.py`
- 문장 세트 40개 (`bench/sentences_ko.txt`)

**미구현**

- AEC 결정, OmniVoice 비교
- 위기 감지 경로(Safeguard + 키워드 + 109·119 고정 안내), AI 고지·동의 화면, 로그 파기 (`docs/LEGAL_REVIEW.md` P0)
- 미리 합성한 안전 안내 WAV (OBJECTIVE §3-9). 지금은 같은 TTS 엔진으로 합성한다
- Unreal 실제 연결, 립싱크

**실행 중인 서비스 (2026-10-12 기준)**

- `healthup-llm` (vLLM, 127.0.0.1:8000), `healthup-cosyvoice` (127.0.0.1:8091), `healthup-asr` (127.0.0.1:8092), 웹 데모 (`scripts/run_demo.sh`, 127.0.0.1:8080)
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
| TTS 참조 음성 | 40문장, LLM 유휴, 2026-10-12. CER P50/P90, TTFA P50 | zh_calm 0.08/0.31, 727ms. calm(한국어 참조+중국어 지시) 0.06/0.33, 757ms. ko_a 0.05/0.29, 841ms. ko_b 0.08/0.33, 977ms. comfort·warm·steady 0.08~0.09/0.33~0.35 |
| 전 구간 | 헤드리스 Chromium + 가짜 마이크(ko.wav), 3턴, 2026-10-11 | 재생 시작 2562 / 3068 / 2987ms |
| 전 구간 | 같은 조건, 새 프롬프트 + first_min 2 + calm, 2026-10-12 | 재생 시작 1834 / 2726 / 2335ms |
| 첫 소리 | `first_audio_bench.py`, GPU 유휴, n=20, 옛 프롬프트 first_min 8 | 첫 청크 P50 1554 / P90 2428, 합계 P50 3112 / P90 3845ms |
| 첫 소리 | 같은 조건, 새 프롬프트 first_min 2 | 첫 청크 P50 769 / P90 1642, 합계 P50 2177 / P90 2889ms |
| 첫 소리 | 같은 조건, 새 프롬프트 first_min 6 | 첫 청크 P50 910 / P90 2171, 합계 P50 2538 / P90 3502ms |
| 전 구간 | 같은 조건 + 맞장구, 3턴, 2026-10-12 | 재생 시작(맞장구) 123 / 101 / 101ms, 본문 TTS 첫 오디오 1551 / 1521 / 1561ms |
| STT 띄어쓰기 | TTS wav 40 + ko.wav, VAD 구간 전부 이어 붙임, SenseVoice | CER(공백 제외) 0.133, 간격오류 0.123 → 0.048 (Kiwi, +0.7ms) |
| STT 비교 | 같은 41개, VAD 구간 56개, LLM 유휴 | SenseVoice+Kiwi CER 0.133 / 간격 0.048 / P50 42ms·P90 69ms. Qwen3-ASR CER 0.072 / 간격 0.034 / P50 417ms·P90 699ms |
| STT Qwen3-ASR 가속 | 같은 41개, LLM 유휴 → LLM 생성 중 | 1.7B bf16 P50 390 / P90 674ms. 1.7B FP8 CER 0.075, 220 / 371 → 468 / 803ms. 0.6B FP8 CER 0.069, 109 / 207 → 257 / 433ms |
| STT 데모 | 브라우저 3턴, Qwen3-ASR bf16 | STT 447ms, 재생 시작(맞장구) 509~511ms, 본문 첫 오디오 1804~2289ms |
| 턴 종료 | 브라우저, "…편할 거야." 3턴, 어미+운율 | 실제 발화 끝 → 턴 확정 294~333ms, 맞장구 359~396ms, 본문 첫 오디오 1794~1948ms |
| 턴 종료 | "어제 잠을 잘 못 잤는데" + 무음 450ms + "오늘은 좀 피곤해요." | 한 턴으로 확정 (머뭇거림 800ms → 이어짐 → 연결 어미 650ms → 이어짐 → EF 200ms), 확정 263ms |
| Smart Turn v3.2 | CPU 2스레드, 특징 추출 포함 | P50 41ms / P90 49ms |
| 톤 프리셋 | 16문장, CER P50 / P90 / F0 중앙값(자체 추정기) | calm 0.03 / 0.38 / 222Hz, comfort 0.03 / 0.33 / 259Hz, warm 0.03 / 0.38 / 247Hz, steady 0.06 / 0.38 / 231Hz |
| 목소리 | 40문장, CER P50 / F0 중앙값 / 초당 글자 시간 | bright 0.11 / 277Hz / 0.190초, calm 0.08 / 219Hz / 0.214초, calm2 0.12 / 127Hz / 0.277초 |
| CV_HOP | 40문장, LLM 유휴, 2026-10-21, `bench/hop_sweep.sh` | hop 25: TTFA P50 787 / P90 843ms, CER P50 0.14 / P90 0.33. hop 15: 615 / 626ms, 0.14 / 0.33. **hop 10: 507 / 521ms, 0.14 / 0.33**. hop 6: 502 / 520ms, 0.17 / 0.33, RTF P90 0.77 |
| 첫 소리 (최종) | `first_audio_bench.py`, LLM 동시, n=20, hop 10 + 새 프롬프트 + 청커 수정 | 첫 청크 P50 659 / P90 808, TTS 첫 소리 P50 991 / P90 1005, 합계 P50 1650 / P90 1808ms. 3/20은 "네,"로 시작해 낙관값이다 |
| e2e 브라우저 | 합성음 마이크 4문장 × 4회, hop 10, 선행 켬·끔 합침 n=16 | 턴 확정 P50 284 / P90 298, 맞장구 소리 P50 349 / P90 376, 첫 청크 P50 793 / P90 964, **본문 첫 오디오 P50 1798 / P90 1964ms** |
| e2e 브라우저 | 같은 조건, hop 25, n=4 | 본문 첫 오디오 P50 2272 / P90 2305ms → hop 10이 약 -470ms |
| 선행 생성 e2e | 켬 n=8 / 끔 n=8 (시험 문장 모두 EF_HIGH) | 본문 첫 오디오 P50 1834 / 1793ms. 차이 없음: 남은 대기가 0이라 선행이 걸리지 않았다 |
| 선행 생성 mock | LLM 첫 토큰 100ms, TTS 100ms, 확정 대기 300ms | 확정 → 첫 프레임 223 → 2ms |
| 루프백 ws | mock 서버, 20회 | RTT P50 1.0ms (ws), 0.75ms (wss). 실제 LAN·태블릿은 재지 않았다 |
| 질환 분류 모델 (AI Hub 71806 제공) | 검증 174세션 | 입력과 무관하게 거의 같은 점수. 쓰지 않는다 (`LEGAL_REVIEW.md` §5.1) |

- 전 구간 수치의 기준점은 VAD가 발화 끝을 확정한 시각이다. 실제 발화 끝은 약 500ms 더 앞이다.
- 전 구간 분해 (3턴): LLM 첫 토큰 174~233ms, 첫 청크 1074~1589ms, TTS 첫 오디오 2500~3006ms.
- TTS 첫 소리는 첫 청크 길이와 무관하게 약 1.4초다 (LLM 동시 실행). 지금 가장 큰 구간이다.
- 첫 청크는 여전히 P90 1.6초다. LLM이 "첫 문장 4~12자" 지시를 자주 어긴다.
- first_min 2는 "네,"만 따로 읽거나 쉼표 앞 조각을 읽을 수 있다. 청취로 확인하지 않았다.
- TTS RTF는 LLM과 동시 실행 시 P90 0.92다. 긴 문장에서 재생 끊김이 생길 수 있다. 청취로 확인하지 않았다.
- `tts_bench.py`의 RTF = 전체 소요 시간(TTFA 포함) ÷ 오디오 길이다.

- 맞장구 wav 길이: calm 388 / 334 / 837ms, calm2 680 / 758 / 1729ms, bright 182 / 398 / 1231ms (`bench/results/backchannel/`). SenseVoice 역전사로 문구를 확인했다 ("음"은 "응"으로 전사).
- 맞장구 끝 → 본문 사이 공백은 약 0.7~1.2초로 추정한다 (calm 기준). 청취로 확인하지 않았다.
- 화면 말풍선에는 LLM 원문("네, ...")이 그대로 보인다. 소리에서만 지운다.
- 맞장구 재생 중 barge-in은 단위 테스트로만 확인했다. 브라우저로 확인하지 않았다.

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

- (2026-10-21) 선행 생성은 `EF_HIGH`(대기 200ms)에서는 걸리지 않는다. VAD 무음 200ms가 이미 지나 남은 대기가 0이다. 이득은 `EF_LOW`·`EC_YO_*`에서만 생긴다. 실제 발화로 효과를 확인하지 않았다
- (2026-10-21) 본문 첫 오디오의 남은 병목은 TTS 첫 오디오다. LLM이 같은 GPU에서 나머지 답을 만드는 동안 약 1.0초 걸린다 (단독 0.5초). 운영 PC GPU 분리 효과는 확인하지 않았다
- (2026-10-21) CV_HOP 10의 음질(끊김, 경계음)은 청취로 확인하지 않았다. 되돌리기: `CV_HOP=25 services/cosyvoice/run.sh`
- (2026-10-21) 맞장구 필터의 말뭉치·600ms 기준은 가설이다. 실제 마이크와 AEC 없는 스피커에서는 봇 자신의 소리가 끼어들기로 잡힐 수 있다
- (2026-10-21) 태블릿: 인증서 설치, 브라우저 마이크, AudioWorklet, 재연결을 실제 기기로 확인하지 않았다. 토큰이 틀리면 브라우저가 계속 재연결한다. 재연결하면 서버 세션이 새로 생겨 대화 기록이 사라지고 화면 선택값이 기본으로 돌아간다
- (2026-10-21) 감정 태그(SenseVoice)의 한국어 정확도는 확인하지 않았다. 합성음 e2e에서는 모두 EMO_UNKNOWN이었다

- Smart Turn 분리력이 약하다. TTS 15문장에서 완전 문장 14/15가 0.5 이상, 중간 자른 문장 20/30만 0.5 미만. 규칙표에서 EF 대기(200/300ms)와 전사 없음 경우에만 쓴다
- 대기표 값은 가설이다. 실제 사용자 발화로 확인하지 않았다
- VoiceOnset 임계값(잡음 바닥 3배, -40dBFS)은 실제 마이크로 조정하지 않았다. 확정 직후 숨·기침이 응답을 다시 시작하게 할 수 있다
- 구간별 전사를 이어 붙이면 마침표가 끼어든다 ("어제. 잠을 잘 못 자는데."). 어미 판단에는 끝 구두점을 떼고 쓴다
- 자동 톤은 SenseVoice 감정 태그의 한국어 정확도에 달려 있다. 확인하지 않았다. 데모 2턴은 모두 calm이었다
- CosyVoice3 출력은 fp16 LLM·flow 5스텝이다. 품질 저하 여부는 청취로 확인하지 않았다. 되돌리기: `CV_LLM_DTYPE=fp32 CV_STEPS=10`.
- 목소리의 차분함은 F0·에너지 지표로만 확인했다. 사람 청취 확인은 하지 않았다. 사용자는 기존 목소리가 "너무 깨발랄하다"고 했다
- 참조 음성은 공식 예제(중국어 화자)다. 한국어 화자 음성은 동의를 받은 뒤 바꾼다.
- 브라우저 AEC 효과는 실제 스피커·마이크로 확인하지 않았다.

**문서 불일치**: README를 dev-guide v2에 맞췄다. 남은 불일치는 없다.

**막힌 것 / 팀 결정 대기**: OBJECTIVE.md §8의 8건 모두 미결정

---

## 2. 다음 할 일 (우선순위 순)

1. **D1 음성 명령 녹음** 후 D1b 문형을 녹음으로 다시 잰다. 녹음 파일은 아직 없다
2. **태블릿 실기 시험** — `scripts/gen_cert.sh <서버IP>` → 인증서 설치 → `https://<IP>:8080/?t=<토큰>` → RTT·마이크·재연결 확인
3. **청취 확인** — `bench/results/latency_2610/hop10/` vs `hop25/` wav, 감정 hint 문장, 맞장구 덕킹
4. **턴 종료 규칙표 조정** — 녹음 로그로 `rules.py` 대기값·`SPECULATE_RULES`·`VoiceOnset` 임계값을 정한다. 선행 생성 효과도 여기서 잰다
5. **위기 감지 경로 + AI 고지·동의 + 로그 파기** — `docs/LEGAL_REVIEW.md` P0. 데모 전에 필요하다
6. **운영 PC 측정** — GPU 분리 시 TTS 첫 오디오 (예상 약 0.5초), LLM 양보 방식은 필요할 때만
7. **AI Hub 71806 평가 세트** — `suicidal` 라벨 발화로 위기 시나리오를 만든다 (데이터는 저장소 안 gitignore 폴더, 보안서약 범위)

---

## 3. 세션 로그 (최신이 위)

### 2026-10-27 TTS 말 속도, vLLM 모델 확인

한 일: `CV_SPEED` 기본 1.2. 스트리밍 청크를 이어서 줄인다. vLLM `/v1/models`와 실행 인자를 읽었다.
확인한 것: `.venv/bin/python -m pytest` 337 passed, 176.89초. 서빙 모델 id는 `Qwen/Qwen3.8-27B`, root는 `nvidia/Qwen3.8-27B-NVFP4`. `--lora-modules` 없음. 앱 요청 이름과 같다.
확인하지 않은 것: 재시작 뒤 청취. 파인튜닝 가중치 파일은 `llm-finetune-research/FINETUNE_PLAN.md`만 있고 체크포인트는 찾지 못했다.
다음: CosyVoice 컨테이너를 다시 띄운 뒤 말 속도를 듣는다.

### 2026-10-27 정적 파일 캐시 무효화

한 일: index 응답에 `?v=수정시각` 을 붙인다. 페이지와 `/static/` 은 `Cache-Control: no-store` 이다. 화면 CSS는 `app.css` 로 분리했다.
확인한 것: `.venv/bin/python -m pytest` 335 passed, 175.74초. `tests/test_static_cache.py` 3 passed.
확인하지 않은 것: Cloudflare 터널 뒤 태블릿 화면.
코드 변경: `web/app.py`, `web/static/index.html`, `web/static/app.css`, `tests/test_static_cache.py`, `tests/test_face_hud.py`
다음: 웹 서버를 재시작한 뒤 터널 주소로 다시 연다.

### 2026-10-27 대화 칸 옆에 큰 카메라

한 일: 얼굴 표정이 켜지면 대화와 카메라를 한 줄에 반반 둔다. 개발 모드에서는 그 화면 위에 얼굴 상자와 랜드마크를 그리고 상태 패널을 같이 둔다. 꺼지면 대화가 전체 폭이다. 700px 이하는 위아래로 쌓는다.
확인한 것: `.venv/bin/python -m pytest` 332 passed, 175.52초. `tests/test_face_hud.py` 6 passed.
확인하지 않은 것: 브라우저에서 태블릿 가로, 세로, 폰 폭.
코드 변경: `web/static/index.html`, `app.js`, `face-hud.js`, `tests/test_face_hud.py`, `bench/web_ui_check.py`
다음: 페이지를 다시 열고 카메라 켜짐과 꺼짐을 본다.

### 2026-10-27 저장된 얼굴 설정에서 카메라 복원

한 일: 처음 불러올 때 `prefs.face`와 `faceConsented`가 맞으면 `startCamera()`를 호출한다. 동의가 없으면 체크를 끈다.
확인한 것: `.venv/bin/python -m pytest` 331 passed, 175.75초. `tests/test_face_hud.py` 5 passed. 개발 모드에서만 `showPreview`가 참이다.
확인하지 않은 것: 브라우저에서 실제 카메라 미리보기.
코드 변경: `web/static/app.js`, `web/static/face-hud.js`, `tests/test_face_hud.py`
다음: 개발 모드로 페이지를 다시 열고 미리보기가 따라오는지 본다.

### 2026-10-27 주석 쉼표, D1b 문형 보강

한 일: 표정 모델 받기 스크립트 주석의 가운데점을 쉼표로 바꿨다. 의도 분류기에 조사, 확인 구어, 위기 어간을 넣었다.
확인한 것: `.venv/bin/python -m pytest` 330 passed, 174.82초. `tests/test_intent.py` 10 passed. D1 녹음 파일은 없다.
확인하지 않은 것: D1 음성 재실행, 브라우저, 서버 부하.
코드 변경: `80ecec5` `scripts/fetch_face_models.sh`. `dc8cabe` `intent.py`, `tests/test_intent.py`.
문서: `docs/plans/webapp_plan.md`, `docs/PLAN.md`, `Claude/docs/STATE.md`.
다음: D1 녹음 뒤 같은 문형을 음성으로 다시 잰다.

### 2026-10-27 개발 모드 얼굴 상태 표시

한 일: 개발 모드에서만 작은 카메라 미리보기와 얼굴 박스, 랜드마크, 상태 패널을 추가했다.
확인한 것: `.venv/bin/python -m pytest` 329 passed. 브라우저로 카메라 화면은 확인하지 않았다.
코드 변경: `web/static/app.js`, `index.html`, `face-hud.js`, `tests/test_face_hud.py`, `bench/web_ui_check.py`, `scripts/fetch_face_models.sh`
다음: 태블릿에서 설정 패널의 개발 모드를 켜고 얼굴 박스가 따라오는지 본다.

### 2026-10-21 — 목표 변경(태블릿·지연), 지연 개선 7개, 법적 검토, 파인튜닝 조사

- 사용자 결정: ① 우울 분류기 미사용, 71806 라벨은 평가용 ② LLM은 프롬프트 수준 ③ 핵심은 STT·TTS 지연(태블릿 브라우저 + 서버 연산) ④ 데모 2026-11-27 ⑤ 감정 맞춤은 LLM 프롬프트로
- 한 일: 태블릿 원격 접속(TLS·토큰·재연결·RTT), 선행 생성, 첫 문장·청커 수정, CV_HOP 스윕, barge-in 맞장구 필터, 감정 hint, 프롬프트 정리. 작업 방식: 코딩은 Sonnet 5.5(medium) 에이전트 2개(태블릿, 선행 생성)와 직접 작업. 주 에이전트가 diff·테스트·실측으로 검증했다
- 확인한 것: 테스트 243개 통과. CV_HOP 10에서 본문 첫 오디오 P50 -470ms (e2e, 같은 날). 선행 생성은 EF_HIGH에서 걸리지 않아 이득이 없었다. 첫 청크에 문장이 둘 들어가던 청커 문제를 찾아 고쳤다
- 확인하지 않은 것: 태블릿 실기, 실제 마이크 맞장구, CV_HOP·감정 hint 청취, 선행 생성의 실제 효과
- 코드 변경: `b739529` `8c970f8` `584b3b3` `323ee0c` `2cc9044` `b910f9f` `d2805cd`
- 문서: `docs/STATE.md`(모듈별), `docs/PLAN.md`(재정렬), `docs/LEGAL_REVIEW.md`. 저장소 밖 `healthup/llm-finetune-research/FINETUNE_PLAN.md`
- 법적 검토 결론: 상담 내용은 민감정보(별도 동의), 진단·치료 표방 금지, AI 기본법 §31 고지는 시행 중. 제공 KlueBERT 3개는 입력과 무관하게 거의 상수라 쓰지 않는다
- 서비스 상태: LLM·TTS 컨테이너 실행 중 (2026-10-14에 내렸다가 이번에 다시 켰다)
- 다음: 「2」의 1번 (사용자 입력)

### 2026-10-12 (6) — 한국어 참조 음성으로 교체

- 한 일: 사용자가 억양이 중국어 같다고 지적했다. 원인은 중국어 참조 음성(공식 예제)이었다. 사용자 제공 `koreanFemale.mp3`(25.9초)를 잘라 참조 A(10.7초)·B(12.1초)를 만들었다. 전사는 Qwen3-ASR로 하고 "피언할"을 "피어날"로 고쳤다
- 확인한 것: 한국어 instruct 지시문은 본문에 읽혀 나온다 (CER 1.0 이상). 중국어·영어 지시문 + 한국어 참조는 정상이다 (CER P50 0.06~0.11)
- 확인한 것: 억양 개선 여부는 사람이 듣지 않았다. 비교 파일: `bench/results/ko_listen/` (zh_calm → calm → ko_a → ko_b 순서)
- 코드 변경: `services/cosyvoice/server.py` 프리셋. 웹 데모 맞장구 일부가 길이 범위 밖으로 버려진다 (ko_a·ko_b·zh_calm·comfort 일부)
- 참고: TTS 후보 조사 — Chatterbox Multilingual(MIT, 한국어 포함)이 새 후보다. Fish Audio S2는 비상업 라이선스라 제외. OBJECTIVE §4는 바꾸지 않았다
- 다음: 청취 비교 후 기본 목소리(`calm` 또는 `ko_a`)를 정한다

### 2026-10-12 (5) — 어미+운율 턴 종료, 실시간 판단 표시

- 사용자 결정: 방법 1(Kiwi 어미 + Smart Turn v3.2)로 구현. 테스트 단계라 판단 사유를 실시간으로 보인다
- 한 일: `endpoint/`(smart_turn·ending·rules·turn), 세션 연결, 배지·대기 막대·판단 로그, VoiceOnset, 확정 직후 이어 붙이기
- 확인: 테스트 173개 통과. 브라우저로 멈춤 있는 문장이 한 턴으로 확정되는 것을 확인했다. 완전 문장 확정 294~333ms
- 발견: 첫 구현은 재발화 감지가 늦어(VAD min_speech 250ms) 650ms 대기 뒤 턴을 잘랐다. VoiceOnset과 이어 붙이기로 고쳤다
- Kiwi 실측: "-는데요"는 EC+JX('요'), "-거든요"는 EF 한 형태소. 둘 다 "연결 어미+요"로 묶었다
- 지연표 기준점이 실제 발화 끝으로 바뀌었다. 이전 수치와 직접 비교하지 않는다

### 2026-10-12 (4) — 자동 톤, 띄어쓰기

- 사용자 요청: calm2 삭제, 여성 목소리만. 사용자 말투에 맞춰 톤 자동 조정. 띄어쓰기 해결, 한국어만 인식
- 한 일: comfort·warm·steady 프리셋(지시문 후보를 CER·F0로 선택), 자동 톤, Kiwi 띄어쓰기·한국어 전용 후처리
- 확인: 테스트 124개 통과. 브라우저 2턴에서 "조금만 생각을 하면서 살면" 정상 띄어쓰기, STT 38~66ms
- 톤이 실제로 위로·따뜻함으로 들리는지는 청취로 확인하지 않았다 (`bench/results/voices/`)
- Qwen3-ASR-1.7B 추가 (사용자 선택). vLLM 0.24가 기본 지원. 오류율은 SenseVoice의 약 절반, 지연은 약 10배
- 사용자 결정 (2026-10-12): Qwen3-ASR은 1.7B FP8로 유지한다. 0.6B는 실제 음성 비교 뒤 다시 본다
- Qwen3-ASR 지연은 디코드(토큰당 약 20ms, 24토큰)가 대부분이다. 인코더+prefill은 29ms. FP8로 기본을 바꿨다
- 확인하지 않음: 실제 마이크 음성 CER. 0.6B가 합성음에서 1.7B보다 낮은 CER을 낸 이유

### 2026-10-12 (3) — STT 선택, 중지 버튼, 맞장구 수정

- 사용자 피드백: 한국어 인식률이 낮다. 맞장구가 목 가다듬는 소리 같다. 중지 버튼이 필요하다
- 한 일: STT 등록부와 화면 선택, 「다시 인식」, 「대화 중지」·「답변 멈춤」, 맞장구 문구에서 "음." 삭제·페이드·길이 필터
- 확인: 테스트 115개 통과. 헤드리스 Chromium으로 세 버튼 동작을 확인했다
- 확인: 데모 경로 음절 공백은 SenseVoice가 앞부분이 50ms 이상 잘린 입력에서 낸다 (ko.wav). 글자 내용은 같다
- 맞장구 남은 수: calm 2개, calm2 1개, bright 3개 (길이 필터 결과). calm2는 매번 같은 문구다
- 대기: 사용자가 추가할 STT를 고른다 (후보: Qwen3-ASR-1.7B 추천, Whisper large-v3-turbo, Zipformer)

### 2026-10-12 (2) — 미리 합성한 맞장구

- 사용자 결정: TTS 대기를 가리는 미리 합성한 맞장구를 쓴다
- 한 일: `BackchannelBank`, 파이프라인 opener, 첫 청크 "네," 제거, 화면 선택. 어댑터가 서버 기본 목소리를 받는다
- 확인: 테스트 110개 통과. 브라우저 3턴에서 재생 시작 101~123ms, 본문 첫 오디오 1521~1561ms
- 데모는 `nohup`으로 띄웠다. 세션을 닫아도 남는다
- 다음: 「2」의 1번

### 2026-10-12 — 말 시작 지연·목소리 개선

- 사용자 피드백: STT가 느리다. 말 시작이 늦다. 목소리가 너무 깨발랄하다
- 확인: STT는 23~29ms다. 체감 지연은 VAD 무음 500ms 대기와 첫 청크·TTS 첫 소리다
- 한 일: 차분한 목소리 프리셋 3개, 짧은 첫 문장 프롬프트, first_min 2, 데모 목소리 선택·설정 플래그
- 확인: GPU 유휴 재측정에서 합계 P50 3112 → 2177ms. 데모 3턴 재생 시작 1834 / 2726 / 2335ms
- 코드 변경: `5c48502`. 테스트 99개 통과
- 다음: 「2」의 1번

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
