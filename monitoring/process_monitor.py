"""
프로세스 감시 모듈

Phase 3.2 리팩토링: ProcessMonitor 클래스 분리
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import platform
import re
import sqlite3
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, cast
from uuid import uuid4

import psutil

from z_pulse.config import (
    EnvConfigHandler,
    load_ignored_dirs,
    get_trading_info_from_env,
    get_entry_count_generic,
    runtime_settings,
)
from z_pulse.constants import (
    TimeoutConfig,
    SizeConfig,
    CacheConfig,
    DurationConfig,
    ColorConfig,
    FileConfig,
)
from z_pulse.utils import escape_markdown, SingleValueCache
from z_pulse.utils.async_helpers import safe_send_message  # Phase 2.1: 메시지 전송 통합
from z_pulse.features.process_control import write_bot_state  # 봇 state 파일 작성
from z_pulse.integration.z_flow_bridge import ZFlowBridge
from z_pulse.monitoring.session_identity import (
    capture_external_bot_identity,
    resolve_terminal_window_id_for_identity,
)
from z_pulse.monitoring.session_store import (
    IdentityGenerationMismatchError,
    SessionRef,
    SessionStore,
)

logger = logging.getLogger(__name__)
_CLEANUP_REASON_PROCESS_EXIT = "process_exit_detected"


def _persist_exit_reservation_session(dir_path: Path, dir_name: str) -> None:
    now = datetime.now().astimezone().isoformat()
    store = SessionStore(dir_path)
    store.merge(
        SessionRef(
            session_id=dir_name,
            dir_name=dir_name,
            runtime_kind="unknown",
            platform="macos" if platform.system() == "Darwin" else platform.system().lower(),
            status="exited",
            source="process-monitor",
            custom_title=dir_name,
            data_dir=str(dir_path),
            last_exit_at=now,
            last_exit_reason="exit_reservation",
            last_state_signal="EXIT_RESERVATION",
            evidence={"normal_exit_logged": True, "exit_reservation": True},
        )
    )
    store.clear_runtime_identity()


def _persist_process_exit_session(dir_path: Path, dir_name: str) -> None:
    """비정상 종료(수동 kill 등) 시 session.json 신원을 null 처리한다."""
    try:
        now = datetime.now().astimezone().isoformat()
        store = SessionStore(dir_path)
        store.merge(
            SessionRef(
                session_id=dir_name,
                dir_name=dir_name,
                runtime_kind="unknown",
                platform="macos" if platform.system() == "Darwin" else platform.system().lower(),
                status="exited",
                source="process-monitor",
                custom_title=dir_name,
                data_dir=str(dir_path),
                last_exit_at=now,
                last_exit_reason=_CLEANUP_REASON_PROCESS_EXIT,
                last_state_signal="PROCESS_EXIT",
                evidence={
                    "process_exit_detected": True,
                    # 이전 정상 종료 evidence를 명시적으로 무효화 (이월 방지)
                    "normal_exit_logged": False,
                    "exit_reservation": False,
                },
            )
        )
        store.clear_runtime_identity()
        logger.info("[MONITOR][PROCESS_EXIT_CLEANUP] target=%s", dir_name)
    except Exception:
        logger.exception("[MONITOR][PROCESS_EXIT_CLEANUP_ERROR] target=%s", dir_name)


def process_uptime(proc: psutil.Process | None) -> timedelta | None:
    """프로세스 가동 시간(uptime)을 반환한다.

    Args:
        proc: psutil.Process 인스턴스 또는 None

    Returns:
        프로세스 생성 시각 기준 경과 시간(timedelta), 또는 None(proc 없음·psutil 오류).
    """
    if proc is None:
        return None
    try:
        return datetime.now() - datetime.fromtimestamp(proc.create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return None


def _normalize_runtime_cmdline(cmdline: list[str]) -> tuple[list[str], str]:
    cmd_parts = [part.replace("\\", "/").lower() for part in cmdline if part]
    return cmd_parts, " ".join(cmd_parts)


def _is_z_flow_runtime_cmdline(cmdline: list[str]) -> bool:
    return ZFlowBridge.matches_runtime_cmdline(cmdline)


def _matches_z_flow_runtime_cmdline(monitor: object, cmdline: list[str]) -> bool:
    bridge = getattr(monitor, "z_flow_bridge", None)
    matcher = getattr(bridge, "is_runtime_cmdline", None)
    if callable(matcher):
        return bool(matcher(cmdline))
    return ZFlowBridge.matches_runtime_cmdline(cmdline)


def _cmdline_has_data_dir(cmdline: list[str], dir_name: str) -> bool:
    _, cmd_str = _normalize_runtime_cmdline(cmdline)
    return dir_name.lower() in cmd_str


def _matches_runtime_target(monitor: object, cmdline: list[str], target: str | Path) -> bool:
    bridge = getattr(monitor, "z_flow_bridge", None)
    matcher = getattr(bridge, "matches_runtime_target", None)
    if callable(matcher):
        return bool(matcher(cmdline, target))
    return ZFlowBridge.matches_runtime_target(cmdline, target)


# PIL 임포트 (선택적)
try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except ImportError:
    Image = cast(Any, None)
    ImageDraw = cast(Any, None)
    ImageFont = cast(Any, None)
    PIL_AVAILABLE = False

# ============================================================================
# Phase 2.1: 중복 함수 제거 및 utils.async_helpers 통합
# ============================================================================
# 기존 구현 (주석 처리 - 롤백 대비):
# async def send_telegram_message(token, chat_id, message):
#     """
#     텔레그램으로 메시지를 비동기적으로 보냅니다. (MarkdownV2 적용)
#     """
#     try:
#         from telegram import Bot
#         bot = Bot(token=token)
#         await bot.send_message(chat_id=chat_id, text=message, parse_mode='MarkdownV2')
#         logger.info(f"텔레그램 메시지 발송 성공 (수신자: {chat_id})")
#     except Exception as e:
#         logger.error(f"텔레그램 메시지 발송 실패: {e}")
#
#
# async def send_telegram_photo(token, chat_id, photo_path, caption):
#     """
#     텔레그램으로 사진을 비동기적으로 보냅니다. (MarkdownV2 적용)
#     """
#     try:
#         from telegram import Bot
#         bot = Bot(token=token)
#         with open(photo_path, 'rb') as photo:
#             await bot.send_photo(chat_id=chat_id, photo=photo, caption=caption, parse_mode='MarkdownV2')
#         logger.info(f"텔레그램 사진 발송 성공 (수신자: {chat_id})")
#     except Exception as e:
#         logger.error(f"텔레그램 사진 발송 실패: {e}")


# 새로운 구현 (safe_send_message 사용):
async def send_telegram_message(token, chat_id, message):
    """
    텔레그램으로 메시지를 비동기적으로 보냅니다. (MarkdownV2 적용)

    Phase 2.1: safe_send_message 래퍼로 변경
    """
    from telegram import Bot

    bot = Bot(token=token)
    success = await safe_send_message(bot, chat_id, message, parse_mode="MarkdownV2")
    if success:
        logger.debug(f"[ALERT][SEND] telegram message delivered chat_id={chat_id}")
    else:
        logger.warning(f"[ALERT][SEND] 텔레그램 알림 발송 실패 chat_id={chat_id}")


async def send_telegram_photo(token, chat_id, photo_path, caption):
    """
    텔레그램으로 사진을 비동기적으로 보냅니다. (MarkdownV2 적용)

    Phase 2.1: 에러 처리 통일 (safe_send_message 패턴 적용)
    """
    try:
        from telegram import Bot

        bot = Bot(token=token)
        with open(photo_path, "rb") as photo:
            await bot.send_photo(
                chat_id=chat_id, photo=photo, caption=caption, parse_mode="MarkdownV2"
            )
        logger.debug(f"[ALERT][SEND] telegram photo delivered chat_id={chat_id}")
    except Exception as e:
        logger.error(f"텔레그램 사진 발송 실패: {e}")


# ============================================================================


class ProcessMonitor:
    def __init__(self, target_dir: str, process_name: str, check_interval: int = 60):
        """
        프로세스 감시 클래스

        Args:
            target_dir: 감시할 프로그램이 있는 기본 디렉토리
            process_name: 감시할 프로그램 이름
            check_interval: 체크 주기 (초)
        """
        self.target_dir = Path(target_dir)
        self.process_name = process_name
        self.check_interval = check_interval
        self.last_seen = None
        self.is_running = False
        self.ignore_list = load_ignored_dirs()
        self.escape_states: dict[str, bool] = {}

        # 터미널 텍스트 추적용
        self.terminal_states = {}

        # 프로세스 개수 추적용
        self.last_process_count = -1
        self.previous_process_count = -1
        self.count_change_time = None
        self.last_count_alert_time = None
        self.last_alerted_process_count = None  # 마지막으로 알림 보낸 감소값
        self._suppress_decrease_alert_until = (
            None  # 의도적 종료 시 알림 억제 타임스탬프
        )
        self._previous_running_dirs: dict = {}  # {dir_name: dir_path} 이전 실행 중 프로세스 추적
        self._previous_z_flow_running_dirs: dict = {}  # {dir_name: dir_path} 이전 실행 중 Z-Flow 런타임 추적
        self._disappeared_dirs: list = []  # [(dir_name, dir_path)] 최근 사라진 프로세스
        self._last_cleanup_request_ids: dict[str, str] = {}
        self._cleanup_handled_until: dict[str, datetime] = {}
        self._start_arrange_handled_until: dict[str, datetime] = {}
        self._running_since: dict = {}  # {dir_name: datetime} 실행 시작 시각 (stale-log 오탐 방지)
        self._window_retry_state: dict[str, tuple[int, datetime]] = {}  # {dir_name: (attempt, last_tried)}
        self._arranged_this_cycle = False

        # [최적화] 프로세스 목록 캐싱 - SingleValueCache 사용
        self._process_cache = SingleValueCache[list](ttl=CacheConfig.CACHE_TTL_SECONDS)

        # [GIL 난타 방지] psutil.process_iter 동시 실행 직렬화 락
        self._psutil_scan_lock = threading.Lock()

        # stale-while-revalidate: lock busy 시 반환할 직전 스캔 결과
        self._last_known_processes: list | None = None

        # 텔레그램 설정
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not self.telegram_bot_token or not self.telegram_chat_id:
            logger.warning(
                "TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않았습니다."
            )
            self.telegram_bot_token = None

        # [최적화] asyncio 메인 루프 참조 (나중에 설정됨)
        self.main_loop = None

        # 프로세스 개수 감소 시 대시보드 새로고침 콜백
        self._on_process_decrease_callback = None

        # 프로세스 개수 감소 시 창 정렬용 WindowManager 참조
        self._window_manager = None

        # [macOS] 프로세스 자동 종료 시 터미널 창 정리 콜백
        self._cleanup_terminal_callback = None
        self._cleanup_orchestrator = None
        self.z_flow_bridge = None

        # 모든 대상 프로그램 경로 찾기
        self.all_program_paths = []
        self.target_paths = []
        self.target_paths_set = set()  # [최적화] 빠른 검색용 Set
        self.find_target_programs()

        logger.info(f"감시 대상 디렉토리: {self.target_dir}")
        logger.info(f"감시할 프로그램: {self.process_name}")
        logger.info(f"무시할 디렉토리: {self.ignore_list}")
        logger.info(f"발견된 프로그램 수: {len(self.target_paths)}")
        for path in self.target_paths:
            logger.info(f"  - {path}")
        logger.info(f"체크 주기: {self.check_interval}초")

    def set_escape_mode_for_dir(self, dir_name: str, enabled: bool) -> None:
        """디렉토리별 이스케이프 모드 설정"""
        self.escape_states[dir_name] = enabled

    def get_escape_mode_status_for_dir(self, dir_name: str) -> bool:
        """디렉토리별 이스케이프 모드 상태 반환"""
        return self.escape_states.get(dir_name, False)

    def set_loop(self, loop):
        """
        [최적화] asyncio 메인 루프를 설정합니다.
        비동기 메시지 전송 성능을 향상시킵니다.

        Args:
            loop: asyncio 이벤트 루프
        """
        self.main_loop = loop
        logger.info("ProcessMonitor에 asyncio 메인 루프 설정 완료")

    def set_process_decrease_callback(self, callback):
        """
        프로세스 개수 감소 시 호출될 콜백을 설정합니다.
        대시보드 새로고침 등에 사용됩니다.

        Args:
            callback: async 콜백 함수
        """
        self._on_process_decrease_callback = callback
        logger.info("ProcessMonitor에 프로세스 감소 콜백 설정 완료")

    def set_window_manager(self, window_manager):
        """
        프로세스 개수 감소 시 창 정렬을 위한 WindowManager를 설정합니다.

        Args:
            window_manager: WindowManager 인스턴스
        """
        self._window_manager = window_manager
        logger.info("ProcessMonitor에 WindowManager 설정 완료")

    def set_cleanup_terminal_callback(self, callback):
        """
        macOS에서 프로세스 자동 종료 시 터미널 창을 정리하기 위한 콜백을 설정합니다.

        Args:
            callback: async def cleanup_terminal(filter_keyword: str) 함수
        """
        self._cleanup_terminal_callback = callback
        logger.info("ProcessMonitor에 터미널 정리 콜백 설정 완료")

    def _refresh_z_flow_runtime_dirs(self):
        bridge = getattr(self, "z_flow_bridge", None)
        locator = getattr(bridge, "get_runtime_pid_files", None)
        if callable(locator):
            runtime_pid_files = locator(self.target_dir, self.ignore_list)
            self.z_flow_dirs = (
                runtime_pid_files if isinstance(runtime_pid_files, dict) else {}
            )
            return

        self.z_flow_dirs = {}

    def set_z_flow_bridge(self, bridge):
        """Z-Flow 런타임 식별용 bridge를 설정합니다."""
        self.z_flow_bridge = bridge
        self._refresh_z_flow_runtime_dirs()
        logger.info("ProcessMonitor에 ZFlowBridge 설정 완료")

    def set_cleanup_orchestrator(self, orchestrator):
        """
        종료 cleanup orchestration 진입점을 설정합니다.

        Args:
            orchestrator: async def request_cleanup(dir_name: str, reason: str) 제공 객체
        """
        self._cleanup_orchestrator = orchestrator
        logger.info("ProcessMonitor에 cleanup orchestrator 설정 완료")

    def mark_cleanup_handled(self, dir_name: str, ttl_seconds: float = 20.0) -> None:
        """이미 명시 cleanup이 처리한 종료를 monitor 자동 cleanup에서 재처리하지 않도록 표시."""
        self._cleanup_handled_until[dir_name] = datetime.now() + timedelta(seconds=ttl_seconds)

    def mark_start_arrange_handled(self, dir_name: str, ttl_seconds: float = 20.0) -> None:
        """시작 경로에서 이미 창 정렬을 예약한 경우 lifecycle 시작 정렬 중복을 막기 위한 표시."""
        self._start_arrange_handled_until[dir_name] = datetime.now() + timedelta(seconds=ttl_seconds)

    def _consume_recent_marker(self, markers: dict[str, datetime], dir_name: str) -> bool:
        expires_at = markers.get(dir_name)
        if expires_at is None:
            return False
        if datetime.now() <= expires_at:
            markers.pop(dir_name, None)
            return True
        markers.pop(dir_name, None)
        return False

    async def _request_cleanup_for_exited_dir(self, dir_name: str, request_id: str) -> None:
        """자동 종료 cleanup은 orchestrator 우선, 실패 시 terminal cleanup으로 fallback"""
        if self._cleanup_orchestrator is not None:
            try:
                cleaned = await self._cleanup_orchestrator.request_cleanup(
                    dir_name,
                    reason=_CLEANUP_REASON_PROCESS_EXIT,
                    request_id=request_id,
                )
                if isinstance(cleaned, dict):
                    if cleaned.get("ok"):
                        return
                elif cleaned:
                    return
                logger.warning(
                    f"[PROC][CLEANUP][FALLBACK] dir={dir_name} request_id={request_id} reason=orchestrator_returned_false"
                )
            except Exception as exc:
                logger.warning(
                    f"[PROC][CLEANUP][FALLBACK] dir={dir_name} request_id={request_id} reason=orchestrator_error error={exc}"
                )

        if self._cleanup_terminal_callback is not None:
            logger.info(
                f"[PROC][CLEANUP][FALLBACK] dir={dir_name} request_id={request_id} action=cleanup_terminal"
            )
            await self._cleanup_terminal_callback(dir_name)
            return

        logger.warning(
            f"[PROC][CLEANUP][SKIP] dir={dir_name} request_id={request_id} reason=no_cleanup_handler"
        )

    def _schedule_cleanup_for_exited_dirs(self, dir_names: list[str]) -> None:
        if not dir_names:
            return

        skipped_dirs = [
            dir_name
            for dir_name in dir_names
            if self._consume_recent_marker(self._cleanup_handled_until, dir_name)
        ]
        if skipped_dirs:
            logger.info(
                f"[PROC][CLEANUP][SKIP] bots={skipped_dirs} reason=already_handled"
            )

        dir_names = [dir_name for dir_name in dir_names if dir_name not in skipped_dirs]
        if not dir_names:
            return

        request_ids: dict[str, str] = {}
        for dir_name in dir_names:
            request_id = f"proc-exit:{dir_name}:{uuid4().hex[:8]}"
            request_ids[dir_name] = request_id
            if self._cleanup_orchestrator or self._cleanup_terminal_callback:
                cleanup_path = (
                    "orchestrator"
                    if self._cleanup_orchestrator is not None
                    else "terminal_only"
                )
                logger.info(
                    f"[PROC][CLEANUP][SCHEDULE] dir={dir_name} request_id={request_id} reason={_CLEANUP_REASON_PROCESS_EXIT} path={cleanup_path}"
                )
                if self.main_loop and self.main_loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._request_cleanup_for_exited_dir(dir_name, request_id),
                        self.main_loop,
                    )
                else:
                    asyncio.run(self._request_cleanup_for_exited_dir(dir_name, request_id))
            else:
                logger.warning(
                    f"[PROC][CLEANUP][SKIP] dir={dir_name} request_id={request_id} reason=no_cleanup_handler"
                )

        self._last_cleanup_request_ids = request_ids
        logger.info(f"[PROC][CLEANUP][ARRANGE] bots={dir_names} request_ids={request_ids}")

        if self._window_manager and not self._arranged_this_cycle:
            self._window_manager.trigger_auto_arrange()
            self._arranged_this_cycle = True

    def suppress_decrease_alert(self, duration_seconds: int = 30):
        """
        의도적 프로세스 종료 시 감소 알림을 일시 억제합니다.
        ProcessController에서 kill/stop 전에 호출합니다.

        Args:
            duration_seconds: 억제 지속 시간 (초)
        """
        self._suppress_decrease_alert_until = datetime.now() + timedelta(
            seconds=duration_seconds
        )
        logger.debug(f"프로세스 감소 알림 {duration_seconds}초간 억제")

    def find_target_programs(self):
        """
        target_dir 하위 디렉토리에서 모든 잠재적 프로그램을 찾고,
        그 중 모니터링 대상만 별도로 저장합니다.

        Z-Flow 런타임(SLOT-* 디렉토리의 z_flow.pid 파일)도 함께 검색합니다.
        """
        try:
            # glob 패턴으로 모든 잠재적 프로그램 찾기
            pattern = self.target_dir / "*" / self.process_name
            all_paths = [Path(p) for p in glob.glob(str(pattern)) if Path(p).is_file()]

            # [추가] 언더바(_)로 시작하는 디렉토리는 목록에서 제외
            all_paths = [p for p in all_paths if not p.parent.name.startswith("_")]

            self.all_program_paths = all_paths  # 모든 경로를 여기에 저장

            # 그 중에서 무시 목록에 없는 것만 실제 감시 대상(target_paths)으로 필터링
            self.target_paths = [
                p for p in all_paths if p.parent.name not in self.ignore_list
            ]
            # [최적화] Path 객체 Set 생성
            self.target_paths_set = set(self.target_paths)

        except Exception as e:
            logger.error(f"대상 프로그램 찾기 실패: {e}")
            self.all_program_paths = []
            self.target_paths = []
            self.target_paths_set = set()

        # Z-Flow 런타임 디렉토리 검색
        try:
            self._refresh_z_flow_runtime_dirs()
            if self.z_flow_dirs:
                logger.info(f"Z-Flow 디렉토리 발견: {list(self.z_flow_dirs.keys())}")
        except Exception as e:
            logger.error(f"Z-Flow 디렉토리 검색 실패: {e}")
            self.z_flow_dirs = {}

    def find_z_flow_processes(self) -> list[tuple]:
        """
        활성 Z-Flow 런타임 프로세스 목록 반환.

        Returns:
            [(psutil.Process | None, dir_name, data_dir_path), ...]

        PID 파일이 존재하고 프로세스가 살아있으면 Process 객체,
        PID 파일이 없거나 프로세스가 죽었으면 None.
        """
        result = []
        for dir_name, pid_file in getattr(self, "z_flow_dirs", {} ).items():
            try:
                if not pid_file.exists():
                    result.append((None, dir_name, pid_file.parent))
                    continue
                pid = int(pid_file.read_text(encoding="utf-8").strip())
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    try:
                        cmdline = proc.cmdline()
                        _, cmd_str = _normalize_runtime_cmdline(cmdline)
                        is_python = any(
                            "python" in (proc.name() or "").lower() for _ in [0]
                        )
                        is_z_flow = _matches_z_flow_runtime_cmdline(self, cmdline)
                        data_dir_match = _matches_runtime_target(self, cmdline, pid_file.parent)

                        if is_python and is_z_flow and data_dir_match:
                            result.append((proc, dir_name, pid_file.parent))
                        else:
                            logger.debug(
                                f"[Z_FLOW][PID_VERIFY] PID {pid} exists but not a valid runtime: "
                                f"python={is_python}, z_flow={is_z_flow}, data_dir_match={data_dir_match}, "
                                f"cmdline={cmd_str[:100]}"
                            )
                            result.append((None, dir_name, pid_file.parent))
                    except (
                        psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess,
                    ):
                        result.append((None, dir_name, pid_file.parent))
                else:
                    result.append((None, dir_name, pid_file.parent))
            except Exception as e:
                logger.debug(f"Z-Flow 프로세스 조회 실패 ({dir_name}): {e}")
                result.append((None, dir_name, pid_file.parent))
        return result

    def find_all_z_flow_os_processes(self, target: str | Path) -> list:
        """
        OS 프로세스 테이블에서 특정 Z-Flow 런타임의 모든 인스턴스를 검색.

        PID 파일에 의존하지 않고 psutil cmdline 검사로 모든 z_flow/run_bot.py
        프로세스를 찾아 반환합니다. 스테일/고아 프로세스 정리에 사용됩니다.

        Args:
            target: 슬롯 디렉토리 이름 또는 정확한 data_dir 경로

        Returns:
            해당 슬롯의 모든 psutil.Process 객체 리스트
        """
        results: list[psutil.Process] = []
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = proc.info.get("name", "")
                if "python" not in (name or "").lower():
                    continue
                cmdline = proc.cmdline()
                if not _matches_z_flow_runtime_cmdline(self, cmdline):
                    continue
                if _matches_runtime_target(self, cmdline, target):
                    results.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return results

    def invalidate_cache(self):
        """프로세스 캐시 강제 초기화"""
        self._process_cache.invalidate()

    def find_processes_by_dir(self, dir_name: str, force_refresh: bool = False):
        """
        특정 봇 디렉토리의 프로세스만 빠르게 조회한다.

        전체 process_iter에서 exe를 매번 수집하면 macOS에서 수 초 이상 멈출 수 있어,
        캐시가 없을 때도 실행파일 이름이 맞는 프로세스에만 exe/cmdline을 조회한다.
        """
        if not force_refresh:
            cached = self._process_cache.get()
            if cached is not None:
                return [
                    (proc, path)
                    for proc, path in cached
                    if path.parent.name == dir_name
                ]

        target_paths = [
            path
            for path in self.all_program_paths
            if path.parent.name == dir_name
            and path.parent.name not in self.ignore_list
        ]
        if not target_paths:
            return []

        target_path_set = set(target_paths)
        target_dir_set = {path.parent for path in target_paths}
        results = []
        seen_pids = set()

        with self._psutil_scan_lock:
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pid = proc.info.get("pid")
                    if pid is None:
                        continue
                    if pid in seen_pids:
                        continue
                    if proc.info.get("name") != self.process_name:
                        continue

                    matched_path = None
                    # name 필터 통과한 후보에만 exe lazy 조회
                    try:
                        exe_path: str | None = proc.exe()
                    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                        exe_path = None

                    if exe_path:
                        proc_path = Path(exe_path)
                        if proc_path in target_path_set:
                            matched_path = proc_path

                    if matched_path is None:
                        try:
                            cmdline = proc.cmdline()
                        except (
                            psutil.NoSuchProcess,
                            psutil.AccessDenied,
                            psutil.ZombieProcess,
                        ):
                            cmdline = []
                        cmd_str = " ".join(str(part) for part in cmdline)
                        for target_path in target_paths:
                            if (
                                str(target_path) in cmd_str
                                or str(target_path.parent) in cmd_str
                            ):
                                matched_path = target_path
                                break

                    if matched_path is None and exe_path:
                        proc_path = Path(exe_path)
                        if proc_path.parent in target_dir_set:
                            matched_path = proc_path

                    if matched_path is not None:
                        results.append((proc, matched_path))
                        seen_pids.add(pid)
                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue

        return results

    def find_processes(self, force_refresh=False):
        """
        대상 프로세스들을 모두 찾아서 (프로세스, 경로) 튜플의 리스트로 반환
        [최적화] SingleValueCache를 통한 캐싱 적용
        """
        # 캐시에서 가져오기 시도
        if not force_refresh:
            cached = self._process_cache.get()
            if cached is not None:
                return cached.copy()
            # 캐시 만료: lock 비경쟁 시도 (stale-while-revalidate)
            if not self._psutil_scan_lock.acquire(blocking=False):
                # lock busy (다른 스캔 진행 중) → stale 즉시 반환 (GIL 대기 없음)
                if self._last_known_processes is not None:
                    return list(self._last_known_processes)
                return []
        else:
            self._psutil_scan_lock.acquire(blocking=True)

        processes = []
        # [Phase 3.1 최적화] 중복 체크용 PID Set (O(n²) → O(n))
        seen_pids = set()

        try:
            # [최적화] exe lazy 수집:
            # process_iter에서 "exe" 제거 → name pre-filter 후 매칭 후보에만
            # proc.exe() lazy 조회 (dashboard cold-cache 전체 스캔 35s → <1s)
            # [GIL 난타 방지] 동시 스캔을 직렬화하여 GIL 쟁탈 해소
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    proc_name = proc.info.get("name")
                    pid = proc.info.get("pid")

                    if pid is None:
                        continue

                    # name pre-filter: 불일치 프로세스는 즉시 건너뜀
                    if proc_name != self.process_name:
                        continue

                    # name 매칭 후보에 대해서만 lazy exe 조회
                    try:
                        exe_path: str | None = proc.exe()
                    except (
                        psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess,
                    ):
                        exe_path = None

                    # 실행파일 경로로 비교 (Set 활용 최적화)
                    if exe_path:
                        proc_path = Path(exe_path)
                        if proc_path.parent.name in self.ignore_list:
                            continue

                        # [최적화] Set을 사용한 O(1) 검색
                        if proc_path in self.target_paths_set:
                            # [Phase 3.1] O(1) 중복 체크
                            if pid not in seen_pids:
                                processes.append((proc, proc_path))
                                seen_pids.add(pid)
                            continue  # exe 매칭 — cmdline fallback 불필요

                    # exe 없거나 target_paths_set 미매칭 → cmdline fallback
                    try:
                        cmdline = proc.cmdline()
                        if cmdline:
                            cmd_str = str(cmdline)
                            for target_path in self.target_paths:
                                if target_path.parent.name in self.ignore_list:
                                    continue

                                if str(target_path) in cmd_str:
                                    # [Phase 3.1] O(n) any() → O(1) Set 검색
                                    if pid not in seen_pids:
                                        processes.append((proc, target_path))
                                        seen_pids.add(pid)
                                        break
                    except (
                        psutil.NoSuchProcess,
                        psutil.AccessDenied,
                        psutil.ZombieProcess,
                    ):
                        continue

                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue

            # 캐시 업데이트 + stale 보존
            self._process_cache.set(processes)
            self._last_known_processes = processes

            return processes
        except Exception as e:
            logger.error(f"프로세스 검색 중 오류: {e}")
            return []
        finally:
            self._psutil_scan_lock.release()

    def get_process_info(self, proc):
        """
        프로세스 정보 반환 (oneshot 최적화)
        """
        try:
            # [최적화] oneshot 컨텍스트 매니저 사용하여 시스템 콜 최소화
            with proc.oneshot():
                # 프로세스가 실제로 살아있는지 다시 한번 확인
                if not proc.is_running():
                    return None

                # 좀비 프로세스인지 확인
                status = proc.status()
                if status == psutil.STATUS_ZOMBIE:
                    return None

                return {
                    "pid": proc.pid,
                    "name": proc.name(),
                    "status": status,
                    "cpu_percent": proc.cpu_percent(),
                    "memory_percent": proc.memory_percent(),
                    "create_time": datetime.fromtimestamp(proc.create_time()),
                    "num_threads": proc.num_threads(),
                }
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return None
        except Exception as e:
            logger.error(f"프로세스 정보 가져오기 실패: {e}")
            return None

    def get_terminal_content(
        self, proc_tty: str, dir_path: Optional[Path] = None
    ) -> Optional[str]:
        """
        터미널 내용을 가져옵니다.
        macOS: 실행 중인 터미널 창의 TTY 스크래핑
        Windows: 해당 디렉토리의 monitor.log 파일 읽기 (Tail)
        """
        if platform.system() == "Windows":
            # [최적화] Windows는 로그 파일의 마지막 40줄을 읽어 터미널 화면처럼 사용
            if not dir_path:
                return None
            log_file = dir_path / FileConfig.MONITOR_LOG
            if not log_file.exists():
                return None
            try:
                # [최적화] deque를 사용하여 파일 전체를 읽지 않고 마지막 40줄만 효율적으로 가져옴
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    lines = list(deque(f, SizeConfig.LOG_HISTORY_LINES))
                    return "".join(lines)
            except Exception:
                return None

        else:
            # macOS Existing Logic
            if not proc_tty:
                return None
            script = f'''
            tell application "Terminal"
                if not it is running then return "ERROR: Terminal is not running."

                set window_list to windows
                repeat with w in window_list
                    set tab_list to tabs of w
                    repeat with t in tab_list
                        try
                            if tty of t is "{proc_tty}" then
                                set the_history to history of t
                                set para_count to count of paragraphs of the_history
                                set start_para to para_count - {SizeConfig.LOG_HISTORY_LINES}
                                if start_para < 1 then set start_para to 1
                                set last_lines to (paragraphs start_para through -1 of the_history) as text
                                return last_lines
                            end if
                        end try
                    end repeat
                end repeat

                return "ERROR: Matching TTY not found."
            end tell
            '''
            try:
                result = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=TimeoutConfig.APPLESCRIPT,
                    check=False,
                )

                if result.returncode != 0 or result.stdout.strip().startswith("ERROR:"):
                    logger.warning(
                        f"터미널 내용 가져오기 실패 (TTY: {proc_tty}): {result.stderr or result.stdout}"
                    )
                    return None

                logger.debug(
                    f"TTY ({proc_tty})에 해당하는 터미널 내용 발견, 길이: {len(result.stdout.strip())}"
                )
                return result.stdout.strip()

            except subprocess.TimeoutExpired:
                logger.error(f"AppleScript 실행 시간 초과 (TTY: {proc_tty})")
                return None
            except Exception as e:
                logger.error(f"AppleScript 실행 오류 (TTY: {proc_tty}): {e}")
                return None

    def check_terminal_stalls(self):
        """
        [수정] monitor.log 파일의 수정 시간을 감지하여 정체 상태를 확인하고,
        정체 시 이미지가 아닌 로그 파일(마지막 100줄)을 전송합니다.
        """
        if not self.telegram_bot_token:
            return

        process_tuples = self.find_processes()
        running_dirs = {p[1].parent.name for p in process_tuples}

        # 실행 중이 아닌 프로세스의 터미널 상태 정리
        for dir_name in list(self.terminal_states.keys()):
            if dir_name not in running_dirs:
                logger.debug(
                    f"프로세스가 종료되어 터미널({dir_name}) 감시를 중단합니다."
                )
                del self.terminal_states[dir_name]

        # 실행 중인 각 프로세스 그룹 확인
        for dir_name in running_dirs:
            if dir_name in self.ignore_list:
                continue

            dir_path = self.target_dir / dir_name
            log_file = dir_path / FileConfig.MONITOR_LOG
            current_time = datetime.now()

            if not log_file.exists():
                continue

            try:
                # 파일 수정 시간 확인
                last_modified_time = datetime.fromtimestamp(log_file.stat().st_mtime)
            except FileNotFoundError:
                continue

            if dir_name not in self.terminal_states:
                self.terminal_states[dir_name] = {
                    "last_change_time": last_modified_time,
                    "stall_start_time": None,
                    "last_error_sent_time": None,
                }
                logger.debug(f"로그 파일({dir_name}) 감시 시작.")
                continue

            # 재기동 직후 유예시간: 오래된 monitor.log 기준 오탐 방지
            running_since = self._running_since.get(dir_name)
            if running_since and (current_time - running_since) < timedelta(
                minutes=DurationConfig.LOG_STALL_GRACE_MINUTES
            ):
                continue

            state = self.terminal_states[dir_name]

            # 수정 시간이 변경되었으면 상태 업데이트
            if last_modified_time > state["last_change_time"]:
                state["last_change_time"] = last_modified_time
                state["stall_start_time"] = None  # 정체 상태 초기화
                state["last_error_sent_time"] = None  # 알림 상태 초기화
            else:
                # 수정 시간이 10분 이상 변경되지 않았을 경우
                if current_time - state["last_change_time"] > timedelta(
                    minutes=DurationConfig.LOG_STALL_MINUTES
                ):
                    if state.get("last_error_sent_time") and (
                        current_time - state["last_error_sent_time"]
                    ) < timedelta(minutes=DurationConfig.LOG_STALL_MINUTES):
                        # 최근 10분 내에 알림을 보냈으면 건너뜀
                        continue

                    logger.warning(
                        f"로그 파일({dir_name})에 {DurationConfig.LOG_STALL_MINUTES}분 이상 변경이 없어 알림을 보냅니다."
                    )

                    # 로그 파일 마지막 100줄 읽기
                    try:
                        with open(
                            log_file, "r", encoding="utf-8", errors="ignore"
                        ) as f:
                            log_content = "".join(deque(f, SizeConfig.LOG_TAIL_LINES))
                    except Exception:
                        log_content = "(로그 파일을 읽을 수 없습니다.)"

                    # 텔레그램으로 로그 내용 전송 (MarkdownV2 적용)
                    escaped_dir_name = escape_markdown(dir_name)
                    last_change_time_str = escape_markdown(
                        state["last_change_time"].strftime("%Y-%m-%d %H:%M:%S")
                    )

                    message = (
                        f"🚨 *프로세스 응답 없음 감지* 🚨\n\n"
                        f"📁 *디렉토리*: `{escaped_dir_name}`\n"
                        f"⏰ *마지막 로그 시간*: {last_change_time_str}\n"
                        f"⏱️ {DurationConfig.LOG_STALL_MINUTES}분 이상 로그 업데이트가 없습니다\\. 확인이 필요합니다\\.\n\n"
                        f"📄 *최신 로그 \\(최대 {SizeConfig.LOG_TAIL_LINES}줄\\)*:\n"
                        f"```\n{log_content.strip()}\n```"
                    )

                    # 메시지 길이 제한 처리
                    if len(message) > SizeConfig.MESSAGE_MAX_LENGTH:
                        message = (
                            message[: SizeConfig.MESSAGE_SAFE_LENGTH]
                            + "\n... (내용이 너무 길어 잘림)```"
                        )

                    # [최적화] 비동기 함수 호출 - 메인 루프가 있으면 threadsafe 방식 사용
                    if self.main_loop and self.main_loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            send_telegram_message(
                                self.telegram_bot_token, self.telegram_chat_id, message
                            ),
                            self.main_loop,
                        )
                    else:
                        # 초기화 중이거나 루프가 없는 경우 fallback
                        asyncio.run(
                            send_telegram_message(
                                self.telegram_bot_token, self.telegram_chat_id, message
                            )
                        )

                    state["last_error_sent_time"] = current_time

    # [개선] 폰트 로딩 로직을 분리하고 캐싱하여 성능 향상
    def _load_font(self, size: int):
        if hasattr(self, "_cached_font"):
            return self._cached_font

        # [최적화] Windows 폰트 경로 추가
        font_paths = []
        system = platform.system()

        if system == "Darwin":  # macOS
            font_paths = [
                "/System/Library/Fonts/AppleSDGothicNeo.ttc",
                "/System/Library/Fonts/Helvetica.ttc",
                "/System/Library/Fonts/Menlo.ttc",
            ]
        elif system == "Windows":  # Windows
            font_paths = [
                "C:/Windows/Fonts/malgun.ttf",  # 맑은 고딕
                "C:/Windows/Fonts/gulim.ttc",  # 굴림
                "C:/Windows/Fonts/arial.ttf",  # Arial
                "C:/Windows/Fonts/seguiemj.ttf",  # Segoe UI Emoji
            ]
        else:  # Linux
            font_paths = [
                "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]

        for path in font_paths:
            try:
                # index=0은 ttc 파일 등을 위해 필요
                font = ImageFont.truetype(path, size, index=0)
                # 한글 테스트
                font.getbbox("한글")
                self._cached_font = font
                return font
            except Exception:
                continue

        # 폰트 로드 실패 시 기본 폰트
        self._cached_font = ImageFont.load_default()
        return self._cached_font

    def create_text_image(self, text, output_path):
        if not PIL_AVAILABLE:
            return None

        try:
            font_size = 16
            # [최적화] 캐시된 폰트를 로드하고, 불필요한 재탐색 로직 제거
            font = self._load_font(font_size)

            line_height = 22
            padding = 25
            bg_color = ColorConfig.BG_DARK
            text_color = ColorConfig.TEXT_BRIGHT

            # 텍스트 줄 분리 및 길이 제한
            lines = text.split("\n")
            max_line_width = 0

            for i, line in enumerate(lines):
                if len(line) > SizeConfig.TEXT_LINE_MAX_LENGTH:
                    lines[i] = line[: SizeConfig.TEXT_LINE_MAX_LENGTH] + "..."
                    line = lines[i]

                try:
                    line_width = font.getbbox(line)[2]
                except Exception:
                    line_width = len(line) * (
                        font_size * ColorConfig.FONT_SIZE_MULTIPLIER
                    )

                max_line_width = max(max_line_width, line_width)

            # 이미지 크기 계산
            image_width = int(
                max(
                    SizeConfig.IMAGE_MIN_WIDTH,
                    min(max_line_width + (padding * 2), SizeConfig.IMAGE_MAX_WIDTH),
                )
            )
            image_height = int((len(lines) * line_height) + (padding * 2))

            # 이미지 생성
            image = Image.new("RGB", (image_width, image_height), bg_color)
            draw = ImageDraw.Draw(image)

            # 텍스트 그리기
            y_position = padding
            for line in lines:
                draw.text((padding, y_position), line, fill=text_color, font=font)
                y_position += line_height

            # 이미지 저장
            image.save(output_path)
            return output_path

        except Exception as e:
            logger.error(f"텍스트 이미지 생성 실패: {e}")
            return None

    def check_process_count_alerts(self):
        if not self.telegram_bot_token:
            return
        # 억제 시간 경과 시 초기화
        if (
            self._suppress_decrease_alert_until
            and datetime.now() >= self._suppress_decrease_alert_until
        ):
            self._suppress_decrease_alert_until = None
        # 버튼/업데이트 등 의도적 종료 여부 (suppress 플래그)
        # EXIT_RESERVATION은 KeywordMonitor 선감지 알림으로 처리(여기서는 중복 메시지 미발송)
        suppressed = bool(self._suppress_decrease_alert_until)

        # 프로세스 개수가 줄어든 경우에만, 변화가 있을 때 1번만 알림
        if (
            self.previous_process_count is not None
            and self.last_process_count < self.previous_process_count
        ):
            if self.last_alerted_process_count == self.last_process_count:
                return  # 같은 값으로 연속 알림 방지
            logger.debug(
                f"프로세스 개수 감소 감지: {self.previous_process_count} -> {self.last_process_count}"
            )

            # 사라진 프로세스별로 메시지 생성 (정상/비정상 분기)
            messages_to_send = []
            normal_exit_dirs = []
            abnormal_exit_dirs = []
            race_guarded_dirs = []
            for dir_name, dir_path in self._disappeared_dirs:
                escaped_name = escape_markdown(dir_name)
                # [RACE GUARD] 재시작 write-after-write 방지:
                # dir이 사라졌다고 판단됐지만 같은 dir에 살아있는 새 프로세스가 있으면
                # 그 identity는 유효한 현재 세션이므로 종료 cleanup(merge/clear)을 스킵한다.
                try:
                    alive = [
                        p for (p, _path) in self.find_processes_by_dir(dir_name, force_refresh=True)
                        if p is not None
                    ]
                except Exception:
                    alive = []  # 라이브니스 확인 실패 → 보수적으로 기존 cleanup 진행
                if alive:
                    logger.info(
                        "[PROC][EXIT][CLEANUP_SKIP] bot=%s reason=process_still_alive "
                        "alive_pids=%s source=race_guard",
                        dir_name,
                        [p.pid for p in alive],
                    )
                    race_guarded_dirs.append(dir_name)
                    continue
                if self._check_exit_reservation(dir_path):
                    # EXIT_RESERVATION: 선감지 알림(KeywordMonitor)만 사용하고 중복 ✅ 메시지는 생략
                    self.suppress_decrease_alert()
                    logger.debug(f"EXIT_RESERVATION 정상 종료 감지: {dir_name}")
                    # state 파일에 EXIT_RESERVATION 기록 → 로테이션 대기 상태
                    write_bot_state(dir_path, "EXIT_RESERVATION")
                    _persist_exit_reservation_session(dir_path, dir_name)
                    normal_exit_dirs.append(dir_name)
                elif not suppressed:
                    # 비정상 종료: suppress되지 않은 경우만 🚨 발송
                    _persist_process_exit_session(dir_path, dir_name)
                    messages_to_send.append(
                        f"🚨 *실행 중인 봇 비정상 종료*: `{escaped_name}`"
                    )
                    abnormal_exit_dirs.append(dir_name)

            # fallback: disappeared_dirs 추적 실패 + suppress 아님 (dir_name 특정 불가)
            # race_guarded_dirs는 살아있는 봇이 있어 cleanup을 스킵한 것이므로 fallback 제외
            if not messages_to_send and not suppressed and not normal_exit_dirs and not race_guarded_dirs:
                decreased = self.previous_process_count - self.last_process_count
                messages_to_send.append(
                    f"🚨 *봇 비정상 종료 감지*\n\n"
                    f"실행 중인 봇 {decreased}개가 종료되었습니다\\."
                )

            logger.info(
                f"[PROC][EXIT] prev={self.previous_process_count} now={self.last_process_count} "
                f"normal={len(normal_exit_dirs)} abnormal={len(abnormal_exit_dirs)} "
                f"suppressed={int(suppressed)} bots={[*normal_exit_dirs, *abnormal_exit_dirs]}"
            )

            # [최적화] 비동기 함수 호출 - 메인 루프가 있으면 threadsafe 방식 사용
            if self.main_loop and self.main_loop.is_running():
                for msg in messages_to_send:
                    asyncio.run_coroutine_threadsafe(
                        send_telegram_message(
                            self.telegram_bot_token, self.telegram_chat_id, msg
                        ),
                        self.main_loop,
                    )
                # 대시보드 새로고침 콜백 호출
                if self._on_process_decrease_callback:
                    asyncio.run_coroutine_threadsafe(
                        self._on_process_decrease_callback(), self.main_loop
                    )

                all_exited_dirs = normal_exit_dirs + abnormal_exit_dirs
                if all_exited_dirs:
                    request_ids = {
                        name: self._last_cleanup_request_ids.get(name, "none")
                        for name in all_exited_dirs
                    }
                    logger.info(
                        f"[PROC][CLEANUP][DETECTED] bots={all_exited_dirs} request_ids={request_ids}"
                    )

                if self._window_manager and not self._arranged_this_cycle:
                    self._window_manager.trigger_auto_arrange()
                    self._arranged_this_cycle = True
            else:
                # 초기화 중이거나 루프가 없는 경우 fallback
                for msg in messages_to_send:
                    asyncio.run(
                        send_telegram_message(
                            self.telegram_bot_token, self.telegram_chat_id, msg
                        )
                    )
            self.last_alerted_process_count = self.last_process_count
        # 개수가 증가하거나 같으면 알림 X
        elif self.last_process_count > self.previous_process_count:
            self.last_alerted_process_count = None  # 증가 시 알림 상태 초기화

    def log_entry_counts(self, changed_dirs=None):
        """
        DB 파일 변경 시 콘솔에 최신 진입 횟수를 출력합니다.
        db_file_watcher의 콜백으로 호출됩니다.

        Args:
            changed_dirs: 변경된 디렉토리 Path Set (None이면 전체 출력)
        """
        process_tuples = self.find_processes()
        if not process_tuples:
            return

        # 변경된 디렉토리 이름 Set (필터링용)
        changed_names = {d.name for d in changed_dirs} if changed_dirs else None

        for _, target_path in process_tuples:
            dir_name = target_path.parent.name

            # 변경된 디렉토리만 필터링
            if changed_names and dir_name not in changed_names:
                continue

            dir_path = target_path.parent
            trading_type, trading_limit_count = get_trading_info_from_env(dir_path)
            entry_count = get_entry_count_generic(dir_path, trading_type)

            # Entry count 로그는 노이즈가 커서 운영 로그에서 제거 (필요 시 DEBUG로 별도 추가)

    @staticmethod
    def _bot_ops_db_enabled_for_cutover() -> bool:
        return runtime_settings.get_bool(
            "BOT_OPS_DB_ENABLED", True
        ) and runtime_settings.get_bool("BOT_OPS_DB_CUTOVER", False)

    @staticmethod
    def _bot_ops_db_path() -> Path | None:
        p = runtime_settings.get_str("BOT_OPS_DB_PATH", "").strip()
        if p:
            return Path(p)
        return ZFlowBridge.default_bot_operations_db_path()

    def _sync_bot_status_db(
        self, current_running_dirs: dict[str, Path], current_time: datetime
    ) -> None:
        """Step C 확장: bot_status에 RUNNING/STOPPED 상태를 동기화."""
        if not self._bot_ops_db_enabled_for_cutover():
            return

        db = self._bot_ops_db_path()
        if db is None:
            return
        db.parent.mkdir(parents=True, exist_ok=True)
        now_iso = current_time.isoformat()

        rows_running = []
        current_names = set(current_running_dirs.keys())

        for bot_name, dir_path in current_running_dirs.items():
            try:
                cfg = EnvConfigHandler.parse(dir_path)
                coin1 = (cfg.get("COIN1") or cfg.get("COIN") or "").strip().upper()
                coin2 = (cfg.get("COIN2") or "").strip().upper()
                current_pair = f"{coin1}/{coin2}" if coin1 and coin2 else None
                trading_type, _ = get_trading_info_from_env(dir_path)
                # bot_status 동기화 시엔 파일 기반 값을 우선 읽어 DB를 갱신
                current_round = get_entry_count_generic(
                    dir_path, trading_type, prefer_db=False
                )
                rows_running.append(
                    (
                        bot_name,
                        trading_type,
                        current_pair,
                        current_round,
                        "RUNNING",
                        now_iso,
                    )
                )
            except Exception:
                continue

        try:
            with sqlite3.connect(db) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_status (
                        bot_name TEXT PRIMARY KEY,
                        slot_type TEXT,
                        current_pair TEXT,
                        current_round INTEGER,
                        process_state TEXT,
                        last_updated TEXT NOT NULL
                    )
                    """
                )

                if rows_running:
                    cur.executemany(
                        """
                        INSERT INTO bot_status (bot_name, slot_type, current_pair, current_round, process_state, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(bot_name) DO UPDATE SET
                            slot_type=excluded.slot_type,
                            current_pair=excluded.current_pair,
                            current_round=excluded.current_round,
                            process_state=excluded.process_state,
                            last_updated=excluded.last_updated
                        """,
                        rows_running,
                    )

                # 현재 실행 목록에 없는 봇은 STOPPED로 마킹
                if current_names:
                    placeholders = ",".join(["?"] * len(current_names))
                    cur.execute(
                        f"""
                        UPDATE bot_status
                        SET process_state='STOPPED', last_updated=?
                        WHERE bot_name NOT IN ({placeholders})
                        """,
                        [now_iso, *list(current_names)],
                    )
                else:
                    cur.execute(
                        "UPDATE bot_status SET process_state='STOPPED', last_updated=?",
                        (now_iso,),
                    )

                conn.commit()

            logger.debug("[BOT_OPS][STATUS] running_synced=%d", len(rows_running))
        except Exception as e:
            logger.error(f"❌ bot_status sync failed: {e}")

    def _check_exit_reservation(self, dir_path: Path) -> bool:
        """
        monitor.log 마지막 50줄에서 EXIT_RESERVATION 키워드를 확인합니다.
        봇이 예약 종료된 경우 True를 반환합니다.
        """
        log_file = dir_path / FileConfig.MONITOR_LOG
        if not log_file.exists():
            return False
        try:
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = list(deque(f, 50))
            return any("EXIT_RESERVATION" in line for line in lines)
        except Exception:
            return False

    def _build_lifecycle_snapshot(
        self,
        current_running_dirs: dict[str, Path],
        current_z_flow_running_dirs: dict[str, Path],
    ) -> dict[str, Path]:
        """일반 봇과 Z-Flow 런타임을 통합한 lifecycle snapshot."""
        snapshot = dict(current_running_dirs)
        for name, path in current_z_flow_running_dirs.items():
            snapshot.setdefault(name, path)
        return snapshot

    def _previous_lifecycle_snapshot(self) -> dict[str, Path]:
        snapshot = dict(self._previous_running_dirs)
        for name, path in self._previous_z_flow_running_dirs.items():
            snapshot.setdefault(name, path)
        return snapshot

    def _diff_lifecycle_dirs(
        self,
        current_running_dirs: dict[str, Path],
        current_z_flow_running_dirs: dict[str, Path],
    ) -> tuple[dict[str, Path], dict[str, Path]]:
        previous_snapshot = self._previous_lifecycle_snapshot()
        current_snapshot = self._build_lifecycle_snapshot(
            current_running_dirs,
            current_z_flow_running_dirs,
        )
        started_dirs = {
            name: path
            for name, path in current_snapshot.items()
            if name not in previous_snapshot
        }
        exited_dirs = {
            name: path
            for name, path in previous_snapshot.items()
            if name not in current_snapshot
        }
        return started_dirs, exited_dirs

    def _session_store_for(self, dir_name: str) -> SessionStore:
        """dir_name에 해당하는 SessionStore를 반환한다."""
        return SessionStore(self.target_dir / dir_name)

    def _retry_partial_identity(
        self,
        current_running_dirs: dict,
        current_z_flow_running_dirs: dict,
        current_time: datetime,
    ) -> None:
        """window_id가 null인 외부봇에 대해 per-dir 3회 cap으로 재시도."""
        MAX_RETRIES = 3
        for dir_name in current_running_dirs:
            if dir_name in current_z_flow_running_dirs:
                continue
            ref = self._session_store_for(dir_name).load()
            if ref is None:
                continue
            if ref.runtime_kind != "external_bot":
                continue
            if ref.identity_generation is None or ref.window_id is not None:
                continue
            # 시도 횟수 확인
            attempt, _ = self._window_retry_state.get(dir_name, (0, current_time))
            if attempt >= MAX_RETRIES:
                continue
            # osascript로 window_id 조회
            wid, status, count, wtty = resolve_terminal_window_id_for_identity(
                tty=ref.tty, custom_title=dir_name
            )
            self._window_retry_state[dir_name] = (attempt + 1, current_time)
            if wid is None:
                logger.info(
                    "[IDENTITY][WINDOW_RETRY] dir=%s attempt=%d status=%s matches=%d",
                    dir_name, attempt + 1, status, count,
                )
                continue
            # window_id 채우기 (None-coalescing merge — pid/pgid/tty 보존)
            try:
                self._session_store_for(dir_name).update_runtime_identity(
                    identity_generation=ref.identity_generation,
                    window_id=wid,
                    tty=ref.tty or wtty,
                    window_captured_at=current_time.astimezone().isoformat(),
                    validation_status="captured",
                    validation_reason="window_retry_success",
                )
                del self._window_retry_state[dir_name]
                logger.info(
                    "[IDENTITY][WINDOW_RETRY_OK] dir=%s window_id=%s attempt=%d",
                    dir_name, wid, attempt + 1,
                )
            except IdentityGenerationMismatchError:
                self._window_retry_state.pop(dir_name, None)
                logger.warning(
                    "[IDENTITY][WINDOW_RETRY_MISMATCH] dir=%s — generation changed, aborting retry",
                    dir_name,
                )

    def check_status(self):
        """
        현재 상태 확인 (Escape Kill 로직 제거됨)
        """
        self.ignore_list = load_ignored_dirs()
        process_tuples = self.find_processes()
        current_time = datetime.now()
        process_count = len(process_tuples)
        changed_escape_dirs = []  # 호환성을 위해 빈 리스트 유지

        self._arranged_this_cycle = False

        # 현재 실행 중인 프로세스 디렉토리 맵
        current_running_dirs = {
            proc_path.parent.name: proc_path.parent for _, proc_path in process_tuples
        }

        # Z-Flow 런타임 실행 상태 추적 (PID 파일 기반)
        z_flow_process_tuples = self.find_z_flow_processes()
        current_z_flow_running_dirs = {
            dir_name: dir_path for proc, dir_name, dir_path in z_flow_process_tuples if proc is not None
        }

        started_dirs, exited_dirs = self._diff_lifecycle_dirs(
            current_running_dirs,
            current_z_flow_running_dirs,
        )

        # 실행 시작 시각 추적 (신규 실행 디렉토리만 기록)
        current_lifecycle_names = set(
            self._build_lifecycle_snapshot(
                current_running_dirs,
                current_z_flow_running_dirs,
            ).keys()
        )
        for name in started_dirs:
            self._running_since[name] = current_time
        # 종료된 디렉토리는 정리
        for name in list(self._running_since.keys()):
            if name not in current_lifecycle_names:
                del self._running_since[name]
                self._window_retry_state.pop(name, None)

        # Phase 2 Step C 확장: bot_status DB 동기화
        self._sync_bot_status_db(current_running_dirs, current_time)

        if self.last_process_count != -1 and process_count != self.last_process_count:
            logger.debug(
                f"프로세스 개수 변경 감지: {self.last_process_count} -> {process_count}"
            )
            self.previous_process_count = self.last_process_count
            self.count_change_time = current_time
            self.last_count_alert_time = None  # 알림 타이머 초기화

        if started_dirs:
            logger.info(
                f"[PROC][START] bots={list(started_dirs.keys())} source=lifecycle_diff"
            )
            for dir_name, dir_path in started_dirs.items():
                # Z-Flow slot bot은 identity 캡처 대상에서 제외
                if dir_name in current_z_flow_running_dirs:
                    logger.info(
                        "[PROC][START][IDENTITY_SKIP] bot=%s reason=z_flow_runtime source=lifecycle_diff",
                        dir_name,
                    )
                    continue
                # 이미 identity_generation이 존재하는 봇은 중복 캡처 방지
                try:
                    existing = SessionStore(dir_path).load()
                    if existing is not None and existing.identity_generation is not None and existing.window_id is not None:
                        logger.info(
                            "[PROC][START][IDENTITY_SKIP] bot=%s reason=already_captured source=lifecycle_diff",
                            dir_name,
                        )
                        continue
                except Exception:
                    pass
                try:
                    capture_external_bot_identity(
                        target=dir_name,
                        data_dir=dir_path,
                        process_lookup=lambda t, _pts=process_tuples: (
                            next(
                                (
                                    (proc, proc_path.parent)
                                    for proc, proc_path in _pts
                                    if proc_path.parent.name == t
                                ),
                                None,
                            )
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        "[PROC][START][IDENTITY_CAPTURE_FAILED] bot=%s reason=%s source=lifecycle_diff",
                        dir_name,
                        exc,
                    )

        self._disappeared_dirs = list(exited_dirs.items())
        exited_dir_names = [name for name, _ in self._disappeared_dirs]
        self._schedule_cleanup_for_exited_dirs(exited_dir_names)

        arrange_started_dirs = {
            name: path
            for name, path in started_dirs.items()
            if not self._consume_recent_marker(
                self._start_arrange_handled_until,
                name,
            )
        }
        skipped_arrange_dirs = [
            name for name in started_dirs if name not in arrange_started_dirs
        ]
        if skipped_arrange_dirs:
            logger.info(
                f"[PROC][START][ARRANGE_SKIP] bots={skipped_arrange_dirs} reason=already_scheduled"
            )
            if not arrange_started_dirs:
                self._arranged_this_cycle = True

        if arrange_started_dirs and self._window_manager and not self._arranged_this_cycle:
            self._window_manager.trigger_auto_arrange()
            self._arranged_this_cycle = True

        self._retry_partial_identity(current_running_dirs, current_z_flow_running_dirs, current_time)

        self._previous_running_dirs = current_running_dirs
        self._previous_z_flow_running_dirs = current_z_flow_running_dirs
        self.last_process_count = process_count

        if process_tuples:
            if not self.is_running:
                logger.info(
                    f"✅ 프로세스 발견: {self.process_name} (개수: {process_count})"
                )
                self.is_running = True
                # 최초 프로세스 발견 시 터미널 창 자동 정렬
                if self._window_manager and not self._arranged_this_cycle:
                    self._window_manager.trigger_auto_arrange()
                    self._arranged_this_cycle = True

            self.last_seen = current_time

            # 프로세스 정보 업데이트 (CPU 등 내부 카운터 갱신)
            for proc, _ in process_tuples:
                self.get_process_info(proc)

            return True, changed_escape_dirs
        else:
            if self.is_running:
                logger.warning(f"❌ 프로세스 중단됨: {self.process_name} (개수: 0)")
                self.is_running = False

            return False, changed_escape_dirs

    def run_monitor(self):
        """
        감시 프로그램 실행 (백그라운드에서)
        """
        logger.info("🔍 프로세스 감시 시작")

        try:
            while True:
                self.check_status()

                # 터미널 정체 확인
                self.check_terminal_stalls()

                # 프로세스 개수 변경 알림 확인
                self.check_process_count_alerts()

                time.sleep(self.check_interval)

        except KeyboardInterrupt:
            logger.info("👋 감시 프로그램 종료")
        except Exception as e:
            logger.error(f"예상치 못한 오류: {e}")
