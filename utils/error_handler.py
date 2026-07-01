"""
예외 처리 데코레이터
"""

import functools
import logging
import json
import asyncio
import httpx
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest

logger = logging.getLogger(__name__)

# ===== Transient 텔레그램/네트워크 예외 판별 =====

TRANSIENT_TELEGRAM_ERRORS: tuple[type[BaseException], ...] = (
    NetworkError,           # telegram.error.NetworkError (httpx.ReadError가 여기로 래핑됨)
    TimedOut,
    RetryAfter,             # NetworkError 비상속이므로 명시 포함
    httpx.ReadError,        # raw(미래핑) 방어
    httpx.ConnectError,
    httpx.TimeoutException,
    ConnectionError,
    asyncio.TimeoutError,
    TimeoutError,           # py3.11+ TimeoutError 별칭(무해 중복)
)

# 이 PTB 버전에서 BadRequest는 NetworkError의 서브클래스이지만
# 의미상 애플리케이션 오류(400 Bad Request)이므로 transient 판별에서 제외한다.
_NON_TRANSIENT_NETWORK_SUBCLASSES: tuple[type[BaseException], ...] = (
    BadRequest,
)


def is_transient_telegram_error(exc: BaseException | None) -> bool:
    """일시적(재시도 가능) 텔레그램/네트워크 예외 여부를 반환한다.

    PTB polling 루프는 NetworkError를 자체 백오프 재시도하므로,
    이 함수가 True를 반환하는 예외는 ERROR 수준 로깅 없이 WARNING으로 처리해도 안전하다.

    Args:
        exc: 검사할 예외 객체 (None이면 False 반환).

    Returns:
        True면 일시적 네트워크/타임아웃 예외, False면 진짜 애플리케이션 오류.
    """
    if exc is None:
        return False
    # BadRequest 등 의미상 비-transient 서브클래스를 먼저 제외한다.
    if isinstance(exc, _NON_TRANSIENT_NETWORK_SUBCLASSES):
        return False
    return isinstance(exc, TRANSIENT_TELEGRAM_ERRORS)


T = TypeVar('T')


def safe_file_operation(default_return: Optional[Any] = None) -> Callable:
    """
    파일 작업 예외 처리 데코레이터

    Args:
        default_return: 예외 발생 시 반환할 기본값

    Returns:
        데코레이터 함수

    Examples:
        >>> @safe_file_operation(default_return=[])
        >>> def read_lines(file_path):
        ...     with open(file_path) as f:
        ...         return f.readlines()
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except FileNotFoundError as e:
                logger.warning(f"File not found in {func.__name__}: {e}")
                return default_return
            except PermissionError as e:
                logger.error(f"Permission denied in {func.__name__}: {e}")
                return default_return
            except OSError as e:
                logger.error(f"OS error in {func.__name__}: {e}")
                return default_return
            except Exception as e:
                logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


def safe_json_operation(default_return: Optional[Any] = None) -> Callable:
    """
    JSON 작업 예외 처리 데코레이터

    Args:
        default_return: 예외 발생 시 반환할 기본값

    Returns:
        데코레이터 함수

    Examples:
        >>> @safe_json_operation(default_return={})
        >>> def load_config(file_path):
        ...     with open(file_path) as f:
        ...         return json.load(f)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error in {func.__name__}: {e}")
                return default_return
            except FileNotFoundError as e:
                logger.warning(f"File not found in {func.__name__}: {e}")
                return default_return
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


def safe_process_operation(default_return: Optional[Any] = None) -> Callable:
    """
    프로세스 작업 예외 처리 데코레이터

    Args:
        default_return: 예외 발생 시 반환할 기본값

    Returns:
        데코레이터 함수

    Examples:
        >>> @safe_process_operation(default_return=None)
        >>> def get_process_info(pid):
        ...     proc = psutil.Process(pid)
        ...     return proc.info
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            try:
                return func(*args, **kwargs)
            except (ProcessLookupError, PermissionError) as e:
                logger.warning(f"Process operation failed in {func.__name__}: {e}")
                return default_return
            except Exception as e:
                logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


async def safe_telegram_operation(
    func: Callable,
    *args,
    ignore_not_modified: bool = True,
    **kwargs
) -> bool:
    """
    텔레그램 API 작업 예외 처리 헬퍼

    Args:
        func: 실행할 함수
        *args: 함수 인자
        ignore_not_modified: "Message is not modified" 오류 무시 여부
        **kwargs: 함수 키워드 인자

    Returns:
        작업 성공 여부

    Examples:
        >>> await safe_telegram_operation(
        ...     query.edit_message_text,
        ...     text="Updated",
        ...     parse_mode='MarkdownV2'
        ... )
    """
    try:
        await func(*args, **kwargs)
        return True
    except Exception as e:
        error_msg = str(e)
        if ignore_not_modified and "Message is not modified" in error_msg:
            # 내용이 같아서 발생하는 에러는 무시
            return True
        logger.error(f"Telegram operation failed: {e}")
        return False


def with_retry(max_retries: int = 3, delay: float = 1.0) -> Callable:
    """
    재시도 데코레이터

    Args:
        max_retries: 최대 재시도 횟수
        delay: 재시도 간 대기 시간 (초)

    Returns:
        데코레이터 함수

    Examples:
        >>> @with_retry(max_retries=3, delay=2.0)
        >>> def unreliable_operation():
        ...     # 실패할 수 있는 작업
        ...     pass
    """
    import time

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries} failed for {func.__name__}: {e}. "
                            f"Retrying in {delay}s..."
                        )
                        time.sleep(delay)
                    else:
                        logger.error(
                            f"All {max_retries} attempts failed for {func.__name__}: {e}"
                        )
            raise last_exception
        return wrapper
    return decorator


# ===== Async 버전 데코레이터 (Phase 2.3) =====

def async_safe_file_operation(default_return: Optional[Any] = None) -> Callable:
    """
    비동기 파일 작업 예외 처리 데코레이터

    Args:
        default_return: 예외 발생 시 반환할 기본값

    Returns:
        데코레이터 함수

    Examples:
        >>> @async_safe_file_operation(default_return=[])
        >>> async def read_lines(file_path):
        ...     async with aiofiles.open(file_path) as f:
        ...         return await f.readlines()
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except FileNotFoundError as e:
                logger.warning(f"File not found in {func.__name__}: {e}")
                return default_return
            except PermissionError as e:
                logger.error(f"Permission denied in {func.__name__}: {e}")
                return default_return
            except OSError as e:
                logger.error(f"OS error in {func.__name__}: {e}")
                return default_return
            except Exception as e:
                logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


def async_safe_json_operation(default_return: Optional[Any] = None) -> Callable:
    """
    비동기 JSON 작업 예외 처리 데코레이터

    Args:
        default_return: 예외 발생 시 반환할 기본값

    Returns:
        데코레이터 함수

    Examples:
        >>> @async_safe_json_operation(default_return={})
        >>> async def load_config(file_path):
        ...     async with aiofiles.open(file_path) as f:
        ...         return json.loads(await f.read())
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except json.JSONDecodeError as e:
                logger.error(f"JSON decode error in {func.__name__}: {e}")
                return default_return
            except FileNotFoundError as e:
                logger.warning(f"File not found in {func.__name__}: {e}")
                return default_return
            except Exception as e:
                logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


def async_safe_process_operation(default_return: Optional[Any] = None) -> Callable:
    """
    비동기 프로세스 작업 예외 처리 데코레이터

    Args:
        default_return: 예외 발생 시 반환할 기본값

    Returns:
        데코레이터 함수

    Examples:
        >>> @async_safe_process_operation(default_return=None)
        >>> async def get_process_info(pid):
        ...     proc = psutil.Process(pid)
        ...     return proc.info
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            try:
                return await func(*args, **kwargs)
            except (ProcessLookupError, PermissionError) as e:
                logger.warning(f"Process operation failed in {func.__name__}: {e}")
                return default_return
            except Exception as e:
                logger.error(f"Unexpected error in {func.__name__}: {e}", exc_info=True)
                return default_return
        return wrapper
    return decorator


def async_with_retry(max_retries: int = 3, delay: float = 1.0) -> Callable:
    """
    비동기 재시도 데코레이터

    Args:
        max_retries: 최대 재시도 횟수
        delay: 재시도 간 대기 시간 (초)

    Returns:
        데코레이터 함수

    Examples:
        >>> @async_with_retry(max_retries=3, delay=2.0)
        >>> async def unreliable_operation():
        ...     # 실패할 수 있는 비동기 작업
        ...     pass
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries} failed for {func.__name__}: {e}. "
                            f"Retrying in {delay}s..."
                        )
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"All {max_retries} attempts failed for {func.__name__}: {e}"
                        )
            raise last_exception
        return wrapper
    return decorator
