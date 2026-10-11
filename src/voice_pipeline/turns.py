"""turn_id 기반 취소 (개발 포인트 문서 §6.3).

정지 = 현재 turn_id 무효화. 재생·전송 쪽은 무효 turn_id의 결과를 모두 버린다.
큐를 비우는 시점과의 경쟁 상태가 생기지 않는다.
"""

from __future__ import annotations

import asyncio


class TurnGuard:
    """현재 turn_id만 살아 있다. 나머지 turn_id의 결과는 모두 버린다."""

    def __init__(self) -> None:
        self.current = 0
        self._tasks: dict[int, set[asyncio.Task]] = {}

    def next_turn(self) -> int:
        self.current += 1
        return self.current

    def is_live(self, turn_id: int) -> bool:
        return turn_id == self.current

    def track(self, turn_id: int, task: asyncio.Task) -> asyncio.Task:
        bucket = self._tasks.setdefault(turn_id, set())
        bucket.add(task)
        task.add_done_callback(bucket.discard)
        return task

    def invalidate(self) -> int:
        """현재 턴을 무효화하고, 그 턴의 작업을 모두 취소한다. 무효화한 turn_id를 돌려준다."""
        dead = self.current
        self.next_turn()
        for task in list(self._tasks.pop(dead, ())):
            task.cancel()
        return dead
