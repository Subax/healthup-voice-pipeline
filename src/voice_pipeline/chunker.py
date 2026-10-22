"""LLM 토큰 스트림을 TTS 청크로 자른다 (개발 포인트 문서 §5.2).

- 첫 청크는 짧게 (첫 쉼표에서도 끊는다).
- 이후 청크는 문장 끝에서만 끊는다. 토큰 중간에 문장 끝이 있어도 거기서 자른다.
- "숫자." 다음 토큰이 숫자면 끊지 않는다 (36.5 보호).
- 강제 분할은 마지막 공백에서 한다 (어절 보호).
"""

from __future__ import annotations

END_MARKS = (".", "?", "!", "。", "？", "！", "…")
SOFT_MARKS = (",", "、")


class SemanticChunker:
    def __init__(
        self,
        first_min: int = 8,
        min_chars: int = 15,
        max_chars: int = 40,
        skip_filler_comma: bool = False,
    ):
        self.first_min, self.min_chars, self.max_chars = first_min, min_chars, max_chars
        self.skip_filler_comma = skip_filler_comma
        self.buf, self.first = "", True

    def feed(self, token: str) -> list[str]:
        out: list[str] = []
        # 버퍼가 "숫자."로 끝나 판단을 미뤘다면, 새 토큰을 보고 결정한다
        if self._held_dot() and not token[:1].isdigit():
            if len(self.buf.strip()) >= self._limit():
                out.append(self._flush())
        self.buf += token
        # 토큰 하나에 문장 끝이 중간에 들어올 수 있다 (MTP는 토큰을 2~3개씩 한 번에 낸다).
        # 첫 문장 끝에서 바로 자르고 나머지는 버퍼에 남긴다
        cut = self._cut_index()
        while cut is not None:
            out.append(self._flush_head(cut))
            cut = self._cut_index()
        if not out and len(self.buf) >= self.max_chars:
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

    def _cut_index(self) -> int | None:
        """버퍼에서 자를 수 있는 첫 끝 표시의 다음 위치. 없으면 None.

        앞부분이 현재 최소 길이 이상이어야 한다. "숫자."는 뒤 글자를 보고 정한다
        (버퍼 끝이면 다음 토큰까지 미루고, 뒤가 숫자면 소수점으로 본다).
        """
        buf, limit = self.buf, self._limit()
        for i, ch in enumerate(buf):
            end = ch in END_MARKS or (self.first and ch in SOFT_MARKS)
            if not end:
                continue
            if self.skip_filler_comma and self.first and ch in SOFT_MARKS:
                part = buf[: i + 1].strip()
                if part[:-1].strip() in ("네", "아", "음", "예", "응"):
                    continue
            if ch == "." and i > 0 and buf[i - 1].isdigit():
                if i == len(buf) - 1:
                    return None   # 다음 토큰을 보고 정한다
                if buf[i + 1].isdigit():
                    continue      # 소수점
            if len(buf[: i + 1].strip()) >= limit:
                return i + 1
        return None

    def _flush_head(self, cut: int) -> str:
        head, self.buf = self.buf[:cut].strip(), self.buf[cut:].lstrip()
        self.first = False
        return head

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
