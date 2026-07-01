"""
session_identity.py

외부봇 신원 캡처 로직을 process_actions.py에서 독립 모듈로 추출.
process_control과 process_actions 양측에서 재사용 가능하도록 순환 import 없이 위치.
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psutil

from z_pulse.monitoring.session_store import (
    IdentityGenerationMismatchError,
    SessionRef,
    SessionStore,
)
from z_pulse.platforms.macos import resolve_terminal_window_id_for_identity
from z_pulse.utils.bot_type import is_variational_bot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 보조 헬퍼 (process_actions.py에서 이식)
# ---------------------------------------------------------------------------


def _new_identity_generation() -> str:
    return uuid.uuid4().hex


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_create_time(pid: int | None) -> float | None:
    if pid is None:
        return None
    try:
        return psutil.Process(pid).create_time()
    except (psutil.Error, OSError):
        return None


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def lookup_started_process(
    is_process_running: Callable,
    target: str,
) -> tuple[Any, Path] | None:
    """프로세스 lookup.

    process_actions.py의 _lookup_external_started_process(648-665) 이식.
    is_process_running은 ProcessController.is_process_running 바인딩.
    """
    try:
        result = is_process_running(target, True)
    except Exception:
        result = None

    if isinstance(result, tuple) and len(result) >= 2:
        pairs = result[1]
        if isinstance(pairs, (list, tuple)) and pairs:
            try:
                proc, path = pairs[0]
            except (TypeError, ValueError):
                proc = path = None
            if proc is not None and path is not None:
                return proc, Path(path)

    return None


def capture_external_bot_identity(
    *,
    target: str,
    data_dir: Path,
    process_lookup: Callable[[str], tuple[Any, Path] | None],
    platform: str | None = None,
) -> str | None:
    """외부봇 신원을 캡처하여 session.json에 기록.

    성공 시 identity_generation(str) 반환, 실패 시 None.
    process_lookup이 None을 반환해도 begin은 완료되므로 identity_generation을 반환.
    """
    effective_platform = platform if platform is not None else sys.platform

    # 0. z_flow_runtime으로 이미 시작된 세션에 external_bot을 덮어쓰지 않음
    try:
        existing = SessionStore(data_dir).load()
        if existing is not None and getattr(existing, "runtime_kind", None) == "z_flow_runtime":
            logger.info(
                "[IDENTITY_CAPTURE_SKIP] target=%s reason=z_flow_runtime_already_active",
                target,
            )
            return None
    except Exception:
        pass

    # 1. generation 발급
    identity_generation = _new_identity_generation()

    # 2. begin_runtime_identity — 신원 캡처 시작 기록
    try:
        SessionStore(data_dir).begin_runtime_identity(
            SessionRef(
                session_id=target,
                dir_name=target,
                runtime_kind="external_bot",
                platform="macos" if effective_platform == "darwin" else effective_platform,
                status="starting",
                source="launcher",
                custom_title=target,
                data_dir=str(data_dir),
                auto_restart_24h=is_variational_bot(data_dir),
            ),
            identity_generation=identity_generation,
            operation_id=identity_generation,
            validation_status="starting",
        )
    except Exception as exc:
        logger.warning(
            "[START][IDENTITY_MERGE_SKIP] target_id=%s reason=begin_runtime_identity_failed error_type=%s",
            target,
            type(exc).__name__,
        )
        return None

    # 3. process_lookup → None이면 SKIP 경고 후 identity_generation 반환 (begin은 완료)
    found = process_lookup(target)
    if found is None:
        logger.warning(
            "[START][IDENTITY_MERGE_SKIP] target_id=%s reason=process_lookup_failed hint=check_process_monitor_find_processes",
            target,
        )
        return identity_generation

    proc, _ = found

    # 4. pid 추출
    pid = getattr(proc, "pid", None)
    if pid is None:
        logger.warning(
            "[START][IDENTITY_MERGE_SKIP] target_id=%s reason=missing_pid hint=process_lookup_returned_no_pid",
            target,
        )
        return identity_generation

    # 5. pgid
    pgid: int | None = None
    try:
        pgid = os.getpgid(pid)
    except OSError as exc:
        logger.warning(
            "[START][IDENTITY_MERGE_PARTIAL] target_id=%s pid=%s reason=pgid_lookup_failed hint=pid_persisted_without_pgid error_type=%s",
            target,
            pid,
            type(exc).__name__,
        )

    # 6. tty
    tty: str | None = None
    terminal = getattr(proc, "terminal", None)
    if callable(terminal):
        try:
            proc_tty = terminal()
            tty = str(proc_tty).strip() or None if proc_tty is not None else None
        except Exception as exc:
            logger.warning(
                "[START][IDENTITY_MERGE_PARTIAL] target_id=%s pid=%s pgid=%s reason=terminal_lookup_failed hint=pid_pgid_persisted_without_tty_window_id error_type=%s",
                target,
                pid,
                pgid,
                type(exc).__name__,
            )

    # 7. window_id (darwin 전용)
    window_id: str | None = None
    window_reason = "not_attempted"
    window_matches = 0
    window_tty: str | None = None
    if effective_platform == "darwin" and (tty or target):
        try:
            window_id, window_reason, window_matches, window_tty = resolve_terminal_window_id_for_identity(
                tty=tty,
                custom_title=target,
            )
        except Exception as exc:
            window_reason = "window_id_lookup_failed"
            logger.warning(
                "[START][IDENTITY_MERGE_PARTIAL] target_id=%s pid=%s pgid=%s tty=%s reason=window_id_lookup_failed hint=pid_pgid_tty_persisted_without_window_id error_type=%s",
                target,
                pid,
                pgid,
                tty,
                type(exc).__name__,
            )

    if not tty and window_tty:
        tty = window_tty
        logger.info(
            "[START][IDENTITY_TTY_FROM_WINDOW] target=%s tty=%s window_id=%s",
            target,
            tty,
            window_id,
        )

    if window_reason == "ambiguous":
        logger.warning(
            "[START][IDENTITY_MERGE_AMBIGUOUS] target_id=%s pid=%s pgid=%s tty=%s matches=%s hint=window_id_not_persisted",
            target,
            pid,
            pgid,
            tty,
            window_matches,
        )

    # 8. update_runtime_identity
    now = _utc_now_iso()
    try:
        SessionStore(data_dir).update_runtime_identity(
            identity_generation=identity_generation,
            status="running",
            source="process-monitor",
            pid=pid,
            pgid=pgid,
            tty=tty,
            window_id=window_id,
            pid_create_time=_process_create_time(pid),
            identity_captured_at=now,
            window_captured_at=now if window_id else None,
            validation_status="captured" if (tty or window_id) else "degraded",
            validation_checked_at=now,
            validation_reason=window_reason,
            last_seen_at=now,
            last_running_at=now,
            evidence={"pid_alive": True},
        )
    except IdentityGenerationMismatchError as exc:
        # 9. mismatch → graceful return
        logger.warning(
            "[START][IDENTITY_MERGE_PARTIAL] target_id=%s pid=%s reason=identity_generation_mismatch error_type=%s",
            target,
            pid,
            type(exc).__name__,
        )
        return identity_generation

    # 10. 성공 로그
    if tty or window_id:
        logger.info(
            "[START][IDENTITY_MERGE_SUCCESS] target_id=%s pid=%s pgid=%s tty=%s window_id=%s window_reason=%s",
            target,
            pid,
            pgid,
            tty,
            window_id,
            window_reason,
        )
    else:
        logger.warning(
            "[START][IDENTITY_MERGE_PARTIAL] target_id=%s pid=%s pgid=%s reason=tty_window_id_unavailable hint=pid_pgid_persisted_only window_reason=%s",
            target,
            pid,
            pgid,
            window_reason,
        )

    # 11. identity_generation 반환
    return identity_generation
