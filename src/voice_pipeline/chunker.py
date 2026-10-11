"""LLM 토큰 스트림을 TTS 청크로 자른다 (개발 포인트 문서 §5.2).

- 첫 청크는 짧게 (첫 쉼표에서도 끊는다).
- 이후 청크는 문장 끝에서만 끊는다.
- "숫자." 다음 토큰이 숫자면 끊지 않는다 (36.5 보호).
- 강제 분할은 마지막 공백에서 한다 (어절 보호).
"""

from __future__ import annotations

END_MARKS = (".", "?", "!", "。", "？", "！", "…")
SOFT_MARKS = (",", "、")


class SemanticChunker:
    def __init__(self, first_min: int = 8, min_chars: int = 15, max_chars: int = 40):
        self.first_min, self.min_chars, self.max_chars = first_min, min_chars, max_chars
        self.buf, self.first = "", True

    def feed(self, token: str) -> list[str]:
        out: list[str] = []
        # 버퍼가 "숫자."로 끝나 판단을 미뤘다면, 새 토큰을 보고 결정한다
        if self._held_dot() and not token[:1].isdigit():
            if len(self.buf.strip()) >= self._limit():
                out.append(self._flush())
        self.buf += token
        if self._ready():
            out.append(self._flush())
        elif len(self.buf) >= self.max_chars:
            out.append(self._flush_at_space())
        return [c for c in out if c]

    def close(self) -> list[str]:
        return [self._flush()] if self.buf.strip() else []

    def reset(self) -> None:
        self.buf, self.first = "", True

    def _limit(self) -> int:
        return self.first_min if self.first else self.min_chars

    def _held_dot(self) -> bool:
        return len(self.buf) >= 2 and self.buf[-1] == "." and self.buf[-2].isdigit()

    def _ready(self) -> bool:
        s = self.buf.rstrip()
        if self._held_dot() or len(s) < self._limit():
            return False
        if s.endswith(END_MARKS):
            return True
        return self.first and s.endswith(SOFT_MARKS)  # 첫 청크만 쉼표 허용

    def _flush_at_space(self) -> str:
        cut = self.buf.rfind(" ")
        if cut <= 0:
            return self._flush()
        head, self.buf = self.buf[:cut].strip(), self.buf[cut + 1:]
        self.first = False
        return head

    def _flush(self) -> str:
        head, self.buf = self.buf.strip(), ""
        self.first = False
        return head
