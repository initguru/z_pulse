"""
bot/utils.py - 봇 유틸리티 함수

비동기 배치 처리 등 공통 유틸리티 함수를 제공합니다.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def run_batch_operations(
    items: list,
    operation_func,
    batch_size: int = 5,
    delay: float = 0.2
) -> int:
    """
    항목들을 배치 단위로 병렬 실행하여 성능을 향상시킵니다.

    Args:
        items: 처리할 항목 리스트
        operation_func: 각 항목을 처리할 비동기 함수 (인자: item, 반환: bool 성공여부)
        batch_size: 동시에 실행할 최대 개수 (세마포어)
        delay: 각 실행 사이의 미세 지연 (CPU 스파이크 방지)

    Returns:
        성공한 항목 수
    """
    if not items:
        return 0

    semaphore = asyncio.Semaphore(batch_size)
    success_count = 0

    async def _worker(item):
        nonlocal success_count
        async with semaphore:
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                if await operation_func(item):
                    success_count += 1
            except Exception as e:
                logger.error(f"Batch processing failed for {item}: {e}")

    tasks = [_worker(item) for item in items]
    await asyncio.gather(*tasks)
    return success_count
