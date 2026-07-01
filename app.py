#!/usr/bin/env python3
"""
Z-Pulse 텔레그램 봇 (모니터링 통합)
프로세스 모니터링과 텔레그램 봇 기능을 통합하여 제공
"""

import warnings
import asyncio
import logging
import os
import sys
import atexit
import subprocess
from pathlib import Path
from typing import Any, TYPE_CHECKING, Optional, cast

from z_pulse import __version__

# Windows multiprocessing(spawn)은 각 worker 프로세스에서 이 스크립트를 '__mp_main__'으로
# 재실행함. Worker는 pair_trading.correlator._worker_task만 필요하며 bot 의존성
# (telegram→httpx→ssl)은 불필요. 재임포트 시 ssl.py에서 SystemError 발생 방지.
_IS_WORKER = __name__ == "__mp_main__"

if TYPE_CHECKING:
    import psutil
    from telegram import Update
    from telegram.ext import ContextTypes

if not _IS_WORKER:
    import psutil
    from telegram import Update
    from telegram.ext import ContextTypes
    from z_pulse.platforms import get_platform_handler
    from z_pulse.features import ProcessController
    from z_pulse.features import WindowManager
    from z_pulse.config import load_env_file, runtime_settings
    from z_pulse.monitoring import ProcessMonitor, LogKeywordMonitor
    from z_pulse.bot.handlers.dashboard import DashboardHandler
    from z_pulse.bot.handlers.process_actions import ProcessActionHandler
    from z_pulse.bot.handlers.settings import SettingsHandler
    from z_pulse.bot.handlers.commands import BotCommandHandler
    from z_pulse.bot.handlers.keywords import KeywordHandler
    from z_pulse.bot.handlers.callback_router import CallbackRouter
    from z_pulse.bot.auth import AuthManager
    from z_pulse.bot.monitoring_thread import BotMonitoringThread
    from z_pulse.bot.factory import BotFactory
    from z_pulse.monitoring.cleanup_orchestrator import CleanupOrchestrator
    from z_pulse.monitoring.cleanup_snapshot import TerminalCleanupSnapshot
    from z_pulse.monitoring.session_store import SessionRef, SessionStore
    from z_pulse.utils.instrumentation import EventLoopLagProbe

    # bot/ui는 BotCommandHandler, BotFactory에서 직접 import
    from z_pulse.features import FileOperations

    # 경제지표 캘린더 임포트
    try:
        from z_pulse.features.economic_calendar import EconomicCalendarManager

        ECONOMIC_CALENDAR_AVAILABLE = True
    except ImportError:
        ECONOMIC_CALENDAR_AVAILABLE = False
        print("경제지표 모듈을 찾을 수 없습니다. 경제지표 기능이 비활성화됩니다.")
else:
    # Worker 프로세스: bot 상수 기본값만 설정 (실제로 사용되지 않음)
    ECONOMIC_CALENDAR_AVAILABLE = False
    psutil = cast(Any, None)
    Update = cast(Any, None)
    ContextTypes = cast(Any, None)
    get_platform_handler = cast(Any, None)
    ProcessController = cast(Any, None)
    WindowManager = cast(Any, None)
    load_env_file = cast(Any, None)
    runtime_settings = cast(Any, None)
    ProcessMonitor = cast(Any, None)
    LogKeywordMonitor = cast(Any, None)
    DashboardHandler = cast(Any, None)
    ProcessActionHandler = cast(Any, None)
    SettingsHandler = cast(Any, None)
    BotCommandHandler = cast(Any, None)
    KeywordHandler = cast(Any, None)
    CallbackRouter = cast(Any, None)
    AuthManager = cast(Any, None)
    BotMonitoringThread = cast(Any, None)
    BotFactory = cast(Any, None)
    CleanupOrchestrator = cast(Any, None)
    TerminalCleanupSnapshot = cast(Any, None)
    SessionRef = cast(Any, None)
    SessionStore = cast(Any, None)
    EventLoopLagProbe = cast(Any, None)
    FileOperations = cast(Any, None)
    EconomicCalendarManager = cast(Any, None)

# 경제지표 스케줄러 환경변수 로딩
ECONOMIC_UPDATE_HOUR = int(os.getenv("ECONOMIC_UPDATE_HOUR", "6"))
ECONOMIC_ENABLED = os.getenv("ECONOMIC_CALENDAR_ENABLED", "true").lower() == "true"
DEFAULT_TELEGRAM_BOOTSTRAP_RETRIES = -1
DEFAULT_TELEGRAM_POLL_TIMEOUT_SEC = 10


def _telegram_bootstrap_retries() -> int:
    """Telegram bootstrap 재시도 횟수. -1은 python-telegram-bot의 무제한 재시도."""
    if _IS_WORKER:
        return DEFAULT_TELEGRAM_BOOTSTRAP_RETRIES
    return runtime_settings.get_int(
        "TELEGRAM_BOOTSTRAP_RETRIES",
        DEFAULT_TELEGRAM_BOOTSTRAP_RETRIES,
    )


def _telegram_poll_timeout() -> int:
    """getUpdates long polling 대기 시간."""
    if _IS_WORKER:
        return DEFAULT_TELEGRAM_POLL_TIMEOUT_SEC
    return runtime_settings.get_int(
        "TELEGRAM_POLL_TIMEOUT_SEC",
        DEFAULT_TELEGRAM_POLL_TIMEOUT_SEC,
    )

if not _IS_WORKER:
    # urllib3 SSL 경고 무시 (모든 import 이후에 호출)
    warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+")

    # 로깅 설정 (stdout 기반, 파일 기록은 run_all.sh가 담당)
    from z_pulse.utils.log_setup import setup_logging

    setup_logging()

logger = logging.getLogger(__name__)

# 싱글 인스턴스 락 파일 (중복 실행 방지)
_LOCK_FILE = "z_pulse.lock"
_LOCK_FD = None

# 텔레그램 관련 로그 레벨 조정
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.vendor.ptb_urllib3.urllib3").setLevel(logging.WARNING)
logging.getLogger("telegram.ext._application").setLevel(logging.WARNING)


class ZPulse:
    def __init__(self, token: str, target_dir: str, process_name: str):
        """
        텔레그램 봇 초기화

        Args:
            token: 텔레그램 봇 토큰
            target_dir: 감시할 프로그램들이 있는 디렉토리
            process_name: 감시할 프로그램 이름
        """
        self.token = token
        self.target_dir = target_dir
        self.process_name = process_name
        self.monitor = ProcessMonitor(target_dir, process_name, check_interval=10)
        self.log_keyword_monitor = LogKeywordMonitor(
            target_dir, self
        )  # 로그 키워드 감시 인스턴스
        self._auth_manager = AuthManager()
        self.application = None
        self.commands_set = False  # 명령어 메뉴 설정 여부
        self.monitor_task = None  # 백그라운드 모니터링 태스크
        self.main_loop = None  # [추가] 명시적 초기화
        self.platform_handler = get_platform_handler()
        self.economic_manager = None
        self.last_economic_update = None  # 마지막 경제지표 업데이트 시간
        if ECONOMIC_CALENDAR_AVAILABLE:
            try:
                self.economic_manager = EconomicCalendarManager()
                logger.info("경제지표 캘린더 매니저 초기화 완료")
            except Exception as e:
                logger.warning(f"경제지표 캘린더 매니저 초기화 실패: {e}")
                self.economic_manager = None
        self.window_manager = WindowManager(
            monitor=self.monitor,
            platform_handler=self.platform_handler,
            process_name=self.process_name,
        )
        self.process_controller = ProcessController(
            monitor=self.monitor,
            platform_handler=self.platform_handler,
            process_name=self.process_name,
            auto_arrange_callback=self.window_manager.trigger_auto_arrange,
        )
        self.file_operations = FileOperations(
            process_controller=self.process_controller
        )
        self._auth_manager.load_authorized_chat_id()
        self.dashboard_handler = DashboardHandler(self, self.monitor)
        self.process_action_handler = ProcessActionHandler(
            bot_instance=self,
            monitor=self.monitor,
            process_controller=self.process_controller,
            file_operations=self.file_operations,
            dashboard_handler=self.dashboard_handler,
        )
        self.settings_handler = SettingsHandler(
            bot_instance=self,
            process_controller=self.process_controller,
            dashboard_handler=self.dashboard_handler,
            monitor=self.monitor,
        )
        self.keyword_handler = KeywordHandler(
            bot_instance=self,
            log_keyword_monitor=self.log_keyword_monitor,
            dashboard_handler=self.dashboard_handler,
        )
        self.bot_command_handler = BotCommandHandler(
            bot_instance=self,
            monitor=self.monitor,
            process_controller=self.process_controller,
            file_operations=self.file_operations,
            economic_manager=self.economic_manager,
            window_manager=self.window_manager,
            dashboard_handler=self.dashboard_handler,
            settings_handler=self.settings_handler,
            grvt_manager=None,
        )

        # z_flow 파일시스템 IPC 브릿지 초기화 (Z_FLOW_PATH 설정 시 활성화)
        from z_pulse.integration.z_flow_bridge import ZFlowBridge
        self.z_flow_bridge = ZFlowBridge.from_env()
        if self.z_flow_bridge.enabled:
            self.z_flow_bridge.wire_z_flow_runtime_di()
        self.monitor.set_z_flow_bridge(self.z_flow_bridge)
        self.telegram_extensions = self.z_flow_bridge.build_telegram_extensions(self)

        # Bridge 배선 이후 — grvt_manager를 Bridge accessor로 획득
        self.grvt_manager = self.z_flow_bridge.get_grvt_transfer_service()
        self.bot_command_handler.grvt_manager = self.grvt_manager

        # WindowManager에 Z-Flow 세션 로더 주입 (창 정렬 키워드 보강용)
        def _load_active_z_flow_sessions() -> list:
            sessions = []
            if not self.z_flow_bridge.enabled:
                return sessions
            try:
                pid_files = self.z_flow_bridge.get_runtime_pid_files(
                    self.monitor.target_dir, self.monitor.ignore_list
                )
                for _dir_name, pid_file in pid_files.items():
                    session = SessionStore(pid_file.parent).load()
                    if session is not None:
                        sessions.append(session)
            except Exception as e:
                logger.warning(f"Z-Flow 세션 로드 실패 (무시): {e}")
            return sessions

        self.window_manager.set_z_flow_session_loader(_load_active_z_flow_sessions)

        self.callback_router = CallbackRouter(self)
        self._loop_lag_probe = EventLoopLagProbe()
        self._loop_lag_probe_interval_seconds = 0.100
        self._loop_lag_probe_task: asyncio.Task[Any] | None = None  # pyright: ignore[reportGeneralTypeIssues]

        # 외부봇 자동 배정용 PairTradingManager 생성 (z_flow 패키지가 존재할 때만)
        if self.z_flow_bridge.enabled:
            from z_pulse.config.symbols import load_symbols
            from z_pulse.config.env_handler import EnvConfigHandler
            from z_pulse.monitoring.bot_state import is_pair_trading_type
            symbols = load_symbols()
            use_testnet = False
            try:
                env_path = runtime_settings.env_path
                if env_path.exists():
                    for raw in env_path.read_text(encoding="utf-8").splitlines():
                        line = raw.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        if key.strip() == "USE_TESTNET":
                            use_testnet = (
                                value.strip().strip('"\' ').lower() == "true"
                            )
                            break
            except Exception:
                use_testnet = False
            # TARGET_DIR 하위 봇의 TRADING_TYPE을 수집해 소스별 manager 초기화
            _trading_types: set[str] = set()
            try:
                _target_dir = Path(self.target_dir)
                for _candidate in _target_dir.iterdir():
                    if not _candidate.is_dir() or _candidate.name.startswith("_"):
                        continue
                    _env_path = _candidate / "setting.env"
                    if not _env_path.exists():
                        continue
                    _tt = str(
                        EnvConfigHandler.parse(_env_path).get("TRADING_TYPE") or ""
                    ).strip()
                    if is_pair_trading_type(_tt):
                        _trading_types.add(_tt)
            except Exception as _e:
                logger.warning(f"TRADING_TYPE 수집 실패 (기본값 GRVT_PAIR 사용): {_e}")
                _trading_types = {"GRVT_PAIR"}
            if not _trading_types:
                _trading_types = {"GRVT_PAIR"}
            self.z_flow_bridge.init_pair_managers(
                symbols=symbols,
                trading_types=sorted(_trading_types),
            )
            if runtime_settings.get_bool("Z_FLOW_MARKET_DATA_DAEMON_MODE", True):
                logger.info(
                    "Z_FLOW_MARKET_DATA_DAEMON_MODE=true — "
                    "Z-Pulse 내부 market data coordinator 비활성화"
                )
            else:
                self.z_flow_bridge.start_market_data_coordinator(
                    symbols=symbols,
                    use_testnet=use_testnet,
                )

        self.cleanup_orchestrator = CleanupOrchestrator(
            executor=self.platform_handler,
            session_lookup=lambda dir_name: SessionStore.from_dir_name(
                self.target_dir, dir_name
            ).load(),
            session_store_factory=lambda dir_name: SessionStore.from_dir_name(
                self.target_dir, dir_name
            ),
        )

        self.monitoring_thread = BotMonitoringThread(
            monitor=self.monitor,
            check_interval=self.monitor.check_interval,
            economic_manager=self.economic_manager,
            log_keyword_monitor=self.log_keyword_monitor,
            economic_update_hour=ECONOMIC_UPDATE_HOUR,
            economic_enabled=ECONOMIC_ENABLED and ECONOMIC_CALENDAR_AVAILABLE,
        )
        self.monitoring_thread.set_pair_trading_bridge(self.z_flow_bridge)

        # DB 파일 감시자 콜백 연결 (대시보드 실시간 갱신용)
        self.monitoring_thread.set_file_watcher_callback(self.dashboard_handler)

        # EXIT_RESERVATION 감지 시 프로세스 감소 알림 억제 연결
        self.monitoring_thread.setup_exit_reservation_suppression()

        logger.info("텔레그램 봇 초기화 완료")
        logger.info(f"감시 대상: {len(self.monitor.target_paths)}개 프로그램")

    # ========== 인증 모듈 - AuthManager로 위임 ==========
    @property
    def authorized_chat_id(self):
        """인증된 채팅 ID (AuthManager로 위임)"""
        return self._auth_manager.authorized_chat_id

    @authorized_chat_id.setter
    def authorized_chat_id(self, value):
        """인증된 채팅 ID 설정 (AuthManager로 위임)"""
        self._auth_manager.authorized_chat_id = value

    def load_authorized_chat_id(self):
        """setting.env에서 인증된 채팅 ID 로드 (AuthManager로 위임)"""
        return self._auth_manager.load_authorized_chat_id()

    def is_authorized(self, user_id: int) -> bool:
        """사용자가 인증되었는지 확인 (AuthManager로 위임)"""
        return self._auth_manager.is_authorized(user_id)

    def refresh_target_programs(self):
        """감시 대상 프로그램 목록 갱신"""
        self.monitor.find_target_programs()

    def _mark_session_closed_after_dashboard_fallback(self, filter_keyword: str) -> None:
        """fallback cleanup 성공 후 session 상태를 closed로 정리"""
        target_dir = getattr(self, "target_dir", None)
        if not target_dir:
            return

        session_store = SessionStore.from_dir_name(target_dir, filter_keyword)
        session = session_store.load()
        if session is None:
            return

        session_store.save(
            SessionRef(
                session_id=session.session_id,
                dir_name=session.dir_name,
                runtime_kind=session.runtime_kind,
                platform=session.platform,
                status="closed",
                source="dashboard_cleanup",
                pid=session.pid,
                pgid=session.pgid,
                tty=session.tty,
                window_id=session.window_id,
                custom_title=session.custom_title,
                data_dir=session.data_dir,
                created_at=session.created_at,
                updated_at=session.updated_at,
            )
        )
        session_store.clear_runtime_identity()

    async def _cleanup_terminal_for_dashboard(
        self,
        filter_keyword: str,
        *,
        request_id: Optional[str] = None,
        scope_type: str = "single",
        cleanup_policy: str = "full",
    ) -> None:
        """대시보드용 터미널 정리 (orchestrator 우선, fallback은 platform_handler)"""
        cleanup_marker = getattr(
            getattr(self, "monitor", None),
            "mark_cleanup_handled",
            None,
        )
        if callable(cleanup_marker):
            cleanup_marker(filter_keyword)

        orchestrator = getattr(self, "cleanup_orchestrator", None)
        if orchestrator is not None:
            cleanup_kwargs: dict[str, object] = {
                "reason": "dashboard_cleanup",
                "scope_type": scope_type,
                "max_attempts": 1,
            }
            logger.info(
                "[CLEANUP][POLICY] request_id=%s target_id=%s cleanup_policy=%s critical_path=false",
                request_id or "none",
                filter_keyword,
                cleanup_policy,
            )
            if request_id is not None:
                cleanup_kwargs["request_id"] = request_id
            cleaned = await orchestrator.request_cleanup(
                filter_keyword,
                **cleanup_kwargs,
            )
            if isinstance(cleaned, dict):
                if cleaned.get("ok"):
                    return
                snapshot = cleaned.get("snapshot")
                attempted_steps = []
                if isinstance(snapshot, dict):
                    attempted_steps = list(snapshot.get("attempted_steps") or [])
                if attempted_steps:
                    logger.info(
                        "대시보드 터미널 정리 fallback 생략: filter=%s steps=%s",
                        filter_keyword,
                        attempted_steps,
                    )
                    self._mark_session_closed_after_dashboard_fallback(filter_keyword)
                    return
            elif cleaned:
                return
        await self.platform_handler.cleanup_terminal(filter_keyword)
        self._mark_session_closed_after_dashboard_fallback(filter_keyword)

    async def _cleanup_terminal_snapshot_for_dashboard(
        self,
        snapshot: Any,
    ) -> None:
        """Snapshot-only terminal cleanup for restart background paths.

        This method intentionally never resolves ``snapshot.target`` through
        ``SessionStore``/``CleanupOrchestrator`` and never falls back to title,
        cwd, dir name, or broad terminal matching. The only positive identities
        are the pre-spawn ``window_id`` and ``tty`` captured by the caller.
        """
        cleanup_marker = getattr(getattr(self, "monitor", None), "mark_cleanup_handled", None)
        if callable(cleanup_marker):
            cleanup_marker(snapshot.target)

        has_window_id = bool(snapshot.window_id)
        has_tty = bool(snapshot.tty)
        has_pid = bool(getattr(snapshot, "pid", None))
        has_pgid = bool(getattr(snapshot, "pgid", None))
        has_identity = bool(snapshot.window_id or snapshot.tty or has_pid or has_pgid)

        if not has_identity:
            session_id = getattr(snapshot, "session_id", None) or snapshot.target
            status = getattr(snapshot, "status", None) or "unknown"
            source = getattr(snapshot, "source", None) or "unknown"
            has_custom_title = bool(getattr(snapshot, "custom_title", None))
            hint = "persist_pid_pgid_tty_window_id_before_stop; custom_title_only_is_not_cleanup_identity"
            logger.info(
                "[CLEANUP][SNAPSHOT_SKIP] request_id=%s target_id=%s source=%s "
                "session_id=%s status=%s session_source=%s "
                "has_pid=%s has_pgid=%s has_tty=%s has_window_id=%s "
                "has_custom_title=%s has_identity=%s reason=%s hint=%s method=%s",
                snapshot.request_id or "none",
                snapshot.target,
                "dashboard_snapshot",
                session_id,
                status,
                source,
                has_pid,
                has_pgid,
                has_tty,
                has_window_id,
                has_custom_title,
                has_identity,
                "no_identity",
                hint,
                "none",
            )
            return

        class _SnapshotSession:
            def __init__(
                self,
                target: str,
                window_id: Optional[str],
                tty: Optional[str],
                pid: Optional[int],
                pgid: Optional[int],
            ):
                self.dir_name = target
                self.window_id = window_id
                self.tty = tty
                self.custom_title = None
                self.pid = pid
                self.pgid = pgid

        def _is_close_success(result: Any) -> bool:
            return result is True or (isinstance(result, dict) and result.get("ok") is True)

        window_id = snapshot.window_id
        tty = snapshot.tty
        session = _SnapshotSession(snapshot.target, window_id, tty, getattr(snapshot, "pid", None), getattr(snapshot, "pgid", None))
        if not window_id and not tty and (session.pgid or session.pid):
            logger.info(
                "[CLEANUP][SNAPSHOT_TRY] request_id=%s target_id=%s source=%s "
                "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                snapshot.request_id or "none",
                snapshot.target,
                "dashboard_snapshot",
                has_window_id,
                has_tty,
                has_identity,
                "process_identity_only",
                "terminate_process_group",
            )
            await self.platform_handler.terminate_process_group(session)
            return
        if window_id:
            logger.info(
                "[CLEANUP][SNAPSHOT_TRY] request_id=%s target_id=%s source=%s "
                "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                snapshot.request_id or "none",
                snapshot.target,
                "dashboard_snapshot",
                has_window_id,
                has_tty,
                has_identity,
                "primary_window_id",
                "close_window",
            )
            try:
                close_result = await self.platform_handler.close_window(session)
            except Exception as exc:
                close_result = None
                logger.info(
                    "[CLEANUP][SNAPSHOT_CLOSE_FAILED] request_id=%s target_id=%s source=%s "
                    "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s error_type=%s",
                    snapshot.request_id or "none",
                    snapshot.target,
                    "dashboard_snapshot",
                    has_window_id,
                    has_tty,
                    has_identity,
                    "close_exception",
                    "close_window",
                    type(exc).__name__,
                )
            else:
                if _is_close_success(close_result):
                    logger.info(
                        "[CLEANUP][SNAPSHOT_DONE] request_id=%s target_id=%s source=%s "
                        "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                        snapshot.request_id or "none",
                        snapshot.target,
                        "dashboard_snapshot",
                        has_window_id,
                        has_tty,
                        has_identity,
                        "close_success",
                        "close_window",
                    )
                    return
                logger.info(
                    "[CLEANUP][SNAPSHOT_CLOSE_FAILED] request_id=%s target_id=%s source=%s "
                    "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                    snapshot.request_id or "none",
                    snapshot.target,
                    "dashboard_snapshot",
                    has_window_id,
                    has_tty,
                    has_identity,
                    "close_not_confirmed",
                    "close_window",
                )

            if not tty:
                logger.info(
                    "[CLEANUP][SNAPSHOT_SKIP] request_id=%s target_id=%s source=%s "
                    "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                    snapshot.request_id or "none",
                    snapshot.target,
                    "dashboard_snapshot",
                    has_window_id,
                    has_tty,
                    has_identity,
                    "skip_tty_fallback",
                    "none",
                )
                return

            logger.info(
                "[CLEANUP][SNAPSHOT_TRY] request_id=%s target_id=%s source=%s "
                "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                snapshot.request_id or "none",
                snapshot.target,
                "dashboard_snapshot",
                has_window_id,
                has_tty,
                has_identity,
                "window_close_failed_tty_fallback",
                "terminate_tty_processes",
            )
            await self.platform_handler.terminate_tty_processes(session)
            logger.info(
                "[CLEANUP][SNAPSHOT_TRY] request_id=%s target_id=%s source=%s "
                "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                snapshot.request_id or "none",
                snapshot.target,
                "dashboard_snapshot",
                has_window_id,
                has_tty,
                has_identity,
                "post_tty_close_retry",
                "close_window_retry",
            )
            try:
                close_result = await self.platform_handler.close_window(session)
            except Exception as exc:
                logger.info(
                    "[CLEANUP][SNAPSHOT_RETRY_FAILED] request_id=%s target_id=%s source=%s "
                    "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s error_type=%s",
                    snapshot.request_id or "none",
                    snapshot.target,
                    "dashboard_snapshot",
                    has_window_id,
                    has_tty,
                    has_identity,
                    "window_retry_failed",
                    "close_window_retry",
                    type(exc).__name__,
                )
            else:
                if _is_close_success(close_result):
                    logger.info(
                        "[CLEANUP][SNAPSHOT_RETRY_DONE] request_id=%s target_id=%s source=%s "
                        "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                        snapshot.request_id or "none",
                        snapshot.target,
                        "dashboard_snapshot",
                        has_window_id,
                        has_tty,
                        has_identity,
                        "window_retry_succeeded",
                        "close_window_retry",
                    )
                else:
                    logger.info(
                        "[CLEANUP][SNAPSHOT_RETRY_FAILED] request_id=%s target_id=%s source=%s "
                        "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
                        snapshot.request_id or "none",
                        snapshot.target,
                        "dashboard_snapshot",
                        has_window_id,
                        has_tty,
                        has_identity,
                        "window_retry_failed",
                        "close_window_retry",
                    )
            return

        logger.info(
            "[CLEANUP][SNAPSHOT_TRY] request_id=%s target_id=%s source=%s "
            "has_window_id=%s has_tty=%s has_identity=%s reason=%s method=%s",
            snapshot.request_id or "none",
            snapshot.target,
            "dashboard_snapshot",
            has_window_id,
            has_tty,
            has_identity,
            "tty_only",
            "terminate_tty_processes",
        )
        await self.platform_handler.terminate_tty_processes(session)

    async def button_handler(self, update: Any, context: Any):
        """인라인 버튼 처리 - CallbackRouter로 위임"""
        query = update.callback_query
        if query is None:
            return
        await self.callback_router.route(query, context)

    async def handle_button_text(
        self, update: Any, context: Any
    ):
        """텍스트 버튼 입력 처리 - BotCommandHandler로 위임"""
        if await self.keyword_handler.handle_input(update, context):
            return
        user_data = context.user_data or {}
        if "pending_setting_change" in user_data:
            return
        if not await self.bot_command_handler.handle_text_button(update, context):
            message = update.message
            if message is not None:
                await message.reply_text("❓ 알 수 없는 명령어입니다.")

    def run(self):
        """봇 실행"""
        try:
            self._start_loop_lag_probe()
            self.application = BotFactory.create_application(self.token)

            BotFactory.register_handlers(
                self.application, self, ECONOMIC_CALENDAR_AVAILABLE
            )
            logger.info("텔레그램 봇이 시작되었습니다.")

            # post_init 훅 생성
            self.application.post_init = BotFactory.create_post_init_hook(self)
            self.application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                bootstrap_retries=_telegram_bootstrap_retries(),
                timeout=_telegram_poll_timeout(),
            )

        except Exception as e:
            logger.error(f"봇 실행 중 오류: {e}")
            raise
        finally:
            coro = self._stop_loop_lag_probe()
            if hasattr(coro, 'close'):
                coro.close()
            self._cancel_probe_task_safe()

    def _cancel_probe_task_safe(self) -> None:
        """probe_task를 안전하게 취소한다. 이벤트 루프 종료 후 호출 시 RuntimeError를 무시한다."""
        probe_task = getattr(self, "_loop_lag_probe_task", None)
        if probe_task is not None and not probe_task.done():
            try:
                probe_task.cancel()
            except RuntimeError:
                pass

    async def _loop_lag_probe_runner(self) -> None:
        interval = self._loop_lag_probe_interval_seconds
        expected = asyncio.get_running_loop().time() + interval
        while True:
            await asyncio.sleep(interval)
            now = asyncio.get_running_loop().time()
            self._loop_lag_probe.observe(max(0.0, now - expected))
            expected = now + interval

    def _start_loop_lag_probe(self) -> Optional[asyncio.Task]:  # pyright: ignore[reportReturnType]
        existing_task = getattr(self, "_loop_lag_probe_task", None)
        if existing_task is not None and not existing_task.done():
            return existing_task
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            self._loop_lag_probe_task = None
            return None
        self._loop_lag_probe_task = loop.create_task(
            self._loop_lag_probe_runner(),
            name="z_pulse_loop_lag_probe",
        )
        return self._loop_lag_probe_task

    async def _stop_loop_lag_probe(self) -> None:
        task = getattr(self, "_loop_lag_probe_task", None)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

def _acquire_single_instance_lock() -> None:
    """중복 실행 방지용 파일 락 획득 (Windows: msvcrt, macOS/Linux: fcntl)."""
    global _LOCK_FD

    lock_path = os.path.abspath(_LOCK_FILE)
    _LOCK_FD = open(lock_path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(_LOCK_FD.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(_LOCK_FD.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[WARN] z_pulse already running. Exit duplicate instance.")
        sys.exit(0)

    def _release_lock():
        global _LOCK_FD
        if _LOCK_FD is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(_LOCK_FD.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(_LOCK_FD.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            _LOCK_FD.close()
        except Exception:
            pass
        _LOCK_FD = None

    atexit.register(_release_lock)


def main():
    """메인 실행 함수"""
    _acquire_single_instance_lock()
    load_env_file()
    try:
        git_head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        ).stdout.strip() or "unavailable"
    except (OSError, subprocess.TimeoutExpired):
        git_head = "unavailable"
    logger.info(
        "[STARTUP][IDENTITY] Z-Pulse v%s git_head=%s module_path=%s instrumentation_enabled=true",
        __version__,
        git_head,
        os.path.abspath(__file__),
    )

    # [Phase 1.3] psutil CPU 측정 초기화
    # interval=None 사용 시 첫 호출 전 초기화 필요 (누적 데이터 수집 시작)
    print("시스템 모니터링 초기화 중...")
    psutil.cpu_percent(interval=1)  # 1초간 블로킹하여 초기 측정값 수집
    print("✅ 시스템 모니터링 준비 완료")

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN 환경 변수가 설정되지 않았습니다.")
        sys.exit(1)

    target_dir = os.getenv("TARGET_DIR", "/Users/user/Documents/toolkit")
    process_name = os.getenv("PROCESS_NAME", "2oolkit-bot-macos-arm64")

    bot = ZPulse(token, target_dir, process_name)
    bot.run()


if __name__ == "__main__":
    main()
