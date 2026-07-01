"""
모니터링 스레드 모듈
Z-Pulse에서 분리된 백그라운드 모니터링 로직
"""

import asyncio
import threading
import time
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Callable

from z_pulse.bot.economic_scheduler import EconomicSchedulerMixin
from z_pulse.integration.uptime_restart_scheduler import UptimeRestartSchedulerMixin
from z_pulse.utils.async_helpers import safe_send_message
from z_pulse.utils.telegram_gateway import (
    BACKGROUND_TIMEOUT,
    TelegramPriority,
    get_telegram_gateway,
)
from z_pulse.monitoring.memory_monitor import MemoryMonitor
from z_pulse.config.runtime_settings import runtime_settings

if TYPE_CHECKING:
    from z_pulse.monitoring.process_monitor import ProcessMonitor
    from z_pulse.monitoring.db_file_watcher import DBFileWatcher
    from z_pulse.features.economic_calendar import EconomicCalendarManager as EconomicManager
    from z_pulse.monitoring.keyword_monitor import LogKeywordMonitor
    from z_pulse.bot.handlers.dashboard import DashboardHandler

logger = logging.getLogger(__name__)

MONITOR_LOOP_SLOW_THRESHOLD_SEC = 2.0


class BotMonitoringThread(EconomicSchedulerMixin, UptimeRestartSchedulerMixin):
    """
    백그라운드 모니터링 스레드 관리자

    별도 스레드에서 실행되며, 메인 asyncio 이벤트 루프와 동기화하여
    프로세스 상태 변경, 경제지표 업데이트 등을 처리합니다.

    EconomicSchedulerMixin을 상속하여 경제지표 스케줄러 기능을 통합합니다.
    """

    def __init__(
        self,
        monitor: "ProcessMonitor",
        check_interval: int = 30,
        economic_manager: Optional["EconomicManager"] = None,
        log_keyword_monitor: Optional["LogKeywordMonitor"] = None,
        economic_update_hour: int = 6,
        economic_enabled: bool = True,
    ):
        """
        Args:
            monitor: 프로세스 모니터 인스턴스
            check_interval: 모니터링 주기 (초)
            economic_manager: 경제지표 매니저 인스턴스 (선택)
            log_keyword_monitor: 로그 키워드 모니터 인스턴스 (선택)
            economic_update_hour: 경제지표 일일 업데이트 시간 (0-23, 기본값 6)
            economic_enabled: 경제지표 스케줄러 활성화 여부 (기본값 True)
        """
        self.monitor = monitor
        self.check_interval = check_interval
        self.economic_manager = economic_manager
        self.log_keyword_monitor = log_keyword_monitor

        # 경제지표 스케줄러 믹스인 초기화
        self.__init_economic_scheduler__(
            economic_manager=economic_manager,
            update_hour=economic_update_hour,
            enabled=economic_enabled,
        )

        # 런타임에 설정되는 의존성
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._application = None  # telegram.ext.Application
        self._authorized_chat_id: Optional[int] = None

        # 스레드 관리
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        # 메모리 모니터 (RSS 추적 + 누수 감지)
        memory_alerts_enabled = runtime_settings.get_bool("MEMORY_ALERT_ENABLED", True)
        self._memory_monitor = MemoryMonitor(
            enable_tracemalloc=False, enable_alerts=memory_alerts_enabled
        )

        # DB 파일 감시자
        self._file_watcher: Optional["DBFileWatcher"] = None

        # ZFlowBridge 참조 및 페어 매매 가용성 플래그
        # (스케줄러 타이머 상태는 ZFlowBridge._pair_scheduler 내부로 이전)
        self._z_flow_bridge: Optional[object] = None
        self._source_managers: dict = {}  # 소스별 manager {"grvt": mgr, "binance": mgr, ...}
        self._pair_enabled = False

    def set_pair_trading_bridge(self, bridge: object) -> None:
        """ZFlowBridge를 주입하고 _source_managers를 채운다."""
        self._z_flow_bridge = bridge
        managers: dict = {}
        try:
            managers = bridge.get_pair_managers()  # type: ignore[union-attr]
        except Exception as _e:
            logger.warning(
                f"[PAIR-BRIDGE] bridge.get_pair_managers() 호출 실패 — pair 비활성화: {_e}"
            )

        if not managers:
            self._source_managers = {}
            self._pair_enabled = False
            return

        self._source_managers = dict(managers)
        self._pair_enabled = True
        logger.info(f"[PAIR-BRIDGE] source managers 등록: {list(managers.keys())}")

    def set_pair_trading_dependencies(self, process_controller, monitor) -> None:
        """pair_trading 자동화 의존성을 연결한다. ZFlowBridge 경유."""
        bridge = getattr(self, "_z_flow_bridge", None)
        if bridge is None:
            return
        bridge.setup_pair_trading_schedule(  # type: ignore[union-attr]
            process_controller,
            monitor,
            getattr(self, "_main_loop", None),
            application=self._application,
            authorized_chat_id=self._authorized_chat_id,
        )

    def run_pair_trading_schedule(self) -> None:
        """ZFlowBridge 경유 페어 매매 스케줄 실행. bridge 미주입 시 no-op."""
        if self._z_flow_bridge is not None:
            self._z_flow_bridge.run_pair_trading_schedule()  # type: ignore[union-attr]

    def set_uptime_restart_dependencies(self, process_action_handler: object) -> None:
        """uptime 재시작 스케줄러 의존성을 연결한다.

        set_runtime_dependencies 호출 후(main_loop 설정 후) 호출해야 한다.
        """
        if self._main_loop is None:
            logger.warning("[UPTIME_RESTART] main_loop 미설정 — 초기화를 건너뜁니다.")
            return
        self.__init_uptime_restart_scheduler__(
            process_action_handler=process_action_handler,
            monitor=self.monitor,
            bridge=None,
            main_loop=self._main_loop,
        )

    def set_runtime_dependencies(
        self,
        main_loop: asyncio.AbstractEventLoop,
        application,
        authorized_chat_id: int,
    ) -> None:
        """
        런타임 의존성 설정 (봇 시작 후 호출)

        Args:
            main_loop: asyncio 메인 이벤트 루프
            application: telegram.ext.Application 인스턴스
            authorized_chat_id: 인증된 채팅 ID
        """
        self._main_loop = main_loop
        self._application = application
        self._authorized_chat_id = authorized_chat_id
        self.monitor.set_loop(main_loop)

    def setup_exit_reservation_suppression(self) -> None:
        """
        EXIT_RESERVATION 키워드 감지 시 프로세스 감소 알림을 억제하도록 연결합니다.
        봇이 스스로 정상 종료할 때 비정상 종료 알림이 발송되지 않도록 합니다.
        """
        if self.log_keyword_monitor:
            self.log_keyword_monitor.set_suppress_alert_phrases(
                {"EXIT_RESERVATION"}, self.monitor.suppress_decrease_alert
            )
            logger.info("EXIT_RESERVATION → 프로세스 감소 알림 억제 연결 완료")

    def set_file_watcher_callback(self, dashboard_handler: "DashboardHandler") -> None:
        """
        DB 파일 감시자 설정 (대시보드 갱신 콜백 연결)

        Args:
            dashboard_handler: DashboardHandler 인스턴스
        """
        from z_pulse.monitoring.db_file_watcher import DBFileWatcher

        self._file_watcher = DBFileWatcher(debounce_seconds=1.5)
        self._file_watcher.set_dashboard_refresh_callback(
            dashboard_handler.trigger_refresh
        )
        self._file_watcher.set_entry_count_log_callback(self.monitor.log_entry_counts)
        logger.info("DB 파일 감시자 콜백 설정 완료")

    @property
    def main_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """메인 이벤트 루프 (외부에서 설정 가능)"""
        return self._main_loop

    @main_loop.setter
    def main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._main_loop = loop

    def start(self) -> None:
        """모니터링 스레드 시작"""
        if self._thread and self._thread.is_alive():
            logger.warning("모니터링 스레드가 이미 실행 중입니다.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("백그라운드 모니터링 스레드가 시작되었습니다.")

    def stop(self) -> None:
        """모니터링 스레드 중지"""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
            logger.info("모니터링 스레드가 중지되었습니다.")

    def _run_loop(self) -> None:
        """모니터링 메인 루프 (별도 스레드에서 실행)"""
        logger.info("모니터링 스레드 시작")

        # 메인 루프 초기화 대기 (Race Condition 방지)
        wait_cycles = 0
        while self._main_loop is None and wait_cycles < 50:  # 0.2s * 50 = 10초
            if self._stop_event.is_set():
                return
            time.sleep(0.2)
            wait_cycles += 1

        if self._main_loop is None:
            logger.warning(
                "⚠️ 메인 루프 초기화 대기 시간 초과 - 초기 알림이 누락될 수 있습니다."
            )
        else:
            logger.info("✅ 메인 루프 감지됨, 모니터링 루프 진입")

            # DB 파일 감시자 시작
            self._start_file_watcher()

            # 고위험 키워드 실시간 감시 시작 (추가 진입 폭주 guard)
            if (
                self.log_keyword_monitor
                and runtime_settings.get_bool("RAPID_ENTRY_GUARD_ENABLED", True)
            ):
                self.log_keyword_monitor.start_rapid_entry_guard()

        try:
            while not self._stop_event.is_set():
                loop_started = time.perf_counter()
                step_timings: list[tuple[str, float]] = []

                def _record_step(name: str, started_at: float) -> None:
                    step_timings.append((name, time.perf_counter() - started_at))

                try:
                    if not self._application or self._application.bot is None:
                        logger.info(
                            "봇 애플리케이션이 종료되어 모니터링 스레드를 중단합니다."
                        )
                        break

                    # 로그 키워드 감시 실행
                    step_started = time.perf_counter()
                    if (
                        self.log_keyword_monitor
                        and runtime_settings.get_bool("KEYWORD_MONITOR_ENABLED", True)
                    ):
                        self.log_keyword_monitor.ensure_realtime_monitor_alive()
                        self.log_keyword_monitor.check_logs()
                    _record_step("keyword_monitor", step_started)

                    # 프로세스 상태 확인
                    step_started = time.perf_counter()
                    self.monitor.check_status()
                    _record_step("process_status", step_started)

                    # 기타 모니터링 작업
                    step_started = time.perf_counter()
                    self.monitor.check_terminal_stalls()
                    self.monitor.check_process_count_alerts()
                    self.run_economic_schedule_pending()
                    self.run_pair_trading_schedule()
                    self.run_uptime_restart_schedule()
                    _record_step("scheduled_tasks", step_started)

                    # 메모리 상태 주기적 체크 (임계치 초과 시 텔레그램 알림)
                    step_started = time.perf_counter()
                    mem_alert = self._memory_monitor.check()
                    if mem_alert:
                        self._send_memory_alert(mem_alert)
                    _record_step("memory_monitor", step_started)

                    loop_elapsed = time.perf_counter() - loop_started
                    if loop_elapsed >= MONITOR_LOOP_SLOW_THRESHOLD_SEC:
                        logger.warning(
                            "[MONITOR][LOOP][SLOW] elapsed=%.3fs steps=%s",
                            loop_elapsed,
                            ", ".join(
                                f"{name}={elapsed:.3f}s"
                                for name, elapsed in step_timings
                            ),
                        )

                except Exception as e:
                    logger.error(f"모니터링 루프 중 오류 (계속 실행): {e}")

                # 인터럽트 가능한 sleep
                if self._stop_event.wait(timeout=self.check_interval):
                    break

        except Exception as e:
            logger.error(f"모니터링 스레드 치명적 오류: {e}")
        finally:
            # DB 파일 감시자 종료
            if self._file_watcher:
                self._file_watcher.stop()

            # 고위험 키워드 실시간 감시 종료
            if self.log_keyword_monitor:
                self.log_keyword_monitor.stop_rapid_entry_guard()

            logger.info("모니터링 스레드 종료")

    def _start_file_watcher(self) -> None:
        """DB 파일 감시자 시작"""
        if not self._file_watcher:
            return

        try:
            # 메인 루프 설정
            self._file_watcher.set_main_loop(self._main_loop)  # pyright: ignore[reportArgumentType]

            # 활성 봇 디렉토리 수집
            watch_dirs = {p.parent for p in self.monitor.target_paths}

            if watch_dirs:
                self._file_watcher.start(watch_dirs)
            else:
                logger.info("감시할 봇 디렉토리가 없습니다.")
        except Exception as e:
            logger.warning(f"DB 파일 감시자 시작 실패 (계속 진행): {e}")

    async def send_economic_alert(self, events: list) -> None:
        """
        고중요도 경제지표 알림 전송

        Args:
            events: 경제지표 이벤트 목록
        """
        try:
            if not self._application or not self._authorized_chat_id:
                return
            if not self.economic_manager:
                logger.warning(
                    "economic_manager가 없어 경제지표 알림을 전송할 수 없습니다."
                )
                return
            message = self.economic_manager.format_alert_message(events, max_events=5)
            success = await safe_send_message(
                self._application.bot,
                chat_id=self._authorized_chat_id,
                text=message,
                parse_mode="MarkdownV2",
            )
            if success:
                logger.info(f"경제지표 알림 전송 완료: {len(events)}개 이벤트")
        except Exception as e:
            logger.error(f"경제지표 알림 전송 실패: {e}")

    async def send_scheduled_economic_update(self) -> None:
        """정시 경제지표 수집 완료 후 /economic 형식의 메시지 전송"""
        try:
            if (
                not self._application
                or self._application.bot is None
                or not self._authorized_chat_id
            ):
                return
            if not self.economic_manager:
                logger.warning(
                    "economic_manager가 없어 정시 경제지표 메시지를 전송할 수 없습니다."
                )
                return

            get_status_summary = getattr(self.economic_manager, "get_status_summary", None)
            format_events_message = getattr(
                self.economic_manager, "format_events_message", None
            )
            build_empty_reason_message = getattr(
                self.economic_manager, "build_empty_reason_message", None
            )
            if not callable(format_events_message) or not callable(
                build_empty_reason_message
            ):
                logger.warning("economic_manager에 정시 메시지 포맷 메서드가 없습니다.")
                return

            status = get_status_summary() if callable(get_status_summary) else {}
            last_success_at = status.get("last_success_at", "")  # pyright: ignore[reportAttributeAccessIssue]
            last_update = None
            if last_success_at:
                try:
                    last_update = datetime.fromisoformat(last_success_at)
                except ValueError:
                    logger.warning(
                        f"last_success_at 파싱 실패: {last_success_at}"
                    )

            message = format_events_message(days=7, max_events=8, last_update=last_update)
            if message:
                success = await safe_send_message(
                    self._application.bot,
                    chat_id=self._authorized_chat_id,
                    text=message,  # pyright: ignore[reportArgumentType]
                    parse_mode="MarkdownV2",
                )
            else:
                message = build_empty_reason_message()
                await get_telegram_gateway().enqueue(
                    lambda: self._application.bot.send_message(  # pyright: ignore[reportOptionalMemberAccess]
                        chat_id=self._authorized_chat_id,
                        text=message,
                    ),
                    priority=TelegramPriority.BACKGROUND,
                    timeout=BACKGROUND_TIMEOUT,
                    label="scheduled_economic_empty_message",
                )
                success = True

            if success:
                logger.info("정시 경제지표 메시지 전송 완료")
        except Exception as e:
            logger.error(f"정시 경제지표 메시지 전송 실패: {e}")

    def _send_memory_alert(self, alert_message: str) -> None:
        """메모리 임계치 초과 알림을 텔레그램으로 전송 (Thread-safe)"""
        if not self._main_loop or not self._main_loop.is_running():
            return
        try:
            if not self._application or not self._authorized_chat_id:
                return
            get_telegram_gateway(self._main_loop).enqueue_threadsafe(
                self._main_loop,
                lambda: self._application.bot.send_message(  # pyright: ignore[reportOptionalMemberAccess]
                    chat_id=self._authorized_chat_id,
                    text=alert_message,
                    parse_mode=None,
                ),
                priority=TelegramPriority.BACKGROUND,
                timeout=BACKGROUND_TIMEOUT,
                label="memory_alert",
                drop_ok=True,
            )
            logger.warning(f"[MEM] 메모리 알림 전송 완료")
        except Exception as e:
            logger.error(f"[MEM] 메모리 알림 전송 실패: {e}")

