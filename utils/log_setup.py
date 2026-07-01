"""
프로젝트 표준 로깅 설정 유틸리티.

모든 엔트리포인트(telegram_bot.py, z_flow/run_bot.py 등)가 동일한
로그 포맷을 사용하도록 공통 setup 함수를 제공한다.

표준 포맷:
    [2026-03-25 14:30:01]      z_flow.core.slot_runtime | INFO     | [ZFLOW][BOOT] started
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
    [timestamp]            name (26-char right-aligned) | level (8-char left-aligned) | message
"""

from __future__ import annotations

import logging
import sys

# 프로젝트 표준 로그 포맷 (telegram_bot.py 기준)
LOG_FORMAT = "[%(asctime)s] %(name)26s | %(levelname)-8s | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(*, level: int = logging.INFO, force: bool = False) -> None:
    """프로젝트 표준 stdout 로깅을 구성한다.

    Parameters
    ----------
    level:
        루트 로거 레벨. 기본 ``logging.INFO``.
    force:
        ``True`` 이면 기존 핸들러를 제거하고 재설정 (``logging.basicConfig(force=True)``).
    """
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATEFMT)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logging.basicConfig(level=level, handlers=[stream_handler], force=force)
