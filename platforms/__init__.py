"""
Platform 모듈

OS별 로직을 추상화하여 플랫폼 독립적인 인터페이스 제공

사용법:
    from z_pulse.platforms import get_platform_handler

    handler = get_platform_handler()
    handler.arrange_windows(target_keywords, process_name)
    await handler.cleanup_terminal(filter_keyword)
    await handler.start_bot_process(dir_name, bot_path, executable)
"""

import platform as sys_platform
import logging
from typing import Optional

from .base import PlatformHandler

logger = logging.getLogger(__name__)

# 싱글톤 인스턴스 캐싱
_handler_instance: Optional[PlatformHandler] = None


def get_platform_handler() -> PlatformHandler:
    """
    현재 OS에 맞는 플랫폼 핸들러 반환 (팩토리 패턴 + 싱글톤)

    Returns:
        PlatformHandler: Windows 또는 macOS 핸들러 인스턴스

    Raises:
        NotImplementedError: 지원하지 않는 플랫폼인 경우
    """
    global _handler_instance

    if _handler_instance is not None:
        return _handler_instance

    system = sys_platform.system()

    if system == "Windows":
        from .windows import WindowsHandler
        _handler_instance = WindowsHandler()
        logger.info("Windows 플랫폼 핸들러 초기화")
    elif system == "Darwin":
        from .macos import MacOSHandler
        _handler_instance = MacOSHandler()
        logger.info("macOS 플랫폼 핸들러 초기화")
    else:
        raise NotImplementedError(f"플랫폼 '{system}'은(는) 지원되지 않습니다.")

    return _handler_instance


def reset_handler() -> None:
    """
    플랫폼 핸들러 인스턴스 초기화 (테스트용)
    """
    global _handler_instance
    _handler_instance = None


def is_windows() -> bool:
    """현재 플랫폼이 Windows인지 확인"""
    return sys_platform.system() == "Windows"


def is_macos() -> bool:
    """현재 플랫폼이 macOS인지 확인"""
    return sys_platform.system() == "Darwin"


__all__ = [
    'PlatformHandler',
    'get_platform_handler',
    'reset_handler',
    'is_windows',
    'is_macos',
]
