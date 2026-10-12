"""STT 엔진 등록부. 웹 데모가 런타임에 엔진을 고를 때 쓴다.

새 엔진 추가: 1) `stt/`에 STTEngine 어댑터 파일을 만든다. 2) 아래 STT_ENGINES에 STTSpec 한 줄을 넣는다.
`available`은 모델 파일·패키지 존재(또는 로컬 서버 응답)만 확인한다. 모델은 올리지 않는다 (싸야 한다).
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Callable

from .base import STTEngine


@dataclass(frozen=True)
class STTSpec:
    id: str
    label: str                              # UI에 보이는 이름
    factory: Callable[[], STTEngine]        # 엔진 객체 생성 (모델 로드는 warmup에서)
    available: Callable[[], bool]           # 쓸 수 있는지 (모델 파일·패키지 확인만)


def _sensevoice_factory() -> STTEngine:
    from .sensevoice import SenseVoiceSTT
    return SenseVoiceSTT()


def _sensevoice_available() -> bool:
    from .sensevoice import DEFAULT_MODEL_DIR
    return (DEFAULT_MODEL_DIR / "model.int8.onnx").exists() and importlib.util.find_spec("sherpa_onnx") is not None


def _qwen3asr_factory() -> STTEngine:
    from .qwen3asr import Qwen3ASRSTT
    return Qwen3ASRSTT()


def _qwen3asr_available() -> bool:
    """로컬 ASR 서버가 응답하는지 0.2초 안에 확인한다. 어떤 오류든 False."""
    import os
    import urllib.request

    from .qwen3asr import DEFAULT_URL

    url = (os.environ.get("QWEN3ASR_URL") or DEFAULT_URL).rstrip("/") + "/v1/models"
    try:
        with urllib.request.urlopen(url, timeout=0.2) as r:
            return r.status == 200
    except Exception:
        return False


def _mock_factory() -> STTEngine:
    from ..web.mocks import MockSTT
    return MockSTT()


STT_ENGINES: dict[str, STTSpec] = {
    s.id: s for s in (
        STTSpec("sensevoice", "SenseVoice-Small INT8 (CPU)", _sensevoice_factory, _sensevoice_available),
        STTSpec("qwen3asr", "Qwen3-ASR-1.7B (GPU)", _qwen3asr_factory, _qwen3asr_available),
        STTSpec("mock", "가짜 STT (고정 문장)", _mock_factory, lambda: True),
    )
}
