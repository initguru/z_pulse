"""
비동기 실행 헬퍼 유틸리티
"""

import asyncio
import httpx
import logging
import re
from telegram.error import BadRequest, TimedOut
from typing import Coroutine, Optional, Any
from z_pulse.utils.telegram_gateway import (
    BACKGROUND_TIMEOUT,
    TelegramPriority,
    get_telegram_gateway,
)

logger = logging.getLogger(__name__)

_TELEGRAM_SEND_RETRYABLE = (
    httpx.ReadError,
    httpx.ConnectError,
    httpx.TimeoutException,
    ConnectionError,
    asyncio.TimeoutError,
    TimedOut,
)


def safe_run_async(
    coro: Coroutine, main_loop: Optional[asyncio.AbstractEventLoop] = None
) -> Optional[asyncio.Future]:
    """
    안전한 비동기 실행 헬퍼

    메인 이벤트 루프가 실행 중이면 run_coroutine_threadsafe를 사용하고,
    그렇지 않으면 asyncio.run을 사용합니다.

    Args:
        coro: 실행할 코루틴
        main_loop: 메인 이벤트 루프 (선택사항)

    Returns:
        메인 루프가 있으면 Future 객체, 없으면 None

    Examples:
        >>> async def my_task():
        ...     await asyncio.sleep(1)
        ...     return "Done"
        >>> future = safe_run_async(my_task(), main_loop)
    """
    try:
        if main_loop and main_loop.is_running():
            return asyncio.run_coroutine_threadsafe(coro, main_loop)
        else:
            # 메인 루프가 없거나 실행 중이 아니면 직접 실행
            # 주의: 이 경우 현재 스레드를 블록할 수 있음
            return asyncio.run(coro)
    except Exception as e:
        logger.error(f"비동기 실행 중 오류 발생: {e}")
        return None


async def safe_send_message(
    bot, chat_id: int, text: str, parse_mode: str = "MarkdownV2", **kwargs
) -> bool:
    """
    텔레그램 메시지 안전 전송

    Args:
        bot: 텔레그램 봇 인스턴스
        chat_id: 채팅 ID
        text: 메시지 텍스트
        parse_mode: 파싱 모드 (기본값: MarkdownV2)
        **kwargs: 추가 인자

    Returns:
        전송 성공 여부
    """
    message = await safe_send_message_with_result(
        bot, chat_id, text, parse_mode=parse_mode, **kwargs
    )
    return message is not None


async def safe_send_message_with_result(
    bot,
    chat_id: int,
    text: str,
    parse_mode: str = "MarkdownV2",
    *,
    priority: TelegramPriority = TelegramPriority.BACKGROUND,
    timeout: float = min(BACKGROUND_TIMEOUT, 12.0),
    max_retries: int = 2,
    base_delay: float = 0.5,
    **kwargs,
) -> Optional[Any]:
    """
    텔레그램 메시지 안전 전송 (Message 객체 반환)

    대시보드 등에서 메시지 추적이 필요한 경우 사용
    (예: 마지막 메시지 ID 저장, 메시지 수정 등)

    Args:
        bot: 텔레그램 봇 인스턴스
        chat_id: 채팅 ID
        text: 메시지 텍스트
        parse_mode: 파싱 모드 (기본값: MarkdownV2)
        **kwargs: 추가 인자 (reply_markup 등)

    Returns:
        성공 시 Message 객체, 실패 시 None

    Examples:
        >>> sent_message = await safe_send_message_with_result(
        ...     bot, chat_id, "Hello", reply_markup=keyboard
        ... )
        >>> if sent_message:
        ...     message_id = sent_message.message_id
    """
    retries = max(1, int(max_retries))
    for attempt in range(retries):
        try:
            message = await get_telegram_gateway().send_message(
                bot,
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                priority=priority,
                timeout=timeout,
                **kwargs,
            )
            return message
        except _TELEGRAM_SEND_RETRYABLE as e:
            if attempt >= retries - 1:
                logger.warning(
                    "메시지 전송 재시도 소진: %s: %s",
                    type(e).__name__,
                    e,
                )
                return None
            delay = base_delay * (2**attempt)
            logger.warning(
                "메시지 전송 일시 오류 (재시도 %s/%s, %.1fs 대기): %s: %s",
                attempt + 1,
                retries,
                delay,
                type(e).__name__,
                e,
            )
            await asyncio.sleep(delay)
        except Exception as e:
            logger.exception("메시지 전송 실패: %s: %s", type(e).__name__, e)
            return None
    return None


async def safe_edit_message(
    query, text: str, reply_markup=None, parse_mode: str = "MarkdownV2"
) -> bool:
    """
    텔레그램 메시지 안전 수정

    Args:
        query: 콜백 쿼리 객체
        text: 메시지 텍스트
        reply_markup: 키보드 마크업 (선택사항)
        parse_mode: 파싱 모드 (기본값: MarkdownV2)

    Returns:
        수정 성공 여부
    """
    try:
        await get_telegram_gateway().edit_message_text(
            query,
            text=text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        return True
    except Exception as e:
        # "Message is not modified" 오류는 무시
        if "Message is not modified" not in str(e):
            logger.error(f"메시지 수정 실패: {e}")
        return False


async def safe_send_html_message(
    bot,
    chat_id: int,
    text: str,
    reply_markup=None,
    **kwargs,
) -> bool:
    """
    [트레이딩 및 에러 알림 전용] 텔레그램 메시지 안전 전송 (HTML 모드 고정)

    동적 데이터(코인명, 외부 에러 메시지, 하이픈·슬래싱·특수기호 포함 문자열)로 인해
    MarkdownV2 파싱 에러가 발생하는 것을 원천 차단하기 위해 HTML 모드로 고정 발송합니다.

    사용 도메인:
      - pair_trading/: 신규 할당, 발산 감지, 스마트 필터 알림
      - 외부 API 응답이 포함된 에러 리포트

    Args:
        bot: telegram.Bot 인스턴스
        chat_id: 발송 대상 채팅 ID
        text: HTML 태그가 포함된 메시지 본문
        reply_markup: 인라인 키보드 등 (선택)
        **kwargs: bot.send_message에 전달될 추가 인자

    Returns:
        발송 성공 여부 (bool)
    """
    # Truncate long messages (original logic preserved)
    if len(text) > 1024:
        text = text[:1024]

    max_retries = 3
    base_delay = 1.0

    for attempt in range(max_retries):
        try:
            await get_telegram_gateway().send_message(
                bot,
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
                priority=TelegramPriority.BACKGROUND,
                timeout=BACKGROUND_TIMEOUT,
                **kwargs,
            )
            return True
        except BadRequest as e:
            error_msg = str(e).lower()
            if "parse" in error_msg or "can't parse" in error_msg:
                # Try plaintext fallback
                plain_text = re.sub(r"<[^>]+>", "", text)
                logger.warning(f"HTML parse failed, trying plaintext fallback")
                try:
                    await get_telegram_gateway().send_message(
                        bot,
                        chat_id=chat_id,
                        text=plain_text,
                        parse_mode=None,
                        reply_markup=reply_markup,
                        priority=TelegramPriority.BACKGROUND,
                        timeout=BACKGROUND_TIMEOUT,
                        **kwargs,
                    )
                    return True
                except (httpx.ReadError, httpx.ConnectError, httpx.TimeoutException,
                        ConnectionError, asyncio.TimeoutError) as e2:
                    # Transient error on plaintext - retry with same logic
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.warning(f"Plaintext transient error, retrying in {delay}s: {e2}")
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.error(f"Plaintext all attempts failed: {e2}")
                        return False
                except BadRequest:
                    # Permanent error on plaintext too - don't retry
                    logger.error(f"Plaintext also failed with BadRequest")
                    return False
            else:
                # Other BadRequest - don't retry
                logger.warning(f"BadRequest (not parse error): {e}")
                return False
        except (
            httpx.ReadError,
            httpx.ConnectError,
            httpx.TimeoutException,
            ConnectionError,
            asyncio.TimeoutError,
        ) as e:
            # Transient error - retry with backoff
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt)  # 1s, 2s, 4s
                logger.warning(
                    f"Transient error on attempt {attempt + 1}, retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"All {max_retries} attempts failed: {e}")
                return False
        except Exception as e:
            # Unknown error - log and fail
            logger.error(f"Unexpected error: {e}")
            return False

    return False
