# next_04_draft — CosyVoice 첫 요청 TTFA 단축 계획 (711ms → 500ms 구간 분석 및 개선)

> 결론: 같은 짧은 한국어 문장(8~11자)이라도 직전 작업이 음성 합성 (TTS)이면 서버 첫 오디오 합성 시간 (TTFA)이 백분위수 50 (P50) 약 500ms인데, 거대 언어 모델 (LLM) 생성 직후나 유휴 뒤 첫 요청에서는 약 711ms로 약 200ms 느리다 (`docs/plans/next_03_review.md` §1-B-1). 이 200ms의 원인은 아직 확인하지 않았으며, 녹음·청취·태블릿 없이 서버에서 바로 측정·단축할 수 있다.
> 지금 할 일: 1단계로 `services/cosyvoice/server.py`에 구간 분해 타이밍 로그를 넣어 200ms 차이의 원인을 실측으로 특정한 뒤, 2단계로 `time.sleep(0.1)` 폴링 주기 축소(또는 TTS 예열)를 적용해 검증한다.
>
> - 상태: ✅ `next_03` 취소 잠금 단축 완료 / 🔄 첫 요청 TTFA 단축 계획(`next_04_draft.md`) / ⏭️ 원인 분해 계측 및 패치 실측
> - 전제: 2026-11-27 데모 마감, 산업용 태블릿 브라우저 클라이언트(마이크·재생)·서버 연산 구조 (`docs/PLAN.md` §0)

---

## 1. 배경 및 관측된 사실 (`next_03_review.md` §1-B)

같은 길이(8~11자)의 짧은 첫 청크 문장에서 직전 그래픽 처리 장치 (GPU) 상태에 따라 서버 첫 청크 시각이 약 100ms 단위로 갈린다:

| 조건 | 관측된 서버 첫 청크 P50 | 출처 |
|---|---|---|
| 직전 요청이 TTS인 경우 (Warm TTS: 취소 직후 재요청, 연속 2청크) | **492 ~ 520ms** | `cosyvoice_cancel_lock_v3.txt`, `next_01_1b_yield_v3.txt` 2청크 |
| 직전 작업이 vLLM 생성인 경우 (`first_audio_bench.py`의 `idle`·양보 1청크) | **709 ~ 712ms** (일부 턴 **608 ~ 614ms**) | `next_01_1b_yield_v3.txt` 1청크 |
| vLLM이 동시에 생성 중인 경우 (`concurrent` 1청크) | **997 ~ 1000ms** (일부 턴 **607ms**, **1072ms**) | `next_01_1b_yield_v3.txt` 1청크 |

- 값들이 `~500ms`, `~610ms`, `~710ms`, `~1000ms`처럼 **약 100ms 간격으로 양자화**되어 있다.
- 컨테이너 안 `/opt/CosyVoice/cosyvoice/cli/model.py` 338행의 스트리밍 루프가 `time.sleep(0.1)`(100ms) 주기로 토큰 개수를 확인하기 때문이다.

---

## 2. 원인 가설 (4가지, 실측 전까지 확인하지 않았다)

1. **가설 1 — `CosyVoice2Model.tts()`의 `time.sleep(0.1)` 폴링 양자화 대기 (주원인 후보)**:
   - `model.tts`는 백그라운드 `llm_job` 스레드가 첫 홉 토큰(`this_token_hop_len + pre_lookahead_len`, `CV_HOP=10` 기준 약 13~16토큰)을 채울 때까지 `time.sleep(0.1)`로 100ms마다 깨어나 확인한다.
   - 만약 토큰 생성이 200ms 직전(예: 190ms)에 끝나면 200ms에 바로 `token2wav`(약 300ms)가 돌아 ~500ms에 첫 소리가 나오지만, 200ms를 조금만 넘기면(예: 210ms) 다음 폴링 주기인 300ms·400ms까지 GPU가 빈 채로 대기해 610ms·710ms로 +100~200ms 밀린다.
2. **가설 2 — vLLM(27B) 실행 직후 GPU 컨텍스트·캐시 전환 지연 + 100ms 계단 증폭**:
   - 직전 GPU 작업이 vLLM(Qwen3.8-27B)이면 CosyVoice3(0.5B LLM + `CUDAGraph`)의 첫 프리필 또는 초반 토큰 디코드가 L2 캐시 미스나 컨텍스트 전환으로 20~80ms 느려지고, 이 지연이 가설 1의 100ms 폴링 경계를 1~2칸 넘기며 200ms 차이로 증폭된다 (**확인하지 않았다**).
3. **가설 3 — 수 초 이상 GPU 유휴 후 클럭(P-state) 저하**:
   - 서버가 잠시 쉬고 난 뒤 첫 요청에서 GPU 클럭이 올라오는 데 수십 ms가 더 걸려 폴링 경계를 넘긴다 (**확인하지 않았다**).
4. **가설 4 — 문장별 생성 토큰 수와 첫 홉 임계값의 관계**:
   - `"네, 알겠습니다."`(8자)와 `"많이 힘드셨겠어요."`(10자)의 음성 토큰 수 차이, 또는 전체 토큰 수가 첫 홉 임계값보다 적을 때 `llm_end_dict[uuid] = True` 확인까지 추가 100ms 주기를 기다리는 현상이다 (**확인하지 않았다**).

---

## 3. 해결 선택지 비교 (3개 중 추천 1개)

1. **(추천) ⏭️ 선택지 A — `model.tts` 폴링 주기를 100ms → 10ms(또는 이벤트 신호)로 축소 (`services/cosyvoice/server.py`)**:
   - 바인드 마운트된 `server.py`에서 `cosyvoice.cli.model`의 `time.sleep(0.1)` 대기를 10ms(`0.01`초) 또는 토큰 생성 이벤트 대기로 바꿔, 토큰이 모이는 즉시(≤ 10ms) `token2wav`를 시작한다.
   - 도커 (Docker) 이미지 재빌드가 필요 없고 GPU 추가 부하가 없다.
2. **⏭️ 선택지 B — 턴 확정(또는 맞장구 시작) 시점에 CosyVoice 경량 예열 호출**:
   - 가설 2·3(GPU 클럭·캐시 전환)이 주원인일 때만 유효하며, LLM 첫 토큰 생성과 GPU가 겹치면 오히려 첫 청크 시각이 늦어질 위험이 있다.
3. **⏭️ 선택지 C — `CV_HOP`을 첫 홉에만 10 → 8로 축소**:
   - `CV_HOP=6`에서 문자 오류율 (CER)과 실시간 계수 (RTF)가 나빠졌으므로(`docs/PLAN.md` §2 #3) 음질 위험이 있다.

---

## 4. 변경 파일 (2단계 분리)

1. **[1단계: 원인 분해 계측] `bench/tts_ttfa_breakdown.py` (신규) 및 `services/cosyvoice/server.py` (`CV_PROF` 확장)**:
   - `server.py`의 `CV_PROF` 모드에 ① `llm_first_tok_ms`(프리필), ② `hop1_tokens_ready_ms`(첫 홉 토큰 충족 시각), ③ `poll_wait_ms`(`token2wav` 진입까지 폴링 대기 시간), ④ `token2wav_ms`(Flow + HiFT 시간)를 기록한다.
   - 같은 문장 2종(`"네, 알겠습니다."`, `"많이 힘드셨겠어요."`)을 (A) TTS 직후, (B) 3초 완전 유휴 뒤, (C) vLLM 생성 직후 조건으로 각 10회씩 측정해 `bench/results/next_04_breakdown.txt`에 저장한다.
2. **[2단계: 패치 및 벤치 검증] `services/cosyvoice/server.py`**:
   - 1단계에서 `poll_wait_ms` 또는 계단 현상이 확인되면, `CV_POLL_MS`(기본 `10`, 기존 동작은 `100`) 환경변수로 폴링 주기를 조절하는 패치를 넣는다.
3. **[2단계] `bench/first_audio_bench.py`**:
   - `CV_POLL_MS=100`(현재)과 `CV_POLL_MS=10`(수정)에서 `idle` 및 `concurrent` 모드의 첫 소리 지연(n=20)을 비교한다.

---

## 5. 검증 방법 (절차)

> ⚠️ 경고: 측정 전에 `free -g`로 가용 메모리를 확인하고, `healthup-llm`(8000)과 `healthup-cosyvoice`(8091) 외 다른 벤치가 돌고 있지 않은지 확인한다.

1. 1단계 분해 계측 스크립트(`bench/tts_ttfa_breakdown.py`)로 조건 (A) TTS 직후, (B) 3초 유휴 뒤, (C) vLLM 직후의 구간별 시간을 측정해 `bench/results/next_04_breakdown.txt`에 기록한다.
2. 분해표에서 `500ms`와 `711ms`의 차이(약 200ms)가 `poll_wait_ms`(폴링 대기), `hop1_tokens_ready_ms`(LLM 토큰 생성), `token2wav_ms` 중 어디서 생기는지 확인한다.
3. `services/cosyvoice/server.py`에 폴링 주기 축소(`CV_POLL_MS=10`)를 적용하고 `services/cosyvoice/run.sh`로 컨테이너를 재시작한다.
4. 같은 분해 계측 스크립트를 다시 돌려 조건 (A)·(B)·(C)의 서버 첫 청크 P50·백분위수 90 (P90) 변화를 확인한다.
5. `.venv/bin/python bench/first_audio_bench.py --first-min 2 -n 20 --prompt fixed --chunker fixed`로 실제 LLM 동시 실행(`concurrent`) 및 유휴(`idle`) 첫 오디오 합계 P50·P90을 측정한다.
6. `.venv/bin/python bench/tts_bench.py --engine cosyvoice3 --save-wav bench/results/poll10_check`와 `tts_roundtrip.py`로 40문장 왕복 CER과 RTF를 확인한다.
7. 결과를 `Claude/docs/STATE.md`, `docs/STATE.md` §7, `docs/PLAN.md` §2에 기록한다.

---

## 6. 합격 기준 (수치) 및 중단 조건

- **합격 기준 (모두 만족해야 기본값 반영)**:
  1. **유휴·vLLM 직후 첫 청크 TTFA (`idle`, n=20)**: 서버 첫 청크 P50 ≤ **580ms** (기존 712ms 대비 **≥ 130ms 단축**).
  2. **LLM 동시 실행 첫 소리 (`concurrent`, n=20)**: TTS 첫 소리 P50이 기존(999~1001ms) 대비 **≥ 80ms 단축**(≤ **920ms**)되거나 합계(`chunk + tts`) P50이 **≤ 1410ms**로 단축.
  3. **품질·성능 무결성 (40문장)**: 왕복 CER P50 ≤ **0.14**(악화 없음), 단독 RTF P90 ≤ **0.45**(악화 없음), `.venv/bin/python -m pytest` 전체 통과.
- **중단 조건**:
  - 1단계 분해 계측에서 `poll_wait_ms`가 30ms 미만이고 200ms 차이가 순수 GPU 하드웨어 전력 상태 전환에서만 발생해 소프트웨어 패치로 줄지 않거나,
  - 폴링 주기를 10ms로 줄였을 때 파이썬 전역 인터프리터 잠금 (GIL) 경합으로 `llm_job` 토큰 생성 속도가 느려져 동시 실행(`concurrent`) TTFA가 개선되지 않으면 변경을 되돌리고 중단한다.

---

## 7. 위험

1. **파이썬 GIL 경합 위험**: 폴링 주기를 너무 짧게(예: 1ms 이하) 잡으면 메인 합성 스레드가 `tts_speech_token_dict` 길이를 확인하느라 GIL을 자주 잡아 백그라운드 `llm_job` 스레드의 CUDA 그래프 (`CUDAGraph`) 재생을 방해할 수 있다 (10ms 주기로 제한하거나 `threading.Event` 통지 방식을 쓴다).
2. **`token_offset == 0` 조기 진입 여부**: 폴링 주기를 줄여도 첫 홉에 필요한 최소 토큰 수(`this_token_hop_len + pre_lookahead_len`)는 그대로 유지해야 첫 청크 음질(CER)이 깨지지 않는다.
3. **LLM 동시 실행 시 이득 축소 가능성**: `concurrent` 모드에서는 vLLM과 GPU를 나누므로 토큰 생성 속도 자체가 느려, 폴링 대기를 줄여도 단축 폭이 유휴(약 100~200ms)보다 작을 수 있다 (**확인하지 않았다**).
4. **운영 PC(RTX 4090 ×2) 재확인**: 개발 장비(단일 GB10) 수치는 최종 수치가 아니므로(`CLAUDE.md` §4), 5주차 운영 PC 이식 시에도 동일하게 측정한다.
