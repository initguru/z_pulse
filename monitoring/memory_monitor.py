"""
런타임 메모리 모니터

모니터링 스레드에 통합되어 주기적으로 프로세스 메모리를 추적합니다.
- 5분 간격 RSS 샘플링
- 1시간 단위 추세 분석 (누수 감지)
- 임계치 초과 시 텔레그램 알림
- tracemalloc 스냅샷 (선택적)

알림 철학:
- 단순 수치 나열보다 실제 운영 조치가 필요한 경우만 경고
- VMS, peak RSS, 단기 증가율의 24h 외삽은 기본 경고에서 제외
- 경고는 OOM 위험 또는 지속적 메모리 누수 의심에 집중
"""

import gc
import logging
import os
import time
import tracemalloc
from pathlib import Path
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import psutil

logger = logging.getLogger(__name__)

# 기본 설정
SAMPLE_INTERVAL_SEC = 300  # 5분 간격 샘플링
RSS_WARNING_MB = 800  # 절대 RSS 경고 임계치
RSS_CRITICAL_MB = 1500  # 절대 RSS 위험 임계치
RSS_WARNING_PERCENT = 10.0  # 시스템 메모리 대비 경고 %
RSS_CRITICAL_PERCENT = 20.0  # 시스템 메모리 대비 위험 %
SUSTAINED_GROWTH_WINDOW = 12  # 최근 1시간(5분 × 12샘플)
SUSTAINED_GROWTH_WARNING_MB = 300  # 최근 1시간 순증가량 경고
SUSTAINED_GROWTH_WARNING_MB_PER_MIN = 5.0  # 최근 1시간 평균 증가율 경고
SUSTAINED_GROWTH_MIN_CURRENT_MB = 600  # 너무 낮은 RSS에서는 누수 경고 억제
MAX_SAMPLES = 288  # 최대 보관 (5분 × 288 = 24시간)
TRACEMALLOC_TOP_N = 15  # 상위 할당 추적 수


@dataclass
class MemorySample:
    """메모리 샘플 데이터"""

    timestamp: datetime
    rss_mb: float
    vms_mb: float
    percent: float
    num_threads: int
    open_files: int = 0


class MemoryMonitor:
    """런타임 메모리 모니터"""

    def __init__(self, enable_tracemalloc: bool = False, enable_alerts: bool = True):
        """
        Args:
            enable_tracemalloc: tracemalloc 활성화 여부 (오버헤드 ~5% 추가)
            enable_alerts: 메모리 임계치 초과 시 텔레그램 알림 발송 여부
        """
        self._process = psutil.Process(os.getpid())
        self._samples: deque[MemorySample] = deque(maxlen=MAX_SAMPLES)
        self._last_sample_time: float = 0
        self._last_alert_time: float = 0
        self._alert_cooldown: float = 3600  # 1시간 쿨다운
        self._tracemalloc_enabled = enable_tracemalloc
        self._enable_alerts = enable_alerts
        self._baseline_rss: Optional[float] = None
        self._peak_rss: float = 0

        # tracemalloc 초기화
        if enable_tracemalloc and not tracemalloc.is_tracing():
            tracemalloc.start(10)  # 10 frames depth
            logger.info("[MEM] tracemalloc 활성화 (depth=10)")

        # 초기 샘플
        self._take_sample()
        if self._samples:
            self._baseline_rss = self._samples[-1].rss_mb

        logger.info(
            f"[MEM] MemoryMonitor 초기화 완료 "
            f"(baseline={self._baseline_rss:.1f}MB, "
            f"tracemalloc={'ON' if enable_tracemalloc else 'OFF'}, "
            f"alerts={'ON' if enable_alerts else 'OFF'})"
        )

    def check(self) -> Optional[str]:
        """
        주기적 메모리 체크. 모니터링 루프에서 호출합니다.

        Returns:
            알림 메시지 (임계치 초과 시) 또는 None
        """
        now = time.time()

        # 샘플링 간격 체크
        if now - self._last_sample_time < SAMPLE_INTERVAL_SEC:
            return None

        sample = self._take_sample()
        if not sample:
            return None

        # 피크 갱신
        if sample.rss_mb > self._peak_rss:
            self._peak_rss = sample.rss_mb

        # 알림 판정
        alert_msg = self._evaluate_alert(sample, now)
        return alert_msg

    def _take_sample(self) -> Optional[MemorySample]:
        """메모리 샘플을 수집합니다."""
        try:
            mem = self._process.memory_info()
            sample = MemorySample(
                timestamp=datetime.now(),
                rss_mb=mem.rss / 1024 / 1024,
                vms_mb=mem.vms / 1024 / 1024,
                percent=self._process.memory_percent(),
                num_threads=self._process.num_threads(),
            )
            try:
                sample.open_files = len(self._process.open_files())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

            self._samples.append(sample)
            self._last_sample_time = time.time()
            return sample
        except Exception as e:
            logger.error(f"[MEM] 샘플 수집 실패: {e}")
            return None

    def _evaluate_alert(self, current: MemorySample, now: float) -> Optional[str]:
        """실제 조치가 필요한 메모리 경고만 생성합니다."""
        # 알림 비활성화 체크
        if not self._enable_alerts:
            return None

        # 쿨다운 체크
        if now - self._last_alert_time < self._alert_cooldown:
            return None

        alerts: list[tuple[str, str]] = []

        # 1) 절대 RSS / 시스템 메모리 비율 임계치
        if current.rss_mb >= RSS_CRITICAL_MB:
            alerts.append(
                ("critical", f"RSS {current.rss_mb:.0f}MB ≥ {RSS_CRITICAL_MB}MB")
            )
        elif current.rss_mb >= RSS_WARNING_MB:
            alerts.append(
                ("warning", f"RSS {current.rss_mb:.0f}MB ≥ {RSS_WARNING_MB}MB")
            )

        if current.percent >= RSS_CRITICAL_PERCENT:
            alerts.append(
                (
                    "critical",
                    f"시스템 메모리 사용 비율 {current.percent:.1f}% ≥ {RSS_CRITICAL_PERCENT:.1f}%",
                )
            )
        elif current.percent >= RSS_WARNING_PERCENT:
            alerts.append(
                (
                    "warning",
                    f"시스템 메모리 사용 비율 {current.percent:.1f}% ≥ {RSS_WARNING_PERCENT:.1f}%",
                )
            )

        # 2) 지속적 메모리 누수 의심 (1시간 동안 의미 있는 순증가 + 현재 RSS도 충분히 큼)
        growth_rate = self._calc_growth_rate(window=SUSTAINED_GROWTH_WINDOW)
        growth_delta = self._calc_growth_delta(window=SUSTAINED_GROWTH_WINDOW)
        if (
            growth_rate is not None
            and growth_delta is not None
            and current.rss_mb >= SUSTAINED_GROWTH_MIN_CURRENT_MB
            and growth_rate >= SUSTAINED_GROWTH_WARNING_MB_PER_MIN
            and growth_delta >= SUSTAINED_GROWTH_WARNING_MB
        ):
            alerts.append(
                (
                    "warning",
                    f"최근 1시간 RSS 지속 증가: +{growth_delta:.0f}MB ({growth_rate:+.2f}MB/min)",
                )
            )

        if not alerts:
            return None

        self._last_alert_time = now
        severity = (
            "critical" if any(level == "critical" for level, _ in alerts) else "warning"
        )
        title = "🚨 *메모리 위험 경고*" if severity == "critical" else "⚠️ *메모리 경고*"

        lines = [
            title,
            "",
            f"RSS: {current.rss_mb:.1f} MB ({current.percent:.1f}%)",
            f"스레드: {current.num_threads}",
            f"가동시간: {self._uptime_str()}",
            "",
        ]
        for _, msg in alerts:
            lines.append(f"• {msg}")

        # tracemalloc top allocations
        if self._tracemalloc_enabled:
            top = self._get_tracemalloc_top(5)
            if top:
                lines.append("")
                lines.append("Top 할당:")
                lines.extend(top)

        return "\n".join(lines)

    def _calc_growth_rate(self, window: int = 12) -> Optional[float]:
        """
        최근 window개 샘플에서 RSS 증가율 (MB/min)을 계산합니다.
        """
        if len(self._samples) < max(3, window):
            return None

        recent = list(self._samples)[-window:]
        first, last = recent[0], recent[-1]
        elapsed_min = (last.timestamp - first.timestamp).total_seconds() / 60

        if elapsed_min < 5:
            return None

        return (last.rss_mb - first.rss_mb) / elapsed_min

    def _calc_growth_delta(self, window: int = 12) -> Optional[float]:
        """최근 window개 샘플의 RSS 순증가량(MB)을 계산합니다."""
        if len(self._samples) < max(3, window):
            return None

        recent = list(self._samples)[-window:]
        first, last = recent[0], recent[-1]
        return last.rss_mb - first.rss_mb

    def _uptime_str(self) -> str:
        """프로세스 가동 시간 문자열"""
        try:
            create_time = datetime.fromtimestamp(self._process.create_time())
            delta = datetime.now() - create_time
            hours = delta.total_seconds() / 3600
            if hours < 1:
                return f"{delta.total_seconds() / 60:.0f}분"
            return f"{hours:.1f}시간"
        except Exception:
            return "알 수 없음"

    def _get_tracemalloc_top(self, n: int = 5) -> list[str]:
        """tracemalloc 상위 할당을 반환합니다."""
        if not tracemalloc.is_tracing():
            return []
        try:
            snapshot = tracemalloc.take_snapshot()
            # 프로젝트 파일만 필터
            snapshot = snapshot.filter_traces(
                [
                    tracemalloc.Filter(True, str(Path(__file__).parent.parent / "*")),
                    tracemalloc.Filter(False, "<frozen*>"),
                    tracemalloc.Filter(False, "<unknown>"),
                ]
            )
            stats = snapshot.statistics("lineno")
            lines = []
            for stat in stats[:n]:
                # 경로를 짧게
                frame = str(stat.traceback)
                size = stat.size / 1024
                lines.append(f"  {size:.1f}KB: {frame[:80]}")
            return lines
        except Exception as e:
            return [f"  tracemalloc 오류: {e}"]

    def get_status_line(self) -> str:
        """대시보드용 한 줄 상태 문자열"""
        if not self._samples:
            return "MEM: 데이터 없음"

        current = self._samples[-1]
        growth = self._calc_growth_rate(SUSTAINED_GROWTH_WINDOW)
        growth_str = f" ({growth:+.2f}/min)" if growth is not None else ""

        return (
            f"MEM: {current.rss_mb:.0f}MB{growth_str} "
            f"| peak {self._peak_rss:.0f}MB "
            f"| threads {current.num_threads}"
        )

    def get_summary(self) -> dict:
        """현재 메모리 상태 요약 딕셔너리"""
        if not self._samples:
            return {}

        current = self._samples[-1]
        return {
            "rss_mb": current.rss_mb,
            "vms_mb": current.vms_mb,
            "peak_rss_mb": self._peak_rss,
            "baseline_rss_mb": self._baseline_rss,
            "growth_rate": self._calc_growth_rate(SUSTAINED_GROWTH_WINDOW),
            "growth_delta_mb": self._calc_growth_delta(SUSTAINED_GROWTH_WINDOW),
            "num_threads": current.num_threads,
            "samples_count": len(self._samples),
            "uptime": self._uptime_str(),
        }

    def get_hourly_report(self) -> str:
        """1시간 단위 리포트 (로그 출력용)"""
        if len(self._samples) < 2:
            return ""

        current = self._samples[-1]
        growth_1h = self._calc_growth_rate(SUSTAINED_GROWTH_WINDOW)
        growth_all = self._calc_growth_rate(len(self._samples))
        growth_1h_str = f"{growth_1h:+.2f}MB/min" if growth_1h is not None else "n/a"
        growth_all_str = f"{growth_all:+.3f}MB/min" if growth_all is not None else "n/a"

        return (
            f"[MEM][HOURLY] RSS={current.rss_mb:.1f}MB "
            f"VMS={current.vms_mb:.1f}MB "
            f"peak={self._peak_rss:.1f}MB "
            f"threads={current.num_threads} "
            f"growth_1h={growth_1h_str} "
            f"growth_all={growth_all_str} "
            f"samples={len(self._samples)} "
            f"uptime={self._uptime_str()}"
        )

    def force_gc_and_report(self) -> str:
        """강제 GC 실행 후 회수된 메모리를 보고합니다."""
        before = self._process.memory_info().rss / 1024 / 1024

        collected = gc.collect()
        gc.collect()  # 2차 수집

        after = self._process.memory_info().rss / 1024 / 1024
        freed = before - after

        report = (
            f"[MEM][GC] 수집된 객체: {collected}개 | "
            f"RSS 변화: {before:.1f}MB → {after:.1f}MB ({freed:+.1f}MB)"
        )
        logger.info(report)
        return report
