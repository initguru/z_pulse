"""event_relay.py — pair_slot_{id}.db → trade_events → Telegram 발송"""
from __future__ import annotations
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)

class EventRelay:
    def __init__(self, bot: "Bot", chat_id: int):
        self._bot = bot
        self._chat_id = chat_id

    def read_unrelayed(self, db_path: Path) -> list[dict[str, Any]]:
        if not db_path.exists():
            return []
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT event_id, event_type, payload_json, created_at "
                "FROM trade_events WHERE relayed = 0 ORDER BY event_id"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def mark_relayed(self, db_path: Path, event_ids: list[int]) -> None:
        if not event_ids:
            return
        conn = sqlite3.connect(str(db_path))
        placeholders = ",".join("?" * len(event_ids))
        conn.execute(
            f"UPDATE trade_events SET relayed = 1 WHERE event_id IN ({placeholders})",
            event_ids,
        )
        conn.commit()
        conn.close()
