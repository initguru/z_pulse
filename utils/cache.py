"""
캐싱 유틸리티 모듈
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Optional, TypeVar, Generic

T = TypeVar('T')


class SmartCache(Generic[T]):
    """LRU + TTL 하이브리드 캐시

    특징:
    - LRU (Least Recently Used): 오래된 항목 자동 제거
    - TTL (Time To Live): 시간 경과 후 자동 만료
    - Thread-safe: 멀티스레드 환경에서 안전하게 사용 가능

    Args:
        maxsize: 최대 캐시 항목 수 (기본값: 128)
        ttl: 캐시 항목 유효 시간(초) (기본값: 2.0)
    """

    def __init__(self, maxsize: int = 128, ttl: float = 2.0):
        self._cache: OrderedDict[Any, T] = OrderedDict()
        self._timestamps: dict[Any, float] = {}
        self._maxsize = maxsize
        self._ttl = ttl
        self._lock = threading.RLock()
        self._hit_count = 0
        self._miss_count = 0

    @property
    def maxsize(self) -> int:
        """최대 캐시 크기"""
        return self._maxsize

    @property
    def ttl(self) -> float:
        """캐시 TTL (초)"""
        return self._ttl

    @ttl.setter
    def ttl(self, value: float) -> None:
        """TTL 동적 변경"""
        with self._lock:
            self._ttl = value

    def get(self, key: Any, default: Optional[T] = None) -> Optional[T]:
        """캐시에서 값을 가져옴

        Args:
            key: 캐시 키
            default: 키가 없거나 만료된 경우 반환할 기본값

        Returns:
            캐시된 값 또는 기본값
        """
        with self._lock:
            if key not in self._cache:
                self._miss_count += 1
                return default

            # TTL 체크
            if time.time() - self._timestamps[key] > self._ttl:
                del self._cache[key]
                del self._timestamps[key]
                self._miss_count += 1
                return default

            # LRU: 최근 사용된 항목을 끝으로 이동
            self._cache.move_to_end(key)
            self._hit_count += 1
            return self._cache[key]

    def set(self, key: Any, value: T) -> None:
        """캐시에 값을 저장

        Args:
            key: 캐시 키
            value: 저장할 값
        """
        with self._lock:
            current_time = time.time()

            if key in self._cache:
                self._cache.move_to_end(key)

            self._cache[key] = value
            self._timestamps[key] = current_time

            # maxsize 초과 시 가장 오래된 항목 제거
            while len(self._cache) > self._maxsize:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
                del self._timestamps[oldest]

    def invalidate(self, key: Any) -> bool:
        """특정 키의 캐시를 무효화

        Args:
            key: 무효화할 캐시 키

        Returns:
            키가 존재했으면 True, 아니면 False
        """
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                del self._timestamps[key]
                return True
            return False

    def clear(self) -> None:
        """모든 캐시 항목 제거"""
        with self._lock:
            self._cache.clear()
            self._timestamps.clear()

    def cleanup_expired(self) -> int:
        """만료된 모든 항목 정리

        Returns:
            제거된 항목 수
        """
        with self._lock:
            current_time = time.time()
            expired_keys = [
                key for key, timestamp in self._timestamps.items()
                if current_time - timestamp > self._ttl
            ]

            for key in expired_keys:
                del self._cache[key]
                del self._timestamps[key]

            return len(expired_keys)

    def __contains__(self, key: Any) -> bool:
        """캐시에 키가 존재하고 유효한지 확인 (TTL 체크 포함)"""
        with self._lock:
            if key not in self._cache:
                return False
            if time.time() - self._timestamps[key] > self._ttl:
                del self._cache[key]
                del self._timestamps[key]
                return False
            return True

    def __len__(self) -> int:
        """현재 캐시된 항목 수 (만료된 항목 포함)"""
        return len(self._cache)

    @property
    def stats(self) -> dict:
        """캐시 통계 정보"""
        with self._lock:
            total = self._hit_count + self._miss_count
            hit_rate = (self._hit_count / total * 100) if total > 0 else 0
            return {
                'size': len(self._cache),
                'maxsize': self._maxsize,
                'ttl': self._ttl,
                'hits': self._hit_count,
                'misses': self._miss_count,
                'hit_rate': f"{hit_rate:.1f}%"
            }


class SingleValueCache(Generic[T]):
    """단일 값 캐싱을 위한 간단한 TTL 캐시

    ProcessMonitor의 프로세스 목록처럼 단일 값만 캐싱할 때 사용

    Args:
        ttl: 캐시 유효 시간(초) (기본값: 2.0)
    """

    def __init__(self, ttl: float = 2.0):
        self._value: Optional[T] = None
        self._timestamp: float = 0
        self._ttl = ttl
        self._lock = threading.RLock()

    @property
    def ttl(self) -> float:
        """캐시 TTL (초)"""
        return self._ttl

    @ttl.setter
    def ttl(self, value: float) -> None:
        """TTL 동적 변경"""
        with self._lock:
            self._ttl = value

    def get(self, default: Optional[T] = None) -> Optional[T]:
        """캐시된 값을 가져옴

        Args:
            default: 캐시가 없거나 만료된 경우 반환할 기본값

        Returns:
            캐시된 값 또는 기본값
        """
        with self._lock:
            if self._value is None:
                return default

            if time.time() - self._timestamp > self._ttl:
                self._value = None
                self._timestamp = 0
                return default

            return self._value

    def set(self, value: T) -> None:
        """값을 캐시에 저장

        Args:
            value: 저장할 값
        """
        with self._lock:
            self._value = value
            self._timestamp = time.time()

    def invalidate(self) -> None:
        """캐시 무효화"""
        with self._lock:
            self._timestamp = 0

    def clear(self) -> None:
        """캐시 완전 초기화"""
        with self._lock:
            self._value = None
            self._timestamp = 0

    def is_valid(self) -> bool:
        """캐시가 유효한지 확인"""
        with self._lock:
            if self._value is None:
                return False
            return time.time() - self._timestamp <= self._ttl

    @property
    def age(self) -> float:
        """캐시 나이(초) - 캐시가 없으면 -1 반환"""
        with self._lock:
            if self._value is None:
                return -1
            return time.time() - self._timestamp
