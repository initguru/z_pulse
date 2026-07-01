from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TerminalCleanupSnapshot:
    """Terminal cleanup identity captured before restart mutations.

    ``target`` is only for logging/debugging. Cleanup selectors must use only
    ``window_id`` or ``tty`` so background cleanup cannot re-resolve a newer
    session after a replacement terminal has spawned.
    """

    target: str
    session_id: str | None = None
    status: str | None = None
    source: str | None = None
    pid: int | None = None
    pgid: int | None = None
    window_id: str | None = None
    tty: str | None = None
    custom_title: str | None = None
    request_id: str | None = None

    @property
    def has_identity(self) -> bool:
        return bool(self.pid or self.pgid or self.window_id or self.tty)


@dataclass(frozen=True)
class FallbackResult:
    """Result of a fallback termination path when session identity is unavailable."""

    process_cleared: bool  # True if no surviving processes after barrier re-check
    killed_count: int  # number of processes killed by kill_specific_process
    terminal_closed: bool  # True if terminal close succeeded (or no window to close)
    survivors: int  # number of surviving processes after barrier re-check
    method: Literal["window_id", "title", "none"]  # how the terminal was closed
