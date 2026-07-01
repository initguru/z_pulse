"""command_status_reader.py — pair_control.db command_ack 상태 조회"""
from __future__ import annotations
import logging
import sqlite3
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

class CommandStatusReader:
    def read_pending(self, db_path: Path) -> list[dict[str, Any]]:
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT command_id, slot_id, command_type, payload_json, status, created_at "
                "FROM commands WHERE status IN ('PENDING', 'PROCESSING') ORDER BY command_id"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []
