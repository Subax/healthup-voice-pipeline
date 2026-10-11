"""STT·VAD 어댑터 (CPU, sherpa-onnx)."""

from .base import STTEngine, STTResult
from .sensevoice import SenseVoiceSTT
from .vad import SpeechEnd, SpeechStart, VADEvent, VADSegmenter

__all__ = [
    "STTEngine",
    "STTResult",
    "SenseVoiceSTT",
    "SpeechStart",
    "SpeechEnd",
    "VADEvent",
    "VADSegmenter",
]
