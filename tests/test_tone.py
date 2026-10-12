"""톤 선택 테스트."""

import numpy as np

from voice_pipeline.tone import (CALM, COMFORT, LOW_RMS_DB, SLOW_RATE_CPS, STEADY, WARM, extract_features,
                                 hangul_count, pick_tone)

SLOW_QUIET = {"rms_db": LOW_RMS_DB - 5, "duration_s": 2.0, "rate_cps": SLOW_RATE_CPS - 1}
NORMAL = {"rms_db": -25.0, "duration_s": 2.0, "rate_cps": 5.0}


def test_emotion_mapping():
    assert pick_tone("SAD", NORMAL) == COMFORT and pick_tone("FEARFUL", {}) == COMFORT
    assert pick_tone("ANGRY", NORMAL) == STEADY and pick_tone("DISGUSTED", {}) == STEADY
    assert pick_tone("HAPPY", NORMAL) == WARM and pick_tone("happy", {}) == WARM
    assert pick_tone("NEUTRAL", NORMAL) == CALM and pick_tone("SURPRISED", NORMAL) == CALM
    assert pick_tone(None, {}) == CALM and pick_tone("UNKNOWN", {}) == CALM


def test_neutral_low_energy_slow_is_comfort():
    assert pick_tone("NEUTRAL", SLOW_QUIET) == COMFORT
    assert pick_tone(None, SLOW_QUIET) == COMFORT
    assert pick_tone("NEUTRAL", {**SLOW_QUIET, "rate_cps": 5.0}) == CALM      # 빠르면 아님
    assert pick_tone("NEUTRAL", {**SLOW_QUIET, "rms_db": -25.0}) == CALM      # 크면 아님
    assert pick_tone("NEUTRAL", {**SLOW_QUIET, "duration_s": 0.3}) == CALM    # 너무 짧음
    assert pick_tone("HAPPY", SLOW_QUIET) == WARM                              # 감정 태그가 우선


def test_extract_features():
    assert extract_features(None) == {} and extract_features(b"") == {}
    pcm = np.full(16000, 3277, dtype="<i2").tobytes()  # 1초, 약 -20 dBFS
    f = extract_features(pcm, "안녕하세요 abc")
    assert abs(f["duration_s"] - 1.0) < 1e-6 and abs(f["rms_db"] + 20) < 0.5 and f["rate_cps"] == 5
    assert hangul_count("가a나1") == 2
