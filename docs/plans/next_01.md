# next_01 — 단일 GPU 경합 완화를 위한 LLM 양보 및 첫 문장 프롬프트 보정 (재검토 반영판)

> 결론: 본문 첫 오디오 지연은 백분위수 50 (P50) 1798ms로 목표 1000ms(`OBJECTIVE.md` §5)에 미달한다. 거대 언어 모델 (LLM) 동시 실행 시 첫 청크 음성 합성 (TTS) 첫 오디오 합성 시간 (TTFA)은 P50 991ms(`Claude/docs/STATE.md` 실측 표, `CV_HOP=10`)다. 양보로 300~450ms를 줄여도 약 1350~1500ms로 1000ms 목표에는 못 미치며, 남은 차이는 실제 발화 기반 선행 생성(`docs/PLAN.md` §3)과 5주차 그래픽 처리 장치 (GPU) 2장 분리로 줄인다 (두 방법의 실제 단축 효과는 확인하지 않았다).
> 지금 할 일: 1단계로 기본 동작을 유지한 채 옵션·별도 상수로 프롬프트·청커 보정과 LLM 양보를 구현하고, `pytest` 통과 뒤 (1a)·(1b) 벤치를 분리 측정한다.
>
> - 상태: ✅ 지연 개선 1~7단계 완료 / ✅ `next_01_review.md` 재검토 반영 / ⏭️ 1단계 단위 시험 및 벤치 분리 실측
> - 일정: `docs/PLAN.md` §4 1주차(10/21~10/25) 범위 안이며 §3(녹음·청취·태블릿 대기)을 막지 않는다

---

## 0. 후보 선택 (사용자 입력 없이 가능한 작업 3개)

1. **(추천) ⏭️ LLM 양보(`--llm-yield`) 및 첫 문장 프롬프트·청커 보정 (`docs/PLAN.md` §2 후보 1)**: 녹음·청취·태블릿 IP 없이 서버에서 즉시 본문 첫 오디오 지연을 줄일 수 있는 유일한 남은 지연 개선 후보다.
2. ⏭️ 판단 로그 전사 저장 기본 끔 및 위기 키워드 고정 경로 (`docs/LEGAL_REVIEW.md` P0): 데모 전 필수이나 음성 인식 (STT)·TTS 저지연 목표와 직접 관련이 없다.
3. ⏭️ 태블릿 웹소켓 (WebSocket) 재연결 시 대화 기록·설정 유지 (`Claude/docs/STATE.md` §1): 원격 연결 보조 개선이나 지연 단축 효과는 없다.

---

## 1. 목표·합격 기준·중단 조건

- **기본값 보존 원칙**: 합격 전에는 기존 기본 동작을 바꾸지 않는다. 새 프롬프트는 `PROMPT_NO_FILLER` 별도 상수로 두고, 청커 보정은 `SemanticChunker(skip_filler_comma=False)` 옵션(기본 `False`)으로 둔다. (1a) 합격 뒤에만 기본값으로 올린다.
- **기준선 및 지표 단일화**: 단독 TTFA P50 507ms(`sentences_ko.txt` 40문장, LLM 유휴)와 991ms(`first_audio_bench.py` 짧은 첫 청크 20개, LLM 동시)는 문장 세트가 달라 직접 뺄 수 없으며 단축 폭은 **확인하지 않았다**. 군더더기 지표는 **"첫 청크가 군더더기(`네,`·`아,`·`음,`·`예,`·`응,`) 단독으로 잘린 턴 수(20턴 중)"** 하나로 통일하고, (1a)의 `current` 측정값을 기준선으로 삼는다 (기존 기록은 `final_firstaudio.txt` 4/20, `STATE.md` 3/20으로 달랐다).
- **2차 요청 시점 및 `max_tokens` 배분**: 기본 시점은 **1청크 TTS 합성 완료 직후(`after_chunk1_tts`)**로 하되, 첫 청크가 7~14자로 짧으면 2차 첫 토큰 시간 (TTFT)(약 224ms) + 2청크 생성 + TTS 시간 때문에 `gap_ms > 0` 0/20을 못 맞출 수 있으므로 (1b)에서 `after_first_frame`과 비교해 합격 시점을 고른다 (**확인하지 않았다**). `max_tokens`는 전체 256 중 **1차 `64`**, **2차 `256 - 1차 생성 토큰 수`(최소 192)**로 정한다.
- **청크 사이 무음 공백(`gap_ms`) 정의**: 1청크 첫 프레임 시각 $t_1$, 1청크 오디오 길이 $D_1 = \text{pcm\_bytes}_1 / (2 \times \text{sample\_rate}) \times 1000\text{ (ms)}$, 2청크 첫 프레임 시각 $t_2$일 때 `gap_ms = max(0, (t2 - t1) - D1)`이다.
- **합격 기준 (모두 만족해야 합격)**:
  1. **(1a) 프롬프트·청커 단독(`--prompt fixed --chunker fixed` vs `current`, n=20 교차)**: 군더더기 단독 첫 청크 0/20, 첫 청크 시각 P50이 같은 실행의 `current` 대비 +100ms 이내.
  2. **(1b) 1단계 벤치(`--compare-yield`, n=20 교차)**: 같은 첫 청크 세트에서 TTS 첫 소리 P50 ≤ 600ms, 합계(첫 청크+TTS) P50이 동시 실행 대비 ≥ 300ms 단축, `gap_ms > 0`인 턴 **0/20**.
  3. **(2단계) 브라우저 전 구간(`web_e2e.py`, n=16 교차)**: 같은 날 교차 측정한 `--no-llm-yield` 대비 `--llm-yield`의 본문 첫 오디오(`tts_first_audio`) P50 개선 ≥ 300ms, 브라우저 재생 중 청크 사이 끊김(`gap_ms > 0`) 턴 **0/16**.
- **중단 조건**: (1a) 미달 시 프롬프트·청커 기본값을 바꾸지 않고 `current` 위에서 (1b) 양보만 시험한다. (1b) 미달 시 2단계(`pipeline.py`·`web/`)는 구현하지 않고 중단한다. 2단계 미달 시 기본값을 끔(`--no-llm-yield`)으로 두고 종료한다.

---

## 2. 변경 파일 (2단계 분리)

### 2.1 1단계 (벤치 검증 및 단위 시험, 기본 동작 불변)

1. `src/voice_pipeline/llm/openai_stream.py` — `PROMPT_NO_FILLER` 상수를 추가하고(`DEFAULT_SYSTEM_PROMPT` 유지), `continue_final_message: True`·`add_generation_prompt: False` 이어쓰기 요청과 1차(`64`)·2차(`256 - 1차 토큰 수`) `max_tokens` 설정을 지원한다.
2. `src/voice_pipeline/chunker.py` — `SemanticChunker`에 `skip_filler_comma: bool = False` 옵션을 추가해 켜졌을 때만 첫 청크 앞 군더더기(`"네,"`, `"아,"`, `"음,"`, `"예,"`, `"응,"`) 뒤 쉼표 분할을 건너뛴다.
3. `tests/test_chunker.py`, `tests/test_llm_openai.py` — `skip_filler_comma` 군더더기 미분할 시험과 이어쓰기 요청 본문·`max_tokens` 분할 단위 시험을 추가한다.
4. `bench/first_audio_bench.py` — `--prompt current|fixed`, `--chunker current|fixed`, `--compare-prompt-chunker`(1a 교차), `--compare-yield`(1b: LLM 유휴·동시·`after_chunk1_tts`·`after_first_frame` 교차) 인자와 군더더기 단독 첫 청크 수·첫 청크 길이·`gap_ms` 집계를 추가한다.

### 2.2 2단계 (1단계 합격 시에만 파이프라인·웹 통합)

1. `src/voice_pipeline/pipeline.py` — `llm_yield` 옵션을 추가하고 1단계에서 확정한 시점에 2차 LLM 스트림을 이어 호출한다 ((1a) 합격 시 프롬프트·청커 기본값도 이때 승격한다).
2. `src/voice_pipeline/web/engines.py`, `src/voice_pipeline/web/__main__.py` — `Engines`·`build_engines()`에 `llm_yield`를 연결하고 `--llm-yield` / `--no-llm-yield` 실행 인자를 추가한다.
3. `tests/test_pipeline.py`, `tests/test_speculative.py` — 2단계 이어쓰기, `barge_in()` 취소, 대화 기록 합치기, 선행 생성(`PendingTurn`) 중 2차 요청 취소·확정 단위 시험을 추가한다.

---

## 3. 검증 방법

> ⚠️ 경고: 측정 전에 `free -g`로 가용 메모리를 확인하고 `healthup-llm`(8000)과 `healthup-cosyvoice`(8091, `CV_HOP=10`)가 켜져 있는지 확인한다.

1. 1단계 파일 4개를 수정한 뒤 `.venv/bin/python -m pytest`를 실행해 기존 243개 시험과 신규 1단계 단위 시험 통과를 확인한다.
2. `curl`로 vLLM에 `enable_thinking: False`와 `continue_final_message: True`·`add_generation_prompt: False` 요청 1건을 보내고, 1차 연결 종료 직후 `curl -s http://127.0.0.1:8000/metrics | grep num_requests_running`이 `0`인지 확인한다.
3. **(1a) 프롬프트·청커 단독 교차 측정**: `.venv/bin/python bench/first_audio_bench.py --first-min 2 -n 20 --compare-prompt-chunker`로 `current`(`--prompt current --chunker current`)와 `fixed`(`--prompt fixed --chunker fixed`)를 ABAB 교차 실행해 군더더기 단독 첫 청크 수와 첫 청크 P50을 비교한다.
4. **(1b) 1단계 양보 교차 측정**: `.venv/bin/python bench/first_audio_bench.py --first-min 2 -n 20 --compare-yield`로 같은 문장 세트의 LLM 유휴·동시·양보(`after_chunk1_tts`·`after_first_frame`) TTFA와 `gap_ms`를 ABAB 교차 측정한다.
5. (1b) 합격 기준을 통과하면 2단계 파일을 구현하고 `.venv/bin/python -m pytest`로 전체 단위 시험(`test_pipeline.py`, `test_speculative.py` 포함) 통과를 확인한다.
6. **(2단계) 브라우저 e2e 교차 측정**: 데모 서버를 `--no-llm-yield`와 `--llm-yield`로 각각 `127.0.0.1:8080`에 번갈아 띄우고, `CHROME_PATH=<경로> <playwright-venv>/bin/python bench/web_e2e.py <4문장_16k.wav> 4`를 4회씩(n=16턴) 실행해 같은 날 두 모드의 본문 첫 오디오 P50과 청크 사이 끊김(`gap_ms > 0`) 턴 수를 비교한다.
7. 측정 수치·장비·조건을 `Claude/docs/STATE.md` 세션 로그, `docs/PLAN.md` §2 표(8번 행), `docs/STATE.md` 모듈 연혁에 기록한다.

---

## 4. 위험

1. **선행 생성(`PendingTurn`) 상호작용**: 확정 전에는 프레임이 `_Outbox`에 쌓여 재생되지 않으므로, 2차 요청 계기는 브라우저 재생이 아니라 **TTS 어댑터의 1청크 프레임 생성**이어야 하며 `abort()`·`barge_in()` 시 1차·2차 요청을 모두 취소해야 한다.
2. **이어쓰기 경계 (다중 토큰 예측 (MTP) 잔여 글자·공백)**: 1차 스트림에서 첫 문장 끝 뒤로 딸려 온 토큰 글자를 버릴지 접두사에 포함할지 고정하지 않으면 중복·누락이나 공백 깨짐이 생긴다 (1차 첫 청크 원문까지만 접두사로 쓰고 잔여 버퍼는 버리는 방식을 기본으로 검증한다).
3. **대화 기록(`history`)·`hint`·접두사 캐시 (prefix cache)**: `pipeline.history`에는 1차+2차를 합친 한 응답으로 남기고 끼어들기 시 들린 부분만 남기는 기존 규칙을 지켜야 하며, 2차 요청에도 동일한 시스템 프롬프트·`hint`를 넣어야 접두사 캐시가 유지된다.
4. **채팅 템플릿 호환성**: Qwen3.8 템플릿의 생각 모드 끔(`enable_thinking: False`)과 `continue_final_message`·`add_generation_prompt=False` 조합이 특수 토큰 오류 없이 이어지는지는 **확인하지 않았다**.
5. **vLLM 서버 중단 지연**: 클라이언트가 HTTP 연결을 닫아도 vLLM이 진행 중인 스텝을 바로 비우지 않으면 TTS와 경합이 남는다 (**확인하지 않았다**, `/metrics`로 확인).
6. **2차 TTFT 및 청크 사이 끊김**: `after_chunk1_tts`는 2차 프리필(TTFT P50 224ms, 캐시 미적용 시 더 김) + 2청크 생성 + TTS 첫 소리가 1청크 오디오 길이(`D1`) 안에 끝나야 하므로, 첫 청크가 7~14자로 짧으면 `gap_ms > 0` 끊김이 생길 수 있다 (**확인하지 않았다**, 이 경우 `after_first_frame`을 대안으로 비교).
7. **운영 PC(RTX 4090 ×2) 재측정**: 개발 장비(단일 GB10) 수치는 최종 수치가 아니며(`CLAUDE.md` §4), GPU 2장 분리 환경에서는 양보가 불필요하거나 2차 TTFT 손해만 날 수 있으므로 5주차 운영 PC에서 켬/끔을 다시 측정한다.
