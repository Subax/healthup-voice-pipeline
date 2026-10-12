"""STT·VAD 어댑터 (SenseVoice·VAD는 CPU sherpa-onnx, Qwen3-ASR은 로컬 GPU 서버)."""

from .base import STTEngine, STTResult
from .sensevoice import SenseVoiceSTT
from .vad import SpeechEnd, SpeechStart, VADEvent, VADSegmenter, VoiceOnset

__all__ = [
    "STTEngine",
    "STTResult",
    "SenseVoiceSTT",
    "SpeechStart",
    "SpeechEnd",
    "VADEvent",
    "VADSegmenter",
    "VoiceOnset",
]
