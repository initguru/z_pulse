"""
심볼 로더

z_flow 의존성 없이 SYMBOLS.cfg를 읽는 경량 구현.
z_flow/strategy/pair_trading/correlator.py의 load_symbols()와 동일 로직.
"""

import logging
import os

from .paths import REPO_ROOT

_SYMBOL_MAP = {"KBONK": "1000BONK", "KPEPE": "1000PEPE", "KSHIB": "1000SHIB"}
_DEFAULT_SYMBOL_FILE = REPO_ROOT / "z_flow" / "data" / "SYMBOLS.cfg"


def load_symbols() -> list[str]:
    """SYMBOLS.cfg에서 심볼 목록을 읽어 매핑 후 반환.

    파일이 없으면(z_flow 미설치 포함) 경고 로그 후 빈 리스트 반환.
    """
    symbol_file = os.environ.get("WS_KLINE_SYMBOL_FILE", str(_DEFAULT_SYMBOL_FILE))
    try:
        with open(symbol_file, "r", encoding="utf-8") as f:
            symbols = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except FileNotFoundError:
        logging.warning("[symbols] SYMBOLS.cfg not found: %s. Returning empty list.", symbol_file)
        return []
    return [_SYMBOL_MAP.get(s, s) for s in symbols]
