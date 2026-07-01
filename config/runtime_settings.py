"""
Runtime settings provider for setting.env hot-reload.

- mtime 기반 캐시
- 타입 변환 getter 제공
- setting.env 변경 시 다음 조회에서 즉시 반영
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class RuntimeSettingsProvider:
    def __init__(self, env_path: Path | None = None):
        self._lock = Lock()
        self._env_path = env_path or (Path(__file__).resolve().parents[1] / "setting.env")
        self._cache: dict[str, str] = {}
        self._mtime: float | None = None

    @property
    def env_path(self) -> Path:
        return self._env_path

    def set_env_path(self, path: Path | str) -> None:
        with self._lock:
            self._env_path = Path(path)
            self._mtime = None
            self._cache = {}

    def _parse_file(self, path: Path) -> dict[str, str]:
        config: dict[str, str] = {}
        if not path.exists():
            return config

        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip().strip('"\' ')
        except Exception as e:
            logger.warning(f"runtime settings parse failed: {path} ({e})")

        return config

    def _load_if_needed(self) -> None:
        path = self._env_path

        try:
            mtime = path.stat().st_mtime if path.exists() else -1.0
        except Exception:
            mtime = -1.0

        with self._lock:
            if self._mtime == mtime:
                return
            self._cache = self._parse_file(path)
            self._mtime = mtime

    def get_str(self, key: str, default: str = "") -> str:
        self._load_if_needed()
        if key in self._cache:
            return self._cache[key]
        env_val = os.getenv(key)
        if env_val is not None:
            return env_val
        return default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get_str(key, "")
        if value == "":
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.get_str(key, "")
        if value == "":
            return default
        try:
            return int(value)
        except ValueError:
            logger.warning(f"invalid int setting: {key}={value!r}, fallback={default}")
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self.get_str(key, "")
        if value == "":
            return default
        try:
            return float(value)
        except ValueError:
            logger.warning(f"invalid float setting: {key}={value!r}, fallback={default}")
            return default


runtime_settings = RuntimeSettingsProvider()
