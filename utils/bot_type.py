"""봇 타입 판별 유틸리티."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


def is_variational_bot(dir_or_setting_env: Union[Path, str]) -> bool:
    """VARIATIONAL 거래소 봇인지 판별한다.

    Args:
        dir_or_setting_env: 봇 디렉토리 경로 또는 setting.env 파일 경로.

    Returns:
        TRADING_TYPE이 VARIATIONAL 거래소에 해당하면 True, 아니면 False.
        파싱 실패·FileNotFoundError 등 모든 예외에서 False를 반환한다 (오탐 방지).
    """
    try:
        from z_pulse.config.env_handler import EnvConfigHandler
        from z_pulse.integration.z_flow_bridge import ZFlowBridge

        parsed = EnvConfigHandler.parse(dir_or_setting_env)
        tt = str(parsed.get("TRADING_TYPE") or "").strip()
        exchange = (ZFlowBridge.get_exchange_for_trading_type(tt) or "").strip().upper()
        return exchange == "VARIATIONAL"
    except Exception as exc:
        logger.debug(
            "is_variational_bot: 판별 실패 (False 반환) path=%r error=%s",
            dir_or_setting_env,
            exc,
        )
        return False
