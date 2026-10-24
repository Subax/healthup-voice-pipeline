"""사용자 목소리와 얼굴 표정 감정을 LLM에게 줄 어조 지시(hint)로 바꾼다.

(webapp_plan.md §4.5, §4.6)
감정 태그는 STT(SenseVoice)와 브라우저 얼굴 표정 모델(F1/F2)이 준다.
틀릴 수 있으므로 지시는 참고로만 쓰고, 이용자의 말 내용이 우선이라고 함께 적는다.
화면·로그·보호자 보고서에는 내보내지 않는다. 이번 턴의 LLM 요청에만 들어간다.
"""

from __future__ import annotations

from typing import Any

_NOTE = " 다만 목소리와 표정 판단은 틀릴 수 있으니 이용자가 한 말의 내용을 먼저 따릅니다."
_VOICE_NOTE = " 다만 목소리 판단은 틀릴 수 있으니 이용자가 한 말의 내용을 먼저 따릅니다."

# 음성 전용 어조 지시 문구 (R0, R2, R6)
_HINTS = {
    "SAD": "이용자의 목소리가 가라앉고 슬프게 들립니다. 서두르지 말고 부드럽게 마음을 먼저 알아주세요.",
    "FEARFUL": "이용자의 목소리가 불안하게 들립니다. 안심이 되는 짧고 차분한 말로 시작하세요.",
    "ANGRY": "이용자의 목소리가 화나 보입니다. 변명하거나 가르치지 말고 속상한 마음을 먼저 인정해 주세요.",
    "DISGUSTED": "이용자의 목소리가 불쾌하게 들립니다. 먼저 불편한 마음을 인정하고 차분하게 들어 주세요.",
    "HAPPY": "이용자의 목소리가 밝게 들립니다. 함께 기뻐하되 들뜨지 말고 차분한 어조를 지키세요.",
    "SURPRISED": "이용자가 놀란 듯합니다. 침착한 말투로 천천히 상황을 물어보세요.",
}

# R1: 목소리와 표정이 같은 쪽 부정 ("목소리와 표정이 모두" 문형)
_HINTS_R1 = {
    "SAD": "이용자의 목소리와 표정이 모두 가라앉고 슬퍼 보입니다. 서두르지 말고 부드럽게 마음을 먼저 알아주세요.",
    "FEARFUL": "이용자의 목소리와 표정이 모두 불안해 보입니다. 안심이 되는 짧고 차분한 말로 시작하세요.",
    "ANGRY": "이용자의 목소리와 표정이 모두 화나 보입니다. 변명하거나 가르치지 말고 속상한 마음을 먼저 인정해 주세요.",
    "DISGUSTED": "이용자의 목소리와 표정이 모두 불쾌해 보입니다. 먼저 불편한 마음을 인정하고 차분하게 들어 주세요.",
}

# R3: 음성 중립/모름 + 표정 부정 ("표정이 ~해 보일 수 있습니다" 약한 문형)
_HINTS_R3 = {
    "SAD": "이용자의 표정이 조금 가라앉아 보일 수 있습니다. 서두르지 말고 부드럽게 대화를 이어가세요.",
    "FEARFUL": "이용자의 표정이 다소 불안해 보일 수 있습니다. 안심이 되는 차분한 말로 대화를 이어가세요.",
    "ANGRY": "이용자의 표정이 다소 굳어 보일 수 있습니다. 단정하지 말고 마음을 존중하며 들어 주세요.",
    "DISGUSTED": "이용자의 표정이 다소 불편해 보일 수 있습니다. 먼저 차분하게 경청해 주세요.",
}

# R5: 긍정 ↔ 부정 충돌 지시
_HINT_R5 = "이용자의 기분을 단정하지 말고 말씀 내용에 맞춰 차분하게 경청해 주세요."

_NEGATIVES = {"SAD", "FEARFUL", "ANGRY", "DISGUSTED"}
_POSITIVES = {"HAPPY", "SURPRISED"}
_SURPRISED = {"SURPRISED"}

_FACE_NORM = {
    "HAPPY": "HAPPY", "HAPPINESS": "HAPPY", "SMILE": "HAPPY",
    "SAD": "SAD", "SADNESS": "SAD",
    "ANGRY": "ANGRY", "ANGER": "ANGRY", "FROWN": "ANGRY",
    "FEARFUL": "FEARFUL", "FEAR": "FEARFUL",
    "DISGUSTED": "DISGUSTED", "DISGUST": "DISGUSTED",
    "SURPRISED": "SURPRISED", "SURPRISE": "SURPRISED",
    "NEUTRAL": "NEUTRAL",
}

_VOICE_NORM = {
    "SAD": "SAD",
    "FEARFUL": "FEARFUL",
    "ANGRY": "ANGRY",
    "DISGUSTED": "DISGUSTED",
    "HAPPY": "HAPPY",
    "SURPRISED": "SURPRISED",
    "NEUTRAL": "NEUTRAL",
    "EMO_UNKNOWN": "NEUTRAL",
}


def emotion_hint(emotion: str | None) -> str | None:
    """감정 태그에 맞는 어조 지시. 중립이거나 모르면 None."""
    clean = (emotion or "").strip("<|> ").upper()
    norm = _VOICE_NORM.get(clean, clean)
    base = _HINTS.get(norm)
    return base + _VOICE_NOTE if base else None


def _norm_voice(emo: str | None) -> str:
    if not emo:
        return "NEUTRAL"
    clean = emo.strip("<|> ").upper()
    return _VOICE_NORM.get(clean, "NEUTRAL")


def _norm_face(lbl: str | None) -> str:
    if not lbl:
        return "NEUTRAL"
    clean = lbl.strip().upper()
    return _FACE_NORM.get(clean, clean)


def is_valid_face_summary(summary: dict[str, Any] | None, *, min_coverage: float = 0.4, min_conf: float = 0.4) -> bool:
    """얼굴 요약의 유효성 검사 (§4.6: 관측 비율 ≥ 기준, 신뢰도 ≥ 기준)."""
    if not isinstance(summary, dict):
        return False
    raw_label = summary.get("label")
    if not raw_label or not isinstance(raw_label, str):
        return False
    cov = float(summary.get("coverage", 0.0))
    total_frames = summary.get("total_frames")
    if total_frames is not None and float(total_frames) > 0:
        frame_cov = float(summary.get("n_frames", 0)) / float(total_frames)
        cov = min(cov, frame_cov) if cov > 0 else frame_cov
    if cov > 1.0:
        cov /= 100.0
    conf = float(summary.get("conf", 0.0))
    if conf > 1.0:
        conf /= 100.0
    return cov >= min_coverage and conf >= min_conf


def fused_hint(
    voice_emotion: str | None,
    face_summary: dict[str, Any] | None,
    *,
    face_enabled: bool = True,
) -> tuple[str | None, str]:
    """음성 감정과 얼굴 표정 요약을 합친 어조 지시 (§4.6).

    반환: (hint 문장 | None, 규칙 번호: 'R0'~'R6')
    """
    # R0: 얼굴 표정 기능 꺼짐 또는 요약이 유효하지 않음 -> 기존 음성 감정 hint 유지
    if not face_enabled or not is_valid_face_summary(face_summary):
        return emotion_hint(voice_emotion), "R0"

    v = _norm_voice(voice_emotion)
    f = _norm_face(face_summary.get("label"))

    # 1. 음성이 부정일 때
    if v in _NEGATIVES:
        if f == v:
            # R1: 같은 쪽 부정 ("목소리와 표정이 모두" 문형)
            hint_str = _HINTS_R1.get(v, _HINTS_R1["SAD"]) + _NOTE
            return hint_str, "R1"
        elif f in _NEGATIVES:
            # R2: 다른 부정 (음성 우선)
            return emotion_hint(voice_emotion), "R2"
        elif f in _POSITIVES:
            # R5: 부정 ↔ 긍정·놀람 충돌
            return _HINT_R5 + _NOTE, "R5"
        else:
            # 표정 중립 등
            return emotion_hint(voice_emotion), "R0"

    # 2. 음성이 긍정·놀람일 때
    elif v in _POSITIVES:
        if f in _POSITIVES:
            # R6: 긍정 ↔ 긍정 / 놀람
            return emotion_hint(voice_emotion), "R6"
        elif f in _NEGATIVES:
            # R5: 긍정·놀람 ↔ 부정 충돌
            return _HINT_R5 + _NOTE, "R5"
        else:
            # 표정 중립 등
            return emotion_hint(voice_emotion), "R0"

    # 3. 음성이 중립 또는 모름일 때
    else:
        if f in _NEGATIVES:
            # R3: 중립·모름 + 부정 표정 ("표정이 ~해 보일 수 있습니다" 약한 문형)
            hint_str = _HINTS_R3.get(f, _HINTS_R3["SAD"]) + _NOTE
            return hint_str, "R3"
        elif f in _POSITIVES:
            # R4: 중립·모름 + 긍정·놀람 표정 -> hint 없음
            return None, "R4"
        else:
            # 둘 다 중립/모름
            return None, "R0"
