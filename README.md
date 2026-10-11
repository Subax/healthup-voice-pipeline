# healthup-voice-pipeline

HealthUp Station 버츄얼휴먼의 음성 파이프라인이다. 마이크부터 Unreal 재생까지의 지연을 줄이고, barge-in을 정확하게 처리하는 것이 목표다.

**지금 할 일**: `bench/tts_bench.py`에 로컬 TTS 어댑터(OmniVoice 또는 CosyVoice3)를 하나 붙여 첫 실측을 한다.

설계 근거는 [docs/dev-guide.md](docs/dev-guide.md)에 있다. 아키텍처 정본은 `10-ai26-2-ai.md` (v0.3)이다.

## 진행 상태

- ✅ turn_id 기반 취소 (`TurnGuard`), 첫 청크 단축·소수점 보호 청커, 텍스트 정규화
- ✅ 모의 상담 LLM (in-process, SSE 서버·클라이언트), 모의 TTS, 턴 추적 기록
- ✅ 파이프라인: 청크 큐(3-slot), barge-in, 119 정지 후 안전 안내, 들은 부분만 대화 기록
- ✅ Unreal 브리지 프로토콜 + 가짜 Unreal 서버 (재생 위치 회신, 립싱크 지연 흉내)
- 🔄 TTS 후보 실측 (벤치 스크립트만 있음)
- ⏭️ 실제 TTS 어댑터 (OmniVoice, CosyVoice3, Azure, CLOVA), STT·VAD·Smart Turn, AEC, 선행 생성

## 구조

```text
src/voice_pipeline/
  pipeline.py        LLM 스트림 → 청커 → 정규화 → TTS → 20ms 프레임 → 싱크
  turns.py           TurnGuard: 정지 = 현재 turn_id 무효화
  chunker.py         SemanticChunker
  normalize.py       기호·단위 → 읽을 수 있는 말
  audio.py           AudioFrame, Framer, fade_out
  trace.py           턴 추적 기록, P50·P90
  sinks.py           MemorySink(테스트), LocalSpeakerSink(로컬 스피커, sounddevice)
  llm/               §32 이벤트(임시 스키마), MockLLM, SSE 서버·클라이언트
  tts/               TTSEngine 인터페이스, MockTTS
  bridge/            Unreal WebSocket 싱크, 프레임 형식, 가짜 Unreal 서버
bench/               TTS 벤치, 응답 지연 시뮬레이션, 테스트 문장 세트
tests/               청커·정규화·barge-in·119·브리지·SSE 테스트
```

## 실행

1. 의존성을 설치한다: `pip install -e ".[dev]"`
2. 테스트를 실행한다: `pytest`
3. 지연 시뮬레이션을 본다: `python bench/latency_sim.py --ttft-ms 400 --tps 40`
4. TTS 벤치를 실행한다: `python bench/tts_bench.py --engine mock`
5. 모의 LLM 서버를 띄운다: `python -m voice_pipeline.llm.sse --port 8090`
6. 가짜 Unreal 서버를 띄운다: `python -m voice_pipeline.bridge.fake_unreal --port 8765`

## 새 TTS 엔진 붙이기

1. `src/voice_pipeline/tts/`에 `TTSEngine`을 따르는 클래스를 만든다.
2. `stream(text)`에서 PCM16 모노 조각을 차례로 낸다.
3. `bench/tts_bench.py`의 `ENGINES`에 등록한다.
4. 같은 문장 세트로 TTFA·RTF를 재고 `--save-wav`로 청취 파일을 만든다.

## 주의

- §32 이벤트 필드 이름은 임시다. 정신상담 LLM 문서 §7 원문은 확인하지 않았다. 정본을 받으면 `llm/events.py`와 `llm/sse.py`만 고친다.
- `SAFETY_TEXT_119`는 임시 문구다. 정본 안전 응답 템플릿으로 바꾼다.
- `LocalSpeakerSink`는 오디오 장치가 있는 환경에서 실행을 확인하지 않았다.
- 클라우드 TTS 사용은 조교·지도교수 승인 전까지 실측용으로만 쓴다 (dev-guide §10).
