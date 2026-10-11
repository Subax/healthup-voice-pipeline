"""STT·VAD 테스트. 실제 모델이 필요한 것은 모델 파일이 있을 때만 돈다."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

from voice_pipeline.stt import (
    STTEngine,
    STTResult,
    SenseVoiceSTT,
    SpeechEnd,
    SpeechStart,
    VADSegmenter,
)

ROOT = Path(__file__).resolve().parents[1]
VAD_MODEL = ROOT / "models" / "silero_vad.onnx"
SV_DIR = ROOT / "models" / "sense-voice-int8"
KO_WAV = SV_DIR / "test_wavs" / "ko.wav"

need_vad = pytest.mark.skipif(not VAD_MODEL.exists(), reason="silero_vad.onnx 없음")
need_sv = pytest.mark.skipif(
    not (SV_DIR / "model.int8.onnx").exists() or not (SV_DIR / "tokens.txt").exists() or not KO_WAV.exists(),
    reason="SenseVoice 모델 없음",
)


def _chunks(pcm: bytes, ms: int = 20):
    n = 16 * ms * 2
    for i in range(0, len(pcm), n):
        yield pcm[i : i + n]


def _feed_all(vad: VADSegmenter, pcm: bytes, ms: int = 20):
    ev = []
    for c in _chunks(pcm, ms):
        ev += vad.feed(c)
    return ev


def test_sensevoice_is_engine():
    assert isinstance(SenseVoiceSTT(), STTEngine)
    assert STTResult(text="a").elapsed_ms == 0.0


@need_vad
def test_vad_silence_emits_nothing():
    vad = VADSegmenter(str(VAD_MODEL))
    assert _feed_all(vad, bytes(16000 * 2 * 3)) == []


@need_vad
def test_vad_noise_and_odd_chunk_sizes():
    vad = VADSegmenter(str(VAD_MODEL))
    rng = np.random.default_rng(0)
    pcm = (rng.normal(0, 30, 16000 * 2)).astype(np.int16).tobytes()
    # 윈도(512)에 안 맞는 조각 크기로 넣어도 죽지 않는다
    ev = []
    for i in range(0, len(pcm), 777):
        ev += vad.feed(pcm[i : i + 777])
    assert not any(isinstance(e, SpeechEnd) for e in ev)
    vad.reset()


def _load_ko() -> bytes:
    sys.path.insert(0, str(ROOT / "bench"))
    from stt_bench import load_wav_16k

    return load_wav_16k(KO_WAV)


@need_sv
async def test_sensevoice_transcribes_ko():
    stt = SenseVoiceSTT()
    await stt.warmup()
    r = await stt.transcribe(_load_ko())
    assert r.text.strip()
    assert "<|" not in r.text
    assert r.elapsed_ms > 0
    assert any("가" <= ch <= "힣" for ch in r.text)
    # 2025-09-09 모델은 한자가 섞이고 lang=yue로 나왔다. 2024-07-17 모델을 쓴다
    assert not any("\u4e00" <= ch <= "\u9fff" for ch in r.text)
    assert "훨씬" in r.text


@need_sv
@need_vad
async def test_vad_segments_real_speech():
    speech = _load_ko()
    sil = bytes(16000 * 2)
    pcm = sil + speech + sil + sil
    vad = VADSegmenter(str(VAD_MODEL))
    ev = _feed_all(vad, pcm)
    starts = [e for e in ev if isinstance(e, SpeechStart)]
    ends = [e for e in ev if isinstance(e, SpeechEnd)]
    assert len(starts) == 1 and len(ends) == 1
    assert starts[0].t_ms < ends[0].t_ms
    assert 900 <= starts[0].t_ms <= 2500
    stt = SenseVoiceSTT()
    r = await stt.transcribe(ends[0].pcm16)
    assert r.text.strip()
