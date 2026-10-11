import pytest

from voice_pipeline.llm import MockLLM
from voice_pipeline.tts import MockTTS


@pytest.fixture
def fast_llm():
    return MockLLM(ttft_ms=40, tokens_per_s=400)


@pytest.fixture
def fast_tts():
    # 오디오 길이는 그대로, 합성은 실시간보다 훨씬 빠르게
    return MockTTS(first_audio_ms=10, rtf=0.02)
