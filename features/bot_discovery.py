"""bot_discovery.py — BOT_BASE_DIR에서 슬롯 봇 디렉토리 자동 탐색"""
from __future__ import annotations
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

def discover_slot_dirs(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    slot_dirs = []
    for d in sorted(base_dir.iterdir()):
        if d.is_dir() and d.name.startswith("SLOT-"):
            if (d / "setting.env").exists() or (d / "session.json").exists():
                slot_dirs.append(d)
            else:
                logger.debug(f"스킵: setting.env/session.json 없음 — {d.name}")
    return slot_dirs
