"""개발 모드 세션 로그 파일 기록 및 보존 주기 관리 (webapp_plan 작업 B2).

위치: logs/dev/YYYYMMDD/<session_id>.jsonl
형식: 한 줄 = 한 턴 (JSONL)
필드: ts, session_id, utt_id, turn_id, mode, transcript, stt_id, stt_ms,
      emotion, event, face, hint_rule, intent, llm_text, spoken_text,
      interrupted, trace, turn_eval_reason, barge
보존 주기: 기본 7일 (--dev-log-days 7). 서버 기동 시 및 일 단위로 만료 파일 삭제.
데모 모드: mode == "demo"일 경우 파일 및 디렉터리를 일절 생성하지 않음.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
import json
import logging
import os
from pathlib import Path
import time
from typing import Any

log = logging.getLogger("voice_pipeline.web.devlog")

DEFAULT_DEV_LOG_DIR = Path("logs/dev")
DEFAULT_RETENTION_DAYS = 7


def purge_old_logs(
    base_dir: Path,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    """보존 기한이 지난 로그 파일 및 빈 디렉터리를 삭제한다. 삭제된 파일 수를 반환."""
    base = Path(base_dir)
    if not base.exists():
        return 0

    if now is None:
        now = datetime.now()

    cutoff_date_str = (now.date() - timedelta(days=retention_days)).strftime("%Y%m%d")
    cutoff_ts = now.timestamp() - (retention_days * 86400)
    deleted_files = 0

    try:
        for date_dir in list(base.iterdir()):
            if not date_dir.is_dir():
                continue

            dir_name = date_dir.name
            is_date_dir = len(dir_name) == 8 and dir_name.isdigit()

            for file_path in list(date_dir.glob("*.jsonl")):
                try:
                    mtime = file_path.stat().st_mtime
                    if (is_date_dir and dir_name < cutoff_date_str) or (mtime < cutoff_ts):
                        file_path.unlink()
                        deleted_files += 1
                except OSError:
                    log.exception("로그 파일 삭제 실패: %s", file_path)

            try:
                if not any(date_dir.iterdir()):
                    date_dir.rmdir()
            except OSError:
                pass
    except OSError:
        log.exception("로그 디렉터리 순회 실패: %s", base)

    return deleted_files


async def daily_purge_loop(base_dir: Path, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    """24시간 주기로 오래된 로그를 지우는 백그라운드 태스크 루프."""
    try:
        while True:
            await asyncio.sleep(86400)
            purge_old_logs(base_dir, retention_days)
    except asyncio.CancelledError:
        pass


class DevLogger:
    def __init__(self, base_dir: Path | None = None, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.base_dir = Path(base_dir) if base_dir is not None else DEFAULT_DEV_LOG_DIR
        self.retention_days = retention_days

    def write_turn(self, record: dict[str, Any]) -> Path | None:
        """한 턴의 개발 로그를 JSONL로 기록한다. mode가 'dev'가 아니면 아무것도 쓰지 않는다."""
        if record.get("mode") != "dev":
            return None

        now = datetime.now()
        date_str = now.strftime("%Y%m%d")
        session_id = str(record.get("session_id", "default"))
        target_dir = self.base_dir / date_str
        target_dir.mkdir(parents=True, exist_ok=True)

        file_path = target_dir / f"{session_id}.jsonl"
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        return file_path

    def purge(self, now: datetime | None = None) -> int:
        """현재 인스턴스의 base_dir에서 만료된 로그를 정리한다."""
        return purge_old_logs(self.base_dir, self.retention_days, now=now)
