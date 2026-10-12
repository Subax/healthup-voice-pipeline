"""endpoint 부품 테스트: Kiwi 어미 분류, 대기 규칙표, Smart Turn 전처리."""

import numpy as np
import pytest

from voice_pipeline.endpoint.ending import Ending, classify_ending
from voice_pipeline.endpoint.rules import MAX_SILENCE_MS, decide
from voice_pipeline.endpoint.smart_turn import MockEndpointScorer, prepare_window

pytest.importorskip("kiwipiepy")


@pytest.mark.parametrize(
    "text,cat",
    [
        ("힘들었는데요", "EC_YO"),
        ("그래서 왔고요", "EC_YO"),
        ("잠을 못 자거든요", "EC_YO"),
        ("힘들어요", "EF"),
        ("괜찮아", "EF"),
        ("그랬는데", "EC"),
        ("먹고", "EC"),
        ("저는", "JX_JK"),
        ("혈압을", "JX_JK"),
        ("혈압", "NOUN"),
        ("음", "IC"),
        ("어 그", "IC"),
        ("그러니까", "IC"),
        ("어떻게 해야 할까요", "EF"),
        ("좋겠다", "EF"),
        ("그런가", "EF"),
        ("했어", "EF"),
        ("", "NONE"),
        ("  . ", "NONE"),
    ],
)
def test_classify(text, cat):
    assert classify_ending(text).category == cat


def test_stt_punct_recorded_not_used():
    e = classify_ending("먹고.")
    assert e.category == "EC" and e.stt_punct
    assert not classify_ending("먹고").stt_punct


def test_rules_table():
    ef = Ending("EF", "어요")
    assert decide(ef, 0.9).wait_ms == 200
    assert decide(ef, 0.5).wait_ms == 300
    assert decide(ef, None).wait_ms == 300
    yo = Ending("EC_YO", "는데")
    assert decide(yo, 0.8).wait_ms == 250
    assert decide(yo, 0.2).wait_ms == 400
    assert decide(Ending("EC", "고"), 0.99).wait_ms == 650
    assert decide(Ending("NOUN", "혈압"), 0.99).wait_ms == 650
    assert decide(Ending("JX_JK", "를"), 0.99).wait_ms == 700
    assert decide(Ending("IC", "음"), 0.99).wait_ms == 800


def test_rules_none_and_cap():
    assert decide(None, 0.8).wait_ms == 300
    assert decide(None, 0.5).wait_ms == 600
    assert decide(None, 0.1).wait_ms == 900
    assert decide(None, None).wait_ms == 900
    assert decide(Ending("NONE"), 0.1).rule_id == "NONE_LOW"
    assert all(decide(Ending(c, "x"), p).wait_ms <= MAX_SILENCE_MS for c in ("EF", "IC", "NONE") for p in (None, 0.1, 0.9))


def test_reason_text():
    d = decide(Ending("EF", "요"), 0.91)
    assert "Smart Turn 0.91" in d.reason_ko and "200ms" in d.reason_ko


def test_prepare_window_pads_left_and_truncates():
    short = (np.ones(1600) * 16384).astype(np.int16).tobytes()
    w = prepare_window(short)
    assert len(w) == 128000 and w[0] == 0 and w[-1] == pytest.approx(0.5)
    long = np.arange(200000, dtype=np.int16).tobytes()
    assert len(prepare_window(long)) == 128000


def test_mock_scorer():
    assert MockEndpointScorer(p=0.3).score(b"") == 0.3
