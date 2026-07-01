"""Monitoring module for Z-Pulse."""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = ["ProcessMonitor", "LogKeywordMonitor", "DBFileWatcher"]


def __getattr__(name: str) -> Any:
    if name == "ProcessMonitor":
        return import_module(".process_monitor", __name__).ProcessMonitor
    if name == "LogKeywordMonitor":
        return import_module(".keyword_monitor", __name__).LogKeywordMonitor
    if name == "DBFileWatcher":
        return import_module(".db_file_watcher", __name__).DBFileWatcher
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
