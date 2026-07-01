"""
Process Action Handler Module

대시보드 및 상세 화면에서 발생하는 프로세스 제어 액션(시작, 종료, 초기화, Ignore 등)을 담당합니다.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Literal, Optional, Protocol, cast
from telegram import CallbackQuery

import psutil

from z_pulse.config import load_ignored_dirs, get_trading_info_from_env

from z_pulse.bot.utils import run_batch_operations
from z_pulse.features.process_control import clear_bot_state, write_bot_state
from z_pulse.monitoring.cleanup_snapshot import TerminalCleanupSnapshot, FallbackResult
from z_pulse.monitoring.session_identity import (
    _new_identity_generation,
    _process_create_time,
    _utc_now_iso,
)
from z_pulse.monitoring.session_store import IdentityGenerationMismatchError, SessionRef, SessionStore
from z_pulse.utils.bot_type import is_variational_bot
from z_pulse.utils.instrumentation import OperationContext
from z_pulse.utils.telegram_gateway import TelegramPriority, get_telegram_gateway

logger = logging.getLogger(__name__)

OPERATION_LOCK_TTL_SECONDS = 30.0


def _manual_stop_evidence() -> dict[str, Any]:
    return {"manual_stop": True}


def _persist_manual_stop_session(
    data_dir: Path,
    target: str,
    runtime_kind: str,
    source: str,
) -> None:
    now = _utc_now_iso()
    SessionStore(data_dir).merge(
        SessionRef(
            session_id=target,
            dir_name=target,
            runtime_kind=runtime_kind,
            platform="macos" if sys.platform == "darwin" else sys.platform,
            status="stopped",
            source=source,
            custom_title=target,
            data_dir=str(data_dir),
            last_exit_at=now,
            last_exit_reason="manual_stop",
            last_state_signal="MANUAL_STOP",
            evidence=_manual_stop_evidence(),
        )
    )


class ProcessRunningChecker(Protocol):
    def __call__(self, target: str, force_refresh: bool = False) -> tuple[bool, list[Any]]:
        ...


def _get_z_flow_dirs(monitor: Any, bridge_owner: Any | None = None) -> dict[str, Path]:
    bridge = getattr(bridge_owner, "get_runtime_pid_files", None)
    target_dir = getattr(monitor, "target_dir", None)
    ignore_list = getattr(monitor, "ignore_list", None)
    if callable(bridge) and isinstance(target_dir, Path) and isinstance(ignore_list, set):
        runtime_pid_files = bridge(target_dir, ignore_list)
        if isinstance(runtime_pid_files, dict):
            return runtime_pid_files

    attrs = getattr(monitor, "__dict__", {})
    z_flow_dirs = attrs.get("z_flow_dirs")
    if isinstance(z_flow_dirs, dict):
        return z_flow_dirs
    return {}


def _find_z_flow_processes(
    monitor: Any, bridge_owner: Any | None = None
) -> list[tuple[Any | None, str, Path]]:
    bridge = getattr(bridge_owner, "find_runtime_processes", None)
    if callable(bridge):
        runtime_processes = bridge(monitor)
        if isinstance(runtime_processes, list):
            return runtime_processes
        return []

    attrs = getattr(monitor, "__dict__", {})
    find_runtime_processes = attrs.get("find_z_flow_processes")
    if not callable(find_runtime_processes):
        return []
    runtime_processes = find_runtime_processes()
    if isinstance(runtime_processes, list):
        return runtime_processes
    return []


def _find_all_z_flow_os_processes(
    monitor: Any, target: str | Path, bridge_owner: Any | None = None
) -> list[Any]:
    bridge = getattr(bridge_owner, "find_all_runtime_os_processes", None)
    if callable(bridge):
        runtime_processes = bridge(monitor, target)
        if isinstance(runtime_processes, list):
            return runtime_processes
        return []

    find_all = getattr(monitor, "find_all_z_flow_os_processes", None)
    if not callable(find_all):
        return []
    runtime_processes = find_all(target)
    if isinstance(runtime_processes, list):
        return runtime_processes
    return []


def _kill_all_z_flow_processes(
    monitor: Any, target: str | Path, timeout: float = 1.0, bridge_owner: Any | None = None
) -> int:
    """OS 프로세스 테이블에서 해당 Z-Flow 런타임의 모든 인스턴스를 종료.

    PID 파일에 의존하지 않고 psutil cmdline 기반으로 전체 프로세스 트리를
    순회하여 고아/중복 프로세스까지 포함하여 정리합니다.

    Args:
        monitor: ProcessMonitor 인스턴스
        target: Z-Flow 디렉토리 이름 (예: "SLOT-GRVT-1")
        timeout: terminate 후 wait 최대 대기 시간(초)

    Returns:
        종료된 프로세스 수
    """
    procs = _find_all_z_flow_os_processes(monitor, target, bridge_owner)

    # PID-file 기반 primary kill: OS-scan을 보완해 런타임을 정확히 타겟
    data_dir = _resolve_z_flow_target_dir(monitor, str(target), bridge_owner)
    if data_dir is not None:
        pid_file = data_dir / "z_flow.pid"
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except Exception as _exc:
            logger.debug("[Z_FLOW][SWEEP] PID-file 읽기 실패: target=%s err=%s", target, _exc)
        else:
            try:
                if psutil.pid_exists(pid) and not any(p.pid == pid for p in procs):
                    procs = list(procs) + [psutil.Process(pid)]
            except psutil.NoSuchProcess:
                pass  # pid_exists 확인 직후 프로세스 사망 — 무해

    if not procs:
        return 0

    killed = 0
    # 자식 프로세스 먼저 종료하기 위해 pid 역순 정렬
    # (부모보다 자식이 나중에 생성되어 pid가 클 가능성 높음)
    procs.sort(key=lambda p: p.pid, reverse=True)

    for proc in procs:
        try:
            logger.info(
                f"[Z_FLOW][SWEEP] Z-Flow 프로세스 종료 시도: {target} pid={proc.pid}"
            )
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except psutil.TimeoutExpired:
                proc.kill()
                logger.warning(f"[Z_FLOW][SWEEP] 강제 종료: {target} pid={proc.pid}")
            killed += 1
        except psutil.NoSuchProcess:
            killed += 1  # 이미 종료됨
        except Exception as e:
            logger.error(
                f"[Z_FLOW][SWEEP] 프로세스 종료 실패: {target} pid={proc.pid} err={e}"
            )

    if killed > 0:
        logger.info(
            f"[Z_FLOW][SWEEP] {target}: {killed}/{len(procs)}개 프로세스 정리 완료"
        )
    return killed


def _resolve_z_flow_target_dir(
    monitor: Any,
    target: str,
    bridge_owner: Any | None = None,
) -> Path | None:
    bridge = bridge_owner
    resolver = getattr(bridge, "resolve_runtime_data_dir", None)
    if callable(resolver):
        data_dir = resolver(target, monitor)
        if isinstance(data_dir, Path):
            return data_dir

    z_flow_dirs = _get_z_flow_dirs(monitor, bridge_owner)
    pid_file = z_flow_dirs.get(target)
    if pid_file is not None:
        return pid_file.parent
    return None


def _is_z_flow_target(target: str, monitor: Any, bridge_owner: Any | None = None) -> bool:
    bridge = bridge_owner
    checker = getattr(bridge, "is_runtime_target", None)
    if callable(checker):
        result = checker(target, monitor)
        if result is True:
            return True
    return _resolve_z_flow_target_dir(monitor, target, bridge_owner) is not None


def _is_external_pair_trading_bot(target_path: Path | None) -> bool:
    if target_path is None or not isinstance(target_path, Path):
        return False
    try:
        from z_pulse.monitoring.bot_state import is_pair_trading_type

        trading_type, _ = get_trading_info_from_env(target_path.parent)
        return is_pair_trading_type(trading_type)
    except Exception:
        return False


class ProcessActionHandler:
    def __init__(
        self,
        bot_instance,
        monitor,
        process_controller,
        file_operations,
        escape_manager=None,
        dashboard_handler=None,
        salsal_manager=None,
    ):
        """
        Args:
            bot_instance: 메인 봇 인스턴스 (필요 시)
            monitor: ProcessMonitor
            process_controller: ProcessController
            file_operations: FileOperations
            escape_manager: EscapeManager
            dashboard_handler: DashboardHandler (화면 갱신용)
            salsal_manager: SalsalManager (선택)
        """
        self.bot = bot_instance
        self.monitor = monitor
        self.process_controller = process_controller
        self.file_operations = file_operations
        self.escape_manager = escape_manager
        self.dashboard_handler: Any = dashboard_handler
        self.salsal_manager = salsal_manager
        self._target_operation_locks: dict[str, tuple[str, float]] = {}
        self._target_latest_generation: dict[str, str] = {}
        self._in_flight: set[str] = set()

    def _get_z_flow_bridge(self):
        return getattr(self.bot, "z_flow_bridge", None)

    def _is_automation_on(self, target: str) -> bool:
        bridge = self._get_z_flow_bridge()
        if bridge is None:
            return False
        if not bridge.is_pair_trading_ui_enabled():
            return False
        return bridge.is_rotation_enabled(target)

    def _require_pair_trading_env(self):
        bridge = self._get_z_flow_bridge()
        if bridge is None:
            raise RuntimeError("z_flow bridge is not available")
        return bridge.require_pair_trading_env()

    def _get_pair_trading_env_path(self):
        bridge = self._get_z_flow_bridge()
        if bridge is None:
            raise RuntimeError("z_flow bridge is not available")
        return bridge.get_pair_trading_env_path()

    def _get_pair_trading_config_error(self):
        bridge = self._get_z_flow_bridge()
        if bridge is None:
            raise RuntimeError("z_flow bridge is not available")
        return bridge.get_pair_trading_config_error()

    def require_pair_trading_env(self):
        return self._require_pair_trading_env()

    def get_pair_trading_env_path(self):
        return self._get_pair_trading_env_path()

    def get_pair_trading_config_error(self):
        return self._get_pair_trading_config_error()

    def _get_slot_type(self, target: str):
        bridge = self._get_z_flow_bridge()
        if bridge is None:
            return None
        return bridge.get_slot_type(target)

    async def _reply_via_message(self, message_source: Any, text: str) -> None:
        reply_text = getattr(message_source, "reply_text", None)
        if not callable(reply_text):
            return
        reply_callable = cast(Callable[[str], Awaitable[Any]], reply_text)
        await reply_callable(text)

    async def _queue_reply_via_message(self, message_source: Any, text: str) -> None:
        reply_text = getattr(message_source, "reply_text", None)
        if not callable(reply_text):
            return
        reply_callable = cast(Callable[[str], Awaitable[Any]], reply_text)
        await get_telegram_gateway().enqueue(
            lambda: reply_callable(text),
            priority=TelegramPriority.USER_ACTION,
            timeout=5.0,
            label="process_action_reply_text",
            wait_result=False,
            drop_ok=True,
        )

    async def _safe_answer_query(self, query: CallbackQuery, *args: Any, **kwargs: Any) -> None:
        await get_telegram_gateway().answer_callback_query(query, *args, **kwargs)

    async def _safe_edit_query_message(
        self,
        query: CallbackQuery,
        text: str,
        **kwargs: Any,
    ) -> None:
        await get_telegram_gateway().edit_message_text(
            query,
            text=text,
            priority=TelegramPriority.USER_ACTION,
            timeout=5.0,
            drop_ok=True,
            **kwargs,
        )

    async def _notify_restart_status(
        self,
        query: CallbackQuery,
        text: str,
        *,
        callback_pre_acked: bool,
        show_alert: bool = False,
    ) -> None:
        if callback_pre_acked:
            await self._safe_edit_query_message(query, text)
            return
        await self._safe_answer_query(query, text, show_alert=show_alert)

    async def _notify_start_status(
        self,
        query: CallbackQuery,
        text: str,
        *,
        callback_pre_acked: bool,
    ) -> None:
        if callback_pre_acked:
            await self._safe_edit_query_message(query, text)
            return
        await self._safe_answer_query(query, text)

    def _try_begin_target_operation(self, target: str, operation_id: str) -> bool:
        now = time.monotonic()
        current = self._target_operation_locks.get(target)
        if current is not None:
            current_operation_id, started_at = current
            if now - started_at < OPERATION_LOCK_TTL_SECONDS:
                logger.info(
                    "[PROC][LOCK][BUSY] operation_id=%s target_id=%s current_operation_id=%s elapsed_ms=%d",
                    operation_id,
                    target,
                    current_operation_id,
                    int((now - started_at) * 1000),
                )
                return False
            logger.warning(
                "[PROC][LOCK][STALE] operation_id=%s target_id=%s stale_operation_id=%s age_ms=%d",
                operation_id,
                target,
                current_operation_id,
                int((now - started_at) * 1000),
            )
        self._target_operation_locks[target] = (operation_id, now)
        return True

    def _finish_target_operation(self, target: str, operation_id: str) -> None:
        current = self._target_operation_locks.get(target)
        if current and current[0] == operation_id:
            self._target_operation_locks.pop(target, None)

    def _is_latest_generation(self, target: str, generation_id: str) -> bool:
        return self._target_latest_generation.get(target) == generation_id

    def _load_dashboard_session_identity(self, target: str) -> tuple[SessionStore | None, SessionRef | None]:
        target_dir = getattr(self.monitor, "target_dir", None)
        if not isinstance(target_dir, (str, Path)):
            return None, None
        store = SessionStore.from_dir_name(Path(target_dir), target)
        return store, store.load()

    def _liveness_check_o1(self, target: str) -> bool:
        """시작 전 살아있음 확인을 O(1) pid 검증으로 수행한다.

        session.json에 pid가 있으면 `psutil.Process(pid).is_running()`로 직접 확인해
        전체 process_iter 스캔(force_refresh=True)을 회피한다. pid가 없으면 캐시된
        스냅샷(force_refresh=False)으로 폴백한다 — 어느 경로도 전체 스캔을 하지 않는다.
        """
        _store, session = self._load_dashboard_session_identity(target)
        snapshot_pid = getattr(session, "pid", None) if session is not None else None
        if snapshot_pid is not None:
            try:
                proc = psutil.Process(snapshot_pid)
                if not (proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE):
                    return False
                # PID reuse 방지: create_time이 기록돼 있으면 비교
                create_time = getattr(session, "pid_create_time", None)
                if create_time is not None:
                    try:
                        if proc.create_time() != create_time:
                            return False
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        return False
                return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                return False
        # pid 없음 — 캐시 스냅샷으로 폴백 (전체 스캔 회피)
        running, _ = self.process_controller.is_process_running(target, False)
        return running

    def _is_validated_dashboard_identity(self, session: SessionRef | None) -> bool:
        if session is None:
            return False
        return (
            session.validation_status == "verified"
            and session.pgid is not None
            and bool(session.tty)
            and bool(session.identity_generation)
        )

    def _has_null_identity_fields(self, session: SessionRef | None) -> bool:
        """신원 필드(pgid/tty/identity_generation)가 모두 null인 경우 True.

        stale이지만 실제 pid/tty가 존재하는 세션과 구분하기 위해 사용한다.
        null-identity 경로(fallback)는 이 경우에만 활성화된다.
        """
        if session is None:
            return True
        return (
            session.pgid is None
            and not bool(session.tty)
            and not bool(session.identity_generation)
        )

    def _live_verify_dashboard_identity(
        self, target: str, session: SessionRef | None
    ) -> str:
        """캡처/degraded 신원의 실제 프로세스 생존을 확인해 3-way 결과 반환.

        Returns: "verified" | "dead" | "unverifiable"

        - "verified": 세션 pgid 또는 pid+create_time이 실행 중인 프로세스와 일치
        - "dead": 프로세스가 이미 종료됨
        - "unverifiable": 프로세스가 살아 있지만 신원을 확인할 수 없음
        """
        if self._is_validated_dashboard_identity(session):
            return "verified"

        snapshot_pid = getattr(session, "pid", None) if session is not None else None
        if snapshot_pid is not None:
            # snapshot pid가 있으면 O(1) 직접 확인 (전체 process_iter 스캔 skip)
            _proc: psutil.Process | None = None
            try:
                _proc = psutil.Process(snapshot_pid)
                is_alive = _proc.is_running() and _proc.status() != psutil.STATUS_ZOMBIE
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                is_alive = False
            if not is_alive:
                return "dead"
            # 살아 있음 — 해당 pid 프로세스로 신원 확인
            if session is not None and session.pgid is not None:
                try:
                    if os.getpgid(snapshot_pid) == int(session.pgid):
                        return "verified"
                except (ProcessLookupError, OSError):
                    pass
            if session is not None and session.pid_create_time is not None and _proc is not None:
                try:
                    if _proc.create_time() == session.pid_create_time:
                        return "verified"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return "unverifiable"

        # pid 없을 때만 process scan fallback — cache 우선 사용(force_refresh=False)으로 GIL 점유 방지.
        # BotMonitoringThread가 주기적으로 캐시를 갱신하므로 kill 직전 stale 리스크 낮음.
        running, procs = self.process_controller.is_process_running(target, False)
        if not running or not procs:
            return "dead"
        if session is not None and session.pgid is not None:
            for proc, _path in procs:
                try:
                    if os.getpgid(proc.pid) == int(session.pgid):
                        return "verified"
                except (ProcessLookupError, OSError):
                    continue
        if session is not None and session.pid is not None and session.pid_create_time is not None:
            for proc, _path in procs:
                try:
                    if proc.pid == session.pid and proc.create_time() == session.pid_create_time:
                        return "verified"
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return "unverifiable"

    # ------------------------------------------------------------------
    # Fallback terminate helpers (null-identity path)
    # ------------------------------------------------------------------

    def _is_close_ok(self, result: object) -> bool:
        """플랫폼 핸들러의 창 닫기 결과가 성공인지 판단한다."""
        if result is True:
            return True
        if isinstance(result, dict) and result.get("ok") is True:
            return True
        return False

    async def _fallback_close_terminal(
        self,
        target: str,
        snapshot: TerminalCleanupSnapshot | None,
        operation_id: str,
    ) -> tuple[bool, Literal["window_id", "title", "none"]]:
        """신원 없이 터미널을 제목 또는 window_id로 닫는다."""
        ph = getattr(self.bot, "platform_handler", None)
        if ph is None:
            logger.warning("[FALLBACK][CLOSE_TERMINAL][NO_PLATFORM_HANDLER] op_id=%s target=%s", operation_id, target)
            return False, "none"

        window_id = getattr(snapshot, "window_id", None) if snapshot else None
        custom_title = getattr(snapshot, "custom_title", None) if snapshot else None

        if window_id and hasattr(ph, "close_window_by_window_id"):
            try:
                result = await ph.close_window_by_window_id(window_id)
                ok = self._is_close_ok(result)
                logger.info(
                    "[FALLBACK][CLOSE_TERMINAL] op_id=%s target=%s method=window_id ok=%s",
                    operation_id, target, ok,
                )
                return ok, "window_id"
            except Exception as exc:
                logger.warning(
                    "[FALLBACK][CLOSE_TERMINAL][ERROR] op_id=%s target=%s method=window_id reason=%s",
                    operation_id, target, exc,
                )

        title = custom_title or target
        if title and hasattr(ph, "close_window_by_title_tty"):
            try:
                result = await ph.close_window_by_title_tty(title, None)
                ok = self._is_close_ok(result)
                logger.info(
                    "[FALLBACK][CLOSE_TERMINAL] op_id=%s target=%s method=title ok=%s",
                    operation_id, target, ok,
                )
                return ok, "title"
            except Exception as exc:
                logger.warning(
                    "[FALLBACK][CLOSE_TERMINAL][ERROR] op_id=%s target=%s method=title reason=%s",
                    operation_id, target, exc,
                )

        return False, "none"

    async def _fallback_confirm_cleared(
        self,
        target: str,
        *,
        retries: int = 0,
        delay: float = 0.4,
    ) -> int:
        """프로세스가 종료되었는지 재확인. 잔존 개수 반환.

        기본값 retries=0: kill 없이 한 번만 조회한다.
        retries>0이면 아직 살아 있을 때 kill 후 재확인한다.
        """
        pairs: list = []
        for attempt in range(retries + 1):
            is_running, pairs = self.process_controller.is_process_running(
                target, force_refresh=True
            )
            if not is_running:
                return 0
            if attempt < retries:
                await asyncio.to_thread(
                    self.process_controller.kill_specific_process, target
                )
                await asyncio.sleep(delay)
        return len(pairs)

    async def _fallback_terminate_without_identity(
        self,
        target: str,
        snapshot: TerminalCleanupSnapshot | None,
        *,
        operation_id: str,
    ) -> FallbackResult:
        """세션 신원 정보 없이 이름 기반으로 프로세스를 종료하고 터미널을 닫는다.
        macOS 전용. 다른 플랫폼에서는 SKIP을 반환한다."""
        if sys.platform != "darwin":
            logger.info(
                "[FALLBACK][SKIP_NON_MACOS] op_id=%s target=%s platform=%s",
                operation_id, target, sys.platform,
            )
            return FallbackResult(
                process_cleared=False,
                killed_count=0,
                terminal_closed=False,
                survivors=-1,
                method="none",
            )

        # (a) 프로세스 종료 — PROCESS_NAME + dir_name 기반, pid 불필요
        killed_count = await asyncio.to_thread(
            self.process_controller.kill_specific_process, target
        )
        logger.info(
            "[FALLBACK][KILL_DONE] op_id=%s target=%s killed=%s",
            operation_id, target, killed_count,
        )

        # (b) 사망 확인 barrier
        survivors = await self._fallback_confirm_cleared(target)
        process_cleared = survivors == 0

        # (c) 터미널 닫기
        terminal_closed, method = await self._fallback_close_terminal(
            target, snapshot, operation_id
        )

        logger.info(
            "[FALLBACK][RESULT] op_id=%s target=%s killed=%s survivors=%s "
            "cleared=%s terminal_closed=%s method=%s",
            operation_id, target, killed_count, survivors,
            process_cleared, terminal_closed, method,
        )
        return FallbackResult(
            process_cleared=process_cleared,
            killed_count=killed_count,
            terminal_closed=terminal_closed,
            survivors=survivors,
            method=method,
        )

    # ------------------------------------------------------------------

    def _mark_dashboard_identity_manual_recovery(
        self,
        store: SessionStore | None,
        session: SessionRef | None,
        *,
        operation_id: str,
        reason: str,
    ) -> None:
        if store is None or session is None:
            return
        evidence = dict(session.evidence or {})
        evidence["manual_recovery_required"] = {
            "operation_id": operation_id,
            "reason": reason,
            "recorded_at": _utc_now_iso(),
        }
        store.save(
            replace(
                session,
                status="manual_recovery_required",
                operation_id=operation_id,
                validation_status="stale",
                validation_checked_at=_utc_now_iso(),
                validation_reason=reason,
                evidence=evidence,
                updated_at=_utc_now_iso(),
            )
        )

    def _get_bounded_stop_specific_process(self):
        bounded_stop = vars(self.process_controller).get("bounded_stop_specific_process")
        if callable(bounded_stop):
            return bounded_stop
        class_attr = getattr(type(self.process_controller), "bounded_stop_specific_process", None)
        if class_attr is not None:
            bounded_stop = getattr(self.process_controller, "bounded_stop_specific_process", None)
            if callable(bounded_stop):
                return bounded_stop
        return None

    def _classify_external_restart_target(
        self,
        target: str,
        session: SessionRef | None,
    ) -> str:
        """Classify restart safety for an external target without stopping it."""
        has_stopped_evidence = bool(
            session is not None and session.has_terminal_stopped_evidence()
        )
        try:
            is_running, _ = self.process_controller.is_process_running(
                target,
                force_refresh=True,
            )
        except Exception as exc:
            logger.warning(
                "[RESTART][CLASSIFY_AMBIGUOUS] target_id=%s kind=external reason=liveness_error error=%s",
                target,
                exc,
            )
            return "liveness_error"
        if is_running:
            return "live"
        if has_stopped_evidence:
            return "stopped"
        return "ambiguous"

    def _classify_z_flow_restart_target(self, target: str) -> tuple[str, Path | None]:
        """Bridge-only stopped classifier for Z-Flow restart safety."""
        bridge = self._get_z_flow_bridge()
        data_dir = _resolve_z_flow_target_dir(self.monitor, target, bridge)
        if data_dir is None:
            return "missing", None
        try:
            live_processes = _find_all_z_flow_os_processes(self.monitor, data_dir, bridge)
        except Exception as exc:
            logger.warning(
                "[RESTART][CLASSIFY_AMBIGUOUS] target_id=%s kind=z_flow reason=bridge_liveness_error error=%s",
                target,
                exc,
            )
            return "ambiguous", data_dir
        if live_processes:
            return "live", data_dir
        session = SessionStore(data_dir).load()
        if session is not None and session.has_terminal_stopped_evidence():
            return "stopped", data_dir
        return "ambiguous", data_dir

    def _capture_old_terminal_cleanup_snapshot(
        self,
        target: str,
        request_id: str,
    ) -> TerminalCleanupSnapshot | None:
        target_path = self.process_controller.find_target_directory(target)
        if target_path is None or not isinstance(target_path, (str, os.PathLike)):
            logger.info(
                "[CLEANUP][SNAPSHOT_CAPTURE_SKIP] request_id=%s target_id=%s source=%s "
                "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                request_id,
                target,
                "process_action_capture",
                False,
                False,
                False,
                "target_not_found",
                "capture_snapshot",
            )
            return None

        target_path = Path(target_path)
        data_dir = target_path if target_path.is_dir() else target_path.parent
        session = SessionStore(data_dir).load()
        if session is None:
            logger.info(
                "[CLEANUP][SNAPSHOT_CAPTURE_SKIP] request_id=%s target_id=%s source=%s "
                "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                request_id,
                target,
                "process_action_capture",
                False,
                False,
                False,
                "session_not_found",
                "capture_snapshot",
            )
            return None

        has_window_id = bool(session.window_id)
        has_tty = bool(session.tty)
        has_pid = bool(session.pid)
        has_pgid = bool(session.pgid)
        logger.info(
            "[CLEANUP][SNAPSHOT_CAPTURED] request_id=%s target_id=%s source=%s "
            "has_pid=%s has_pgid=%s has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
            request_id,
            target,
            "process_action_capture",
            has_pid,
            has_pgid,
            has_window_id,
            has_tty,
            bool(session.pid or session.pgid or session.window_id or session.tty),
            "captured",
            "capture_snapshot",
        )

        return TerminalCleanupSnapshot(
            target=target,
            session_id=session.session_id,
            status=session.status,
            source=session.source,
            pid=session.pid,
            pgid=session.pgid,
            window_id=session.window_id,
            tty=session.tty,
            custom_title=session.custom_title,
            request_id=request_id,
        )

    def _log_no_identity_cleanup_skip(
        self,
        *,
        prefix: str,
        operation_id: str,
        target: str,
        cleanup_snapshot: TerminalCleanupSnapshot | None,
        generation_id: str | None = None,
        label: str | None = None,
    ) -> None:
        session_id = (
            cleanup_snapshot.session_id
            if cleanup_snapshot is not None and cleanup_snapshot.session_id
            else target
        )
        status = cleanup_snapshot.status if cleanup_snapshot is not None else None
        source = cleanup_snapshot.source if cleanup_snapshot is not None else None
        has_pid = bool(cleanup_snapshot and cleanup_snapshot.pid)
        has_pgid = bool(cleanup_snapshot and cleanup_snapshot.pgid)
        has_tty = bool(cleanup_snapshot and cleanup_snapshot.tty)
        has_window_id = bool(cleanup_snapshot and cleanup_snapshot.window_id)
        has_custom_title = bool(cleanup_snapshot and cleanup_snapshot.custom_title)
        hint = "persist_pid_pgid_tty_window_id_before_stop; custom_title_only_is_not_cleanup_identity"
        if generation_id is not None:
            logger.info(
                "%s operation_id=%s target_id=%s generation_id=%s session_id=%s status=%s source=%s "
                "has_pid=%s has_pgid=%s has_tty=%s has_window_id=%s has_custom_title=%s "
                "critical_path=false cleanup_policy=snapshot reason=no_identity hint=%s",
                prefix,
                operation_id,
                target,
                generation_id,
                session_id,
                status or "unknown",
                source or "unknown",
                has_pid,
                has_pgid,
                has_tty,
                has_window_id,
                has_custom_title,
                hint,
            )
            return
        logger.info(
            "%s operation_id=%s target_id=%s step=%s session_id=%s status=%s source=%s "
            "has_pid=%s has_pgid=%s has_tty=%s has_window_id=%s has_custom_title=%s "
            "critical_path=false cleanup_policy=snapshot reason=no_identity hint=%s",
            prefix,
            operation_id,
            target,
            label or "unknown",
            session_id,
            status or "unknown",
            source or "unknown",
            has_pid,
            has_pgid,
            has_tty,
            has_window_id,
            has_custom_title,
            hint,
        )

    def _schedule_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        operation_id: str,
        target: str,
        label: str,
    ) -> None:
        task = asyncio.create_task(coro)

        def _log_result(done: asyncio.Task[Any]) -> None:
            try:
                done.result()
            except Exception as exc:
                logger.warning(
                    "[PROC][BACKGROUND][ERROR] operation_id=%s target_id=%s step=%s error=%s",
                    operation_id,
                    target,
                    label,
                    exc,
                )

        task.add_done_callback(_log_result)

    async def _background_restart_cleanup(
        self,
        query: CallbackQuery,
        target: str,
        operation_id: str,
        generation_id: str,
        cleanup_snapshot: TerminalCleanupSnapshot | None = None,
    ) -> None:
        if not self._is_latest_generation(target, generation_id):
            logger.info(
                "[RESTART][BACKGROUND_SKIP] operation_id=%s target_id=%s generation_id=%s reason=stale",
                operation_id,
                target,
                generation_id,
            )
            return

        if cleanup_snapshot is None or not cleanup_snapshot.has_identity:
            self._log_no_identity_cleanup_skip(
                prefix="[RESTART][BACKGROUND_CLEANUP_SKIP]",
                operation_id=operation_id,
                target=target,
                generation_id=generation_id,
                cleanup_snapshot=cleanup_snapshot,
            )
        elif hasattr(self.bot, "_cleanup_terminal_snapshot_for_dashboard"):
            started = time.monotonic()
            await self.bot._cleanup_terminal_snapshot_for_dashboard(cleanup_snapshot)
            target_path = self.process_controller.find_target_directory(target)
            if target_path is not None and isinstance(target_path, (str, os.PathLike)):
                target_path = Path(target_path)
                data_dir = target_path if target_path.is_dir() else target_path.parent
                SessionStore(data_dir).clear_runtime_identity()
            logger.info(
                "[RESTART][BACKGROUND_CLEANUP_DONE] operation_id=%s target_id=%s generation_id=%s "
                "critical_path=false cleanup_policy=snapshot elapsed_ms=%d",
                operation_id,
                target,
                generation_id,
                int((time.monotonic() - started) * 1000),
            )

        if not self._is_latest_generation(target, generation_id):
            return
        started = time.monotonic()
        await self.dashboard_handler.update_dashboard(query, force_rescan=True)
        logger.info(
            "[RESTART][BACKGROUND_DASHBOARD_DONE] operation_id=%s target_id=%s generation_id=%s "
            "critical_path=false refresh_type=dashboard_force elapsed_ms=%d",
            operation_id,
            target,
            generation_id,
            int((time.monotonic() - started) * 1000),
        )

    async def _background_cleanup_dashboard(
        self,
        query: CallbackQuery,
        target: str,
        operation_id: str,
        *,
        label: str,
        cleanup_snapshot: TerminalCleanupSnapshot | None = None,
    ) -> None:
        if cleanup_snapshot is None or not cleanup_snapshot.has_identity:
            self._log_no_identity_cleanup_skip(
                prefix="[PROC][BACKGROUND_CLEANUP_SKIP]",
                operation_id=operation_id,
                target=target,
                label=label,
                cleanup_snapshot=cleanup_snapshot,
            )
        elif hasattr(self.bot, "_cleanup_terminal_snapshot_for_dashboard"):
            started = time.monotonic()
            try:
                await self.bot._cleanup_terminal_snapshot_for_dashboard(cleanup_snapshot)
            except Exception as cleanup_error:
                logger.warning(
                    "[PROC][BACKGROUND_CLEANUP_FAILED] operation_id=%s target_id=%s step=%s "
                    "critical_path=false error=%s",
                    operation_id,
                    target,
                    label,
                    cleanup_error,
                    exc_info=True,
                )
            else:
                logger.info(
                    "[PROC][BACKGROUND_CLEANUP_DONE] operation_id=%s target_id=%s step=%s "
                    "critical_path=false cleanup_policy=snapshot elapsed_ms=%d",
                    operation_id,
                    target,
                    label,
                    int((time.monotonic() - started) * 1000),
                )

        if hasattr(self.bot, "window_manager"):
            try:
                self.bot.window_manager.trigger_auto_arrange()
            except Exception as arrange_error:
                logger.warning(
                    "[PROC][BACKGROUND_ARRANGE_FAILED] operation_id=%s target_id=%s step=%s "
                    "critical_path=false error=%s",
                    operation_id,
                    target,
                    label,
                    arrange_error,
                    exc_info=True,
                )

        started = time.monotonic()
        try:
            await self._wait_for_state_change(
                target,
                expect_running=False,
                timeout=3,
                known_pid=cleanup_snapshot.pid if cleanup_snapshot is not None else None,
            )
        except Exception as state_error:
            logger.warning(
                "[PROC][BACKGROUND_STATE_WAIT_FAILED] operation_id=%s target_id=%s step=%s "
                "critical_path=false error=%s",
                operation_id,
                target,
                label,
                state_error,
                exc_info=True,
            )
        else:
            logger.info(
                "[PROC][BACKGROUND_STATE_WAIT_DONE] operation_id=%s target_id=%s step=%s "
                "critical_path=false expect_running=false elapsed_ms=%d",
                operation_id,
                target,
                label,
                int((time.monotonic() - started) * 1000),
            )

        started = time.monotonic()
        try:
            await self.dashboard_handler.update_dashboard(query, force_rescan=True)
        except Exception as dashboard_error:
            logger.warning(
                "[PROC][BACKGROUND_DASHBOARD_FAILED] operation_id=%s target_id=%s step=%s "
                "critical_path=false error=%s",
                operation_id,
                target,
                label,
                dashboard_error,
                exc_info=True,
            )
        else:
            logger.info(
                "[PROC][BACKGROUND_DASHBOARD_DONE] operation_id=%s target_id=%s step=%s "
                "critical_path=false refresh_type=dashboard_force elapsed_ms=%d",
                operation_id,
                target,
                label,
                int((time.monotonic() - started) * 1000),
            )

    async def _wait_for_state_change(
        self,
        target: str,
        expect_running: bool,
        timeout: float = 5,
        poll_interval: float = 0.25,
        known_pid: int | None = None,
    ):
        """
        프로세스 상태가 기대하는 상태로 변할 때까지 대기 (Polling)

        Args:
            target: 디렉토리 명
            expect_running: True면 켜질 때까지, False면 꺼질 때까지 대기
            timeout: 최대 대기 시간(초)
            poll_interval: 확인 간격(초). 호출 직후 1회 즉시 확인합니다.
            known_pid: kill path 전용. 지정 시 psutil.pid_exists(known_pid) O(1) 로
                       전체 psutil 스캔을 회피한다. GIL 점유 11-25s → ~0ms.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        interval = max(0.05, float(poll_interval))
        iteration = 0
        while True:
            # known_pid kill path: O(1) pid_exists 로 전체 스캔 회피
            if not expect_running and known_pid is not None:
                is_running = await asyncio.to_thread(psutil.pid_exists, known_pid)
                iteration += 1
                if is_running == expect_running:
                    return True
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
                continue

            # start-path: 첫 폴링만 force_refresh=True (fresh snapshot 시드), 이후 캐시 재활용.
            # kill-path: 항상 force_refresh=True — kill 직후 iteration=0에서 캐시가 'alive'로
            # 시드되면 이후 폴링이 OS 확인 없이 timeout까지 대기하는 버그를 방지한다.
            force_refresh = (iteration == 0) or (not expect_running)
            is_running = False
            if _is_z_flow_target(target, self.monitor, self._get_z_flow_bridge()):
                _monitor_ref = self.monitor
                _bridge_ref = self._get_z_flow_bridge()
                _z_procs = await asyncio.to_thread(
                    _find_z_flow_processes, _monitor_ref, _bridge_ref
                )
                for proc, dir_name, _ in _z_procs:
                    if dir_name == target and proc is not None:
                        is_running = True
                        break
            else:
                checker = getattr(self.process_controller, "is_process_running", None)
                if callable(checker):
                    running_checker = cast(ProcessRunningChecker, checker)
                    started = time.monotonic()
                    is_running, _ = await asyncio.to_thread(
                        running_checker, target, force_refresh=force_refresh
                    )
                    elapsed = time.monotonic() - started
                    if elapsed > 1.0:
                        logger.warning(
                            "[PROC][STATE_CHECK_SLOW] target=%s elapsed=%.2fs",
                            target,
                            elapsed,
                        )
                else:
                    _procs = await asyncio.to_thread(
                        self.monitor.find_processes, force_refresh
                    )
                    for proc, path in _procs:
                        if path.parent.name == target:
                            is_running = True
                            break
            iteration += 1

            if is_running == expect_running:
                return True  # 상태 변경 확인됨

            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))

        return False  # 타임아웃

    async def kill_process(self, query: CallbackQuery, target: str):
        """프로세스 종료"""
        if target in self._in_flight:
            await self._safe_answer_query(query, f"⏳ {target} 이미 처리 중...")
            return
        self._in_flight.add(target)
        try:
            await self._kill_process_impl(query, target)
        finally:
            self._in_flight.discard(target)

    async def _kill_process_impl(self, query: "CallbackQuery", target: str) -> None:
        """kill_process 실제 구현 (in-flight lock 내부에서 호출)."""
        trace = OperationContext("kill", target=target)
        op_id = f"kill-{int(time.time() * 1000)}-{target}"
        started_at = time.monotonic()
        logger.info(
            "[KILL_UI][START] op_id=%s target=%s query_id=%s message_id=%s",
            op_id,
            target,
            getattr(query, "id", None),
            getattr(getattr(query, "message", None), "message_id", None),
        )
        try:
            cleanup_snapshot = self._capture_old_terminal_cleanup_snapshot(target, op_id)
            # dedup 마커를 kill 착수 시점에 세팅: 모니터 tick보다 먼저 존재해야 cleanup 이중호출 차단
            cleanup_marker = getattr(getattr(self, "monitor", None), "mark_cleanup_handled", None)
            if callable(cleanup_marker):
                cleanup_marker(target)
            # Z-Flow 런타임 처리 분기
            if await asyncio.to_thread(
                _is_z_flow_target, target, self.monitor, self._get_z_flow_bridge()
            ):
                slot_data_dir: Path | None = None
                for _, dir_name, data_dir in _find_z_flow_processes(
                    self.monitor, self._get_z_flow_bridge()
                ):
                    if dir_name == target:
                        slot_data_dir = data_dir
                        break
                if slot_data_dir is None:
                    slot_data_dir = _resolve_z_flow_target_dir(
                        self.monitor, target, self._get_z_flow_bridge()
                    )

                # OS 프로세스 스캔으로 모든 인스턴스 종료 (고아/중복 포함)
                killed_count = await asyncio.to_thread(
                    _kill_all_z_flow_processes,
                    self.monitor,
                    slot_data_dir or target,
                    1.0,
                    self._get_z_flow_bridge(),
                )
                if killed_count == 0:
                    logger.info(
                        "[KILL_UI][NO_PROCESS] op_id=%s target=%s kind=z_flow",
                        op_id,
                        target,
                    )
                    await self._safe_answer_query(
                        query,
                        "❌ 실행 중인 Z-Flow 프로세스를 찾을 수 없습니다."
                    )
                    await self.dashboard_handler.update_dashboard(
                        query, force_rescan=True
                    )
                    return
                if slot_data_dir is not None:
                    write_bot_state(slot_data_dir, "MANUAL_STOP")
                    _persist_manual_stop_session(
                        slot_data_dir,
                        target,
                        "z_flow_runtime",
                        "manual-stop",
                    )
                    self.bot.z_flow_bridge.cleanup_runtime_artifacts(slot_data_dir)
            else:
                identity_store, identity_session = await asyncio.to_thread(
                    self._load_dashboard_session_identity, target
                )
                if identity_session is not None and not self._is_validated_dashboard_identity(identity_session):
                    if self._has_null_identity_fields(identity_session):
                        # 신원 필드가 전부 null → name-based fallback 경로
                        logger.warning(
                            "[KILL_UI][IDENTITY_REFUSED] op_id=%s target=%s reason=%s entering_fallback",
                            op_id, target, "identity_not_verified",
                        )
                        fb = await self._fallback_terminate_without_identity(
                            target, cleanup_snapshot, operation_id=op_id
                        )
                        if fb.process_cleared:
                            target_path = self.process_controller.find_target_directory(target)
                            if fb.killed_count and isinstance(target_path, (str, Path)):
                                try:
                                    _persist_manual_stop_session(
                                        Path(target_path).parent,
                                        target,
                                        "external_bot",
                                        "manual-stop",
                                    )
                                except OSError as persist_error:
                                    logger.warning(
                                        "[KILL_UI][FALLBACK_MANUAL_STOP_SESSION_SKIP] op_id=%s target=%s error=%s",
                                        op_id, target, persist_error,
                                    )
                            await self._safe_answer_query(
                                query,
                                f"🛑 {target} 종료 완료 (fallback, killed={fb.killed_count})",
                            )
                            await self.dashboard_handler.update_dashboard(query, force_rescan=True)
                        else:
                            self._mark_dashboard_identity_manual_recovery(
                                identity_store,
                                identity_session,
                                operation_id=op_id,
                                reason="dashboard_stop_fallback_survivors",
                            )
                            await self._safe_answer_query(
                                query,
                                f"⚠️ {target} 일부 프로세스가 남아 있어 종료를 완료하지 못했습니다.",
                            )
                        return
                    # 신원 필드는 있지만 검증 실패 → live 3-way 검증
                    live_result = await asyncio.to_thread(self._live_verify_dashboard_identity, target, identity_session)
                    if live_result == "verified":
                        pass  # 아래 bounded_stop으로 fall through
                    elif live_result == "dead":
                        # T4: 프로세스가 이미 없음
                        # captured(미검증) 신원은 안전하게 null 처리 후 이미 종료 보고
                        # stale 등 이미 검증됐다가 만료된 신원은 수동 복구 경로로 보존
                        if (
                            identity_session is not None
                            and identity_session.validation_status == "captured"
                        ):
                            if identity_store is not None:
                                identity_store.clear_runtime_identity()
                            logger.info(
                                "[KILL_UI][ALREADY_DEAD] op_id=%s target=%s",
                                op_id,
                                target,
                            )
                            await self._safe_answer_query(
                                query,
                                f"[{target}] 프로세스가 이미 종료되어 있습니다. 세션 정보를 초기화했습니다.",
                            )
                            return
                        else:
                            self._mark_dashboard_identity_manual_recovery(
                                identity_store,
                                identity_session,
                                operation_id=op_id,
                                reason="dashboard_stop_identity_not_verified",
                            )
                            logger.warning(
                                "[KILL_UI][ALREADY_DEAD] op_id=%s target=%s reason=dashboard_stop_identity_not_verified",
                                op_id,
                                target,
                            )
                            await self._safe_answer_query(
                                query,
                                "⚠️ 저장된 세션 신원이 확인되지 않아 자동 종료를 거부했습니다. 수동 복구가 필요합니다.",
                                show_alert=True,
                            )
                            return
                    else:  # "unverifiable"
                        self._mark_dashboard_identity_manual_recovery(
                            identity_store,
                            identity_session,
                            operation_id=op_id,
                            reason="dashboard_stop_identity_not_verified",
                        )
                        logger.warning(
                            "[KILL_UI][IDENTITY_REFUSED] op_id=%s target=%s reason=dashboard_stop_identity_not_verified",
                            op_id,
                            target,
                        )
                        await self._safe_answer_query(
                            query,
                            "⚠️ 저장된 세션 신원이 확인되지 않아 자동 종료를 거부했습니다. 수동 복구가 필요합니다.",
                            show_alert=True,
                        )
                        return

                bounded_stop = self._get_bounded_stop_specific_process()
                still_alive_pids = ()
                if callable(bounded_stop):
                    stop_kwargs: dict[str, object] = {
                        "terminate_timeout_seconds": 1.0,
                        "kill_timeout_seconds": 1.0,
                        "force_refresh": False,
                        "operation_id": op_id,
                    }
                    if identity_session is not None:
                        stop_kwargs["session_identity"] = identity_session
                    stop_result = await asyncio.to_thread(
                        bounded_stop,
                        target,
                        **stop_kwargs,
                    )
                    killed_count = getattr(stop_result, "stopped_count", 0)
                    still_alive_pids = getattr(stop_result, "still_alive_pids", ())
                    logger.info(
                        "[KILL_UI][BOUNDED_STOP_DONE] op_id=%s target=%s killed=%s "
                        "still_alive_pids=%s elapsed_ms=%s identity_generation=%s pgid=%s tty=%s",
                        op_id,
                        target,
                        killed_count,
                        list(still_alive_pids),
                        getattr(stop_result, "elapsed_ms", None),
                        getattr(identity_session, "identity_generation", None),
                        getattr(identity_session, "pgid", None),
                        getattr(identity_session, "tty", None),
                    )
                    trace.checkpoint("stop_attempt_done")
                else:
                    check_started_at = time.monotonic()
                    _, target_pairs = await asyncio.to_thread(
                        self.process_controller.is_process_running,
                        target,
                        True,
                    )
                    logger.info(
                        "[KILL_UI][CHECK_DONE] op_id=%s target=%s pairs=%d elapsed=%.2fs",
                        op_id,
                        target,
                        len(target_pairs),
                        time.monotonic() - check_started_at,
                    )

                    if not target_pairs:
                        logger.info(
                            "[KILL_UI][NO_PROCESS] op_id=%s target=%s kind=external",
                            op_id,
                            target,
                        )
                        await self._safe_answer_query(
                            query, "❌ 실행 중인 프로세스를 찾을 수 없습니다."
                        )
                        # 대시보드 갱신
                        await self.dashboard_handler.update_dashboard(
                            query, force_rescan=True
                        )
                        return

                    killed_count = await asyncio.to_thread(
                        self.process_controller.kill_specific_process, target
                    )
                    trace.checkpoint("stop_attempt_done")
                if still_alive_pids:
                    await self._safe_answer_query(
                        query,
                        f"⚠️ {target} 프로세스가 아직 살아 있어 종료 완료로 처리하지 않았습니다.",
                    )
                    return
                target_path = self.process_controller.find_target_directory(target)
                if killed_count and isinstance(target_path, (str, Path)):
                    try:
                        _persist_manual_stop_session(
                            Path(target_path).parent,
                            target,
                            "external_bot",
                            "manual-stop",
                        )
                    except OSError as persist_error:
                        logger.warning(
                            "[KILL_UI][MANUAL_STOP_SESSION_SKIP] op_id=%s target=%s error=%s",
                            op_id,
                            target,
                            persist_error,
                        )
                logger.info(
                    "[KILL_UI][KILL_DONE] op_id=%s target=%s killed=%s elapsed=%.2fs",
                    op_id,
                    target,
                    killed_count,
                    time.monotonic() - started_at,
                )

            if killed_count == 0:
                logger.info(
                    "[KILL_UI][FAIL_ZERO] op_id=%s target=%s elapsed=%.2fs",
                    op_id,
                    target,
                    time.monotonic() - started_at,
                )
                await self._safe_answer_query(query, "❌ 프로세스 종료 실패")
                return

            # KILL_DONE: kill 성공 시에만 dedup 마커 재세팅 (TTL 30s — monitor check_interval 10s × 3)
            _cleanup_marker = getattr(getattr(self, "monitor", None), "mark_cleanup_handled", None)
            if callable(_cleanup_marker):
                _cleanup_marker(target, ttl_seconds=30.0)

            target_path_for_msg = self.process_controller.find_target_directory(target)
            is_managed = self._is_automation_on(target)
            if _is_z_flow_target(target, self.monitor, self._get_z_flow_bridge()) and is_managed:
                await self._safe_answer_query(
                    query, f"🛑 {target} 슬롯 수동 정지됨 → ON_MANUAL_STOP"
                )
            elif _is_external_pair_trading_bot(target_path_for_msg) and is_managed:
                await self._safe_answer_query(
                    query, f"🛑 {target} 수동 정지됨 → ON_MANUAL_STOP"
                )
            elif _is_external_pair_trading_bot(target_path_for_msg) or _is_z_flow_target(
                target, self.monitor, self._get_z_flow_bridge()
            ):
                await self._safe_answer_query(query, f"🛑 {target} 종료 완료")
            else:
                await self._safe_answer_query(query, f"🛑 {target} 종료 신호 전송 완료")
            trace.checkpoint("callback_answer_done")
            logger.info(
                "[KILL_UI][ANSWER_SENT] op_id=%s target=%s elapsed=%.2fs",
                op_id,
                target,
                time.monotonic() - started_at,
            )

            self._schedule_background_task(
                self._background_cleanup_dashboard(
                    query,
                    target,
                    op_id,
                    label="kill_process_cleanup_dashboard",
                    cleanup_snapshot=cleanup_snapshot,
                ),
                operation_id=op_id,
                target=target,
                label="kill_process_cleanup_dashboard",
            )
            trace.checkpoint("cleanup_dashboard_scheduled")
            trace.log_summary()

        except Exception as e:
            logger.exception(
                "[KILL_UI][ERROR] op_id=%s target=%s elapsed=%.2fs error=%s",
                op_id,
                target,
                time.monotonic() - started_at,
                e,
            )
            await self._safe_edit_query_message(
                query,
                f"❌ 프로세스 종료 중 오류가 발생했습니다:\n{str(e)}",
            )

    async def start_process(
        self,
        query: CallbackQuery,
        target: str,
        *,
        callback_pre_acked: bool = False,
    ) -> None:
        """프로세스 시작"""
        if target in self._in_flight:
            await self._safe_answer_query(query, f"⏳ {target} 이미 처리 중...")
            return
        self._in_flight.add(target)
        try:
            await self._start_process_impl(
                query, target, callback_pre_acked=callback_pre_acked
            )
        finally:
            self._in_flight.discard(target)

    async def _start_process_impl(
        self,
        query: Optional[CallbackQuery],
        target: str,
        *,
        callback_pre_acked: bool = False,
    ) -> None:
        """start_process 실제 구현 (in-flight lock 내부에서 호출).

        query=None 허용: 백그라운드 자동 재시작 경로(_simple_restart_core)에서
        UI 알림 없이 호출 가능.
        """
        trace = OperationContext("start", target=target)
        try:
            # Z-Flow 런타임 처리 분기
            slot_data_dir = await asyncio.to_thread(
                _resolve_z_flow_target_dir, self.monitor, target, self._get_z_flow_bridge()
            )
            if slot_data_dir is not None:
                data_dir = slot_data_dir
                if not data_dir.exists():
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            "❌ Z-Flow 디렉토리를 찾을 수 없습니다.",
                            callback_pre_acked=callback_pre_acked,
                        )
                    return

                # 고아/중복 프로세스 선제 정리 (시작 전 sweep)
                orphan_count = await asyncio.to_thread(
                    _kill_all_z_flow_processes,
                    self.monitor,
                    data_dir,
                    1.0,
                    self._get_z_flow_bridge(),
                )
                if orphan_count > 0:
                    logger.info(
                        f"[SLOT][START] {target}: 시작 전 {orphan_count}개 잔존 프로세스 정리됨"
                    )

                clear_bot_state(data_dir)
                self.bot.z_flow_bridge.cleanup_runtime_artifacts(data_dir)
                metadata = self.bot.z_flow_bridge.get_slot_runtime_metadata(data_dir)
                if metadata is None:
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            "❌ setting.env를 읽을 수 없습니다.",
                            callback_pre_acked=callback_pre_acked,
                        )
                    return
                slot_id = int(metadata["slot_id"])
                exchange_id = str(metadata["exchange_id"])
                data_dir = Path(metadata["data_dir"])

                try:
                    self.bot.z_flow_bridge.reset_locked_slot_state(slot_id=slot_id)
                except Exception as _e:
                    logger.warning(
                        f"[SLOT][START] LOCKED state 리셋 실패 (무시): {_e}"
                    )

                identity_generation = _new_identity_generation()
                session_store = SessionStore(data_dir)
                session_store.begin_runtime_identity(
                    SessionRef(
                        session_id=target,
                        dir_name=target,
                        runtime_kind="z_flow_runtime",
                        platform="macos" if sys.platform == "darwin" else sys.platform,
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

                launch_spec = self.bot.z_flow_bridge.get_runtime_launch_spec(
                    slot_id=slot_id,
                    data_dir=data_dir,
                    exchange_id=exchange_id,
                )
                cmd = launch_spec["cmd"]
                launch_cwd = str(launch_spec["cwd"])
                arrange_scheduled = False
                # Try to start Z-Flow in separate Terminal window (macOS)
                try:
                    quoted_args = [shlex.quote(str(arg)) for arg in cmd]
                    run_cmd = " ".join(quoted_args)
                    applescript = f'''
                    tell application "Terminal"
                        activate
                        try
                            set newWindow to do script "cd '{shlex.quote(launch_cwd)}'"
                            delay 0.2
                            do script "echo '🚀 {target} Z-Flow 시작 중...'" in newWindow
                            delay 0.2
                            do script "{run_cmd}" in newWindow

                            set bounds of front window to {{100, 100, 800, 600}}
                            set visible of front window to true

                            set custom title of newWindow to "{target}"
                            return (id of front window) as string
                        on error errMsg
                            log "Error starting Z-Flow: " & errMsg
                            return ""
                        end try
                    end tell
                    '''
                    platform = self.process_controller.platform_handler
                    returncode, stdout, stderr = await platform.run_shell_command(
                        applescript, is_applescript=True
                    )
                    window_id: str | None = None
                    if returncode == 0 and stdout.strip():
                        window_id = stdout.strip()
                        logger.info(
                            f"[SLOT][START] {target}: window_id={window_id} 수집 완료"
                        )
                    else:
                        logger.warning(
                            f"[SLOT][START] {target}: window_id 수집 실패 "
                            f"(rc={returncode}, stderr={stderr[:200] if stderr else ''})"
                        )

                    # window_id를 즉시 session.json에 기록 (tty는 background에서 수집)
                    if window_id:
                        now = _utc_now_iso()
                        session_store.update_runtime_identity(
                            identity_generation=identity_generation,
                            tty=None,
                            window_id=window_id,
                            window_captured_at=now,
                            validation_status="starting",
                            validation_checked_at=now,
                            validation_reason="window_tty_captured",
                        )
                        arrange_marker = getattr(
                            self.monitor,
                            "mark_start_arrange_handled",
                            None,
                        )
                        if callable(arrange_marker):
                            arrange_marker(target)
                        if hasattr(self.bot, "window_manager"):
                            self.bot.window_manager.trigger_auto_arrange()
                            arrange_scheduled = True

                        # tty 수집: background task에서 3회 재시도 (임계 경로 밖)
                        tty_query = f'''
                        tell application "Terminal"
                            repeat with w in windows
                                if (id of w) is {window_id} then
                                    return tty of tab 1 of w
                                end if
                            end repeat
                            return ""
                        end tell
                        '''

                        async def _collect_tty_background(
                            gen: str,
                            store: SessionStore,
                            plat: Any,
                            query_script: str,
                            tgt: str,
                        ) -> None:
                            for attempt in range(3):
                                rc, tty_out, _ = await plat.run_shell_command(
                                    query_script, is_applescript=True
                                )
                                if rc == 0 and tty_out.strip():
                                    tty_val = tty_out.strip()
                                    # generation-advance abort 가드
                                    try:
                                        store.update_runtime_identity(
                                            identity_generation=gen,
                                            tty=tty_val,
                                        )
                                    except IdentityGenerationMismatchError:
                                        logger.info(
                                            "[SLOT][START] %s: tty background: generation changed, skip",
                                            tgt,
                                        )
                                        return
                                    logger.info(
                                        "[SLOT][START] %s: tty=%s background 수집 완료 (attempt=%d)",
                                        tgt,
                                        tty_val,
                                        attempt,
                                    )
                                    return
                                await asyncio.sleep(0.3)
                            logger.warning(
                                "[SLOT][START] %s: tty background 수집 실패 (3회 재시도 후)",
                                tgt,
                            )

                        self._schedule_background_task(
                            _collect_tty_background(
                                identity_generation,
                                session_store,
                                platform,
                                tty_query,
                                target,
                            ),
                            operation_id=identity_generation,
                            target=target,
                            label="tty_background_collect",
                        )
                except Exception as e:
                    logger.warning(
                        f"Failed to start Z-Flow via Terminal: {e}, falling back to background process"
                    )
                    subprocess.Popen(
                        cmd,
                        cwd=launch_cwd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        start_new_session=True,
                    )
                is_managed = self._is_automation_on(target)
                if is_managed:
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            f"🟢🔄 {target} 슬롯 런타임 재기동됨 → ON_RUNNING",
                            callback_pre_acked=callback_pre_acked,
                        )
                else:
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            f"🚀 {target} 현재 설정으로 시작됨",
                            callback_pre_acked=callback_pre_acked,
                        )
                is_started = await self._wait_for_state_change(
                    target, expect_running=True, timeout=5
                )
                if is_started:
                    for proc, dir_name, resolved_data_dir in _find_z_flow_processes(
                        self.monitor, self._get_z_flow_bridge()
                    ):
                        if dir_name != target or proc is None:
                            continue
                        try:
                            pgid = os.getpgid(proc.pid)
                        except OSError:
                            pgid = proc.pid
                        now = _utc_now_iso()
                        SessionStore(resolved_data_dir).update_runtime_identity(
                            identity_generation=identity_generation,
                            status="running",
                            source="process-monitor",
                            pid=proc.pid,
                            pgid=pgid,
                            pid_create_time=_process_create_time(proc.pid),
                            identity_captured_at=now,
                            validation_status="captured",
                            validation_checked_at=now,
                            validation_reason="runtime_process_captured",
                            last_seen_at=now,
                            last_running_at=now,
                            evidence={"pid_alive": True},
                        )
                        break
                    if not arrange_scheduled and hasattr(self.bot, "window_manager"):
                        self.bot.window_manager.trigger_auto_arrange()
                    await self.dashboard_handler.update_dashboard(
                        query, force_rescan=True
                    )
                else:
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            "⚠️ 구동 확인 지연 (새로고침 필요)",
                            callback_pre_acked=callback_pre_acked,
                        )
                    await self.dashboard_handler.update_dashboard(
                        query, force_rescan=True
                    )
                return

            # 이미 실행 중인지 확인 — session.json pid 기반 O(1) 검증으로
            # 전체 psutil 스캔(force_refresh=True)을 회피한다.
            already_running = await asyncio.to_thread(
                self._liveness_check_o1,
                target,
            )
            trace.checkpoint("pre_start_liveness_check_done")
            if already_running:
                if query is not None:
                    await self._notify_start_status(
                        query,
                        "⚠️ 프로세스가 이미 실행 중입니다.",
                        callback_pre_acked=callback_pre_acked,
                    )
                return

            # 공통 실행 로직 호출
            if await self.process_controller.start_bot_process(target):
                trace.checkpoint("spawn_command_done")
                target_path_for_msg = self.process_controller.find_target_directory(
                    target
                )
                is_managed = self._is_automation_on(target)
                if _is_external_pair_trading_bot(target_path_for_msg) and is_managed:
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            f"🟢🔄 {target} 유지보수 후 재기동됨 → ON_RUNNING",
                            callback_pre_acked=callback_pre_acked,
                        )
                elif _is_external_pair_trading_bot(target_path_for_msg):
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            f"🚀 {target} 현재 설정으로 시작됨",
                            callback_pre_acked=callback_pre_acked,
                        )
                else:
                    if query is not None:
                        await self._notify_start_status(
                            query,
                            f"🚀 {target} 시작 명령 전송됨. 구동 대기 중...",
                            callback_pre_acked=callback_pre_acked,
                        )
                trace.checkpoint("callback_answer_done")

                # start_bot_process 내부에서 이미 실제 프로세스 출현을 확인한다.
                await self.dashboard_handler.update_dashboard(
                    query, force_rescan=True
                )
                trace.checkpoint("dashboard_update_done")
            else:
                if query is not None:
                    await self._notify_start_status(
                        query,
                        "❌ 실행 파일을 찾을 수 없거나 실행 실패",
                        callback_pre_acked=callback_pre_acked,
                    )

        except Exception as e:
            logger.error(f"시작 오류: {e}")
            if query is not None:
                await self._safe_edit_query_message(query, f"❌ 오류: {e}")
        finally:
            trace.log_summary()

    async def clean_run(self, query: CallbackQuery, target: str):
        """초기화 후 시작 (_DB.json 삭제)"""
        message_source = None
        started_at = time.monotonic()
        logger.info("[CLEAN_RUN][START] target=%s", target)
        try:
            message_source = query.message if query else None
            if not message_source:
                logger.warning("[CLEAN_RUN][SKIP] target=%s reason=no_message", target)
                return

            await self._queue_reply_via_message(
                message_source,
                f"✨ '{target}'의 DB 데이터를 초기화하고 재시작합니다...",
            )
            logger.info(
                "[CLEAN_RUN][NOTICE_QUEUED] target=%s elapsed=%.2fs",
                target,
                time.monotonic() - started_at,
            )

            if _is_z_flow_target(target, self.monitor, self._get_z_flow_bridge()):
                cleanup_snapshot = self._capture_old_terminal_cleanup_snapshot(
                    target,
                    f"clean-run-{int(time.time() * 1000)}-{target}",
                )
                data_dir = await self._stop_z_flow_target(
                    target,
                    cleanup_snapshot=cleanup_snapshot,
                )
                if data_dir is None:
                    await get_telegram_gateway().answer_callback_query(
                        query, "❌ Z-Flow 디렉토리를 찾을 수 없습니다."
                    )
                    return

                deleted_files = self._delete_slot_runtime_db(data_dir)
                logger.info(
                    "[CLEAN_RUN][DB_DELETE] target=%s kind=z_flow files=%d elapsed=%.2fs",
                    target,
                    len(deleted_files),
                    time.monotonic() - started_at,
                )
                if deleted_files:
                    await self._queue_reply_via_message(
                        message_source, f"🗑️ 삭제된 파일: {', '.join(deleted_files)}"
                    )
                else:
                    await self._queue_reply_via_message(
                        message_source, "🤷‍♀️ 삭제할 포지션 DB 파일이 없습니다."
                    )

                await self.start_process(query, target)
                self._schedule_background_task(
                    self._background_cleanup_dashboard(
                        query,
                        target,
                        f"clean-run-z-flow-{int(time.time() * 1000)}",
                        label="clean_run_z_flow_cleanup_dashboard",
                        cleanup_snapshot=cleanup_snapshot,
                    ),
                    operation_id=f"clean-run-z-flow-{int(time.time() * 1000)}",
                    target=target,
                    label="clean_run_z_flow_cleanup_dashboard",
                )
                logger.info(
                    "[CLEAN_RUN][DONE] target=%s kind=z_flow elapsed=%.2fs",
                    target,
                    time.monotonic() - started_at,
                )
                return

            # 1. 프로세스 종료: simple_restart와 같은 bounded fast path를 사용해
            # callback critical path에서 긴 순차 wait/중복 상태 확인을 피한다.
            operation_id = f"clean-run-{int(time.time() * 1000)}-{target}"
            cleanup_snapshot = self._capture_old_terminal_cleanup_snapshot(target, operation_id)
            bounded_stop = self._get_bounded_stop_specific_process()
            if callable(bounded_stop):
                stop_result = await asyncio.to_thread(
                    bounded_stop,
                    target,
                    terminate_timeout_seconds=1.0,
                    kill_timeout_seconds=1.0,
                    force_refresh=False,
                    operation_id=operation_id,
                )
                killed_count = getattr(stop_result, "stopped_count", 0)
                still_alive_pids = getattr(stop_result, "still_alive_pids", ())
            else:
                killed_count = await asyncio.to_thread(
                    self.process_controller.kill_specific_process, target
                )
                still_alive_pids = ()
            logger.info(
                "[CLEAN_RUN][KILL_DONE] target=%s killed=%s still_alive_pids=%s elapsed=%.2fs",
                target,
                killed_count,
                list(still_alive_pids),
                time.monotonic() - started_at,
            )
            if still_alive_pids:
                await self._safe_edit_query_message(
                    query,
                    f"⚠️ {target} 이전 프로세스가 아직 살아 있어 DB 초기화/재시작을 보류했습니다.",
                )
                return

            # 2. JSON 파일 삭제
            target_path = self.process_controller.find_target_directory(target)
            if target_path:
                deleted_files = self.file_operations.delete_files_in_dir(
                    target_path.parent, "_DB.json"
                )
                logger.info(
                    "[CLEAN_RUN][DB_DELETE] target=%s kind=external files=%d elapsed=%.2fs",
                    target,
                    len(deleted_files),
                    time.monotonic() - started_at,
                )

                if deleted_files:
                    await self._queue_reply_via_message(
                        message_source, f"🗑️ 삭제된 파일: {', '.join(deleted_files)}"
                    )
                else:
                    await self._queue_reply_via_message(
                        message_source, "🤷‍♀️ 삭제할 DB 파일이 없습니다."
                    )
            else:
                logger.warning(
                    "[CLEAN_RUN][DB_DELETE_SKIP] target=%s reason=target_not_found elapsed=%.2fs",
                    target,
                    time.monotonic() - started_at,
                )

            # 3. 프로세스 시작. start_bot_process 내부에서 출현 확인을 수행하므로
            # clean_run 경로에서 종료 확인 wait를 한 번 더 반복하지 않는다.
            started = await self.process_controller.start_bot_process(
                target,
                appearance_timeout_seconds=1.8,
                appearance_poll_interval_seconds=0.3,
                force_refresh_liveness=False,
            )
            if not started:
                await self._safe_edit_query_message(query, f"❌ {target} 새 프로세스 시작 확인 실패")
                return
            await self._safe_edit_query_message(query, f"✅ {target} DB 초기화 후 새 프로세스 시작 확인")
            logger.info(
                "[CLEAN_RUN][CRITICAL_DONE] target=%s kind=external elapsed=%.2fs",
                target,
                time.monotonic() - started_at,
            )
            self._schedule_background_task(
                self._background_cleanup_dashboard(
                    query,
                    target,
                    operation_id,
                    label="clean_run_cleanup_dashboard",
                    cleanup_snapshot=cleanup_snapshot,
                ),
                operation_id=operation_id,
                target=target,
                label="clean_run_cleanup_dashboard",
            )

        except Exception as e:
            logger.exception(
                "[CLEAN_RUN][ERROR] target=%s elapsed=%.2fs error=%s",
                target,
                time.monotonic() - started_at,
                e,
            )
            if message_source:
                await self._queue_reply_via_message(message_source, f"❌ 오류 발생: {e}")

    async def simple_restart(
        self,
        query: CallbackQuery,
        target: str,
        *,
        callback_pre_acked: bool = False,
    ) -> None:
        """DB 유지 재시작."""
        trace = OperationContext("restart", target=target)
        started_at = time.monotonic()
        operation_id = f"restart-{uuid.uuid4().hex[:12]}"
        old_generation_id = f"old-{uuid.uuid4().hex[:8]}"
        new_generation_id = f"new-{uuid.uuid4().hex[:8]}"
        logger.info(
            "[RESTART][START] operation_id=%s target_id=%s generation_id=%s mode=simple "
            "critical_path=true cleanup_policy=fast refresh_type=targeted result=start",
            operation_id,
            target,
            old_generation_id,
        )
        if not self._try_begin_target_operation(target, operation_id):
            await self._notify_restart_status(
                query,
                f"⏳ {target} 작업이 이미 진행 중입니다.",
                callback_pre_acked=callback_pre_acked,
            )
            return
        self._target_latest_generation[target] = new_generation_id
        cleanup_snapshot = self._capture_old_terminal_cleanup_snapshot(target, operation_id)
        await self._notify_restart_status(
            query,
            f"🔄 {target} 재시작 요청 접수",
            callback_pre_acked=callback_pre_acked,
        )
        trace.checkpoint("initial_callback_done")

        try:
            if _is_z_flow_target(target, self.monitor, self._get_z_flow_bridge()):
                z_flow_state, data_dir = self._classify_z_flow_restart_target(target)
                if z_flow_state == "missing":
                    await self._notify_restart_status(
                        query,
                        "❌ Z-Flow 디렉토리를 찾을 수 없습니다.",
                        callback_pre_acked=callback_pre_acked,
                    )
                    return
                if z_flow_state == "stopped":
                    logger.info(
                        "[RESTART][SAFE_START] operation_id=%s target_id=%s kind=z_flow result=skip_stop_start_only",
                        operation_id,
                        target,
                    )
                    await self._start_process_impl(query, target, callback_pre_acked=callback_pre_acked)
                    return

                data_dir = await self._stop_z_flow_target(
                    target,
                    cleanup_snapshot=cleanup_snapshot,
                    operation_id=operation_id,
                )
                if data_dir is None:
                    await self._notify_restart_status(
                        query,
                        "❌ Z-Flow 디렉토리를 찾을 수 없습니다.",
                        callback_pre_acked=callback_pre_acked,
                    )
                    return
                await self._start_process_impl(query, target, callback_pre_acked=callback_pre_acked)
                self._schedule_background_task(
                    self._background_restart_cleanup(
                        query,
                        target,
                        operation_id,
                        new_generation_id,
                        cleanup_snapshot=cleanup_snapshot,
                    ),
                    operation_id=operation_id,
                    target=target,
                    label="restart_z_flow_cleanup_dashboard",
                )
                return

            identity_store, identity_session = self._load_dashboard_session_identity(target)
            restart_state = self._classify_external_restart_target(target, identity_session)
            if restart_state == "stopped":
                logger.info(
                    "[RESTART][SAFE_START] operation_id=%s target_id=%s kind=external result=skip_stop_start_only",
                    operation_id,
                    target,
                )
                await self._start_process_impl(query, target, callback_pre_acked=callback_pre_acked)
                return

            _skip_bounded_stop = False  # fallback/dead 경로에서 bounded_stop 건너뛰기 플래그
            if not self._is_validated_dashboard_identity(identity_session):
                if self._has_null_identity_fields(identity_session) and restart_state != "liveness_error":
                    # 신원 필드가 전부 null → name-based fallback 경로
                    logger.warning(
                        "[RESTART][IDENTITY_REFUSED] op_id=%s target=%s reason=%s entering_fallback",
                        operation_id, target, "identity_not_verified",
                    )
                    fb = await self._fallback_terminate_without_identity(
                        target, cleanup_snapshot, operation_id=operation_id
                    )
                    if not fb.process_cleared:
                        # barrier 실패 — 시작 금지, manual recovery 유지
                        self._mark_dashboard_identity_manual_recovery(
                            identity_store,
                            identity_session,
                            operation_id=operation_id,
                            reason="dashboard_restart_fallback_survivors",
                        )
                        await self._notify_restart_status(
                            query,
                            f"⚠️ {target} 이전 프로세스가 남아 있어 재시작을 보류했습니다. 수동 복구가 필요합니다.",
                            callback_pre_acked=callback_pre_acked,
                            show_alert=True,
                        )
                        return
                    # barrier 통과 — bounded_stop 건너뛰고 start_bot_process로 직접 진행
                    logger.info(
                        "[RESTART][FALLBACK_CLEARED] op_id=%s target=%s killed=%d method=%s",
                        operation_id, target, fb.killed_count, fb.method,
                    )
                    _skip_bounded_stop = True
                else:
                    # 신원 필드는 있지만 검증 실패 → live 3-way 검증 (kill_process T3/T4와 대칭)
                    # liveness_error인 경우 이미 조회 실패했으므로 live 검증 건너뛰고 unverifiable 취급
                    if restart_state == "liveness_error":
                        live_result = "unverifiable"
                    else:
                        live_result = await asyncio.to_thread(self._live_verify_dashboard_identity, target, identity_session)
                    if live_result == "verified":
                        pass  # 아래 bounded_stop으로 fall through
                    elif live_result == "dead":
                        # captured(미검증) 신원 + 프로세스 없음 → null 처리 후 재시작 단계로 진행
                        # (종료할 프로세스가 없으므로 bounded_stop 건너뛰고 start_process로)
                        if (
                            identity_session is not None
                            and identity_session.validation_status == "captured"
                        ):
                            if identity_store is not None:
                                identity_store.clear_runtime_identity()
                            logger.info(
                                "[RESTART][ALREADY_DEAD] operation_id=%s target=%s "
                                "reason=captured_identity_process_gone skip_stop=true",
                                operation_id,
                                target,
                            )
                            await self._start_process_impl(
                                query, target, callback_pre_acked=callback_pre_acked
                            )
                            return
                        else:
                            self._mark_dashboard_identity_manual_recovery(
                                identity_store,
                                identity_session,
                                operation_id=operation_id,
                                reason="dashboard_restart_identity_not_verified",
                            )
                            logger.warning(
                                "[RESTART][IDENTITY_REFUSED] operation_id=%s target_id=%s "
                                "reason=dashboard_restart_dead_not_captured",
                                operation_id,
                                target,
                            )
                            await self._notify_restart_status(
                                query,
                                "⚠️ 저장된 세션 신원이 확인되지 않아 자동 재시작을 거부했습니다. 수동 복구가 필요합니다.",
                                callback_pre_acked=callback_pre_acked,
                                show_alert=True,
                            )
                            return
                    else:
                        # unverifiable → 기존 거부 동작 유지
                        self._mark_dashboard_identity_manual_recovery(
                            identity_store,
                            identity_session,
                            operation_id=operation_id,
                            reason="dashboard_restart_identity_not_verified",
                        )
                        logger.warning(
                            "[RESTART][IDENTITY_REFUSED] operation_id=%s target_id=%s reason=dashboard_restart_identity_not_verified",
                            operation_id,
                            target,
                        )
                        await self._notify_restart_status(
                            query,
                            "⚠️ 저장된 세션 신원이 확인되지 않아 자동 재시작을 거부했습니다. 수동 복구가 필요합니다.",
                            callback_pre_acked=callback_pre_acked,
                            show_alert=True,
                        )
                        return
            if not _skip_bounded_stop:
                bounded_stop = self._get_bounded_stop_specific_process()
                if not callable(bounded_stop):
                    await self._notify_restart_status(
                        query,
                        f"⚠️ {target} 신원 확인 종료 기능을 사용할 수 없어 재시작을 보류했습니다.",
                        callback_pre_acked=callback_pre_acked,
                        show_alert=True,
                    )
                    logger.warning(
                        "[RESTART][ABORT_NO_EXACT_STOP] operation_id=%s target_id=%s result=blocked_no_bounded_stop",
                        operation_id,
                        target,
                    )
                    return
                stop_kwargs: dict[str, object] = {
                    "terminate_timeout_seconds": 1.0,
                    "kill_timeout_seconds": 1.0,
                    "force_refresh": True,
                    "operation_id": operation_id,
                    "session_identity": identity_session,
                }
                stop_result = await asyncio.to_thread(
                    bounded_stop,
                    target,
                    **stop_kwargs,
                )
                logger.info(
                    "[RESTART][STOP_ATTEMPT_DONE] operation_id=%s target_id=%s generation_id=%s "
                    "critical_path=true result=%s elapsed_ms=%d still_alive_pids=%s",
                    operation_id,
                    target,
                    old_generation_id,
                    "blocked_alive" if getattr(stop_result, "still_alive_pids", ()) else "stopped",
                    getattr(stop_result, "elapsed_ms", int((time.monotonic() - started_at) * 1000)),
                    list(getattr(stop_result, "still_alive_pids", ())),
                )
                trace.checkpoint("stop_attempt_done")
                if getattr(stop_result, "still_alive_pids", ()) or not getattr(
                    stop_result,
                    "safe_to_spawn",
                    True,
                ):
                    await self._notify_restart_status(
                        query,
                        f"⚠️ {target} 이전 프로세스가 아직 살아 있어 새 프로세스 시작을 보류했습니다.",
                        callback_pre_acked=callback_pre_acked,
                        show_alert=True,
                    )
                    logger.warning(
                        "[RESTART][ABORT_DUPLICATE_RISK] operation_id=%s target_id=%s generation_id=%s "
                        "critical_path=true result=blocked_old_alive elapsed_ms=%d",
                        operation_id,
                        target,
                        old_generation_id,
                        int((time.monotonic() - started_at) * 1000),
                    )
                    return

            started = await self.process_controller.start_bot_process(
                target,
                appearance_timeout_seconds=1.8,
                appearance_poll_interval_seconds=0.3,
                force_refresh_liveness=False,
            )
            logger.info(
                "[RESTART][SPAWN_LIVENESS_DONE] operation_id=%s target_id=%s generation_id=%s "
                "critical_path=true result=%s elapsed_ms=%d",
                operation_id,
                target,
                new_generation_id,
                "spawn_alive" if started else "spawn_failed",
                int((time.monotonic() - started_at) * 1000),
            )
            trace.checkpoint("spawn_liveness_done")
            if not started:
                await self._notify_restart_status(
                    query,
                    f"❌ {target} 새 프로세스 시작 확인 실패",
                    callback_pre_acked=callback_pre_acked,
                )
                return

            await self._notify_restart_status(
                query,
                f"✅ {target} 새 프로세스 시작 확인",
                callback_pre_acked=callback_pre_acked,
            )
            trace.checkpoint("result_notification_done")
            logger.info(
                "[RESTART][CRITICAL_DONE] operation_id=%s target_id=%s old_generation_id=%s "
                "new_generation_id=%s critical_path=true result=spawn_alive elapsed_ms=%d",
                operation_id,
                target,
                old_generation_id,
                new_generation_id,
                int((time.monotonic() - started_at) * 1000),
            )
            self._schedule_background_task(
                self._background_restart_cleanup(
                    query,
                    target,
                    operation_id,
                    new_generation_id,
                    cleanup_snapshot=cleanup_snapshot,
                ),
                operation_id=operation_id,
                target=target,
                label="restart_cleanup_dashboard",
            )
            trace.checkpoint("cleanup_dashboard_scheduled")
        finally:
            trace.log_summary()
            self._finish_target_operation(target, operation_id)

    async def _simple_restart_core(self, target: str, notifier: object | None = None) -> None:
        """Query-free 재시작 코어 — 백그라운드 자동 재시작 전용.

        UI 알림(query) 없이 Z-Flow 재시작 로직만 수행한다.
        _background_restart_cleanup 및 대시보드 갱신은 생략된다.
        (query가 없으므로 갱신 경로 없음)

        호출 순서:
          1. _try_begin_target_operation  → 중복 방지
          2. _classify_z_flow_restart_target → 상태 분류
          3. _stop_z_flow_target (stopped 상태가 아닌 경우)
          4. _start_process_impl(query=None, target)
        """
        operation_id = f"auto-restart-{uuid.uuid4().hex[:12]}"
        logger.info(
            "[AUTO_RESTART][START] operation_id=%s target_id=%s mode=uptime_24h",
            operation_id,
            target,
        )
        if not self._try_begin_target_operation(target, operation_id):
            logger.info(
                "[AUTO_RESTART][SKIP] operation_id=%s target_id=%s reason=operation_in_progress",
                operation_id,
                target,
            )
            return

        new_generation_id = f"new-{uuid.uuid4().hex[:8]}"
        self._target_latest_generation[target] = new_generation_id
        cleanup_snapshot = self._capture_old_terminal_cleanup_snapshot(target, operation_id)

        try:
            if _is_z_flow_target(target, self.monitor, self._get_z_flow_bridge()):
                z_flow_state, data_dir = self._classify_z_flow_restart_target(target)
                if z_flow_state == "missing":
                    logger.warning(
                        "[AUTO_RESTART][SKIP] operation_id=%s target_id=%s reason=z_flow_dir_missing",
                        operation_id,
                        target,
                    )
                    return
                if z_flow_state == "stopped":
                    logger.info(
                        "[AUTO_RESTART][SAFE_START] operation_id=%s target_id=%s kind=z_flow result=skip_stop_start_only",
                        operation_id,
                        target,
                    )
                    if notifier:
                        await notifier.update("🔄 재시작 중...")  # type: ignore[union-attr]
                    await self._start_process_impl(None, target)
                    if notifier:
                        await notifier.update("✅ 재시작 완료")  # type: ignore[union-attr]
                    return

                if notifier:
                    await notifier.update("🛑 프로세스 종료 중...")  # type: ignore[union-attr]
                data_dir = await self._stop_z_flow_target(
                    target,
                    cleanup_snapshot=cleanup_snapshot,
                    operation_id=operation_id,
                )
                if data_dir is None:
                    logger.warning(
                        "[AUTO_RESTART][SKIP] operation_id=%s target_id=%s reason=stop_returned_none",
                        operation_id,
                        target,
                    )
                    if notifier:
                        await notifier.update("⚠️ 재시작 스킵 (stop_returned_none)")  # type: ignore[union-attr]
                    return
                if notifier:
                    await notifier.update("🔄 재시작 중...")  # type: ignore[union-attr]
                await self._start_process_impl(None, target)
                if notifier:
                    await notifier.update(f"✅ 재시작 완료 (uptime {getattr(notifier, '_uptime_hours', 0.0):.1f}h)")  # type: ignore[union-attr]
                return

            # 외부봇 경로: stopped 상태이면 바로 시작
            identity_store, identity_session = self._load_dashboard_session_identity(target)
            restart_state = self._classify_external_restart_target(target, identity_session)
            if restart_state == "stopped":
                logger.info(
                    "[AUTO_RESTART][SAFE_START] operation_id=%s target_id=%s kind=external result=skip_stop_start_only",
                    operation_id,
                    target,
                )
                if notifier:
                    await notifier.update("🔄 재시작 중...")  # type: ignore[union-attr]
                await self._start_process_impl(None, target)
                if notifier:
                    await notifier.update(f"✅ 재시작 완료 (uptime {getattr(notifier, '_uptime_hours', 0.0):.1f}h)")  # type: ignore[union-attr]
                return

            # 외부 pair-trading 봇: live 상태여도 stop→start 허용 (24h 자동 재시작)
            if restart_state == "live":
                _bridge = self._get_z_flow_bridge()
                _target_raw = self.process_controller.find_target_directory(target)
                _data_dir = (
                    Path(_target_raw) if _target_raw is not None else None
                )
                if _data_dir is not None and not _data_dir.is_dir():
                    _data_dir = _data_dir.parent
                _pair_meta = (
                    _bridge.get_external_pair_runtime_metadata(_data_dir)
                    if _bridge is not None and _data_dir is not None
                    else None
                )
                if _pair_meta is not None:
                    logger.info(
                        "[AUTO_RESTART][PAIR_STOP_START] operation_id=%s target_id=%s kind=external_pair",
                        operation_id,
                        target,
                    )
                    if notifier:
                        await notifier.update("🛑 프로세스 종료 중...")  # type: ignore[union-attr]
                    fb = await self._fallback_terminate_without_identity(
                        target, cleanup_snapshot, operation_id=operation_id
                    )
                    if not fb.process_cleared:
                        logger.warning(
                            "[AUTO_RESTART][SKIP] operation_id=%s target_id=%s reason=pair_stop_survivors_not_cleared",
                            operation_id,
                            target,
                        )
                        if notifier:
                            await notifier.update("⚠️ 재시작 스킵 (survivors_not_cleared)")  # type: ignore[union-attr]
                        return
                    if notifier:
                        await notifier.update("🔄 재시작 중...")  # type: ignore[union-attr]
                    await self._start_process_impl(None, target)
                    if notifier:
                        await notifier.update("✅ 재시작 완료")  # type: ignore[union-attr]
                    return

            logger.info(
                "[AUTO_RESTART][SKIP] operation_id=%s target_id=%s reason=state_%s_not_safe_for_auto_restart",
                operation_id,
                target,
                restart_state,
            )
            if notifier:
                await notifier.update(f"⚠️ 재시작 스킵 ({restart_state})")  # type: ignore[union-attr]
        except Exception as e:
            logger.exception(
                "[AUTO_RESTART][ERROR] operation_id=%s target_id=%s error=%s",
                operation_id,
                target,
                e,
            )
            if notifier:
                try:
                    await notifier.update("❌ 재시작 오류")  # type: ignore[union-attr]
                except Exception:
                    pass
        finally:
            self._finish_target_operation(target, operation_id)

    async def _stop_z_flow_target(
        self,
        target: str,
        *,
        cleanup_snapshot: TerminalCleanupSnapshot | None = None,
        operation_id: str | None = None,
    ) -> Path | None:
        cleanup_operation_id = operation_id or f"stop-{int(time.time() * 1000)}-{target}"
        if cleanup_snapshot is None:
            cleanup_snapshot = self._capture_old_terminal_cleanup_snapshot(
                target,
                cleanup_operation_id,
            )
        data_dir: Path | None = None
        for _, dir_name, slot_data_dir in _find_z_flow_processes(
            self.monitor, self._get_z_flow_bridge()
        ):
            if dir_name == target:
                data_dir = slot_data_dir
                break

        if data_dir is None:
            data_dir = _resolve_z_flow_target_dir(
                self.monitor, target, self._get_z_flow_bridge()
            )

        if data_dir is None:
            return None

        write_bot_state(data_dir, "MANUAL_STOP")
        _persist_manual_stop_session(
            data_dir,
            target,
            "z_flow_runtime",
            "manual-stop",
        )
        # 창 닫기로 종료되지 않은 고아/중복 런타임만 짧게 추가 정리한다.
        killed = await asyncio.to_thread(
            _kill_all_z_flow_processes,
            self.monitor,
            data_dir,
            1.0,
            self._get_z_flow_bridge(),
        )
        if killed > 0:
            logger.info(f"[Z_FLOW][STOP] {target}: {killed}개 프로세스 종료됨")

        self.bot.z_flow_bridge.cleanup_runtime_artifacts(data_dir)
        return data_dir

    def _delete_slot_runtime_db(self, data_dir: Path) -> list[str]:
        deleted: list[str] = []
        for name in ("pair_slot.db", "pair_slot.db-shm", "pair_slot.db-wal"):
            path = data_dir / name
            if not path.exists():
                continue
            try:
                os.remove(path)
                deleted.append(name)
            except OSError as e:
                logger.warning(f"Z-Flow DB 삭제 실패 ({path}): {e}")
        return deleted

    async def restart_all(self, query: CallbackQuery):
        """모든 프로세스 재시작"""
        try:
            await self._safe_edit_query_message(query, "🔄 모든 프로세스 종료 중...")

            await self.process_controller.stop_all_processes()
            await self.bot._cleanup_terminal_for_dashboard(self.bot.process_name)

            await asyncio.sleep(3)
            await self._safe_edit_query_message(query, "🚀 봇 병렬 재시작 시작...")

            ignored = load_ignored_dirs()
            await asyncio.to_thread(self.monitor.find_target_programs)
            target_dirs = [
                p.parent.name
                for p in self.monitor.target_paths
                if p.parent.name not in ignored
            ]

            if not target_dirs:
                await self._safe_edit_query_message(query, "⚠️ 재시작할 대상이 없습니다.")
                await asyncio.sleep(2)
                await self.dashboard_handler.update_dashboard(query, force_rescan=True)
                return

            success_count = await run_batch_operations(
                target_dirs,
                self.process_controller.start_bot_process,
                batch_size=5,
                delay=0.5,
            )

            if success_count > 0:
                await self._safe_edit_query_message(
                    query,
                    f"✅ {success_count}개 봇 재시작 명령 완료! 확인 중..."
                )
                await asyncio.sleep(4)
                await self.dashboard_handler.update_dashboard(query, force_rescan=True)
            else:
                await self._safe_edit_query_message(query, "❌ 재시작 가능한 봇이 없습니다.")

        except Exception as e:
            logger.error(f"전체 재시작 중 오류: {e}")
            await self._safe_edit_query_message(query, f"❌ 오류 발생: {e}")

    async def restart_running_only(self, query: CallbackQuery):
        """현재 실행 중인 봇만 재시작 (중단된 봇은 그대로 유지)"""
        try:
            await self._safe_edit_query_message(
                query, "🔍 현재 실행 중인 봇을 확인하고 있습니다..."
            )

            # 현재 실행 중인 봇 목록 확인
            running_dirs = []
            process_pairs = await asyncio.to_thread(self.monitor.find_processes, True)
            for _, path in process_pairs:
                running_dirs.append(path.parent.name)

            if not running_dirs:
                await self._safe_edit_query_message(query, "⚠️ 현재 실행 중인 봇이 없습니다.")
                await asyncio.sleep(2)
                await self.dashboard_handler.update_dashboard(query, force_rescan=True)
                return

            # 중복 제거
            running_dirs = list(set(running_dirs))

            await self._safe_edit_query_message(
                query,
                f"🛑 실행 중인 {len(running_dirs)}개 봇을 종료합니다..."
            )

            # 실행 중인 봇만 종료 + 해당 창 정리
            for dir_name in running_dirs:
                await asyncio.to_thread(
                    self.process_controller.kill_specific_process, dir_name
                )
                await self.bot._cleanup_terminal_for_dashboard(dir_name)

            await asyncio.sleep(3)

            # 종료된 봇들 재시작
            await self._safe_edit_query_message(
                query,
                f"🚀 {len(running_dirs)}개 봇 병렬 재시작 시작..."
            )

            success_count = await run_batch_operations(
                running_dirs,
                self.process_controller.start_bot_process,
                batch_size=5,
                delay=0.5,
            )

            if success_count > 0:
                await self._safe_edit_query_message(
                    query,
                    f"✅ {success_count}개 실행중 봇 재시작 명령 완료! 확인 중...\n"
                    f"(중단되었던 봇은 그대로 유지됩니다)"
                )
                await asyncio.sleep(4)
                await self.dashboard_handler.update_dashboard(query, force_rescan=True)
            else:
                await self._safe_edit_query_message(query, "❌ 재시작 가능한 봇이 없습니다.")

        except Exception as e:
            logger.error(f"실행중 봇 재시작 중 오류: {e}")
            await self._safe_edit_query_message(query, f"❌ 오류 발생: {e}")

    async def toggle_rotation(self, query: CallbackQuery, target: str):
        """페어 로테이션 ON/OFF 토글"""
        try:
            bridge = self._get_z_flow_bridge()
            if not bridge:
                await self._safe_answer_query(query, "❌ 페어 매매가 활성화되지 않았습니다.")
                return
            if not bridge.is_pair_trading_ui_enabled():
                await self._safe_answer_query(query, "❌ 페어 트레이딩이 비활성화되어 있습니다.")
                return

            # 이미 ON이면 → OFF로 전환 (auto_managed=0 + WAITING 중립화)
            if bridge.is_rotation_enabled(target):
                bridge.disable_rotation_to_manual(target)
                await self._safe_answer_query(query, f"{target} ⏸️ OFF · 수동 즉시진입('시작' 시 1라운드)")
                await self.dashboard_handler.show_process_detail(query, target)
                return

            # OFF → ON: 슬롯 타입 선택 UI 표시
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            keyboard = [
                [
                    InlineKeyboardButton(
                        "BTC/ETH 페어", callback_data=f"set_slot_type:{target}:BTC_ETH"
                    ),
                    InlineKeyboardButton(
                        "기타 페어", callback_data=f"set_slot_type:{target}:OTHER"
                    ),
                ],
                [InlineKeyboardButton("🔙 취소", callback_data=f"detail:{target}")],
            ]

            from z_pulse.utils import escape_markdown

            esc_target = escape_markdown(target)
            await self._safe_edit_query_message(
                query,
                f"🤖 *{esc_target}* 자동 배정 활성화\n\n"
                f"슬롯 타입을 선택하세요:\n"
                f"• *BTC/ETH 페어*: BTC, ETH 관련 페어만 자동 배정\n"
                f"• *기타 페어*: BTC/ETH 외 알트코인 페어 자동 배정\n\n"
                f"자동 배정 ON 시, 프로세스가 정지 상태라면 즉시 실행되며 첫 진입은 시그널 확인 후 진행됩니다\\.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(f"페어 로테이션 토글 중 오류: {e}")
            await self._safe_answer_query(query, "❌ 페어 로테이션 변경 실패")

    async def set_slot_type(self, query: CallbackQuery, target: str, slot_type: str):
        """슬롯 타입 선택 후 로테이션 활성화"""
        try:
            bridge = self._get_z_flow_bridge()
            if not bridge:
                await self._safe_answer_query(query, "❌ 페어 매매가 활성화되지 않았습니다.")
                return
            if not bridge.is_pair_trading_ui_enabled():
                await self._safe_answer_query(query, "❌ 페어 트레이딩이 비활성화되어 있습니다.")
                return

            try:
                self._require_pair_trading_env()
            except self._get_pair_trading_config_error() as exc:
                env_path = self._get_pair_trading_env_path()
                message = (
                    f"❌ 자동 배정 ON 중단\n"
                    f"설정 파일: {env_path}\n"
                    f"사유: {exc}"
                )
                logger.error("[PAIR][CONFIG] %s", message.replace("\n", " | "))
                await self._safe_answer_query(query, "❌ pair_trading 설정 오류")
                if getattr(query, "message", None):
                    await self._reply_via_message(query.message, message)
                return

            bridge.enable_rotation(target, slot_type)

            if bridge.is_runtime_target(target, self.monitor):
                try:
                    already_running, _ = await asyncio.to_thread(
                        self.process_controller.is_process_running,
                        target,
                        True,
                    )
                    if not already_running:
                        resume = getattr(bridge, "resume_auto_assign_to_waiting", None)
                        if callable(resume):
                            try:
                                resume(target)
                            except Exception as resume_err:
                                logger.warning(
                                    "[SET_SLOT_TYPE][WAITING_FAILED] target=%s error=%s",
                                    target,
                                    resume_err,
                                )
                        launch_spec = bridge.get_runtime_launch_spec_for_target(target, self.monitor)
                        if launch_spec is None:
                            logger.warning("[SET_SLOT_TYPE] launch_spec 구성 실패: target=%s", target)
                            await self._safe_answer_query(
                                query,
                                f"❌ {target} 자동 배정 ON 실패: Z-Flow 런타임 실행/할당 실패",
                            )
                            await self.dashboard_handler.show_process_detail(query, target)
                            return
                        launched = await self.process_controller.start_bot_process(target, launch_spec=launch_spec)
                        if launched:
                            await self._safe_answer_query(query, f"{target} 자동 배정: 🚀 실행됨 (ON_WAITING → 실행 중)")
                        else:
                            await self._safe_answer_query(
                                query,
                                f"❌ {target} 자동 배정 ON 실패: Z-Flow 런타임 실행/할당 실패",
                            )
                        await self.dashboard_handler.show_process_detail(query, target)
                        return

                    await self._safe_answer_query(query, f"{target} 자동 배정: ⏳ ON_WAITING")
                    await self.dashboard_handler.show_process_detail(query, target)
                except Exception:
                    logger.exception("%s: Z-Flow 내부봇 자동 배정 ON 실패", target)
                    await self._safe_answer_query(
                        query,
                        f"❌ {target} 자동 배정 ON 실패: Z-Flow 런타임 실행/할당 실패",
                    )
                return

            from z_pulse.config import EnvConfigHandler

            target_path = self.process_controller.find_target_directory(target)
            if target_path:
                target_dir = target_path if target_path.is_dir() else target_path.parent
                exit_reservation_ok = EnvConfigHandler.update_key(
                    target_dir, "EXIT_RESERVATION", "true"
                )
                if not exit_reservation_ok:
                    logger.error(f"❌ {target}: 자동 배정 ON 설정 실패 (EXIT_RESERVATION=false)")
                    fail_message = f"❌ {target} 자동 배정 ON 실패: setting.env 업데이트 실패"
                    await self._safe_answer_query(query, fail_message)
                    if getattr(query, "message", None):
                        await self._reply_via_message(query.message, fail_message)
                    return
            else:
                logger.error(f"❌ {target}: 자동 배정 ON 설정 실패 (target path not found)")
                fail_message = f"❌ {target} 자동 배정 ON 실패: 대상 경로 확인 실패"
                await self._safe_answer_query(query, fail_message)
                if getattr(query, "message", None):
                    await self._reply_via_message(query.message, fail_message)
                return

            already_running, _ = await asyncio.to_thread(
                self.process_controller.is_process_running,
                target,
                True,
            )
            if not already_running:
                resume = getattr(bridge, "resume_auto_assign_to_waiting", None)
                if callable(resume):
                    try:
                        resume(target)
                    except Exception as resume_err:
                        logger.warning(
                            "[SET_SLOT_TYPE][WAITING_FAILED] target=%s error=%s",
                            target,
                            resume_err,
                        )
                await self._safe_answer_query(query, f"{target} 자동 배정: ⏳ ON_WAITING")
                await self.dashboard_handler.show_process_detail(query, target)
                return

            slot_label = "BTC/ETH" if slot_type == "BTC_ETH" else "기타"
            await self._safe_answer_query(query, f"{target} 자동 배정: 🤖 ON ({slot_label})")

            # 상세 정보 화면 새로고침
            await self.dashboard_handler.show_process_detail(query, target)
        except Exception as e:
            logger.error(f"슬롯 타입 설정 중 오류: {e}")
            await self._safe_answer_query(query, "❌ 슬롯 타입 설정 실패")

    async def force_assign(self, query: CallbackQuery, target: str):
        """비정상 종료 봇을 강제할당 가능 상태로 전환 (DB 삭제 + 플래그 설정)"""
        try:
            async def _safe_answer(*args: Any, **kwargs: Any) -> None:
                await get_telegram_gateway().answer_callback_query(
                    query, *args, **kwargs
                )

            bridge = self._get_z_flow_bridge()
            if not bridge:
                await _safe_answer("❌ 페어 매매가 활성화되지 않았습니다.")
                return
            if not bridge.is_pair_trading_ui_enabled():
                await _safe_answer("❌ 페어 트레이딩이 비활성화되어 있습니다.")
                return

            resume_to_waiting = getattr(bridge, "resume_auto_assign_to_waiting", None)
            if callable(resume_to_waiting):
                deleted_files = resume_to_waiting(target)
            else:
                deleted_files = bridge.force_assign_bot(target)
            if not isinstance(deleted_files, list):
                deleted_files = []
            logger.info(f"[FORCE_ASSIGN][UI] {target}: 강제할당 버튼 처리 완료")

            msg = f"⚡ {target}: 자동 배정 재개 완료"
            if deleted_files:
                msg += f"\n🗑️ 삭제: {', '.join(deleted_files)}"
            await _safe_answer(msg, show_alert=True)

            # 상세 정보 화면 새로고침
            await self.dashboard_handler.show_process_detail(query, target)
        except Exception as e:
            logger.error(f"강제할당 중 오류: {e}")
            await get_telegram_gateway().answer_callback_query(query, "❌ 강제할당 실패")

