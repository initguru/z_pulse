"""
EconomicSchedulerMixin - BotMonitoringThread에 통합되는 경제지표 스케줄러

기존 economic_scheduler.py의 스케줄링 로직을 믹스인 형태로 재구성하여
BotMonitoringThread의 모니터링 루프 내에서 동작하도록 함.
"""

import asyncio
import logging
import schedule
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from z_pulse.features.economic_calendar import EconomicCalendarManager

logger = logging.getLogger(__name__)


class EconomicSchedulerMixin:
    """
    BotMonitoringThread에 믹스인으로 추가되는 경제지표 스케줄러

    별도 스레드/프로세스 없이 모니터링 루프 내에서 schedule 라이브러리를 사용하여
    지정된 시간에 경제지표 업데이트를 수행합니다.

    Usage:
        class BotMonitoringThread(EconomicSchedulerMixin):
            def __init__(self, ..., economic_update_hour=6, economic_enabled=True):
                ...
                self.__init_economic_scheduler__(
                    economic_manager=economic_manager,
                    update_hour=economic_update_hour,
                    enabled=economic_enabled,
                )

            def _run_loop(self):
                while ...:
                    ...
                    self.run_economic_schedule_pending()
    """

    # 믹스인 속성 (타입 힌트용)
    _economic_manager: Optional["EconomicCalendarManager"]
    _economic_enabled: bool
    _economic_update_hour: int
    _economic_scheduler: schedule.Scheduler

    def __init_economic_scheduler__(
        self,
        economic_manager: Optional["EconomicCalendarManager"],
        update_hour: int = 6,
        enabled: bool = True,
    ) -> None:
        """
        경제지표 스케줄러 초기화

        Args:
            economic_manager: EconomicCalendarManager 인스턴스
            update_hour: 일일 업데이트 시간 (0-23, 기본값 6시)
            enabled: 스케줄러 활성화 여부
        """
        self._economic_manager = economic_manager
        self._economic_enabled = enabled and economic_manager is not None
        self._economic_update_hour = update_hour
        self._economic_bootstrap_checked = False

        # 독립 스케줄러 인스턴스 (글로벌 schedule과 분리)
        self._economic_scheduler = schedule.Scheduler()

        if self._economic_enabled:
            self._setup_economic_schedule()
            logger.info(
                f"✅ 경제지표 스케줄러 초기화 완료 (매일 {update_hour:02d}:00 업데이트)"
            )
        else:
            logger.info("ℹ️ 경제지표 스케줄러 비활성화됨")

    def _setup_economic_schedule(self) -> None:
        """
        schedule 라이브러리를 사용하여 일일 업데이트 스케줄 설정

        차단 회피를 위해 하루 1회만 실행 (기본 06:00)
        """
        update_time = f"{self._economic_update_hour:02d}:00"

        self._economic_scheduler.every().day.at(update_time).do(
            self._trigger_economic_update
        )

        logger.info(f"📅 경제지표 업데이트 스케줄 설정: 매일 {update_time}")

    def _trigger_economic_update(self) -> None:
        """
        동기 → 비동기 브릿지

        schedule 라이브러리는 동기 기반이므로, 새 이벤트 루프를 생성하여
        비동기 업데이트 함수를 실행합니다.
        """
        logger.info("🔄 스케줄된 경제지표 업데이트 트리거됨")

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._async_economic_update())
            finally:
                loop.close()
        except Exception as e:
            logger.error(f"경제지표 업데이트 트리거 실패: {e}")

    async def bootstrap_economic_update_if_needed(self) -> None:
        """시작 시 비어 있는 경제지표 상태를 1회 복구한다."""
        if self._economic_bootstrap_checked:
            return

        self._economic_bootstrap_checked = True

        if not self._economic_enabled:
            logger.info("경제지표 부트스트랩을 건너뜁니다. 경제지표 스케줄러가 비활성화되었습니다.")
            return

        if not self._economic_manager:
            logger.info("경제지표 부트스트랩을 건너뜁니다. economic_manager가 없습니다.")
            return

        try:
            needs_bootstrap = self._economic_manager.needs_bootstrap_update(7)
            if not needs_bootstrap:
                logger.info("경제지표 부트스트랩이 필요하지 않습니다.")
                return

            logger.info("경제지표 부트스트랩 업데이트를 실행합니다.")
            results = await self._economic_manager.update_calendar(days_ahead=14)
            logger.info(f"경제지표 부트스트랩 업데이트 완료: {results}")
        except Exception as e:
            logger.error(f"경제지표 부트스트랩 업데이트 실패: {e}")

    async def _async_economic_update(self) -> None:
        """
        실제 경제지표 업데이트 수행

        EconomicCalendarManager.update_calendar()를 호출하여
        최신 경제지표 데이터를 수집합니다.
        """
        if not self._economic_manager:
            logger.warning("economic_manager가 없어 업데이트를 건너뜁니다.")
            return

        try:
            logger.info("📊 스케줄된 경제지표 업데이트 시작")
            results = await self._economic_manager.update_calendar(days_ahead=14)
            logger.info(f"✅ 스케줄된 업데이트 완료: {results}")

            # 고중요도 이벤트 로깅
            high_importance_events = self._economic_manager.get_high_importance_events(3)
            if high_importance_events:
                logger.info(
                    f"🔴 다가오는 고중요도 이벤트 {len(high_importance_events)}개 발견"
                )
                for event in high_importance_events[:5]:
                    logger.info(
                        f"   - {event['date']} {event['time']} | {event['event_name']}"
                    )

            send_scheduled_message = getattr(
                self, "send_scheduled_economic_update", None
            )
            if send_scheduled_message:
                await send_scheduled_message()

        except Exception as e:
            logger.error(f"스케줄된 경제지표 업데이트 실패: {e}")

    def run_economic_schedule_pending(self) -> None:
        """
        모니터링 루프에서 호출되는 스케줄 체크 함수

        schedule.run_pending()을 호출하여 예약된 작업이 있으면 실행합니다.
        모니터링 루프의 각 사이클에서 호출되어야 합니다.
        """
        if not self._economic_enabled:
            return

        try:
            self._economic_scheduler.run_pending()
        except Exception as e:
            logger.error(f"경제지표 스케줄 실행 중 오류: {e}")

    def get_next_economic_update_time(self) -> Optional[datetime]:
        """
        다음 경제지표 업데이트 예정 시간 반환

        Returns:
            다음 업데이트 시간 또는 None (비활성화 시)
        """
        if not self._economic_enabled:
            return None

        jobs = self._economic_scheduler.get_jobs()
        if jobs:
            return jobs[0].next_run
        return None

    async def cleanup_economic_scheduler(self) -> None:
        """
        경제 캘린더 리소스 정리

        봇 종료 시 httpx.AsyncClient 등 리소스를 안전하게 해제
        """
        if self._economic_manager:
            try:
                await self._economic_manager.close()
                logger.info("Economic scheduler resources cleaned up")
            except Exception as e:
                logger.error(f"Economic scheduler cleanup error: {e}")
