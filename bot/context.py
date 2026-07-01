# bot/context.py
# -*- coding: utf-8 -*-
from dataclasses import dataclass
from typing import Optional, Callable, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from z_pulse.monitoring.process_monitor import ProcessMonitor
    from z_pulse.features.process_control import ProcessController
    from z_pulse.platforms.base import PlatformHandler
    from z_pulse.bot.keyboard_builder import KeyboardBuilder
    from z_pulse.features.economic_calendar import EconomicCalendar
    from telegram import Update, Message
    from telegram.ext import ContextTypes

@dataclass
class BotContext:
    """
    봇의 상태와 주요 컴포넌트들을 공유하는 컨텍스트 클래스.
    순환 참조를 방지하기 위해 각 컴포넌트는 초기화 시점에 주입됩니다.
    """
    # 핵심 컴포넌트
    process_monitor: Optional['ProcessMonitor'] = None
    process_controller: Optional['ProcessController'] = None
    platform_handler: Optional['PlatformHandler'] = None
    keyboard_builder: Optional['KeyboardBuilder'] = None
    economic_calendar: Optional['EconomicCalendar'] = None

    # 상태 변수
    last_chat_id: Optional[int] = None
    dashboard_message_id: Optional[int] = None
    dashboard_chat_id: Optional[int] = None
    
    # 콜백 함수 (의존성 주입용)
    # 갱신 로직을 핸들러 내부로 숨기기 위해 Callable로 정의
    update_dashboard_callback: Optional[Callable] = None
    trigger_dashboard_refresh_callback: Optional[Callable] = None
    