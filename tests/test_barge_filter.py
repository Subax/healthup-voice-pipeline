"""barge-in 맞장구 판별 단위 시험."""

import pytest

from voice_pipeline.barge_filter import BACKCHANNEL_MAX_MS, is_backchannel, normalize


@pytest.mark.parametrize("text", ["네", "네.", "네네", "응, 응", "음...", "아, 네.", "그렇죠.", "그렇구나", "맞아요 맞아요",
                                  "아하", "알겠어요", "<|Cough|> 네", "흠", "그럼요!"])
def test_short_backchannel_is_kept(text):
    assert is_backchannel(text, 500)


@pytest.mark.parametrize("text", ["잠깐만요", "아니요", "그건 아닌데요", "네 근데 말씀드릴 게 있어요", "그만", "다시 말해 주세요",
                                  "네 맞아요 그런데 저는", "네 네 네 네 네"])
def test_real_interruption_is_not(text):
    assert not is_backchannel(text, 500)


def test_empty_transcript_is_noise():
    assert is_backchannel("", 400)
    assert is_backchannel("<|Cough|>", 400)


def test_long_utterance_is_never_backchannel():
    assert not is_backchannel("네", BACKCHANNEL_MAX_MS + 1)
    assert not is_backchannel("", BACKCHANNEL_MAX_MS + 1)


def test_normalize_strips_tags_and_punct():
    assert normalize("<|HAPPY|> 아, 네... ") == ["아", "네"]
