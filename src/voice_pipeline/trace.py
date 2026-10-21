"""턴 추적 기록 (개발 포인트 문서 §8.1).

turn_id마다 단조 시계로 시각을 남긴다. 기준점은 t0(사용자 발화 종료 또는 요청 시각)이다.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field

# 표준 마크 이름 (§8.1 순서)
SPEECH_END = "speech_end"        # t0 사용자 발화 종료 (VAD)
TURN_CONFIRMED = "turn_confirmed"  # t1
STT_FINAL = "stt_final"          # t2
LLM_FIRST_TOKEN = "llm_first_token"  # t3
FIRST_CHUNK = "first_chunk"      # t4
BACKCHANNEL = "backchannel"      # 미리 합성한 맞장구의 첫 프레임을 낸 시각
TTS_FIRST_AUDIO = "tts_first_audio"  # t5
PLAYBACK_START = "playback_start"  # t6 (Unreal 회신 또는 로컬 싱크)
SPEC_START = "spec_start"        # 선행 생성 시작 (분석용)
SPEC_COMMIT = "spec_commit"      # 선행 결과를 commit한 시각 (분석용)


@dataclass
class TurnTrace:
    turn_id: int
    marks: dict[str, float] = field(default_factory=dict)

    def mark(self, name: str, t: float | None = None) -> None:
        """같은 이름은 처음 시각만 남긴다."""
        self.marks.setdefault(name, time.monotonic() if t is None else t)

    def since(self, name: str, origin: str = SPEECH_END) -> float | None:
        """origin부터 name까지 걸린 시간(ms)."""
        if name not in self.marks or origin not in self.marks:
            return None
        return (self.marks[name] - self.marks[origin]) * 1000.0

    def to_json(self) -> str:
        t0 = min(self.marks.values()) if self.marks else 0.0
        return json.dumps(
            {"turn_id": self.turn_id,
             "ms": {k: round((v - t0) * 1000.0, 1) for k, v in self.marks.items()}},
            ensure_ascii=False,
        )


def percentile(values: list[float], p: float) -> float:
    """선형 보간 백분위수. p는 0~100."""
    if not values:
        raise ValueError("empty")
    xs = sorted(values)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(traces: list[TurnTrace], name: str, origin: str = SPEECH_END) -> dict[str, float]:
    vals = [v for t in traces if (v := t.since(name, origin)) is not None]
    if not vals:
        return {}
    return {"n": len(vals), "p50": percentile(vals, 50), "p90": percentile(vals, 90),
            "mean": statistics.fmean(vals)}
