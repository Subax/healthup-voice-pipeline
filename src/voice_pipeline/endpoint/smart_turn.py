"""Smart Turn v3.2 (pipecat-ai, BSD-2) 턴 완료 확률.

사용한 파일 (확인함):
- 모델: Hugging Face `pipecat-ai/smart-turn-v3` 의 `smart-turn-v3.2-cpu.onnx` (약 8.7MB)
  -> `models/smart-turn/smart-turn-v3.2-cpu.onnx`
  https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx
- 참조 코드: https://github.com/pipecat-ai/smart-turn (`inference.py`, `audio_utils.py`, main 브랜치)

전처리는 참조 코드와 같다.
1. 16kHz 모노 float32 (PCM16 / 32768)
2. 마지막 8초만 쓴다. 8초보다 짧으면 앞쪽(왼쪽)을 0으로 채운다
3. `WhisperFeatureExtractor(chunk_length=8)`, padding="max_length", max_length=128000,
   truncation=True, do_normalize=True (torch 없이 numpy로 동작)
4. 입력 `input_features` (1, 80, 800) float32, 출력 `logits` (1, 1) — 이미 sigmoid가 적용된 확률이다
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
WINDOW_SECONDS = 8
DEFAULT_MODEL = "models/smart-turn/smart-turn-v3.2-cpu.onnx"


class EndpointScorer(Protocol):
    """턴 완료 확률 계산기. 입력은 PCM16 모노 16kHz."""

    def score(self, pcm16: bytes) -> float: ...


class MockEndpointScorer:
    """테스트용: 항상 같은 확률을 낸다."""

    def __init__(self, p: float = 0.5) -> None:
        self.p = p

    def score(self, pcm16: bytes) -> float:
        return self.p


def prepare_window(pcm16: bytes) -> np.ndarray:
    """PCM16 -> 최근 8초 float32. 짧으면 앞쪽을 0으로 채운다 (참조 코드와 동일)."""
    audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    n = WINDOW_SECONDS * SAMPLE_RATE
    if len(audio) > n:
        return audio[-n:]
    if len(audio) < n:
        return np.pad(audio, (n - len(audio), 0), mode="constant")
    return audio


class SmartTurnScorer:
    """Smart Turn v3.2 ONNX (CPU). 처음 쓸 때 모델을 읽는다."""

    def __init__(self, model_path: str | Path = DEFAULT_MODEL, num_threads: int = 2) -> None:
        self.model_path = str(model_path)
        self.num_threads = num_threads
        self._session = None
        self._extractor = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._session is not None:
            return
        with self._lock:
            if self._session is not None:
                return
            import onnxruntime as ort
            from transformers import WhisperFeatureExtractor

            so = ort.SessionOptions()
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            so.inter_op_num_threads = 1
            so.intra_op_num_threads = self.num_threads
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            self._extractor = WhisperFeatureExtractor(chunk_length=WINDOW_SECONDS)
            self._session = ort.InferenceSession(
                self.model_path, sess_options=so, providers=["CPUExecutionProvider"]
            )
            log.info("Smart Turn 로드: %s (threads=%d)", self.model_path, self.num_threads)

    def warmup(self) -> None:
        """모델을 읽고 더미 입력으로 한 번 돌린다."""
        self._load()
        self.score(b"\x00\x00" * SAMPLE_RATE)

    def score(self, pcm16: bytes) -> float:
        """턴이 끝났을 확률 0..1."""
        self._load()
        audio = prepare_window(pcm16)
        feats = self._extractor(
            audio,
            sampling_rate=SAMPLE_RATE,
            return_tensors="np",
            padding="max_length",
            max_length=WINDOW_SECONDS * SAMPLE_RATE,
            truncation=True,
            do_normalize=True,
        ).input_features.astype(np.float32)  # (1, 80, 800)
        out = self._session.run(None, {"input_features": feats})
        return float(out[0].reshape(-1)[0])
