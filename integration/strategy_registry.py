"""Bridge-neutral accessors for Z-Flow strategy registry metadata."""

from __future__ import annotations


def is_pair_trading_type(trading_type: str | None) -> bool:
    """Return whether ``trading_type`` resolves to the pair-trading family."""
    from z_pulse.integration.z_flow_bridge import ZFlowBridge  # lazy — avoids circular

    return ZFlowBridge.is_pair_trading_type(trading_type)
