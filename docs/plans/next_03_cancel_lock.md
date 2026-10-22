# next_03_cancel_lock — CosyVoice 취소 뒤 서버 잠금 대기(1.1~1.7초) 단축 계획

> 결론: `bench/results/cosyvoice_cancel_lock.txt` 실측 결과, 유휴 상태에서 짧은 문장("네, 알겠습니다.")의 첫 오디오 합성 시간 (TTFA)은 백분위수 50 (P50) 501ms(잠금 대기 0ms)이나, 긴 문장 취소 직후 요청하면 잠금 대기가 **P50 1094~1727ms** 발생해 짧은 문장 TTFA가 **P50 1589~2222ms**로 3~4배 길어진다.
> 지금 할 일: `services/cosyvoice/server.py`에서 클라이언트 연결 종료 즉시 감지(`is_closing()`)와 `token2wav` 취소 차단을 적용하고 `bench/cosyvoice_cancel_lock_bench.py`로 재측정한다.
>
> - 상태: ✅ 수정 전 기준선 실측 완료(`bench/results/cosyvoice_cancel_lock.txt`) / 🔄 단축 계획 수립(`next_03_cancel_lock.md`) / ⏭️ `server.py` 수정 및 재측정

---

## 0. 수정 전 기준선 실측 (`bench/results/cosyvoice_cancel_lock.txt`, 각 n=10)

긴 문장(65자, 2문장) 합성 중 첫 오디오 수신 뒤 취소(`tts.cancel()`)하고 즉시 짧은 문장(8자)을 요청한 결과:

| 조건 | 실제 취소 시각(첫 소리 후) | 짧은 문장 TTFA P50 / 백분위수 90 (P90) | 서버 합성 P50 | 잠금 대기(`lock_wait_ms`) P50 / P90 |
|---|---|---|---|---|
| 유휴 기준선 (취소 없음, n=5) | - | **501 / 510ms** | 500ms | **0ms** |
| 첫 오디오 + 100ms 취소 | +415ms (2청크 수신 시) | **1589 / 1604ms** | 492ms | **1099 / 1103ms** |
| 첫 오디오 + 300ms 취소 | +415ms (2청크 수신 시) | **1589 / 1598ms** | 495ms | **1094 / 1106ms** |
| 첫 오디오 + 600ms 취소 | +1087ms (3청크 수신 시) | **2222 / 2245ms** | 496ms | **1727 / 1733ms** |

---

## 1. 원인 가설 (코드 및 로그 확인 결과 3가지)

1. **가설 1 — `handle_tts`의 `await q.get()` 블로킹으로 인한 연결 종료 감지 지연 (주원인)**:
   - `services/cosyvoice/server.py` 360~366행에서 청크를 `await resp.write(item)`로 보낸 직후, 코루틴은 다음 청크가 나올 때까지 `await q.get()`에 막혀 있다.
   - 클라이언트가 `resp.close()`로 연결을 닫아도 서버는 다음 청크 합성이 끝나 `await resp.write(item)`를 부를 때까지 끊김을 알지 못해 `eng.cancel.set()`이 호출되지 않는다 (`token_hop_len`이 10 → 20 → 40으로 2배씩 커져 뒤쪽 홉일수록 잠금 대기가 1.1초 → 1.7초로 늘어난다).
2. **가설 2 — `CosyVoice2Model.tts()`가 `cancel`을 확인하지 않고 다음·잔여 `token2wav`를 강행**:
   - 컨테이너 안 `/opt/CosyVoice/cosyvoice/cli/model.py` 328~366행은 시작 시 백그라운드 스레드 `p`(`llm_job`)를 띄워 토큰을 `tts_speech_token_dict[uuid]`에 미리 쌓는다.
   - `eng.cancel.set()`이 `llm_job`을 멈춰 `llm_end_dict[uuid] = True`가 되어도, `CosyVoice2Model.tts()`는 `cancel`을 보지 않고 이미 쌓인 토큰으로 다음 홉 `self.token2wav(...)`와 루프 밖 잔여 `self.token2wav(..., finalize=True)`(Flow 5스텝 + HiFT)를 끝까지 실행한 뒤에야 `yield`한다.
3. **가설 3 — `gen.close()` 시 `p.join()` 및 세션 사전 정리 누락**:
   - `server.py` 311행에서 `gen.close()`를 부르면 `model.py`의 `yield` 지점에서 `GeneratorExit`가 발생해 하단의 `p.join()`과 `tts_speech_token_dict`·`llm_end_dict`·`hift_cache_dict` 삭제가 건너뛰어진다.

---

## 2. 고칠 위치

- `services/cosyvoice/run.sh`는 `services/cosyvoice/server.py`를 `/opt/server.py`로 마운트하므로 도커 (Docker) 이미지를 다시 빌드하지 않는다.
1. **`services/cosyvoice/server.py` — `handle_tts` (359~374행)**:
   - `q.get()`을 30~50ms 단위로 대기(`asyncio.wait_for(q.get(), timeout=0.04)`)하면서 매번 `request.transport is None or request.transport.is_closing()`을 확인한다.
   - 연결이 닫혔으면 즉시 `eng.cancel.set()`을 켜고 루프를 빠져나가 `th.join()`으로 넘어간다.
2. **`services/cosyvoice/server.py` — `Engine` 초기화 및 `synth` (148~312행)**:
   - `self.cv.model.token2wav`와 `self.cv.model.flow.inference` 앞에 `if self.cancel.is_set(): raise _SynthCancelled()` 검사를 감싸, 취소 신호 직후에는 새 `token2wav`나 HiFT 보코더가 시작되지 않게 즉시 중단한다.
   - `synth`에서 `_SynthCancelled`를 잡아 정상 종료하고, `finally:`에서 `gen.close()` 뒤 남아 있는 `llm_job` 스레드 종료와 `tts_speech_token_dict`·`llm_end_dict`·`hift_cache_dict` 정리를 보장한다.
3. **`bench/cosyvoice_cancel_lock_bench.py` 및 `tests/test_tts_cosyvoice.py`**:
   - 벤치에서 청크 수신 대기와 별도 타이머 태스크로 정확한 시각(+100ms, +300ms, +600ms)에 `tts.cancel()`을 호출하는 조건도 함께 확인하고, 단위 시험을 추가한다.

---

## 3. 합격 기준 (수치) 및 중단 조건

- **합격 기준 (`bench/cosyvoice_cancel_lock_bench.py`, +100/+300/+600ms 각 n=10, 모두 만족)**:
  1. **잠금 대기(`lock_wait_ms`)**: 세 조건 모두 P50 ≤ **150ms**, P90 ≤ **300ms** (수정 전 P50 **1094~1727ms** 대비 **85% 이상 단축**).
  2. **취소 직후 짧은 문장 TTFA**: 세 조건 모두 P50 ≤ **650ms** (유휴 기준선 501ms 대비 지연 증가분 ≤ **150ms**, 수정 전 P50 **1589~2222ms**).
  3. **안정성·품질**: 30회 연속 취소·재요청 동안 CUDA 그래프 (`CUDAGraph`) 오류 **0건**, `tts_speech_token_dict` 잔여 키 **0개**, 취소 뒤 40문장 왕복 문자 오류율 (CER) P50 ≤ **0.14**(기존과 동일).
- **중단 조건**:
  - 패치 후에도 이미 진입한 파이토치 (PyTorch) 커널 동기 대기 때문에 `lock_wait_ms` P50이 **350ms**를 넘거나, `token2wav` 중단으로 인해 다음 요청의 `CUDAGraph`(`_patch_llm_graph`) 또는 CUDA 스트림이 깨지면 변경을 되돌리고 중단한다.

---

## 4. 측정 방법

> ⚠️ 경고: 컨테이너 재시작 전에 다른 프로세스가 `healthup-cosyvoice`(8091)를 쓰고 있지 않은지 확인한다.

1. `services/cosyvoice/server.py`에 연결 끊김 즉시 감지와 `token2wav` 취소 차단을 반영하고 `.venv/bin/python -m pytest`를 실행한다.
2. `services/cosyvoice/run.sh`로 `healthup-cosyvoice` 컨테이너를 재시작하고 `/info` 응답을 확인한다.
3. `.venv/bin/python bench/cosyvoice_cancel_lock_bench.py`를 실행해 수정 후 수치(+100ms·+300ms·+600ms 잠금 대기 및 짧은 문장 TTFA)를 `bench/results/cosyvoice_cancel_lock.txt`와 비교한다.
4. 컨테이너 로그(`docker logs --tail 50 healthup-cosyvoice`)에서 예외·CUDA 오류 유무를 확인한다.
5. `.venv/bin/python bench/tts_bench.py --engine cosyvoice3 --save-wav bench/results/cancel_check`와 `.venv/bin/python bench/tts_roundtrip.py bench/results/cancel_check`로 취소 시험 직후 정상 합성의 TTFA·왕복 CER을 확인한다.
6. 수정 전·후 수치를 `Claude/docs/STATE.md`와 `docs/STATE.md` §7에 기록한다.

---

## 5. 위험

1. **`CUDAGraph` 및 `StaticCache` 충돌 위험**: `_patch_llm_graph`는 고정된 `StaticCache`와 `CUDAGraph`를 재사용하므로, `llm_job` 스레드가 완전히 멈추기 전에 다음 요청이 시작되면 캐시가 겹쳐 음성이 깨지거나 CUDA 오류가 난다 (`lock` 해제 전 `llm_job` 종료 보장이 필수다).
2. **`self.llm_context` 별도 CUDA 스트림 동기화**: `CosyVoice3Model`은 `llm_job`을 별도 CUDA 스트림(`self.llm_context`)에서 돌리므로, 취소 정리 시 `self.llm_context` 스트림 동기화가 필요할 수 있다 (**확인하지 않았다**).
3. **이미 시작된 단일 커널 실행 시간**: 취소 신호 직전에 이미 시작된 Flow/HiFT GPU 커널 1개(약 50~150ms)는 파이썬 예외로 중간에 끊을 수 없으므로 최소 수십 ms의 잠금 대기는 남는다.
4. **루프백 소켓 반닫힘(half-close) 감지**: 클라이언트가 `resp.close()`를 불렀을 때 서버의 `request.transport.is_closing()`이 즉시 `True`가 되는지 컨테이너 환경에서 먼저 확인해야 한다 (**확인하지 않았다**).
