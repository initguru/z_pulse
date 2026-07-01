# -*- coding: utf-8 -*-
"""
Callback Router: 텔레그램 인라인 버튼 콜백 라우터
"""

import logging
from typing import Optional, TYPE_CHECKING

from telegram import Update, CallbackQuery
from telegram.ext import ContextTypes

if TYPE_CHECKING:
    from z_pulse.bot.context import BotContext
    from z_pulse.bot.handlers.commands import CommandHandlers
    from z_pulse.bot.handlers.dashboard import DashboardHandler

logger = logging.getLogger(__name__)


class CallbackRouter:
    """콜백 라우터 클래스"""

    def __init__(
        self,
        bot_context: 'BotContext',
        command_handlers: 'CommandHandlers',
        dashboard_handler: Optional['DashboardHandler'] = None
    ):
        """
        초기화

        Args:
            bot_context: BotContext 인스턴스
            command_handlers: CommandHandlers 인스턴스
            dashboard_handler: DashboardHandler 인스턴스 (선택적)
        """
        self.ctx = bot_context
        self.commands = command_handlers
        self.dashboard = dashboard_handler

    async def route(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """
        콜백 쿼리를 적절한 핸들러로 라우팅

        Args:
            update: 텔레그램 업데이트 객체
            context: 콘텍스트 객체

        Returns:
            bool: 라우팅 성공 여부
        """
        query: CallbackQuery = update.callback_query
        if not query:
            logger.warning("콜백 쿼리가 없습니다.")
            return False

        try:
            # 액션과 파라미터 분리
            data = query.data
            if not data:
                logger.warning("콜백 데이터가 비어 있습니다.")
                return False

            # ':'로 분리 (액션:파라미터)
            parts = data.split(':', 1)
            action = parts[0]
            param = parts[1] if len(parts) > 1 else None

            # 대시보드 관련 액션 처리
            if self.dashboard:
                # refresh_dashboard
                if action == "refresh_dashboard":
                    if hasattr(self.dashboard, 'trigger_refresh'):
                        self.dashboard.trigger_refresh(query)
                        await query.answer("🔄 대시보드를 새로고침합니다...")
                        return True
                    else:
                        logger.warning("DashboardHandler에 trigger_refresh 메서드가 없습니다.")

                # detail:{dir_name}
                elif action == "detail" and param:
                    if hasattr(self.dashboard, 'show_detail'):
                        await self.dashboard.show_detail(query, param)
                        return True
                    else:
                        logger.warning("DashboardHandler에 show_detail 메서드가 없습니다.")

                # kill:{dir_name}
                elif action == "kill" and param:
                    if hasattr(self.dashboard, 'kill_process'):
                        await self.dashboard.kill_process(query, param)
                        return True
                    else:
                        logger.warning("DashboardHandler에 kill_process 메서드가 없습니다.")

                # run:{dir_name}
                elif action == "run" and param:
                    if hasattr(self.dashboard, 'start_process'):
                        await self.dashboard.start_process(query, param)
                        return True
                    else:
                        logger.warning("DashboardHandler에 start_process 메서드가 없습니다.")

                # restart_all_confirm
                elif action == "restart_all_confirm":
                    if hasattr(self.dashboard, 'restart_all'):
                        await self.dashboard.restart_all(query)
                        return True
                    else:
                        logger.warning("DashboardHandler에 restart_all 메서드가 없습니다.")

                # clean_run:{dir_name}
                elif action == "clean_run" and param:
                    if hasattr(self.dashboard, 'clean_run'):
                        await self.dashboard.clean_run(query, param)
                        return True
                    else:
                        logger.warning("DashboardHandler에 clean_run 메서드가 없습니다.")

            # 대시보드 핸들러가 없거나 처리되지 않은 액션은 로깅
            logger.info(f"라우터에서 처리되지 않은 액션: {action}, 파라미터: {param}")
            await query.answer(f"⚠️ 처리할 수 없는 액션입니다: {action}")
            return False

        except Exception as e:
            logger.error(f"콜백 라우팅 중 오류: {e}")
            await query.answer("❌ 처리 중 오류가 발생했습니다.")
            return False