# 버츄얼휴먼 음성 파이프라인 (STT·TTS·통합) 개발 포인트

> 작성: 2026-10-11 · 기준 문서: `10-ai26-2-ai.md` (아키텍처 설계 v0.3, 2026-09-19)
> 범위: 마이크 → 음성 인식 (STT) → 상담 LLM 결합 → 청커 → 음성 합성 (TTS) → 재생·립싱크 (lip-sync, 메타휴먼 결합)

## 0. 결론

1. 응답 지연 (voice-to-voice latency)은 TTS 엔진 하나로 정해지지 않는다. 턴 종료 감지 (end-of-turn detection), LLM 첫 청크 (chunk), TTS 첫 오디오의 합이다.
2. CLOVA·Azure 같은 클라우드 TTS는 설계서의 "개인 건강정보 외부 전송 방지" 조건과 충돌한다. 채택 전에 승인이 필요하다.
3. 추천: TTS를 어댑터로 분리하고 로컬 TTS를 1순위로 실측한다. 로컬 품질이 기준에 못 미칠 때 Azure를 쓴다.

**지금 할 일**: 조교에게 "클라우드 TTS로 답변 텍스트를 외부 전송해도 되는가"를 묻는다. 질문 문안은 §10에 있다.

**진행 상태**

- ✅ Qwen3-TTS 1차 검증: 속도·한국어 품질 문제 확인
- 🔄 TTS 대안 검토: 이 문서
- ⏭️ 측정 하네스·모의 LLM 서버 구축 (§8)

---

## 1. 담당 범위와 경계

```text
마이크 → AEC → VAD·턴 종료 감지 → 스트리밍 STT
                                      ↓
                    Dialogue Controller (turn_id 발급·무효화)
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
- 출력 경계는 §22 Unreal Bridge다. PCM 스트림 메시지는 아직 정의되어 있지 않다 (§7.5 제안).
- 핵심 원칙: 모든 메시지와 오디오 프레임에 turn_id를 붙인다. 사용자 끼어들기 (barge-in)의 정확성이 여기서 나온다.

---

## 2. 지연 예산 — 가장 중요한 기준

### 2.1 목표

- 응답 지연은 "사용자 발화 종료 → 스피커 첫 음성"으로 정의한다.
- 10개 언어(한국어 포함) 비교 연구에서 겹침 회피와 침묵 최소화가 공통으로 관찰됐다 (Stivers et al., 2009).
- 사람 대화의 턴 사이 간격은 보통 약 200ms로 소개된다.
- 설계서 목표는 체감 첫 발화 0.7~1.5초(§26), MVP 1.5초 이하(§36)다.

### 2.2 설계서 수치의 문제

- LLM 첫 토큰 지연 (TTFT)을 400ms로 두고 §26의 나머지 구간 상한을 더하면 1,750ms다.
- 첫 청크 구간은 TTFT 이후 구간으로 해석했다.
- §16 PCM 선행 버퍼 상한 300ms를 더하면 2,050ms다.
- 같은 방식으로 나머지 구간 하한을 더하면 900ms다. 선행 버퍼 하한 100ms를 더하면 1,000ms다.
- 상한끼리 더하면 목표 1.5초를 넘는다.
- 그래서 구간을 순서대로 기다리지 않고 겹쳐야 한다.

### 2.3 제안 예산 (설계 목표값, 실측 전)

| 구간 | 목표 | 줄이는 방법 |
|---|---:|---|
| 턴 종료 감지 | 250ms | 짧은 침묵 + 턴 종료 모델 (§3.1) |
| STT 최종 전사 | 100ms | 스트리밍 부분 결과 유지 |
| LLM TTFT | 400ms | §32 계약값. 프롬프트 접두부 캐시 |
| 첫 청크 완성 | 200ms | 첫 청크만 짧게 (§5.2) |
| TTS 첫 오디오 | 100ms | 모델·연결 예열, 원시 PCM |
| 출력 버퍼 | 50ms | 선행 버퍼 최소화 |
| **합계 (순차)** | **1,100ms** | 선행 생성으로 약 200ms 겹침 → **약 900ms** |

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

### 3.1 턴 종료 감지 (end-of-turn detection)

- 침묵을 기다리는 시간은 그대로 응답 지연이 된다.
- 너무 짧게 기다리면 사용자의 말을 중간에 자른다.
- 그래서 침묵 길이와 "말이 끝났는가"를 함께 판단한다.

구성 절차:

1. VAD로 말 멈춤을 찾는다.
2. 멈춤마다 턴 종료 모델(Smart Turn v3.2)을 실행한다.
3. 완료 확률이 높으면 짧게(예: 200ms) 기다린다.
4. 완료 확률이 낮으면 길게 기다린다.
5. 최대 침묵 상한에 닿으면 턴을 끝낸다.

Smart Turn v3.2 확인 사항:

- 23개 언어를 지원하고 한국어가 포함된다.
- 전사가 아니라 오디오의 운율·속도·억양을 본다.
- 입력은 현재 턴의 최근 8초, 16kHz 모노 PCM이다.
- VAD가 아니다. VAD가 멈춤을 보고한 뒤 실행한다.
- Pipecat Cloud 표준 인스턴스에서 추론 약 65~70ms가 보고됐다.
- 라이선스는 BSD-2다.

한국어 보조 규칙 (가설, 효과는 확인하지 않았다):

- STT 부분 결과 끝이 종결 어미(-요, -다, -까, -죠)면 대기를 줄인다.
- 연결 어미(-고, -서, -는데, -면)면 대기를 늘린다.
- 최대 침묵 상한은 스테이션 사용자의 실제 발화로 정한다.

### 3.2 선행 생성 (preemptive generation)

- 정의: 턴 종료가 확정되기 전에 LLM 생성을 시작한다.
- LiveKit 기본 동작은 LLM만 먼저 실행하고 TTS는 턴 확정까지 기다린다.
- 대가: 버려지는 생성이 늘어난다. 긴 발화에서는 이득이 작다.
- 이 시스템은 사용자가 말하는 동안 GPU 1(LLM)이 쉰다. 그래서 선행 생성 비용이 작다.

> ⚠️ 경고: 선행 생성 결과는 턴 확정 전에 재생하지 않는다. 선행 생성 중 받은 risk_update 처리 방식은 조교와 정한다 (§10).

절차:

1. 침묵 200ms 시점의 전사로 LLM 요청을 보낸다.
2. 사용자가 다시 말하면 그 요청을 취소한다.
3. 턴이 확정되고 최종 전사가 같으면 결과를 채택한다.
4. 최종 전사가 다르면 결과를 버리고 다시 요청한다.

### 3.3 STT 후보

| 후보 | 장점 | 위험 |
|---|---|---|
| **Qwen3-ASR 0.6B/1.7B (추천: 먼저 실측)** | 한국어 지원. 한 모델로 스트리밍·오프라인 처리. Apache-2.0 | 스트리밍은 vLLM 백엔드에서만 지원. vLLM의 Windows 네이티브 지원은 확인하지 않았다 |
| sherpa-onnx 스트리밍 Zipformer (한국어) | 실제 스트리밍. CPU 동작. 크로스플랫폼 | 정확도가 낮다 (커뮤니티 174M 모델의 스트리밍 오류율 7.53%) |
| 클라우드 STT (CLOVA Speech, Azure) | 운영 부담이 적다 | 사용자 원음이 외부로 나간다 → 비추천 |

- Qwen3-ASR 스트리밍 정확도는 오프라인보다 조금 낮다. 1.7B 평균 단어 오류율 (WER)이 2.69에서 3.33으로 오른다 (영어·중국어 벤치 기준).
- Qwen3-ASR 스트리밍 모드는 배치와 타임스탬프를 지원하지 않는다.
- Windows에서 vLLM이 막히면 2단 구성으로 바꾼다.
  - 부분 결과 (턴 감지·barge-in 확인용): Zipformer, CPU
  - 최종 전사 (LLM 입력용): Qwen3-ASR transformers 백엔드로 발화 구간을 1회 인식
- 2단 구성의 최종 인식 지연은 확인하지 않았다. 1주차에 잰다.

### 3.4 STT GPU 배치 (검토 항목)

- §5.2는 STT를 렌더링과 같은 GPU 0에 둔다.
- STT 추론이 몰리면 렌더링 프레임 시간이 흔들릴 수 있다. 영향 크기는 확인하지 않았다.
- LLM은 사용자가 말하는 동안 쉰다. 그래서 STT를 GPU 1로 옮기는 안이 있다.
- GPU 1 합계는 18.2GB + 2GB = 20.2GB다. 24GB 안에 든다.
- 단 barge-in 감지 중에는 STT와 LLM이 동시에 돈다. 실측 후 정한다.

---

## 4. TTS 선택

### 4.1 현재 Qwen3-TTS가 느린 이유

- 설계서 §13.4의 `generate_custom_voice` 호출은 청크 전체를 합성한 뒤 반환한다.
- 이 경우 첫 오디오 지연 (TTFA, Time To First Audio)은 청크 전체 합성 시간이다.
- Qwen3-TTS가 주장하는 첫 패킷 97ms는 스트리밍 경로 기준이다.
- 한국어 품질 문제는 스트리밍과 관계가 없다. 그래서 대안 검토는 타당하다.

### 4.2 후보 비교

| 항목 | Azure TTS | CLOVA Voice | 로컬 (OmniVoice·CosyVoice3) |
|---|---|---|---|
| 출력 스트리밍 | 지원. 첫 바이트 지연은 텍스트 길이와 무관 | 스트리밍 API를 공식 문서에서 찾지 못했다. REST로 mp3/wav 파일 반환 | CosyVoice3: 입출력 양방향 스트리밍, 최저 150ms 주장. OmniVoice: 비자기회귀 (NAR) 생성, 실시간 계수 (RTF) 최저 0.025 주장 |
| 입력 텍스트 스트리밍 | 지원 (Speech SDK C#·C++·Python, WebSocket v2). TextStream은 rate 미적용 → 음성 합성 마크업 (SSML) 스트림 사용 (MS Q&A 답변) | 없음. 1회 최대 2,000자 | CosyVoice3 지원. OmniVoice 없음 |
| 립싱크·타이밍 보조 | ko-KR은 입모양 ID (viseme ID)만 지원. 얼굴 블렌드셰이프 (blend shape)는 미지원. 단어 단위 타임스탬프 제공 | 문서에서 찾지 못했다 | 없음 → 오디오 기반 립싱크 |
| 온프레미스 경로 | 신경망 TTS 컨테이너에 한국어 음성 태그(SunHi, InJoon) 있음. Embedded Speech는 신청 승인 필요 | 확인하지 않았다 | 기본이 로컬 |
| 데이터·이용 정책 | MS 문서: 입력 텍스트와 출력 오디오를 MS 로그에 저장하지 않음 | 실시간 호출만 허용. 생성 파일 저장·편집 재사용 불가 (CLOVA Dubbing 별도) | 외부 전송 없음. 두 모델 모두 Apache-2.0 |
| 비용 | 월 50만 자 무료 구간. Neural 100만 자당 약 $15 (제3자 집계, 2026-10-08) | 호출이 없어도 기본료 발생. Premium 월 100만 자 포함. 단가는 공식 페이지에서 확인하지 못했다 | 라이선스 비용 없음. GPU·CPU 자원 사용 |
| 한국어 품질 | 청취 테스트 필요 | 청취 테스트 필요 | 청취 테스트 필요. 참고: 한국어 문자 오류율 (CER) OmniVoice 3.22, Qwen3-TTS 4.07 (낮을수록 좋음, 자연스러움 지표 아님) |

- 한국어 CER 수치는 Supertone이 Minimax-MLS-test로 집계한 표다.
- CosyVoice3의 한국어 지원은 제3자 자료로 확인했다. 공식 모델 카드의 언어 목록은 확인하지 않았다.
- Azure Embedded Speech는 로케일별로 여성·남성 중 1~2개 음성만 제공한다. Python SDK는 하이브리드(클라우드+임베디드) 모드를 지원하지 않는다.

### 4.3 제외 후보: Supertonic 3

- 한국어 CER은 3.26으로 좋다 (Supertone 자체 집계).
- 모델 라이선스(BigScience Open RAIL-M) 부속서 A (l)항은 "의료 조언 및 의료 결과 해석" 용도를 금지한다.
- 이 시스템은 혈압·혈당 결과를 음성으로 읽어 준다. 금지 용도 해당 여부는 법적 판단이 필요하다. 확인하지 않았다.
- 공식 저장소는 2026-07-23 공지로 아카이브된다. 오픈소스 모델의 공식 지원도 끝난다.
- 그래서 1순위 후보에서 뺀다.

### 4.4 선택지와 추천

**1안 (추천): 로컬 TTS 우선 — OmniVoice·CosyVoice3 실측**

- 설계서 원칙(로컬 처리, 외부 전송 방지)을 그대로 지킨다.
- 네트워크 지연과 인터넷 장애가 없다.
- barge-in이 단순하다. 로컬 작업만 버리면 된다.
- 위험: 한국어 자연스러움이 검증되지 않았다.
- 위험: GPU 0 자원을 렌더링과 나눠 쓴다.

**2안: Azure TTS**

- 출력 스트리밍, 입력 텍스트 스트리밍, 단어 타임스탬프, 한국어 viseme ID가 있다.
- 한국어 음성 컨테이너가 있다. 나중에 온프레미스로 옮길 경로가 된다.
- 위험: 외부 전송 승인이 필요하다.
- 위험: 인터넷 연결에 의존한다.

**3안: CLOVA Voice**

- 국내 서비스이고 감정(Emotion) 파라미터가 있다.
- 위험: 스트리밍과 타이밍 이벤트가 없다.
- 위험: 생성 음성 저장·재사용이 금지다. 안전 안내 음성을 캐시할 수 없다.
- 위험: 서비스를 만들기만 해도 기본료가 나간다. 실측 기간을 조교와 정한다.

결정 규칙:

1. 같은 문장 세트로 후보를 모두 잰다 (TTFA, RTF, 청취 점수).
2. 로컬이 청취 기준을 넘으면 로컬로 확정한다.
3. 로컬이 기준 미달이면 승인 결과를 보고 Azure를 쓴다.
4. CLOVA는 청취 품질이 확실히 앞설 때만 쓴다.

추가 주의:

- 개인정보 보호법은 건강 정보를 민감정보로 분류한다 (제23조). 클라우드 전송의 법적 판단은 이 문서 범위 밖이다.
- OmniVoice 음성 복제를 쓰면 당사자 동의를 받은 참조 음성만 쓴다.
- TTS 확정 후 설계서 §5.2(VRAM), §13(TTS 큐), §35(코딩 에이전트 제약)를 갱신한다.

### 4.5 TTS 어댑터 인터페이스

```python
from dataclasses import dataclass
from typing import AsyncIterator, Protocol


@dataclass
class AudioFrame:
    turn_id: int
    seq: int          # 턴 안에서 0부터 증가
    pcm16: bytes      # 모노 16-bit, 20ms 단위
    sample_rate: int
    is_last: bool


class TTSEngine(Protocol):
    sample_rate: int

    async def warmup(self) -> None: ...              # 모델 로드, 연결 미리 열기

    def stream(self, text: str) -> AsyncIterator[bytes]: ...   # PCM16 모노 조각

    async def cancel(self) -> None: ...              # 진행 중 합성 중단
```

- 엔진은 PCM 조각만 낸다. turn_id·seq·20ms 분할은 파이프라인이 붙인다.
- 엔진을 바꿔도 청커·큐·재생 코드는 바꾸지 않는다.
- 구현 후보: `OmniVoiceTTS`, `CosyVoiceTTS`, `AzureTTS`, `ClovaTTS`, `QwenTTS`.
- 지연 측정은 어댑터 바깥에서 한다. 엔진마다 측정 방식이 달라지지 않는다.

### 4.6 클라우드 TTS를 쓸 때의 규칙

1. 연결을 미리 열고 재사용한다 (Azure 공식 권장 사항).
2. 출력은 원시 PCM으로 받는다. MP3는 디코딩 지연과 앞뒤 패딩이 생긴다.
3. 첫 오디오 타임아웃을 둔다.
4. 타임아웃이 나면 그 턴 전체를 로컬 엔진으로 넘긴다.
5. 안전 안내(고위험·119) 음성은 항상 로컬에서 낸다. 인터넷이 끊겨도 나와야 한다.

---

## 5. 청커·텍스트 정규화·오디오 출력

### 5.1 현재 SemanticChunker(§12)의 문제

1. 소수점에서 끊긴다. "오늘 측정하신 체온은 36." (15자)에서 끊기고 "5도입니다."는 따로 읽힌다.
2. 강제 분할(40자)이 어절 중간을 자른다.
3. 첫 청크도 최소 15자를 기다린다. 첫 오디오가 늦어진다.
4. `end_marks`에 "?"와 "!"가 두 번 들어 있다. 전각 문자(？！)를 의도했는지는 확인하지 않았다.

### 5.2 개선 규칙

- 첫 청크만 첫 쉼표에서도 끊는다. 최소 길이는 실측으로 정한다 (후보 6~12자).
- 이후 청크는 문장 끝에서만 끊는다. 청크마다 억양이 새로 시작되기 때문이다.
- "숫자." 다음 토큰이 숫자면 끊지 않는다.
- 강제 분할은 마지막 공백에서 한다.

참고 구현 (아래 사례로 동작 확인함, 실제 토크나이저로 재검증 필요):

```python
END_MARKS = (".", "?", "!", "。", "？", "！", "…")
SOFT_MARKS = (",", "、")


class SemanticChunker:
    """첫 청크는 짧게, 이후는 문장 단위. 소수점과 어절을 보호한다."""

    def __init__(self, first_min: int = 8, min_chars: int = 15, max_chars: int = 40):
        self.first_min, self.min_chars, self.max_chars = first_min, min_chars, max_chars
        self.buf, self.first = "", True

    def feed(self, token: str) -> list[str]:
        out: list[str] = []
        # 버퍼가 "숫자."로 끝나 판단을 미뤘다면, 새 토큰을 보고 결정한다
        if self._held_dot() and not token[:1].isdigit():
            if len(self.buf.strip()) >= self._limit():
                out.append(self._flush())
        self.buf += token
        if self._ready():
            out.append(self._flush())
        elif len(self.buf) >= self.max_chars:
            out.append(self._flush_at_space())
        return out

    def close(self) -> list[str]:
        return [self._flush()] if self.buf.strip() else []

    def _limit(self) -> int:
        return self.first_min if self.first else self.min_chars

    def _held_dot(self) -> bool:
        return len(self.buf) >= 2 and self.buf[-1] == "." and self.buf[-2].isdigit()

    def _ready(self) -> bool:
        s = self.buf.rstrip()
        if self._held_dot() or len(s) < self._limit():
            return False
        if s.endswith(END_MARKS):
            return True
        return self.first and s.endswith(SOFT_MARKS)   # 첫 청크만 쉼표 허용

    def _flush_at_space(self) -> str:
        cut = self.buf.rfind(" ")
        if cut <= 0:
            return self._flush()
        head, self.buf = self.buf[:cut].strip(), self.buf[cut + 1:]
        self.first = False
        return head

    def _flush(self) -> str:
        head, self.buf = self.buf.strip(), ""
        self.first = False
        return head
```

### 5.3 텍스트 정규화 (text normalization)

- LLM 답변의 기호와 단위를 읽을 수 있는 말로 바꾼다.
- CLOVA Voice는 기호와 괄호 안 텍스트를 읽지 않는다 (공식 제약).
- 숫자는 엔진이 바르게 읽는지 먼저 시험한다. 기호와 단위는 항상 바꾼다.

| LLM 출력 | TTS 입력 (예시) | 처리 대상 |
|---|---|---|
| 120/80mmHg | 120에 80 | 슬래시, 단위 |
| 126mg/dL | 126 밀리그램 퍼 데시리터 | 단위 |
| 36.5℃ | 36.5도 | 기호 |
| 25.3% | 25.3 퍼센트 | 기호 |
| 3~4회 | 3에서 4회 | 물결표 |
| `**주의**`, `- 항목` | (기호 제거) | 마크다운 |

- 읽기 규칙(예: 혈압 "120에 80")은 조교·상담 자문과 맞춘다.
- LLM 담당에게 요청한다: 마크다운·목록·이모지 없이 말하듯 쓴다.
- LLM 담당에게 요청한다: 첫 문장을 짧게 쓴다. 첫 청크 지연이 줄어든다.

### 5.4 큐·버퍼·출력

- 3-slot 큐(§13)는 유지한다. LLM 역압 (backpressure)에 쓴다.
- §14 적응형 배치는 C2 완료를 C3 완료까지 늦춘다. TTS RTF가 충분히 낮으면 배치를 쓰지 않는다.
- §16 선행 버퍼 100~300ms는 응답 지연에 그대로 더해진다. 첫 청크는 40~60ms부터 실측한다.
- 오디오는 16-bit 모노 원시 PCM, 20ms 프레임으로 보낸다.
- 리샘플링은 한 곳에서 한 번만 한다.
- 버퍼 고갈 (underrun) 횟수를 기록한다 (§27).

---

## 6. barge-in 설계

### 6.1 전제: AEC

- AEC가 없으면 아바타 목소리가 마이크로 들어온다. 그러면 아바타가 스스로 말을 끊는다.
- 설계서 §5.1의 "USB 마이크 또는 지향성 마이크"는 AEC를 보장하지 않는다.

| 안 | 방식 | 판단 |
|---|---|---|
| **1안 (추천)** | AEC 내장 USB 스피커폰을 스피커 겸 마이크로 쓴다 | 장치가 재생 신호를 직접 안다. 기준 신호가 정확하다. OS와 무관하다 |
| 2안 | 소프트웨어 AEC (WebRTC AEC3, `pywebrtc-audio`) | 기준 신호는 스피커로 나가는 순간의 오디오여야 한다. Unreal이 재생하면 시스템 루프백으로 받아야 한다 |
| 3안 | 반이중 (half-duplex): 말하는 동안 마이크 무시 + 화면 "잠깐" 버튼 | 대비책이다. 음성 barge-in은 포기한다 |

- `pywebrtc-audio`는 Linux·macOS·Windows(x86_64) 휠을 제공한다.
- AEC 성능은 실제 스테이션 하드웨어(스피커·마이크 배치)에서만 확인할 수 있다.

### 6.2 2단계 barge-in (자연스러운 끼어들기)

| 단계 | 조건 | 동작 |
|---|---|---|
| 1. 감지 | VAD가 발화 시작을 잡음 | TTS 음량을 낮춘다 (덕킹, ducking). 아바타가 사용자를 본다. 생성은 계속한다 |
| 2. 확정 | 발화가 250~400ms 이어지고 맞장구가 아님 | 30~50ms 페이드아웃 후 정지한다. turn_id를 무효화한다. LISTENING으로 간다 |
| 2'. 취소 | 기침·잡음·짧은 맞장구 | 음량을 되돌리고 말을 이어 간다 |

- 맞장구 (backchannel) 예: 네, 예, 응, 음, 아.
- 맞장구마다 멈추면 대화가 끊긴다. 그래서 짧은 맞장구로는 멈추지 않는다.
- 기준값(250~400ms, 덕킹 폭)은 테스트 문장 세트로 정한다.

### 6.3 turn_id 기반 취소

설계서 §24 샘플의 문제:

1. LLM 스트림을 취소하지 않는다. 정지 후에도 청크가 다시 들어온다.
2. 합성 중인 TTS 작업(`to_thread`)은 멈추지 않는다. 지난 오디오가 `audio_queue`로 들어온다.
3. `audio_queue`, 재정렬 버퍼, `expected_seq`를 초기화하지 않는다. 다음 턴 재생이 멈추거나 지난 오디오가 나온다.
4. `except:`가 `CancelledError`까지 삼킨다.
5. 페이드 없이 끊으면 클릭음이 난다.

수정 원칙:

- 정지를 "현재 turn_id 무효화"로 정의한다.
- 재생·전송 쪽은 무효 turn_id의 프레임을 모두 버린다.
- 그러면 큐를 비우는 시점과의 경쟁 상태 (race condition)가 생기지 않는다.
- 재정렬 버퍼는 turn_id별로 만든다. seq는 턴마다 0부터 시작한다.

```python
import asyncio


class TurnGuard:
    """현재 turn_id만 살아 있다. 나머지 turn_id의 결과는 모두 버린다."""

    def __init__(self) -> None:
        self.current = 0
        self.tasks: set[asyncio.Task] = set()

    def next_turn(self) -> int:
        self.current += 1
        return self.current

    def is_live(self, turn_id: int) -> bool:
        return turn_id == self.current

    def track(self, task: asyncio.Task) -> asyncio.Task:
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


async def handle_barge_in(guard: TurnGuard, ue, llm) -> None:
    dead = guard.current
    guard.next_turn()                                                     # 1) 지난 턴 무효화
    await ue.send({"type": "interrupt", "turn_id": dead, "fade_ms": 40})  # 2) 들리는 소리부터 정지
    await llm.cancel(dead)                                                # 3) LLM 요청 취소 (§32)
    for task in list(guard.tasks):                                        # 4) TTS·전송 작업 취소
        task.cancel()
    await ue.send({"type": "avatar_state", "state": "listening"})


async def forward_frames(guard: TurnGuard, frames: asyncio.Queue, ue) -> None:
    while True:
        frame = await frames.get()
        if guard.is_live(frame.turn_id):      # 지난 턴 프레임은 버린다
            await ue.send_binary(frame)
```

### 6.4 대화 기록 자르기

- assistant 메시지에는 사용자가 실제로 들은 부분만 남긴다.
- Unreal이 회신한 재생 위치(`played_ms`)로 마지막 청크를 길이 비율로 자른다.
- Azure를 쓰면 단어 타임스탬프로 더 정확히 자를 수 있다.
- "중단됨" 표시를 다음 LLM 요청에 넣을지는 LLM 담당과 정한다.

### 6.5 안전 모드와의 관계

- 119 등급 정지는 barge-in과 같은 정지 경로를 쓴다 (§24, §31).
- 안전 안내 중 barge-in 허용 여부는 정해져 있지 않다. 조교·지도교수가 정한다.

### 6.6 지표

- barge-in 정지 지연: 사용자 발화 시작 → 출력 무음
- 오정지율: 맞장구·기침·잡음에 멈춘 비율
- 미검출률: 사용자가 끼어들었는데 멈추지 않은 비율

---

## 7. 메타휴먼 결합과 리눅스 제약

### 7.1 확인된 플랫폼 사실

| 항목 | 확인 내용 |
|---|---|
| MetaHuman Animator | Windows에서만 지원. DirectX 12 필요 |
| MetaHuman Creator | UE 5.7부터 Windows·Mac·Linux 에디터에서 캐릭터 생성 가능. Identity·Performance 에셋 생성은 Mac·Linux에서 비활성 |
| 실시간 오디오 애니메이션 (MetaHuman Audio Live Link 소스) | 입력은 Windows·macOS가 지원하는 USB 오디오 캡처 장치. Linux 지원은 문서에 없다. 확인하지 않았다 |
| 실시간 비디오·오디오 소스 부하 | GPU를 많이 쓴다. Live Link Hub는 별도 PC 실행을 권장한다 |
| NVIDIA ACE UE 플러그인 | Linux는 앱·에디터의 Animation Graph만 지원. 로컬 Audio2Face 실행 플러그인은 Win64 전용 (NVIDIA 포럼). 원격 Audio2Face 제공자는 Linux 지원 |
| 서드파티 Runtime MetaHuman Lip Sync | Linux를 포함한 다중 플랫폼 지원 표기. 오디오를 직접 입력받는다. 가격과 한국어 품질은 확인하지 않았다 |
| 운영 PC | Windows 11 + RTX 4090 ×2 (설계서 §5·§6 정본) |

- DirectX 12는 Windows API다. Linux 에디터는 Vulkan으로 렌더링한다.
- Epic의 Linux 개발 권장 OS는 Ubuntu 22.04다.

### 7.2 Linux에서 할 일과 Windows에서 할 일

- Linux (지금): 백엔드 전체를 만든다. STT, TTS, 청커, barge-in, 측정이 여기에 해당한다.
- Linux에서는 Unreal 대신 "로컬 스피커 출력" 구현으로 같은 인터페이스를 시험한다.
- Windows (2~3주차부터): Unreal 연결, 립싱크, AEC, 최종 지연 측정을 한다.
- 이유 1: 공식 립싱크 경로(MetaHuman Animator 계열)가 Windows 중심이다.
- 이유 2: 운영 PC가 Windows다.

### 7.3 개발 구성 선택지

| 안 | 구성 | 판단 |
|---|---|---|
| **1안 (추천)** | Linux 개발 PC의 백엔드 ↔ LAN WebSocket ↔ Windows PC의 Unreal | 지금 바로 가능하다. 렌더링 경로가 운영과 같다 |
| 2안 | Linux에서 UE 5.7 에디터 + Linux 지원 립싱크 플러그인 | 립싱크 경로가 운영과 달라진다 |
| 3안 | 개발 PC를 Windows로 듀얼 부팅 | 백엔드의 Windows 동작도 같이 확인한다 |

운영 PC에서 백엔드를 돌리는 방식은 2주차에 정한다:

- A: Windows 네이티브 실행. 크로스플랫폼 의존성만 쓴다.
- B: WSL2 안에서 실행. Linux 전용 의존성(vLLM 등)을 유지할 수 있다. 오디오 장치는 Windows 쪽이 소유한다.
- 결정 규칙: 선택한 STT·TTS가 Windows 네이티브로 동작하면 A, 아니면 B로 간다.

### 7.4 재생 주체 (audio clock owner): Unreal이 재생한다 (제안)

- 설계서 §17은 같은 PCM을 스피커와 MetaHuman 입력으로 나눈다.
- 백엔드가 스피커를 직접 재생하고 Unreal이 립싱크하면 시계가 둘이 된다.
- 시계가 둘이면 입모양과 소리가 어긋나기 쉽다.
- 제안: Unreal이 PCM을 받아 재생하고, 같은 버퍼로 립싱크한다 (예: `USoundWaveProcedural`).
- 립싱크 처리 지연만큼 오디오를 늦춰 입모양과 소리를 맞춘다.
- 허용 기준: ITU-R BT.1359에서 오디오가 45ms 앞서거나 125ms 늦으면 감지되기 시작한다.
- Unreal은 재생 시작·진행·정지를 백엔드에 회신한다. 이 값으로 실제 TTFA를 재고 대화 기록을 자른다.

### 7.5 Unreal 브리지 메시지 제안 (§22 확장)

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

- PCM은 바이너리 프레임으로 보낸다. base64는 크기를 약 33% 늘리고 인코딩 비용이 든다.
- 감정·제스처를 LLM 스트림의 어디에 담을지는 정해지지 않았다. §20 JSON과 §32 토큰 스트림이 다르다 (§10).

### 7.6 립싱크 방식 선택지

| 안 | 방식 | 판단 |
|---|---|---|
| **1안 (추천: 먼저 검증)** | UE 공식 실시간 오디오 애니메이션 | 무료다. 무드 12종을 지정할 수 있어 감정 상태와 연결된다. 입력이 오디오 장치라서 TTS 출력을 가상 오디오 장치로 넘겨야 할 수 있다 (확인하지 않았다). GPU 부하가 있다 |
| 2안 | 서드파티 런타임 립싱크 플러그인 | 오디오 버퍼를 직접 넣는다. Linux를 지원한다. 가격은 확인하지 않았다. 서드파티에 의존한다 |
| 3안 | NVIDIA Audio2Face-3D (오픈소스 SDK) | 로컬 실행 플러그인은 Win64 전용이다. UE 5.7 지원은 확인하지 않았다. GPU 부하가 있다 |

- 세 안 모두 백엔드 출력(§7.5 PCM 스트림)은 같다. 그래서 선택이 늦어져도 내 작업은 막히지 않는다.
- §5.2 VRAM 예산표에는 립싱크 처리 항목이 없다. GPU 0 예산에 추가하고 실측한다.

### 7.7 사람처럼 보이게 하는 반응

- 턴 종료 직후 100~200ms 안에 비언어 반응(끄덕임, 숨쉬기, 생각하는 표정)을 보인다.
- 음성이 나오기 전에 "들었다"는 신호가 먼저 간다.
- THINKING 상태 메시지는 턴 확정 시점에 바로 보낸다.
- 미리 녹음한 추임새("네,")는 반복되면 기계적으로 들린다. 상담 문맥에서는 신중히 쓴다.
- LISTENING 중 시선 맞춤과 작은 끄덕임은 Unreal 담당과 정한다.

---

## 8. 측정·검증

### 8.1 턴 추적 기록 (turn trace)

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

### 8.3 테스트 문장 세트 (30~50개)

- 짧은 대답 (네, 아니요)
- 수치가 들어간 질문 (혈압, 혈당, 체온)
- 상담 발화 (감정 표현, 여러 절로 된 긴 문장)
- 중간에 쉼이 긴 발화
- barge-in 시나리오 (맞장구, 말 정정, 기침)

### 8.4 모의 LLM 서버 (mock)

- §32 계약(meta → token → done, risk_update)을 흉내 낸다.
- TTFT, 토큰 속도, risk_update 시점을 설정값으로 바꾼다.
- 파인튜닝 모델이 오기 전에 LLM 담당에게 베이스 모델 GGUF(같은 양자화)를 받는다.
- 베이스 모델로 실제 TTFT와 디코딩 속도를 잰다.
- 측정에는 대표 프롬프트(시스템 프롬프트 + 건강 맥락 + 대화 기록)를 쓴다. 프롬프트 길이가 TTFT를 바꾼다.
- 파인튜닝 모델이 오면 엔드포인트만 바꾸고 같은 테스트를 다시 돈다.

### 8.5 합격 기준 (제안)

| 지표 | 목표 |
|---|---|
| 응답 지연 P50 | 1.0초 이하 |
| 응답 지연 P90 | 1.5초 이하 (§36) |
| barge-in 정지 지연 P90 | 300ms 이하 |
| 맞장구 오정지율 | 1주차 측정 후 설정 |
| 청크 사이 무음 | 0ms (§26) |
| MetaHuman FPS | 30 이상 (§26) |

---

## 9. 4주 일정 (2026-10-12 ~ 11-08)

목표 종료는 11월 초다 (9월 말 착수, 약 6주).

**1주차 (10/12~10/18) ⏭️**

1. 턴 추적 기록과 모의 LLM 서버를 만든다.
2. TTS 후보를 같은 문장 세트로 잰다.
3. 팀원 청취 테스트를 한다.
4. STT 후보를 잰다.
5. 선택 후보가 Windows에서 동작하는지 확인한다.

**2주차 (10/19~10/25) ⏭️**

1. STT → 모의 LLM → 청커 → TTS → 로컬 재생을 잇는다.
2. Smart Turn 턴 종료 감지와 선행 생성을 붙인다.
3. turn_id 기반 barge-in 1차 버전을 만든다.
4. AEC 장비와 방식을 정한다.
5. 운영 PC 백엔드 실행 방식(A·B)을 정한다.

**3주차 (10/26~11/01) ⏭️**

1. Unreal 브리지(§7.5)를 Unreal 담당과 연결한다.
2. 립싱크 방식을 확정한다.
3. 베이스 모델 GGUF로 실제 LLM을 결합한다.

**4주차 (11/02~11/08) ⏭️**

1. P50·P90 응답 지연을 튜닝한다.
2. 2단계 barge-in과 맞장구 처리를 다듬는다.
3. 안전 모드(119) 정지 경로를 시험한다.
4. 회귀 테스트와 데모를 준비한다.

---

## 10. 팀에 확인할 결정 사항

| # | 결정 사항 | 결정자 |
|---|---|---|
| 1 | 클라우드 TTS 허용 여부 (외부로 나가는 것: LLM 답변 텍스트) | 조교·지도교수 |
| 2 | 재생 주체를 Unreal로 할지 (§7.4) | Unreal 담당·조교 |
| 3 | 립싱크 방식 (§7.6) | Unreal 담당 |
| 4 | 감정·제스처 메타데이터 위치 (제안: 토큰 앞 meta 이벤트) | LLM 담당 |
| 5 | 선행 생성 중 risk_update 처리 (§3.2) | 조교 |
| 6 | 안전 안내 중 barge-in 허용 여부 (§6.5) | 조교·지도교수 |
| 7 | STT GPU 배치 (GPU 0 또는 1, §3.4) | 조교 |

1번 질문 문안:

> TTS 후보로 Azure와 CLOVA를 검토하고 있습니다. 클라우드 TTS를 쓰면 LLM 답변 텍스트(측정값·상담 내용 포함)가 외부 서버로 전송됩니다. 설계서 §2(외부 전송 방지)·§35(외부 AI API 금지)와 충돌하는데, MVP 데모에서는 허용되나요?

---

## 11. 확인하지 않은 항목

1. 로컬 TTS(OmniVoice·CosyVoice3)의 한국어 자연스러움
2. Azure·CLOVA의 한국어 TTFA 실측값
3. vLLM(Qwen3-ASR 스트리밍)의 Windows 네이티브 동작
4. MetaHuman 실시간 오디오 소스에 TTS 출력을 넣는 경로와 립싱크 처리 지연
5. 립싱크 처리의 GPU 0 VRAM·연산 부하
6. Supertonic 라이선스 의료 용도 제한의 해당 여부 (법적 판단)
7. 정신상담 LLM 문서 §7 계약 원문

---

## 12. 참고 자료

**TTS (클라우드)**

- [Azure: 음성 합성 지연 줄이기 (first byte latency, pre-connect)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-lower-speech-synthesis-latency)
- [Azure: viseme 지원 로케일 (ko-KR = Viseme ID)](https://github.com/MicrosoftDocs/azure-ai-docs/blob/main/articles/ai-services/speech-service/includes/language-support/viseme.md)
- [Azure: Embedded Speech (신청 승인, 플랫폼, 음성 수)](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/embedded-speech)
- [Azure: 신경망 TTS 컨테이너 태그 목록 (ko-kr-sunhineural, ko-kr-injoonneural)](https://mcr.microsoft.com/v2/azure-cognitive-services/speechservices/neural-text-to-speech/tags/list)
- [Azure: TTS 데이터·개인정보 처리](https://learn.microsoft.com/en-us/legal/cognitive-services/speech-service/custom-neural-voice/data-privacy-security-custom-neural-voice)
- [MS Q&A: 입력 텍스트 스트리밍은 SDK·WebSocket v2](https://learn.microsoft.com/en-us/answers/questions/5850986/it-seems-that-there-is-a-gc-bug-in-the-azure-speec) · [TextStream rate 미적용](https://learn.microsoft.com/en-us/answers/questions/5723830/how-to-change-speech-speed(rate)-when-use-input-te)
- [CLOVA Voice 사용 준비](https://guide.ncloud-docs.com/docs/clovavoice-spec) · [CLOVA Voice 이용 정책](https://guide.ncloud-docs.com/docs/clovavoice-policy)

**TTS (로컬)**

- [Qwen3-TTS (스트리밍 97ms 주장, Apache-2.0)](https://github.com/QwenLM/Qwen3-TTS)
- [OmniVoice 모델 카드 (Apache-2.0, 한국어 포함)](https://huggingface.co/k2-fsa/OmniVoice)
- [Fun-CosyVoice3-0.5B-2512 (양방향 스트리밍, Apache-2.0)](https://huggingface.co/FunAudioLLM/Fun-CosyVoice3-0.5B-2512)
- [Supertonic README (아카이브 공지, 한국어 CER 표)](https://github.com/supertone-inc/supertonic) · [Supertonic 3 LICENSE](https://huggingface.co/Supertone/supertonic-3/blob/main/LICENSE)
- [Azure TTS 단가 제3자 집계 (2026-10-08)](https://inventivehq.com/tools/developer/tts-stt-api-cost-calculator)

**STT·턴 종료·AEC**

- [Qwen3-ASR (한국어, vLLM 스트리밍, Apache-2.0)](https://github.com/QwenLM/Qwen3-ASR)
- [sherpa-onnx 한국어 스트리밍 Zipformer](https://huggingface.co/k2-fsa/sherpa-onnx-streaming-zipformer-korean-2024-06-16) · [커뮤니티 174M 모델](https://huggingface.co/kangkyu/icefall-asr-ko-streaming-zipformer-174m)
- [Smart Turn (23개 언어, 오디오 기반 턴 종료)](https://github.com/pipecat-ai/smart-turn)
- [LiveKit 선행 생성](https://docs.livekit.io/agents/build/speech/)
- [pywebrtc-audio (WebRTC AEC3 Python 바인딩)](https://pypi.org/project/pywebrtc-audio/)

**MetaHuman·Unreal**

- [MetaHuman 하드웨어·플랫폼 요구사항](https://dev.epicgames.com/documentation/metahuman/hardware-requirements)
- [MetaHuman Audio Live Link 소스](https://dev.epicgames.com/documentation/metahuman/using-a-metahuman-audio-source-in-unreal-engine)
- [MetaHuman 실시간 애니메이션 (GPU 부하, Live Link Hub)](https://dev.epicgames.com/documentation/metahuman/realtime-animation-for-metahumans-in-unreal-engine)
- [NVIDIA ACE Unreal 플러그인 변경 이력](https://docs.nvidia.com/ace/ace-unreal-plugin/latest/ace-unreal-plugin-changelog.html) · [로컬 실행 Linux 미지원 (포럼)](https://forums.developer.nvidia.com/t/ace-linux-support-for-nvaudio2facemark-plugin-audio2face-3d-local-execution/348812)
- [Runtime MetaHuman Lip Sync (서드파티)](https://forums.unrealengine.com/t/runtime-metahuman-lip-sync-real-time-offline-lip-sync-with-audio-tts-ai-for-npc-cc4-genesis-arkit-and-more-facial-animations/2383289)
- [Unreal Linux 개발 요구사항](https://dev.epicgames.com/documentation/en-us/unreal-engine/linux-development-requirements-for-unreal-engine)

**지각·대화 연구**

- Stivers et al. (2009). Universals and cultural variation in turn-taking in conversation. PNAS 106(26). doi:10.1073/pnas.0903616106
- [ITU-R BT.1359 오디오·영상 동기 감지 한계 (요약)](https://en.wikipedia.org/wiki/Audio-to-video_synchronization)
