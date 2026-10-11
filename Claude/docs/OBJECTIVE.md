# OBJECTIVE — 음성 파이프라인 개발 목표 (정본)

> ⚠️ 이 문서는 **고정 정본**이다. 사용자가 명시적으로 요청할 때만 고친다.
> 작업 중 이 문서와 다른 방향이 필요해 보이면, 고치지 말고 사용자에게 먼저 묻는다.
> 근거 문서: `docs/dev-guide.md` (v2, 2026-10-11). 절 번호(§)는 dev-guide 기준이다.
> 아키텍처 정본: `10-ai26-2-ai.md` (설계 v0.3, 2026-09-19). 이 저장소에는 없다.

## 1. 한 줄 목표

HealthUp Station 버츄얼휴먼이 **사용자 발화 종료 → 스피커 첫 음성**을 P50 1.0초(상향 0.9초) 안에 내고, 사용자 끼어들기(barge-in)를 정확하게 처리하는 **전 구간 로컬** 음성 파이프라인을 만든다.

## 2. 담당 범위 (§1)

```text
마이크 → AEC → VAD ─┬─ Smart Turn (운율)
                    ├─ STT (구간 전사) → Kiwi (어미)   ← 턴 종료 판단
                    ↓
        Dialogue Controller (turn_id 발급·무효화, 선행 생성)
                    ↓
     상담 LLM 스트리밍 (§32: meta → token → done, risk_update)
                    ↓
   청커 → 텍스트 정규화 → TTS 어댑터 → PCM 프레임 (turn_id, seq)
                    ↓
       Unreal: 재생 + 립싱크 → 재생 위치 회신
```

- **범위 안**: 마이크 → PCM 프레임 전송, LLM 결합, 메타휴먼(립싱크) 결합.
- **범위 밖**: LLM 파인튜닝(다른 팀), MetaHuman 에셋·Behavior Controller(Unreal 담당).
- 입력 경계: §32 상담 LLM 계약. 출력 경계: §22 Unreal Bridge (메시지는 §7.5).

## 3. 바꾸지 않는 원칙

1. **전 구간 로컬 처리.** 사용자 원음과 상담 텍스트는 외부로 나가지 않는다 (설계서 §2, §35). 클라우드 TTS는 조교·지도교수 승인 시 테스트 문장 비교용으로만 쓴다 (§4.6, §10 #1).
2. **모든 메시지와 오디오 프레임에 turn_id를 붙인다.** 정지 = 현재 turn_id 무효화 (§6.3).
3. **구간을 순서대로 기다리지 않고 겹친다.** STT는 턴 종료 대기와 겹치고, 선행 생성을 쓴다 (§2.3, §3.2).
4. **선행 생성 결과는 턴 확정 전에 재생하지 않는다** (§3.2).
5. **재생 주체(audio clock owner)는 Unreal이다.** 백엔드는 PCM만 보낸다 (§7.4).
6. **엔진은 어댑터 뒤에 둔다.** 엔진을 바꿔도 청커·큐·재생 코드는 바꾸지 않는다. 지연 측정은 어댑터 바깥에서 한다 (§4.5).
7. **판단은 실측으로 한다.** 평균이 아니라 P50·P90으로 본다. 출처 없는 수치는 "확인하지 않았다"로 표기한다 (§2.5, §13).
8. **최종 지연 수치는 운영 PC(Windows 11 + RTX 4090 ×2)에서만 확정한다.** 개발 장비 수치는 기능 확인용이다 (§14).
9. 안전 안내(고위험·119) 음성은 미리 합성한 WAV로 로컬에서 바로 재생한다 (§4.4).

## 4. 실측 1순위 후보 (§0-3)

| 구간 | 1순위 | 비교·예비 | 제외 |
|---|---|---|---|
| STT | SenseVoice-Small INT8 (sherpa-onnx, CPU, VAD 구간 전사) | Qwen3-ASR (정확도 기준점), 스트리밍 Zipformer | 클라우드 STT, Moonshine-tiny-ko, Faster-Whisper |
| 턴 종료 | Silero VAD + Smart Turn v3.2 + Kiwi 어미 규칙 (§3.1 규칙표) | Smart Turn 단독 | — |
| TTS | CosyVoice3 (Fun-CosyVoice3-0.5B-2512) | OmniVoice → (승인 시) Azure | Supertonic 3, Kokoro, F5-TTS (MeloTTS 예비) |
| 립싱크 | Runtime MetaHuman Lip Sync | Audio Live Link, Audio2Face-3D | — |
| AEC | AEC 내장 USB 스피커폰 | WebRTC AEC3 소프트웨어, 반이중 대비책 | — |

- 후보 순위는 실측 결과로만 바꾼다. 바꿀 때는 사용자 확인 후 이 표와 변경 이력을 고친다.

## 5. 합격 기준 (§8.5)

| 지표 | 기본 목표 | 상향 목표 |
|---|---|---|
| 응답 지연 P50 | 1.0초 이하 | 0.9초 이하 |
| 응답 지연 P90 | 1.5초 이하 | 1.2초 이하 |
| 덕킹 지연 P90 | 150ms 이하 | |
| 정지 지연 P90 | 확정 구간 + 50ms 이하 | |
| 맞장구 유지율 | 95% 이상 | |
| TTS RTF (LLM 동시 실행) | 0.5 미만 | |
| 청크 사이 무음 | 0ms | |
| 입모양-소리 차이 | 오디오 45ms 앞섬 ~ 125ms 늦음 안 | ±30ms |
| MetaHuman FPS | 30 이상 | 60 |
| 장시간 구동 | 48시간 메모리 증가 없음 | |

## 6. 고정 인터페이스

- TTS 어댑터 (§4.5, `src/voice_pipeline/tts/base.py`): `warmup()`, `stream(text) -> AsyncIterator[bytes]` (PCM16 모노), `cancel()`. turn_id·seq·20ms 분할은 파이프라인이 붙인다.
- Unreal 브리지 (§7.5, `src/voice_pipeline/bridge/`): `audio_begin` / 바이너리 프레임 `[u32 turn_id][u32 seq][u16 flags] + PCM16 20ms` / `audio_end` / `interrupt` / `avatar_state`, 회신 `playback`(started·progress·stopped).
- 턴 추적 기록 (§8.1, `trace.py`): t0 발화 종료 ~ t6 Unreal 재생 시작.
- §32 LLM 이벤트 필드 이름은 **임시**다. 정본을 받으면 `llm/events.py`, `llm/sse.py`만 고친다.

## 7. 일정 (§9, 2026-10-12 ~ 11-08)

| 주차 | 기간 | 핵심 산출물 |
|---|---|---|
| 1주차 | 10/12~10/18 | TTS 후보(CosyVoice3·OmniVoice) 실측, STT 후보(SenseVoice·Qwen3-ASR) 실측, 청취 테스트, Windows 동작 확인 |
| 2주차 | 10/19~10/25 | STT → 모의 LLM → 청커 → TTS → 로컬 재생 연결, Smart Turn+Kiwi 턴 종료·선행 생성, barge-in 맞장구 필터, AEC 결정, 운영 PC 실행 방식(A·B) 결정 |
| 3주차 | 10/26~11/01 | Unreal 브리지 연결, 립싱크 1안 검증, 베이스 GGUF로 실제 LLM 결합, GPU 1 공존 측정 |
| 4주차 | 11/02~11/08 | P50·P90 튜닝, barge-in 다듬기, 119 정지 경로 시험, 48시간 구동, 회귀 테스트·데모 |

## 8. 팀 결정 대기 (§10)

내가 정하지 않는다. 결정이 나면 STATE.md에 기록하고, 원칙·후보에 영향이 있으면 사용자 확인 후 이 문서를 고친다.

1. 클라우드 TTS 비교 측정 허용 (조교·지도교수)
2. 재생 주체 Unreal (Unreal 담당·조교)
3. 립싱크 방식·플러그인 구매 (Unreal 담당·조교)
4. 감정·제스처 메타데이터 위치 (LLM 담당)
5. 선행 생성 중 risk_update 처리 (조교)
6. 안전 안내 중 barge-in 허용 (조교·지도교수)
7. STT 감정·이벤트 태그를 LLM 요청에 넣을지 (LLM 담당·조교)
8. SenseVoice 모델 라이선스 적용 여부 (조교·지도교수)

## 변경 이력

| 날짜 | 변경 | 요청자 |
|---|---|---|
| 2026-10-11 | 최초 작성 (dev-guide v2 기준) | 사용자 |
