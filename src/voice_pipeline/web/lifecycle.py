"""세션 상태 기계 (SessionLifecycle).

대기 (idle) → 진행 (active) → 확인 대기 (confirming) → 종료 중 (closing)
상태 전환 및 확인 대기(5종 확인 질문)를 관리한다.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("voice_pipeline.web.lifecycle")


class SessionState:
    IDLE = "idle"              # 대기: 세션 시작 전 또는 종료 후
    ACTIVE = "active"          # 진행: 정상 대화 진행 중
    CONFIRMING = "confirming"  # 확인 대기: 되돌릴 수 없는 명령 또는 동의 확인 질문 후 응답 대기
    CLOSING = "closing"        # 종료 중: 종료 안내 음성 재생 및 기록 정리 중
    ENDING = "closing"         # 별칭 호환


VALID_STATES = {
    SessionState.IDLE,
    SessionState.ACTIVE,
    SessionState.CONFIRMING,
    SessionState.CLOSING,
}


class SessionLifecycle:
    """세션 상태 기계."""

    def __init__(
        self,
        initial_state: str = SessionState.IDLE,
        on_state_change: Callable[[str, str], Awaitable[None] | None] | None = None,
    ):
        if initial_state == "ending":
            initial_state = SessionState.CLOSING
        if initial_state not in VALID_STATES:
            raise ValueError(f"유효하지 않은 초기 세션 상태: {initial_state}")
        self._state = initial_state
        self._prev_state: str = initial_state
        self._on_state_change = on_state_change
        self.pending_intent: str | None = None
        self.pending_payload: dict[str, Any] = {}

    @property
    def state(self) -> str:
        return self._state

    @property
    def is_idle(self) -> bool:
        return self._state == SessionState.IDLE

    @property
    def is_active(self) -> bool:
        return self._state == SessionState.ACTIVE

    @property
    def is_confirming(self) -> bool:
        return self._state == SessionState.CONFIRMING

    @property
    def is_closing(self) -> bool:
        return self._state in (SessionState.CLOSING, "ending")

    async def transition_to(self, new_state: str) -> None:
        """상태를 전이하고 등록된 콜백을 호출한다."""
        if new_state == "ending":
            new_state = SessionState.CLOSING
        if new_state not in VALID_STATES:
            raise ValueError(f"유효하지 않은 세션 상태: {new_state}")

        old_state = self._state
        if old_state == new_state:
            return

        self._state = new_state
        log.debug("세션 상태 전이: %s -> %s", old_state, new_state)
        if self._on_state_change:
            r = self._on_state_change(old_state, new_state)
            if inspect.isawaitable(r):
                await r

    async def start(self) -> None:
        """대기 상태에서 진행 상태로 전환."""
        await self.transition_to(SessionState.ACTIVE)

    async def request_confirm(self, intent: str, payload: dict[str, Any] | None = None) -> None:
        """확인 대기 상태로 진입."""
        if self._state != SessionState.CONFIRMING:
            self._prev_state = self._state
        self.pending_intent = intent
        self.pending_payload = dict(payload or {})
        await self.transition_to(SessionState.CONFIRMING)

    async def resolve_confirm(self, confirmed: bool) -> tuple[str | None, dict[str, Any]]:
        """확인 대기 상태 해소.

        confirmed=True: 대기 중이던 (intent, payload)를 반환.
        confirmed=False: 취소 처리하고 (None, {})를 반환.
        """
        intent = self.pending_intent
        payload = self.pending_payload
        self.pending_intent = None
        self.pending_payload = {}

        return_state = self._prev_state
        if confirmed:
            if self._state == SessionState.CONFIRMING:
                await self.transition_to(return_state)
            return intent, payload
        else:
            if self._state == SessionState.CONFIRMING:
                await self.transition_to(return_state)
            return None, {}

    async def close(self) -> None:
        """세션 종료: closing 전이 후 idle로 복귀."""
        await self.transition_to(SessionState.CLOSING)
        self.pending_intent = None
        self.pending_payload = {}
        await self.transition_to(SessionState.IDLE)

    async def reset(self) -> None:
        """세션 초기화: 대기 상태로 복귀."""
        self.pending_intent = None
        self.pending_payload = {}
        await self.transition_to(SessionState.IDLE)
