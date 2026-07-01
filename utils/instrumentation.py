"""Small observability helpers for latency-sensitive paths."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class EventLoopLagProbe:
    def __init__(self, warn_seconds: float = 0.100) -> None:
        self.warn_seconds = warn_seconds

    def observe(self, elapsed_seconds: float) -> None:
        if elapsed_seconds >= self.warn_seconds:
            logger.warning(
                "[LOOP][LAG] elapsed=%.3fs threshold=%.3fs",
                elapsed_seconds,
                self.warn_seconds,
            )


@dataclass(frozen=True)
class OperationCheckpoint:
    name: str
    elapsed: float


class OperationContext:
    def __init__(self, operation: str, *, target: str | None = None) -> None:
        self.operation = operation
        self.target = target
        self.started_at = time.perf_counter()
        self.checkpoints: list[OperationCheckpoint] = []

    def checkpoint(self, name: str) -> None:
        self.checkpoints.append(
            OperationCheckpoint(name=name, elapsed=time.perf_counter() - self.started_at)
        )

    def log_summary(self) -> None:
        total_elapsed = time.perf_counter() - self.started_at
        checkpoints = ",".join(
            f"{checkpoint.name}=%.3fs" % checkpoint.elapsed
            for checkpoint in self.checkpoints
        )
        logger.info(
            "[CRITICAL_PATH] operation=%s target=%s total_elapsed=%.3fs checkpoints=%s",
            self.operation,
            self.target,
            total_elapsed,
            checkpoints,
        )
