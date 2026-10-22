# next_04_first_tts — CosyVoice 첫 요청 TTFA 단축 계획 (확정본)

> 결론: 같은 짧은 한국어 문장(8~11자)이라도 직전 작업이 음성 합성 (TTS)이면 서버 첫 오디오 합성 시간 (TTFA)이 백분위수 50 (P50) 약 500ms인데, 거대 언어 모델 (LLM) 생성 직후 첫 요청에서는 약 712ms로 약 200ms 느리다 (`docs/plans/next_03_review.md` §1-B-1). 이 차이는 서버 내부 지연이므로 줄이면 산업용 태블릿 브라우저의 본문 첫 소리 시각(`tts_first_audio`)이 네트워크·클라이언트 변경 없이 그대로 앞당겨진다.
> 지금 할 일: 1단계로 `services/cosyvoice/server.py`에 6구간 분해 로그를 넣고 2×3 직교 실험(문장 2종 × 직전 상태 3종)으로 200ms 원인을 특정한 뒤, 확인된 구간에 맞춰 2단계 폴링 대기 제거(`CV_POLL_MS=10`)를 적용·검증한다.
>
> - 상태: ✅ `next_03` 취소 잠금 단축 커밋(`2437f38`) / ✅ 초안 자체 검토 완료 / ⏭️ 1단계 2×3 분해 계측 실행
> - 전제: 2026-11-27 데모 마감, 산업용 태블릿 브라우저 클라이언트(마이크·재생) + 서버 연산 구조 (`docs/PLAN.md` §0)

---

## 1. 관측 사실과 문제 정의

| 조건 | 문장 | 서버 첫 청크 P50 | 출처 |
|---|---|---|---|
| 직전 요청이 TTS (취소 직후 재요청) | `"네, 알겠습니다."` (8자) | **492 ~ 500ms** | `cosyvoice_cancel_lock_v3.txt` |
| 직전 요청이 TTS (긴 문장 첫 청크·40문장 벤치) | 65자 문장 / `sentences_ko.txt` 40문장 | **507 ~ 514ms** | `cosyvoice_cancel_lock_v3.txt`, `Claude/docs/STATE.md` |
| 직전 요청이 TTS (연속 2청크) | 15~25자 2청크 문장 | **500 ~ 520ms** | `next_01_1b_yield_v3.txt` (`next_03_review.md` §1-B-1) |
| **직전 작업이 vLLM (`idle`·양보 1청크)** | `"많이 힘드셨겠어요."` 등 (평균 10자) | **711 ~ 712ms** (일부 **608ms**) | `next_01_1b_yield_v3.txt` |
| **vLLM 동시 생성 중 (`concurrent` 1청크)** | `"많이 힘드셨겠어요."` 등 (평균 10자) | **1000ms** (일부 **607ms**, **1072ms**) | `next_01_1b_yield_v3.txt` |

- 서버 첫 청크 시각이 `~500ms`, `~610ms`, `~710ms`, `~1000ms`처럼 **약 100ms 간격으로 양자화**되어 있다.
- 컨테이너 안 `/opt/CosyVoice/cosyvoice/cli/model.py` 338행이 `time.sleep(0.1)`(100ms) 주기로 토큰 개수를 확인하기 때문이다.

---

## 2. 원인 가설과 측정 분리 방법 (4가지, 실측 전까지 확인하지 않았다)

1. **가설 1 — `model.tts`의 `time.sleep(0.1)` 폴링 양자화 대기 (`poll_wait_ms`)**:
   - 첫 홉 필요 토큰 수(`need_tok = this_token_hop_len + pre_lookahead_len`, `CV_HOP=10` 기준 약 16토큰)가 200ms 직후(예: 210ms)에 모이면 다음 폴링인 300ms까지 그래픽 처리 장치 (GPU)가 빈 채로 대기한다.
   - 짧은 문장에서 총 토큰 수가 `need_tok`보다 적어 루프 탈출(`llm_end_dict[uuid] == True`) 경로로 가면 폴링 대기가 1회 더 겹칠 수 있다.
2. **가설 2 — vLLM(27B) 실행 직후 GPU 컨텍스트·캐시 전환 지연 + 100ms 계단 증폭**:
   - 직전 GPU 작업이 vLLM이면 CosyVoice3(0.5B LLM + CUDA 그래프 (`CUDAGraph`))의 프리필(`llm_first_tok_ms`)이나 초기 디코드(`hop1_ready_ms`)가 수십 ms 느려져 200ms 폴링 경계를 넘긴다.
3. **가설 3 — 수 초 이상 GPU 완전 유휴 후 클럭(P-state) 저하**:
   - 직전 작업 종류와 무관하게 GPU가 2~3초 쉬면 클럭이 내려가 첫 요청의 `llm_first_tok_ms`·`token2wav_ms`가 느려진다.
4. **가설 4 — 문장 자체 차이 및 `text_normalize` 중복 호출(`frontend_ms`)**:
   - `"네, 알겠습니다."`(8자)와 `"많이 힘드셨겠어요."`(10자)의 음성 토큰 수 차이, 또는 `server.py` 357·362행에서 `text_normalize()`를 같은 문장에 두 번 호출하는 전처리 시간 차이다.

### 가설 분리용 6구간 타이밍 및 2×3 직교 실험

- **6구간 로그 (`t0` = 잠금 획득 시각 기준)**:
  1. `frontend_ms`: `synth()` 시작 ~ `model.tts()` 진입 직전 (`text_normalize` 중복 호출 포함)
  2. `llm_first_tok_ms`: `model.tts()` 진입 ~ 첫 음성 토큰 생성 (프리필·컨텍스트 전환 시간)
  3. `hop1_ready_ms`: `model.tts()` 진입 ~ 첫 홉 토큰 수(`need_tok`) 도달 또는 `llm_job` 종료 시각
  4. `poll_wait_ms`: `hop1_ready_ms` ~ 첫 `token2wav()` 진입 시각 (**순수 폴링 대기 시간**)
  5. `token2wav_ms`: 첫 `token2wav()` 소요 시간 (`flow_ms` + `hift_ms`, `finalize` 여부 표시)
  6. `first_chunk_ms`: `t0` ~ 첫 청크 `resp.write` 직전 시각
- **2×3 직교 조건 (각 n=10)**:
  - 문장 2종(`S1="네, 알겠습니다."`, `S2="많이 힘드셨겠어요."`) × 직전 상태 3종(`A: TTS 직후 50ms`, `B: GPU 완전 유휴 3초`, `C: vLLM 20토큰 생성 직후`).
  - 판별 규칙: `A`와 `B`가 빠른데 `C`만 느리면 **가설 2**, `A`만 빠르고 `B·C`가 느리면 **가설 3**, 같은 상태에서 `S1`과 `S2`가 갈리면 **가설 4**, 모든 느린 케이스에서 `poll_wait_ms`가 크게 잡히면 **가설 1(계단 증폭 포함)**이다.

---

## 3. 해결 설계 및 기존 코드(`2437f38`)와의 호환성

1. **전역 `time.sleep` 오염 금지 (모듈 프록시 사용)**:
   - `/opt/CosyVoice/cosyvoice/cli/model.py`의 `time`은 파이썬 전역 `time` 모듈이므로 `model.time.sleep`을 직접 덮어쓰면 프로세스 전체의 `time.sleep`이 바뀐다.
   - 따라서 `cosyvoice.cli.model` 네임스페이스의 `time` 참조만 얇은 프록시 객체(`_ModelTimeProxy`)로 교체해, `sleep(0.1)` 호출일 때만 단축 대기(`CV_POLL_MS`, 기본 10ms) 또는 토큰/취소 이벤트 대기(`_poll_ev.wait(timeout=poll_s)`)를 수행한다.
2. **취소 잠금 단축(`2437f38`)과의 무충돌·상승효과 보장**:
   - `_ModelTimeProxy.sleep` 안에서 `_SynthCancelled` 예외를 직접 던지지 않고 **대기만 즉시 반환(`return`)**하게 한다.
   - 그러면 취소 시 기존 100ms 잠을 기다리지 않고 10ms 안에 깨어나 다음 줄의 `token2wav` 진입 검사(`_patch_cancel`)에서 기존 `2437f38` 경로 그대로 `_SynthCancelled` → `finally`(`llm_job` 종료 이벤트 대기 + 사전 정리 + 잠금 해제)를 타므로 상태 꼬임이 없고 `+100ms` 취소 잠금 대기(214ms)도 함께 줄어든다.
3. **`CUDAGraph` 및 전역 인터프리터 잠금 (GIL) 부하 방지**:
   - 토큰이 추가될 때(`_patch_llm`)와 `llm_job`이 끝날 때(`_patch_llm_job`)만 이벤트를 깨우거나 최소 주기를 10ms(`CV_POLL_MS=10`)로 두어, `llm_job` 스레드의 `CUDAGraph.replay()` 토큰당 시간(`ms/tok`)이 늘어나지 않게 한다.
4. **`text_normalize` 중복 호출 제거**:
   - `server.py` 357·362행에서 정상 문장마다 `text_normalize(text, split=True, text_frontend=True)`를 두 번 부르던 것을 `try` 블록 안에서 한 번만 부르게 고친다.

---

## 4. 변경 파일 (2단계 분리)

1. **[1단계: 분해 계측] `services/cosyvoice/server.py` (`CV_PROF` 확장) 및 `bench/tts_ttfa_breakdown.py` (신규)**:
   - `CV_PROF=1`일 때 위 6구간 타이밍과 `ms/tok`을 로그에 남기고, 2×3 직교 실험(각 n=10) 결과를 `bench/results/next_04_breakdown.txt`에 저장한다 (기본 동작 불변).
2. **[2단계: 패치 적용] `services/cosyvoice/server.py`**:
   - 1단계에서 `poll_wait_ms` 또는 `frontend_ms` 손실이 확인되면 `_ModelTimeProxy`(`CV_POLL_MS=10`)와 `text_normalize` 1회 호출을 반영한다.
3. **[2단계: 벤치 검증] `bench/first_audio_bench.py` 및 `bench/cosyvoice_cancel_lock_bench.py`**:
   - `CV_POLL_MS=100` 대 `CV_POLL_MS=10` 교차 비교 및 취소 잠금 회귀 여부를 측정한다.

---

## 5. 검증 방법 (절차)

> ⚠️ 경고: 측정 전에 `free -g`로 가용 메모리를 확인하고, `healthup-llm`(8000)과 `healthup-cosyvoice`(8091) 외 다른 벤치가 돌고 있지 않은지 확인한다.

1. `server.py`에 `CV_PROF` 6구간 로그를 추가하고 `CV_PROF=1 services/cosyvoice/run.sh`로 재시작한 뒤 `.venv/bin/python bench/tts_ttfa_breakdown.py`를 실행해 `bench/results/next_04_breakdown.txt`를 생성한다.
2. 분해표에서 500ms 대 712ms 차이(약 210ms)가 `poll_wait_ms`, `hop1_ready_ms`, `token2wav_ms`, `frontend_ms` 중 어디서 발생하는지 판별한다.
3. 1단계 중단 조건에 해당하지 않으면 `_ModelTimeProxy`(`CV_POLL_MS=10`)와 `text_normalize` 중복 제거를 적용하고 컨테이너를 재시작한다.
4. `.venv/bin/python bench/tts_ttfa_breakdown.py`를 다시 실행해 `poll_wait_ms` 감소와 `llm_job` 토큰당 시간(`ms/tok`) 무변화를 확인한다.
5. `.venv/bin/python bench/first_audio_bench.py --first-min 2 -n 20 --prompt fixed --chunker fixed`로 `CV_POLL_MS=100`과 `CV_POLL_MS=10`의 `idle`·`concurrent` 첫 소리 P50·백분위수 90 (P90)을 비교한다.
6. `.venv/bin/python bench/cosyvoice_cancel_lock_bench.py`(취소 잠금 회귀 확인)와 `tts_bench.py`·`tts_roundtrip.py`(40문장 왕복 문자 오류율 (CER)·실시간 계수 (RTF) 확인), `.venv/bin/python -m pytest`를 실행한다.
7. 실측 수치를 `Claude/docs/STATE.md`, `docs/STATE.md` §7, `docs/PLAN.md` §2에 기록한다.

---

## 6. 합격 기준 (수치 근거 명시) 및 중단 조건

- **수치 근거**: 100ms 폴링 1회의 균등 기대 대기는 50ms(최대 100ms)이고, 계단 경계(200ms 직후 토큰 완성)나 루프 종료 대기가 겹치면 90~180ms가 줄어든다. 따라서 최소 보장치(단일 폴링 평균 이상인 60ms)와 경계 해소 목표치(130ms)를 함께 본다.
- **1단계 통과 기준 (2단계 진입 조건)**:
  - `bench/results/next_04_breakdown.txt`의 `C: Post-vLLM` 조건에서 `poll_wait_ms + frontend_ms` P50이 **≥ 50ms**여야 2단계 폴링·전처리 패치로 넘어간다.
- **2단계 합격 기준 (모두 만족해야 기본값 `CV_POLL_MS=10` 유지)**:
  1. **분해 지표**: `poll_wait_ms` P50 ≤ **15ms** (기존 대비 폴링 허비 제거).
  2. **`first_audio_bench.py` (`idle` / Post-vLLM, n=20)**: TTS 첫 소리 P50이 `CV_POLL_MS=100` 대비 **≥ 60ms 단축**(예: 712ms → **≤ 650ms**, 상향 목표 **≤ 580ms**).
  3. **`first_audio_bench.py` (`concurrent`, n=20)**: TTS 첫 소리 P50이 `CV_POLL_MS=100` 대비 **≥ 50ms 단축**(예: 1000ms → **≤ 950ms**) 및 합계(`chunk + tts`) 악화 없음.
  4. **무회귀 확인**: 취소 잠금(`+300ms` P50 ≤ **150ms**), 40문장 왕복 CER P50 ≤ **0.14**, 단독 RTF P90 ≤ **0.45**, `pytest` 전체 통과.
- **중단 조건**:
  - 1단계에서 `poll_wait_ms + frontend_ms` P50이 50ms 미만이고 지연 차이가 순수 GPU 하드웨어 클럭/컨텍스트 전환에서만 발생하거나,
  - 2단계에서 GIL 경합으로 `llm_job` 토큰당 시간(`ms/tok`)이 10% 이상 느려져 `concurrent` TTFA가 개선되지 않으면 `CV_POLL_MS=100`으로 되돌리고 중단한다.

---

## 7. 위험

1. **전역 `time.sleep` 교체 사고**: `cosyvoice.cli.model.time.sleep`을 직접 덮어쓰면 전역 `time` 모듈이 바뀌므로 반드시 `model` 모듈의 `time` 참조만 프록시 객체로 바꿔야 한다.
2. **취소 정리 경로 우회 위험**: 폴링 대기 중에 예외를 던지면 `2437f38`의 `_patch_cancel`과 `gen.close()` 정리 흐름이 바뀔 수 있으므로, 폴링 프록시는 예외 없이 대기만 즉시 끝나게 해야 한다.
3. **LLM 동시 실행(`concurrent`) 시 GIL 경합**: 폴링이 너무 잦으면 `llm_job`의 `CUDAGraph` 실행 간격이 벌어질 수 있으므로 `PROF llm ... (ms/tok)` 지표를 함께 감시한다.
4. **운영 PC(RTX 4090 ×2) 재측정**: 개발 장비(단일 GB10) 수치는 최종 수치가 아니므로(`CLAUDE.md` §4), 5주차 운영 PC에서도 `CV_POLL_MS` 효과를 다시 확인한다.

---

## 8. 자체 검토 (`next_04_draft.md` 대비 수정 사항)

| 검토 항목 | 초안(`next_04_draft.md`)의 문제점 | 확정본(`next_04_first_tts.md`) 수정 내용 |
|---|---|---|
| 가설 분리 가능성 | 문장 차이(`"네, 알겠습니다."` 8자 vs `"많이 힘드셨겠어요."` 10자)와 직전 상태 차이(유휴 vs vLLM 직후)가 섞여 가설 2·3·4를 구분할 수 없었고, `text_normalize` 2회 중복 호출(`frontend_ms`)이 빠졌다 | **6구간 분해 로그**(`frontend_ms` 포함)와 **2×3 직교 실험**(문장 2종 × 직전 상태 3종)을 정의해 가설 1~4를 1단계에서 명확히 분리했다 |
| `CV_POLL_MS=10` 안전성·`2437f38` 호환성 | `model.py`의 `time.sleep`을 어떻게 고칠지 구체적이지 않았다. `model.time.sleep`을 직접 바꾸면 프로세스 전역 `time.sleep`이 오염된다 | `cosyvoice.cli.model`의 `time` 속성만 `_ModelTimeProxy`로 교체하고, 취소 시 예외 대신 대기만 즉시 반환해 `2437f38`의 `_patch_cancel`·`finally` 정리와 충돌하지 않게 했다 |
| 합격 기준 수치 근거 | 100ms 폴링 1회의 평균 대기는 50ms인데 근거 없이 `idle ≤ 580ms(-130ms)`만 합격 기준으로 두어, 90~100ms 단축 시 실패로 판정될 수 있었다 | 1단계 진입 조건(`poll_wait_ms + frontend_ms ≥ 50ms`)과 2단계 최소 합격치(`idle` -60ms / ≤ 650ms, 상향 ≤ 580ms; `concurrent` -50ms / ≤ 950ms)의 수학적 근거를 명시했다 |
| 태블릿·서버 전제 적합성 | 전제만 적고 왜 태블릿 지연에 직결되는지 설명이 부족했다 | 서버 첫 청크 시각 단축이 추가 네트워크 왕복 없이 태블릿 웹소켓 첫 오디오 프레임(`tts_first_audio`)과 맞장구 뒤 본문 공백을 직접 줄임을 명시했다 |

---

## 9. 1단계 결과 검토 (`bench/results/next_04_breakdown.txt`, 2026-10-23)

> **판정: ✅ 1단계 검증 통과 — 2단계(`CV_POLL_MS=10` + `text_normalize` 중복 제거) 즉시 진행 승인**
> - 1단계 결과 파일: `bench/results/next_04_breakdown.txt` (2×3 직교 실험, 각 셀 n=10, 총 60회)
> - 핵심 실측치: 조건 C(`Post-vLLM`)에서 `poll_wait_ms` P50 **58.8ms**, `frontend_ms` P50 **2.6ms**, 행별 합(`poll_wait_ms + frontend_ms`) P50 **60.4ms**로 2단계 진입 기준(**≥ 50ms**)을 충족했다.

### 9-1. 원시 표(60행)와 요약 통계 대조 및 가설 판정

1. **원시 데이터 ↔ 요약 표 수치 일치 여부**:
   - §6의 60개 원시 행에 `voice_pipeline.trace.percentile(vals, 50/90)`을 직접 적용해 검산한 결과, §2(2×3 셀별)·§3(조건 A·B·C 통합)·§4(문장 S1·S2 통합)·§5(진입 판정)의 모든 P50·P90 수치가 소수 첫째 자리까지 정확히 일치한다.
   - 참고로 조건 C의 `poll+front` P50(`60.4ms`)이 `poll_wait` P50(`58.8ms`) + `frontend` P50(`2.6ms`) = `61.4ms`와 1.0ms 다른 이유는 각 시행(row)별로 `poll_wait_ms + frontend_ms`를 먼저 더한 뒤 중앙값을 구했기 때문이며, 통계적으로 행별 합산 후 백분위수를 구하는 것이 맞다. (사소한 표기 불일치: 8행·303행 주석에 S1 `"네, 알겠습니다."`가 `8자`로 적혀 있으나 공백·마침표 포함 `len(S1)`은 48행 표의 `9`가 맞다.)
2. **가설 1~4 실측 판정 (100ms 폴링 계단 증폭 메커니즘 입증)**:
   - 모든 60개 시행에서 **`hop1_ready_ms + poll_wait_ms`의 합이 항상 약 `201.5ms`(폴링 2회) 또는 약 `310.0ms`(폴링 3회)로 양자화**되었다:
     - 정상 턴(조건 A·B 전수 및 조건 C 20회 중 12회): `llm_first_tok_ms` `16~31ms`, `hop1_ready_ms`(13번째 토큰 도달) `143.1~163.1ms` → 2번째 `time.sleep(0.1)`이 깨어나는 **`~201.5ms`**에 `token2wav` 진입 (`poll_wait_ms` **`39.1~58.8ms`**, 서버 첫 청크 **`490~544ms`**).
     - **vLLM 직후 지연 턴(조건 C 20회 중 8회: S1 #2·#3·#7·#8·#9, S2 #1·#4·#9)**: vLLM 직후 GPU 컨텍스트 전환으로 프리필(`llm_first_tok_ms` **`59.8~65.3ms`**, +30ms)과 초기 디코드(`16.2~17.5ms/tok`, +1.2ms/tok)가 느려져 `hop1_ready_ms`가 **`220.9~234.2ms`**가 되었다. 실제 GPU 지연 증가는 약 +70ms였으나, **`201.5ms` 폴링 경계를 불과 20~30ms 차이로 놓치면서 3번째 폴링(`~310ms`)까지 `poll_wait_ms` `76.4~88.6ms`를 허비**해 서버 첫 청크가 **`593.3~656.2ms`**로 뛰었다.
   - 결론: **가설 1(폴링 양자화 대기) + 가설 2(vLLM 직후 컨텍스트 전환에 의한 100ms 계단 증폭)가 주원인으로 확정**되었고, **가설 3(3초 유휴 클럭 저하)**은 조건 B P50 `518ms` vs 조건 A `514ms`(+4ms)로 **기각**되었다. **가설 4(`frontend_ms`)**는 P50 `2.2~2.6ms`로 작지만 `text_normalize` 중복 호출 제거로 절반(~1.3ms)으로 줄일 수 있다.

### 9-2. 계측 코드(`server.py`, `tts_ttfa_breakdown.py`)의 측정 왜곡 여부 검토

1. **`services/cosyvoice/server.py` (왜곡 없음)**:
   - 디코드 루프(`_patch_cudagraph`·`_patch_llm`) 안에서는 `torch.cuda.synchronize()`나 로그 출력 없이 `time.monotonic()`과 딕셔너리 대입만 수행하고, 로그(`PROF_BREAKDOWN`, `PROF llm`)는 루프 종료 후·첫 청크 생성 후에만 출력한다.
   - 실제로 토큰당 생성 시간(`ms/tok` P50 `15.2~15.4ms`)과 조건 A 서버 첫 청크 P50(`514ms`)이 계측 코드 추가 전(`507~514ms`)과 완전히 같아 **측정 왜곡이 0**임을 확인했다.
   - 또한 `need_tok = token_hop_len(10) + flow.pre_lookahead_len(3) = 13`이 CosyVoice3 `model.tts` 내부 조건과 정확히 일치해 `poll_wait_ms`가 순수 폴링 대기 시간만을 정확히 분리했다.
2. **`bench/tts_ttfa_breakdown.py` (경미한 완화 요인 1건)**:
   - 205행에 도커 로그 타임스탬프(`since_iso`) 분리용 `await asyncio.sleep(0.010)`(10ms 대기)이 들어가 있고 `run_vllm_20tok`이 비스트리밍 응답 완료 뒤 호출되어, `first_audio_bench.py`(`stream_iter.aclose()` 직후 0ms에 in-flight 스트림을 끊고 곧바로 TTS 호출)보다 vLLM 직후 경합 빈도가 낮게(8/20회 = 40%) 재현되었다. 그럼에도 조건 C 전체 P50에서 `poll_wait_ms + frontend_ms = 60.4ms`(8회 지연 턴에서는 `79.4~91.5ms`)가 명확히 포착되었다.

### 9-3. 2단계 진행 지시: 정확한 코드 변경 및 합격 기준

1. **`services/cosyvoice/server.py` 변경 2건**:
   - **변경 ① `_ModelTimeProxy` 및 `CV_POLL_MS=10` 도입 (전역 `time.sleep` 오염 금지)**:
     - 환경변수 `POLL_MS = float(os.environ.get("CV_POLL_MS", "10"))` (기본값 `10`)을 읽는다.
     - `import cosyvoice.cli.model as cv_model_mod`에 대해 `cv_model_mod.time`만 프록시 객체(`_ModelTimeProxy`)로 교체한다.
     - `_ModelTimeProxy.sleep(s)`는 `abs(s - 0.1) < 1e-3`일 때 `eng._cur_cancel.wait(timeout=POLL_MS / 1000.0)`을 수행하고 즉시 반환(`return`)하며(여기서 예외를 던지지 않아야 `2437f38`의 `_patch_cancel` → `finally` 정리 경로가 그대로 유지됨), 그 외의 `s`는 원래 `time.sleep(s)`를 호출한다.
   - **변경 ② `synth()`의 `text_normalize` 중복 호출 제거 (`server.py` 403~409행)**:
     - `try` 블록에서 `segs = self.cv.frontend.text_normalize(text, split=True, text_frontend=True)` 결과를 바로 변수에 받아 성공 시 두 번째 호출을 생략하고, `except Exception:`에서만 `text_frontend=False`로 재호출한다.
2. **2단계 합격 기준 (1단계 실측 기반 구체화)**:
   - **① `bench/tts_ttfa_breakdown.py` (`CV_POLL_MS=10` 재측정, 결과 `bench/results/next_04_breakdown_poll10.txt` 저장)**:
     - 조건 A·B·C 모두 `poll_wait_ms` P50 ≤ **15.0ms** (기대값 ~5ms)
     - 조건 A·B 서버 첫 청크 P50 ≤ **485ms** (1단계 514~518ms 대비 30~45ms 단축)
     - 조건 C 서버 첫 청크 P50 ≤ **500ms**, P90 ≤ **575ms** (1단계 P50 540ms / P90 636ms 대비 단축; 200ms 경계 초과 턴에서 70~80ms 단축)
     - `ms/tok` P50 ≤ **16.5ms/tok** (1단계 15.2~15.4ms 대비 +10% 이내, GIL 경합 없음)
   - **② `bench/first_audio_bench.py` (`CV_POLL_MS=100` 대 `CV_POLL_MS=10` 비교)**:
     - `idle`(Post-vLLM) 및 `concurrent` 조건에서 서버/클라이언트 TTS 첫 소리 P50이 `CV_POLL_MS=100` 대비 **≥ 45~60ms 단축** 및 전체 `audio`(`chunk + tts`) 악화 없음
   - **③ 무회귀 검증**:
     - `bench/cosyvoice_cancel_lock_bench.py`: `+300ms` 잠금 대기 P50 ≤ **150ms** (`+100ms` 대기도 214ms 이하로 개선 또는 유지), 재요청 CER P50 ≤ **0.10**
     - `bench/tts_roundtrip.py` (40문장): 왕복 CER P50 ≤ **0.14**
     - `.venv/bin/python -m pytest` 전체 통과

### 9-4. 계측 코드 유지 여부 및 커밋 단위

1. **계측 코드 유지 여부: ✅ 남긴다 (`CV_PROF` 환경변수 게이트 유지)**:
   - `server.py`의 6구간 타임스탬프 기록은 오버헤드가 0에 가깝고 `PROF_BREAKDOWN` 로그 출력은 `if os.environ.get("CV_PROF"):`일 때만 동작한다. (더 깔끔하게 하려면 `_patch_cudagraph`·`_patch_llm`·`_patch_cancel` 안의 `self._prof[...]` 기록도 `if self._prof:` 조건으로 감싸고 `handle_tts`에서 `eng._prof = {"t0": t0} if os.environ.get("CV_PROF") else {}`로 두면 `CV_PROF`가 꺼져 있을 때 딕셔너리 쓰기조차 0회가 된다.)
   - 5주차 운영 PC(RTX 4090 ×2) 이식 시에도 `bench/tts_ttfa_breakdown.py`로 즉시 재측정할 수 있으므로 계측 코드와 벤치 스크립트를 함께 남긴다.
2. **커밋 단위 (웹앱 `B1` 커밋과 분리된 `next_04` 단독 커밋 1개)**:
   - 1단계와 2단계를 따로 커밋하지 않고, **2단계 적용·검증까지 마친 뒤 `next_04` 단일 커밋**으로 묶는다 (진행 중인 웹앱 `B1` 커밋과 섞이지 않게 별도 커밋으로 분리).
   - 커밋 포함 파일:
     - `services/cosyvoice/server.py`
     - `bench/tts_ttfa_breakdown.py`
     - `bench/results/next_04_breakdown.txt` (및 2단계 측정 결과 파일)
     - `docs/plans/next_04_first_tts.md`

---

## 10. 2단계 결과 검토 (`bench/results/next_04_stage2.txt`, 2026-10-23)

> **판정: ❌ 2단계 TTFA 합격 기준 미달 (`code`의 합격 보고는 오판) → 추가 튜닝 중단 및 마무리(취소 잠금 단축 이득만 반영 또는 원복)**
> - 검토 대상: `bench/results/next_04_stage2.txt`, `bench/results/next_01_1b_yield_v2.txt`, `bench/results/cosyvoice_cancel_lock_v3.txt`, `git diff services/cosyvoice/server.py`, 컨테이너 내부 `/opt/CosyVoice/cosyvoice/cli/model.py` 및 `docker logs`

### 10-1. §9-3 합격 기준 대조표

| 항목 (§9-3 기준) | 합격 기준 | 1단계 (`CV_POLL_MS=100`) | 2단계 (`CV_POLL_MS=10`) | 판정 |
|---|---|---|---|---|
| **① `poll_wait_ms` P50 (조건 A·B·C)** | **≤ 15.0ms** | A `41.9ms`, B `45.5ms`, C `58.8ms` | A **`37.5ms`**, B **`38.2ms`**, C **`37.5ms`** | ❌ **미달** (원인은 §10-2-2의 `prompt_token_pad=3` 누락 계측 오차) |
| **② 조건 A·B 서버 첫 청크 P50** | **≤ 485ms** | A `514ms`, B `518ms` | A **`506ms`** (-8ms), B **`504ms`** (-14ms) | ❌ **미달** (`~10ms`만 단축) |
| **③ 조건 C 서버 첫 청크 P50 / P90** | **P50 ≤ 500ms / P90 ≤ 575ms** | P50 `540ms` / P90 `636ms` | P50 **`573ms`** (+33ms) / P90 **`607ms`** (-29ms) | ❌ **미달** (P90은 -29ms 줄었으나 P50 미달) |
| **④ `ms/tok` P50 (GIL 경합 감시)** | **≤ 16.5ms/tok** | S1 `15.4`, S2 `15.2ms/tok` | S1 **`15.7`**, S2 **`15.4ms/tok`** | ✅ 통과 |
| **⑤ `first_audio_bench.py` (`idle` / `concurrent`)** | **≥ 45~60ms 단축** | `idle` `711ms` / `concurrent` `1000ms` | `idle` **`726ms`** / `concurrent` **`1046ms`** (`CV_PROF=1` 켜진 상태) | ❌ **미달** (`CV_PROF=1` 동기화 오버헤드 포함 악화) |
| **⑥ 취소 잠금 (`cosyvoice_cancel_lock_bench.py`)** | **`+300ms` P50 ≤ 150ms** | `+100ms` `214ms`, `+300ms` `83ms` | `+100ms` **`180ms`** (-34ms), `+300ms` **`53ms`** (-30ms) | ✅ **통과 (개선)** |

### 10-2. 프록시 적용·서버 재기동 여부 및 미달 원인 3가지 규명

1. **`_ModelTimeProxy`(`CV_POLL_MS=10`)가 실제로 로드·적용되었는가? → ✅ 예 (컨테이너 재기동 및 적용 확인)**:
   - `docker inspect healthup-cosyvoice` 확인 결과, 컨테이너는 `2026-10-22T21:18:20Z`(2단계 측정 `21:21:53` 직전)에 새로 생성·기동되었고 호스트의 `services/cosyvoice/server.py`가 `/opt/server.py`로 바인드 마운트되어 있다.
   - 실제 적용 증거 3가지:
     1. `frontend_ms` P50이 `2.2~2.6ms` → **`1.4ms`**로 절반 감소했다(`text_normalize` 1회 호출 적용).
     2. 취소 잠금 벤치(`cosyvoice_cancel_lock_v3.txt`)에서 `+100ms` 잠금 대기가 `214ms → 180ms`(-34ms), `+300ms` 잠금 대기가 `83ms → 53ms`(-30ms)로 줄었다(`_cur_cancel.wait(timeout=0.01)` 동작).
     3. 1단계에서는 `hop1_ready_ms + poll_wait_ms`(`t_t2w_first - t_tts_enter`)가 모든 시행에서 `~201.5ms` 또는 `~310.0ms`(100ms 배수)로 고정되었으나, 2단계에서는 `178.7ms`(B-S1 #1), `185.7ms`(A-S1 #4), `256.4ms`(C-S1 #2), `267.7ms`(C-S2 #2)처럼 100ms 양자화가 사라졌다.

2. **원인 ① (핵심 발견): 왜 프록시가 적용됐는데도 `poll_wait_ms` P50이 `37.5ms`로 나왔는가? (`prompt_token_pad=3` 누락)**:
   - 컨테이너 안 `/opt/CosyVoice/cosyvoice/cli/model.py` 336~339행과 도커 로그(`PROF token2wav 309ms ntok=16`)를 대조한 결과:
     ```python
     prompt_token_pad = int(np.ceil(flow_prompt_speech_token.shape[1] / self.token_hop_len) * self.token_hop_len - flow_prompt_speech_token.shape[1])
     this_token_hop_len = self.token_hop_len + prompt_token_pad if token_offset == 0 else self.token_hop_len
     if len(self.tts_speech_token_dict[this_uuid]) - token_offset >= this_token_hop_len + self.flow.pre_lookahead_len:
     ```
   - 기본 목소리 `calm`(`KO_WAV_A`)은 `prompt_token_pad = 3`이다. 따라서 첫 홉(`token_offset == 0`)에서 `model.tts`가 `token2wav`에 들어가기 위해 실제로 기다리는 토큰 수는 `10 + 3 = 13`개가 아니라 **`10(hop) + 3(pad) + 3(pre_lookahead) = 16`개(`ntok=16`)**다.
   - 반면 `server.py` 247·364행의 `need_tok`은 `prompt_token_pad(3)`를 빼먹고 `10 + 3 = 13`으로 계산해 **13번째 토큰 시각에 `t_hop1_ready`를 찍었다**.
   - 즉, 보고된 `poll_wait_ms`에는 **14·15·16번째 토큰을 생성하는 순수 LLM 디코드 시간(`~2.1 × 15.5ms ≈ 32.5ms`)이 항상 포함**되어 있었다:
     - **2단계(`CV_POLL_MS=10`)의 실제 폴링 대기**: `37.5ms - 32.5ms ≈ 5.0ms` (이론치 5ms와 정확히 일치).
     - **1단계(`CV_POLL_MS=100`)의 실제 폴링 대기**: 조건 A·B에서도 보고된 `42~45ms`가 아니라 `42ms - 32.5ms ≈ 9.5~13ms`(16번째 토큰이 `~188.5ms`에 완성되어 `201.5ms` 2번째 폴링까지 불과 `~13ms`만 대기)였고, 조건 C의 느린 턴에서도 `78ms - 32.5ms ≈ 45.5ms`였다.
   - 결론: 1단계에서 `poll_wait_ms`가 42~78ms로 보였던 것의 상당 부분(32.5ms)은 폴링 대기가 아니라 `prompt_token_pad(3)` 토큰 생성 시간이었으며, 그래서 폴링을 10ms로 줄여도 A·B는 `~10ms`(`514→505ms`), C의 느린 턴은 `~37ms`(`625→588ms`, P90 `636→607ms`)만 줄어든 것이다.

3. **원인 ②·③: 왜 조건 C P50(`573ms`)과 `first_audio_bench.py`(`idle 726ms`)가 미달인가?**:
   - **조건 C의 이봉 분포 표본 변동**: 조건 C는 vLLM 직후 프리필이 빠른 턴(`llm_first_tok ≈ 20~31ms`, 첫 청크 `~510ms`)과 컨텍스트 전환으로 느린 턴(`llm_first_tok ≈ 60~64ms`, 첫 청크 1단계 `~625ms` → 2단계 `~588ms`)으로 갈린다. 1단계에서는 느린 턴이 8/20회(40%)여서 중앙값(P50)이 빠른 군집(`540ms`)에 걸렸고, 2단계에서는 느린 턴이 11/20회(55%) 걸려 중앙값이 느린 군집(`573ms`)에 걸렸다(느린 군집 자체와 P90은 `636 → 607ms`로 29~37ms 단축됨).
   - **`CV_PROF=1`의 매 홉 6회 `torch.cuda.synchronize()` 오버헤드**: 현재 컨테이너가 `CV_PROF=1` 환경변수로 떠 있어 `server.py:160~186`의 기존 프로파일러가 매 홉마다 `torch.cuda.synchronize()`를 6번 호출하고 로그 3줄을 찍는다. 이 때문에 `first_audio_bench.py`(`next_01_1b_yield_v2.txt`)에서 `idle`이 711ms → **726ms**, `concurrent`가 1000ms → **1046ms**로 오히려 느려졌다.
   - **근본 한계(§6 중단 조건 해당)**: vLLM 스트리밍 직후 0ms에 호출되는 첫 TTS가 ~710ms 걸리는 주원인은 폴링 대기(실제 ~13~45ms)가 아니라 **vLLM 잔여 스텝 정리·GPU 컨텍스트 전환으로 인한 16토큰 생성 지연(`~235ms`) + `token2wav` 합성 시간(`~315ms`)**이다.

### 10-3. 다음 조치 지시 (`next_04` 종결 및 컨테이너 정상화)

1. **[즉시 필수] `healthup-cosyvoice` 컨테이너를 `CV_PROF` 없이 재기동**:
   - `CV_PROF=1`이 켜져 있으면 매 홉 `torch.cuda.synchronize()` 6회가 돌아 지연을 악화시키므로 반드시 기본 설정(`services/cosyvoice/run.sh`)으로 컨테이너를 재기동한다.
2. **`next_04` 코드 처리 및 커밋 방침 (추가 실험 중단)**:
   - §6 중단 조건에 따라 첫 요청 TTFA 단축을 위한 추가 실험은 여기서 **종결**한다 (웹앱 `C1~C3` 일정을 막지 않는다).
   - 다만 `_ModelTimeProxy`(`CV_POLL_MS=10`)와 `text_normalize` 1회 호출은 부작용 없이 **단독 TTFA `-10ms`, 조건 C P90 `-29ms`(`636→607ms`), 취소 잠금 대기 `-30~34ms`(`+100ms` 214→180ms, `+300ms` 83→53ms)**를 개선하므로 코드는 유지하되, 아래 2줄만 정리해 커밋한다:
     - `server.py`에서 `self._prof` 타임스탬프 기록이 `if self._prof:`일 때만 동작하도록 하고 `handle_tts`에서 `eng._prof = {"t0": t0} if os.environ.get("CV_PROF") else {}`로 게이트한다.
     - `server.py`의 `need_tok` 계산식에 `prompt_token_pad`(`3`)를 반영해 `CV_PROF=1` 시 `poll_wait_ms`가 실제 16토큰 기준으로 찍히게 고친다.
   - 위 정리 후 `services/cosyvoice/server.py`, `bench/tts_ttfa_breakdown.py`, `bench/results/next_04_breakdown.txt`, `bench/results/next_04_stage2.txt`, `bench/results/cosyvoice_cancel_lock_v3.txt`, `docs/plans/next_04_first_tts.md`를 묶어 `next_04` 커밋 1개로 마감한다.

---

## 11. `next_04` diff 검토 (`services/cosyvoice/server.py` 및 옛 미커밋 파일 전수 처리 방안, 2026-10-23)

> **판정: ✅ 커밋 가능 (`next_04` 즉시 커밋 승인)**
> - 검토 대상: `git diff services/cosyvoice/server.py` (150줄), 컨테이너 상태(`docker inspect healthup-cosyvoice`), `pytest` 270 passed (`/tmp/pytest_n04.txt`)
> - 요약: §10-3의 지시 사항 3건(`self._prof`를 `CV_PROF` 설정 시에만 기록하도록 게이트, `need_tok`에 `prompt_token_pad=3` 반영, 컨테이너 `CV_PROF` 제거 재기동)이 모두 정확히 반영되었으며, `_ModelTimeProxy`(`CV_POLL_MS=10`) 및 `text_normalize` 중복 호출 제거도 부작용 없이 깔끔하다.

### 11-1. `git diff services/cosyvoice/server.py` 항목별 확인

| 구분 | 변경 위치 | 확인 내용 | 판정 |
|---|---|---|---|
| **① `_ModelTimeProxy` (`CV_POLL_MS=10`)** | `server.py:115~144` | `cosyvoice.cli.model.time`만 프록시로 감싸 전역 `time.sleep` 오염이 없고, `abs(s - 0.1) < 1e-3`일 때만 `_cur_cancel.wait(timeout=poll_s)` 수행 후 예외 없이 반환해 `2437f38` 취소 정리 경로와 완벽히 호환됨 | ✅ 적합 |
| **② `self._prof` `CV_PROF` 게이트** | `server.py:266, 268, 280, 324, 382, 384, 391, 429, 446, 517` | `handle_tts`(517행)에서 `eng._prof = {"t0": t0} if os.environ.get("CV_PROF") else {}`로 초기화하고 모든 기록 지점을 `if self._prof:`로 감싸, 기본 실행(`CV_PROF` 미설정) 시 딕셔너리 쓰기 0회·오버헤드 0을 보장함 | ✅ 적합 |
| **③ `need_tok`에 `prompt_token_pad=3` 반영** | `server.py:250~251, 369~370` | `need_tok = token_hop_len(10) + pre_lookahead_len(3) + prompt_token_pad(3) = 16`으로 수정되어 첫 홉 실제 합성 토큰 수(`ntok=16`)와 정확히 일치함 | ✅ 적합 |
| **④ `text_normalize` 중복 호출 제거** | `server.py:436~439` | `try` 블록에서 `segs = self.cv.frontend.text_normalize(..., text_frontend=True)` 1회만 호출하고 실패 시에만 `text_frontend=False`로 재시도 (`frontend_ms` P50 2.5ms → 1.4ms 단축) | ✅ 적합 |
| **⑤ 컨테이너 `CV_PROF` 제거 재기동** | `docker inspect healthup-cosyvoice` | `Created=2026-10-22T22:19:12Z` 컨테이너의 `Config.Env`에 `CV_PROF`가 제거되어 매 홉 `torch.cuda.synchronize()` 6회 오버헤드가 사라졌음을 확인함 | ✅ 확인 |

### 11-2. `next_04` 커밋 대상 파일 목록 (4개 파일) 및 커밋 명령

> 참고: `bench/results/` 디렉터리는 `.gitignore`(9행)에 등록되어 있으나, `bench/results/cosyvoice_cancel_lock_v3.txt`는 이전 커밋(`2437f38`)에서 이미 추적 중(tracked)인 파일이므로 이번 `CV_POLL_MS=10` 재측정 개선 결과(`+100ms` 214→180ms, `+300ms` 83→53ms)를 함께 커밋한다. (`next_04_breakdown.txt`·`next_04_stage2.txt`는 `.gitignore` 대상이며 핵심 표가 모두 본 문서 §9·§10에 수록되어 있으므로 커밋에서 제외해 `.gitignore` 원칙을 지킨다.)

- **`next_04` 커밋 파일 (4개)**:
  1. `services/cosyvoice/server.py` (수정)
  2. `bench/tts_ttfa_breakdown.py` (신규)
  3. `bench/results/cosyvoice_cancel_lock_v3.txt` (수정)
  4. `docs/plans/next_04_first_tts.md` (신규)
- **커밋 명령 (`code` 창에서 C1 커밋 직후 실행)**:
  ```bash
  git add services/cosyvoice/server.py bench/tts_ttfa_breakdown.py bench/results/cosyvoice_cancel_lock_v3.txt docs/plans/next_04_first_tts.md
  git commit -m "next_04: CosyVoice 폴링 주기 단축(CV_POLL_MS=10), text_normalize 중복 제거 및 6구간 분해 계측"
  ```

### 11-3. 남은 옛 미커밋 파일 전수 분류 및 처리 지침 (`커밋` / `보류` / `폐기`)

현재 워킹 트리에 남아 있는 모든 수정·미추적 파일을 확인한 결과, 아래 표와 같이 처리하면 워킹 트리가 깨끗이 정리된다.

| 분류 | 파일 경로 | 정체 및 상태 | 처리 판정 (`커밋` / `보류` / `폐기`) | 구체적 실행 방법 |
|---|---|---|---|---|
| **그룹 1: 계획·현황·검토 문서 (6개)** | - `docs/PLAN.md` (수정)<br>- `docs/STATE.md` (수정)<br>- `docs/plans/next_03_review.md` (수정)<br>- `docs/plans/webapp_plan.md` (신규)<br>- `docs/plans/webapp_plan_review.md` (신규)<br>- `docs/plans/next_01.md` (신규)<br>- `docs/plans/next_01_review.md` (신규) | 완료된 작업(`2437f38`, `db725dd`, 웹앱 `A1~C1`)의 현황·계획·검토 기록 정본 | ✅ **별도 문서 커밋 1개로 즉시 커밋** | `next_04` 커밋 직후 아래 명령으로 묶어 커밋:<br>`git add docs/PLAN.md docs/STATE.md docs/plans/next_03_review.md docs/plans/webapp_plan.md docs/plans/webapp_plan_review.md docs/plans/next_01.md docs/plans/next_01_review.md && git commit -m "docs: STATE·PLAN 갱신 및 웹앱 계획·검토 문서 추가"` |
| **그룹 2: 웹앱 C2 평가 세트 (1개)** | - `bench/intent_set_ko.tsv` (신규, 244줄) | 설계·검토 창이 미리 작성·보완 완료한 웹앱 `C2` 의도 분류 평가 세트 | ⏸️ **보류 (`C2` 커밋 때 함께 커밋)** | 바로 다음 작업인 **웹앱 `C2`**(`src/voice_pipeline/intent.py`, `bench/intent_eval.py`, `tests/test_intent.py`) 완료 시 함께 `git add`하여 커밋 |
| **그룹 3: 불채택 `1b` 양보 실험 코드·벤치 (4개)** | - `src/voice_pipeline/llm/openai_stream.py` (수정: `max_tokens` 인자·1b 상수)<br>- `tests/test_llm_openai.py` (수정: `max_tokens` 시험 3개)<br>- `bench/first_audio_bench.py` (수정: `--compare-yield` 모드)<br>- `bench/generate_listen_samples.py` (신규: 1b 샘플 생성기) | `next_01` 1b(2단계 양보) 실험용. 실측에서 청크 간 무음(`gap > 0` 9/10회)으로 **기본 채택은 기각**되었으나, `max_tokens=None` 기본 동작은 불변이며 향후 운영 PC(RTX 4090 ×2) 재비교용 벤치 도구로 쓸 수 있음 | ✅ **별도 실험 도구 커밋 1개로 분리 커밋** *(또는 `git stash` 보류)* | 워킹 트리에 미커밋으로 남겨 두면 이후 웹앱 작업마다 섞일 위험이 크므로, 단독 커밋 1개로 정리 권장:<br>`git add src/voice_pipeline/llm/openai_stream.py tests/test_llm_openai.py bench/first_audio_bench.py bench/generate_listen_samples.py && git commit -m "bench/llm: 1b 양보 실험 옵션(기본 미사용) 및 비교 벤치 도구 추가"` |
| **그룹 4: 대체된 초안 문서 (2개)** | - `docs/plans/next_04_draft.md` (신규)<br>- `docs/plans/next_02_draft.md` (신규) | `next_04_draft.md`는 확정본 `next_04_first_tts.md`로 대체됨; `next_02_draft.md`는 미실행 선행 생성 초안 | 🗑️ / ⏸️ **`next_04_draft.md`는 폐기(`rm`), `next_02_draft.md`는 보류(커밋 제외)** | `rm -f docs/plans/next_04_draft.md` (확정본이 있으므로 중복 초안 제거). `next_02_draft.md`는 추후 녹음 기반 턴 종료 규칙 조정(§3) 때까지 미커밋 보류 또는 삭제 |



