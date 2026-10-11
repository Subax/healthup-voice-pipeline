# 버츄얼휴먼 음성 파이프라인 (STT·TTS·통합) 개발 포인트 v2

> 작성: 2026-10-11 (v2) · 기준 문서: `10-ai26-2-ai.md` (아키텍처 설계 v0.3, 2026-09-19)
> 범위: 마이크 → 음성 인식 (STT) → 상담 LLM 결합 → 청커 → 음성 합성 (TTS) → 재생·립싱크 (lip-sync, 메타휴먼 결합)
> v2: 외부 분석 보고서 「차세대 STT·TTS 모듈 및 통합 아키텍처 분석」을 검토해 반영했다. 검토 결과는 §13에 있다.

## 0. 결론

1. 보고서의 방향(전 구간 로컬 처리, 구간 겹치기, 한국어 어미로 턴 종료 판단)은 이 가이드와 같다. 채택한다.
2. 보고서의 "확정" 판단은 따르지 않는다. 핵심 수치 다수에 출처가 없다. 라이선스 3건과 지연 합계 2건이 틀렸다 (§13).
3. v2 실측 1순위: STT는 SenseVoice-Small, TTS는 CosyVoice3, 턴 종료는 Smart Turn + Kiwi 결합, 립싱크는 Runtime MetaHuman Lip Sync다.

**지금 할 일**: `bench/tts_bench.py`에 CosyVoice3 어댑터를 붙여 첫 실측(TTFA·RTF)을 한다.

**진행 상태**

- ✅ Qwen3-TTS 1차 검증: 속도·한국어 품질 문제 확인
- ✅ 저장소 뼈대: turn_id 취소, 청커, 정규화, 모의 LLM·TTS, Unreal 브리지 (`healthup-voice-pipeline`)
- ✅ 외부 분석 보고서 검토 (§13)
- 🔄 TTS·STT 후보 실측 (벤치 스크립트만 있음)
- ⏭ 하이브리드 턴 종료 감지, AEC, 립싱크 결합

### v1 → v2 변경

| 절 | 변경 |
|---|---|
| §2.3 | 구간 합산표를 시간축표로 바꿨다. STT를 턴 종료 대기와 겹친다 |
| §3.1 | Kiwi 형태소 트랙과 동적 대기 규칙표를 추가했다 |
| §3.3 | SenseVoice-Small을 STT 1순위 실측 후보로 올렸다. Moonshine-tiny-ko를 제외했다 |
| §4 | CosyVoice3 한국어 지원을 공식 모델 카드로 확인했다. Kokoro·F5-TTS를 제외했다. 클라우드 TTS는 승인 시 비교용으로 내렸다 |
| §5.5 | GPU 1 공존 조건(vLLM 메모리 선점, 동시 디코딩)을 추가했다 |
| §6.6 | barge-in 지표를 덕킹 지연과 정지 지연으로 나눴다 |
| §7.6 | 립싱크 1순위를 Runtime MetaHuman Lip Sync로 바꿨다 |
| §8.5 | 합격 기준에 상향 목표, 맞장구 유지율, 장시간 구동을 추가했다 |
| §13·§14 | 보고서 검토 결과와 개발 장비(MacBook·ATOM) 메모를 추가했다 |

---

## 1. 담당 범위와 경계

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

- 그림의 약어: 에코 제거 (AEC, Acoustic Echo Cancellation), 음성 구간 검출 (VAD, Voice Activity Detection).
- 내 범위는 마이크부터 PCM 프레임 전송까지다.
- LLM 결합과 메타휴먼 결합도 내 작업이다.
- 범위 밖: LLM 파인튜닝(다른 팀), MetaHuman 에셋·Behavior Controller(역할표 §33의 Unreal 담당).
- 입력 경계는 §32 상담 LLM 계약이다. 정본(정신상담 LLM 문서 §7) 원문은 확인하지 않았다.
- 출력 경계는 §22 Unreal Bridge다. PCM 스트림 메시지는 §7.5에서 제안한다.
- 핵심 원칙: 모든 메시지와 오디오 프레임에 turn_id를 붙인다. 사용자 끼어들기 (barge-in)의 정확성이 여기서 나온다.

---

## 2. 지연 예산 — 가장 중요한 기준

### 2.1 목표

- 응답 지연 (voice-to-voice latency)은 "사용자 발화 종료 → 스피커 첫 음성"으로 정의한다.
- 10개 언어(한국어 포함) 비교 연구에서 겹침 회피와 침묵 최소화가 공통으로 관찰됐다 (Stivers et al., 2009).
- 사람 대화의 턴 사이 간격은 보통 약 200ms로 소개된다.
- 설계서 목표는 체감 첫 발화 0.7~1.5초(§26), MVP 1.5초 이하(§36)다.
- v2 상향 목표: P50 900ms 이하, P90 1,200ms 이하 (보고서 제안 채택).

### 2.2 설계서 수치의 문제

- §26 구간 목표: VAD 종료 감지 100~250ms, STT 100~300ms, LLM 첫 토큰 지연 (TTFT) 400ms 이하, 첫 청크 200~500ms, 첫 TTS 100~300ms.
- 하한을 더하면 900ms다. 상한을 더하면 1,750ms다.
- §16 PCM 선행 버퍼(100~300ms)를 더하면 1,000~2,050ms다.
- 상한끼리 더하면 목표 1.5초를 넘는다. 그래서 구간을 순서대로 기다리지 않고 겹쳐야 한다.
- 보고서의 "기존 설계" 합계(1,950~2,710ms)는 §26 표와 다르다. 이 가이드는 §26을 기준으로 한다.

### 2.3 시간축 예산 (설계 목표값, 실측 전)

| 시각 (발화 끝 기준) | 사건 | 근거 |
|---:|---|---|
| 0ms | 사용자 발화 끝 | |
| 약 100ms | VAD가 멈춤 보고 | 최소 침묵 설정값. 실측으로 정한다 |
| 약 200ms | Smart Turn·STT·Kiwi 결과 도착 → 선행 생성 시작 | 세 작업을 병렬로 실행 (§3.1) |
| 250ms | 턴 확정 (종결 어미 + 높은 완료 확률) | 동적 대기 (§3.1) |
| 600ms | LLM 첫 토큰 | TTFT 400ms (§32), 선행 생성 시작 기준 |
| 800ms | 첫 청크 완성 | 200ms (§2.4) |
| 950ms | TTS 첫 오디오 | 150ms (CosyVoice3 주장 최저값) |
| 1,000ms | 스피커 첫 소리 | 출력 버퍼 50ms |

- 이 경로의 응답 지연은 약 1,000ms다.
- 상향 목표 900ms에 닿으려면 100ms를 더 줄여야 한다.
- 줄일 후보: TTFT(프롬프트 접두부 캐시), 첫 청크 길이, TTS 토큰 직접 입력 (§4.4).
- STT 최종 전사는 턴 종료 대기와 겹친다. 그래서 별도 구간으로 더하지 않는다.
- 연결 어미·머뭇거림으로 끝난 발화는 대기가 길다. P90은 이 발화들이 정한다.
- 모든 값은 가정이다. 1주차 실측으로 바꾼다.

### 2.4 첫 청크 대기는 계산할 수 있다

- 첫 청크 대기 = 첫 청크 토큰 수 ÷ LLM 디코딩 속도.
- 예: 10토큰 ÷ 40 tok/s = 250ms다. 12토큰 ÷ 30 tok/s = 400ms다.
- 디코딩 속도는 가정값이다. 27B Q4_K_M 실측이 필요하다.
- 결론: 첫 청크 최소 길이를 줄이면 응답 지연이 바로 줄어든다.

### 2.5 판단 기준

- 평균 대신 P50과 P90 (백분위수, percentile)을 함께 본다.
- 가끔 생기는 긴 지연은 평균에 묻힌다.

---

## 3. 턴 종료 감지와 STT

### 3.1 턴 종료 감지 (end-of-turn detection): 운율 + 어미 결합

- 침묵을 기다리는 시간은 그대로 응답 지연이 된다.
- 너무 짧게 기다리면 사용자의 말을 중간에 자른다.
- 그래서 오디오 운율과 문장 끝 어미를 함께 본다.

구성 절차:

1. VAD(Silero VAD)로 말 멈춤을 찾는다.
2. 멈춤마다 Smart Turn v3.2를 실행해 완료 확률을 낸다.
3. 같은 시점에 STT로 직전 구간을 전사한다 (§3.3).
4. Kiwi로 전사 끝 어절의 품사를 본다.
5. 아래 규칙표로 침묵 대기 시간을 정한다.
6. 대기 중 말이 다시 시작되면 판단을 버린다.
7. 대기가 끝나면 턴을 확정한다.

Smart Turn v3.2 확인 사항:

- 23개 언어를 지원하고 한국어가 포함된다.
- 전사가 아니라 오디오의 운율·속도·억양을 본다.
- 입력은 현재 턴의 최근 8초, 16kHz 모노 PCM이다.
- Pipecat Cloud 표준 인스턴스에서 추론 약 65~70ms가 보고됐다.
- 라이선스는 BSD-2다.

Kiwi (한국어 형태소 분석기) 확인 사항:

- 품사 태그에 EF(종결 어미), EC(연결 어미), JX(보조사), JKS(주격 조사), IC(감탄사)가 있다.
- 평가 도구 기준 1줄당 약 1~2ms가 걸렸다 (Kiwi README). 지연 영향은 작다.
- 라이선스 표기가 다르다. Kiwi GitHub README는 LGPL v3, kiwipiepy PyPI는 Apache-2.0이다. 배포 전에 확인한다.
- kiwipiepy는 Linux x86-64·aarch64, macOS arm64, Windows x86-64 휠이 있다.

동적 대기 규칙표 (가설. 값은 실측으로 정한다):

| 전사 끝 | 예 | Smart Turn 완료 확률 | 침묵 대기 |
|---|---|---|---:|
| 종결 어미 (EF) | -요, -다, -까, -죠, -네 | 0.7 이상 | 150~200ms |
| 종결 어미 (EF) | 위와 같음 | 0.7 미만 | 300ms |
| 연결 어미 + 요 | -는데요, -고요, -거든요 | 0.7 이상 | 200~300ms |
| 연결 어미 (EC) | -고, -서, -는데, -면 | 무관 | 600~700ms |
| 조사 (JX·JK) | -는, -를, -가, -도 | 무관 | 650~800ms |
| 감탄사·머뭇거림 (IC) | 음, 어, 그, 저 | 무관 | 800ms |
| 전사 없음 | STT 결과 없음 | 무관 | Smart Turn 단독 규칙 |

보고서 규칙표에서 고친 점:

- "연결 어미 + 요" 행을 추가했다. 존댓말은 연결 어미 뒤 "요"로 문장을 자주 끝낸다.
- 보고서 표대로면 이 발화는 긴 대기로 분류된다. 응답이 늦어진다.
- Kiwi가 "-는데요"를 EF로 낼지 EC+JX로 낼지는 확인하지 않았다. 두 경우를 모두 규칙에 넣는다.
- 반말의 "-는데", "-고"도 문장 끝이 될 수 있다. 그래서 연결 어미 규칙은 대기만 늘린다.
- 최대 침묵 상한에 닿으면 턴을 끝낸다. 상한은 실제 사용자 발화로 정한다 (후보 800~1,000ms).
- STT가 문장 끝 어미를 틀리면 규칙도 틀린다. 어미 인식 정확도를 따로 잰다.

### 3.2 선행 생성 (preemptive generation)

- 정의: 턴 종료가 확정되기 전에 LLM 생성을 시작한다.
- LiveKit 기본 동작은 LLM만 먼저 실행하고 TTS는 턴 확정까지 기다린다.
- 대가: 버려지는 생성이 늘어난다.
- 이 시스템은 사용자가 말하는 동안 GPU 1(LLM)이 쉰다. 그래서 선행 생성 비용이 작다.

> ⚠️ 경고: 선행 생성 결과는 턴 확정 전에 재생하지 않는다. 선행 생성 중 받은 risk_update 처리 방식은 조교와 정한다 (§10).

절차:

1. 종결 어미이고 완료 확률이 높으면, 결과 도착 시점(약 200ms)에 LLM 요청을 보낸다.
2. 사용자가 다시 말하면 그 요청을 취소한다.
3. 턴이 확정되고 최종 전사가 같으면 결과를 채택한다.
4. 최종 전사가 다르면 결과를 버리고 다시 요청한다.

TTS 선행 합성 (보고서 제안):

- 보고서는 TTS까지 미리 합성해 큐에 격리 보관한다. 턴 확정 시 바로 내보낸다.
- 이득은 턴 확정이 첫 청크보다 늦을 때만 생긴다. 대기 600ms 이상 규칙이 해당한다.
- 종결 어미 경로(턴 확정 250ms, 첫 청크 800ms)에서는 이득이 없다.
- 그래서 설정값으로 두고 실측으로 켤지 정한다.

### 3.3 STT 후보

| 후보 | 장점 | 위험 |
|---|---|---|
| **SenseVoice-Small INT8, sherpa-onnx (추천: 먼저 실측)** | 한국어 포함. 비자기회귀 (NAR) 구조라 빠르다 (10초 오디오 70ms 주장, 하드웨어 미표기). 감정·오디오 이벤트(기침, 웃음, 울음) 태그. sherpa-onnx는 Windows·macOS·Linux arm64 빌드 제공 | 스트리밍 모델이 아니다. VAD 구간 단위로 전사한다. 한국어 정확도 수치는 공개 자료에서 찾지 못했다. 모델 라이선스는 FunASR MODEL_LICENSE다 (아래) |
| Qwen3-ASR 0.6B/1.7B | 한국어 정확도 기준점. 한 모델로 스트리밍·오프라인 처리. Apache-2.0 | 스트리밍은 vLLM 백엔드 전용. vLLM의 Windows 네이티브 동작은 확인하지 않았다. GPU를 상시 점유한다 |
| sherpa-onnx 스트리밍 Zipformer (한국어) | 실제 스트리밍 부분 결과. CPU 동작 | 커뮤니티 174M 모델의 스트리밍 오류율 7.53% |
| 클라우드 STT (CLOVA Speech, Azure) | 운영 부담이 적다 | 사용자 원음이 외부로 나간다 → 제외 |

SenseVoice 모델 라이선스 (FunASR MODEL_LICENSE):

- 사용·수정·공유를 허용한다.
- 출처·저자 표기와 모델 이름 유지가 의무다.
- 상업 이용은 명시적으로 다루지 않는다.
- "참고·학습 목적" 문구가 있다. 산학 데모 범위에서 문제가 되는지 조교에게 확인한다.

SenseVoice 운용 규칙:

1. VAD 멈춤마다 직전 멈춤 이후 구간만 전사한다.
2. 턴 전체를 매번 다시 전사하지 않는다.
3. 구간 결과를 이어 붙여 턴 전사로 쓴다.

- 이유: CPU RTF가 0.1이면 5초 발화 재전사에 약 500ms가 걸린다. 지연 예산을 넘는다.
- 대가: 구간 경계에서 정확도가 떨어질 수 있다. 확인하지 않았다.
- 1주차에 구간 전사와 턴 전체 전사를 같은 녹음으로 비교한다.
- 정확도 기준점은 Qwen3-ASR 오프라인 전사로 둔다.

제외한 후보:

- Moonshine-tiny-ko: 한국어 레거시 모델은 Moonshine Community License(비상업)다. 한국어 스트리밍 모델은 MIT로 표기되나 성능은 확인하지 않았다. 재검토 후보로 둔다.
- Faster-Whisper·WhisperLiveKit: 보고서는 무음 구간 환각을 위험으로 든다. 이 주장은 확인하지 않았다. 라이선스는 WhisperLiveKit Apache-2.0, SimulStreaming MIT(GitHub 표기)다. 우선순위가 낮아 이번 실측에서 뺀다.

### 3.4 STT 자원 배치

- SenseVoice를 CPU에서 돌리면 v1의 GPU 배치 문제가 사라진다.
- 대신 CPU 경합이 생긴다.
- 같은 CPU를 쓰는 작업: Unreal 게임·렌더 스레드, 립싱크 플러그인, AEC·VAD, STT, Kiwi, 파이썬 제어.

절차:

1. sherpa-onnx 스레드 수를 고정한다 (후보 2).
2. STT 실행 중 Unreal 프레임 시간을 잰다.
3. 프레임 시간이 흔들리면 SenseVoice를 GPU 1로 옮긴다.
4. Qwen3-ASR을 쓰게 되면 v1 기준(GPU 1 합계 18.2GB + 2GB)을 다시 본다.

---

## 4. TTS 선택

### 4.1 현재 Qwen3-TTS가 느린 이유

- 설계서 §13.4의 `generate_custom_voice` 호출은 청크 전체를 합성한 뒤 반환한다.
- 이 경우 첫 오디오 지연 (TTFA, Time To First Audio)은 청크 전체 합성 시간이다.
- Qwen3-TTS가 주장하는 첫 패킷 97ms는 스트리밍 경로 기준이다.
- 한국어 품질 문제는 스트리밍과 관계가 없다. 그래서 대안 검토는 타당하다.

### 4.2 로컬 후보 비교

| 항목 | CosyVoice3 (Fun-CosyVoice3-0.5B-2512) | OmniVoice | Qwen3-TTS |
|---|---|---|---|
| 한국어 | 공식 모델 카드 9개 언어에 포함. 한국어 CER 공개값 없음 | 모델 카드는 "600개 이상 언어"라고만 쓴다. 한국어 CER 3.22 (Supertone 집계) | 한국어 CER 4.07 (Supertone 집계). 1차 검증에서 품질 문제 |
| 스트리밍 | 입력 텍스트·출력 오디오 양방향. 최저 150ms 주장 (조건 미표기) | 모델 카드에 스트리밍 언급 없음 → 문장 단위 입력으로 본다 | 스트리밍 경로 첫 패킷 97ms 주장 |
| 속도 | RTF 공개값은 확인하지 않았다 | RTF 최저 0.025 주장 (하드웨어 미표기) | 확인하지 않았다 |
| 감정 제어 | 자연어 지시(감정·속도·음량). `[breath]` 같은 태그 | 확인하지 않았다 | 확인하지 않았다 |
| 가속 런타임 | 로드맵의 vLLM 지원은 CosyVoice2만 명시. TensorRT-LLM(Triton)은 대상 버전 미표기 | 확인하지 않았다 | 확인하지 않았다 |
| 플랫폼 | 설치 문서는 Linux(conda, Python 3.10). ttsfrd 휠은 Linux x86_64 전용, 없으면 wetext 사용. Windows·macOS 문서 없음 | CUDA 예시와 Apple Silicon 설치 절이 있다 | 확인하지 않았다 |
| 라이선스 | Apache-2.0 | Apache-2.0 | Apache-2.0 |

- CER은 문자 오류율 (Character Error Rate)이다. 낮을수록 좋다. 자연스러움 지표는 아니다.
- 보고서의 "CosyVoice3 한국어 발음 오류 60% 개선"과 "VRAM 2.5~3.0GB"는 출처를 찾지 못했다.

### 4.3 제외 후보

| 후보 | 제외 이유 |
|---|---|
| Supertonic 3 | 라이선스(BigScience Open RAIL-M) 부속서 A (l)항이 "의료 조언 및 의료 결과 해석" 용도를 금지한다. 해당 여부는 법적 판단이 필요하다. 공식 저장소는 2026-07-23 공지로 아카이브된다 |
| Kokoro-82M | VOICES.md의 9개 언어에 한국어가 없다 |
| F5-TTS | 보고서 기준 공개 가중치가 CC-BY-NC(비상업)다. 원문은 이번에 확인하지 않았다 |
| MeloTTS | 예비 후보로만 둔다. 보고서 기준 한국어 지원·MIT다. "음색이 단조롭다"는 평가는 청취로 확인한다 |

### 4.4 선택지와 추천

**1안 (추천): CosyVoice3**

- 양방향 스트리밍이 LLM 토큰 스트림 구조와 맞는다.
- 자연어 지시로 상담 톤(차분하게, 공감하는 어조)을 줄 수 있다.
- 위험: Windows 동작 문서가 없다. 운영 PC에서는 WSL2(§7.3 B안)가 필요할 수 있다.
- 위험: CosyVoice3의 vLLM 가속 지원은 확인하지 않았다.
- 위험: 한국어 자연스러움이 검증되지 않았다.
- 위험: GPU 1에서 LLM과 경합한다 (§5.5).

**2안: OmniVoice**

- RTF가 낮다고 주장한다.
- 입력 스트리밍이 없어도 영향은 첫 청크 길이만큼이다. 청커가 이미 문장 단위로 끊는다.
- 위험: 스트리밍 출력 여부를 확인하지 않았다. 청크 전체 합성이면 TTFA = 청크 합성 시간이다.

**3안: 클라우드 TTS (Azure)**

- 조교·지도교수가 승인할 때만 비교 측정용으로 쓴다.
- 이유: 설계서 §2(외부 전송 방지)·§35(외부 AI API 금지)와 충돌한다.

두 가지 입력 경로를 모두 잰다 (CosyVoice3):

1. 청커 경로: 청커가 만든 청크를 넣는다.
2. 토큰 직접 입력 경로: LLM 토큰을 TTS 입력 스트림에 바로 넣는다.

- 2번은 청커 대기(§2.4)를 TTS 안으로 옮긴다. 이득과 억양 품질을 함께 본다.

결정 규칙:

1. 같은 문장 세트로 후보를 모두 잰다 (TTFA, RTF, 청취 점수).
2. LLM 디코딩과 동시에 실행한 조건에서도 잰다 (§5.5).
3. 로컬이 청취 기준을 넘으면 로컬로 확정한다.
4. 로컬이 모두 기준 미달이면 승인 결과를 보고 클라우드를 검토한다.

추가 주의:

- 안전 안내(고위험·119) 음성은 확정 TTS로 미리 합성해 WAV로 둔다. GPU 상태와 무관하게 바로 재생된다.
- OmniVoice·CosyVoice3 음성 복제를 쓰면 당사자 동의를 받은 참조 음성만 쓴다.
- TTS 확정 후 설계서 §5.2(VRAM), §13(TTS 큐), §35(코딩 에이전트 제약)를 갱신한다.

### 4.5 TTS 어댑터 인터페이스

구현: 저장소 `src/voice_pipeline/tts/base.py`.

```python
class TTSEngine(Protocol):
    name: str
    sample_rate: int

    async def warmup(self) -> None: ...                          # 모델 로드, 예열
    def stream(self, text: str) -> AsyncIterator[bytes]: ...     # PCM16 모노 조각
    async def cancel(self) -> None: ...                          # 진행 중 합성 중단
```

- 엔진은 PCM 조각만 낸다. turn_id·seq·20ms 분할은 파이프라인이 붙인다.
- 엔진을 바꿔도 청커·큐·재생 코드는 바꾸지 않는다.
- 구현 순서: `CosyVoiceTTS` → `OmniVoiceTTS` → (승인 시) `AzureTTS`.
- 지연 측정은 어댑터 바깥에서 한다. 엔진마다 측정 방식이 달라지지 않는다.

### 4.6 클라우드 TTS 참고 (승인 시에만)

| 항목 | Azure TTS | CLOVA Voice |
|---|---|---|
| 출력 스트리밍 | 지원 | 공식 문서에서 찾지 못했다. REST로 mp3/wav 반환 |
| 입력 텍스트 스트리밍 | 지원 (Speech SDK, WebSocket v2). TextStream은 rate 미적용 → SSML 스트림 사용 | 없음. 1회 최대 2,000자 |
| 립싱크 보조 | ko-KR은 viseme ID만. 단어 타임스탬프 제공 | 문서에서 찾지 못했다 |
| 데이터 정책 | MS 문서: 입력 텍스트·출력 오디오를 MS 로그에 저장하지 않음 | 실시간 호출만 허용. 생성 파일 저장·재사용 불가 |
| 비용 | 월 50만 자 무료. Neural 100만 자당 약 $15 (제3자 집계, 2026-10-08) | 호출이 없어도 기본료 발생. 단가는 확인하지 못했다 |

클라우드를 쓸 때의 규칙:

1. 연결을 미리 열고 재사용한다 (Azure 공식 권장 사항).
2. 출력은 원시 PCM으로 받는다. MP3는 디코딩 지연과 앞뒤 패딩이 생긴다.
3. 첫 오디오 타임아웃을 둔다.
4. 타임아웃이 나면 그 턴 전체를 로컬 엔진으로 넘긴다.
5. 안전 안내 음성은 항상 로컬에서 낸다.
6. 승인 전에는 테스트 문장으로만 측정한다. 실제 상담 텍스트는 보내지 않는다.

---

## 5. 청커·텍스트 정규화·오디오 출력·GPU

### 5.1 설계서 SemanticChunker(§12)의 문제

1. 소수점에서 끊긴다. "오늘 측정하신 체온은 36." (15자)에서 끊기고 "5도입니다."는 따로 읽힌다.
2. 강제 분할(40자)이 어절 중간을 자른다.
3. 첫 청크도 최소 15자를 기다린다. 첫 오디오가 늦어진다.
4. `end_marks`에 "?"와 "!"가 두 번 들어 있다.

### 5.2 개선 규칙 (구현 완료: `src/voice_pipeline/chunker.py`, 테스트 포함)

- 첫 청크만 첫 쉼표에서도 끊는다. 최소 길이는 실측으로 정한다 (후보 6~12자).
- 이후 청크는 문장 끝에서만 끊는다. 청크마다 억양이 새로 시작되기 때문이다.
- "숫자." 다음 토큰이 숫자면 끊지 않는다.
- 강제 분할은 마지막 공백에서 한다.

### 5.3 텍스트 정규화 (구현 완료: `src/voice_pipeline/normalize.py`)

| LLM 출력 | TTS 입력 (예시) | 처리 대상 |
|---|---|---|
| 120/80mmHg | 120에 80 | 슬래시, 단위 |
| 126mg/dL | 126 밀리그램 퍼 데시리터 | 단위 |
| 36.5℃ | 36.5도 | 기호 |
| 25.3% | 25.3 퍼센트 | 기호 |
| 3~4회 | 3에서 4회 | 물결표 |
| `**주의**`, `- 항목` | (기호 제거) | 마크다운 |

- 숫자는 엔진이 바르게 읽는지 먼저 시험한다. 기호와 단위는 항상 바꾼다.
- CosyVoice3는 ttsfrd가 없으면 wetext로 정규화한다. 우리 정규화를 먼저 적용해 엔진 차이를 줄인다.
- 읽기 규칙(예: 혈압 "120에 80")은 조교·상담 자문과 맞춘다.
- LLM 담당에게 요청한다: 마크다운·목록·이모지 없이 말하듯 쓴다.
- LLM 담당에게 요청한다: 첫 문장을 짧게 쓴다.

### 5.4 큐·버퍼·출력

- 3-slot 큐(§13)는 유지한다. LLM 역압 (backpressure)에 쓴다.
- §14 적응형 배치는 C2 완료를 C3 완료까지 늦춘다. TTS RTF가 충분히 낮으면 배치를 쓰지 않는다.
- §16 선행 버퍼 100~300ms는 응답 지연에 그대로 더해진다. 첫 청크는 40~60ms부터 실측한다.
- 오디오는 16-bit 모노 원시 PCM, 20ms 프레임으로 보낸다.
- 리샘플링은 한 곳에서 한 번만 한다.
- 버퍼 고갈 (underrun) 횟수를 기록한다 (§27).

### 5.5 GPU 1 공존 조건 (LLM + TTS)

- 보고서 배치: GPU 1에 LLM 18.2GB와 CosyVoice3를 함께 둔다.
- CosyVoice3 VRAM(보고서 2.5~3.0GB)은 확인하지 않았다.
- 보고서 값이면 합계 약 21GB로 24GB 안에 든다.

> ⚠️ 경고: vLLM은 기본으로 GPU 메모리의 대부분(`gpu_memory_utilization` 0.9)을 미리 잡는다. llama.cpp LLM과 같은 GPU에 두면 이 값을 낮춘다.

측정 절차:

1. LLM만 실행해 tok/s를 잰다.
2. TTS만 실행해 RTF를 잰다.
3. LLM 디코딩 중 TTS를 실행해 두 값을 다시 잰다.
4. 동시 실행 TTS RTF가 0.5 미만(§28)인지 확인한다.
5. 미달이면 TTS를 GPU 0으로 옮기는 안을 Unreal 프레임 시간과 함께 잰다.

---

## 6. barge-in 설계

### 6.1 전제: AEC

- AEC가 없으면 아바타 목소리가 마이크로 들어온다. 그러면 아바타가 스스로 말을 끊는다.
- 설계서 §5.1의 "USB 마이크 또는 지향성 마이크"는 AEC를 보장하지 않는다.

| 안 | 방식 | 판단 |
|---|---|---|
| **1안 (추천)** | AEC 내장 USB 스피커폰을 스피커 겸 마이크로 쓴다 | 장치가 재생 신호를 직접 안다. OS와 무관하다 |
| 2안 | 소프트웨어 AEC (WebRTC AEC3, `pywebrtc-audio`) | 기준 신호는 스피커로 나가는 순간의 오디오여야 한다. Unreal이 재생하면 시스템 루프백으로 받아야 한다 |
| 3안 | 반이중 (half-duplex): 말하는 동안 마이크 무시 + 화면 "잠깐" 버튼 | 대비책이다. 음성 barge-in은 포기한다 |

- `pywebrtc-audio`는 Linux·macOS·Windows(x86_64) 휠을 제공한다.
- AEC 성능은 실제 스테이션 하드웨어에서만 확인할 수 있다.

### 6.2 2단계 barge-in

| 단계 | 조건 | 동작 |
|---|---|---|
| 1. 감지 | VAD가 발화 시작을 잡음 | TTS 음량을 낮춘다 (덕킹, ducking). 아바타가 사용자를 본다. 생성은 계속한다 |
| 2. 확정 | 발화가 250~400ms 이어지고 맞장구가 아님 | 30~50ms 페이드아웃 후 정지한다. turn_id를 무효화한다. LISTENING으로 간다 |
| 2'. 취소 | 기침·잡음·짧은 맞장구 | 음량을 되돌리고 말을 이어 간다 |

- 맞장구 (backchannel) 예: 네, 예, 응, 음, 아.
- 확정 구간 오디오를 SenseVoice로 전사해 맞장구를 거른다.
- SenseVoice 이벤트 태그(기침, 웃음)로 잡음을 거른다. 효과는 확인하지 않았다.
- 기준값(250~400ms, 덕킹 폭)은 테스트 문장 세트로 정한다.

### 6.3 turn_id 기반 취소 (구현 완료: `src/voice_pipeline/turns.py`, `pipeline.py`)

설계서 §24 샘플의 문제:

1. LLM 스트림을 취소하지 않는다. 정지 후에도 청크가 다시 들어온다.
2. 합성 중인 TTS 작업(`to_thread`)은 멈추지 않는다. 지난 오디오가 `audio_queue`로 들어온다.
3. `audio_queue`, 재정렬 버퍼, `expected_seq`를 초기화하지 않는다.
4. `except:`가 `CancelledError`까지 삼킨다.
5. 페이드 없이 끊으면 클릭음이 난다.

수정 원칙:

- 정지를 "현재 turn_id 무효화"로 정의한다.
- 재생·전송 쪽은 무효 turn_id의 프레임을 모두 버린다.
- 그러면 큐를 비우는 시점과의 경쟁 상태 (race condition)가 생기지 않는다.
- 재정렬 버퍼는 turn_id별로 만든다. seq는 턴마다 0부터 시작한다.

### 6.4 대화 기록 자르기

- assistant 메시지에는 사용자가 실제로 들은 부분만 남긴다.
- Unreal이 회신한 재생 위치(`played_ms`)로 마지막 청크를 길이 비율로 자른다.
- "중단됨" 표시를 다음 LLM 요청에 넣을지는 LLM 담당과 정한다.

### 6.5 안전 모드와의 관계

- 119 등급 정지는 barge-in과 같은 정지 경로를 쓴다 (§24, §31).
- 안전 안내 중 barge-in 허용 여부는 조교·지도교수가 정한다.

### 6.6 지표

- 덕킹 지연: 사용자 발화 시작 → 음량 낮춤. 사용자가 느끼는 반응 속도다.
- 정지 지연: 사용자 발화 시작 → 출력 무음. 확정 구간 + 페이드 시간이 하한이다.
- 보고서의 "barge-in 반응 250ms 이하"는 정지 지연으로 보면 확정 구간(250~400ms)과 충돌한다. 덕킹 지연 기준으로 해석한다.
- 오정지율: 맞장구·기침·잡음에 멈춘 비율.
- 맞장구 유지율: 맞장구에서 말을 이어 간 비율.
- 미검출률: 사용자가 끼어들었는데 멈추지 않은 비율.

---

## 7. 메타휴먼 결합과 리눅스 제약

### 7.1 확인된 플랫폼 사실

| 항목 | 확인 내용 |
|---|---|
| MetaHuman Animator | Windows에서만 지원. DirectX 12 필요 |
| MetaHuman Creator | UE 5.7부터 Windows·Mac·Linux 에디터에서 캐릭터 생성 가능. Identity·Performance 에셋 생성은 Mac·Linux에서 비활성 |
| MetaHuman Audio Live Link 소스 | 입력은 Windows·macOS가 지원하는 USB 오디오 캡처 장치. Linux 지원은 문서에 없다 |
| 실시간 소스 부하 | GPU를 많이 쓴다. Live Link Hub는 별도 PC 실행을 권장한다 |
| NVIDIA ACE UE 플러그인 | Linux는 Animation Graph만 지원. 로컬 Audio2Face 실행 플러그인은 Win64 전용 |
| Runtime MetaHuman Lip Sync (서드파티) | Windows·Mac·iOS·Linux·Android·Meta Quest 표기. 입력: 마이크, TTS, 오디오 버퍼, 스트리밍. 모델 3종: Standard(viseme 14개), Realistic(얼굴 컨트롤 81개), Mood-Enabled(무드 12종). 가격은 판매 페이지에 표시되지 않았다 |
| 운영 PC | Windows 11 + RTX 4090 ×2 (설계서 §5·§6 정본) |

### 7.2 Linux에서 할 일과 Windows에서 할 일

- Linux (지금): 백엔드 전체를 만든다. STT, TTS, 청커, barge-in, 측정이 여기에 해당한다.
- Linux에서는 가짜 Unreal 서버(`bridge/fake_unreal.py`)로 같은 인터페이스를 시험한다.
- Windows (2~3주차부터): Unreal 연결, 립싱크, AEC, 최종 지연 측정을 한다.

### 7.3 개발 구성 선택지

| 안 | 구성 | 판단 |
|---|---|---|
| **1안 (추천)** | Linux 개발 장비의 백엔드 ↔ LAN WebSocket ↔ Windows PC의 Unreal | 지금 바로 가능하다. 렌더링 경로가 운영과 같다 |
| 2안 | Linux에서 UE 5.7 에디터 + Linux 지원 립싱크 플러그인 | 렌더링 경로가 운영과 달라진다 |
| 3안 | 개발 PC를 Windows로 듀얼 부팅 | 백엔드의 Windows 동작도 같이 확인한다 |

운영 PC에서 백엔드를 돌리는 방식은 2주차에 정한다:

- A: Windows 네이티브 실행. 크로스플랫폼 의존성만 쓴다.
- B: WSL2 안에서 실행. Linux 전용 의존성(vLLM, CosyVoice3 설치 경로)을 유지한다. 오디오 장치는 Windows 쪽이 소유한다.
- 결정 규칙: 선택한 STT·TTS가 Windows 네이티브로 동작하면 A, 아니면 B로 간다.

### 7.4 재생 주체 (audio clock owner): Unreal

- 백엔드가 스피커를 직접 재생하고 Unreal이 립싱크하면 시계가 둘이 된다.
- 시계가 둘이면 입모양과 소리가 어긋나기 쉽다.
- Unreal이 PCM을 받아 재생하고, 같은 버퍼로 립싱크한다 (예: `USoundWaveProcedural`).
- 립싱크 처리 지연만큼 오디오를 늦춰 입모양과 소리를 맞춘다.
- 허용 기준: ITU-R BT.1359에서 오디오가 45ms 앞서거나 125ms 늦으면 감지되기 시작한다.
- Unreal은 재생 시작·진행·정지를 백엔드에 회신한다.
- 보고서도 같은 구조를 제안한다.

### 7.5 Unreal 브리지 메시지 (구현 완료: `src/voice_pipeline/bridge/`)

```jsonc
// 백엔드 → Unreal
{"type": "audio_begin", "turn_id": 12, "sample_rate": 24000, "channels": 1}
// 바이너리 프레임: [u32 turn_id][u32 seq][u16 flags] + PCM16 20ms
{"type": "audio_end", "turn_id": 12, "last_seq": 57}
{"type": "interrupt", "turn_id": 12, "fade_ms": 40}
{"type": "avatar_state", "state": "thinking", "emotion": "reassuring", "gesture": "small_nod"}

// Unreal → 백엔드
{"type": "playback", "turn_id": 12, "event": "started", "t_ue_ms": 1830551}
{"type": "playback", "turn_id": 12, "event": "progress", "played_ms": 1200}
{"type": "playback", "turn_id": 12, "event": "stopped", "played_ms": 2140, "reason": "interrupt"}
```

- PCM은 바이너리 프레임으로 보낸다. base64는 크기를 약 33% 늘린다.
- 감정·제스처를 LLM 스트림의 어디에 담을지는 정해지지 않았다 (§10).

### 7.6 립싱크 방식 선택지

| 안 | 방식 | 판단 |
|---|---|---|
| **1안 (추천: 먼저 검증)** | Runtime MetaHuman Lip Sync | 오디오 버퍼·스트리밍을 직접 받는다. Unreal 재생 버퍼와 같은 PCM을 쓴다. Mood-Enabled 모델로 감정 상태와 연결된다. 위험: 가격 미표시, UE 5.7 호환 미확인(데모는 5.6 이상), 처리 지연 수치 없음, CPU·GPU 사용 미표기 |
| 2안 | UE 공식 실시간 오디오 애니메이션 (Audio Live Link) | 무료다. 입력이 오디오 장치라서 가상 오디오 장치 경로가 필요할 수 있다. GPU 부하가 있다 |
| 3안 | NVIDIA Audio2Face-3D | 로컬 실행 플러그인은 Win64 전용이다. GPU 부하가 있다 |

- 보고서의 "처리 지연 5ms 이하", "VRAM 0MB", "CPU SIMD 처리"는 판매 페이지에서 확인하지 못했다. 판매 페이지는 수치 없이 "zero-latency"라고만 쓴다.
- 세 안 모두 백엔드 출력(§7.5 PCM 스트림)은 같다. 선택이 늦어져도 백엔드 작업은 막히지 않는다.

1안 검증 절차:

1. 가격과 라이선스를 조교와 확인한다.
2. UE 5.7 프로젝트에 설치한다.
3. 같은 PCM을 재생하면서 화면을 녹화한다.
4. 녹화 파형과 입모양으로 처리 지연을 잰다.
5. 립싱크 실행 중 GPU 0 프레임 시간과 CPU 사용률을 잰다.

### 7.7 사람처럼 보이게 하는 반응

- 턴 종료 직후 100~200ms 안에 비언어 반응(끄덕임, 숨쉬기, 생각하는 표정)을 보인다.
- 음성이 나오기 전에 "들었다"는 신호가 먼저 간다.
- THINKING 상태 메시지는 턴 확정 시점에 바로 보낸다.
- 미리 녹음한 추임새("네,")는 반복되면 기계적으로 들린다. 상담 문맥에서는 신중히 쓴다.

---

## 8. 측정·검증

### 8.1 턴 추적 기록 (구현 완료: `src/voice_pipeline/trace.py`)

turn_id마다 아래 시각을 단조 시계 (monotonic clock)로 남긴다:

1. t0: 사용자 발화 종료 (VAD)
2. t1: 턴 확정
3. t2: STT 최종 전사
4. t3: LLM 첫 토큰
5. t4: 첫 청크 완성
6. t5: TTS 첫 오디오 프레임
7. t6: Unreal 재생 시작 (회신값을 왕복 지연으로 보정)

### 8.2 외부 실측

1. 마이크 입력과 스피커 출력을 함께 녹음한다.
2. 파형에서 "사용자 발화 끝 → 아바타 음성 시작" 간격을 잰다.
3. 로그 값과 비교해 기록되지 않은 지연을 찾는다.

### 8.3 테스트 세트

TTS 문장 세트 (`bench/sentences_ko.txt`, 30~50개 목표):

- 짧은 대답, 수치가 들어간 문장, 상담 발화, 긴 다절 문장, 질문 억양

STT·턴 종료 녹음 세트 (팀원 녹음, 신규):

- 수치 말하기 (혈압, 혈당, 체온)
- 존댓말 "연결 어미 + 요"로 끝나는 발화 (-는데요, -거든요)
- 중간에 쉼이 긴 발화, 머뭇거림 (음, 어)
- barge-in 시나리오 (맞장구, 말 정정, 기침)

### 8.4 모의 LLM 서버 (구현 완료: `src/voice_pipeline/llm/`)

- §32 계약(meta → token → done, risk_update)을 흉내 낸다. 필드 이름은 임시다.
- 파인튜닝 모델이 오기 전에 LLM 담당에게 베이스 모델 GGUF(같은 양자화)를 받는다.
- 베이스 모델로 실제 TTFT와 디코딩 속도를 잰다.
- 측정에는 대표 프롬프트(시스템 프롬프트 + 건강 맥락 + 대화 기록)를 쓴다.

### 8.5 합격 기준 (제안)

| 지표 | 기본 목표 | 상향 목표 |
|---|---|---|
| 응답 지연 P50 | 1.0초 이하 | 0.9초 이하 |
| 응답 지연 P90 | 1.5초 이하 (§36) | 1.2초 이하 |
| 덕킹 지연 P90 | 150ms 이하 | |
| 정지 지연 P90 | 확정 구간 + 50ms 이하 | |
| 맞장구 유지율 | 95% 이상 | |
| TTS RTF (LLM 동시 실행) | 0.5 미만 (§28) | |
| 청크 사이 무음 | 0ms (§26) | |
| 입모양-소리 차이 | 오디오 45ms 앞섬 ~ 125ms 늦음 안 (ITU 감지 한계) | ±30ms (보고서 제안, 근거 미확인) |
| MetaHuman FPS | 30 이상 (§26) | 60 |
| 장시간 구동 | 48시간 메모리 증가 없음 | |

---

## 9. 4주 일정 (2026-10-12 ~ 11-08)

**1주차 (10/12~10/18) 🔄**

1. ✅ 턴 추적 기록과 모의 LLM 서버를 만든다.
2. CosyVoice3·OmniVoice를 같은 문장 세트로 잰다 (청커 경로, 토큰 직접 입력 경로).
3. SenseVoice-Small·Qwen3-ASR을 녹음 세트로 잰다 (CER, 구간 전사 지연).
4. 팀원 청취 테스트를 한다.
5. 선택 후보가 Windows에서 동작하는지 확인한다.

**2주차 (10/19~10/25) ⏭**

1. STT → 모의 LLM → 청커 → TTS → 로컬 재생을 잇는다.
2. Smart Turn + Kiwi 하이브리드 턴 종료 감지와 선행 생성을 붙인다.
3. turn_id 기반 barge-in에 STT 맞장구 필터를 붙인다.
4. AEC 장비와 방식을 정한다.
5. 운영 PC 백엔드 실행 방식(A·B)을 정한다.

**3주차 (10/26~11/01) ⏭**

1. Unreal 브리지(§7.5)를 Unreal 담당과 연결한다.
2. 립싱크 1안을 검증 절차(§7.6)로 시험하고 확정한다.
3. 베이스 모델 GGUF로 실제 LLM을 결합한다.
4. GPU 1 공존 조건(§5.5)을 잰다.

**4주차 (11/02~11/08) ⏭**

1. P50·P90 응답 지연을 튜닝한다.
2. 2단계 barge-in과 맞장구 처리를 다듬는다.
3. 안전 모드(119) 정지 경로를 시험한다.
4. 48시간 구동 시험을 시작한다.
5. 회귀 테스트와 데모를 준비한다.

---

## 10. 팀에 확인할 결정 사항

| # | 결정 사항 | 결정자 |
|---|---|---|
| 1 | 클라우드 TTS를 비교 측정용으로라도 쓸지 (외부로 나가는 것: 테스트 문장) | 조교·지도교수 |
| 2 | 재생 주체를 Unreal로 할지 (§7.4) | Unreal 담당·조교 |
| 3 | 립싱크 방식과 서드파티 플러그인 구매 (§7.6) | Unreal 담당·조교 |
| 4 | 감정·제스처 메타데이터 위치 (제안: 토큰 앞 meta 이벤트) | LLM 담당 |
| 5 | 선행 생성 중 risk_update 처리 (§3.2) | 조교 |
| 6 | 안전 안내 중 barge-in 허용 여부 (§6.5) | 조교·지도교수 |
| 7 | STT 감정·이벤트 태그를 LLM 요청에 넣을지 | LLM 담당·조교 |
| 8 | SenseVoice 모델 라이선스("참고·학습 목적" 문구)의 프로젝트 적용 여부 (§3.3) | 조교·지도교수 |

1번 질문 문안:

> 운영은 전부 로컬 TTS로 갈 계획입니다. 비교를 위해 Azure TTS를 테스트 문장(실제 상담 내용 없음)으로만 측정해도 될까요? 설계서 §2·§35와의 충돌 여부를 확인하고 싶습니다.

---

## 11. 확인하지 않은 항목

1. CosyVoice3·OmniVoice의 한국어 자연스러움, VRAM, Windows 동작
2. SenseVoice-Small의 한국어 CER과 CPU 구간 전사 지연
3. Kiwi의 "-는데요"류 분석 결과와 라이선스 표기 차이
4. Runtime MetaHuman Lip Sync의 가격, UE 5.7 호환, 처리 지연
5. vLLM(Qwen3-ASR, CosyVoice3)의 Windows 네이티브 동작
6. Supertonic 라이선스 의료 용도 제한의 해당 여부 (법적 판단)
7. 정신상담 LLM 문서 §7 계약 원문

---

## 12. 참고 자료

**STT·턴 종료**

- [SenseVoice-Small 모델 카드 (한국어 포함, 감정·이벤트 태그)](https://huggingface.co/FunAudioLLM/SenseVoiceSmall) · [FunASR MODEL_LICENSE](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)
- [sherpa-onnx README (SenseVoice zh-en-ja-ko-yue 모델, 플랫폼)](https://github.com/k2-fsa/sherpa-onnx)
- [Qwen3-ASR (한국어, vLLM 스트리밍, Apache-2.0)](https://github.com/QwenLM/Qwen3-ASR)
- [sherpa-onnx 한국어 스트리밍 Zipformer](https://huggingface.co/k2-fsa/sherpa-onnx-streaming-zipformer-korean-2024-06-16) · [커뮤니티 174M 모델](https://huggingface.co/kangkyu/icefall-asr-ko-streaming-zipformer-174m)
- [Moonshine 라이선스 (한국어 레거시 모델 비상업)](https://moonshine-voice.readthedocs.io/en/latest/license/) · [moonshine-tiny-ko](https://huggingface.co/UsefulSensors/moonshine-tiny-ko)
- [SimulStreaming (MIT)](https://github.com/ufal/SimulStreaming) · [WhisperLiveKit (Apache-2.0)](https://github.com/QuentinFuxa/WhisperLiveKit)
- [Smart Turn (23개 언어, 오디오 기반 턴 종료)](https://github.com/pipecat-ai/smart-turn)
- [Kiwi (품사 태그, LGPL v3 표기)](https://github.com/bab2min/Kiwi) · [kiwipiepy PyPI (Apache-2.0 표기, 휠)](https://pypi.org/project/kiwipiepy/)
- [LiveKit 선행 생성](https://docs.livekit.io/agents/build/speech/)
- [pywebrtc-audio (WebRTC AEC3 Python 바인딩)](https://pypi.org/project/pywebrtc-audio/)

**TTS (로컬)**

- [Fun-CosyVoice3-0.5B-2512 (9개 언어, 양방향 스트리밍, Apache-2.0)](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)
- [OmniVoice 모델 카드 (Apache-2.0, RTF 0.025 주장)](https://huggingface.co/k2-fsa/OmniVoice)
- [Qwen3-TTS (스트리밍 97ms 주장, Apache-2.0)](https://github.com/QwenLM/Qwen3-TTS)
- [Kokoro-82M VOICES.md (한국어 없음)](https://huggingface.co/hexgrad/Kokoro-82M/blob/main/VOICES.md)
- [Supertonic README (아카이브 공지, 한국어 CER 표)](https://github.com/supertone-inc/supertonic) · [Supertonic 3 LICENSE](https://huggingface.co/Supertone/supertonic-3/blob/main/LICENSE)
- [CosyVoice3 macOS CPU 실행 (제3자, RTF 0.3~0.5)](https://clawhub.ai/lhuaizhong/cosyvoice3-macos)

**TTS (클라우드)**

- [Azure: 음성 합성 지연 줄이기](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-lower-speech-synthesis-latency)
- [Azure: viseme 지원 로케일](https://github.com/MicrosoftDocs/azure-ai-docs/blob/main/articles/ai-services/speech-service/includes/language-support/viseme.md)
- [Azure: TTS 데이터·개인정보 처리](https://learn.microsoft.com/en-us/legal/cognitive-services/speech-service/custom-neural-voice/data-privacy-security-custom-neural-voice)
- [CLOVA Voice 사용 준비](https://guide.ncloud-docs.com/docs/clovavoice-spec) · [CLOVA Voice 이용 정책](https://guide.ncloud-docs.com/docs/clovavoice-policy)
- [Azure TTS 단가 제3자 집계 (2026-10-08)](https://inventivehq.com/tools/developer/tts-stt-api-cost-calculator)

**MetaHuman·Unreal**

- [MetaHuman 하드웨어·플랫폼 요구사항](https://dev.epicgames.com/documentation/metahuman/hardware-requirements)
- [MetaHuman Audio Live Link 소스](https://dev.epicgames.com/documentation/metahuman/using-a-metahuman-audio-source-in-unreal-engine)
- [MetaHuman 실시간 애니메이션 (GPU 부하, Live Link Hub)](https://dev.epicgames.com/documentation/metahuman/realtime-animation-for-metahumans-in-unreal-engine)
- [Runtime MetaHuman Lip Sync 판매 페이지 (플랫폼, 모델 3종)](https://www.fab.com/listings/b514294e-e78b-4b8b-ad21-78ce51dc7e8c) · [포럼 글](https://forums.unrealengine.com/t/runtime-metahuman-lip-sync-real-time-offline-lip-sync-with-audio-tts-ai-for-npc-cc4-genesis-arkit-and-more-facial-animations/2383289)
- [NVIDIA ACE Unreal 플러그인 변경 이력](https://docs.nvidia.com/ace/ace-unreal-plugin/latest/ace-unreal-plugin-changelog.html) · [로컬 실행 Linux 미지원 (포럼)](https://forums.developer.nvidia.com/t/ace-linux-support-for-nvaudio2facemark-plugin-audio2face-3d-local-execution/348812)
- [Unreal Linux 개발 요구사항](https://dev.epicgames.com/documentation/en-us/unreal-engine/linux-development-requirements-for-unreal-engine)

**장비**

- [GIGABYTE AI TOP ATOM 사양 (GB10, 통합 메모리, DGX OS)](https://www.gigabyte.com/th/AI-TOP-PC/GIGABYTE-AI-TOP-ATOM/sp)

**지각·대화 연구**

- Stivers et al. (2009). Universals and cultural variation in turn-taking in conversation. PNAS 106(26). doi:10.1073/pnas.0903616106
- [ITU-R BT.1359 오디오·영상 동기 감지 한계 (요약)](https://en.wikipedia.org/wiki/Audio-to-video_synchronization)

---

## 13. 외부 분석 보고서 검토 (2026-10-11)

대상: 「버츄얼휴먼 음성 파이프라인 고도화를 위한 차세대 STT·TTS 모듈 및 통합 아키텍처 분석」

### 13.1 채택한 내용

- 전 구간 로컬 처리와 비동기 스트리밍 방향
- SenseVoice-Small을 STT 실측 후보로 추가 (§3.3)
- Kiwi 형태소 트랙으로 침묵 대기 조절 (§3.1)
- CosyVoice3를 TTS 1순위로 둠 (§4.4)
- Runtime MetaHuman Lip Sync를 립싱크 1순위 검증 대상으로 둠 (§7.6)
- 상향 목표(P50 900ms, P90 1,200ms), 48시간 구동 시험 (§8.5)

### 13.2 고친 내용

| 보고서 주장 | 확인 결과 | 반영 |
|---|---|---|
| 고도화 합계 "820~950ms" | 구간 상한을 더하면 1,060ms다 | §2.3 시간축표로 대체 |
| 기존 설계 합계 "1,950~2,710ms" | 보고서 구간 상한 합은 3,250ms다. §26 표(900~1,750ms)와도 다르다 | §26 기준 유지 |
| SenseVoice-Small "Apache-2.0" | 모델은 FunASR MODEL_LICENSE다 | §3.3, §10 #8 |
| Moonshine-tiny-ko "Permissive Open" | 한국어 레거시 모델은 비상업 라이선스다 | 제외 |
| SimulStreaming "PolyForm 비상업" | 현재 GitHub 표기는 MIT다 | 제외 사유에서 뺐다 |
| Kokoro "한국어 발음 왜곡" | 한국어 음성 자체가 없다 | 제외 사유 수정 |
| "SenseVoice 부분 전사 버퍼" | SenseVoice는 스트리밍 모델이 아니다 | §3.3 구간 전사 규칙 |
| "Audio Live Link Windows 전용" | Epic 문서는 Windows·macOS 입력 장치를 기재한다 | 기존 유지 |
| "barge-in 반응 250ms 이하" | 확정 구간 250~400ms와 충돌한다 | §6.6 덕킹·정지 지연 분리 |
| 연결 어미 일괄 600~700ms 대기 | 존댓말 "-는데요"류 문장 끝을 놓친다 | §3.1 규칙 추가 |
| "Kiwipiepy 1ms 이내" | README 평가값은 1줄당 약 1~2ms다 | 지연 영향은 작다 |
| 개인정보 보호법 제23조 "외부 전송 제한" | 제23조는 민감정보 처리 제한 조항이다 | 법적 판단은 이 문서 범위 밖 |

### 13.3 출처가 없거나 확인하지 못한 수치

- STT 후보별 한국어 CER 범위 (예: SenseVoice 8.5~11.0%). Zipformer 14~18%는 기존 출처(7.53%)와 다르다.
- CosyVoice3의 "한국어 발음 오류 60% 개선", VRAM 2.5~3.0GB, vLLM 지원.
- Runtime MetaHuman Lip Sync의 "5ms 이하", "VRAM 0MB", "CPU SIMD 처리".
- Audio Live Link 120~200ms, Audio2Face 100~300ms·1.4~4.0GB.
- TTS 후보별 TTFA 범위 (예: OmniVoice 200~300ms).
- 립싱크 "±30ms" 합격 기준의 근거.
- 보고서 본문에 인용 흔적(`[span_…]`)이 남아 있다. 원문 출처를 대조할 수 없다.

---

## 14. 개발 장비 메모

> ⚠️ 경고: 최종 지연 수치는 운영 PC(Windows 11 + RTX 4090 ×2)에서만 확정한다. 아래 장비 수치는 기능 확인용이다.

**MacBook Air M1 16GB**

- 27B LLM(약 15.7GB)은 메모리에 들어가지 않는다. `MockLLM`을 쓴다.
- CosyVoice3는 CPU 모드로 RTF 약 0.3~0.5다 (제3자 자료).
- sherpa-onnx와 kiwipiepy는 macOS arm64 빌드가 있다.

**AI TOP ATOM (GIGABYTE, NVIDIA GB10)**

- Arm64 CPU 20코어, Blackwell 통합 GPU, 통합 메모리 64GB 또는 128GB, DGX OS(Ubuntu)다.
- 메모리 대역폭은 273GB/s다. RTX 4090(약 1,008GB/s)보다 낮다.
- LLM 디코딩 속도는 메모리 대역폭에 크게 좌우된다. ATOM의 tok/s를 4090 값으로 쓰지 않는다.
- sherpa-onnx와 kiwipiepy는 Linux arm64 빌드가 있다.
- CosyVoice3의 ttsfrd 휠은 x86_64 전용이다. ATOM에서는 wetext로 대체된다.
- GB10용 PyTorch CUDA 빌드에서 CosyVoice3·OmniVoice가 도는지는 확인하지 않았다.
