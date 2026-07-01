"""uptime 기반 24h 자동 재시작 스케줄러 믹스인.

BotMonitoringThread에 mixin하여 모니터링 루프 매 사이클마다
auto_restart_24h=True인 봇의 가동 시간을 확인하고,
24시간이 넘으면 _simple_restart_core로 비동기 재시작을 디스패치한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from z_pulse.monitoring.process_monitor import process_uptime
from z_pulse.monitoring.session_store import SessionStore

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class UptimeRestartNotifier:
    """24h 자동 재시작 진행 상황을 텔레그램 단일 메시지 edit으로 표시.

    첫 update() 호출 시 send_message로 메시지 생성 후 message_id 저장,
    이후 update() 호출 시 edit_message_text로 동일 메시지 수정.
    모든 실패는 graceful warning — 재시작 로직을 막지 않는다.
    """

    def __init__(self, bot: object, chat_id: int, target: str, uptime_hours: float) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._target = target
        self._uptime_hours = uptime_hours
        self._message_id: int | None = None

    async def update(self, body: str) -> None:
        from z_pulse.utils import escape_markdown

        text = f"🔄 *{escape_markdown(self._target)}* 24h 재시작\n{escape_markdown(body)}"
        try:
            if self._message_id is None:
                msg = await self._bot.send_message(  # type: ignore[union-attr]
                    chat_id=self._chat_id,
                    text=text,
                    parse_mode="MarkdownV2",
                )
                self._message_id = msg.message_id
            else:
                await self._bot.edit_message_text(  # type: ignore[union-attr]
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    text=text,
                    parse_mode="MarkdownV2",
                )
        except Exception as e:
            logger.warning(
                "[UPTIME_RESTART][NOTIFY] %s 알림 실패: %s",
                self._target,
                type(e).__name__,
            )


class UptimeRestartSchedulerMixin:
    """24h uptime 자동 재시작 스케줄러 믹스인.

    BotMonitoringThread가 이 믹스인을 상속하고,
    __init_uptime_restart_scheduler__()로 초기화한 뒤
    _run_loop 내 스케줄 작업 블록에서 run_uptime_restart_schedule()을 호출한다.
    """

    COOLDOWN_MINUTES: int = 30

    def __init_uptime_restart_scheduler__(
        self,
        process_action_handler: object,
        monitor: object,
        bridge: object,
        main_loop: asyncio.AbstractEventLoop,
        *,
        threshold_hours: int = 24,
    ) -> None:
        """uptime 재시작 스케줄러 초기화.

        Args:
            process_action_handler: ProcessActionHandler 인스턴스
                (_simple_restart_core 메서드 보유).
            monitor: ProcessMonitor 인스턴스
                (find_processes() 메서드 보유).
            bridge: ZFlowBridge 인스턴스 (현재 미사용, 미래 확장용).
            main_loop: asyncio 메인 이벤트 루프
                (run_coroutine_threadsafe 디스패치 대상).
            threshold_hours: 재시작 uptime 임계값 (기본 24h).
        """
        self._uptime_restart_handler = process_action_handler
        self._uptime_restart_monitor = monitor
        self._uptime_restart_bridge = bridge
        self._uptime_restart_main_loop = main_loop
        self._uptime_restart_threshold = timedelta(hours=threshold_hours)
        self._uptime_restart_cooldown: dict[str, datetime] = {}

    def run_uptime_restart_schedule(self) -> list[str]:
        """모니터링 루프 매 사이클에서 호출되는 동기 스케줄러.

        실행 중인 전체 봇을 열거하고,
        auto_restart_24h=True + uptime>=threshold 조건을 만족하는 봇을
        asyncio.run_coroutine_threadsafe로 _simple_restart_core에 디스패치한다.

        Returns:
            이번 사이클에서 재시작을 디스패치한 봇 dir_name 목록.
        """
        if not hasattr(self, "_uptime_restart_handler"):
            return []
        if not self._uptime_restart_main_loop:
            return []

        try:
            process_tuples = self._uptime_restart_monitor.find_processes(force_refresh=False)  # pyright: ignore[reportAttributeAccessIssue]
        except Exception as e:
            logger.warning("[UPTIME_RESTART][ERROR] find_processes 실패: %s", e)
            return []

        dispatched: list[str] = []
        for proc, path in process_tuples:
            dir_name: str = path.parent.name
            try:
                if self._check_and_dispatch_uptime_restart(proc, path, dir_name):
                    dispatched.append(dir_name)
            except Exception as e:
                logger.warning(
                    "[UPTIME_RESTART][WARN] target=%s 체크 중 예외 발생 (스킵): %s",
                    dir_name,
                    e,
                )
                continue
        return dispatched

    def _check_and_dispatch_uptime_restart(
        self,
        proc: object,
        path: Path,
        dir_name: str,
    ) -> bool:
        """개별 봇에 대해 uptime 재시작 조건을 확인하고 디스패치한다.

        Returns:
            True if restart was dispatched, False otherwise.
        """
        # 1. session 로드 → auto_restart_24h 확인
        data_dir = path.parent
        session = SessionStore(data_dir).load()
        if session is None or session.auto_restart_24h is not True:
            return False

        # 2. uptime 확인
        uptime = process_uptime(proc)  # pyright: ignore[reportArgumentType]
        if uptime is None:
            return False
        if uptime < self._uptime_restart_threshold:
            return False

        # 3. 쿨다운 체크
        now = datetime.now()
        cooldown_time = self._uptime_restart_cooldown.get(dir_name)
        if cooldown_time is not None:
            if now - cooldown_time < timedelta(minutes=self.COOLDOWN_MINUTES):
                logger.debug(
                    "[UPTIME_RESTART][COOLDOWN] target=%s 쿨다운 중 (%.1f분 남음)",
                    dir_name,
                    (timedelta(minutes=self.COOLDOWN_MINUTES) - (now - cooldown_time)).total_seconds() / 60,
                )
                return False

        # 4. 쿨다운 기록
        self._uptime_restart_cooldown[dir_name] = now

        # 5. 디스패치
        logger.info(
            "[UPTIME_RESTART][DISPATCH] target=%s uptime=%.1fh",
            dir_name,
            uptime.total_seconds() / 3600,
        )
        _notifier = None
        if (
            self._application is not None  # pyright: ignore[reportAttributeAccessIssue]
            and self._application.bot is not None  # pyright: ignore[reportAttributeAccessIssue]
            and self._authorized_chat_id  # pyright: ignore[reportAttributeAccessIssue]
        ):
            _notifier = UptimeRestartNotifier(
                self._application.bot,  # pyright: ignore[reportAttributeAccessIssue]
                self._authorized_chat_id,  # pyright: ignore[reportAttributeAccessIssue]
                dir_name,
                uptime.total_seconds() / 3600,
            )
        asyncio.run_coroutine_threadsafe(
            self._uptime_restart_handler._simple_restart_core(dir_name, notifier=_notifier),  # pyright: ignore[reportAttributeAccessIssue]
            self._uptime_restart_main_loop,
        )
        return True
