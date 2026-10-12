"""침묵 대기 규칙표 (dev-guide §3.1). 아래 숫자는 모두 가설이다. 실측으로 고친다."""

from __future__ import annotations

from dataclasses import dataclass

from voice_pipeline.endpoint.ending import Ending

# --- 가설: 실제 사용자 발화로 조정한다 ---
SMART_TURN_HIGH = 0.7  # 완료 확률이 높다고 보는 기준
SMART_TURN_MID = 0.4  # NONE 규칙의 중간 기준
MAX_SILENCE_MS = 1000  # 대기 상한 (후보 800~1000ms)

WAIT_EF_HIGH = 200  # 종결 어미 + p 높음
WAIT_EF_LOW = 300  # 종결 어미 + p 낮음
WAIT_EC_YO_HIGH = 250  # 연결 어미 + 요, p 높음
WAIT_EC_YO_LOW = 400  # 연결 어미 + 요, p 낮음
WAIT_EC = 650  # 연결 어미·명사 끝 (p 무관)
WAIT_JX_JK = 700  # 조사 끝 (p 무관)
WAIT_IC = 800  # 감탄사·머뭇거림 (p 무관)
WAIT_NONE_HIGH = 300  # 전사 없음: Smart Turn 단독
WAIT_NONE_MID = 600
WAIT_NONE_LOW = 900


@dataclass(frozen=True)
class Decision:
    wait_ms: int
    reason_ko: str
    rule_id: str


def _p_text(p: float | None) -> str:
    return "Smart Turn 없음" if p is None else f"Smart Turn {p:.2f}"


def decide(ending: Ending | None, p: float | None) -> Decision:
    """어미 범주와 완료 확률 p로 침묵 대기 시간을 정한다. p=None이면 p 낮음 열을 쓴다."""
    high = p is not None and p >= SMART_TURN_HIGH
    cat = ending.category if ending else "NONE"
    m = ending.morph if ending else ""
    d = _decide(cat, m, p, high)
    return Decision(min(d.wait_ms, MAX_SILENCE_MS), d.reason_ko, d.rule_id)


def _decide(cat: str, m: str, p: float | None, high: bool) -> Decision:
    pt = _p_text(p)
    if cat == "EF":
        w = WAIT_EF_HIGH if high else WAIT_EF_LOW
        return Decision(w, f"종결 어미 '-{m}' · {pt} → {w}ms 대기", "EF_HIGH" if high else "EF_LOW")
    if cat == "EC_YO":
        w = WAIT_EC_YO_HIGH if high else WAIT_EC_YO_LOW
        return Decision(w, f"연결 어미 + 요 '-{m}요' · {pt} → {w}ms 대기", "EC_YO_HIGH" if high else "EC_YO_LOW")
    if cat == "EC":
        return Decision(WAIT_EC, f"연결 어미 '-{m}' → 문장이 끝나지 않음, {WAIT_EC}ms 대기", "EC")
    if cat == "NOUN":
        return Decision(WAIT_EC, f"명사 '{m}'로 끝남 → 조사가 빠져 말이 이어질 수 있음, {WAIT_EC}ms 대기", "NOUN")
    if cat == "JX_JK":
        return Decision(WAIT_JX_JK, f"조사 '-{m}'로 끝남 → 할 말이 더 있다고 판단, {WAIT_JX_JK}ms 대기", "JX_JK")
    if cat == "IC":
        return Decision(WAIT_IC, f"머뭇거림 '{m}' → 말이 이어질 것, {WAIT_IC}ms 대기", "IC")
    # NONE: 전사 없음. Smart Turn만 본다
    if p is None:
        return Decision(WAIT_NONE_LOW, f"전사 없음 · {pt} → {WAIT_NONE_LOW}ms 대기", "NONE_LOW")
    if high:
        return Decision(WAIT_NONE_HIGH, f"전사 없음 · {pt} → {WAIT_NONE_HIGH}ms 대기", "NONE_HIGH")
    if p >= SMART_TURN_MID:
        return Decision(WAIT_NONE_MID, f"전사 없음 · {pt} → {WAIT_NONE_MID}ms 대기", "NONE_MID")
    return Decision(WAIT_NONE_LOW, f"전사 없음 · {pt} → {WAIT_NONE_LOW}ms 대기", "NONE_LOW")
