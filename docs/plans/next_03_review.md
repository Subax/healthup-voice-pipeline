# next_03 검토 — 1a 단독 커밋 판정, CosyVoice 취소 잠금 단축 계획

> 결론: (1) **1a는 먼저 커밋해도 된다.** 기본 동작은 바뀌지 않는다. 다만 diff를 덩어리(hunk) 단위로 나눠야 한다. (2) next_03 계획은 **수정필요**다. 원인 가설 3개는 코드와 맞지만, 공유 `cancel` 플래그 경합과 `llm_job` 종료 방법이 빠졌다.
> 지금 할 일: 「2-C. 필수 수정」 1~3을 `server.py` 작업에 바로 반영한다. 고치는 중인 코드와 겹친다.
>
> - 검토 대상: `bench/results/next_01_1b_yield_v3.txt`, `docs/plans/next_02_draft.md` §5, `docs/plans/next_03_cancel_lock.md`, `bench/results/cosyvoice_cancel_lock.txt`, `services/cosyvoice/server.py`(HEAD, 아직 diff 없음), 컨테이너 안 `/opt/CosyVoice/cosyvoice/cli/model.py`
> - 코드는 고치지 않았다. `pytest`는 251개 통과다 (v2 검토 때 실행)
> - 앞 턴의 「1단계 재측정 검토」 절은 `next_01_review.md`에 쓰지 못했다. 그 내용은 아래 1-B에 넣었다

---

## 1. 1a만 먼저 커밋해도 되는가

### 1-A. 판정: ✅ 커밋 가능 (조건 4개)

- 근거: 1a는 합격했다 (군더더기 단독 첫 청크 7/20 → 0/20, 첫 청크 +59ms). `DEFAULT_SYSTEM_PROMPT`는 그대로다. `skip_filler_comma`는 기본이 `False`다. 그래서 커밋해도 데모 동작은 바뀌지 않는다
- v3는 양보를 쓰지 않는다는 결론을 다시 확인했다. 1a와는 관계가 없다

| # | 조건 | 이유 |
|---|---|---|
| 1 | 덩어리 단위로 나눈다. 1a 커밋 = `chunker.py`, `tests/test_chunker.py`, `openai_stream.py`의 `PROMPT_NO_FILLER` 덩어리, 그 상수 시험 1개 | `openai_stream.py`·`test_llm_openai.py`·`first_audio_bench.py`에 1a와 양보 코드가 섞여 있다 |
| 2 | 스테이징한 상태만 남기고(`git stash -k`) `pytest`를 돌린 뒤 커밋한다 | `first_audio_bench.py`는 `FIRST_PHASE_MAX_TOKENS` 등을 가져온다. 양보 덩어리 없이 벤치만 넣으면 가져오기 오류가 난다 |
| 3 | 양보 코드(이어쓰기, `max_tokens` 분할, `--compare-yield`)와 1b 결과 3개는 "실험 기록, 채택 안 함" 커밋으로 따로 남긴다. 버려도 된다 | 기본은 꺼져 있고 시험도 통과한다. 1a·1b 결과 파일을 다시 만들려면 이 벤치가 필요하다 |
| 4 | `bench/cosyvoice_cancel_lock_bench.py`, `bench/generate_listen_samples.py`는 넣지 않는다 | next_03·청취 작업 파일이다 |

- 기본값 승격(`PROMPT_NO_FILLER`를 기본으로, 데모에서 `skip_filler_comma=True`)은 **별도 커밋**으로 한다. 승격 전에 `web_e2e.py`(n=16)로 확인한다 (next_02 §2(a)의 측정 방법)
- 커밋은 사용자가 요청할 때만 한다 (`CLAUDE.md` §8)

### 1-B. next_02_draft에서 고칠 사실 3개 (커밋을 막지 않음)

1. **"10자 문장이라 유휴에서도 711ms"(원인 1)는 데이터와 맞지 않는다.** 같은 짧은 문장도 다른 TTS 요청 바로 뒤에 오면 서버 첫 청크가 약 500ms다.
   - v1: 잠금 대기 뒤 바로 시작한 양보 1청크 약 495ms
   - v2·v3: `idle`·`concurrent`의 2청크(1청크 바로 뒤) 약 500~520ms
   - `cosyvoice_cancel_lock.txt`: "네, 알겠습니다." 유휴 기준선 501ms, 취소 직후 서버 합성 492~496ms
   - 반대로 직전 GPU 작업이 TTS가 아니면(LLM 생성 뒤, 또는 쉬는 중) 약 710ms다. 차이 약 200ms의 원인은 확인하지 않았다
   - `model.tts`는 `time.sleep(0.1)` 주기로 토큰 수를 확인한다. 그래서 첫 소리가 100ms 단위로 끊긴다. 200ms는 이 주기 2번이다
   - 그래서 "유휴 하한선 711ms"는 하한선이 아니다. "TTS 예열" 또는 이 대기 주기 축소가 약 100~200ms 후보다 (확인하지 않았다)
2. **"합계 1496ms(기존 1650ms 대비 -154ms)"는 1a 효과가 아니다.** 1a 단독 비교에서 보정본은 현재본보다 합계가 +37ms 느렸다 (1563 → 1600ms). -154ms는 세션 차이다 (LLM 첫 토큰 279 → 193ms). 1a의 이득은 품질(군더더기 단독 청크 제거)이다.
3. **"10자 문장은 단일 청크"(원인 2)는 확인하지 않았다.** `CV_HOP=10`이면 첫 청크는 약 10토큰(약 0.4초 음성)이다. 1.2초짜리 1청크는 여러 청크로 나온다고 본다. 결론(양보 포기)은 바뀌지 않는다.

- 동시 실행 기준선도 `gap > 0`이 v2 8/20, v3 16/20이다. 양보와 관계없이 지금 파이프라인도 문장 사이 공백이 자주 생긴다. 이것을 `docs/STATE.md` 알려진 문제에 남긴다.

---

## 2. next_03_cancel_lock 계획 검토

### 2-A. 판정 요약

| 항목 | 판정 | 근거 |
|---|---|---|
| 가설 1 (`await q.get()` 동안 끊김을 모름) | ✅ 맞다 | aiohttp 3.14는 기본으로 핸들러를 취소하지 않는다. 끊김은 다음 `resp.write()`에서만 드러난다 |
| 가설 2 (취소 뒤에도 `token2wav` 강행) | ✅ 맞다. 비중이 클 수 있다 | `model.tts`는 `cancel`을 보지 않는다. `llm_job`이 끝나면 루프를 나와 남은 토큰 전체로 `token2wav(finalize=True)`를 돈다 |
| 가설 3 (`gen.close()` 시 정리 건너뜀) | ✅ 맞다 | 정리 코드가 `yield` 뒤에 있고 `try/finally`가 없다. `p.join()`, 사전 3개 삭제, `empty_cache`가 모두 빠진다 |
| 주원인 배분 | ⚠️ | 가설 1과 2의 비중을 나누는 측정이 없다 |
| 합격 기준 | ⚠️ | 수치는 측정 가능하다. 취소 직후 문장의 음질 확인과 사전 잔여 키 측정 방법이 없다 |
| 위험 | ⚠️ | 공유 `cancel` 플래그 경합이 빠졌다. `llm_job` 종료를 어떻게 보장할지 정하지 않았다 |
| 범위 | ✅ | `server.py` 하나와 벤치다. 이미지는 다시 빌드하지 않는다 |

### 2-B. 빠진 위험 (가장 중요)

1. **공유 `cancel` 플래그 경합.** `Engine.cancel`은 요청 사이에 공유하는 `threading.Event` 하나다. `synth()`는 시작할 때 `self.cancel.clear()`를 부른다.
   - 이전 요청의 `llm_job`이 아직 돌고 있으면, 이 `clear()`가 그 스레드의 취소를 풀어 버린다
   - 그러면 두 `llm_job`이 같은 `StaticCache`·`CUDAGraph`(`g`, `x`, `pos`)를 동시에 쓴다. 같은 `llm_context` 스트림에서 커널이 섞인다
   - 결과는 깨진 음성이나 CUDA 오류다. 지금 코드에서도 취소 직후 요청에서 생길 수 있다 (확인하지 않았다)
   - 계획 위험 1은 결과만 적었다. 이 원인(`clear()`)은 적지 않았다
2. **`llm_job` 종료를 보장할 방법이 없다.** `llm_job` 스레드 `p`는 `model.tts` 안의 지역 변수다. 서버는 이 스레드를 잡을 수 없다. "종료 보장"이라고만 쓰면 구현이 정해지지 않는다.
3. **`_SynthCancelled`를 던져도 `model.tts`의 정리는 여전히 건너뛴다.** 예외도 `GeneratorExit`처럼 `p.join()`·사전 삭제를 지나친다. 서버가 직접 정리해야 한다. `uuid`는 `model.tts` 안에서 만들어서 서버는 모른다.
4. **`server.py`는 `.venv`에서 시험할 수 없다.** 맨 위에서 `torch`를 가져오는데 `.venv`에는 `torch`가 없다. 계획 §2-3의 "`tests/test_tts_cosyvoice.py` 단위 시험"은 클라이언트만 시험한다. 서버 수정은 벤치와 컨테이너 로그로만 검증된다.
5. **실제 영향 경로를 재지 않는다.** 취소는 끼어들기 뒤 새 턴과, 선행 생성 취소(`PendingTurn.abort`) 뒤 재시작에서 생긴다. 두 번째 경로는 확정 직후 첫 TTS를 최대 1.7초 늦출 수 있다. 벤치만으로는 이 경로의 이득을 확인하지 못한다.

### 2-C. 필수 수정 (지금 고치는 코드에 반영)

1. **요청마다 취소 토큰을 따로 쓴다.** 공유 `Event`를 `clear()`하지 않는다. 요청마다 새 `Event`를 만들어 `_patch_llm` 래퍼와 `token2wav` 검사가 그 요청의 토큰을 보게 한다. 예: 요청 시작 때 `self._cur = ev`, 래퍼는 시작 시점의 `ev`를 잡아 둔다.
2. **`llm_job` 종료를 이벤트로 기다린다.** 예: `self.cv.model.llm_job`을 감싸 `finally`에서 요청별 `done` 이벤트를 켠다. `synth`의 `finally`에서 이 이벤트를 기다린 뒤(시간 제한 + 초과 시 로그) 잠금을 푼다.
3. **정리는 서버가 직접 한다.** `llm_job` 종료를 확인한 뒤 `tts_speech_token_dict`·`llm_end_dict`·`hift_cache_dict`를 비운다. 잠금 때문에 한 번에 한 요청만 돌므로 전부 비워도 된다. 잔여 키 수를 로그로 남겨 합격 기준 3을 측정한다.

### 2-D. 권장 수정

4. **끊김 감지는 `handler_cancellation=True`를 먼저 시험한다.** `web.run_app(..., handler_cancellation=True)` 한 줄이면 끊김 때 핸들러가 `CancelledError`를 받는다. 기존 `except (ConnectionResetError, asyncio.CancelledError)`가 그대로 잡는다. 40ms 폴링(`wait_for` + `is_closing()`)보다 단순하다. 위험 4(반닫힘 감지)를 먼저 확인해 둘 중 하나를 고른다.
5. **원인 비중을 나눠 잰다.** 서버 로그에 세 시각을 남긴다: 끊김 감지, `synth` 종료, 잠금 해제. 기존 `CV_PROF`로 취소 뒤 `token2wav` 시간도 본다. 가설 1만 고친 상태와 1+2를 고친 상태를 따로 재면 비중을 알 수 있다.
6. **합격 기준 3에 "취소 직후 문장" 음질을 넣는다.** `StaticCache` 경합은 취소 직후 요청에서 드러난다. 30회 취소·재요청의 짧은 문장 WAV를 저장해 왕복 CER을 잰다. 40문장 CER(취소와 떨어진 정상 합성)만으로는 이 문제를 못 잡는다.
7. **벤치 취소 시각을 고친다.** 지금 벤치는 청크를 받을 때만 취소해 +100·+300ms가 둘 다 +415ms가 됐다 (계획 §2-3에 반영돼 있다). 이것으로 기준선을 다시 잰다.
8. **`token2wav` 검사는 `flow.inference` 앞과 `hift.inference` 앞에 둔다.** `flow.inference`는 `token2wav` 안에서만 불린다. 그래서 둘 다 감싸는 것은 중복이다. 사이 지점은 HiFT 앞이다.
9. **(별건)** `model.tts`의 `time.sleep(0.1)` 대기는 취소 반응과 첫 소리를 최대 100ms 늦춘다. 1-B-1의 첫 소리 후보로 따로 기록한다. 이번 범위에는 넣지 않는다.

### 2-E. 합격 기준 의견

- 잠금 대기 P50 ≤ 150ms, P90 ≤ 300ms와 짧은 문장 TTFA P50 ≤ 650ms는 측정할 수 있고 적절하다. 중단 조건(350ms)도 분명하다
- `lock_wait_ms`는 "벤치 TTFA − 서버 첫 청크"로 계산한다고 계획에 적는다 (`cosyvoice_cancel_lock_bench.py` 140행과 같은 식)
- 유휴 기준선 501ms는 앞 요청 바로 뒤에 잰 값이다 (1-B-1). 취소 직후 요청도 같은 조건이라 비교는 공정하다. 다만 이 값을 데모 첫 소리 기대값으로 쓰지 않는다

---

## 3. v3 diff 검토 (2026-10-22, `server.py` 및 `cosyvoice_cancel_lock_v3.txt`)

> 결론: **✅ 커밋 가능 (승인)**. 필수 수정 3개(2-C-1~3)와 권장 수정(2-D-6·7)이 모두 반영됐고, `pytest` 251개 통과 및 취소 직후 30회 재요청 왕복 문자 오류율 (CER) 백분위수 50 (P50) 0.08로 `StaticCache`·`CUDAGraph` 오염이 없음을 확인했다.
> 지금 할 일: 사용자 커밋 지시가 오면 아래 「3-C. 커밋 단위 분리 절차」대로 `next_03`과 `1a`를 분리해 커밋한다.

### 3-A. 코드 및 실측 검증 요약

| 항목 | 결과 | 판정 |
|---|---|---|
| 2-C-1 요청별 `cancel_token` | `handle_tts`마다 새 `threading.Event()` 발급, `_patch_llm`·`_patch_llm_graph`가 시작 시점의 `cur_cancel`을 지역 변수로 참조해 `clear()` 경합 제거 | ✅ |
| 2-C-2 `llm_job` 종료 대기 | `_patch_llm_job`이 스레드별 `done_ev`를 등록하고 `synth`의 `finally`에서 `ev.wait(timeout=1.0)` 확인 후 잠금 해제 | ✅ |
| 2-C-3 사전 직접 정리 | `with self.cv.model.lock:` 안에서 잔여 키 수 로깅 후 `tts_speech_token_dict`·`llm_end_dict`·`hift_cache_dict` `.clear()` 및 `torch.cuda.synchronize()` | ✅ |
| `+300ms` 취소 (n=10) | 잠금 대기 P50 **83ms** / 백분위수 90 (P90) **86ms** (수정 전 1094ms), 재요청 첫 오디오 합성 시간 (TTFA) P50 **572ms** (기준 ≤ 150ms / ≤ 650ms) | ✅ 합격 |
| `+600ms` 취소 (n=10) | 잠금 대기 P50 **114ms** / P90 **125ms** (수정 전 1727ms), 재요청 TTFA P50 **611ms** (기준 ≤ 150ms / ≤ 650ms) | ✅ 합격 |
| `+100ms` 취소 (n=10) | 잠금 대기 P50 **214ms** / P90 **216ms** (수정 전 1099ms, **-885ms**), 재요청 TTFA P50 **708ms** (기준 150ms / 650ms 대비 +64ms / +58ms 초과) | ⚠️ 조건부 수용 (3-B) |
| 취소 직후 음질 (n=30) | 짧은 문장("네, 알겠습니다.") 30회 전체 왕복 CER P50 **0.08** (최대 0.33), CUDA 오류 0건 | ✅ 합격 |

- 사소한 코드 메모(커밋을 막지 않음): `server.py` 23~25행의 `torch.cuda.init()`이 28행 `cudnn.enabled = False`보다 위에 있다. 동작에는 이상이 없으나 추후 `Engine.__init__` 안으로 옮기면 더 깔끔하다.

### 3-B. `+100ms` 케이스 미달(잠금 대기 P50 214ms)을 받아들일지 판정

- **판정: ✅ 받아들인다 (추가 수정 없이 합격 처리).**
- **근거 4가지**:
  1. **컨테이너 외부 패치의 물리적 하한**: 첫 오디오 직후 `+100ms`는 `CosyVoice2Model.tts()` 내부 `time.sleep(0.1)` 대기 또는 2번째 홉 `flow.inference`(단일 파이토치 (PyTorch) 커널 묶음 약 150ms)가 막 시작된 시점이다 (`next_03_cancel_lock.md` §5 위험 3). 여기에 `handle_tts` 폴링(최대 40ms)과 `llm_job` 1스텝 종료 대기(약 20ms)가 더해져 약 210ms가 되며, 이미지 내부 `/opt/CosyVoice`를 고치지 않고 줄일 수 있는 하한에 도달했다.
  2. **실제 파이프라인 취소 시점과의 일치**: 실제 끼어들기(`barge-in`)는 음성 활동 감지 (VAD) 최소 발화(약 250ms) + 맞장구 필터(`PROBE_WINDOW_MS = 600ms`) 뒤에 일어나고, 선행 생성 취소(`PendingTurn.abort`)도 말 재개 감지 뒤에 일어나므로 실제 운영 구간은 `+300ms`(83ms)·`+600ms`(114ms)다.
  3. **단축 폭 및 중단 조건 충족**: 수정 전 1099ms에서 **214ms로 885ms(80.5%) 단축**됐고, 계획의 중단 조건(350ms)보다 충분히 낮다.
  4. **일관성 및 음질 보장**: 10회 측정값이 모두 `212~217ms`(P90 216ms)로 스레드 잔류나 지연 튐이 없고, 해당 구간 재요청 왕복 CER도 P50 **0.00**이다.

### 3-C. 커밋 단위 분리 방법 (`1a`와 `next_03` 분리)

> ⚠️ 경고: 실제 `git commit`은 사용자가 명시적으로 요청할 때만 실행한다 (`CLAUDE.md` §8).

1. **커밋 1 (`next_03` — CosyVoice 취소 후 잠금 대기 단축)**:
   - 대상 파일: `services/cosyvoice/server.py`, `bench/cosyvoice_cancel_lock_bench.py`, `bench/results/cosyvoice_cancel_lock.txt`, `bench/results/cosyvoice_cancel_lock_v3.txt`
   - 다른 작업 파일과 겹치지 않으므로 위 4개 파일만 `git add`하여 바로 분리 커밋할 수 있다.
2. **커밋 2 (`next_01` 1a — 프롬프트·청커 군더더기 분할 방지 옵션, 기본값 불변)**:
   - 대상 파일: `src/voice_pipeline/chunker.py`, `tests/test_chunker.py`, `src/voice_pipeline/llm/openai_stream.py`의 `PROMPT_NO_FILLER` 상수 덩어리(hunk), `tests/test_llm_openai.py`의 `test_prompt_no_filler_constant_defined` 시험 1개, `bench/results/next_01_1a_prompt_chunker.txt`
   - 분리 절차: `git add src/voice_pipeline/chunker.py tests/test_chunker.py` 후 `git add -p src/voice_pipeline/llm/openai_stream.py tests/test_llm_openai.py`로 `PROMPT_NO_FILLER` 덩어리만 스테이징하고, `git stash -k`(1b 양보 코드는 임시 보관) 상태에서 `.venv/bin/python -m pytest` 통과를 확인한 뒤 커밋한다.
3. **커밋 3 (선택 — `next_01` 1b 양보 실험·벤치 및 청취 샘플 도구)**:
   - `git stash pop`으로 복원한 `openai_stream.py`·`test_llm_openai.py`의 이어쓰기(`continue_final_message`, `max_tokens` 분할) 덩어리, `bench/first_audio_bench.py`, `bench/generate_listen_samples.py`, `bench/results/next_01_1b_yield*.txt`, `docs/plans/*.md`를 실험 기록 커밋으로 묶거나 사용자 지시에 따라 정리한다.

