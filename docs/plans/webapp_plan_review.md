# webapp_plan 검토 — 2026-10-30 웹 애플리케이션 완성 계획

> **판정: ⚠️ 수정필요 (조건부 승인 — A1·A2는 아래 §1만 지켜 바로 진행 가능, B1~E1 착수 전 §2~§4 반영 필수)**
> - **핵심 결론**: 8일 안에 화면 2모드·개발 로그·음성 명령·세션 흐름을 끝내는 구조와 가지치기 순서(F1~F3 → C4 → B1 일부)는 타당하다. 그러나 사용자 결정 2개(담당 에이전트·작업별 커밋) 반영과 함께, **사전 조건(미커밋 파일 혼재)**, **기존 시험(`test_web_demo.py`, `web_e2e.py`)과의 하위 호환성**, **`LEGAL_REVIEW.md` P1-3 명칭 충돌("상담 도우미")**을 고쳐야 한다.
> - 검토 대상: `docs/plans/webapp_plan.md`, `src/voice_pipeline/web/` 전체, `src/voice_pipeline/pipeline.py`, `tests/test_web_demo.py`, `docs/LEGAL_REVIEW.md`, `docs/PLAN.md`, `docs/STATE.md`
> - 코드는 고치지 않았다. 현재 워킹 트리 기준 `pytest` 수집 시험 수는 **251개**(`db725dd` 커밋분 248개 + 미커밋 1b 3개)다.

---

## 1. 🚨 지금 진행 중인 A1에 즉시 반영할 점 (맨 앞 요약)

`code` 창(`agy Gemini Flash`)이 지금 A1(`web/session.py`, `web/__main__.py`, `web/engines.py`, `tests/test_web_modes.py`)을 구현 중이다. A1 완료·커밋 전에 아래 **4가지**를 반드시 확인한다.

1. **`config.mode` 수신 시 같은 값이어도 `mode_ready`를 항상 보낸다**:
   - 현재 `session.py` 266행의 `endpoint` 처리는 `ep != self.endpoint_mode`일 때만 `endpoint_ready`를 보낸다.
   - 그러나 `mode`는 A2에서 브라우저가 연결 직후(`ws.onopen`) `localStorage` 값을 `{"type": "config", "mode": ...}`로 보낼 때와 C3 음성 전환 때 UI·시험이 `mode_ready`를 기다린다.
   - 따라서 `msg.get("mode") in ("demo", "dev")`이면 **현재 모드와 같더라도 항상 `{"type": "mode_ready", "mode": self.mode}`를 응답**하고, `("demo", "dev")`가 아닌 잘못된 값(`"invalid"`, `None`, `123` 등)일 때만 무시(응답 없음, 기존 모드 유지)하도록 구현한다.
2. **`__main__.py` → `build_engines()` → `Engines` → `DemoSession` 인자 전달 누락 방지**:
   - `web/__main__.py`에 `--default-mode`(`choices=["demo", "dev"], default="demo"`)를 추가하고, 58행 `build_engines(..., default_mode=a.default_mode)` 호출과 64행 시작 출력 로그에 `default_mode`를 연결한다.
   - `Engines` 데이터클래스에 `default_mode: str = "demo"` 필드를 두고, 잘못된 문자열이 들어오면 `"demo"`로 보정한다.
3. **A1에서는 기존 대화 시작 동작과 판단 로그 동작을 바꾸지 않는다**:
   - A1에서 `self.mode` 기본값이 `"demo"`가 되더라도, **B2(데모 모드 판단 로그 차단)나 C1(`대기` 상태 시작·AI 고지)**을 A1에서 미리 넣으면 기존 `tests/test_web_demo.py`(380행·476행 `tmp_path` JSONL 검증 등 14개 시험)가 깨진다. A1은 순수하게 모드 상태 필드(`hello.mode`, `config.mode`, `mode_ready`, `--default-mode`)만 다룬다.
4. **A1 커밋 시 미커밋 파일 혼입 절대 주의 (`git commit -a` 금지)**:
   - 현재 워킹 트리에 `next_01` 1b 파일 3개(`bench/first_audio_bench.py`, `src/voice_pipeline/llm/openai_stream.py`, `tests/test_llm_openai.py`)와 `next_04` 파일 2개(`services/cosyvoice/server.py`, `bench/tts_ttfa_breakdown.py`)가 미커밋 상태로 섞여 있다.
   - 사용자 결정 (2)에 따라 A1 검증 뒤 커밋할 때는 **반드시 A1 파일 4개(`src/voice_pipeline/web/__main__.py`, `src/voice_pipeline/web/engines.py`, `src/voice_pipeline/web/session.py`, `tests/test_web_modes.py`)만 명시적으로 `git add`하여 커밋**한다.

---

## 2. 사용자 결정 2개 반영 (계획서 필수 수정)

| # | 사용자 결정 | `webapp_plan.md` 수정 위치 및 내용 |
|---|---|---|
| **결정 1** | 구현 담당은 Grok 4.7이 아니라 **`agy Gemini Flash (code 창)`**다 | - 머리말(4행): `Grok 4.7에 맡긴다` → `agy Gemini Flash(code 창)에 맡긴다`<br>- §0 표(25행): `구현 담당 \| Grok 4.7` → `구현 담당 \| agy Gemini Flash (code 창)`<br>- §3.4-6(156행): `규칙은 Grok 4.7이 쓰고, 텍스트 평가 세트는 주 에이전트가 따로 쓴다` → `규칙은 code 창(agy Gemini Flash)이 쓰고, 텍스트 평가 세트는 설계·검토 창이 따로 쓴다`<br>- §5(272행 및 278~289행 표 `담당` 열 12곳): `Grok 4.7` → `agy Gemini Flash (code)`<br>- 부록 제목(370행): `Grok 4.7 에이전트 프롬프트 공통 계약` → `agy Gemini Flash (code 창) 작업 공통 계약` |
| **결정 2** | 커밋은 사용자 요청 때만이 아니라 **작업 항목마다 검증 뒤 한다** | - §6(314행): `커밋은 사용자가 요청할 때만 한다 (CLAUDE.md §8)` → `작업 항목(A1~E1)마다 검증(pytest·벤치·화면 검사)을 통과하면 즉시 해당 작업의 대상 파일만 묶어 커밋한다 (이 계획 한정 CLAUDE.md §8 예외).`<br>- 부록 7번(380행): `커밋하지 않는다. diff만 남긴다` → `각 작업 항목 완료 후 전체 pytest와 해당 항목 검증이 통과하면, 그 작업에서 고친 파일만 명시적으로 git add하여 커밋한다 (git commit -a나 git add . 금지).` |

---

## 3. 6개 항목별 판정 및 상세 검토

### 3-1. 사전 조건 (미커밋 변경 정리 및 기준 시험 수) — ❌ 미충족 (즉시 정리 필요)

1. **워킹 트리 미커밋 변경 혼재**:
   - 계획서 §0 사전 조건 1(29행)은 "10/23 작업 시작 전 미커밋 변경 정리"를 요구했으나, 현재 워킹 트리에 ① `next_01` 1b 실험 코드(`openai_stream.py`, `test_llm_openai.py`, `first_audio_bench.py`, `generate_listen_samples.py`), ② `next_04` 측정 코드(`services/cosyvoice/server.py`, `bench/tts_ttfa_breakdown.py`), ③ 미추적 계획 문서 6개가 섞여 있다.
   - 작업 항목마다 자동 커밋하는 체제(사용자 결정 2)에서 워킹 트리가 더러우면 실수로 다른 작업 파일이 커밋에 섞이거나 `pytest` 실패 원인이 불분명해진다.
   - **조치**: A1 커밋 직후(또는 직전)에 `next_03_review.md` §4-C 절차대로 `next_01` 1b 실험·문서 묶음을 별도 커밋하고, `next_04` 작업분(`services/cosyvoice/server.py`, `bench/tts_ttfa_breakdown.py`)은 `next_04` 측정 완료 후 커밋하거나 `git stash push -u -m "wip: next_04"`로 분리해 워킹 트리를 깨끗하게 만든다.
2. **기준 시험 수 확정 (§0 사전 조건 2)**:
   - `.venv/bin/python -m pytest --collect-only -q` 실측 결과: 현재 워킹 트리 기준 **251개**(`HEAD`인 `db725dd` 단독 기준 **248개** + 미커밋 `tests/test_llm_openai.py`의 1b 시험 **3개**).
   - 1b 실험 코드를 커밋하면 기준선은 **251개**(A1의 신규 3개 추가 시 **254개**)다.

### 3-2. 일정 현실성 (10/23~10/30, 8일) — ⚠️ 조건부 현실적 (컷오프 규칙 명시 필요)

1. **주말 포함 8일 강행군 및 병목 날짜 2곳**:
   - 토·일(10/24~25)을 모두 작업일로 써야 하며, 특히 **10/27(화)**(`C4 대기 청취` + `D1 120문장 녹음 🎙️` + `F1 PC·태블릿 표정 모델 3종 벤치 📷📱`)과 **10/29(목)**(`D2 지연 회귀 ABAB 32회` + `D3 태블릿 실기 📱` + `D4 청취 🎧` + `F3 표정 시험 📷🎧`)에 사용자 참여 작업과 에이전트 구현 작업이 과밀하게 몰려 있다.
2. **가지치기(Cut-off) 발동 시점 명시 필요**:
   - §6(312행)의 제외 우선순위(① `F1~F3 얼굴 표정` → ② `C4 대기 청취` → ③ `B1 덕킹 지연·요약 줄`)는 합리적이다.
   - 다만 언제 포기할지 기준 시각이 없다. **"10/26(월) 20:00까지 C3(의도 연결)가 커밋되지 않거나, 10/27(화) 오전까지 태블릿(📱)이 준비되지 않으면 F1~F3은 즉시 §10 후속 과제로 넘긴다"**를 §6에 명시한다.

### 3-3. 작업 순서 및 의존성 — ⚠️ 수정필요 (3가지)

1. **단일 구현 창(`code`) 기준 순차 진행으로 표기 수정**:
   - §6 표(302·304·306행)는 A1·A2, C1·C2, C4·F1을 "병렬 가능"으로 적었으나, 구현을 `code` 창 하나가 맡고 작업마다 `pytest` 후 커밋하므로 실제 코드 구현은 **`A1 커밋 → A2 커밋`**, **`C1 커밋 → C2 커밋`** 순차 진행이다.
   - 진짜 병렬로 돌릴 수 있는 것은 **C1 구현 중 설계·검토 창이 `bench/intent_set_ko.tsv`(C2 평가 세트)를 작성하는 작업**과 **C4 구현 중 사용자가 D1 녹음을 진행하는 작업**뿐이다.
2. **B2 (`endpoint/turn.py` 로그 게이트)와 기존 시험의 순서 충돌 방지**:
   - `tests/test_web_demo.py`의 `test_smart_mode_end_to_end`(400행)와 `test_reopen_after_confirm_before_tts_audio`(500행)는 `Engines(..., endpoint_log_dir=tmp_path)`를 만들어 `tmp_path`에 판단 로그(`*.jsonl`)가 생기는지 검사한다.
   - B2에서 "데모 모드 → 판단 로그 0줄"을 구현할 때, `TurnEndpointer`에 `enabled: Callable[[], bool]` 콜백을 넘기되 **기존 시험 픽스처 2곳(`_smart_client`, `_reopen_client`)에 `default_mode="dev"`를 지정**하거나, `endpoint_log_dir`가 명시적으로 주어진 시험에서는 허용하고 기본 경로(`DEFAULT_LOG_DIR`)만 `mode == "dev"`로 막는 규칙을 정해야 기존 시험이 깨지지 않는다.
3. **C1 (`web/lifecycle.py` 상태 기계)과 기존 시험·벤치의 하위 호환성**:
   - 현재 `DemoSession`은 접속 즉시 대화가 가능한(`진행`) 상태이며, `tests/test_web_demo.py` 전체와 `bench/web_e2e.py`(D2 지연 벤치)는 `"시작하자"` 음성 명령이나 시작 고지 재생 없이 첫 발화/텍스트 입력이 곧바로 LLM 응답으로 가는 것을 전제로 한다.
   - 만약 C1에서 모든 세션을 무조건 `대기(idle)` 상태로 시작하게 바꾸면 기존 웹 시험 14개와 `web_e2e.py`가 전부 실패한다.
   - **해결**: 세션이 `대기(idle)` 상태에서 `SESSION_START`를 기다리는 것은 **C4의 `--wake`(또는 `Engines.lifecycle=True`)가 켜졌을 때** 또는 브라우저가 명시적으로 대기 시작을 요청했을 때로 한정하고, 기본 `Engines(...)` 단위 시험과 `text_input` 경로에서는 기존처럼 즉시 턴 처리가 동작하도록 설계해야 한다.

### 3-4. 통과 기준의 측정 가능성 — ⚠️ 5개 항목 구체화 필요

| 작업 | 현재 통과 기준 (`webapp_plan.md`) | 문제점 (측정 불가·모호성) | 수정안 (측정 가능하게 구체화) |
|---|---|---|---|
| **B1** | `dev_event`에 `덕킹 지연(서버 기준)` 표시 | 서버는 `vad.feed()`에서 `SpeechStart`가 나오는 같은 이벤트 루프 틱에 `send({"type": "duck", "on": True})`를 부르므로 서버 내부 시간차는 항상 `0~1ms`여서 지표로서 의미가 없다 | **"VAD `SpeechStart` 감지 시각 − 해당 발화 첫 오디오 프레임 수신 시각(또는 `duck` 전송~브라우저 `duck_ack` 왕복 ms)"** 중 하나로 수식을 명시한다 |
| **C1** | `PromptBank`: `합성 수 = 문장 수 × 목소리 수. 길이 필터로 버리는 문장 0개` | 실제 `CosyVoiceTTS`에서 안내 문장(고지·시작·종료·확인 4종·재요청 등 최소 8문장) × 목소리 4종 = **32회 합성**을 서버 시작(`warmup`) 때마다 돌리면 기동에 **약 18~25초**가 추가된다 | 1) 기본 목소리(`tts.voice`, 1종)만 기동 시 선합성하고 다른 목소리는 선택 시 합성(또는 `models/prompt_cache/` 디스크 캐시 사용)으로 바꾼다.<br>2) `MockTTS` 단위 시험에서는 호출 횟수(`len(tts.calls) == 문장 수 × 대상 목소리 수`)로 검증한다 |
| **C3** | `mock에서 정지 명령 확정 → interrupt 전송 ≤ 50ms` | 말하는 중 짧은 발화("멈춰")는 `_probe_classify`에서 STT가 끝난 뒤에야 명령으로 판정된다. 기준 시각이 발화 끝인지 STT 완료인지 적히지 않았다 | 기준 시각을 **"말하는 중 `_probe_classify`의 `stt.transcribe()` 반환 시각부터 `sink.interrupt()` 호출 완료까지 ≤ 50ms"**로 명시한다 |
| **D2** | `bench/web_e2e.py`로 새 기능 켬(개발 모드·의도 분류) vs 끔(`--no-intent`) ABAB 교차 | 1) 기본 모드가 `demo`면 데모 모드에서 `#start` 버튼이나 `#lat` 표가 숨겨져 `web_e2e.py`의 `page.click("#start")`·`#lat tr` 파싱이 실패할 수 있다.<br>2) 시작 직후 AI 고지 음성이 재생되면 `web_e2e.py`가 흘려보내는 1턴째 사용자 음성과 겹쳐 barge-in이 발생한다 | 1) D2 실행 시 서버에 `--default-mode dev`를 주거나 데모 모드에서도 `#lat` DOM은 `hidden` 속성으로만 숨겨 텍스트 파싱이 되게 한다.<br>2) `web_e2e.py` 실행 시에는 시작 고지 완료를 기다리거나 `--no-greeting`(또는 기존 `#start` 동작 유지)으로 4문장 지연을 공정 비교한다 |
| **F2** | `bench/web_ui_check.py` (가짜 카메라 `--use-fake-device-for-media-stream`) | Chromium 기본 가짜 카메라는 회전하는 초록색 원 화면이라 얼굴이 검출되지 않는다(`face_summary` 미생성) | 얼굴이 찍힌 테스트용 `.y4m` 영상(`--use-file-for-fake-video-capture=...`)을 쓰거나, `app.js`에 시험용 주입 함수(`window.__injectFaceFrame` / `window.__mockFaceSummary`)를 두어 파이프라인 연결을 검증한다 |

### 3-5. 0절 법적 충돌 처리 (`LEGAL_REVIEW.md` 대조) — ⚠️ 보완 필요 (4가지)

1. **[신규 발견] `LEGAL_REVIEW.md` P1-3 명칭 충돌 ("AI 상담 도우미" → "AI 마음 건강 도우미")**:
   - 계획서 §2.2(65행), §5 C1(282행), §8-4(342행)에서 화면 배지와 음성 고지 문구를 **"AI 상담 도우미"**로 적었다.
   - 그러나 [LEGAL_REVIEW.md](file:///home/maisc05/Desktop/healthup/healthup-voice-pipeline/docs/LEGAL_REVIEW.md#L38) P1-3 및 §5(120행)는 **"정신건강전문요원·임상심리사·상담사" 명칭을 쓰지 않는다 (심리상담사법 계류 및 자격 명칭 오인 방지)**고 정했고, 현재 LLM 프롬프트([openai_stream.py](file:///home/maisc05/Desktop/healthup/healthup-voice-pipeline/src/voice_pipeline/llm/openai_stream.py#L20))도 **"마음 건강 도우미"**(및 "인공지능 도우미")로 통일되어 있다.
   - **수정**: 화면 예외 1번 표시와 C1의 AI 고지 음성 문구에서 "상담"을 빼고 **"AI 마음 건강 도우미"**(또는 "인공지능 마음 건강 도우미")로 통일한다.
2. **대기 청취(`--wake`) 시 화면 상시 표시 추가 (통신비밀보호법 P0-5 완화)**:
   - §0(21행)과 §3.5(164행)에서 `--wake` 옵트인 분리 및 대기 중 비명령 전사 즉시 폐기·미기록 처리는 적절하다.
   - 다만 대기 중에 마이크가 켜져 있으므로, **데모 모드 자리표시 영역(대기 상태)에 `"마이크 대기 중 ('시작하자'라고 말씀해 주세요)"` 표시**를 반드시 띄워 주변 사람이 청취 상태를 알 수 있게 해야 한다.
3. **개발 로그(`--dev-log-days`) 및 판단 로그 차단 (개인정보보호법 P0-6)**:
   - §0(22행)과 §2.4(96~98행)에서 기본값 `demo`에서는 개발 로그와 `endpoint/turn.py` 판단 로그를 모두 쓰지 않고, `dev` 모드에서만 보유기간(`--dev-log-days`, 기본 7일) 자동 파기를 둔 것은 `LEGAL_REVIEW.md` P0-6을 정확히 해결한다.
4. **카메라 동의 방식 보완 (`LEGAL_REVIEW.md` §3 시행령 §17 및 §4.2)**:
   - §4.4-4(220행)는 카메라 동의를 음성 `"네"`로만 받도록 했으나, `LEGAL_REVIEW.md` 79행은 **"음성 안내 + 큰 글씨 + 버튼 확인"**을 권고한다.
   - 스피커 에코나 주변 소리로 `"네"`가 오인식되어 동의 없이 카메라가 켜지는 것을 막기 위해, 동의 오버레이에 **큰 `[동의하고 카메라 켜기]` / `[끄고 계속하기]` 터치 버튼을 함께 두고**, 안내 문구에 거부권("동의하지 않아도 대화할 수 있습니다")을 포함한다.

### 3-6. 누락 항목 — 6가지 보완 필요

1. **데모 모드에서 첫 화면 터치 UX (A2·C1·C4 연결)**:
   - 데모 모드에서는 기존 헤더의 `#start` 버튼을 숨기는데(§2.2 69행), 브라우저 오디오·마이크 권한을 열기 위한 "첫 화면 터치 1회"(§1-4, §3.5)를 어디서 받는지가 빠져 있다.
   - **보완**: 데모 모드에서는 **자리표시 영역(화면 전체 카드) 자체가 첫 터치 버튼 역할**을 하여, 최초 1회 터치 시 `AudioContext.resume()` + `getUserMedia({audio})` + WS 연결을 수행하고(`--wake`가 꺼져 있으면 즉시 세션 시작, 켜져 있으면 대기 청취 진입), 그 뒤로는 음성 명령으로만 조작하게 명세한다.
2. **`REPEAT_BOT`("다시 말해 줘")의 원문 보존 및 대화 기록 중복 방지 (C3)**:
   - §3.1(121행)은 `pipeline.say(직전 답 전체 문장)`을 부른다고만 적었다.
   - 현재 `VoicePipeline.history`에는 사용자가 끊었을 때 잘린 문장(`spoken_text + " (사용자가 중간에 끊음)"`)만 남거나, 직전 답 원문 전체를 따로 저장하는 필드가 없다.
   - **보완**: 세션에 `self._last_bot_text: str`(직전 턴 LLM 생성 원문 전체 또는 완료된 답)를 따로 보관하고, `REPEAT_BOT` 발화("다시 말해 줘") 자체는 `pipeline.history`에 user 메시지로 들어가지 않게 한다.
3. **`REDO_USER`("다시 말할게요", "방금 거 취소") 실행 시 음성 안내 피드백 (C1·C3)**:
   - 기록(`pipeline.history`)에서 직전 `(user, assistant)`만 지우고 아무 소리를 내지 않으면 사용자는 취소 성공 여부를 알 수 없다.
   - **보완**: `PromptBank`(C1)에 취소 완료 안내("네, 방금 말씀은 지웠어요. 다시 말씀해 주세요.")와 취소할 기록이 없을 때의 안내("아직 주고받은 대화가 없어요. 편하게 말씀해 주세요.")를 추가한다.
4. **확인 대기 상태에서 확인 질문의 종류 구분 (C1·C3)**:
   - `RESET`("처음부터 다시 할까요?"), `SESSION_END`("대화를 마칠까요?"), `MODE_DEV`/`MODE_DEMO`("개발 모드로 바꿀까요?"), 카메라 동의("카메라를 켜도 될까요?") 모두确认 질문 문구가 다르다. `PromptBank`의 키 목록(최소 8~10개)을 C1 작업 명세에 확정해 적어야 한다.
5. **1a(`PROMPT_NO_FILLER`, `skip_filler_comma=True`) 및 `next_04` 반영 시점 누락**:
   - 이미 합격·커밋된 `db725dd`(1a 프롬프트·청커 옵션)를 웹 데모(`Engines` / `DemoSession`) 기본값으로 켜는 작업과 `next_04`(CosyVoice 첫 청크 TTFA 단축)가 `webapp_plan.md`에 빠져 있다. D2(10/29 지연 회귀) 전에 1a 기본값 승격 여부를 결정해 반영해야 한다.
6. **MediaPipe 로컬 서빙 파일 목록 (F1)**:
   - `@mediapipe/tasks-vision`을 외부 CDN 없이(`OBJECTIVE.md` §3-1) 로컬에서 돌리려면 JS 번들뿐 아니라 WASM 로더 파일(`vision_wasm_internal.js`, `vision_wasm_internal.wasm`)과 모델 파일(`face_landmarker.task`)을 `models/mediapipe/` 또는 `web/static/vendor/`에 두고 경로를 명시적으로 지정해야 한다.

---

## 4. 작업별(A1~E1) 검토 요약표

| 작업 | 판정 | 반영할 핵심 수정 |
|---|---|---|
| **A1** (10/23) | ✅ 진행 중 | §1의 4가지 반영(`config.mode` 동일값에도 `mode_ready` 응답, `build_engines` 연결, 기존 시험 불변, A1 파일 4개만 커밋) |
| **A2** (10/23) | ⚠️ 보완 후 진행 | 예외 1번 문구를 `"AI 마음 건강 도우미"`로 수정. 데모 모드에서 자리표시 영역 터치 시 첫 시작(`start()`) 연결. `web_e2e.py` 호환을 위해 `#lat` 등은 DOM에서 삭제하지 않고 `hidden` 처리 |
| **B1** (10/24) | ⚠️ 보완 후 진행 | `덕킹 지연` 계산식을 명시(`SpeechStart` 감지 시각 기준 또는 브라우저 보고 기준) |
| **B2** (10/24) | ⚠️ 보완 후 진행 | `endpoint/turn.py` 로그 게이트 추가 시 `tests/test_web_demo.py`의 `_smart_client`·`_reopen_client`가 깨지지 않도록 처리 |
| **C1** (10/25) | ⚠️ 보완 후 진행 | `PromptBank` 문구 목록(AI 마음 건강 도우미 고지, 시작, 종료, 확인 4종, 취소 완료, 재요청) 확정 및 기본 목소리 1종만 기동 시 합성. 기본 단위 시험·`text_input` 하위 호환성 유지 |
| **C2** (10/25) | ✅ 적합 | `code` 창이 `intent.py`를 짜는 동안 설계·검토 창이 `bench/intent_set_ko.tsv`를 독립 작성 |
| **C3** (10/26) | ⚠️ 보완 후 진행 | `REPEAT_BOT` 원문 보관(`_last_bot_text`) 및 기록 오염 방지, `REDO_USER` 음성 피드백 재생, 정지 지연 50ms 기준점(`_probe_classify` STT 완료 시각) 명시. `--no-intent` 실행 인자 추가(D2 비교용) |
| **C4** (10/27) | ⚠️ 보완 후 진행 | `--wake` 시 화면에 `"마이크 대기 중"` 표시 추가 |
| **D1·D1b** (10/27~28) | ✅ 적합 | 사용자 녹음(🎙️) 일정 확보 필수 |
| **F1~F3** (10/27~29) | ⚠️ 선택 (컷오프 적용) | 10/26 저녁 C3 미완 또는 10/27 태블릿 미확보 시 즉시 제외. 진행 시 로컬 WASM 경로 고정, 동의 터치 버튼 병행, F2 가짜 영상/`.y4m` 또는 모의 주입 훅 추가 |
| **D2~D4, E1** (10/29~30) | ⚠️ 보완 후 진행 | D2는 `--default-mode dev`에서 `--no-intent` 유무로 ABAB 비교 |

---

## 5. 사용자 결정(10/23 02:42) 반영 및 Flash(`code` 창) 다음 작업 순서

### 5-1. 확정된 사용자 결정 요약 (`webapp_plan.md` 반영 완료)

1. **결정 1 (`wake` 대기 청취)**: **추천안 (a) 확정** — 10/30 연구실 데모에서 `--wake`로 켜고, 공개 설치 전 법률 검토를 받는다.
2. **결정 2 (`face` 얼굴 감정)**: **추천안 (a) 확정** — 이번 주에 개발용 실험 기능으로 만들고(기본값 `false`), 동의한 경우에만 켠다.
3. **결정 3 (`--dev-log-days` 보유기간)**: **추천안 (a) 확정** — 개발 로그는 **7일** 보관 후 자동 파기한다.
4. **결정 4 (데모 모드 예외 3개 표시)**: **추천안 (a) 확정** — 데모 모드에 AI 고지(`"AI 마음 건강 도우미"`), 카메라 표시, 연결 상태를 기본으로 보인다.
5. **추가 요구 (데모 모드 개별 기능 토글 + AI 고지 해제 경고)**: **확정**
   - 데모 모드에서도 접이식 설정 패널(`⚙️ 설정`)과 자연어 음성 명령으로 6개 항목을 **하나씩 개별 on/off** 할 수 있게 한다 (기본값은 추천안: `wake=true(--wake 시)`, `face=false`, `show_ai_notice=true`, `show_camera_badge=true`, `show_conn_status=true`, `show_dev_info=false`).
   - 특히 `show_dev_info`를 켜면 데모 모드에서도 대화·턴 판단·지연 표(`#lat`)를 함께 볼 수 있다(단, 파일 로그는 `mode == "dev"`에서만 기록).
   - **`show_ai_notice`(AI 고지)는 법적 필수(AI 기본법 §31)이므로 끌 때 경고창(음성 명령 시 경고 안내 + 확인 대기)을 띄워 확인했을 때만 숨긴다.**

### 5-2. `agy Gemini Flash (code 창)`에 줄 다음 작업 순서

아래 순서대로 한 단계씩 구현 → 검증(`pytest` / 벤치) → 해당 파일만 단독 커밋한다.

#### Step 0. A1 검증·커밋 및 워킹 트리 정리 (즉시)
- 현재 `code` 창이 구현한 A1(`src/voice_pipeline/web/__main__.py`, `src/voice_pipeline/web/engines.py`, `src/voice_pipeline/web/session.py`, `tests/test_web_modes.py`)은 §1의 요구사항 4가지를 모두 충족했다.
- 실행 명령:
  1. `.venv/bin/python -m pytest tests/test_web_modes.py` 및 `.venv/bin/python -m pytest` 전체 통과 확인
  2. **A1 단독 커밋**:
     ```bash
     git add src/voice_pipeline/web/__main__.py src/voice_pipeline/web/engines.py src/voice_pipeline/web/session.py tests/test_web_modes.py
     git commit -m "webapp A1: 서버 모드 상태(demo/dev) 및 --default-mode 추가"
     ```
  3. **잔여 미커밋 파일 분리**: `next_03_review.md` §4-C에 따라 `rm -f docs/plans/next_04_draft.md` 후 `next_01` 1b 파일 3개 + 계획/검토 문서들을 별도 커밋하고, `next_04` 진행분(`services/cosyvoice/server.py`, `bench/tts_ttfa_breakdown.py`)은 `git stash push -u -m "wip: next_04"`(또는 `next_04` 측정 후 별도 커밋)로 분리해 워킹 트리를 깨끗이 비운다.

#### Step 1. 작업 A2 — 브라우저 화면 2모드 + 개별 기능 설정 패널(`⚙️`) + AI 고지 경고 (10/23)
- **수정·생성 파일**: `src/voice_pipeline/web/static/index.html`, `src/voice_pipeline/web/static/app.js`, 신규 `bench/web_ui_check.py`
- **구현 핵심**:
  1. 데모 모드 자리표시 영역(전체 화면 아바타 상태 카드 + 말할 때 재생 음량 막대 + 최초 1회 터치 시 `start()` 호출)과 상단/모서리 예외 표시 3개(`"AI 마음 건강 도우미"` 고지 배지, 카메라/대기마이크 배지, 연결 상태·RTT)를 만든다.
  2. 구석에 접이식 `[⚙️ 설정]` 패널을 만들어 모드(`demo`/`dev`)와 개별 기능 6종(`wake`, `face`, `show_ai_notice`, `show_camera_badge`, `show_conn_status`, `show_dev_info`) 체크박스를 둔다 (기본값은 `webapp_plan.md` §2.2 표).
  3. `show_ai_notice` 체크를 해제하려 할 때 법적 의무 경고(`window.confirm` 또는 커스텀 경고 모달: *"AI 기본법 제31조에 따른 법적 의무 고지입니다. 내부 시험 목적으로만 숨기시겠습니까?"*)를 띄워 취소 시 체크를 유지하고 확인 시에만 숨긴다.
  4. 데모 모드에서 `show_dev_info`가 꺼져 있을 때 기존 개발 카드들(`#log`, `#lat`, `#epCard` 등)은 DOM에서 지우지 않고 `hidden` 속성으로 숨긴다.
  5. 모드와 6개 개별 토글을 `localStorage`에 저장하고, 재연결(`ws.onopen`) 및 토글 변경 시 `{"type": "config", "mode": ...}`를 전송한다.
- **검증 및 커밋**: `bench/web_ui_check.py`(Playwright, 6개 항목 검사) + `.venv/bin/python -m pytest` 통과 후 `index.html`, `app.js`, `bench/web_ui_check.py`만 `git add`하여 커밋한다.

#### Step 2. 작업 B1 → B2 (10/24)
- **B1 (`web/session.py`, `app.js`, `index.html`)**:
  - `dev_event` 메시지(barge-in 판별 결과, 끊긴 `turn_id`와 `played_ms`, 덕킹 지연 ms, 의도 판정, 적용된 `hint_rule`), 지연 표 상단 P50·P90 요약 줄, LLM 응답의 `spoken_text`(들린 부분) 표시를 추가한다.
  - `bench/web_ui_check.py` 및 `tests/test_web_demo.py` 확장 통과 후 **B1 단독 커밋**.
- **B2 (신규 `web/devlog.py`, `web/session.py`, `endpoint/turn.py`, `.gitignore`, 신규 `tests/test_devlog.py`)**:
  - `logs/dev/YYYYMMDD/<session_id>.jsonl` 기록(`mode == "dev"`일 때만), `--dev-log-days 7`(기본값 7일) 자동 삭제, `endpoint/turn.py` 판단 로그의 데모 모드 차단(단, 단위 시험에서 `endpoint_log_dir`을 명시 주입한 경우는 허용), `.gitignore`에 `logs/` 추가.
  - `tests/test_devlog.py` 및 전체 `pytest` 통과 후 **B2 단독 커밋**.

#### Step 3. 작업 C1 → C2 (10/25)
- **C1 (신규 `web/lifecycle.py`, 신규 `prompts.py`, `web/session.py`, `web/engines.py`, 신규 `tests/test_lifecycle.py`)**:
  - 세션 상태 기계(`idle` / `active` / `confirming` / `ending`)와 `PromptBank`(`"AI 마음 건강 도우미"` 고지, 시작·종료 인사, 확인 질문 5종[종료·초기화·모드전환·카메라동의·AI고지숨김], 직전 턴 취소 완료 안내, 재요청) 구현.
  - 기동 시간 단축을 위해 `warmup` 시 기본 목소리 1종만 선합성하고, 기존 `test_web_demo.py`·`text_input` 하위 호환성을 유지한다.
  - `tests/test_lifecycle.py` 및 전체 `pytest` 통과 후 **C1 단독 커밋**.
- **C2 (신규 `src/voice_pipeline/intent.py`, 신규 `bench/intent_eval.py`, 신규 `tests/test_intent.py`)**:
  - C1 진행 중 설계·검토 창이 `bench/intent_set_ko.tsv`를 작성한다.
  - `code` 창은 기본 명령 9종 + 개별 기능 토글 명령(`CAMERA_ON/OFF`, `WAKE_ON/OFF`, `DEV_INFO_ON/OFF`, `UI_BADGE_TOGGLE`)을 포함한 규칙 분류기 `intent.py`와 `bench/intent_eval.py`를 구현한다.
  - 합격선(재현율 ≥ 95%, 위기 오판 0, 비명령 되돌릴 수 없는 명령 0, P99 ≤ 5ms) 및 `pytest` 통과 후 **C2 단독 커밋**.

#### Step 4. 작업 C3 — 의도 연결 및 개별 기능 음성 제어 (10/26)
- **수정·생성 파일**: `web/session.py`, `web/__main__.py`(`--no-intent` 추가), `app.js`(서버가 보낸 `feature_prefs` 동기화 및 AI 고지 경고 표시), 신규 `tests/test_session_commands.py`
- **구현 핵심**: `_run_turn`·`_on_speculate`·`_probe_classify` 3곳 연결, `_last_bot_text` 원문 보관(`REPEAT_BOT`), `REDO_USER` 취소 안내 음성 재생, 확인 대기 중 `"네"` 맞장구 필터 우회, 음성으로 개별 기능 on/off(AI 고지 숨김·카메라 켜기는 확인 대기 진입).
- **검증 및 커밋**: `tests/test_session_commands.py`(9개 통합 시험) 및 전체 `pytest` 통과 후 **C3 단독 커밋**.

#### Step 5. 작업 C4, F1·F2, D1·D1b (10/27~10/28)
- **C4 (`--wake` 대기 청취 + `config.wake` 개별 토글 연동)**: `tests/test_wake.py` + `bench/web_ui_check.py` 통과 후 **C4 단독 커밋**.
- **F1 (`face-probe.html`, `face-probe.js`, 로컬 MediaPipe 서빙)**: 측정 페이지 구현·커밋 후 사용자와 PC·태블릿 측정(📷📱).
- **D1·D1b**: 사용자 녹음(🎙️) 평가 후 `intent.py` 표기 변형 보강·커밋.
- **F2 (`fused_hint`, 동의 버튼+음성, `config.face` 토글, 바이너리 프레임 무결성 시험)**: `tests/test_hint.py` + `bench/web_ui_check.py` 통과 후 **F2 단독 커밋**.

#### Step 6. 작업 D2~D4, F3, E1 (10/29~10/30)
- D2 지연 회귀(`web_e2e.py`, `--default-mode dev`에서 `--no-intent` 유무 ABAB 비교), D3 태블릿 실기(📱), D4 청취(🎧), F3 표정 시험(📷🎧), E1 리허설 3회 및 문서(`STATE.md`, `README.md`) 갱신 후 최종 커밋.

---

## 6. A2 diff 검토 (2026-10-23, `index.html`·`app.js`·`bench/web_ui_check.py`)

> **판정: ⚠️ 수정필요 (필수 수정 3건 반영 후 즉시 커밋 가능)**
> - 검토 대상: `git diff src/voice_pipeline/web/static/index.html src/voice_pipeline/web/static/app.js` 및 미추적 `bench/web_ui_check.py` (현재 `web_ui_check.py` 6/6, `pytest` 254 passed 기준)
> - 요약: 본 문서 122행의 핵심 요구 3가지(`"AI 마음 건강 도우미"` 문구, `#avatarCard` 터치 시 `start()` 연결, `#lat`·`#log` DOM 유지 및 `hidden` 토글)와 AI 고지 해제 시 법적 경고(`window.confirm`)는 정확히 구현되었다. 다만 `webapp_plan.md` A2 행 및 §2.2 기준에 비추어 **데모 모드에서 WS 연결(`onHello`) 뒤 헤더의 엔진 정보·선택기가 노출되는 문제** 등 아래 **필수 수정 3건**을 반영한 뒤 커밋한다.

### 6-1. 기준별 점검 표 (`webapp_plan.md` A2 행 & `webapp_plan_review.md` 122행)

| 기준 항목 | 현재 구현 상태 | 판정 |
|---|---|---|
| **리뷰 122행 ①**: 예외 1번 문구 `"AI 마음 건강 도우미"` | `index.html:76` `<span id="aiNotice">AI 마음 건강 도우미</span>` 반영 | ✅ 통과 |
| **리뷰 122행 ②**: 자리표시 영역(`#avatarCard`) 터치 시 첫 시작(`start()`) | `app.js:422` `$("avatarCard").onclick = () => { if (!running) start(); }` 및 `check_6` 통과 | ✅ 통과 |
| **리뷰 122행 ③**: `web_e2e.py` 호환(`#lat`·`#log` DOM 유지, `hidden` 제어) | `index.html:13` `[hidden] { display: none !important; }` 및 `#micCard`·`#textCard`·`#logCard`·`#latCard` `hidden` 토글 (`check_1` 65~66행 확인) | ✅ 통과 |
| **계획 A2 ①**: 데모 기본 화면에 자리표시·예외 3개·설정/모드 버튼만 표시 (§2.2 엔진 선택 숨김) | 카드 4개는 숨겨지나, **`start()` 후 `onHello`가 실행되면 헤더의 `#engines`·`#voiceWrap`·`#sttWrap`·`#epWrap`이 데모 모드에서도 그대로 노출됨** | ❌ 수정필요 (필수 1) |
| **계획 A2 ②**: 개발 모드에 기존 패널 전부 표시 | `#modeDev` 선택 시 4개 개발 카드 노출 및 `#avatarCard` 숨김 (`check_2` 통과) | ✅ 통과 |
| **계획 A2 ③·⑤·⑥**: 설정 패널로 6개 기능 개별 on/off, `config` 전송, 재연결 뒤 모드·개별 토글 유지 | UI·핸들러는 구현됐으나, ① `ws.onopen` 재연결 시 `config`에 `wake`·`face`가 빠졌고, ② `web_ui_check.py`에 6개 개별 토글 동작·전송·유지 검사가 아직 없음 (`code` 창 추가 중) | ⚠️ 수정필요 (필수 2) |
| **계획 A2 ④**: AI 고지 해제 시 법적 경고창(취소 시 유지·확인 시 숨김) | `app.js:450` `window.confirm("AI 기본법 제31조...")` 및 `check_5` 통과 | ✅ 통과 |
| **계획 §2.2 예외 2**: `#cameraBadge`(`카메라 켜짐` / `마이크 대기 중`) 상태 연동 | `index.html:78` 텍스트가 `"마이크 대기 중"`으로 고정돼 있고, `prefs.face`를 켜도 `"카메라 켜짐"`이 표시되지 않으며 `wake`·`face`를 모두 꺼도 배지가 안 숨겨짐 | ❌ 수정필요 (필수 3) |

### 6-2. 필수 수정 목록 (3건 — 반영 후 바로 커밋)

1. **[필수 1] 데모 모드(`!showDev`)에서 헤더 엔진 정보·드롭다운 숨김 (`app.js`)**:
   - 원인: `applyVisibility()`(55~83행)가 `#engines`, `#voiceWrap`, `#sttWrap`, `#epWrap`을 제어하지 않고, `onHello(m)`(246~271행)이 `hidden = false`로 덮어쓴다.
   - 수정: `applyVisibility()` 안에 아래를 추가하고, `onHello(m)` 끝에서 `applyVisibility()`를 호출한다.
     ```js
     if ($("engines")) $("engines").hidden = !showDev;
     if ($("voiceWrap")) $("voiceWrap").hidden = !showDev || !$("voice") || $("voice").options.length === 0;
     if ($("sttWrap")) $("sttWrap").hidden = !showDev || !$("stt") || $("stt").options.length === 0;
     if ($("epWrap")) $("epWrap").hidden = !showDev || !$("endpoint") || $("endpoint").options.length === 0;
     ```
   - 검사 추가: `bench/web_ui_check.py`에서 WS 연결(`start()`) 이후 데모 모드일 때 `not await page.is_visible("#engines")` 및 `not await page.is_visible("#sttWrap")`을 확인한다.

2. **[필수 2] 재연결 시 `config`에 `wake`·`face` 포함 및 `web_ui_check.py` ③·⑤·⑥ 개별 토글 검사 완성 (`app.js`, `bench/web_ui_check.py`)**:
   - `app.js` 323행 `ws.onopen`의 `send({ type: "config", ... })`에 `wake: prefs.wake, face: prefs.face`를 추가한다.
   - 지금 `code`가 추가 중인 `bench/web_ui_check.py` 토글 검사에 다음 4가지가 모두 들어 있는지 확인한다:
     1. 데모 모드에서 `#prefDevInfo` 체크 시 `#avatarCard`와 `#logCard`·`#latCard`가 함께 보이는지(③)
     2. `#prefConnStatus`·`#prefCameraBadge` 체크 해제 시 `#connStatus`·`#cameraBadge`가 숨겨지는지(③)
     3. `#prefWake`·`#prefFace` 토글 시 WS로 `{"type": "config", "wake": ...}`, `{"type": "config", "face": ...}`가 전송되는지(⑤)
     4. `page.reload()` 뒤에도 변경된 `healthup_prefs`(개별 토글 값)와 `ws.onopen` 재전송이 유지되는지(⑥)

3. **[필수 3] `#cameraBadge` 문구 및 표시 여부를 `prefs.face`·`prefs.wake` 상태와 연동 (`app.js`)**:
   - 원인: 현재 `if ($("cameraBadge")) $("cameraBadge").hidden = !prefs.show_camera_badge;`만 있어서 `prefs.face`를 켜도 `"카메라 켜짐"`이 나오지 않고, `wake`와 `face`를 모두 꺼도 `"마이크 대기 중"`이 계속 보인다.
   - 수정: `applyVisibility()`에서 아래처럼 문구와 `hidden` 조건을 함께 갱신한다:
     ```js
     if ($("cameraBadge")) {
       const parts = [];
       if (prefs.face) parts.push("카메라 켜짐");
       if (prefs.wake) parts.push("마이크 대기 중");
       $("cameraBadge").textContent = parts.join(" · ") || "마이크 대기 중";
       $("cameraBadge").hidden = !prefs.show_camera_badge || parts.length === 0;
     }
     ```

### 6-3. 권장 수정 목록 (2건 — 함께 고치면 좋은 점)

1. **`#avatarHint` 문구의 `"상담"` → `"대화"` 변경 (`LEGAL_REVIEW.md` P1-3 일관성)**:
   - `index.html:116` 및 `app.js:386`의 `"화면을 터치하여 상담을 시작하세요"` / `"상담 진행 중입니다 (음성으로 대화하세요)"`를 `"화면을 터치하여 대화를 시작하세요"` / `"대화 진행 중입니다 (편하게 말씀하세요)"`로 고친다.
2. **`stopAll()` 및 `ws.onclose()`에서 `#avatarStatusText` 갱신 (`app.js`)**:
   - `stopAll()`(401행)과 `ws.onclose()`(334~335행)는 `$("badge")`만 `"중지됨"` / `"연결 끊김"` / `"재연결 중"`으로 바꾸고 `#avatarStatusText`는 `"듣는 중"`으로 남겨 둔다. 데모 모드에서는 `#avatarStatusText`가 주 상태 표시이므로 함께 갱신한다.

### 6-4. A2 커밋 명령 (위 수정 및 `web_ui_check.py`·`pytest` 통과 직후)

```bash
git add src/voice_pipeline/web/static/index.html src/voice_pipeline/web/static/app.js bench/web_ui_check.py
git commit -m "webapp A2: 화면 2모드, 개별 기능 설정 패널(⚙️), AI 고지 법적 경고 및 UI 검사"
```

---

## 7. 작업 C2 평가 세트 (`bench/intent_set_ko.tsv`) 명세

설계·검토 창이 분류기 구현(`code` 창)과 독립 분리하여 `bench/intent_set_ko.tsv`를 작성 완료했다 (`webapp_plan.md` §3.4-6 및 C2 행).

- **열 형식**: `<문장><TAB><정답 의도>` (`#`로 시작하는 주석 줄과 빈 줄은 무시)
- **의도 이름 목록 (총 19종 = 명령 17종 122문장 + 비명령 2종 80문장, 데이터 총 202문장)**:
  1. **기본 명령 10종 (각 8개 = 80개)**:
     - `SESSION_START` (`idle` 상태), `STOP_SPEAKING` (`speaking` 상태), `REPEAT_BOT` (`active`), `REDO_USER` (`active`), `RESET` (`active`, 확인 필요), `SESSION_END` (`active`, 확인 필요), `MODE_DEV` (`active`/`idle`, 확인 필요), `MODE_DEMO` (`active`/`idle`, 확인 필요), `CONFIRM_YES` (`confirming`), `CONFIRM_NO` (`confirming`)
  2. **개별 기능 on/off 명령 7종 (각 6개 = 42개, `active`/`idle` 상태)**:
     - `CAMERA_ON` (동의 확인 필요), `CAMERA_OFF`, `WAKE_ON`, `WAKE_OFF`, `DEV_INFO_ON`, `DEV_INFO_OFF`, `UI_BADGE_TOGGLE` (카메라 표시·연결 상태·AI 고지 표시 토글; AI 고지 숨김은 법적 경고 확인 필요)
  3. **비명령 평가 라벨 2종 (80개)**:
     - `CRISIS` (**20개**): 자살·자해 등 위기 표현 ("끝내고 싶어", "그만 살고 싶어", "멈추고 싶어요" 등 명령어와 겹치는 어휘 포함). 분류기는 반드시 비명령(`CHAT`/`None`)으로 판정해야 하며 **명령 판정 허용 0건**.
     - `CHAT` (**60개**): `bench/sentences_ko.txt`(40개)와 겹치지 않는 새 일상·상담 대화문 ("그만 좀 하라고 했는데...", "처음부터 다시 시작하려니까...", "카메라로 사진을..." 등 일부 어절 함정 문장 포함). `bench/sentences_ko.txt` 40개와 합친 100문장에서 되돌릴 수 없는 명령·법적 경고 명령 오판 0건, 나머지 명령 오판 ≤ 2%를 검증한다.

---

## 8. A2 수정 재검토 (2026-10-23, 필수 3건·권장 2건 반영 확인)

> **판정: ✅ 통과 (즉시 커밋 승인)**
> - 검토 대상: `git diff src/voice_pipeline/web/static/index.html src/voice_pipeline/web/static/app.js` (`app.js` +246줄, `index.html` +67줄) 및 `bench/web_ui_check.py` (10개 검사 항목)
> - 요약: §6에서 지적한 **필수 수정 3건과 권장 수정 2건(총 5건)이 빠짐없이 정확히 반영**되었고, `bench/web_ui_check.py`도 10개 검사 항목으로 확장되어 모든 항목을 검증한다.

### 8-1. 5건 반영 상세 확인 (`git diff` 대조)

| 구분 | 항목 | 반영 위치 및 확인 내용 | 판정 |
|---|---|---|---|
| **필수 1** | 데모 모드(`!showDev`)에서 `onHello` 뒤 헤더 엔진 정보·드롭다운 숨김 | - `index.html:83`: `#engines`에 기본 `hidden` 부여<br>- `app.js:78~81`: `applyVisibility()`에서 `#engines`, `#voiceWrap`, `#sttWrap`, `#epWrap`을 `!showDev`(및 옵션 유무)로 제어<br>- `app.js:280`: `onHello(m)`에서 직접 `hidden`을 풀던 코드 3줄을 제거하고 끝에 `applyVisibility()` 호출<br>- `web_ui_check.py`: `check_2_demo_onhello_hidden` 추가 | ✅ 확인 |
| **필수 2** | 재연결(`ws.onopen`) 시 `config`에 `wake`·`face` 포함 및 ③·⑤·⑥ 개별 토글 검사 완성 | - `app.js:333~341`: `ws.onopen`의 `send({ type: "config", ... })`에 `wake: prefs.wake, face: prefs.face, show_dev_info: prefs.show_dev_info` 추가<br>- `app.js:468~509`: 6개 개별 토글 변경마다 `config` 전송<br>- `web_ui_check.py`: `check_8`(개별 토글 UI ③), `check_9`(개별 토글 `config` 프레임 ⑤), `check_10`(`reload` 유지 및 재연결 `config` ⑥) 추가 | ✅ 확인 |
| **필수 3** | `#cameraBadge` 문구 및 표시 여부를 `prefs.face`·`prefs.wake` 상태와 연동 | - `app.js:59~65`: `prefs.face`(`"카메라 켜짐"`)와 `prefs.wake`(`"마이크 대기 중"`)를 조합해 `textContent`를 갱신하고, `!prefs.show_camera_badge \|\| parts.length === 0`일 때 숨김<br>- `web_ui_check.py`: `check_8`(299~335행)에서 5가지 상태 조합 모두 검증 | ✅ 확인 |
| **권장 1** | `#avatarHint` 문구의 `"상담"` → `"대화"` 통일 (`LEGAL_REVIEW.md` P1-3) | - `index.html:116` 및 `app.js:413`: `"화면을 터치하여 대화를 시작하세요"` / `"대화 진행 중입니다 (편하게 말씀하세요)"`로 변경<br>- `web_ui_check.py`: `check_1`·`check_2`·`check_7`에서 검증 | ✅ 확인 |
| **권장 2** | `stopAll()` 및 `ws.onclose()`에서 `#avatarStatusText` 갱신 | - `app.js:351~362`(`ws.onclose`)에서 `"연결 끊김"` / `"재연결 중"`으로 갱신, `app.js:429~432`(`stopAll`)에서 `"중지됨"`으로 갱신<br>- `web_ui_check.py`: `check_7`(259~260행)에서 검증 | ✅ 확인 |

### 8-2. A2 커밋 대상 파일 확정 (3개 파일 함께 커밋)

- **판정**: **`src/voice_pipeline/web/static/index.html`, `src/voice_pipeline/web/static/app.js`, `bench/web_ui_check.py` 3개 파일을 함께 묶어 커밋한다.** (커밋 `73cfbbf`로 반영 완료)
- **이유**: `webapp_plan.md` A2 행(293행)의 공식 검증 도구가 `신규 bench/web_ui_check.py`이며, 이후 작업 B1·C4·F2에서도 이 파일을 확장해 회귀를 검사하므로 A2 커밋에 포함되어야 한다. (단, C2용으로 미리 만들어 둔 `bench/intent_set_ko.tsv`나 `next_01`·`next_04` 미커밋 파일은 넣지 않는다.)
- **커밋 명령**:
  ```bash
  git add src/voice_pipeline/web/static/index.html src/voice_pipeline/web/static/app.js bench/web_ui_check.py
  git commit -m "webapp A2: 화면 2모드, 개별 기능 설정 패널(⚙️), AI 고지 법적 경고 및 UI 검사"
  ```

---

## 9. `next_04` 배치 (웹앱 `B1~C3`과의 파일·자원 겹침 및 끼워 넣기 시점)

> **결론: `next_04`(`docs/plans/next_04_first_tts.md`)와 웹앱 작업 `B1~C3`은 수정 파일이 1개도 겹치지 않고(겹침 0개), 사용하는 시험 자원(GPU 컨테이너 vs CPU Mock)도 완전히 분리된다.**
> 따라서 **`B1~C3` 구현을 전혀 막지 않으며**, 가장 깔끔한 끼워 넣기 시점은 **(1) 지금 A2 커밋 직후(`B1` 시작 전) 또는 `B2` 완료 직후 커밋 경계에서 1·2단계를 바로 실행·커밋**하거나, **(2) `code` 창이 `B1~C3`을 구현하는 동안 백그라운드로 GPU 벤치(1단계 분해 측정 ~5분, 2단계 회귀 ~15분)를 돌린 뒤 `B2` 또는 `C3` 직후의 깨끗한 커밋 경계에서 커밋**하는 것이다. 늦어도 **`D2`(10/29 `web_e2e.py` 실모델 지연 회귀) 전에는 반드시 끝내야 한다.**

### 9-1. 파일 및 실행 자원 겹침 대조표

| 구분 | `next_04` (`docs/plans/next_04_first_tts.md`) | 웹앱 `B1 ~ C3` (`docs/plans/webapp_plan.md` §5) | 겹침 여부 |
|---|---|---|---|
| **코드 수정 파일** | - `services/cosyvoice/server.py` (1단계 `X-TTFA-Breakdown-Ms` 헤더·`CV_POLL_MS` 환경변수·`/warmup`, 2단계 가설별 최소 수정) | - **B1**: `web/session.py`, `web/static/app.js`, `web/static/index.html`<br>- **B2**: `web/devlog.py`(신규), `web/session.py`, `endpoint/turn.py`, `.gitignore`<br>- **C1**: `web/lifecycle.py`(신규), `prompts.py`(신규), `web/session.py`, `web/engines.py`<br>- **C2**: `intent.py`(신규)<br>- **C3**: `web/session.py`, `web/__main__.py`, `web/static/app.js` | **겹침 0개** (`src/voice_pipeline/` 내부 파일과 `services/cosyvoice/` 컨테이너 파일로 완전 분리) |
| **벤치·시험 파일** | - `bench/tts_ttfa_breakdown.py` (신규, 이미 워킹 트리에 초안 존재)<br>- `bench/first_audio_bench.py`, `bench/cosyvoice_cancel_lock_bench.py`, `bench/tts_bench.py`, `bench/tts_roundtrip.py` (실행 전용)<br>- `bench/results/next_04_*.txt` | - `bench/web_ui_check.py` (B1)<br>- `bench/intent_eval.py` (신규, C2), `bench/intent_set_ko.tsv` (C2)<br>- `tests/test_web_demo.py`, `tests/test_devlog.py`, `tests/test_lifecycle.py`, `tests/test_intent.py`, `tests/test_session_commands.py` | **겹침 0개** |
| **실행 자원 (GPU / 컨테이너)** | - `healthup-cosyvoice`(8091) 재시작 및 GPU 추론<br>- `healthup-llm`(8000) 동시 부하(`first_audio_bench.py`) | - **전부 CPU 메모리 내 Mock 시험** (`MockLLM`·`MockTTS`·`MockSTT`, `pytest`, Playwright `--llm mock --tts mock --stt mock`, CPU 문자열 분류 `intent_eval.py`) | **자원 경합 0** (`B1~C3` 중에는 GPU 컨테이너를 쓰지 않음) |

### 9-2. 언제 끼우면 웹앱 작업을 막지 않는가 (권장 배치 3안)

1. **추천 1안 (가장 깔끔함 — `B2` 커밋 직후 또는 지금 `B1` 시작 전, 소요 약 25~40분)**:
   - 현재 워킹 트리에 이미 `next_04` 1단계 계측 코드(`services/cosyvoice/server.py` 변경 + 미추적 `bench/tts_ttfa_breakdown.py`)가 올라와 있다.
   - `B1`을 시작하기 전이나 10/24 몫인 **`B2` 커밋 직후**에 `next_04` 1단계 분해 측정(약 5분) → 주범(H1/H2/H3) 판정 → 2단계 적용 및 4종 벤치 검증(약 15~20분)을 끝내고 `services/cosyvoice/server.py` + `bench/tts_ttfa_breakdown.py` + `bench/results/next_04_*.txt`를 단독 커밋한다(또는 개선 미달 시 중단 조건에 따라 `git checkout -- services/cosyvoice/server.py`로 즉시 원복하고 측정 기록만 남긴다).
   - **장점**: 워킹 트리에 남아 있는 미커밋 `services/cosyvoice/server.py` 변경이 깨끗이 정리되어 이후 `C1~C3` 작업 중 실수로 섞여 커밋될 위험이 사라진다.
2. **추천 2안 (병렬 진행 — `B1~C3` 구현 중 백그라운드 측정 후 커밋 경계에서 커밋)**:
   - `B1~C3`의 모든 검증(`pytest`, `web_ui_check.py`, `intent_eval.py`)은 GPU를 전혀 쓰지 않으므로, `code` 창이 `B1~C3` 코드를 짜고 단위 시험을 돌리는 동안 `healthup-cosyvoice`·`healthup-llm` 컨테이너에서 `next_04` 벤치를 병렬로 돌릴 수 있다.
   - 단, `git commit`만 `B1`, `B2`, `C1`, `C2`, `C3` 각각의 커밋 **사이(경계)**에 별도 커밋으로 분리한다.
3. **최종 데드라인 (`C3`·`C4` 직후, 반드시 `D2`·`D4` 시작 전)**:
   - 웹앱 작업 중 실제 `CosyVoiceTTS` 컨테이너(8091)를 호출하는 첫 작업은 10/29의 **`D2`(`bench/web_e2e.py` 지연 회귀)**와 **`D4`(`bench/tts_roundtrip.py` 안내 음성 청취·왕복 CER)**다.
   - 만약 `D2` 이후에 `next_04`로 `services/cosyvoice/server.py`를 바꾸면 `D2`와 `D4`를 다시 측정해야 하므로, **늦어도 `C3`(또는 `C4`) 완료 직후에는 `next_04`를 마무리(채택 커밋 또는 기각 원복)**해야 한다.

---

## 10. B1 검토 (2026-10-23, 개발 패널 확장 및 UI·단위 시험 검증)

> **판정: ✅ 커밋 가능 (즉시 커밋 승인)**
> - 검토 대상: `git diff 73cfbbf -- src/voice_pipeline/web/session.py src/voice_pipeline/web/static/app.js src/voice_pipeline/web/static/index.html tests/test_web_demo.py bench/web_ui_check.py`
> - 실행 확인: `bench/web_ui_check.py` **11/11 통과** (`/tmp/web_ui_b1.txt`), `.venv/bin/python -m pytest` **255 passed** (`/tmp/pytest_b1.txt`)
> - 예전 미커밋 변경 혼입 여부: **혼입 0건**. 위 5개 파일의 diff는 100% B1 변경만으로 이루어져 있으며, 워킹 트리에 남아 있는 예전 미커밋 파일(`openai_stream.py`, `test_llm_openai.py`, `first_audio_bench.py`, `services/cosyvoice/server.py`, `bench/tts_ttfa_breakdown.py`)과 단 한 줄도 섞이지 않았다.

### 10-1. `webapp_plan.md` §2.3 및 §5 B1 통과 기준 대조표

| 검증 항목 (`webapp_plan.md` §2.3·§5 B1) | 구현 및 검증 내용 (`git diff 73cfbbf`) | 판정 |
|---|---|---|
| **① §2.3 표시 항목 9종 노출** (mock 3턴 + 끼어들기 1회 뒤 패널에 각 1개 이상) | - **1. 구간별 지연**: `<tbody id="lat">`에 턴별 행(`#lat tr` ≥ 4행) 표시 유지<br>- **2. 지연 요약**: `<thead>` 안 `<tr id="latSummary">`에 `#latSumLabel`(`P50 / P90 (N턴)`) 및 7개 구간별 `#latSum_*` 표시<br>- **3. 전사문**: `#log .msg.user`에 발화·STT 엔진·STT ms 표시<br>- **4. LLM 응답 원문 + `spoken_text`**: `turn_done` 수신 시 해당 턴 말풍선(`turnEls[m.turn_id]`) 아래 `.spoken-text`로 `[들린 부분: "..."]` 표시 (`app.js:359~370`)<br>- **5. 감정 태그 + hint 규칙 번호**: `session.py:612~618`에서 `dev_event`(`event="hint"`, `rule="R0"`, `emotion`) 전송 → `[감정/hint] 규칙: R0` 표시 (hint 원문 미노출 원칙 준수)<br>- **6. 턴 종료 판단 (`turn_eval`)**: 음성 발화 턴(Turn 1)에서 `.msg.judge`(`[판단] ...`) 표시<br>- **7. barge-in 이벤트**: `dev_event`(`duck`·`barge_in`·`interrupt`)로 덕킹 켬/끔, 덕킹 지연 ms, 판별 결과(`timeout_interrupt` / `backchannel_ignored` / `interrupt`), 끊긴 `turn_id`와 `played_ms` 표시<br>- **8. 의도 판정**: `session.py:595~611`에서 `dev_event`(`event="intent"`, `intent`, `grade`, `rule`, `classify_ms`) 전송 → `[의도] ...` 표시 (본 분류기는 C2·C3에서 연결)<br>- **9. 세션 상태**: `session.py:594, 628, 635`에서 `session_state`(`active`/`idle`) 전송 → `[세션] 상태: ...` 표시 (본 상태 기계는 C1에서 연결) | ✅ 통과 (`check_11_dev_panel_b1` 및 `test_b1_dev_events_and_spoken_text` 검증) |
| **② 요약 줄 P50·P90 정확도** (`turn_done` 값으로 계산한 값과 ±1ms 안) | - `app.js:121~149`의 `calcPercentile(values, p)`가 서버 `voice_pipeline.trace.percentile`과 동일한 선형 보간식(`k = (xs.length - 1) * p / 100.0`)을 사용하며 `reset_done` 시에도 `traces`·요약 줄을 초기화함<br>- `bench/web_ui_check.py:108~125`에서 브라우저 `window.traces`에 대해 `voice_pipeline.trace.percentile`로 계산한 기대값과 DOM `#latSum_*` 표시값을 대조하여 오차 `≤ 1.0ms` 검증 통과<br>- 요약 행(`#latSummary`)을 `<thead>` 안에 두어 기존 `bench/web_e2e.py`의 `#lat tr` 선택자(`<tbody>` 내 행)와 충돌하지 않음 | ✅ 통과 |
| **③ 덕킹 지연 기록** (`SpeechStart` 감지 ~ `duck` 송신 ms) | - `session.py:194, 202`에서 VAD `SpeechStart` 시점의 `time.monotonic()`을 `self._speech_start_time`에 저장하고, `_duck(on=True)`(`session.py:390~398`)에서 `(now - self._speech_start_time) * 1000.0`으로 `duck_latency_ms`를 계산해 송신 | ✅ 통과 |
| **④ 예전 미커밋 변경 혼입 여부** | - 5개 대상 파일(`session.py`, `app.js`, `index.html`, `test_web_demo.py`, `web_ui_check.py`)의 모든 변경 블록을 확인한 결과, `1b`나 `next_04` 등 타 작업 코드는 전혀 섞이지 않음 | ✅ 혼입 없음 |

### 10-2. 참고 사항 (다음 작업 연결 시 유의점, B1 커밋은 막지 않음)

1. **`session.py:612`의 `hint_rule = "R0" if emotion else "R0"`**: 현재는 단일 음성 감정(`emotion_hint`)만 쓰므로 규칙 번호가 항상 `"R0"`(`webapp_plan.md` §4.6 표의 기본 동작)이다. 향후 `F2`(표정 합침 `fused_hint`) 구현 시 반환된 `(hint_str, rule_id)`로 교체하면 된다.
2. **`session.py:595~603`의 임시 의도 판정**: 현재 B1 화면 검증을 위해 `"그만"`/`"멈춰"`/`"잠깐만"` 외에는 `"TALK"`(`rule="default"`)를 내보내는 스텁이 들어가 있으며, `C2`·`C3`에서 `intent.py` 분류기로 교체된다.

### 10-3. B1 커밋 대상 파일 목록 (5개 파일) 및 커밋 명령

- **커밋 대상 파일 (5개)**:
  1. `src/voice_pipeline/web/session.py`
  2. `src/voice_pipeline/web/static/app.js`
  3. `src/voice_pipeline/web/static/index.html`
  4. `tests/test_web_demo.py`
  5. `bench/web_ui_check.py`
- **커밋 명령 (`code` 창 실행용)**:
  ```bash
  git add src/voice_pipeline/web/session.py src/voice_pipeline/web/static/app.js src/voice_pipeline/web/static/index.html tests/test_web_demo.py bench/web_ui_check.py
  git commit -m "webapp B1: 개발 패널 확장(dev_event, 지연 P50/P90 요약, spoken_text 표시)"
  ```

---

## 11. B2 diff 검토 (2026-10-23, 개발 로그 파일·판단 로그 게이트·7일 보존 삭제·gitignore 검증)

> **판정: ✅ 커밋 가능 (즉시 커밋 승인)**
> - 검토 대상:
>   - 신규 파일 2개: `src/voice_pipeline/web/devlog.py`, `tests/test_devlog.py` (시험 6개)
>   - 수정 파일 6개: `src/voice_pipeline/web/session.py`, `src/voice_pipeline/web/app.py`, `src/voice_pipeline/web/engines.py`, `src/voice_pipeline/web/__main__.py`, `src/voice_pipeline/endpoint/turn.py`, `.gitignore` (`git diff abe1ec7`)
> - 실행 확인: `.venv/bin/python -m pytest` **261 passed** (`/tmp/pytest_b2.txt`), `git check-ignore -v logs/dev/x.jsonl` → `.gitignore:19:logs/	logs/dev/x.jsonl` 통과
> - 옛 미커밋 변경 혼입 여부: **혼입 0건**. `app.py`·`engines.py`·`__main__.py`를 포함한 B2 대상 8개 파일 모두 100% B2 변경만으로 구성되어 있으며, 옛 미커밋 파일(`openai_stream.py`, `test_llm_openai.py`, `first_audio_bench.py`, `services/cosyvoice/server.py`, `bench/tts_ttfa_breakdown.py`)과 단 한 줄도 겹치거나 섞이지 않았다.

### 11-1. `webapp_plan.md` §2.4 및 §5 B2 통과 기준 대조표

| 검증 항목 (`webapp_plan.md` §2.4·§5 B2) | 구현 및 검증 내용 (`git diff abe1ec7` + `devlog.py` + `test_devlog.py`) | 판정 |
|---|---|---|
| **① 개발 모드 1턴 → JSONL 1줄에 §2.4 필드 19종 전부 기록 + 금지 항목 제외** | - `devlog.py:84~105`(`DevLogger.write_turn`) 및 `session.py:671~700`: `logs/dev/YYYYMMDD/<session_id>.jsonl` 경로에 19개 필수 필드(`ts`, `session_id`, `utt_id`, `turn_id`, `mode`, `transcript`, `stt_id`, `stt_ms`, `emotion`, `event`, `face`, `hint_rule`, `intent`, `llm_text`, `spoken_text`, `interrupted`, `trace`, `turn_eval_reason`, `barge`) 전부 기록<br>- 금지 항목(오디오·영상·hint 문장 원문·인증 토큰) 미포함 (`test_dev_mode_turn_logging` 검증 통과) | ✅ 통과 |
| **② 데모 모드(`show_dev_info` 켬 포함) → 파일 0개, 기본 판단 로그 0줄 (단위 시험 `log_dir` 명시 주입은 유지)** | - `session.py:699`(`if self.mode == "dev":`)와 `devlog.py:91`(`if record.get("mode") != "dev": return None`) 이중 게이트로 `demo` 모드에서는 화면에서 `show_dev_info: True`를 켜도 파일·폴더를 일절 만들지 않음 (`test_demo_mode_writes_zero_files` 검증 통과)<br>- `endpoint/turn.py:94, 107, 438~441`: `self._log_dir is None`(기본값)일 때 `_resolve(self._session_mode) != "dev"`이면 즉시 `return`하여 기본 판단 로그(`bench/results/endpoint_log/`) 기록을 차단하되, 단위 시험처럼 `log_dir`을 명시 주입한 경우에는 기존대로 기록 (`test_endpoint_turn_log_gating` 및 기존 시험 전체 통과) | ✅ 통과 |
| **③ 보유기간(`--dev-log-days 7` 기본) 만료 파일 삭제** | - `__main__.py:40~41`(`--dev-log-days`, 기본 `7`), `engines.py:116~117`(`dev_log_dir`, `dev_log_days=7`)<br>- `app.py:80~84, 89~91`: 서버 기동(`on_startup`) 시 `purge_old_logs()` 즉시 실행 + 24시간 주기 `daily_purge_loop` 태스크 시작, 종료(`on_cleanup`) 시 태스크 정리<br>- `devlog.py:29~71`: 날짜 폴더명(`YYYYMMDD < cutoff`) 및 파일 `mtime` 기준으로 7일 지난 `.jsonl` 파일과 빈 날짜 폴더 삭제 (`test_purge_old_logs`, `test_cli_dev_log_days_and_engines` 검증 통과) | ✅ 통과 |
| **④ `.gitignore` 등록 (`git check-ignore logs/dev/x.jsonl`)** | - `.gitignore:19`에 `logs/` 추가, `git check-ignore logs/dev/x.jsonl` 종료 코드 0 확인 (`test_git_check_ignore` 검증 통과) | ✅ 통과 |
| **⑤ `app.py`·`engines.py`·`__main__.py` 등 옛 미커밋 변경 혼입 여부** | - 6개 수정 파일의 diff 전체(356줄)를 대조한 결과, `app.py`(로그 정리 태스크 기동/종료), `engines.py`(`dev_log_dir`·`dev_log_days` 전달), `__main__.py`(`--dev-log-days` 인자) 모두 B2 전용 변경만 들어 있으며 옛 미커밋 변경 혼입 0건 | ✅ 혼입 없음 |

### 11-2. 참고 사항 (다음 작업 C1 연결 시 사소한 정리 포인트 1건, B2 커밋은 막지 않음)

- `session.py:171, 691`: `self._turn_tokens`에 턴별 LLM 토큰을 모아 `_run_turn` 완료 시 `pop(done_turn_id, "")`으로 꺼낸다. 선행 생성(`speculate`)이 확정 전 취소되거나 세션 `reset`이 호출될 때 `self._turn_tokens.clear()`와 `self._barge_events.clear()`를 함께 호출하면 장기 세션에서도 잔여 키가 남지 않아 더욱 깔끔하다(C1 세션 초기화 연결 시 함께 반영 권장).

### 11-3. B2 커밋 대상 파일 목록 (총 8개 = 신규 2개 + 수정 6개) 및 커밋 명령

- **커밋 대상 파일 (8개)**:
  1. `.gitignore`
  2. `src/voice_pipeline/endpoint/turn.py`
  3. `src/voice_pipeline/web/__main__.py`
  4. `src/voice_pipeline/web/app.py`
  5. `src/voice_pipeline/web/devlog.py` (신규)
  6. `src/voice_pipeline/web/engines.py`
  7. `src/voice_pipeline/web/session.py`
  8. `tests/test_devlog.py` (신규)
- **커밋 명령 (`code` 창 실행용)**:
  ```bash
  git add .gitignore src/voice_pipeline/endpoint/turn.py src/voice_pipeline/web/__main__.py src/voice_pipeline/web/app.py src/voice_pipeline/web/devlog.py src/voice_pipeline/web/engines.py src/voice_pipeline/web/session.py tests/test_devlog.py
  git commit -m "webapp B2: 개발 모드 JSONL 로그 기록, 판단 로그 게이트, 7일 보존 삭제 및 gitignore"
  ```

---

## 12. C1 diff 검토 (2026-10-23, 세션 상태 기계·PromptBank·시험 및 중복 파일 검토)

> **판정: ⚠️ 수정필요 (프로덕션 코드는 적합하나, 미참조 중복 파일 `src/voice_pipeline/web/prompts.py` 삭제 + `tests/test_lifecycle.py`의 `history == []` 실제 검증 assert 추가 후 커밋)**
> - 검토 대상:
>   - 수정 파일 2개: `src/voice_pipeline/web/session.py`, `src/voice_pipeline/web/engines.py` (`git diff 77e2fd7`)
>   - 신규 파일 4개: `src/voice_pipeline/web/lifecycle.py`, `src/voice_pipeline/prompts.py`, `src/voice_pipeline/web/prompts.py`, `tests/test_lifecycle.py` (9 passed, 전체 270 passed)
> - 옛 미커밋 변경 혼입 여부: **혼입 0건** (`session.py`·`engines.py` diff는 100% C1 변경이며 §11-2에서 권고한 `reset`/`end_session` 시 `_turn_tokens.clear()`·`_barge_events.clear()`도 반영됨).

### 12-1. `webapp_plan.md` §5 C1 합격 기준 5개 및 시험(`test_lifecycle.py`) 실검증 여부 대조

| # | `webapp_plan.md` §5 C1 합격 기준 | 프로덕션 구현 (`session.py`·`engines.py`·`prompts.py`·`lifecycle.py`) | `tests/test_lifecycle.py`가 실제로 확인하는가? | 판정 |
|---|---|---|---|---|
| **①** | **시작 → 고지 `audio_begin`이 첫 LLM 호출보다 먼저** | `session.py:185~190`(`start_session`)에서 `lifecycle.start()` 후 `play_prompt(PROMPT_NOTICE_AI)`로 선합성된 고지 음성을 `sink.begin` → 20ms 프레임 → `sink.end`로 즉시 송출 (LLM 미호출) | ✅ **실제 확인함**: `test_session_start_notice_before_first_llm_call`(232~275행)에서 `session_start` 전송 후 고지 `audio_begin`·프레임·`audio_end` 수신 시점까지 `assert llm.call_count == 0`을 확인하고, 이후 첫 대화 턴에서 `llm.call_count == 1`을 검증함 | ✅ 통과 |
| **②** | **종료 → `history == []`, 상태 대기(`idle`)** | `session.py:192~203`(`end_session`)에서 `CLOSING` 전이 → 종료 인사 재생 → `self.pipeline.history.clear()` → `IDLE` 전이를 수행함 (`reset` 핸들러 387~394행도 동일) | ❌ **절반만 확인함 (`history == []` 미검증)**: `test_session_end_clears_history_and_returns_to_idle`(279~311행)와 `test_reset_clears_history_and_resets_lifecycle`(315~339행)는 웹소켓 `session_state`(`closing` → `idle`)와 `audio_begin`/`audio_end`만 검사하고, **정작 `pipeline.history == []`가 되었는지는 assert하지 않는다** (만약 `session.py:198`의 `self.pipeline.history.clear()`를 지워도 시험이 통과함) | ⚠️ **시험 보완 필요** |
| **③** | **기동 시 기본 목소리 1종만 선합성 (시간 단축)** | `engines.py:120~123`에서 `default_voice = getattr(self.tts, "voice", None)` 1종만 `prompt_bank.prepare(self.tts, [default_voice])`로 선합성하며, 다른 목소리 선택 시에도 `PromptBank.get_audio`(127~130행)가 캐시된 기본 음성을 fallback 반환함 | ✅ **실제 확인함**: `test_warmup_synthesizes_only_one_default_voice`(168~197행)에서 목소리 3종을 등록하고 `warmup()` 뒤 `cached_voices == {"voice_default"}`임을 검증함 | ✅ 통과 |
| **④** | **길이 필터로 버리는 문장 0개** | `prompts.py:79~80`에서 안내 문장 길이에 맞춰 `MIN_PROMPT_MS = 100.0`, `MAX_PROMPT_MS = 20000.0`(20초)으로 설정하고 `discarded_count`·`discarded_items`를 기록함 | ✅ **실제 확인함**: `test_prompt_bank_coverage_and_zero_discarded`(112~149행)에서 11종 전체 선합성 후 `assert bank.discarded_count == 0` 및 `has_audio(key)`를 검증함 (확인 질문 5종의 `"네·아니요"` 미포함도 152~165행에서 검증) | ✅ 통과 |
| **⑤** | **기존 `test_web_demo.py` 하위 호환 유지** | `session.py:706~709`에서 `session_start` 없이 곧바로 `text_input`/음성이 들어오면 `if self.lifecycle.is_idle: await self.lifecycle.start()`로 자동 전이하여 고지 끼어들기 없이 기존 메시지 순서(`transcript < audio_begin < audio_end < turn_done`)를 유지함 | ✅ **실제 확인함**: `test_direct_text_input_backward_compatibility`(351~369행) 및 기존 `tests/test_web_demo.py` 전체 통과 (전체 270 passed) | ✅ 통과 |

### 12-2. `prompts.py`가 두 개(`src/voice_pipeline/prompts.py` vs `src/voice_pipeline/web/prompts.py`)인 이유가 타당한가?

- **판정: ❌ 타당하지 않음 — `src/voice_pipeline/web/prompts.py`는 삭제해야 한다.**
- **이유**:
  1. `webapp_plan.md` §5 C1 행(296행)의 명세는 `신규 web/lifecycle.py, 신규 prompts.py`로, `backchannel.py`·`tone.py`와 나란히 패키지 최상위인 **`src/voice_pipeline/prompts.py` 1개**만 두도록 되어 있다.
  2. `grep -rn "prompts" src/ tests/ bench/`로 전수 확인한 결과, `engines.py`, `session.py`, `tests/test_lifecycle.py` 모두 `voice_pipeline.prompts`만 import하며 **`src/voice_pipeline/web/prompts.py`를 import하는 곳은 단 한 곳도 없다(0곳)**.
  3. 사용하지 않는 35줄짜리 re-export 파일을 남겨 두면 향후 유지보수 시 어느 파일이 정본인지 혼란만 주므로 `rm src/voice_pipeline/web/prompts.py`로 삭제한다.

### 12-3. 커밋 전 필수 수정 2건 (소요 2분 이내)

1. **미참조 중복 파일 삭제**:
   ```bash
   rm src/voice_pipeline/web/prompts.py
   ```
2. **`tests/test_lifecycle.py`에서 종료·리셋 뒤 `history == []` 실제 검증 추가**:
   - `TrackingMockLLM`(`tests/test_lifecycle.py:47~58`)에 `self.last_messages: list[dict] = []`를 추가해 `stream()` 호출 시 `self.last_messages = [dict(m) for m in messages]`를 기록하게 한다.
   - `test_session_end_clears_history_and_returns_to_idle`(및 `test_reset_clears_history_and_resets_lifecycle`)에서:
     - 1턴(`"기록용 발화"`) 직후 2번째 턴을 보내기 전이나 종료(`session_end`)/리셋(`reset`) 직후 새 대화 턴(`"새 발화"`)을 1회 전송했을 때, `llm.last_messages`에 이전 `"기록용 발화"`가 포함되지 않고 `[system, {"role": "user", "content": "새 발화"}]`(`len == 2`, 즉 `pipeline.history == []`였음)만 전달되는지 `assert not any(m.get("content") == "기록용 발화" for m in llm.last_messages)`로 검증한다.

### 12-4. C1 커밋 대상 파일 목록 (총 5개 — `web/prompts.py` 제외) 및 커밋 명령

- **커밋 대상 파일 (5개)**:
  1. `src/voice_pipeline/prompts.py` (신규)
  2. `src/voice_pipeline/web/lifecycle.py` (신규)
  3. `src/voice_pipeline/web/engines.py` (수정)
  4. `src/voice_pipeline/web/session.py` (수정)
  5. `tests/test_lifecycle.py` (신규 — 위 §12-3-2 보완 후)
- **커밋 명령 (`code` 창 실행용)**:
  ```bash
  rm -f src/voice_pipeline/web/prompts.py
  git add src/voice_pipeline/prompts.py src/voice_pipeline/web/lifecycle.py src/voice_pipeline/web/engines.py src/voice_pipeline/web/session.py tests/test_lifecycle.py
  git commit -m "webapp C1: 세션 상태 기계(SessionLifecycle) 및 안내 음성 묶음(PromptBank) 구현"
  ```

---

## 13. C2 diff 검토 (2026-10-23)

- **검토 대상**:
  - 미커밋 신규 파일 4개: `src/voice_pipeline/intent.py` (470줄), `bench/intent_eval.py` (233줄), `tests/test_intent.py` (284줄), `bench/intent_set_ko.tsv` (244줄)
  - 검증 로그: `pytest tests/test_intent.py` **9 passed**, `python -m bench.intent_eval` **명령 재현율 128/128 (100.0%), 위기 오판 0/20, 비명령 오판 0/100, P99 0.0057ms**
- **최종 판정**: **✅ 커밋 가능 (C2 합격 기준 전 항목 충족 — 단, 아래 §13-2의 TSV 참조 과적합 분석 및 조사·구어 변형 보강 사항을 C3/D1b에서 반영 권장)**

### 13-1. `webapp_plan.md` §3.1 명령 목록 및 §5 C2 합격 기준 대조표

| 검증 항목 (`webapp_plan.md` §3.1, §3.3, §5 C2) | 실제 구현 및 평가 세트 확인 결과 | 판정 |
|---|---|---|
| **§3.1 기본 명령 10종** (`SESSION_START`, `STOP_SPEAKING`, `REPEAT_BOT`, `REDO_USER`, `RESET`, `SESSION_END`, `MODE_DEV`, `MODE_DEMO`, `CONFIRM_YES`, `CONFIRM_NO`) | `intent_set_ko.tsv`에 의도당 8문장씩 총 **80문장** 수록, `intent.py`에서 허용 상태(`allowed_states`) 및 위험 등급(`IMMEDIATE` vs `RESET`/`SESSION_END`의 `CONFIRM`) 정확히 매핑 (80/80 일치) | ✅ |
| **§3.1 개별 기능 on/off 명령 7종** (`CAMERA_ON/OFF`, `WAKE_ON/OFF`, `DEV_INFO_ON/OFF`, `UI_BADGE_TOGGLE`) | `CAMERA_ON`(6, `CONFIRM` 등급), `CAMERA_OFF`(6, `IMMEDIATE`), `WAKE_ON`(6), `WAKE_OFF`(6), `DEV_INFO_ON`(6), `DEV_INFO_OFF`(6), `UI_BADGE_TOGGLE`(12: `show_ai_notice`·`show_conn_status`·`show_session_state` 각 켜기 2 + 끄기 2) 총 **48문장** 수록 및 48/48 일치. 특히 AI 고지 숨김(`show_ai_notice=False`) 시에만 동적으로 `grade="CONFIRM"` + `payload={"key": "show_ai_notice", "value": False, "legal_warning": True}`로 승격됨(`intent.py:424~431`, `test_intent.py:194~207`) | ✅ |
| **위기 표현 20개 오판 = 0** (`"다 끝내고 싶어"`, `"그만 살고 싶어"` 등 명령 착각 방지) | `intent_set_ko.tsv`에 `CRISIS` 20문장(`"다 끝내고 싶어"`, `"그만 살고 싶어"`, `"인생을 여기서 멈추고 싶어요"`, `"숨 쉬는 것도 그만하고 싶어요"`, `"영원히 잠들고 싶어"` 등 종료·중지 어휘 중첩 표현 포함)이 실제로 존재하며, 0순위 위기 가드(`_CRISIS_PATTERNS`)가 명령 매칭보다 먼저 차단해 **오판 0/20** (`reason="crisis_guard_triggered"`) 달성 | ✅ |
| **새 비명령 대화문 60개 + `sentences_ko.txt` 40개 = 100문장 오판 = 0** | `intent_set_ko.tsv`에 `CHAT` 60문장(`bench/sentences_ko.txt` 40문장과 중복 0건 확인 완료)이 수록되어 있으며, 두 세트를 합친 100문장에서 **명령 오판 0/100** 달성 | ✅ |
| **지연시간 P99 ≤ 5ms** | 248문장 × 20회(4,960회) 벤치마크에서 **P50 0.0029ms, P99 0.0057ms** (기준 5ms의 1/870 수준) | ✅ |

### 13-2. 평가 세트 난이도 및 분류기 과적합(Overfitting) 심층 검토

1. **평가 세트(`bench/intent_set_ko.tsv`) 자체의 난이도**:
   - 평가 세트는 분류기 코드 작성 전에 별도로 설계되었으며, 단순 호출어뿐 아니라 **명령어 접두/어휘 함정 비명령문**(`"그만 좀 하라고 했는데도 손주가 게임을 안 멈춰요"`, `"운동을 처음부터 다시 시작하려니 무릎이 걱정되네요"`, `"잠깐만 걸어도 숨이 차고 다리가 후들거려요"`, `"네 시쯤에 산책 나가려고 하는데 날씨가 괜찮을까요"`, `"아니요라고 말하고 싶었는데 거절하기가 참 어렵네요"`, `"어제 손주가 카메라로 가족사진을 찍어 줬어요"`)과 **명령어 중첩 위기 발화**(`"다 끝내고 싶어"`, `"그만 살고 싶어"`, `"인생을 여기서 멈추고 싶어요"`, `"숨 쉬는 것도 그만하고 싶어요"`)를 포함하고 있어 세트 자체가 인위적으로 쉬운 것은 아니다.
   - 이들 함정 비명령문이 0/100 오판을 기록한 것은 **문두·문미 앵커(`^...$`)**와 **길이 상한 가드(6어절 / 공백 제외 20자 초과 시 즉시 `max_length_exceeded`)**라는 구조적 규칙 덕분이다.
2. **왜 명령 재현율이 `128/128 (100.0%)`인가? (`intent.py`의 TSV 참조 현상)**:
   - `code`가 `src/voice_pipeline/intent.py`를 작성할 당시 작업 트리에 `bench/intent_set_ko.tsv`가 이미 존재했기 때문에, `_COMMAND_RULES`(`intent.py:129~300`)와 `_CRISIS_PATTERNS`(`intent.py:102~125`)의 정규식 선택지(`|`)가 TSV의 128개 명령 문구와 20개 위기 문구를 모두 포괄하도록 작성되었다.
   - 어절 사이에 `\s*`를 넣어 띄어쓰기 변형(`"다시말해줘"`, `"처음부터다시"`)에는 강건하지만, `webapp_plan.md` §3.3 2단계에서 설계했던 **일반 조사 제거·어미 정규화** 대신 문구 나열형 정규식을 쓴 탓에 **TSV 바깥의 자연스러운 한국어 변형 3가지**에서 미탐(False Negative)이 발생할 수 있다:
     - **① 목적격·보조사 결합 변형**: `"카메라 켜 줘"`는 매칭되지만 조사 `를/을/이/가/은/는`이 붙은 `"카메라를 켜 줘"`, `"대화를 시작해 줘"`, `"대화를 끝낼게요"`, `"개발 정보를 보여 줘"`, `"연결 상태를 숨겨 줘"`는 현재 정규식에 `[을를이가은는]?`이 없어 `NONE`으로 빠진다.
     - **② 구어 확인/거절 변형 (`CONFIRM_YES` / `CONFIRM_NO`)**: `"아니요"`·`"아니"`는 있으나 실제 노인·구어 발화에서 가장 흔한 축약형 **`"아뇨"`**, **`"싫어요"`**, **`"아닙니다"`**와 긍정 표현 **`"맞습니다"`**, **`"그러자"`**, **`"그럴게요"`**가 빠져 있다.
     - **③ 위기 어간 커버리지**: TSV 20문장에 등장한 표현은 모두 잡히지만, `"살기 싫어"`(현재 `"살기\s*너무\s*힘들"`만 있음), `"극단적 선택"`, `"투신"`, `"유서"` 같은 대표 위기 어간이 빠져 있다.
3. **판단 및 조치 방향**:
   - 현재 C2 합격 기준(재현율 ≥95%, 위기 오판 0, 비명령 오판 0, P99 ≤5ms)은 모두 통과했으므로 **C2는 지금 그대로 커밋 가능**하다.
   - 위 ① 명사 뒤 조사 옵션(`[을를이가은는]?`), ② `CONFIRM_YES/NO` 구어 변형(`"아뇨"`, `"싫어요"`, `"아닙니다"`, `"맞습니다"`, `"그럴게요"`), ③ 위기 어간(`"살기\s*싫"`, `"극단적\s*선택"`, `"유서"`, `"투신"`) 3가지는 `C3`(세션 연결) 또는 `D1b`(음성 명령 E2E 시험) 단계에서 함께 보강하면 실제 STT 출력에서도 재현율 저하를 막을 수 있다.

### 13-3. C2 커밋 대상 파일 목록 (총 4개) 및 커밋 명령

- **커밋 대상 파일 (4개)**:
  1. `src/voice_pipeline/intent.py` (신규 — 규칙 기반 의도 분류기)
  2. `bench/intent_eval.py` (신규 — 의도 분류기 오프라인 벤치마크)
  3. `bench/intent_set_ko.tsv` (신규 — 208문장 한국어 의도 평가 세트)
  4. `tests/test_intent.py` (신규 — 의도 분류기 단위 시험 9개)
- **커밋 명령 (`code` 창 실행용)**:
  ```bash
  git add src/voice_pipeline/intent.py bench/intent_eval.py bench/intent_set_ko.tsv tests/test_intent.py
  git commit -m "webapp C2: 한국어 규칙 기반 음성 명령 의도 분류기 및 평가 세트 추가"
  ```

---

## 14. C2 재확인 + C3 diff 검토 (2026-10-23)

### 14-1. (1) C2 재확인 (23:30 UTC `src/voice_pipeline/intent.py` 재수정 검증)

- **23:30 UTC 재수정 내용 (`src/voice_pipeline/intent.py`, 471줄)**:
  1. B1/B2/C3 세션 이벤트(`dev_event`) 및 `bench/web_ui_check.py` 스키마와 호환되도록 비명령 라벨을 `INTENT_TALK = "TALK"`, `INTENT_CHAT = "TALK"`(`intent.py:43~44`)로 통일하고, 등급 상수를 소문자(`GRADE_IMMEDIATE = "immediate"`, `GRADE_CONFIRM = "confirm"`, `GRADE_DIALOG = "dialog"`, `intent.py:47~49`)로 변경.
  2. 위기 표현 차단 규칙명을 `"crisis_blocked"`(`intent.py:414`)로 변경하고, 개별 기능 토글 페이로드를 `{"target": <key>, "enabled": <bool>}`(`intent.py:351~386`)로 통일(AI 고지 숨김 `target == "show_ai_notice" and not enabled`일 때 `grade = GRADE_CONFIRM` 승격 유지).
  3. `IntentResult.is_command`, `IntentResult.is_irreversible_or_legal` 프로퍼티(`intent.py:316~327`) 추가.
- **직접 실행 결과**:
  1. `.venv/bin/python bench/intent_eval.py bench/intent_set_ko.tsv`:
     - 명령 재현율: **128/128 (100.0%)** [기준: ≥ 95%] → **PASS**
     - 위기 표현 명령 오판: **0건** (`active/idle/speaking/confirming/None` 5개 상태 전수 검사) [기준: 0건] → **PASS**
     - 비명령 대화문 오판 (총 100문장 = `intent_set_ko.tsv` 60 + `sentences_ko.txt` 40):
       - 되돌릴 수 없는/법적의무 명령 오판: **0건** [기준: 0건] → **PASS**
       - 기타 명령 오판: **0건 (0.0%)** [기준: ≤ 2%] → **PASS**
     - 분류 지연 시간 (N=4960): **P50 0.0028 ms, P90 0.0036 ms, P99 0.0058 ms** [기준: ≤ 5.0ms] → **PASS**
     - 최종 출력: `>>> C2 판정 결과: ALL PASS (모든 합격 기준 충족) <<<`
  2. `pytest tests/test_intent.py` 직접 실행:
     - `.venv/bin/python -m pytest tests/test_intent.py` → **9 passed**
     - ⚠️ **단독 `.venv/bin/pytest` 실행 시 경로 주의점 발견**: `python -m` 없이 `.venv/bin/pytest tests/test_intent.py`를 직접 실행하면 `pyproject.toml`의 `pythonpath = ["src"]`에 저장소 루트(`.`)가 없어 `tests/test_intent.py:276`의 `from bench.intent_eval import run_evaluation`에서 `ModuleNotFoundError: No module named 'bench'`로 **1 failed, 8 passed**가 나온다. `tests/test_intent.py` 상단에 `sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` 1줄을 넣으면 두 방식 모두 9/9 통과한다.
- **C2 재판정**: **✅ C2 합격 기준 전 항목 여전히 충족 (커밋 가능 — 단, `tests/test_intent.py`에 `sys.path` 1줄 추가 권장)**

---

### 14-2. (2) C3 diff 및 통합 시험 10개(`tests/test_session_commands.py`) 검토

- **전체 `pytest` 직접 실행 결과 (마지막 줄)**:
  - `.venv/bin/python -m pytest -q`:
    ```text
    289 passed, 5 warnings in 136.36s (0:02:16)
    ```
  - `.venv/bin/pytest -q` (`python -m` 미사용 시, 위 `test_intent.py:276` `bench` import 1건만 실패):
    ```text
    1 failed, 288 passed, 5 warnings in 136.00s (0:02:16)
    ```

#### A. `tests/test_barge_probe.py`에 `intent=False`를 넣은 것이 기존 끼어들기 시험을 약화시키는지 판정

- **왜 `code`가 `intent=False`를 넣었는가?**:
  - `tests/test_barge_probe.py:90`의 `test_real_interruption_after_short_utterance_stops_answer`는 말하는 중 짧은 비맞장구 끼어들기 예시로 **`"잠깐만요"`**를 보내고 `assert "interrupt" in t`와 **`assert "transcript" in t` (`# 끼어든 말이 새 턴으로 이어진다`)**를 검사한다.
  - C3에서 `intent=True`(기본값)가 켜지면 `"잠깐만요"`는 `speaking` 상태에서 **`INTENT_STOP_SPEAKING` 정지 명령**(`intent.py:147`)으로 분류된다. `webapp_plan.md` §3.1·§3.3-5 규칙상 정지 명령은 **현재 답만 끊고(`interrupt`) 새 턴을 만들지 않으므로(`transcript` 미전송)** 기존 단언문 `assert "transcript" in t`가 실패한다.
- **약화 여부 판정**: **❌ 기존 끼어들기 시험을 약화시킨다 (수정 필요).**
  - `make_client()`(`test_barge_probe.py:23~26`)에 `intent=False`를 일괄 주입하면 `test_barge_probe.py`의 5개 시험 전체가 실제 운영 기본 경로(`intent=True`)의 `_probe_classify` 의도 분기(`session.py:615~632`)를 통째로 우회하게 된다.
  - **해결 방법**: `test_barge_probe.py`의 `make_client()`에서 `intent=False`를 제거해 기본값(`intent=True`)을 유지하고, `test_real_interruption_after_short_utterance_stops_answer`(`test_barge_probe.py:91`)의 모의 전사 문구만 정지 명령이 아닌 짧은 일반 끼어들기 문구(예: **`"저기요"`** 또는 **`"질문 있어요"`**)로 바꾼다. 그러면 운영 기본 설정(`intent=True`)에서 ① 맞장구(`"네"`) 무시, ② 정지 명령(`"멈춰"`/`"잠깐만요"`, `test_session_commands.py` ②) 정지 후 새 턴 없음, ③ 일반 짧은 끼어들기(`"저기요"`) 정지 후 새 턴 생성이 모두 검증된다.

#### B. `webapp_plan.md` §5 C3 통합 시험 9개 vs `tests/test_session_commands.py`(10개) 1:1 대조표

| # | `webapp_plan.md` §5 C3 합격 기준 | 대응 시험 (`tests/test_session_commands.py`) | 실제 동작 검증 여부 상세 분석 | 판정 |
|---|---|---|---|---|
| ① | 명령 턴 LLM 호출 0회 | `test_command_turn_llm_zero_calls` (108~133행) | `"개발 정보 보여 줘"` 전송 시 `llm.call_count` 불변(0회), `DEV_INFO_ON`/`immediate` 이벤트 및 `config.show_dev_info=True` 수신을 실제로 확인함 | ✅ |
| ② | 말하는 중 "멈춰" → `interrupt` 1회·새 턴 0 | `test_speaking_stop_command_interrupts_and_zero_new_turn` (137~172행) | `audio_begin` 수신 후 `_probe_classify`로 `"멈춰"` 주입 시 새 `_task`가 생기지 않고 `barge_in(result="stop_command")`가 오는 것은 확인하나, 루프에서 읽은 메시지들 중 **`type == "interrupt"`가 정확히 1회 왔는지와 새 `transcript`가 0회인지** 단언하지 않음 | ⚠️ 단언 추가 필요 |
| ③ | 확인 "아니요" → 세션 유지 | `test_confirm_no_maintains_session` (176~208행) | `"대화 끝낼게요"`로 `confirming` 진입 후 `"아니요"` 전송 시 `active` 복귀 및 LLM 호출 0회를 실제로 확인함 | ✅ |
| ④ | "다시 말해 줘" → 직전 답 원문 재생·기록 오염 없음 | `test_repeat_bot_replays_last_bot_text_without_history_pollution` (212~241행) | 1턴 응답 후 `"다시 말해 줘"` 전송 시 LLM 재호출 0회, `audio_begin` 발생, 3턴 `llm.last_messages`에 `"다시 말해 줘"` 미포함을 확인함 (`turn_done.spoken_text == "챗봇 기본 답변입니다."` 단언 1줄 추가 시 원문 일치까지 완전 검증) | ✅ |
| ⑤ | 위기 표현 → LLM 경로 | `test_crisis_expression_goes_to_llm` (245~267행) | `"다 끝내고 싶어"` 전송 시 `rule == "crisis_blocked"`, `intent == "TALK"`, `llm.call_count` +1 증가를 실제로 확인함 | ✅ |
| ⑥ | 명령 후보 전사로 선행 생성 0회 | `test_speculation_skipped_for_command_candidates` (271~300행) | `active`에서 `"개발 모드로 바꿔"`·`"그만할게요"`, `idle`에서 `"시작하자"`로 `_on_speculate` 호출 시 `_spec is None` 유지, 일반 대화문에서는 `_spec is not None` 생성을 실제로 확인함 | ✅ |
| ⑦ | 확인 대기 중 "네"를 맞장구로 버리지 않음 | `test_confirming_state_preserves_short_yes_from_backchannel` (304~329행) | **❌ 시험 경로 오류**: `session.py:605~612`에는 `_probe_classify` 안에 `if self.lifecycle.is_confirming:` 가드가 올바르게 구현됐으나, 시험(321행)은 확인 질문 재생이 끝난 뒤 **`text_input` JSON**으로 `"네"`를 보냈다. `text_input`은 원래 `is_backchannel()`(`_probe_classify`)을 거치지 않으므로 `session.py:605~612`를 지워도 시험이 통과한다. `confirming` 상태에서 `session_obj._probe_classify(b"\x00" * 640, time.monotonic())`를 `MockSTT("네")`로 호출해 검증해야 한다 | ❌ 수정 필수 |
| ⑧ | 음성으로 개별 기능("개발 정보 보여 줘", "대기 청취 꺼 줘") 즉시 전환 및 "AI 고지 숨겨 줘" 확인 대기 진입 | `test_voice_individual_feature_toggles` (333~363행) | `"개발 정보 보여 줘"`→`show_dev_info: True`, `"대기 청취 꺼 줘"`→`wake: False`, `"AI 고지 숨겨 줘"`→`confirming` 진입 후 `"네"` 응답 시 `show_ai_notice: False` 전송을 실제로 확인함 | ✅ |
| ⑨ | mock에서 정지 명령 STT 완료 → `interrupt` 전송 ≤ 50ms | `test_mock_stop_command_stt_to_interrupt_latency` (367~385행) | **❌ 시험 경로 오류**: 턴을 시작하지 않은 유휴 상태(`sink.active == False`, `running == False`)에서 곧바로 `_probe_classify()`를 호출해 `_stop_current()` 안의 `pipeline.barge_in()`(`sink.interrupt`)이 **실제로는 호출되지 않았다**. 시험 ②처럼 발화를 시작해 `audio_begin`을 받은 뒤(`sink.active == True`) `_probe_classify()`를 호출하고, 웹소켓에서 `type == "interrupt"` 수신 및 `int_latency_ms <= 50.0`을 함께 단언해야 한다 | ❌ 수정 필수 |
| (보조) | `--no-intent` 인자 동작 | `test_no_intent_flag` (388~416행) | `intent=False`에서 `"개발 정보 보여 줘"`가 명령으로 빠지지 않고 LLM을 호출(`call_count == 1`)함을 확인함 | ✅ |

---

### 14-3. 커밋 가능 여부 판정 및 C2·C3 커밋 파일 목록 분리

1. **C2 판정**: **✅ 커밋 가능** (`tests/test_intent.py` 상단에 `import sys; sys.path.insert(0, str(Path(__file__).resolve().parents[1]))` 1줄 추가 후 즉시 커밋)
   - **C2 커밋 파일 목록 (4개)**:
     - `src/voice_pipeline/intent.py`
     - `bench/intent_eval.py`
     - `bench/intent_set_ko.tsv`
     - `tests/test_intent.py`
   - **C2 커밋 명령**:
     ```bash
     git add src/voice_pipeline/intent.py bench/intent_eval.py bench/intent_set_ko.tsv tests/test_intent.py
     git commit -m "webapp C2: 한국어 규칙 기반 음성 명령 의도 분류기 및 평가 세트 추가"
     ```

2. **C3 판정**: **⚠️ 시험 파일 2개(`test_barge_probe.py`, `test_session_commands.py`) 보완 후 커밋 가능**
   - 서버·프론트엔드 구현(`session.py`, `app.js`, `app.py`, `engines.py`, `__main__.py`)은 C3 요구사항을 정확히 구현했으나, 커밋 전 아래 3건을 고쳐야 한다:
     1. `tests/test_barge_probe.py`: `make_client()`에서 `intent=False`를 제거하고, `test_real_interruption_after_short_utterance_stops_answer`(91행)의 `make_client("잠깐만요")`를 비명령 끼어들기 문구 `make_client("저기요")`로 변경.
     2. `tests/test_session_commands.py` ⑦ (`test_confirming_state_preserves_short_yes_from_backchannel`): `"대화 끝낼게요"`로 `confirming` 상태가 된 뒤 `text_input` 대신 `session_obj.engines._stt_cache[session_obj.stt_id] = MockSTT("네")` 설정 후 `await session_obj._probe_classify(b"\x00" * 640, time.monotonic())`를 호출해 `_probe_classify`의 `is_confirming` 우선 분기를 실제로 타는지 검증.
     3. `tests/test_session_commands.py` ②·⑨: 시험 ⑨에서 먼저 일반 발화를 보내 `audio_begin`까지 대기(`sink.active == True`)한 뒤 `_probe_classify`를 실행하여 실제로 `{"type": "interrupt"}` 메시지가 전송되는지와 `elapsed_ms <= 50.0`을 검증하고, 시험 ②에서도 수신 메시지 목록에 `type == "interrupt"`가 정확히 1개 포함되어 있는지 단언 추가.
   - **C3 커밋 파일 목록 (7개)**:
     - `src/voice_pipeline/web/session.py`
     - `src/voice_pipeline/web/static/app.js`
     - `src/voice_pipeline/web/app.py`
     - `src/voice_pipeline/web/engines.py`
     - `src/voice_pipeline/web/__main__.py`
     - `tests/test_session_commands.py` (신규 — ②·⑦·⑨ 보완 후)
     - `tests/test_barge_probe.py` (`intent=False` 제거 및 `"저기요"` 교체 후)
   - **C3 커밋 명령**:
     ```bash
     git add src/voice_pipeline/web/session.py src/voice_pipeline/web/static/app.js src/voice_pipeline/web/app.py src/voice_pipeline/web/engines.py src/voice_pipeline/web/__main__.py tests/test_session_commands.py tests/test_barge_probe.py
     git commit -m "webapp C3: 세션 의도 분류기 연결, 확인 흐름, 개별 기능 음성 토글 및 통합 시험 추가"
     ```

---

## 15. F1 검토 (2026-10-23)

- **검토 대상**: 미커밋 신규 파일 2개 — `src/voice_pipeline/web/static/face-probe.html` (445줄), `src/voice_pipeline/web/static/face-probe.js` (816줄)
- **최종 판정**: **⚠️ 수정필요 (동의 전 `getUserMedia` 0회·영상 프레임 서버 전송 0·후보 3개 및 지표 5종 구성은 합격이나, 모델 파일이 없을 때의 "모의 벤치마크"가 인위적 차등 지연과 녹색 `PASS (우수)` 및 하드코딩된 1순위 추천 결론을 출력해 실측과 혼동될 위험이 있어 아래 §15-2 수정 후 커밋)**

### 15-1. `webapp_plan.md` §4.3·§4.4·§4.8·§5 F1 항목별 대조표

| 검증 항목 | `face-probe.html` / `face-probe.js` 확인 결과 | 판정 |
|---|---|---|
| **동의 버튼 전 `getUserMedia` 0회** (§4.2-1, §4.4-4) | 페이지 로드 및 초기화 시 미디어 API 호출이 전혀 없으며, 오직 `btnConsentOn`(`[✅ 동의하고 카메라 켜기]`) 클릭 리스너(`face-probe.js:205~219`) 안에서만 `navigator.mediaDevices.getUserMedia({video: ..., audio: false})`를 호출함. 동의 전 벤치마크 버튼 클릭 시 경고 후 차단(`623~626행`, `649~652행`) | ✅ |
| **영상 프레임 서버 전송 0 바이트 & 외부 CDN 0회** (§4.1, §4.3, §4.4-1) | `WebSocket`, `XMLHttpRequest`, `sendBeacon`, `POST` 전송 코드가 0건이며 외부 CDN `<script>` 태그도 없음(`face-probe.html:443`은 로컬 `/static/face-probe.js`만 로드). 유일한 `fetch`는 로컬 정적 경로(`/static/models/...`) 존재 여부를 확인하는 `HEAD` 요청(`face-probe.js:367`)뿐이며, 비디오 프레임은 브라우저 메모리 내 `offscreenCanvas`(`448행`)에서만 처리 후 폐기됨 | ✅ |
| **§4.3 후보 3개 및 필수 계측 지표 5종** (`로드 시간`, `추론 P50·P90`, `실제 분석 주기`, `언더런 수`) | 후보 3종(`c1: MediaPipe Face Landmarker`, `c2: Face Detector + FER+ ONNX`, `c3: face-api.js`) 모두 선택·일괄 측정 가능하며, 결과표(`face-probe.html:380~429`)와 보고서(`face-probe.js:745~788`)에 **로드 시간(`loadTimeMs`), 추론 P50·P90(`latStats.p50/p90`), 실제 분석 주기(`intStats.mean` 및 `FPS`), 오디오 언더런 수(`AudioUnderrunMonitor`, `nextTime < now - 0.005`)**가 모두 포함됨 | ✅ |
| **모델 파일 미배치 시 "모의 벤치마크" 표시 투명성** (모의 수치가 실측처럼 보이지 않는지) | 표 마지막 칸과 보고서 행에 `[모의]`/`(모의)` 표기는 들어가 있으나, **① 모의 실행에 녹색 `PASS (우수)` 판정 부여, ② 후보별 인위적 가짜 딜레이(`140/310/190ms`) 및 삼각함수 루프 차등(`22000/45000/30000`회)으로 후보 1이 이기도록 조작, ③ 실시간 감정 막대에 `Math.sin` 가짜 파형을 모의 표기 없이 출력, ④ 보고서 하단에 실측 여부와 무관하게 `"1순위 추천 후보: MediaPipe Face Landmarker"` 결론 하드코딩** 등 4건의 왜곡 위험이 있음 | ❌ 수정 필수 |

### 15-2. F1 커밋 전 필수 수정 사항 (4건, `face-probe.js`)

1. **모의 모드(`isReal === false`)에서 `PASS` 판정 금지 및 수치 셀에 `(모의)` 명시 (`face-probe.js:557~564, 599~615`)**:
   - 실제 모델이 로드되지 않았을 때(`!isReal`)는 `rating = "모의 측정 (모델 미배치)"`로 고정하고, 배지 색상도 녹색(`badge ok`)이 아닌 주황색(`badge dev`)으로 표시한다 (`PASS (우수)`는 `isReal === true`일 때만 부여).
   - 표의 `로드 시간`, `추론 P50`, `추론 P90` 칸에도 `!res.isReal`이면 `${res.loadTimeMs} ms (모의)`처럼 `(모의)`를 함께 적는다.
2. **후보 간 우열을 인위적으로 만드는 가짜 딜레이·차등 루프 제거 (`face-probe.js:419~421, 459~464`)**:
   - `load()`의 후보별 차등 `setTimeout(140 / 310 / 190)`과 `infer()`의 후보별 차등 루프(`22000 / 45000 / 30000`)는 실제 모델 없이도 항상 `c1 < c3 < c2` 순위가 나오게 만드는 인위적 수치이므로 제거하고, 모델 미배치 시에는 순수 캔버스 프레임 추출(`drawImage` + `getImageData`) 시간만 동일하게 재도록 바꾼다.
3. **실시간 감정 막대 및 내려받기 보고서(`generateReportText`)에 모의 경고 명시 (`face-probe.js:474~483, 730~797`)**:
   - `!isReal`일 때 `topEmotionLabel` 문자열 앞에 `[모의 파형] `을 붙여 실제 표정 인식이 아님을 화면에 즉시 알린다.
   - `generateReportText()`에서 하나라도 `!r.isReal`인 후보가 있으면 보고서 상단에 `⚠️ [주의] 실제 모델 파일(/static/models/) 미배치 상태에서 측정된 모의(Simulated) 수치입니다. F1 모델 선정 실측 근거로 사용할 수 없습니다.`를 출력하고, 하단의 `[종합 검토 및 추천안]`(791~795행)은 3종 모두 `isReal === true`로 실측된 경우에만 실측 수치 기반으로 출력한다.
4. **실제 모델 파일 배치 시 동작 완결성 보완 (`face-probe.js:380~413, 434~442`)**:
   - `infer()` 441행에 주석(`// ... (c2, c3 실제 추론)`)으로 비워 둔 `c2`(`this.modelInstance.run(...)`)·`c3`(`window.faceapi.detectSingleFace(...).withFaceExpressions()`) 실제 추론 호출을 채워, 추후 `/static/models/`와 `/static/vendor/`에 파일만 넣으면 코드 수정 없이 실측이 돌아가게 한다.

- **F1 커밋 대상 파일 목록 (총 2개 — 위 수정 완료 후 독립 커밋)**:
  1. `src/voice_pipeline/web/static/face-probe.html`
  2. `src/voice_pipeline/web/static/face-probe.js`
  ```bash
  git add src/voice_pipeline/web/static/face-probe.html src/voice_pipeline/web/static/face-probe.js
  git commit -m "webapp F1: 브라우저 로컬 표정 모델 후보 3종 성능·언더런 계측 페이지 추가"
  ```

---

### 15-3. C3·F1 커밋 직후 `code` 창에 전달할 다음 구현 항목 지시문 (작업 C4)

`webapp_plan.md` §5·§6 일정상 C1·C2·C3·F1 다음으로 사용자 입력(🎙️·📷) 없이 `code`가 바로 진행할 작업은 **C4(대기 청취 `--wake` 및 `config.wake` 연동)**이다. 아래 지시문을 그대로 `code` 창에 전달한다:

```text
[작업 지시: C2·C3·F1 마무리 커밋 후 webapp C4(대기 청취 --wake) 구현]

1. 선행 정리 및 커밋 (순서 엄수, git add에 파일 명시):
   (a) C2 커밋: tests/test_intent.py 상단에 저장소 루트 sys.path 1줄 추가 후
       git add src/voice_pipeline/intent.py bench/intent_eval.py bench/intent_set_ko.tsv tests/test_intent.py
       커밋.
   (b) C3 커밋: docs/plans/webapp_plan_review.md §14-3대로 tests/test_barge_probe.py(intent=False 제거, 91행 "저기요"로 변경)와 tests/test_session_commands.py(시험 ②·⑦·⑨ 실제 _probe_classify 및 interrupt 검증) 보완 후 C3 7개 파일만 커밋.
   (c) F1 커밋: docs/plans/webapp_plan_review.md §15-2대로 src/voice_pipeline/web/static/face-probe.js의 모의 측정 표시·판정 왜곡 4건 수정 후 face-probe.html, face-probe.js 2개 파일만 커밋.

2. 본 작업: webapp_plan.md §3.5 및 §5 C4 구현 (대기 청취 --wake 및 config.wake 연동)
   - 대상 파일:
     * src/voice_pipeline/web/__main__.py, src/voice_pipeline/web/engines.py
     * src/voice_pipeline/web/session.py
     * src/voice_pipeline/web/static/app.js, src/voice_pipeline/web/static/index.html (필요 시)
     * 신규 시험: tests/test_wake.py (및 bench/web_ui_check.py 확장)
   - 핵심 요구사항:
     ① 실행 인자 및 설정 연동:
       - web/__main__.py에 --wake (또는 기본 켜짐 + --no-wake) 인자와 Engines.wake 기본값을 연결하고, hello 메시지에 wake 상태를 포함하며, 클라이언트의 config.wake 토글(self.feature_prefs["wake"])로 런타임 on/off가 즉시 반영되게 한다.
     ② 대기 상태(lifecycle.is_idle) 마이크 게이트 및 길이 상한 (§3.5):
       - lifecycle.is_idle일 때 self.feature_prefs["wake"]가 False이면 마이크 오디오(SpeechEnd/on_pause)가 들어와도 STT를 절대 호출하지 않고(STT 호출 0회) 즉시 무시한다.
       - lifecycle.is_idle이고 wake가 True일 때는 짧은 발화만 STT에 넣는다: 실제 발화 길이(dur_ms = max(0.0, len(pcm) / 32.0 - PRE_ROLL_MS))가 WAKE_MAX_SPEECH_MS(초기값 2000ms)를 초과하면 STT를 호출하지 않고(STT 호출 0회) 바로 버린다.
       - 대기 상태에서는 선행 생성(_on_speculate)도 하지 않는다.
     ③ 대기 상태 의도 필터링 및 비명령 무기록 폐기 (통신비밀보호법 보호 장치):
       - 대기 상태에서 STT 전사 결과를 classify_intent(text, state="idle")로 분류한다.
       - SESSION_START(예: "시작하자", "안녕하세요")이면 세션을 시작(active 전이 + AI 고지 음성 재생)하고, 대기 상태에서 허용된 기타 명령(MODE_*, CAMERA_*, WAKE_OFF 등)이면 해당 명령 절차를 수행한다.
       - 일반 대화(TALK/비명령)로 분류된 전사는 즉시 버린다: LLM 호출 0회, 브라우저로 transcript 메시지 전송 0개, 개발 모드(mode == "dev")이더라도 JSONL 파일(dev_logger.write_turn)에 절대 기록하지 않고(0줄) 세션 내 무시 카운터(self.wake_ignored_count)만 +1 올린다.
     ④ 화면 표시 및 첫 터치 1회 유지 (app.js):
       - 첫 터치로 AudioContext 및 마이크를 시작한 뒤에는 세션이 종료되어 다시 대기(idle) 상태로 돌아가도 첫 터치 화면을 다시 요구하지 않고(페이지당 1회), 대기 중이고 wake가 켜져 있으면 화면에 "마이크 대기 중 ('시작하자'라고 말씀해 주세요)" 안내가 보이도록 유지한다.
   - 합격 기준 및 검증 (tests/test_wake.py 신규 작성):
     1) 대기 중 비명령 5발화 → LLM 호출 0회, transcript 메시지 0개, 개발 모드 JSONL 로그 0줄, wake_ignored_count == 5
     2) 대기 중 상한(2000ms) 초과 긴 발화 → STT transcribe 호출 0회
     3) 대기 중 "시작하자" 발화 → active 전이 + AI 고지 audio_begin 재생
     4) config.wake=false 전환 시 대기 중 발화 → STT transcribe 호출 0회
     5) 첫 터치 화면은 페이지당 1회만 표시 (세션 종료 후 idle 복귀 시 재표시 없음)
     - 완료 후 .venv/bin/python -m pytest tests/test_wake.py 와 전체 .venv/bin/python -m pytest 통과를 확인하라.
```











