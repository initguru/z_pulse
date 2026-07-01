"""
로그 키워드 감시 모듈

Phase 3.2 리팩토링: LogKeywordMonitor 클래스 분리
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime

from pathlib import Path
from typing import Any, Callable, Optional

WatchdogFileSystemEventHandler: Any = object
WatchdogObserver: Any = None
WatchdogPollingObserver: Any = None

try:
    from watchdog.events import FileSystemEventHandler as WatchdogFileSystemEventHandler
    from watchdog.observers import Observer as WatchdogObserver
    from watchdog.observers.polling import PollingObserver as WatchdogPollingObserver

    WATCHDOG_AVAILABLE = True
except Exception:
    WATCHDOG_AVAILABLE = False

from z_pulse.constants import FileConfig, SizeConfig, DurationConfig
from z_pulse.utils import escape_markdown
from z_pulse.utils.formatters import strip_ansi
from z_pulse.utils.telegram_gateway import (
    BACKGROUND_TIMEOUT,
    TelegramPriority,
    get_telegram_gateway,
)
from z_pulse.utils.json_handler import load_json, save_json  # Phase 2.2: JSON 작업 통합
from z_pulse.config.runtime_settings import runtime_settings

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_OBSERVER_TIMEOUT_SEC_DARWIN = 1.0
DEFAULT_REALTIME_OBSERVER_TIMEOUT_SEC_OTHER = 0.2
DEFAULT_RAPID_ENTRY_POLL_INTERVAL_SEC = 0.2


def _default_realtime_observer_timeout_sec() -> float:
    return (
        DEFAULT_REALTIME_OBSERVER_TIMEOUT_SEC_DARWIN
        if sys.platform == "darwin"
        else DEFAULT_REALTIME_OBSERVER_TIMEOUT_SEC_OTHER
    )


def _format_exit_pnl_message(
    dir_name: str,
    snap: "Optional[dict]",
    cur_equity: "Optional[object]",
) -> str:
    """익절 종료 텔레그램 메시지를 MarkdownV2 이스케이프 적용하여 반환.

    Args:
        dir_name: 봇 디렉토리 이름 (예: "GRVT-P-3B")
        snap: AssignmentEquityData dict(read_assignment_equity 반환값) 또는 None
        cur_equity: 현재 equity (Decimal) 또는 None

    Returns:
        MarkdownV2 이스케이프가 적용된 두 줄 메시지 문자열.
    """
    from decimal import Decimal as _Decimal

    # 첫 줄 구성
    slot_type = snap.get("slot_type") if snap else None
    slot_badge = " 🔒BTC/ETH슬롯" if slot_type == "BTC_ETH" else ""
    first_line_plain = f"⚡️ [익절 종료] {dir_name}{slot_badge}"

    # PnL 계산
    pnl_line_plain: str
    if snap is not None and cur_equity is not None:
        try:
            alloc_equity: _Decimal = snap["total_equity"]
            cur_equity_dec = _Decimal(str(cur_equity))
            pnl = cur_equity_dec - alloc_equity
            sign = "+" if pnl >= 0 else "-"
            amount_str = f"{sign}${abs(pnl):,.2f}"
            if alloc_equity > 0:
                pct = pnl / alloc_equity * 100
                pct_sign = "+" if pct >= 0 else ""
                pnl_line_plain = f"💰 PnL: {amount_str} ({pct_sign}{float(pct):.2f}%)"
            else:
                pnl_line_plain = f"💰 PnL: {amount_str}"
        except Exception:
            pnl_line_plain = "💰 PnL: 산출 불가"
    else:
        pnl_line_plain = "💰 PnL: 산출 불가"

    # MarkdownV2 이스케이프 적용
    plain_message = f"{first_line_plain}\n{pnl_line_plain}"
    return escape_markdown(plain_message)


class _RealtimeLogEventHandler(WatchdogFileSystemEventHandler):
    """monitor.log 변경 이벤트를 즉시 처리하는 핸들러."""

    def __init__(self, monitor: "LogKeywordMonitor"):
        super().__init__()
        self._monitor = monitor

    def on_modified(self, event):
        if event.is_directory:
            return
        self._monitor.handle_realtime_log_event(Path(event.src_path))

    def on_created(self, event):
        if event.is_directory:
            return
        self._monitor.handle_realtime_log_event(Path(event.src_path))


class LogKeywordMonitor:
    """
    모든 monitor.log 파일을 실시간으로 감시하여 특정 키워드가 나타나면 알림을 보냅니다.
    """

    def __init__(self, target_dir: str, bot_instance):
        self.target_dir = Path(target_dir)
        self.bot_instance = bot_instance
        self._file_positions_lock = threading.Lock()
        self.file_positions = {}
        self.last_notification_times = {}
        self._last_mtime = {}  # [Phase 3.2] mtime 기반 변경 감지용
        self.config_file = FileConfig.LOG_KEYWORDS

        self.bot_keywords = {}

        # 특정 phrase 감지 시 프로세스 감소 알림 억제 콜백
        self._suppress_alert_phrases: set = set()
        self._suppress_alert_callback: Optional[Callable[..., None]] = None

        # [Safety Guard] '추가 진입' 폭주 감지 (1초 내 N회) -> 강제종료 + 지연 재시작
        self._rapid_entry_enabled = runtime_settings.get_bool(
            "RAPID_ENTRY_GUARD_ENABLED", True
        )
        self._rapid_entry_phrase = os.getenv("RAPID_ENTRY_GUARD_PHRASE", "추가 진입")
        # 1초 주기 3회(총 3.3초 허용) 감지 파라미터
        self._rapid_entry_sequence_window_sec = float(
            os.getenv("RAPID_ENTRY_GUARD_SEQUENCE_WINDOW_SEC", "3.3")
        )
        self._rapid_entry_sequence_count = int(
            os.getenv("RAPID_ENTRY_GUARD_SEQUENCE_COUNT", "3")
        )
        self._rapid_entry_min_interval_sec = float(
            os.getenv("RAPID_ENTRY_GUARD_MIN_INTERVAL_SEC", "0.7")
        )
        self._rapid_entry_max_interval_sec = float(
            os.getenv("RAPID_ENTRY_GUARD_MAX_INTERVAL_SEC", "1.4")
        )
        self._rapid_entry_force_consecutive_enabled = str(
            os.getenv("RAPID_ENTRY_GUARD_FORCE_CONSECUTIVE", "false")
        ).lower() in {"1", "true", "yes", "on"}
        self._rapid_entry_force_phrases = tuple(
            phrase.strip()
            for phrase in os.getenv(
                "RAPID_ENTRY_GUARD_FORCE_PHRASES", "최초 진입,추가 진입"
            ).split(",")
            if phrase.strip()
        )
        self._rapid_entry_lag_alert_enabled = str(
            os.getenv("RAPID_ENTRY_GUARD_LAG_ALERT_ENABLED", "false")
        ).lower() in {"1", "true", "yes", "on"}
        self._rapid_entry_lag_alert_threshold_sec = float(
            os.getenv("RAPID_ENTRY_GUARD_LAG_ALERT_THRESHOLD_SEC", "5")
        )
        self._rapid_entry_lag_alert_cooldown_sec = float(
            os.getenv("RAPID_ENTRY_GUARD_LAG_ALERT_COOLDOWN_SEC", "60")
        )
        self._rapid_entry_restart_delay_sec = int(
            os.getenv("RAPID_ENTRY_GUARD_RESTART_DELAY_SEC", "180")
        )
        self._rapid_entry_debug = str(
            os.getenv("RAPID_ENTRY_GUARD_DEBUG", "true")
        ).lower() in {"1", "true", "yes", "on"}

        self._rapid_entry_lock = threading.Lock()
        self._rapid_entry_hits: dict[str, deque[float]] = {}
        self._rapid_entry_restarting_until: dict[str, float] = {}
        self._rapid_entry_timers: dict[str, threading.Timer] = {}
        self._rapid_entry_positions: dict[str, int] = {}
        self._rapid_entry_last_lag_alert_ts: dict[str, float] = {}
        self._rapid_entry_file_generations: dict[str, int] = {}
        self._rapid_entry_seen_event_ids: dict[str, dict[str, float]] = {}
        self._rapid_entry_poll_threads: dict[str, threading.Thread] = {}
        self._rapid_entry_poll_stop = threading.Event()
        self._rapid_entry_observer: Optional[Any] = None
        self._rapid_entry_handler: Optional[_RealtimeLogEventHandler] = None
        self._realtime_backend = "none"

        # realtime monitor 상태/알림 제어
        self._realtime_expected = False
        self._realtime_alerted_unavailable = False
        self._realtime_alerted_down = False

        self.last_config_mtime = 0
        self._load_keywords()
        self._initialize_file_positions()

        logger.info(
            f"로그 키워드 감시 초기화 완료. 개별봇 설정: {len(self.bot_keywords)}개"
        )

    def _create_default_config_file(self):
        """기본 설정 파일 생성"""
        logger.info(f"'{self.config_file}' 파일이 없어 기본값으로 생성합니다.")
        default_data = {
            "bots": {},
        }
        # Phase 2.2: save_json 사용 (원자적 쓰기)
        if save_json(self.config_file, default_data):
            self.bot_keywords = default_data["bots"]
        else:
            logger.error("기본 키워드 설정 파일 생성 실패")

    def _load_keywords(self):
        """키워드 목록 로드 및 타입 안전성 검사"""
        if not os.path.exists(self.config_file):
            self._create_default_config_file()
            return

        try:
            self.last_config_mtime = os.path.getmtime(self.config_file)

            # Phase 2.2: load_json 사용 (에러 처리 포함)
            data = load_json(self.config_file, default={"bots": {}})

            # 구버전 형식 (list) 마이그레이션
            if isinstance(data, list):
                logger.warning("구버전 형식 감지. 마이그레이션합니다.")
                self.bot_keywords = {}
                self._save_keywords()
            elif isinstance(data, dict):
                # [안전장치] bots가 dict가 아니면 빈 dict로 초기화
                bots_data = data.get("bots", {})
                if not isinstance(bots_data, dict):
                    logger.warning("'bots' 데이터가 딕셔너리가 아닙니다. 초기화합니다.")
                    self.bot_keywords = {}
                else:
                    self.bot_keywords = bots_data
            else:
                logger.error(f"'{self.config_file}' 형식이 올바르지 않습니다.")
                self.bot_keywords = {}

        except Exception as e:
            logger.error(f"키워드 로드 실패: {e}")
            self.bot_keywords = {}

    def _check_config_reload(self):
        try:
            if not os.path.exists(self.config_file):
                return
            current_mtime = os.path.getmtime(self.config_file)
            if current_mtime > self.last_config_mtime:
                logger.info("설정 파일 변경 감지. 재로드합니다.")
                self._load_keywords()
        except Exception:
            pass

    def _save_keywords(self):
        """키워드 저장 (원자적 쓰기)"""
        data = {"bots": self.bot_keywords}

        # Phase 2.2: save_json 사용 (원자적 쓰기 + 에러 처리)
        if save_json(self.config_file, data):
            try:
                self.last_config_mtime = os.path.getmtime(self.config_file)
                logger.info("키워드 설정 파일 저장 완료")
                return True
            except Exception as e:
                logger.error(f"mtime 업데이트 실패: {e}")
                return True  # 저장은 성공했으므로 True 반환
        else:
            logger.error("키워드 설정 저장 실패")
            return False

    def add_keyword(
        self,
        phrase: str,
        is_json_block: bool,
        cooldown_seconds: int,
        bot_name: str,
    ) -> bool:
        """키워드 추가"""
        try:
            new_entry = {
                "phrase": phrase,
                "is_json_block": is_json_block,
                "cooldown_seconds": cooldown_seconds,
            }

            if bot_name not in self.bot_keywords:
                self.bot_keywords[bot_name] = []
            self.bot_keywords[bot_name].append(new_entry)
            logger.info(f"[{bot_name}] 키워드 추가됨: {phrase}")

            return self._save_keywords()
        except Exception as e:
            logger.error(f"키워드 추가 중 오류 발생: {e}")
            return False

    def update_keyword(
        self,
        index: int,
        phrase: str,
        is_json_block: bool,
        cooldown_seconds: int,
        bot_name: str,
    ) -> bool:
        """키워드 수정"""
        try:
            # 대상 리스트 참조 가져오기
            if bot_name not in self.bot_keywords:
                logger.error(f"수정 실패: '{bot_name}' 봇 설정이 없습니다.")
                return False
            target_list = self.bot_keywords[bot_name]

            if 0 <= index < len(target_list):
                target_list[index] = {
                    "phrase": phrase,
                    "is_json_block": is_json_block,
                    "cooldown_seconds": cooldown_seconds,
                }
                logger.info(f"[{bot_name or 'Global'}] 키워드 수정됨 (idx: {index})")
                return self._save_keywords()

            logger.error(f"수정 실패: 인덱스 {index}가 범위를 벗어남")
            return False
        except Exception as e:
            logger.error(f"키워드 수정 중 오류: {e}")
            return False

    def delete_keyword(self, index: int, bot_name: str) -> bool:
        """키워드 삭제"""
        try:
            if bot_name not in self.bot_keywords:
                return False
            target_list = self.bot_keywords[bot_name]

            if 0 <= index < len(target_list):
                del target_list[index]
                # 리스트가 비어도 봇 키는 남겨둠 (설정 유지)
                logger.info(f"[{bot_name or 'Global'}] 키워드 삭제됨 (idx: {index})")
                return self._save_keywords()

            return False
        except Exception as e:
            logger.error(f"키워드 삭제 중 오류: {e}")
            return False

    def get_keywords(self, bot_name: str) -> list:
        return self.bot_keywords.get(bot_name, [])

    def _initialize_file_positions(self):
        """모든 로그 파일의 현재 크기를 초기 위치로 설정하여, 시작 전 로그를 무시합니다."""
        try:
            log_files = list(self.target_dir.glob("*/" + FileConfig.MONITOR_LOG))
            with self._file_positions_lock:
                with self._rapid_entry_lock:
                    for log_path in log_files:
                        if not log_path.is_file():
                            continue
                        try:
                            current_size = log_path.stat().st_size
                        except Exception:
                            current_size = 0
                        log_key = str(log_path)
                        self.file_positions[log_key] = current_size
                        self._rapid_entry_positions[log_key] = current_size
                        self._rapid_entry_file_generations[log_key] = 0
                        self._rapid_entry_seen_event_ids.setdefault(log_key, {})
        except Exception as e:
            logger.error(f"로그 파일 위치 초기화 중 오류: {e}")

    def reset_file_positions(self):
        """파일 위치를 초기화합니다. (디렉토리 변경 시 호출)"""
        with self._file_positions_lock:
            self.file_positions.clear()
        with self._rapid_entry_lock:
            self._rapid_entry_positions.clear()
            self._rapid_entry_file_generations.clear()
            self._rapid_entry_seen_event_ids.clear()
        self._initialize_file_positions()
        logger.info("로그 파일 위치가 초기화되었습니다.")

    def force_reload_keywords(self):
        """키워드 목록을 강제로 재로드합니다."""
        self.last_config_mtime = 0
        self._load_keywords()
        logger.info("키워드 목록이 강제로 재로드되었습니다.")

    def set_suppress_alert_phrases(
        self, phrases: set, callback: Callable[..., None]
    ) -> None:
        """
        특정 phrase 감지 시 프로세스 감소 알림을 억제할 콜백을 등록합니다.
        monitoring_thread에서 EXIT_RESERVATION 등 정상 종료 신호를 프로세스 모니터에 연결할 때 사용합니다.

        Args:
            phrases: 억제 트리거 phrase 집합 (예: {"EXIT_RESERVATION"})
            callback: 감지 시 호출할 콜백 (예: monitor.suppress_decrease_alert)
        """
        self._suppress_alert_phrases = phrases
        self._suppress_alert_callback = callback

    def _alert_realtime_unavailable(self, reason: str) -> None:
        if self._realtime_alerted_unavailable:
            return
        self._realtime_alerted_unavailable = True
        self._send_notification(
            f"⚠️ *realtime monitor 비활성*\n"
            f"사유: {escape_markdown(reason)}\n"
            f"현재는 폴링 fallback 경로로 동작 중"
        )

    def _alert_realtime_down(self, reason: str) -> None:
        if self._realtime_alerted_down:
            return
        self._realtime_alerted_down = True
        self._send_notification(
            f"🚨 *realtime monitor 장애*\n"
            f"사유: {escape_markdown(reason)}\n"
            f"자동 복구 시도 중 \\(실패 시 폴링 fallback 유지\\)"
        )

    def ensure_realtime_monitor_alive(self) -> None:
        """realtime monitor 생존 확인. 다운 시 알림 + 자동 복구 시도."""
        if not self._realtime_expected:
            return

        obs = self._rapid_entry_observer
        if obs is not None and obs.is_alive():
            return

        self._alert_realtime_down("watchdog observer not alive")
        self._realtime_expected = False
        self.start_rapid_entry_guard()

    def _create_realtime_observer(self):
        default_timeout = _default_realtime_observer_timeout_sec()
        timeout = runtime_settings.get_float(
            "KEYWORD_MONITOR_OBSERVER_TIMEOUT_SEC",
            default_timeout,
        )
        if timeout <= 0:
            logger.warning(
                "invalid KEYWORD_MONITOR_OBSERVER_TIMEOUT_SEC=%s, fallback=%s",
                timeout,
                default_timeout,
            )
            timeout = default_timeout

        if sys.platform == "darwin":
            return WatchdogPollingObserver(timeout=timeout), "polling"
        return WatchdogObserver(timeout=timeout), "watchdog"

    def start_rapid_entry_guard(self) -> None:
        """monitor.log 실시간 감시 시작 (일반 키워드 + rapid entry guard 통합)."""
        if not WATCHDOG_AVAILABLE:
            logger.warning("watchdog 미설치로 realtime log monitor 비활성화")
            self._realtime_expected = False
            self._alert_realtime_unavailable("watchdog unavailable")
            return

        if self._rapid_entry_observer and self._rapid_entry_observer.is_alive():
            self._realtime_expected = True
            return

        try:
            # 시작 시점 기준으로만 감지 (기존 로그 무시)
            self._initialize_file_positions()

            observer, backend = self._create_realtime_observer()
            handler = _RealtimeLogEventHandler(self)

            watch_count = 0
            for entry in os.scandir(self.target_dir):
                if not entry.is_dir():
                    continue
                observer.schedule(handler, entry.path, recursive=False)
                watch_count += 1

            observer.start()
            self._rapid_entry_handler = handler
            self._rapid_entry_observer = observer
            self._realtime_backend = backend

            # rapid entry guard 전용 폴링 스레드 (라인 단위, 실제 타임스탬프)
            if self._rapid_entry_enabled:
                self._rapid_entry_poll_stop.clear()
                for entry in os.scandir(self.target_dir):
                    if not entry.is_dir():
                        continue
                    log_path = Path(entry.path) / FileConfig.MONITOR_LOG
                    t = threading.Thread(
                        target=self._rapid_entry_poll_loop,
                        args=(log_path, entry.name),
                        daemon=True,
                        name=f"rapid_entry_poll_{entry.name}",
                    )
                    t.start()
                    self._rapid_entry_poll_threads[entry.name] = t

            self._realtime_expected = True
            self._realtime_alerted_down = False
            logger.info(
                f"Realtime log monitor started: backend={backend}, watches={watch_count}, "
                f"observer_timeout={getattr(observer, 'timeout', 'unknown')}, "
                f"rapid_guard_enabled={int(self._rapid_entry_enabled)}, "
                f"phrase='{self._rapid_entry_phrase}', "
                f"sequence_count={self._rapid_entry_sequence_count}, "
                f"sequence_window={self._rapid_entry_sequence_window_sec}s, "
                f"poll_interval={self._rapid_entry_poll_interval_sec()}s, "
                f"restart_delay={self._rapid_entry_restart_delay_sec}s"
            )
        except Exception as e:
            self._realtime_expected = False
            logger.error(f"realtime log monitor start failed: {e}")
            self._alert_realtime_down(f"observer start failed: {e}")

    def stop_rapid_entry_guard(self) -> None:
        """rapid entry guard 중지 + 예약된 재시작 타이머 정리."""
        self._realtime_expected = False

        with self._rapid_entry_lock:
            for timer in self._rapid_entry_timers.values():
                try:
                    timer.cancel()
                except Exception:
                    pass
            self._rapid_entry_timers.clear()

        if self._rapid_entry_observer:
            try:
                self._rapid_entry_observer.stop()
                self._rapid_entry_observer.join(timeout=3.0)
            except Exception:
                pass
            self._rapid_entry_observer = None
            self._rapid_entry_handler = None
            self._realtime_backend = "none"

        self._rapid_entry_poll_stop.set()
        for t in self._rapid_entry_poll_threads.values():
            t.join(timeout=1.0)
        self._rapid_entry_poll_threads.clear()
        self._rapid_entry_poll_stop.clear()

    def handle_realtime_log_event(self, log_path: Path) -> None:
        """실시간 이벤트 처리: 일반 키워드 + rapid-entry guard."""
        if log_path.name != FileConfig.MONITOR_LOG:
            return

        self._check_config_reload()

        try:
            self._process_log_file(log_path)
        except Exception as e:
            logger.debug(f"realtime keyword process failed ({log_path}): {e}")

        if not self._rapid_entry_enabled:
            return

        try:
            self.handle_rapid_entry_event(log_path)
        except Exception as e:
            logger.debug(f"realtime rapid-entry process failed ({log_path}): {e}")

    def _extract_event_ts(self, line: str) -> Optional[float]:
        match = re.search(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", line)
        if not match:
            return None

        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return None

    def _maybe_alert_rapid_entry_lag(self, dir_name: str, lag_sec: float) -> None:
        if not self._rapid_entry_lag_alert_enabled:
            return
        if lag_sec < self._rapid_entry_lag_alert_threshold_sec:
            return

        now = time.time()
        with self._rapid_entry_lock:
            last_sent = self._rapid_entry_last_lag_alert_ts.get(dir_name, 0.0)
            if (now - last_sent) < self._rapid_entry_lag_alert_cooldown_sec:
                return
            self._rapid_entry_last_lag_alert_ts[dir_name] = now

        self._send_notification(
            f"⚠️ 감지 지연 경보\n📁 `{escape_markdown(dir_name)}`\n⏱ 지연 {lag_sec:.1f}초"
        )

    def _read_new_content_for_rapid_guard(self, log_path: Path) -> tuple[str, int, int]:
        log_key = str(log_path)
        with self._rapid_entry_lock:
            last_pos = self._rapid_entry_positions.get(log_key, 0)
            generation = self._rapid_entry_file_generations.get(log_key, 0)

        try:
            current_size = log_path.stat().st_size
            if current_size < last_pos:
                last_pos = 0
                generation = self._mark_rapid_entry_file_rotated(log_path)
            if current_size <= last_pos:
                return "", last_pos, generation

            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(last_pos)
                content = f.read()
                new_pos = f.tell()

            with self._rapid_entry_lock:
                self._rapid_entry_positions[log_key] = new_pos

            return content, last_pos, generation
        except Exception:
            return "", last_pos, generation

    def _is_rapid_entry_representative_phrase_line(self, line: str, phrase: str) -> bool:
        if not phrase:
            return False

        stripped = strip_ansi(line).strip()
        if stripped.count(phrase) != 1:
            return False

        left, right = stripped.split(phrase)
        if not left.strip() or not right.strip():
            return False

        decoration_only = re.compile(r"^[^\w가-힣]+$")
        return bool(decoration_only.fullmatch(left)) and bool(
            decoration_only.fullmatch(right)
        )

    def _is_rapid_entry_representative_line(self, line: str) -> bool:
        return self._is_rapid_entry_representative_phrase_line(
            line, self._rapid_entry_phrase
        )

    def _count_keyword_occurrences(self, line: str, phrase: str) -> int:
        return 1 if self._is_rapid_entry_representative_phrase_line(line, phrase) else 0

    def _mark_rapid_entry_file_rotated(self, log_path: Path) -> int:
        log_key = str(log_path)
        with self._rapid_entry_lock:
            generation = self._rapid_entry_file_generations.get(log_key, 0) + 1
            self._rapid_entry_file_generations[log_key] = generation
            self._rapid_entry_positions[log_key] = 0
            self._rapid_entry_seen_event_ids[log_key] = {}
        return generation

    def _build_rapid_entry_event_id(
        self, file_offset: int, occurrence_idx: int, generation: int
    ) -> str:
        return f"{generation}:{file_offset}:{occurrence_idx}"


    def _register_rapid_entry_logical_event(
        self,
        log_path: Path,
        dir_name: str,
        event_ts: float,
        event_id: str,
    ) -> bool:
        log_key = str(log_path)
        should_register = False

        with self._rapid_entry_lock:
            seen_ids = self._rapid_entry_seen_event_ids.setdefault(log_key, {})
            if event_id not in seen_ids:
                seen_ids[event_id] = event_ts
                expire_before = event_ts - max(self._rapid_entry_sequence_window_sec, 10.0)
                stale_ids = [
                    key for key, ts in seen_ids.items() if ts < expire_before and key != event_id
                ]
                for stale_key in stale_ids:
                    seen_ids.pop(stale_key, None)
                should_register = True

        if not should_register:
            return False

        self._register_rapid_entry_hits(dir_name, event_ts)
        return True

    def _try_force_consecutive_trigger(
        self, dir_name: str, chunk_lines: list[str], event_ts: float
    ) -> bool:
        if not self._rapid_entry_force_consecutive_enabled:
            return False

        matched_count = sum(
            1
            for line in chunk_lines
            if any(
                self._is_rapid_entry_representative_phrase_line(line, phrase)
                for phrase in self._rapid_entry_force_phrases
            )
        )
        if matched_count < self._rapid_entry_sequence_count:
            return False

        for _ in range(self._rapid_entry_sequence_count):
            self._register_rapid_entry_hits(dir_name, event_ts)
        return True

    def handle_rapid_entry_event(self, log_path: Path) -> None:
        if not self._rapid_entry_enabled:
            return
        if log_path.name != FileConfig.MONITOR_LOG:
            return

        content, base_pos, generation = self._read_new_content_for_rapid_guard(log_path)
        if not content:
            return

        dir_name = log_path.parent.name
        now = time.time()
        chunk_lines = content.splitlines()

        if self._try_force_consecutive_trigger(dir_name, chunk_lines, now):
            return

        lag_values = []
        cursor = base_pos
        for line in chunk_lines:
            occurrence_count = self._count_keyword_occurrences(
                line, self._rapid_entry_phrase
            )
            line_offset = cursor
            cursor += len(line) + 1
            if occurrence_count <= 0:
                continue

            parsed_ts = self._extract_event_ts(line)
            if parsed_ts is not None:
                lag_values.append(max(now - parsed_ts, 0.0))

            for occurrence_idx in range(occurrence_count):
                event_id = self._build_rapid_entry_event_id(
                    file_offset=line_offset,
                    occurrence_idx=occurrence_idx,
                    generation=generation,
                )
                self._register_rapid_entry_logical_event(
                    log_path,
                    dir_name,
                    now,
                    event_id,
                )

        if lag_values:
            self._maybe_alert_rapid_entry_lag(dir_name, max(lag_values))

    def _rapid_entry_poll_loop(self, log_path: Path, dir_name: str) -> None:
        """라인 단위 폴링, 1 logical keyword occurrence = 1 event count."""
        poll_sec = self._rapid_entry_poll_interval_sec()
        phrase = self._rapid_entry_phrase
        first_open = True
        generation = 0
        while not self._rapid_entry_poll_stop.is_set():
            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    if first_open:
                        f.seek(0, 2)  # 최초: 기존 로그 무시
                        first_open = False
                        with self._rapid_entry_lock:
                            self._rapid_entry_positions[str(log_path)] = f.tell()
                            generation = self._rapid_entry_file_generations.get(
                                str(log_path), 0
                            )
                    while not self._rapid_entry_poll_stop.is_set():
                        pos = f.tell()
                        line = f.readline()
                        if line:
                            if not line.endswith("\n"):
                                f.seek(pos)  # 불완전 라인 되감기
                                self._rapid_entry_poll_stop.wait(poll_sec)
                                continue

                            occurrence_count = self._count_keyword_occurrences(line, phrase)
                            if occurrence_count > 0:
                                event_ts = time.time()
                                for occurrence_idx in range(occurrence_count):
                                    event_id = self._build_rapid_entry_event_id(
                                        file_offset=pos,
                                        occurrence_idx=occurrence_idx,
                                        generation=generation,
                                    )
                                    self._register_rapid_entry_logical_event(
                                        log_path,
                                        dir_name,
                                        event_ts,
                                        event_id,
                                    )

                            with self._rapid_entry_lock:
                                self._rapid_entry_positions[str(log_path)] = f.tell()
                        else:
                            try:
                                if f.tell() > log_path.stat().st_size:
                                    generation = self._mark_rapid_entry_file_rotated(log_path)
                                    break  # 로테이션 감지 → 재열기
                            except OSError:
                                pass
                            self._rapid_entry_poll_stop.wait(poll_sec)
            except FileNotFoundError:
                self._rapid_entry_poll_stop.wait(1.0)
            except Exception as e:
                logger.error(f"[RAPID_ENTRY_GUARD] poll_loop error ({dir_name}): {e}")
                self._rapid_entry_poll_stop.wait(1.0)

    def _rapid_entry_poll_interval_sec(self) -> float:
        poll_sec = runtime_settings.get_float(
            "RAPID_ENTRY_GUARD_POLL_INTERVAL_SEC",
            DEFAULT_RAPID_ENTRY_POLL_INTERVAL_SEC,
        )
        if poll_sec <= 0:
            logger.warning(
                "invalid RAPID_ENTRY_GUARD_POLL_INTERVAL_SEC=%s, fallback=%s",
                poll_sec,
                DEFAULT_RAPID_ENTRY_POLL_INTERVAL_SEC,
            )
            return DEFAULT_RAPID_ENTRY_POLL_INTERVAL_SEC
        return poll_sec

    def _register_rapid_entry_hits(
        self,
        dir_name: str,
        event_ts: float,
        enforce_interval: bool = True,
    ) -> None:
        trigger = False

        with self._rapid_entry_lock:
            if event_ts < self._rapid_entry_restarting_until.get(dir_name, 0):
                if self._rapid_entry_debug:
                    wait_left = (
                        self._rapid_entry_restarting_until.get(dir_name, 0) - event_ts
                    )
                    logger.info(
                        f"[RAPID_ENTRY_GUARD][IGNORE] bot={dir_name} reason=restart_window wait_left={wait_left:.2f}s"
                    )
                return

            dq = self._rapid_entry_hits.setdefault(dir_name, deque())

            while dq and (event_ts - dq[0]) > self._rapid_entry_sequence_window_sec:
                dq.popleft()

            dq.append(event_ts)

            while len(dq) > 32:
                dq.popleft()

            if self._rapid_entry_debug:
                logger.info(
                    f"[RAPID_ENTRY_GUARD][HIT] bot={dir_name} q={len(dq)} "
                    f"need={self._rapid_entry_sequence_count} ts={event_ts:.3f}"
                )

            if len(dq) >= self._rapid_entry_sequence_count:
                seq = list(dq)[-self._rapid_entry_sequence_count :]
                span = seq[-1] - seq[0]
                window_ok = span <= self._rapid_entry_sequence_window_sec

                if window_ok:
                    dq.clear()
                    self._rapid_entry_restarting_until[dir_name] = (
                        event_ts + self._rapid_entry_restart_delay_sec
                    )
                    trigger = True
                elif self._rapid_entry_debug:
                    logger.warning(
                        f"[RAPID_ENTRY_GUARD][NO_TRIGGER] bot={dir_name} reason=window_exceeded "
                        f"span={span:.3f}s"
                    )

        if trigger:
            self._execute_rapid_entry_guard(dir_name)

    def _execute_rapid_entry_guard(self, dir_name: str) -> None:
        logger.critical(
            f"[RAPID_ENTRY_GUARD] triggered bot={dir_name} phrase='{self._rapid_entry_phrase}' "
            f"sequence_count={self._rapid_entry_sequence_count} sequence_window={self._rapid_entry_sequence_window_sec}s"
        )

        # kill 먼저 → 알림은 그 후 (긴급 정지 우선)
        killed = 0
        pc = getattr(self.bot_instance, "process_controller", None)
        if pc is not None:
            try:
                killed = pc.kill_specific_process(dir_name)
            except Exception as e:
                logger.error(f"[RAPID_ENTRY_GUARD] kill failed ({dir_name}): {e}")

        sequence_window_text = escape_markdown(
            f"{self._rapid_entry_sequence_window_sec}"
        )
        restart_delay_text = escape_markdown(str(self._rapid_entry_restart_delay_sec))
        self._send_notification(
            f"🛑 *긴급 정지 트리거*\n"
            f"📁 `{escape_markdown(dir_name)}`\n"
            f"🔑 `{escape_markdown(self._rapid_entry_phrase)}` {self._rapid_entry_sequence_count}회 감지\n"
            f"\\(허용 윈도우 {sequence_window_text}초\\)\n"
            f"⏱ 즉시 강제종료 후 {restart_delay_text}초 뒤 재기동 예약"
        )

        self._send_notification(
            f"⚙️ `{escape_markdown(dir_name)}` 강제종료 결과: {killed}개 종료, "
            f"재기동 대기 {self._rapid_entry_restart_delay_sec}초"
        )

        timer = threading.Timer(
            self._rapid_entry_restart_delay_sec,
            self._restart_after_rapid_entry,
            args=(dir_name,),
        )
        timer.daemon = True
        with self._rapid_entry_lock:
            old = self._rapid_entry_timers.get(dir_name)
            if old:
                try:
                    old.cancel()
                except Exception:
                    pass
            self._rapid_entry_timers[dir_name] = timer
        timer.start()

    def _restart_after_rapid_entry(self, dir_name: str) -> None:
        success = False
        try:
            pc = getattr(self.bot_instance, "process_controller", None)
            if pc is None:
                return

            running, _ = pc.is_process_running(dir_name)
            if running:
                success = True
            else:
                loop = getattr(self.bot_instance, "main_loop", None)
                if loop and loop.is_running():
                    fut = asyncio.run_coroutine_threadsafe(
                        pc.start_bot_process(dir_name), loop
                    )
                    fut.add_done_callback(
                        lambda done: self._send_rapid_entry_restart_result(
                            dir_name, done
                        )
                    )
                    return
        except Exception as e:
            logger.error(f"[RAPID_ENTRY_GUARD] restart failed ({dir_name}): {e}")
        finally:
            if success:
                self._clear_rapid_entry_restart_state(dir_name)

        if success:
            self._send_notification(
                f"✅ `{escape_markdown(dir_name)}` 자동 재기동 완료"
            )
        else:
            self._send_notification(
                f"❌ `{escape_markdown(dir_name)}` 자동 재기동 실패 \\(수동 확인 필요\\)"
            )
            self._clear_rapid_entry_restart_state(dir_name)

    def _clear_rapid_entry_restart_state(self, dir_name: str) -> None:
        with self._rapid_entry_lock:
            self._rapid_entry_timers.pop(dir_name, None)
            self._rapid_entry_hits.pop(dir_name, None)
            self._rapid_entry_restarting_until.pop(dir_name, None)

    def _send_rapid_entry_restart_result(self, dir_name: str, fut) -> None:
        try:
            success = bool(fut.result())
        except Exception as e:
            logger.error(f"[RAPID_ENTRY_GUARD] restart failed ({dir_name}): {e}")
            success = False
        finally:
            self._clear_rapid_entry_restart_state(dir_name)

        if success:
            self._send_notification(
                f"✅ `{escape_markdown(dir_name)}` 자동 재기동 완료"
            )
        else:
            self._send_notification(
                f"❌ `{escape_markdown(dir_name)}` 자동 재기동 실패 \\(수동 확인 필요\\)"
            )

    def _send_notification(self, message: str):
        """텔레그램으로 알림을 비동기적으로 보냅니다. (MarkdownV2 파싱 적용)"""
        if (
            self.bot_instance.main_loop
            and self.bot_instance.main_loop.is_running()
            and hasattr(self.bot_instance, "application")
        ):
            try:
                loop = self.bot_instance.main_loop
                get_telegram_gateway(loop).enqueue_threadsafe(
                    loop,
                    lambda: self.bot_instance.application.bot.send_message(
                        chat_id=self.bot_instance.authorized_chat_id,
                        text=message,
                        parse_mode="MarkdownV2",
                    ),
                    priority=TelegramPriority.BACKGROUND,
                    timeout=BACKGROUND_TIMEOUT,
                    label="keyword_notification",
                    drop_ok=True,
                )
            except Exception as e:
                logger.error(f"로그 알림 메시지 발송 실패: {e}")
        else:
            logger.warning(
                "메인 이벤트 루프가 준비되지 않아 알림을 보낼 수 없습니다. (초기화 중일 수 있음)"
            )

    def check_logs(self):
        """
        메인 진입점: 모든 로그 파일을 확인하고 키워드 감지 시 알림 전송

        Phase 2 리팩토링: 긴 메서드를 5개의 작은 메서드로 분해하여 가독성 향상
        Phase 3.2 최적화: os.scandir() + mtime 기반 스킵으로 I/O 감소
        """
        self._check_config_reload()

        if not self._has_keywords():
            return

        # [Phase 3.2] 기존: glob + is_file() 반복 호출
        # for log_path in self.target_dir.glob("*/" + FileConfig.MONITOR_LOG):
        #     if log_path.is_file():
        #         self._process_log_file(log_path)

        # [Phase 3.2] 개선: os.scandir()로 배치 stat + mtime 기반 스킵
        try:
            for entry in os.scandir(self.target_dir):
                # 디렉토리만 탐색
                if not entry.is_dir():
                    continue

                # 로그 파일 경로 구성
                log_file_path = Path(entry.path) / FileConfig.MONITOR_LOG

                # 파일 존재 확인
                if not log_file_path.exists():
                    continue

                # stat() 한 번만 호출
                try:
                    stat_info = log_file_path.stat()
                except OSError as e:
                    logger.debug(f"로그 파일 접근 실패 {log_file_path}: {e}")
                    continue

                # [Phase 3.2] mtime 기반 스킵 (변경 없는 파일 무시)
                file_key = str(log_file_path)
                current_mtime = stat_info.st_mtime

                # 이전 mtime과 비교
                if file_key in self._last_mtime:
                    if self._last_mtime[file_key] == current_mtime:
                        # 파일 변경 없음 - 스킵
                        continue

                # mtime 업데이트 (파일 변경 감지됨)
                self._last_mtime[file_key] = current_mtime

                # 로그 파일 처리
                self._process_log_file(log_file_path)

        except OSError as e:
            logger.error(f"로그 디렉토리 스캔 실패: {e}")

    def _has_keywords(self) -> bool:
        """키워드가 하나라도 있는지 확인"""
        return bool(self.bot_keywords)

    def _get_merged_keywords(self, dir_name: str) -> list:
        """
        특정 봇에 대한 글로벌 + 봇별 키워드 병합
        봇 키워드가 글로벌 키워드를 덮어씀 (같은 phrase 기준)

        Args:
            dir_name: 봇 디렉토리 이름

        Returns:
            병합된 키워드 목록
        """
        current_bot_keywords = self.bot_keywords.get(dir_name, [])

        # phrase를 키로 사용하여 병합
        merged_keywords_map = {}

        # 봇 키워드 등록
        for kw in current_bot_keywords:
            merged_keywords_map[kw["phrase"]] = kw

        return list(merged_keywords_map.values())

    def _read_new_content(self, log_path: Path) -> tuple:
        """
        로그 파일에서 새로운 콘텐츠만 읽기

        Phase 3.3 최적화: 파일 핸들 재사용으로 open/close 시스템 콜 감소

        Args:
            log_path: 로그 파일 경로

        Returns:
            (new_content, success): 새 콘텐츠와 성공 여부
        """
        with self._file_positions_lock:
            last_pos = self.file_positions.get(str(log_path), 0)

        try:
            current_size = log_path.stat().st_size
            if current_size < last_pos:
                # 로그 로테이션 감지 → 처음부터 읽기
                last_pos = 0

            if current_size <= last_pos:
                return None, False

            # 매번 open/close (Windows에서 FILE_SHARE_DELETE 미지원으로 핸들 캐싱 불가)
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(last_pos)
                new_content = f.read()
                new_pos = f.tell()

            with self._file_positions_lock:
                self.file_positions[str(log_path)] = new_pos

            return new_content, True
        except Exception as e:
            logger.error(f"로그 파일 읽기 실패 ({log_path}): {e}")
            return None, False

    def _process_log_file(self, log_path: Path):
        """
        개별 로그 파일 처리

        Args:
            log_path: 처리할 로그 파일 경로
        """
        dir_name = log_path.parent.name

        new_content, success = self._read_new_content(log_path)
        if not success or not new_content:
            return

        # 정상 종료 phrase 스캔: 키워드 설정과 무관하게 독립적으로 동작
        self._handle_suppress_phrases(new_content, dir_name)

        keywords = self._get_merged_keywords(dir_name)
        if not keywords:
            return

        processed_blocks = set()

        for keyword_info in keywords:
            self._search_keyword_in_content(
                new_content, keyword_info, dir_name, processed_blocks
            )

    def _handle_suppress_phrases(self, content: str, dir_name: str):
        """
        정상 종료 phrase(예: EXIT_RESERVATION)를 콘텐츠에서 스캔합니다.
        키워드 설정과 무관하게 독립적으로 동작하며, 감지 즉시 suppress 콜백 + 텔레그램 선감지 알림을 발송합니다.
        """
        if not self._suppress_alert_phrases:
            return

        for phrase in self._suppress_alert_phrases:
            if phrase not in content:
                continue

            notification_key = (dir_name, f"__suppress__{phrase}")
            now = time.time()
            if (now - self.last_notification_times.get(notification_key, 0)) < 60:
                continue  # 60초 쿨다운 (중복 방지)

            if self._suppress_alert_callback:
                try:
                    self._suppress_alert_callback()
                    logger.debug(
                        f"프로세스 감소 알림 억제: '{phrase}' 감지 ({dir_name})"
                    )
                except Exception as e:
                    logger.warning(f"suppress_alert_callback 호출 실패: {e}")

            # watchdog/폴링 어느 경로든 phrase가 보이는 즉시 선감지 알림 (PnL 포함)
            bot_dir = self.target_dir / dir_name
            from z_pulse.integration.z_flow_bridge import ZFlowBridge
            snap = ZFlowBridge.read_bot_assignment_equity(bot_dir)

            # 현재 equity 조회 (sync 스레드에서 async 호출, GRVT 봇만)
            cur_equity = None
            loop = getattr(self.bot_instance, "main_loop", None)
            is_grvt = dir_name.upper().startswith("GRVT")
            if loop and loop.is_running() and snap and is_grvt:
                try:
                    svc = getattr(self.bot_instance, "grvt_manager", None)
                    if svc is not None:
                        fut = asyncio.run_coroutine_threadsafe(
                            svc.get_total_equity(bot_dir), loop
                        )
                        cur_equity = fut.result(timeout=8)
                except concurrent.futures.TimeoutError:
                    # fut.cancel()은 이미 실행 중인 asyncio 코루틴을 취소하지 않음
                    # (concurrent.futures.Future.cancel은 asyncio 레이어를 인식하지 못함)
                    logger.warning(f"[익절 종료] cur_equity 조회 타임아웃 8초 초과 ({dir_name})")
                    cur_equity = None
                except Exception as e:
                    logger.warning(f"[익절 종료] cur_equity 조회 실패 ({dir_name}): {e}")
                    cur_equity = None

            message = _format_exit_pnl_message(dir_name, snap, cur_equity)
            self._send_notification(message)

            self.last_notification_times[notification_key] = now

    def _handle_kill_action(
        self,
        phrase: str,
        dir_name: str,
        matched_line: str,
        processed_blocks: set,
        match,
        content: str,
    ):
        """
        kill_on_match 키워드 감지 시: 텔레그램 알림 전송 후 해당 봇 프로세스 종료.

        Args:
            phrase: 감지된 키워드
            dir_name: 봇 디렉토리 이름 (종료 대상)
            matched_line: 감지된 로그 라인
            processed_blocks: 중복 처리 방지용 집합
            match: 정규식 매치 객체 (중복 방지 key 계산용)
            content: 전체 로그 콘텐츠
        """
        line_start = content.rfind("\n", 0, match.start()) + 1
        line_end = content.find("\n", match.end())
        if line_end == -1:
            line_end = len(content)
        line_tuple = (line_start, line_end)

        if line_tuple in processed_blocks:
            return
        processed_blocks.add(line_tuple)

        logger.warning(
            f"[Kill] 키워드 '{phrase}' 감지 ({dir_name}) → 봇 프로세스 종료 시작"
        )

        # 1. 텔레그램 알림 (종료 전 먼저 발송)
        from z_pulse.utils import escape_markdown
        from z_pulse.utils.formatters import strip_ansi

        safe_line = strip_ansi(matched_line).replace("`", "'")
        escaped_dir = escape_markdown(dir_name)
        escaped_phrase = escape_markdown(phrase)
        message = (
            f"🔴 *봇 강제 종료* 🔴\n"
            f"📁 디렉토리: `{escaped_dir}`\n"
            f"🔑 Kill 키워드: `{escaped_phrase}`\n"
            f"```text\n{safe_line}\n```"
        )
        self._send_notification(message)

        # 2. 봇 프로세스 종료
        killed = 0
        try:
            pc = getattr(self.bot_instance, "process_controller", None)
            if pc is not None:
                killed = pc.kill_specific_process(dir_name)
                logger.warning(f"[Kill] {dir_name}: {killed}개 프로세스 종료 완료")
            else:
                logger.error("[Kill] process_controller 없음 → 종료 불가")
        except Exception as e:
            logger.error(f"[Kill] 프로세스 종료 중 오류 ({dir_name}): {e}")

        # 3. 결과 후속 알림
        if killed > 0:
            result_msg = (
                f"✅ `{escape_markdown(dir_name)}` 봇 `{killed}`개 프로세스 종료 완료"
            )
        else:
            result_msg = (
                f"⚠️ `{escape_markdown(dir_name)}` 봇 종료 시도했으나 대상 프로세스 없음"
            )
        self._send_notification(result_msg)

    def _search_keyword_in_content(
        self, content: str, keyword_info: dict, dir_name: str, processed_blocks: set
    ):
        """
        콘텐츠에서 키워드 검색 및 알림 분기

        Args:
            content: 검색할 콘텐츠
            keyword_info: 키워드 정보 (phrase, is_json_block, cooldown_seconds)
            dir_name: 봇 디렉토리 이름
            processed_blocks: 이미 처리된 블록 위치 집합
        """
        phrase = keyword_info.get("phrase")
        if not phrase:
            return

        is_json_block_setting = keyword_info.get("is_json_block", False)
        cooldown = keyword_info.get(
            "cooldown_seconds", DurationConfig.COOLDOWN_DEFAULT_SECONDS
        )
        notification_key = (dir_name, phrase)

        kill_on_match = keyword_info.get("kill_on_match", False)

        for match in re.finditer(re.escape(phrase), content):
            # 쿨다운 체크
            now = time.time()
            last_alert_time = self.last_notification_times.get(notification_key, 0)

            if (now - last_alert_time) < cooldown:
                continue

            # kill_on_match: 알림 + 봇 프로세스 즉시 종료
            if kill_on_match:
                matched_line = (
                    self._extract_matched_content(match, content, is_json_block_setting)
                    or ""
                )
                self._handle_kill_action(
                    phrase, dir_name, matched_line, processed_blocks, match, content
                )
                self.last_notification_times[notification_key] = now
                continue

            # JSON 블록 처리 시도
            if is_json_block_setting:
                if self._handle_json_block(
                    match, content, phrase, dir_name, processed_blocks
                ):
                    self.last_notification_times[notification_key] = now
                    continue

            # 일반 텍스트 라인 처리
            if self._handle_text_line(
                match, content, phrase, dir_name, processed_blocks
            ):
                self.last_notification_times[notification_key] = now

    def _extract_matched_content(
        self, match, content: str, is_json_block: bool
    ) -> Optional[str]:
        """
        매칭된 컨텐츠 추출 (핀 키워드용)

        Args:
            match: 정규식 매치 객체
            content: 전체 콘텐츠
            is_json_block: JSON 블록 여부

        Returns:
            추출된 컨텐츠 문자열
        """
        if is_json_block:
            # JSON 블록 추출 시도
            start_brace_pos = content.rfind("{", 0, match.start())
            if start_brace_pos != -1:
                brace_count = 0
                end_brace_pos = -1
                for i in range(start_brace_pos, len(content)):
                    if content[i] == "{":
                        brace_count += 1
                    elif content[i] == "}":
                        brace_count -= 1
                    if brace_count == 0:
                        end_brace_pos = i + 1
                        break

                if end_brace_pos != -1:
                    full_block_text = content[start_brace_pos:end_brace_pos]
                    try:
                        parsed_json = json.loads(full_block_text)
                        return json.dumps(parsed_json, indent=2, ensure_ascii=False)
                    except json.JSONDecodeError:
                        return full_block_text

        # 일반 텍스트 라인 추출
        line_start = content.rfind("\n", 0, match.start()) + 1
        line_end = content.find("\n", match.end())
        if line_end == -1:
            line_end = len(content)
        return content[line_start:line_end].strip()

    def _handle_json_block(
        self, match, content: str, phrase: str, dir_name: str, processed_blocks: set
    ) -> bool:
        """
        JSON 블록 추출 및 알림

        Args:
            match: 정규식 매치 객체
            content: 전체 콘텐츠
            phrase: 검색 키워드
            dir_name: 봇 디렉토리 이름
            processed_blocks: 이미 처리된 블록 위치 집합

        Returns:
            JSON 블록 처리 성공 여부
        """
        start_brace_pos = content.rfind("{", 0, match.start())
        if start_brace_pos == -1:
            return False

        # 중괄호 짝 찾기
        brace_count = 0
        end_brace_pos = -1
        for i in range(start_brace_pos, len(content)):
            if content[i] == "{":
                brace_count += 1
            elif content[i] == "}":
                brace_count -= 1
            if brace_count == 0:
                end_brace_pos = i + 1
                break

        if end_brace_pos == -1:
            return False

        block_tuple = (start_brace_pos, end_brace_pos)
        if block_tuple in processed_blocks:
            return False

        # JSON 블록 추출 및 포맷팅
        line_start_pos = content.rfind("\n", 0, start_brace_pos) + 1
        prefix = content[line_start_pos:start_brace_pos].strip()
        full_block_text = content[start_brace_pos:end_brace_pos]

        try:
            parsed_json = json.loads(full_block_text)
            formatted_block = json.dumps(parsed_json, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            formatted_block = full_block_text

        final_text_block = f"{prefix}\n{formatted_block}".strip()
        # ANSI 이스케이프 시퀀스 제거 (텔레그램은 지원하지 않음)
        final_text_block = strip_ansi(final_text_block)
        message_content = f"```json\n{final_text_block}\n```"

        # 알림 전송
        logger.warning(
            f"로그 키워드 '{phrase}' 발견 ({dir_name}). JSON 블록 알림을 보냅니다."
        )
        escaped_dir = escape_markdown(dir_name)
        escaped_phrase = escape_markdown(phrase)
        message = (
            f"🚨 *로그 키워드 감지 알림* 🚨\n"
            f"📁 디렉토리: `{escaped_dir}`\n"
            f"🔑 키워드: `{escaped_phrase}`\n"
            f"{message_content}"
        )

        if len(message) > SizeConfig.MESSAGE_MAX_LENGTH:
            message = (
                message[: SizeConfig.MESSAGE_SAFE_LENGTH]
                + "\n... (내용이 너무 길어 잘림)```"
            )

        self._send_notification(message)
        processed_blocks.add(block_tuple)
        return True

    def _handle_text_line(
        self, match, content: str, phrase: str, dir_name: str, processed_blocks: set
    ) -> bool:
        """
        일반 텍스트 라인 알림

        Args:
            match: 정규식 매치 객체
            content: 전체 콘텐츠
            phrase: 검색 키워드
            dir_name: 봇 디렉토리 이름
            processed_blocks: 이미 처리된 블록 위치 집합

        Returns:
            텍스트 라인 처리 성공 여부
        """
        line_start = content.rfind("\n", 0, match.start()) + 1
        line_end = content.find("\n", match.end())
        if line_end == -1:
            line_end = len(content)

        matched_line = content[line_start:line_end].strip()
        # ANSI 이스케이프 시퀀스 제거 (텔레그램은 지원하지 않음)
        matched_line = strip_ansi(matched_line)

        # 동일 라인 중복 알림 방지
        line_tuple = (line_start, line_end)
        if line_tuple in processed_blocks:
            return False

        logger.warning(
            f"로그 키워드 '{phrase}' 발견 ({dir_name}). 일반 텍스트 라인 알림을 보냅니다."
        )
        escaped_dir = escape_markdown(dir_name)
        escaped_phrase = escape_markdown(phrase)

        # 백틱 방어
        safe_content = matched_line.replace("`", "'")
        message_content = f"```text\n{safe_content}\n```"

        message = (
            f"🚨 *로그 키워드 감지 알림* 🚨\n"
            f"📁 디렉토리: `{escaped_dir}`\n"
            f"🔑 키워드: `{escaped_phrase}`\n"
            f"{message_content}"
        )

        if len(message) > SizeConfig.MESSAGE_MAX_LENGTH:
            message = (
                message[: SizeConfig.MESSAGE_SAFE_LENGTH]
                + "\n... (내용이 너무 길어 잘림)```"
            )

        self._send_notification(message)
        processed_blocks.add(line_tuple)
        return True
