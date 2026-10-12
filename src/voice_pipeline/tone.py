"""사용자 발화(감정 태그 + 간단한 음향 특징)로 응답 목소리 톤 프리셋을 고른다.

프리셋 id 는 TTS 서버 PRESETS 와 같다 (comfort / warm / steady / calm).
아래 임계값은 튜닝하지 않은 추측값이다. 실제 마이크 데이터로 보정하기 전에는 근거 있는 값이 아니다.
"""

from __future__ import annotations

import numpy as np

CALM, COMFORT, WARM, STEADY = "calm", "comfort", "warm", "steady"
TONES = (CALM, COMFORT, WARM, STEADY)
AUTO = "auto"  # UI 선택값. TTS 서버에는 보내지 않는다

# SenseVoice 감정 태그 -> 톤
EMOTION_TONE = {
    "SAD": COMFORT, "FEARFUL": COMFORT,
    "ANGRY": STEADY, "DISGUSTED": STEADY,
    "HAPPY": WARM,
}

# 추측값 (미튜닝)
LOW_RMS_DB = -38.0     # 발화 RMS 가 이보다 작으면 매우 작은 목소리 (dBFS)
SLOW_RATE_CPS = 3.0    # 한글 글자/초가 이보다 작으면 느린 말
MIN_DURATION_S = 0.8   # 이보다 짧은 발화는 음향 특징을 믿지 않는다


def hangul_count(text: str) -> int:
    return sum(1 for c in text if "가" <= c <= "힣")


def extract_features(pcm16: bytes | None, text: str = "") -> dict:
    """PCM16 16 kHz 모노에서 rms_db, duration_s, rate_cps 를 계산한다. 입력이 없으면 빈 dict."""
    if not pcm16 or len(pcm16) < 2:
        return {}
    x = np.frombuffer(pcm16[: len(pcm16) // 2 * 2], dtype="<i2").astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    dur = len(x) / 16000.0
    return {"rms_db": 20.0 * np.log10(rms + 1e-9), "duration_s": dur, "rate_cps": hangul_count(text) / dur}


def pick_tone(emotion: str | None, features: dict) -> str:
    """감정 태그가 우선이다. NEUTRAL·없음이면 에너지가 매우 낮고 말이 느릴 때만 comfort, 아니면 calm."""
    tone = EMOTION_TONE.get((emotion or "").upper())
    if tone:
        return tone
    rms, dur, rate = features.get("rms_db"), features.get("duration_s"), features.get("rate_cps")
    if rms is not None and dur is not None and rate is not None \
            and dur >= MIN_DURATION_S and rms < LOW_RMS_DB and rate < SLOW_RATE_CPS:
        return COMFORT
    return CALM
