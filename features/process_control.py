from __future__ import annotations

"""
프로세스 제어를 담당하는 클래스

Phase 4.4 리팩토링: Z-Pulse에서 프로세스 제어 관련 로직 분리
"""

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Tuple, List, Callable, Protocol, cast

import psutil

from z_pulse.constants import FileConfig
from z_pulse.monitoring.session_identity import (
    capture_external_bot_identity,
    lookup_started_process,
)

if TYPE_CHECKING:
    from z_pulse.monitoring.process_monitor import ProcessMonitor
    from z_pulse.platforms.base import PlatformHandler

logger = logging.getLogger(__name__)

DEFAULT_PROCESS_APPEARANCE_TIMEOUT_SECONDS = 5.0
DEFAULT_LAUNCH_SPEC_APPEARANCE_TIMEOUT_SECONDS = 15.0  # 내부봇(launch_spec) 전용 — subprocess spawn 후 등장까지 최대 15초
DEFAULT_PROCESS_APPEARANCE_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_PROCESS_TERMINATE_WAIT_SECONDS = 1.0


class ProcessByDirFinder(Protocol):
    def __call__(
        self,
        dir_name: str,
        *,
        force_refresh: bool = False,
    ) -> list[tuple[psutil.Process, Path]]:
        ...


@dataclass(frozen=True)
class BoundedStopResult:
    target: str
    stopped_count: int
    still_alive_pids: tuple[int, ...] = ()
    elapsed_ms: int = 0

    @property
    def safe_to_spawn(self) -> bool:
        return not self.still_alive_pids


def write_bot_state(dir_path: Path, state: str) -> None:
    """봇 state 파일에 상태를 기록합니다.

    Args:
        dir_path: 봇 디렉토리 경로
        state: 기록할 상태 ("EXIT_RESERVATION" 또는 "MANUAL_STOP")
    """
    try:
        state_file = dir_path / FileConfig.BOT_STATE_FILE
        with open(state_file, 'w', encoding='utf-8') as f:
            f.write(state)
        logger.debug(f"봇 state 기록: {dir_path.name} → {state}")
    except Exception as e:
        logger.warning(f"봇 state 기록 실패 ({dir_path.name}): {e}")


def clear_bot_state(dir_path: Path) -> None:
    """봇 state 파일을 삭제합니다 (페어 할당 또는 수동 시작 시 호출).

    Args:
        dir_path: 봇 디렉토리 경로
    """
    try:
        state_file = dir_path / FileConfig.BOT_STATE_FILE
        if state_file.exists():
            state_file.unlink()
            logger.debug(f"봇 state 초기화: {dir_path.name}")
    except Exception as e:
        logger.warning(f"봇 state 초기화 실패 ({dir_path.name}): {e}")


def read_bot_state(dir_path: Path) -> str:
    """봇 state 파일을 읽어 현재 상태를 반환합니다.

    Returns:
        "EXIT_RESERVATION", "MANUAL_STOP", 또는 "" (파일 없음/빈 상태)
    """
    try:
        state_file = dir_path / FileConfig.BOT_STATE_FILE
        if not state_file.exists():
            return ""
        # utf-8-sig: BOM(EF BB BF) 자동 제거
        return state_file.read_text(encoding='utf-8-sig').strip()
    except Exception as e:
        logger.debug(f"봇 state 읽기 실패 ({dir_path.name}): {e}")
        return ""


class ProcessController:
    """프로세스 제어를 담당하는 클래스"""

    def __init__(
        self,
        monitor: 'ProcessMonitor',
        platform_handler: 'PlatformHandler',
        process_name: str,
        auto_arrange_callback: Optional[Callable[[], None]] = None,
        variational_wallet_callback: Optional[Callable[[str], None]] = None,
    ):
        """
        Args:
            monitor: ProcessMonitor 인스턴스 (프로세스 탐색용)
            platform_handler: PlatformHandler 인스턴스 (플랫폼별 명령 실행)
            process_name: 실행할 프로세스 이름 (예: "Z-Pulse 실행 파일명")
            auto_arrange_callback: 프로세스 시작 후 창 정렬 트리거 (선택)
            variational_wallet_callback: VARIATIONAL 봇 기동 시 지갑 연결 트리거 (선택)
                                         인자: dir_name (str)
        """
        self.monitor = monitor
        self.platform_handler = platform_handler
        self.process_name = process_name
        self.auto_arrange_callback = auto_arrange_callback
        self.variational_wallet_callback = variational_wallet_callback

        logger.info(f"ProcessController 초기화 완료 (process_name: {process_name})")

    def find_target_directory(self, dir_name: str) -> Optional[Path]:
        """디렉토리명으로 target_path 찾기"""
        # monitor.all_program_paths를 기준으로 찾아야 ignore된 디렉토리도 찾을 수 있음
        for path in self.monitor.all_program_paths:
            if path.parent.name == dir_name:
                return path
        bridge = getattr(self.monitor, "z_flow_bridge", None)
        resolver = getattr(bridge, "resolve_runtime_data_dir", None)
        if callable(resolver):
            data_dir = resolver(
                dir_name,
                self.monitor,
                target_dir=getattr(self.monitor, "target_dir", None),
                ignore_list=getattr(self.monitor, "ignore_list", set()),
            )
            if isinstance(data_dir, (str, Path)):
                data_dir = Path(data_dir)
                env_path = data_dir / "setting.env"
                return env_path if env_path.exists() else data_dir
        return None

    def is_process_running(
        self,
        dir_name: str,
        force_refresh: bool = False,
    ) -> Tuple[bool, List[Tuple[psutil.Process, Path]]]:
        """특정 디렉토리의 프로세스가 실행 중인지 확인"""
        running_processes = self._find_target_processes(
            dir_name,
            force_refresh=force_refresh,
        )
        return len(running_processes) > 0, running_processes

    def _find_alive_z_flow_proc(self, dir_name: str) -> psutil.Process | None:
        """z_flow runtime이 PID-file로 탐지되면 살아있는 proc 반환, 아니면 None."""
        _bridge = getattr(self.monitor, "z_flow_bridge", None)
        if _bridge is None:
            return None
        _finder = getattr(self.monitor, "find_z_flow_processes", None)
        if not callable(_finder):
            return None
        try:
            _raw = _finder()
            if not isinstance(_raw, list):
                return None
            return next(
                (proc for proc, dname, _ in _raw if dname == dir_name and proc is not None),
                None,
            )
        except Exception:
            return None

    def _find_target_processes(
        self,
        dir_name: str,
        force_refresh: bool = False,
    ) -> List[Tuple[psutil.Process, Path]]:
        finder = getattr(self.monitor, "find_processes_by_dir", None)
        if callable(finder) and hasattr(type(self.monitor), "find_processes_by_dir"):
            typed_finder = cast(ProcessByDirFinder, finder)
            result = list(typed_finder(dir_name, force_refresh=force_refresh))
        else:
            process_tuples = self.monitor.find_processes(force_refresh=force_refresh)
            result = [
                (proc, path)
                for proc, path in process_tuples
                if path.parent.name == dir_name
            ]

        if result:
            return result

        # exe-scan이 z_flow runtime을 못 잡을 때 PID-file 기반으로 보강
        alive = self._find_alive_z_flow_proc(dir_name)
        if alive is not None:
            # sentinel path: kill/stop 경로에서 .parent = Path(dir_name)이 되도록 구성
            # (Path(dir_name)만 쓰면 .parent = Path(".")가 되어 write_bot_state 오기록 위험)
            return [(alive, Path(dir_name) / "_z_flow_sentinel")]
        return []

    def _invalidate_process_cache(self) -> None:
        invalidator = getattr(self.monitor, "invalidate_cache", None)
        if callable(invalidator):
            invalidator()

    async def _wait_for_process_appearance(
        self,
        dir_name: str,
        timeout_seconds: float = DEFAULT_PROCESS_APPEARANCE_TIMEOUT_SECONDS,
        interval_seconds: float = DEFAULT_PROCESS_APPEARANCE_POLL_INTERVAL_SECONDS,
        *,
        force_refresh: bool = True,
    ) -> bool:
        """시작 직후 실제 프로세스 출현을 폴링해 확인"""
        attempts = max(1, math.ceil(timeout_seconds / interval_seconds) + 1)
        for attempt in range(attempts):
            is_running, _ = self.is_process_running(dir_name, force_refresh=force_refresh)
            if is_running:
                return True
            if attempt < attempts - 1:
                await asyncio.sleep(interval_seconds)
        return False

    async def start_bot_process(
        self,
        dir_name: str,
        *,
        launch_spec: dict | None = None,
        appearance_timeout_seconds: float = DEFAULT_PROCESS_APPEARANCE_TIMEOUT_SECONDS,
        appearance_poll_interval_seconds: float = DEFAULT_PROCESS_APPEARANCE_POLL_INTERVAL_SECONDS,
        force_refresh_liveness: bool = True,
    ) -> bool:
        """봇 프로세스 시작 (platform_handler에 위임)"""
        # launch_spec이 없으면 bridge로 runtime target spec 자동 해석 시도
        # (rotation 경로처럼 launch_spec 없이 호출된 경우 내부봇을 정상 기동하기 위함)
        if launch_spec is None:
            _bridge = getattr(self.monitor, "z_flow_bridge", None)
            _resolver = getattr(_bridge, "get_runtime_launch_spec_for_target", None)
            if callable(_resolver):
                try:
                    _resolved = _resolver(dir_name, self.monitor)
                    if isinstance(_resolved, dict) and _resolved:
                        launch_spec = cast("dict[str, object]", _resolved)
                except Exception as _exc:
                    logger.warning(
                        "[PROC][START][LAUNCH_SPEC_RESOLVE_FAILED] bot=%s reason=%s",
                        dir_name, _exc,
                    )

        target_path = self.find_target_directory(dir_name)
        if not target_path:
            if launch_spec is not None:
                # launch_spec이 cmd/cwd를 완전히 지정하므로 디렉토리 탐색 실패를 무시하고 진행
                success = await self.platform_handler.start_bot_process(
                    dir_name, "", self.process_name, launch_spec=launch_spec
                )
                if not success:
                    logger.warning(f"프로세스 시작 실패: {dir_name}")
                    return False
                self._invalidate_process_cache()
                return await self._wait_for_process_appearance(
                    dir_name,
                    timeout_seconds=max(
                        appearance_timeout_seconds,
                        DEFAULT_LAUNCH_SPEC_APPEARANCE_TIMEOUT_SECONDS,
                    ),
                    interval_seconds=appearance_poll_interval_seconds,
                    force_refresh=force_refresh_liveness,
                )
            logger.warning(f"디렉토리를 찾을 수 없음: {dir_name}")
            return False

        target_dir = target_path.parent
        bot_path = str(target_dir)

        # z_flow runtime target이면 이미 살아있는 프로세스를 선검사해 double-spawn 방지
        # (_find_alive_z_flow_proc: bridge 없으면 None, bridge 있으면 PID-file 기반 탐지)
        if getattr(self.monitor, "z_flow_bridge", None) is not None:
            _alive = self._find_alive_z_flow_proc(dir_name)
            if _alive is not None:
                logger.info(
                    "[PROC][START][ALREADY_RUNNING] bot=%s skip spawn (pid=%s)",
                    dir_name,
                    getattr(_alive, "pid", "?"),
                )
                return True

        # Platform handler를 통해 봇 프로세스 시작
        success = await self.platform_handler.start_bot_process(
            dir_name, bot_path, self.process_name,
            launch_spec=launch_spec,
        )

        if not success:
            logger.warning(f"프로세스 시작 실패: {dir_name}")
            return False

        self._invalidate_process_cache()
        appeared = await self._wait_for_process_appearance(
            dir_name,
            timeout_seconds=(
                max(appearance_timeout_seconds, DEFAULT_LAUNCH_SPEC_APPEARANCE_TIMEOUT_SECONDS)
                if launch_spec is not None
                else appearance_timeout_seconds
            ),
            interval_seconds=appearance_poll_interval_seconds,
            force_refresh=force_refresh_liveness,
        )
        if not appeared:
            logger.warning(f"[PROC][START] bot={dir_name} status=no-process")
            return False

        clear_bot_state(target_dir)
        try:
            await asyncio.to_thread(
                capture_external_bot_identity,
                target=dir_name,
                data_dir=target_dir,
                process_lookup=lambda t: lookup_started_process(self.is_process_running, t),
            )
        except Exception as exc:
            logger.warning(
                "[PROC][START][IDENTITY_CAPTURE_FAILED] bot=%s reason=%s",
                dir_name,
                exc,
            )
        logger.info(f"[PROC][START] bot={dir_name} status=ok")
        if self.auto_arrange_callback:
            arrange_marker = getattr(self.monitor, "mark_start_arrange_handled", None)
            if callable(arrange_marker):
                arrange_marker(dir_name)
            self.auto_arrange_callback()
        # [VARI] VARIATIONAL 봇 기동 감지 → 지갑 연결 예약
        if self.variational_wallet_callback:
            self.variational_wallet_callback(dir_name)

        return True

    def bounded_stop_specific_process(
        self,
        target_dir: str,
        *,
        terminate_timeout_seconds: float = 1.0,
        kill_timeout_seconds: float = 1.0,
        force_refresh: bool = False,
        operation_id: str | None = None,
        session_identity: object | None = None,
    ) -> BoundedStopResult:
        """대상 디렉토리 프로세스를 짧은 예산 안에서 종료하고 생존 여부를 반환합니다.

        restart fast path에서 duplicate/token conflict를 피하기 위한 safety-first stop입니다.
        대상 목록은 ProcessMonitor의 dir 기반 탐색 결과로 제한하며 PID 단독 broad kill은 하지 않습니다.
        """
        started_at = time.monotonic()
        self.monitor.suppress_decrease_alert()
        target_pairs = self._find_target_processes(target_dir, force_refresh=force_refresh)
        identity_pgid = getattr(session_identity, "pgid", None)
        if identity_pgid is not None:
            filtered_pairs = []
            for proc, path in target_pairs:
                try:
                    if os.getpgid(proc.pid) == int(identity_pgid):
                        filtered_pairs.append((proc, path))
                except Exception:
                    continue
            target_pairs = filtered_pairs
        target_processes = [proc for proc, _ in target_pairs]
        target_dir_path = target_pairs[0][1].parent if target_pairs else None

        identities: list[dict[str, object]] = []
        for proc in target_processes:
            identity: dict[str, object] = {"pid": getattr(proc, "pid", None)}
            try:
                identity["pid_start_time"] = proc.create_time()
            except Exception:
                identity["pid_start_time"] = None
            try:
                identity["pgid"] = getattr(proc, "pid", None) and os.getpgid(proc.pid)
            except Exception:
                identity["pgid"] = None
            identities.append(identity)

        logger.info(
            "[PROC][STOP][START] operation_id=%s target_id=%s generation_id=old "
            "critical_path=true refresh_type=%s cleanup_policy=fast candidates=%d identities=%s "
            "session_identity_generation=%s session_pgid=%s session_tty=%s",
            operation_id or "none",
            target_dir,
            "force" if force_refresh else "cached_or_dir",
            len(target_processes),
            identities,
            getattr(session_identity, "identity_generation", None),
            getattr(session_identity, "pgid", None),
            getattr(session_identity, "tty", None),
        )

        if not target_processes:
            return BoundedStopResult(
                target=target_dir,
                stopped_count=0,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
            )

        for proc in target_processes:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(
                    "[PROC][STOP][TERM_MISS] operation_id=%s target_id=%s pid=%s error=%s",
                    operation_id or "none",
                    target_dir,
                    getattr(proc, "pid", None),
                    e,
                )
            except Exception as e:
                logger.error(
                    "[PROC][STOP][TERM_ERROR] operation_id=%s target_id=%s pid=%s error=%s",
                    operation_id or "none",
                    target_dir,
                    getattr(proc, "pid", None),
                    e,
                )

        _, alive = psutil.wait_procs(target_processes, timeout=terminate_timeout_seconds)
        for proc in alive:
            try:
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(
                    "[PROC][STOP][KILL_MISS] operation_id=%s target_id=%s pid=%s error=%s",
                    operation_id or "none",
                    target_dir,
                    getattr(proc, "pid", None),
                    e,
                )
            except Exception as e:
                logger.error(
                    "[PROC][STOP][KILL_ERROR] operation_id=%s target_id=%s pid=%s error=%s",
                    operation_id or "none",
                    target_dir,
                    getattr(proc, "pid", None),
                    e,
                )

        still_alive: list[psutil.Process] = []
        if alive:
            _, still_alive = psutil.wait_procs(alive, timeout=kill_timeout_seconds)

        stopped_count = len(target_processes) - len(still_alive)
        if target_dir_path and stopped_count > 0:
            write_bot_state(target_dir_path, "MANUAL_STOP")
        self._invalidate_process_cache()

        result = BoundedStopResult(
            target=target_dir,
            stopped_count=stopped_count,
            still_alive_pids=tuple(getattr(proc, "pid", -1) for proc in still_alive),
            elapsed_ms=int((time.monotonic() - started_at) * 1000),
        )
        logger.info(
            "[PROC][STOP][DONE] operation_id=%s target_id=%s generation_id=old "
            "critical_path=true result=%s stopped=%d still_alive_pids=%s elapsed_ms=%d",
            operation_id or "none",
            target_dir,
            "blocked_alive" if result.still_alive_pids else "stopped",
            result.stopped_count,
            list(result.still_alive_pids),
            result.elapsed_ms,
        )
        return result

    async def stop_all_processes(self) -> int:
        """실행 중인 모든 프로세스 안전 종료 (terminate -> wait -> kill)"""
        self.monitor.suppress_decrease_alert()
        stopped_count = 0
        processes = self.monitor.find_processes()

        if not processes:
            logger.info("종료할 프로세스가 없습니다.")
            return 0

        # 종료 대상 디렉토리 경로 수집 (수동 종료 마커 기록용)
        dir_paths = [path.parent for _, path in processes]

        # 1. Terminate 시도
        for proc, path in processes:
            try:
                proc.terminate()
                logger.debug(f"프로세스 종료 시도 (PID: {proc.pid}, 디렉토리: {path.parent.name})")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"프로세스 종료 실패 (PID: {proc.pid}): {e}")

        # 2. Wait (최대 3초)
        proc_list = [p[0] for p in processes]
        _, alive = psutil.wait_procs(proc_list, timeout=3)

        # 3. Kill (남은 프로세스)
        for proc in alive:
            try:
                proc.kill()
                logger.warning(f"프로세스 강제 종료 (PID: {proc.pid})")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"프로세스 강제 종료 실패 (PID: {proc.pid}): {e}")

        # state 파일에 MANUAL_STOP 기록 (로테이션 시스템이 수동 종료로 인식하도록)
        for dir_path in dir_paths:
            write_bot_state(dir_path, "MANUAL_STOP")

        # 종료된 것으로 간주되는 프로세스 수 반환
        stopped_count = len(processes)
        logger.info(f"총 {stopped_count}개 프로세스 종료 완료")
        return stopped_count

    def kill_specific_process(self, target_dir: str) -> int:
        """특정 디렉토리의 프로세스를 안전하게 종료"""
        started_at = time.monotonic()
        self.monitor.suppress_decrease_alert()
        scan_started_at = time.monotonic()
        target_pairs = self._find_target_processes(target_dir, force_refresh=True)
        scan_elapsed = time.monotonic() - scan_started_at
        if scan_elapsed >= 1.0:
            logger.warning(
                "[PROC][KILL][SCAN_SLOW] target=%s elapsed=%.2fs",
                target_dir,
                scan_elapsed,
            )
        target_processes = [proc for proc, _ in target_pairs]
        target_dir_path = target_pairs[0][1].parent if target_pairs else None

        if not target_processes:
            logger.debug(f"종료할 프로세스가 없음: {target_dir}")
            return 0

        for proc in target_processes:
            try:
                proc.terminate()
                logger.debug(f"프로세스 종료 시도 (PID: {proc.pid}, 디렉토리: {target_dir})")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"프로세스 종료 실패 (PID: {proc.pid}): {e}")
            except Exception as e:
                logger.error(f"프로세스 종료 중 예상치 못한 오류 (PID: {proc.pid}): {e}")

        _, alive = psutil.wait_procs(
            target_processes,
            timeout=DEFAULT_PROCESS_TERMINATE_WAIT_SECONDS,
        )
        for proc in alive:
            try:
                proc.kill()
                logger.warning(f"프로세스 강제 종료 (PID: {proc.pid}, 디렉토리: {target_dir})")
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                logger.warning(f"프로세스 강제 종료 실패 (PID: {proc.pid}): {e}")
            except Exception as e:
                logger.error(f"프로세스 강제 종료 중 예상치 못한 오류 (PID: {proc.pid}): {e}")

        killed_count = len(target_processes)
        self._invalidate_process_cache()

        if alive:
            gone_after_kill, still_alive = psutil.wait_procs(
                alive,
                timeout=DEFAULT_PROCESS_TERMINATE_WAIT_SECONDS,
            )
            if still_alive:
                logger.warning(
                    "프로세스 강제 종료 확인 지연: %s pids=%s",
                    target_dir,
                    [proc.pid for proc in still_alive],
                )
        else:
            gone_after_kill = []

        for proc in target_processes:
            if proc in alive:
                continue
            logger.debug(f"프로세스 정상 종료 (PID: {proc.pid})")

        for proc in gone_after_kill:
            logger.debug(f"프로세스 강제 종료 확인 (PID: {proc.pid})")

        # state 파일에 MANUAL_STOP 기록 (로테이션 시스템이 수동 종료로 인식하도록)
        if target_dir_path and killed_count > 0:
            write_bot_state(target_dir_path, "MANUAL_STOP")

        total_elapsed = time.monotonic() - started_at
        if total_elapsed >= 1.0:
            logger.info(
                "[PROC][KILL][DONE] target=%s killed=%d elapsed=%.2fs",
                target_dir,
                killed_count,
                total_elapsed,
            )

        logger.debug(f"총 {killed_count}개 프로세스 종료 완료: {target_dir}")
        return killed_count

    async def run_shell_command(
        self, command: str, is_applescript: bool = False
    ) -> Tuple[int, str, str]:
        """
        쉘 명령어 비동기 실행 (platform_handler에 위임)

        Args:
            command: 실행할 명령어 (또는 AppleScript 코드)
            is_applescript: True일 경우 osascript -e로 실행 (macOS 전용)

        Returns:
            (returncode, stdout, stderr) 튜플
        """
        return await self.platform_handler.run_shell_command(command, is_applescript)
