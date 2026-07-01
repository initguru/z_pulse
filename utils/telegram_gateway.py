"""
Telegram API 전송 게이트웨이.

모든 짧은 Telegram 호출을 bounded queue와 timeout 뒤로 모아 메인 이벤트 루프가
느린 네트워크 I/O에 오래 붙잡히지 않도록 한다.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable, Optional
from weakref import WeakKeyDictionary

logger = logging.getLogger(__name__)


class TelegramPriority(IntEnum):
    USER_ACTION = 0
    DASHBOARD = 10
    BACKGROUND = 20
    AUTO_REFRESH = 30


CALLBACK_TIMEOUT = 1.0
DASHBOARD_TIMEOUT = 30.0
BACKGROUND_TIMEOUT = 30.0
FILE_UPLOAD_TIMEOUT = 120.0
DEFAULT_QUEUE_SIZE = 128
DEFAULT_WORKER_COUNT = 4
DEFAULT_EVIDENCE_SAMPLE_EVERY = 50
DEFAULT_QUEUE_LAG_WARN_SECONDS = 0.250


def _is_stale_callback_query(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "query is too old" in text
        or "response timeout expired" in text
        or "query id is invalid" in text
    )


@dataclass(order=True)
class _QueuedCall:
    priority: int
    seq: int
    call: Callable[[], Awaitable[Any]] = field(compare=False)
    timeout: float = field(compare=False)
    future: Optional[asyncio.Future] = field(compare=False, default=None)
    label: str = field(compare=False, default="telegram")
    drop_ok: bool = field(compare=False, default=False)
    enqueued_at: float = field(compare=False, default_factory=time.monotonic)
    queue_lag_warn_seconds: float | None = field(compare=False, default=None)


class _GatewayMetrics:
    def __init__(self) -> None:
        self.enqueued = 0
        self.dropped = 0
        self.timeouts = 0
        self.by_priority: dict[str, dict[str, int]] = {}

    def _bucket(self, priority: TelegramPriority | int) -> dict[str, int]:
        name = priority.name if isinstance(priority, TelegramPriority) else TelegramPriority(priority).name
        return self.by_priority.setdefault(
            name,
            {"enqueued": 0, "dropped": 0, "timeouts": 0},
        )

    def record_enqueued(self, priority: TelegramPriority | int) -> None:
        self.enqueued += 1
        self._bucket(priority)["enqueued"] += 1

    def record_dropped(self, priority: TelegramPriority | int) -> None:
        self.dropped += 1
        self._bucket(priority)["dropped"] += 1

    def record_timeout(self, priority: TelegramPriority | int) -> None:
        self.timeouts += 1
        self._bucket(priority)["timeouts"] += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "enqueued": self.enqueued,
            "dropped": self.dropped,
            "timeouts": self.timeouts,
            "by_priority": {
                priority: counts.copy()
                for priority, counts in self.by_priority.items()
            },
        }


class TelegramGateway:
    def __init__(
        self,
        maxsize: int = DEFAULT_QUEUE_SIZE,
        worker_count: int = DEFAULT_WORKER_COUNT,
        evidence_sample_every: int = DEFAULT_EVIDENCE_SAMPLE_EVERY,
        queue_lag_warn_seconds: float = DEFAULT_QUEUE_LAG_WARN_SECONDS,
    ):
        self._queue: asyncio.PriorityQueue[_QueuedCall] = asyncio.PriorityQueue(maxsize=maxsize)
        self._seq = itertools.count()
        self._worker_count = max(1, worker_count)
        self._worker_tasks: list[asyncio.Task] = []
        self._metrics = _GatewayMetrics()
        self._evidence_sample_every = max(1, evidence_sample_every)
        self._queue_lag_warn_seconds = queue_lag_warn_seconds
        self._completed_calls = 0

    def get_metrics(self) -> dict[str, Any]:
        return self._metrics.snapshot()

    def reset_metrics(self) -> None:
        self._metrics = _GatewayMetrics()

    def ensure_worker(self) -> None:
        self._worker_tasks = [
            task for task in self._worker_tasks if not task.done()
        ]
        missing = self._worker_count - len(self._worker_tasks)
        current_count = len(self._worker_tasks)
        for idx in range(missing):
            task_no = current_count + idx + 1
            self._worker_tasks.append(
                asyncio.create_task(
                    self._worker(),
                    name=f"telegram_gateway_worker_{task_no}",
                )
            )

    def full(self) -> bool:
        return self._queue.full()

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            queue_size = self._queue.qsize()
            queue_wait = time.monotonic() - item.enqueued_at
            warn_seconds = item.queue_lag_warn_seconds if item.queue_lag_warn_seconds is not None else self._queue_lag_warn_seconds
            self._completed_calls += 1
            sampled = self._completed_calls % self._evidence_sample_every == 0
            threshold_breach = queue_wait >= warn_seconds
            if threshold_breach:
                logger.warning(
                    "[GATEWAY][LAG] priority=%s label=%s queue_size=%d queue_wait=%.3fs threshold=%.3fs",
                    "INTERACTIVE" if item.priority == int(TelegramPriority.USER_ACTION) else TelegramPriority(item.priority).name,
                    item.label, queue_size, queue_wait, warn_seconds,
                )
            if sampled or threshold_breach:
                log = logger.warning if threshold_breach else logger.info
                log(
                    "[GATEWAY][EVIDENCE] label=%s priority=%s queue_size=%d queue_wait=%.3fs threshold=%.3fs sampled=%s threshold_breach=%s",
                    item.label,
                    TelegramPriority(item.priority).name,
                    queue_size,
                    queue_wait,
                    warn_seconds,
                    sampled,
                    threshold_breach,
                )
            try:
                try:
                    result = await asyncio.wait_for(item.call(), timeout=item.timeout)
                    if item.future and not item.future.done():
                        item.future.set_result(result)
                except asyncio.TimeoutError as exc:
                    self._metrics.record_timeout(item.priority)
                    if item.future and not item.future.done():
                        item.future.set_exception(exc)
                    else:
                        logger.warning("%s timeout", item.label)
                except Exception as exc:
                    if item.label == "answer_callback_query" and _is_stale_callback_query(exc):
                        logger.debug("오래된 callback query 응답 무시: %s", exc)
                        if item.future and not item.future.done():
                            item.future.set_result(None)
                        continue
                    if item.future and not item.future.done():
                        item.future.set_exception(exc)
                    else:
                        logger.warning("%s 실패: %s", item.label, exc)
            finally:
                self._queue.task_done()

    async def enqueue(
        self,
        call: Callable[[], Awaitable[Any]],
        *,
        priority: TelegramPriority = TelegramPriority.BACKGROUND,
        timeout: float = BACKGROUND_TIMEOUT,
        label: str = "telegram",
        wait_result: bool = True,
        drop_ok: bool = False,
        queue_lag_warn_seconds: float | None = None,
    ) -> Any:
        self.ensure_worker()

        if self._queue.full() and drop_ok:
            logger.debug("%s 큐 포화로 드롭", label)
            self._metrics.record_dropped(priority)
            return None

        loop = asyncio.get_running_loop()
        fut = loop.create_future() if wait_result else None
        item = _QueuedCall(
            priority=int(priority),
            seq=next(self._seq),
            call=call,
            timeout=timeout,
            future=fut,
            label=label,
            drop_ok=drop_ok,
            queue_lag_warn_seconds=queue_lag_warn_seconds,
        )
        try:
            self._queue.put_nowait(item)
            self._metrics.record_enqueued(priority)
        except asyncio.QueueFull:
            logger.warning("%s 큐 포화", label)
            self._metrics.record_dropped(priority)
            if fut and not fut.done():
                fut.set_result(None)
            return None

        if not fut:
            return None
        return await fut

    def enqueue_threadsafe(
        self,
        loop: asyncio.AbstractEventLoop,
        call: Callable[[], Awaitable[Any]],
        *,
        priority: TelegramPriority = TelegramPriority.BACKGROUND,
        timeout: float = BACKGROUND_TIMEOUT,
        label: str = "telegram",
        drop_ok: bool = True,
    ) -> None:
        if not loop.is_running():
            return

        def _schedule() -> None:
            asyncio.create_task(
                self.enqueue(
                    call,
                    priority=priority,
                    timeout=timeout,
                    label=label,
                    wait_result=False,
                    drop_ok=drop_ok,
                )
            )

        loop.call_soon_threadsafe(_schedule)

    async def send_message(
        self,
        bot,
        *,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = "MarkdownV2",
        priority: TelegramPriority = TelegramPriority.BACKGROUND,
        timeout: float = BACKGROUND_TIMEOUT,
        wait_result: bool = True,
        drop_ok: bool = False,
        **kwargs,
    ) -> Any:
        return await self.enqueue(
            lambda: bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, **kwargs),
            priority=priority,
            timeout=timeout,
            label="send_message",
            wait_result=wait_result,
            drop_ok=drop_ok,
        )

    async def edit_message_text(
        self,
        target,
        *,
        text: str,
        priority: TelegramPriority = TelegramPriority.DASHBOARD,
        timeout: float = DASHBOARD_TIMEOUT,
        wait_result: bool = True,
        drop_ok: bool = False,
        **kwargs,
    ) -> Any:
        return await self.enqueue(
            lambda: target.edit_message_text(text=text, **kwargs),
            priority=priority,
            timeout=timeout,
            label="edit_message_text",
            wait_result=wait_result,
            drop_ok=drop_ok,
        )

    async def bot_edit_message_text(self, bot, *, timeout: float = DASHBOARD_TIMEOUT, **kwargs) -> Any:
        return await self.enqueue(
            lambda: bot.edit_message_text(**kwargs),
            priority=TelegramPriority.DASHBOARD,
            timeout=timeout,
            label="bot_edit_message_text",
            drop_ok=True,
        )

    async def answer_callback_query(self, query, *args, **kwargs) -> Any:
        try:
            return await asyncio.wait_for(
                query.answer(*args, **kwargs),
                timeout=CALLBACK_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.debug("callback query 응답 timeout 무시")
            return None
        except Exception as exc:
            if _is_stale_callback_query(exc):
                logger.debug("오래된 callback query 응답 무시: %s", exc)
                return None
            raise

    async def delete_message(self, bot, *, chat_id: int, message_id: int) -> Any:
        return await self.enqueue(
            lambda: bot.delete_message(chat_id=chat_id, message_id=message_id),
            priority=TelegramPriority.DASHBOARD,
            timeout=DASHBOARD_TIMEOUT,
            label="delete_message",
        )


_gateways: WeakKeyDictionary[asyncio.AbstractEventLoop, TelegramGateway] = WeakKeyDictionary()


def get_telegram_gateway(loop: Optional[asyncio.AbstractEventLoop] = None) -> TelegramGateway:
    if loop is None:
        loop = asyncio.get_running_loop()
    gateway = _gateways.get(loop)
    if gateway is None:
        gateway = TelegramGateway()
        _gateways[loop] = gateway
    return gateway
