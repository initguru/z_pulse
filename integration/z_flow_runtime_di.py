"""z_flow 런타임 DI 어댑터 — z_pulse 구현체를 z_flow Protocol에 주입."""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class ZPulseBotStateChecker:
    """BotStateCheckerProtocol 구현체 — z_pulse PairBotState 기반."""

    def is_assignable(self, dir_path: Path) -> bool:
        from z_pulse.monitoring.bot_state import PairBotState, resolve_pair_bot_state
        from z_pulse.monitoring.session_store import SessionStore
        try:
            session = SessionStore(dir_path).load()
            state = resolve_pair_bot_state(dir_path, process_running=False, session=session)
            return state in {PairBotState.WAITING, PairBotState.WAITING_WITH_WARNING}
        except Exception as e:
            logger.warning(f"[DI][BotState] is_assignable failed for {dir_path}: {e}")
            return False

    def is_normal_exit(self, dir_path: Path) -> bool:
        from z_pulse.monitoring.bot_state import PairBotState, resolve_pair_bot_state
        from z_pulse.monitoring.session_store import SessionStore
        try:
            session = SessionStore(dir_path).load()
            state = resolve_pair_bot_state(dir_path, process_running=False, session=session)
            return state in {PairBotState.WAITING, PairBotState.WAITING_WITH_WARNING, PairBotState.MANUAL_STOP}
        except Exception as e:
            logger.warning(f"[DI][BotState] is_normal_exit failed for {dir_path}: {e}")
            return False

    def is_manual_stop(self, dir_path: Path) -> bool:
        from z_pulse.monitoring.bot_state import PairBotState, resolve_pair_bot_state
        from z_pulse.monitoring.session_store import SessionStore
        try:
            session = SessionStore(dir_path).load()
            state = resolve_pair_bot_state(dir_path, process_running=False, session=session)
            return state is PairBotState.MANUAL_STOP
        except Exception as e:
            logger.warning(f"[DI][BotState] is_manual_stop failed for {dir_path}: {e}")
            return False


def wire_z_flow_bot_state_checker() -> None:
    """pair_automation._bot_state_checker에 ZPulseBotStateChecker 주입."""
    try:
        from z_flow.strategy.pair_trading.pair_automation import register_bot_state_checker  # pyright: ignore[reportMissingImports]
        register_bot_state_checker(ZPulseBotStateChecker())
        logger.info("[DI] ZPulseBotStateChecker registered")
    except Exception as e:
        logger.warning(f"[DI] BotStateChecker 등록 실패 (z_flow 미설치?): {e}")


def wire_z_flow_alert_send_fn() -> None:
    """pair_automation._alert_send_fn에 z_pulse safe_send_message 주입."""
    try:
        from z_flow.strategy.pair_trading.pair_automation import register_alert_send_fn  # pyright: ignore[reportMissingImports]
        from z_pulse.utils.async_helpers import safe_send_message
        register_alert_send_fn(safe_send_message)
        logger.info("[DI] alert send_fn registered")
    except Exception as e:
        logger.warning(f"[DI] alert send_fn 등록 실패 (z_flow 미설치?): {e}")



