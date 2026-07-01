from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Awaitable, Callable, Protocol, cast

from z_pulse.monitoring.session_store import SessionRef, SessionStore

logger = logging.getLogger(__name__)


class SessionStoreLike(Protocol):
    def load(self) -> SessionRef | None: ...

    def save(self, session: SessionRef) -> SessionRef | None: ...

    def clear_runtime_identity(self) -> SessionRef | None: ...


class CleanupResult(dict):
    def __bool__(self) -> bool:
        return bool(self.get("ok", False))


def _cleanup_step_ok(result: object) -> bool:
    """Cleanup executor results may be legacy bools or structured result dicts."""
    if isinstance(result, dict):
        return bool(result.get("ok", False))
    return bool(result)


def _cleanup_step_details(result: object) -> dict[str, object]:
    if isinstance(result, dict):
        return dict(result)
    return {}


class CleanupOrchestrator:
    def __init__(
        self,
        executor,
        session_lookup: Callable[[str], SessionRef | None] | None = None,
        session_store_factory: Callable[[str], SessionStoreLike] | None = None,
    ):
        self.executor = executor
        self.session_lookup = session_lookup
        self.session_store_factory = session_store_factory

    def _emit_cleanup_event(
        self,
        event_name: str,
        request_id: str | None,
        scope_type: str,
        scope_size: int,
        attempt: int,
        remaining_count: int,
        path: str,
    ) -> None:
        logger.info(
            "[CLEANUP][EVENT] event=%s request_id=%s scope_type=%s scope_size=%d attempt=%d path=%s remaining_count=%d",
            event_name,
            request_id or "none",
            scope_type,
            scope_size,
            attempt,
            path,
            remaining_count,
        )

    def _resolve_session(
        self,
        session: SessionRef | str,
        reason: str,
    ) -> tuple[SessionRef | None, SessionStoreLike | None, str]:
        session_store: SessionStoreLike | None = None
        dir_name = session if isinstance(session, str) else session.dir_name

        if isinstance(session, str):
            if self.session_lookup is not None:
                session_ref = self.session_lookup(session)
            elif self.session_store_factory is not None:
                session_store = self.session_store_factory(session)
                session_ref = session_store.load()
            else:
                logger.warning(
                    f"[CLEANUP][SKIP] dir={dir_name} reason=no_session_lookup requested_reason={reason}"
                )
                return None, None, dir_name
        else:
            session_ref = session

        if session_ref is None:
            logger.warning(
                f"[CLEANUP][SKIP] dir={dir_name} reason=session_not_found requested_reason={reason}"
            )
            return None, None, dir_name

        if session_store is None and session_ref.data_dir:
            session_store = SessionStore(session_ref.data_dir)
        elif session_store is None and self.session_store_factory is not None:
            session_store = self.session_store_factory(session_ref.dir_name)

        return session_ref, session_store, dir_name

    async def _cleanup_single_session(
        self,
        session_ref: SessionRef,
        session_store: SessionStoreLike | None,
        *,
        reason: str,
    ) -> CleanupResult:
        logger.info(
            f"[CLEANUP][REQUEST] dir={session_ref.dir_name} reason={reason} source={session_ref.source}"
        )

        stopping_session = replace(
            session_ref,
            status="stopping",
            source="cleanup_orchestrator",
        )
        if session_store is not None:
            session_store.save(stopping_session)

        attempted_steps: list[str] = []
        step_results: dict[str, dict[str, object]] = {}
        process_killed = False

        if stopping_session.pgid is not None:
            attempted_steps.append("pgid")
            logger.info(f"[CLEANUP][TRY] dir={stopping_session.dir_name} step=pgid")
            result = await self.executor.terminate_process_group(stopping_session)
            details = _cleanup_step_details(result)
            if details:
                step_results["pgid"] = details
            if _cleanup_step_ok(result):
                process_killed = True
                logger.info(f"[CLEANUP][DONE] dir={stopping_session.dir_name} step=pgid")
            else:
                logger.warning(f"[CLEANUP][MISS] dir={stopping_session.dir_name} step=pgid")

        tty_cleanup_required = (
            reason != "process_exit_detected"
            and bool(stopping_session.tty)
            and (not process_killed or reason == "dashboard_cleanup")
        )
        if tty_cleanup_required:
            attempted_steps.append("tty")
            logger.info(f"[CLEANUP][TRY] dir={stopping_session.dir_name} step=tty")
            result = await self.executor.terminate_tty_processes(stopping_session)
            details = _cleanup_step_details(result)
            if details:
                step_results["tty"] = details
            if _cleanup_step_ok(result):
                process_killed = True
                logger.info(f"[CLEANUP][DONE] dir={stopping_session.dir_name} step=tty")
            else:
                logger.warning(f"[CLEANUP][MISS] dir={stopping_session.dir_name} step=tty")

        window_closed = False
        window_closed_by_window_step = False

        if stopping_session.window_id:
            attempted_steps.append("window")
            logger.info(f"[CLEANUP][TRY] dir={stopping_session.dir_name} step=window")
            result = await self.executor.close_window(stopping_session)
            details = _cleanup_step_details(result)
            if details:
                step_results["window"] = details
            if _cleanup_step_ok(result):
                window_closed = True
                window_closed_by_window_step = True
                logger.info(f"[CLEANUP][DONE] dir={stopping_session.dir_name} step=window")
            else:
                logger.warning(f"[CLEANUP][MISS] dir={stopping_session.dir_name} step=window")

        cleanup_terminal_candidate = getattr(self.executor, "cleanup_terminal", None)
        has_cleanup_terminal = callable(cleanup_terminal_candidate) and (
            hasattr(type(self.executor), "cleanup_terminal")
            or "cleanup_terminal" in getattr(self.executor, "__dict__", {})
        )
        cleanup_terminal: Callable[[str], Awaitable[bool]] | None = (
            cast(Callable[[str], Awaitable[bool]], cleanup_terminal_candidate)
            if has_cleanup_terminal
            else None
        )

        if not window_closed and stopping_session.custom_title and cleanup_terminal is not None:
            attempted_steps.append("title_fallback")
            logger.info(f"[CLEANUP][TRY] dir={stopping_session.dir_name} step=title_fallback")
            result = await cleanup_terminal(stopping_session.custom_title)
            details = _cleanup_step_details(result)
            if details:
                step_results["title_fallback"] = details
            if _cleanup_step_ok(result):
                window_closed = True
                logger.info(f"[CLEANUP][DONE] dir={stopping_session.dir_name} step=title_fallback")
            else:
                logger.warning(f"[CLEANUP][MISS] dir={stopping_session.dir_name} step=title_fallback")

        cleanup_terminal_broad_candidate = getattr(self.executor, "cleanup_terminal_broad", None)
        has_cleanup_terminal_broad = callable(cleanup_terminal_broad_candidate) and (
            hasattr(type(self.executor), "cleanup_terminal_broad")
            or "cleanup_terminal_broad" in getattr(self.executor, "__dict__", {})
        )
        cleanup_terminal_broad: Callable[[str], Awaitable[bool]] | None = (
            cast(Callable[[str], Awaitable[bool]], cleanup_terminal_broad_candidate)
            if has_cleanup_terminal_broad
            else None
        )

        allow_broad_terminal_cleanup = reason != "process_exit_detected"

        if not window_closed and allow_broad_terminal_cleanup and cleanup_terminal_broad is not None:
            attempted_steps.append("broad_fallback")
            logger.info(f"[CLEANUP][TRY] dir={stopping_session.dir_name} step=broad_fallback")
            result = await cleanup_terminal_broad(stopping_session.dir_name)
            details = _cleanup_step_details(result)
            if details:
                step_results["broad_fallback"] = details
            if _cleanup_step_ok(result):
                window_closed = True
                logger.info(f"[CLEANUP][DONE] dir={stopping_session.dir_name} step=broad_fallback")
            else:
                logger.warning(f"[CLEANUP][MISS] dir={stopping_session.dir_name} step=broad_fallback")

        if window_closed_by_window_step and allow_broad_terminal_cleanup and cleanup_terminal_broad is not None:
            attempted_steps.append("broad_post_window")
            logger.info(f"[CLEANUP][TRY] dir={stopping_session.dir_name} step=broad_post_window")
            result = await cleanup_terminal_broad(stopping_session.dir_name)
            details = _cleanup_step_details(result)
            if details:
                step_results["broad_post_window"] = details
            if _cleanup_step_ok(result):
                logger.info(f"[CLEANUP][DONE] dir={stopping_session.dir_name} step=broad_post_window")
            else:
                logger.warning(f"[CLEANUP][MISS] dir={stopping_session.dir_name} step=broad_post_window")

        snapshot: dict[str, object] = {
            "dir_name": stopping_session.dir_name,
            "attempted_steps": attempted_steps,
            "process_killed": process_killed,
            "window_closed": window_closed,
        }
        if step_results:
            snapshot["step_results"] = step_results

        require_window_close = bool(
            stopping_session.window_id
            or stopping_session.custom_title
            or has_cleanup_terminal_broad
        )
        cleanup_ok = (process_killed or window_closed) and (window_closed or not require_window_close)

        if cleanup_ok:
            if session_store is not None:
                cleaned_evidence = {
                    k: v for k, v in (stopping_session.evidence or {}).items()
                    if k != "pid_alive"
                }
                session_store.save(replace(stopping_session, status="closed", evidence=cleaned_evidence))
                session_store.clear_runtime_identity()
            logger.info(
                f"[CLEANUP][DONE] dir={stopping_session.dir_name} "
                f"process_killed={process_killed} window_closed={window_closed}"
            )
            return CleanupResult({"ok": True, "scope": "single", "snapshot": snapshot})

        if process_killed and not window_closed and require_window_close:
            logger.warning(
                f"[CLEANUP][FAIL] dir={stopping_session.dir_name} reason=window_cleanup_missed steps={attempted_steps}"
            )
            snapshot["failure_reason"] = "window_cleanup_missed"
            return CleanupResult({"ok": False, "scope": "single", "snapshot": snapshot})

        if not attempted_steps:
            logger.warning(
                f"[CLEANUP][FAIL] dir={stopping_session.dir_name} reason=no_cleanup_target steps=[]"
            )
            snapshot["failure_reason"] = "no_cleanup_target"
            return CleanupResult({"ok": False, "scope": "single", "snapshot": snapshot})

        logger.warning(
            f"[CLEANUP][FAIL] dir={stopping_session.dir_name} reason=all_steps_missed steps={attempted_steps}"
        )
        snapshot["failure_reason"] = "all_steps_missed"
        return CleanupResult({"ok": False, "scope": "single", "snapshot": snapshot})

    async def request_cleanup(
        self,
        session: SessionRef | str,
        *,
        reason: str,
        scope_type: str = "single",
        scope_targets: list[str] | None = None,
        max_attempts: int = 3,
        backoff_seconds: float = 0.15,
        request_id: str | None = None,
    ) -> CleanupResult:
        if scope_type == "batch":
            return await self.request_cleanup_batch(
                scope_targets or [],
                reason=reason,
                max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                request_id=request_id,
            )

        session_ref, session_store, _ = self._resolve_session(session, reason)
        if session_ref is None:
            if reason == "restart_cleanup":
                self._emit_cleanup_event("cleanup_success", request_id, "single", 1, 1, 0, "lookup")
                return CleanupResult(
                    {
                        "ok": True,
                        "scope": "single",
                        "snapshot": {"result": "session_not_found_noop"},
                    }
                )
            self._emit_cleanup_event("cleanup_fail", request_id, "single", 1, 1, 1, "lookup")
            return CleanupResult({"ok": False, "scope": "single", "snapshot": {"failure_reason": "session_not_found"}})

        attempts = max(1, max_attempts)
        last_result = CleanupResult({"ok": False, "scope": "single", "snapshot": {"failure_reason": "unknown"}})
        for attempt in range(1, attempts + 1):
            result = await self._cleanup_single_session(session_ref, session_store, reason=reason)
            last_result = result
            path = "window_id" if session_ref.window_id else ("title_tty" if session_ref.custom_title and session_ref.tty else "broad")
            self._emit_cleanup_event(
                "cleanup_progress",
                request_id,
                "single",
                1,
                attempt,
                0 if result else 1,
                path,
            )
            if result:
                self._emit_cleanup_event("cleanup_success", request_id, "single", 1, attempt, 0, path)
                return result
            if attempt < attempts:
                await asyncio.sleep(backoff_seconds)

        self._emit_cleanup_event("cleanup_fail", request_id, "single", 1, attempts, 1, "max_attempt_reached")
        return last_result

    async def request_cleanup_batch(
        self,
        scope_targets: list[str],
        *,
        reason: str,
        max_attempts: int = 3,
        backoff_seconds: float = 0.15,
        request_id: str | None = None,
    ) -> CleanupResult:
        locked_scope = list(scope_targets)
        attempts = max(1, max_attempts)
        remaining_targets: list[str] = list(locked_scope)

        for attempt in range(1, attempts + 1):
            next_remaining: list[str] = []
            for target in remaining_targets:
                session_ref, session_store, _ = self._resolve_session(target, reason)
                if session_ref is None:
                    if reason == "restart_cleanup":
                        self._emit_cleanup_event(
                            "cleanup_progress",
                            request_id,
                            "batch",
                            len(locked_scope),
                            attempt,
                            len(next_remaining),
                            "lookup",
                        )
                        continue
                    next_remaining.append(target)
                    self._emit_cleanup_event(
                        "cleanup_progress",
                        request_id,
                        "batch",
                        len(locked_scope),
                        attempt,
                        len(next_remaining),
                        "lookup",
                    )
                    continue

                result = await self._cleanup_single_session(session_ref, session_store, reason=reason)
                path = "window_id" if session_ref.window_id else (
                    "title_tty" if session_ref.custom_title and session_ref.tty else "broad"
                )
                if not result:
                    next_remaining.append(target)
                self._emit_cleanup_event(
                    "cleanup_progress",
                    request_id,
                    "batch",
                    len(locked_scope),
                    attempt,
                    len(next_remaining),
                    path,
                )

            if not next_remaining:
                self._emit_cleanup_event(
                    "cleanup_success",
                    request_id,
                    "batch",
                    len(locked_scope),
                    attempt,
                    0,
                    "window_id",
                )
                return CleanupResult(
                    {
                        "ok": True,
                        "scope": "batch",
                        "snapshot": {
                            "requested_scope": locked_scope,
                            "scope_size": len(locked_scope),
                            "remaining": [],
                            "attempts": attempt,
                        },
                    }
                )

            remaining_targets = next_remaining
            if attempt < attempts:
                await asyncio.sleep(backoff_seconds)

        self._emit_cleanup_event(
            "cleanup_fail",
            request_id,
            "batch",
            len(locked_scope),
            attempts,
            len(remaining_targets),
            "max_attempt_reached",
        )
        return CleanupResult(
            {
                "ok": False,
                "scope": "batch",
                "snapshot": {
                    "requested_scope": locked_scope,
                    "scope_size": len(locked_scope),
                    "remaining": remaining_targets,
                    "attempts": attempts,
                },
            }
        )
