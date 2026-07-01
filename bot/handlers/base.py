# -*- coding: utf-8 -*-
"""
Base Handler: 모든 핸들러의 공통 기반 클래스
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from z_pulse.bot.context import BotContext


class BaseHandler:
    """핸들러 기본 클래스"""

    def __init__(self, bot_context: 'BotContext'):
        self.ctx = bot_context