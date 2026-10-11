# CLAUDE.md — healthup-voice-pipeline 작업 안내

HealthUp Station 버츄얼휴먼의 음성 파이프라인(마이크 → STT → 상담 LLM → 청커 → TTS → Unreal 재생·립싱크)이다.
모든 설명·문서는 한국어로 쓴다. 코드 식별자와 기술 용어는 원문 그대로 둔다.

## 1. 세션 시작 시 읽는 순서 (매번)

1. **이 파일** (`CLAUDE.md`) — 규칙과 폴더 구조
2. **`Claude/docs/OBJECTIVE.md`** — 고정 정본. 목표·범위·원칙·후보·합격 기준
3. **`Claude/docs/STATE.md`** — 현재 상태와 다음 할 일. 「2. 다음 할 일」의 맨 위 항목이 기본 작업이다
4. **`docs/dev-guide.md`의 관련 절만** — 약 800줄이므로 전체를 읽지 않는다. 작업에 맞는 절을 찾아 읽는다

| 작업 | dev-guide 절 |
|---|---|
| 지연 예산·목표 | §2, §8.5 |
| 턴 종료·STT | §3 |
| TTS 어댑터·후보 | §4 (인터페이스 §4.5) |
| 청커·정규화·큐·GPU | §5 |
| barge-in·turn_id | §6 |
| Unreal 브리지·립싱크·플랫폼 | §7 |
| 측정·테스트 세트 | §8 |
| 일정·팀 결정 | §9, §10 |
| 개발 장비 (ATOM·MacBook) | §14 |

사용자가 다른 작업을 지시하면 그 지시를 따른다. 다만 OBJECTIVE와 어긋나면 먼저 알린다.

## 2. 세션 진행 절차

1. 위 순서로 읽고, 이번 세션에서 할 일을 사용자에게 한두 줄로 확인한다
2. 작업한다 (방식은 §2.1). 기존 코드의 문체(짧은 주석, 한국어 docstring, 타입 힌트)를 따른다
3. 테스트를 돌린다: `.venv/bin/python -m pytest` (저장소 루트에서)
4. **세션 끝에 `Claude/docs/STATE.md`를 반드시 갱신한다**
   - 「1. 현재 상태」 덮어쓰기 (커밋 해시, 테스트 수, 구현/미구현)
   - 「2. 다음 할 일」 재정렬
   - 「3. 세션 로그」 맨 위에 날짜 항목 추가 (한 일, 확인한 것, 코드 변경, 다음)
5. 실측 결과(TTFA, RTF, CER, 지연 P50·P90)는 수치·장비·조건을 함께 STATE에 남긴다

## 2.1 작업 방식: 코딩 에이전트 + 검증

- 코드 작성은 코딩 에이전트에게 맡긴다. Agent 도구로 `model: "sonnet"`(Sonnet 5.5), `effort: "medium"`을 지정해 호출한다
- 주 에이전트(Opus)는 작업을 나누고, 결과를 검증한다
- 검증 항목:
  1. diff를 직접 읽는다
  2. `.venv/bin/python -m pytest`를 돌린다
  3. 해당하면 벤치·데모를 실제로 실행한다
- 에이전트 프롬프트에 계약을 명시한다. 에이전트가 CLAUDE.md를 읽었다고 가정하지 않는다
  - 인터페이스 경로, 코드 문체(한국어 docstring, 짧은 주석, 타입 힌트), 테스트 명령, 고치면 안 되는 파일
- 검증을 통과한 단계만 커밋한다

## 2.2 글쓰기 규칙 (모든 마크다운 파일·보고서·답변)

출력은 ASD-STE100(Simplified Technical English) 원칙을 한국어에 맞게 적용: 첫 1~3줄에 결론, 그다음 "지금 할 일" 1개
한 문장에 정보 하나만; 절차 문장은 20단어 이하, 설명 문장은 25단어 이하
절차는 번호 목록, 번호당 동작 하나, 경고는 해당 절차 앞에 배치
기술 용어는 처음 등장 시 "한국어 (English)" 형식, 같은 대상은 항상 같은 용어 사용
목록은 7개 이하, 선택지는 2~3개 + 추천 1개 표시, 질문은 한 번에 1개만
불확실한 내용은 "확인하지 않았다"라고 명시, 추측을 사실처럼 쓰지 않음, 관용구·모호한 표현 금지
진행 상태는 ✅ 완료 / 🔄 진행 중 / ⏭️ 다음으로 표시, 답변 끝에 같은 내용 재요약 금지

## 3. 문서 규칙

| 문서 | 성격 | 수정 |
|---|---|---|
| `Claude/docs/OBJECTIVE.md` | 고정 정본 | **사용자가 명시적으로 요청할 때만.** 고치면 변경 이력에 한 줄 추가 |
| `Claude/docs/STATE.md` | 진행 기록 | 매 세션 끝에 갱신 |
| `docs/dev-guide.md` | 설계 근거 (사용자 문서) | 사용자가 요청할 때만 |
| `README.md` | 저장소 소개·실행법 | 구조·실행법이 바뀌면 갱신 |

- 확인하지 않은 수치·주장은 "확인하지 않았다"로 쓴다 (dev-guide와 같은 원칙).
- 문장은 짧은 서술문으로 쓴다.

## 4. 바꾸면 안 되는 것 (요약, 자세한 것은 OBJECTIVE §3)

- 외부 AI API·클라우드로 사용자 음성·상담 텍스트를 보내지 않는다. 클라우드 TTS는 승인 전까지 쓰지 않는다
- 모든 메시지·오디오 프레임에 turn_id. 정지 = turn_id 무효화
- 엔진은 `TTSEngine` 어댑터 뒤에. 청커·큐·재생 코드는 엔진 때문에 바꾸지 않는다
- 선행 생성 결과는 턴 확정 전에 재생하지 않는다
- 개발 장비 수치를 최종 수치로 쓰지 않는다 (운영 PC: Windows 11 + RTX 4090 ×2)

## 5. 폴더 구조

```text
healthup/                      ← 상위 폴더 (저장소 아님)
  healthup-voice-pipeline/     ← git 저장소 루트 (이 파일 위치)
    .venv/                     ← Python 3.12 가상환경 (gitignore 대상, `pip install -e ".[dev]"`)
    CLAUDE.md
    Claude/docs/
      OBJECTIVE.md             고정 정본
      STATE.md                 세션별 진행 기록
    docs/dev-guide.md          설계 근거 v2
    src/voice_pipeline/
      pipeline.py              LLM 스트림 → 청커 → 정규화 → TTS → 20ms 프레임 → 싱크
      turns.py                 TurnGuard: 정지 = 현재 turn_id 무효화
      chunker.py               SemanticChunker
      normalize.py             기호·단위 → 읽을 수 있는 말
      audio.py                 AudioFrame, Framer, fade_out
      trace.py                 턴 추적 기록, P50·P90
      sinks.py                 MemorySink(테스트), LocalSpeakerSink(sounddevice)
      stt/                     STTEngine 인터페이스, SenseVoiceSTT, VADSegmenter (sherpa-onnx, CPU)
      llm/                     §32 이벤트(임시 스키마), MockLLM, SSE, OpenAIStreamLLM(vLLM)
      tts/                     TTSEngine 인터페이스(base.py), MockTTS, CosyVoiceTTS — 새 엔진은 여기
      bridge/                  Unreal WebSocket 싱크, 프레임 형식, 서버 쪽 싱크, 가짜 Unreal 서버
      web/                     localhost 웹 데모 (브라우저 = 재생 주체)
    services/cosyvoice/        CosyVoice3 서버 이미지 (GB10 aarch64, transformers 4.51.3 고정)
    scripts/                   run_llm.sh·stop_llm.sh (vLLM docker), run_demo.sh
    models/                    모델 파일 (gitignore 대상, 받는 법은 README)
    bench/                     tts_bench·tts_roundtrip(CER)·stt_bench·llm_bench·web_e2e, sentences_ko.txt(40)
    tests/
```

## 6. 명령어 (저장소 루트에서)

```bash
.venv/bin/python -m pytest                                    # 테스트
.venv/bin/python bench/latency_sim.py --ttft-ms 400 --tps 40  # 지연 시뮬레이션
.venv/bin/python bench/tts_bench.py --engine mock             # TTS 벤치 (--save-wav로 청취 파일)
.venv/bin/python -m voice_pipeline.llm.sse --port 8090        # 모의 LLM 서버
.venv/bin/python -m voice_pipeline.bridge.fake_unreal --port 8765  # 가짜 Unreal 서버
services/cosyvoice/run.sh                                     # TTS 서버 (127.0.0.1:8091)
scripts/run_llm.sh                                            # vLLM (127.0.0.1:8000, 준비 약 6분)
scripts/run_demo.sh                                           # 웹 데모 (http://localhost:8080/)
.venv/bin/python bench/tts_roundtrip.py bench/results/<dir>   # TTS 발음 검사 (왕복 CER)
```

새 TTS 엔진: `tts/`에 `TTSEngine` 구현 → `bench/tts_bench.py`의 `ENGINES`에 등록 → 같은 문장 세트로 TTFA·RTF 측정.

## 7. 개발 장비 주의

- 현재 장비는 AI TOP ATOM (aarch64, NVIDIA GB10, 통합 메모리). x86_64 전용 휠(예: CosyVoice3 ttsfrd)은 설치되지 않는다
- 모델을 올리기 전에 가용 메모리를 확인한다 (`free -g`, `nvidia-smi`)

## 8. git

- 사용자가 요청할 때만 커밋·푸시한다
- 커밋 메시지는 기존 형식을 따른다 (예: `docs: ...`, 한국어 요약 허용)
