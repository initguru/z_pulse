"""z_pulse.integration — z_flow 파일시스템 IPC 연동 레이어"""

from __future__ import annotations

from typing import Any

__all__ = ["PairTradingConfigError", "ZFlowBridge"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from .z_flow_bridge import PairTradingConfigError, ZFlowBridge

        return {"PairTradingConfigError": PairTradingConfigError, "ZFlowBridge": ZFlowBridge}[name]
    raise AttributeError(name)
