#!/usr/bin/env python3
"""
[DEPRECATED] 경제지표 캘린더 스케줄러

⚠️ 이 모듈은 Z-Pulse로 통합되었습니다.
   - 스케줄러 기능: bot/economic_scheduler.py의 EconomicSchedulerMixin으로 이동
   - BotMonitoringThread에서 자동 실행됨

독립 실행(daemon mode)은 하위 호환성을 위해 계속 지원되나, 권장하지 않습니다.
향후 버전에서 제거될 예정입니다.
"""

import warnings
import asyncio
import logging
import schedule
import time
import threading
from datetime import datetime
from z_pulse.features.economic_calendar import EconomicCalendarManager

# [DEPRECATED] 다른 모듈에서 import 시 경고 표시
warnings.warn(
    "economic_scheduler.py is deprecated. "
    "스케줄러 기능은 Z-Pulse에 통합되었습니다. "
    "독립 실행(daemon mode)만 지원됩니다.",
    DeprecationWarning,
    stacklevel=2
)

# 로깅 설정 - __main__에서만 basicConfig 호출 (다른 모듈에서 import 시 충돌 방지)
logger = logging.getLogger(__name__)


def _setup_logging():
    """독립 실행 시에만 호출되는 로깅 설정"""
    from z_pulse.utils.log_setup import setup_logging
    setup_logging()

class EconomicScheduler:
    """경제지표 캘린더 스케줄러"""
    
    def __init__(self):
        self.manager = EconomicCalendarManager()
        self.running = False
    
    async def update_job(self):
        """스케줄된 업데이트 작업"""
        try:
            logger.info("스케줄된 경제지표 업데이트 시작")
            results = await self.manager.update_calendar(days_ahead=14)
            logger.info(f"스케줄된 업데이트 완료: {results}")
            
            # 고중요도 이벤트 확인
            high_importance_events = self.manager.get_high_importance_events(3)
            if high_importance_events:
                logger.info(f"다가오는 고중요도 이벤트 {len(high_importance_events)}개 발견")
                for event in high_importance_events[:5]:
                    logger.info(f"  - {event['date']} {event['time']} | {event['event_name']}")
            
        except Exception as e:
            logger.error(f"스케줄된 업데이트 실패: {e}")
    
    def run_async_job(self, coro):
        """비동기 작업을 동기 스케줄러에서 실행"""
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(coro)
        except Exception as e:
            logger.error(f"비동기 작업 실행 실패: {e}")
        finally:
            loop.close()
    
    def start_scheduler(self):
        """스케줄러 시작"""
        logger.info("경제지표 캘린더 스케줄러 시작")

        # 스케줄 설정 (차단 회피: 하루 1회로 축소)
        # 매일 오전 6시에 업데이트 (미국 시장 개장 전)
        schedule.every().day.at("06:00").do(
            lambda: self.run_async_job(self.update_job())
        )

        # [REMOVED] 18:00 스케줄 - 차단 회피를 위해 제거
        # [REMOVED] 월요일 05:00 스케줄 - 06:00 스케줄로 통합

        self.running = True
        
        # 스케줄러 실행 루프
        while self.running:
            try:
                schedule.run_pending()
                time.sleep(60)  # 1분마다 체크
            except KeyboardInterrupt:
                logger.info("스케줄러 중단 요청 받음")
                break
            except Exception as e:
                logger.error(f"스케줄러 실행 중 오류: {e}")
                time.sleep(60)
        
        logger.info("경제지표 캘린더 스케줄러 종료")
    
    def stop_scheduler(self):
        """스케줄러 중지"""
        self.running = False
        schedule.clear()
        logger.info("스케줄러 중지 요청")

def run_scheduler_in_thread():
    """별도 스레드에서 스케줄러 실행"""
    scheduler = EconomicScheduler()
    scheduler.start_scheduler()

async def manual_update():
    """수동 업데이트 실행"""
    logger.info("수동 경제지표 업데이트 시작")
    manager = EconomicCalendarManager()
    
    try:
        results = await manager.update_calendar(days_ahead=14)
        print(f"업데이트 결과: {results}")
        
        # 다가오는 이벤트 조회
        upcoming_events = manager.get_upcoming_events(7)
        print(f"\n다가오는 경제지표 이벤트 ({len(upcoming_events)}개):")
        
        for event in upcoming_events[:15]:
            importance_icon = "🔴" if event['importance'] == '3' else "🟡" if event['importance'] == '2' else "🟢"
            print(f"{importance_icon} {event['date']} {event['time']} | {event['event_name']}")
        
        # 고중요도 이벤트만 별도 출력
        high_importance = manager.get_high_importance_events(7)
        if high_importance:
            print(f"\n🔴 고중요도 이벤트 ({len(high_importance)}개):")
            for event in high_importance:
                print(f"  - {event['date']} {event['time']} | {event['event_name']}")
        
    except Exception as e:
        logger.error(f"수동 업데이트 실패: {e}")
        raise

if __name__ == "__main__":
    import sys

    # 독립 실행 시에만 로깅 설정
    _setup_logging()

    # Deprecation 안내
    print("=" * 60)
    print("⚠️  [DEPRECATED] 이 스크립트는 더 이상 권장되지 않습니다.")
    print("   스케줄러 기능은 Z-Pulse에 통합되었습니다.")
    print("   setting.env의 ECONOMIC_CALENDAR_ENABLED=true로 설정하세요.")
    print("=" * 60)
    print()

    if len(sys.argv) > 1 and sys.argv[1] == "update":
        # 수동 업데이트 실행
        asyncio.run(manual_update())
    elif len(sys.argv) > 1 and sys.argv[1] == "daemon":
        # 데몬 모드로 스케줄러 실행
        try:
            scheduler = EconomicScheduler()
            scheduler.start_scheduler()
        except KeyboardInterrupt:
            logger.info("스케줄러 종료")
    else:
        print("사용법:")
        print("  python economic_scheduler.py update    # 수동 업데이트")
        print("  python economic_scheduler.py daemon    # 스케줄러 데몬 실행")
