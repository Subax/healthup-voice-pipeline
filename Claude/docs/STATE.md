# STATE — 개발 현황 (세션마다 갱신)

> 갱신 규칙
> - 세션 시작: 「1. 현재 상태」와 「2. 다음 할 일」을 읽고 이번 세션 작업을 정한다.
> - 세션 끝: 「1」을 덮어써서 최신으로 만들고, 「2」를 다시 정렬하고, 「3. 세션 로그」 맨 위에 항목을 추가한다.
> - 직접 확인한 사실만 쓴다. 확인하지 않은 것은 "확인하지 않았다"로 쓴다.
> - 날짜는 절대 날짜(YYYY-MM-DD)로 쓴다.
> - 방향은 `OBJECTIVE.md`를 따른다. 이 문서는 목표를 바꾸지 않는다.

---

## 1. 현재 상태 (최종 갱신: 2026-10-11)

**단계**: 1주차 준비 — TTS·STT 후보 실측 직전

**저장소**

- git `main` @ `5a012f9` (docs: dev-guide v2), 작업 트리 깨끗함 (Claude/docs, CLAUDE.md 추가 전 기준)
- 테스트: 22개 통과 (`.venv/bin/python -m pytest`, 1.56s)

**구현 완료** (코드·테스트 있음)

- turn_id 기반 취소 `TurnGuard` (`turns.py`)
- 청커: 첫 청크 쉼표 분할, 소수점 보호, 마지막 공백 강제 분할 (`chunker.py`)
- 텍스트 정규화: 기호·단위·마크다운 (`normalize.py`)
- 파이프라인: 3-slot 청크 큐, barge-in, 119 정지 후 안전 안내, 들은 부분만 대화 기록 (`pipeline.py`)
- 오디오 프레임·20ms 분할·페이드아웃 (`audio.py`), 턴 추적 기록 P50·P90 (`trace.py`)
- 모의 상담 LLM (in-process, SSE 서버·클라이언트, `llm/`), 모의 TTS (`tts/mock.py`)
- Unreal 브리지 프로토콜 + 가짜 Unreal 서버 (`bridge/`)
- 벤치: `bench/tts_bench.py`(mock만 동작), `bench/latency_sim.py`, `bench/sentences_ko.txt`(20줄)

**미구현**

- 실제 TTS 어댑터: `bench/tts_bench.py`의 `ENGINES`에 `omnivoice`, `cosyvoice3`가 `_not_yet(...)` 자리표시자로만 있다
- STT(SenseVoice·Qwen3-ASR), VAD, Smart Turn, Kiwi 턴 종료, 선행 생성, AEC, 실제 LLM 결합, 립싱크

**개발 장비 (이 세션에서 확인)**

- AI TOP ATOM: aarch64, NVIDIA GB10, 메모리 121GB
- 확인 시점 메모리 109GB 사용 중, 가용 약 12GB. 모델을 올리기 전에 무엇이 메모리를 쓰는지 확인한다
- dev-guide §14 해당: CosyVoice3의 ttsfrd 휠은 x86_64 전용 → wetext로 대체된다. GB10 PyTorch CUDA에서 CosyVoice3·OmniVoice 동작은 확인하지 않았다. 이 장비의 수치를 운영 PC 수치로 쓰지 않는다
- 가상환경은 저장소 안 `.venv/`에 있다 (Python 3.12, gitignore 대상). 2026-10-11 상위 폴더에서 옮겨 다시 만들었다

**문서 불일치**

- README의 "지금 할 일"은 "OmniVoice 또는 CosyVoice3"이고, dev-guide v2(더 최신 커밋)는 "CosyVoice3 먼저"다. dev-guide v2를 따른다. README는 다음 커밋 때 맞춘다.

**막힌 것 / 팀 결정 대기**: OBJECTIVE.md §8의 8건 모두 미결정

---

## 2. 다음 할 일 (우선순위 순)

1. **CosyVoice3 어댑터** — `src/voice_pipeline/tts/cosyvoice.py`에 `TTSEngine`을 구현하고 `bench/tts_bench.py`의 `ENGINES["cosyvoice3"]`에 연결한다 (dev-guide §0, §4.5)
   - 먼저 ATOM(aarch64·GB10)에서 CosyVoice3 설치 가능 여부를 확인한다 (PyTorch CUDA 빌드, wetext 대체)
   - 첫 실측: `bench/sentences_ko.txt`로 TTFA·RTF, `--save-wav`로 청취 파일
   - 청커 경로와 토큰 직접 입력 경로를 모두 잰다 (§4.4)
2. `bench/sentences_ko.txt`를 30~50문장으로 늘린다 (§8.3: 짧은 대답, 수치, 상담 발화, 긴 다절 문장, 질문 억양)
3. OmniVoice 어댑터 → 같은 문장 세트로 비교 (§4.5 구현 순서)
4. SenseVoice-Small(sherpa-onnx) 구간 전사 실측 + Qwen3-ASR 기준점 (§3.3), STT·턴 종료 녹음 세트 준비 (§8.3)
5. 팀원 청취 테스트, 선택 후보 Windows 동작 확인 (1주차 §9)
6. README "지금 할 일"을 dev-guide v2와 맞춘다

---

## 3. 세션 로그 (최신이 위)

### 2026-10-11

- 한 일: `docs/dev-guide.md` v2를 읽고 `Claude/docs/OBJECTIVE.md`(정본), `Claude/docs/STATE.md`, 루트 `CLAUDE.md`를 만들었다
- 확인: 테스트 22개 통과, git `main` @ `5a012f9`, 장비 aarch64·GB10
- 코드 변경: 없음
- 환경: 가상환경을 상위 폴더에서 저장소 안 `.venv/`로 옮겼다 (같은 패키지 버전으로 다시 만들고 기존 것은 삭제). 테스트 22개 통과
- 다음: CosyVoice3 어댑터 (위 「2」의 1번)
