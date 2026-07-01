"""
DB File Watcher Module

*_DB.json 파일의 실시간 모니터링을 통한 대시보드 자동 갱신.
watchdog 라이브러리를 사용하여 파일 시스템 이벤트를 감지합니다.
"""

import logging
import threading
from os.path import normpath, realpath
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Set, Callable

from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver
from watchdog.events import FileSystemEventHandler

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)

# 설정
DEBOUNCE_SECONDS = 1.5  # 연속 변경 병합 시간
DB_FILE_SUFFIX = "_DB.json"  # 감시 대상 파일 패턴


class DBFileEventHandler(FileSystemEventHandler):
    """
    *_DB.json 파일 이벤트 핸들러.
    디바운싱을 적용하여 연속 변경 시 단일 콜백만 실행합니다.
    """

    def __init__(
        self,
        on_change_callback: Callable[[Set[Path]], None],
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ):
        super().__init__()
        self._on_change_callback = on_change_callback
        self._debounce_seconds = debounce_seconds
        self._debounce_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._changed_dirs: Set[Path] = set()  # 디바운스 동안 변경된 모든 디렉토리 수집

    def on_modified(self, event):
        """파일 수정 이벤트 처리"""
        if event.is_directory:
            return

        src_path = (
            event.src_path.decode()
            if isinstance(event.src_path, bytes)
            else event.src_path
        )
        file_path = Path(src_path)
        if not file_path.name.endswith(DB_FILE_SUFFIX):
            return

        logger.debug(f"DB 파일 변경 감지: {file_path.name}")
        with self._lock:
            self._changed_dirs.add(file_path.parent)
        self._schedule_callback()

    def on_created(self, event):
        """파일 생성 이벤트 처리 (새 DB 파일)"""
        if event.is_directory:
            return

        src_path = (
            event.src_path.decode()
            if isinstance(event.src_path, bytes)
            else event.src_path
        )
        file_path = Path(src_path)
        if not file_path.name.endswith(DB_FILE_SUFFIX):
            return

        logger.debug(f"DB 파일 생성 감지: {file_path.name}")
        with self._lock:
            self._changed_dirs.add(file_path.parent)
        self._schedule_callback()

    def _schedule_callback(self):
        """디바운싱을 적용한 콜백 스케줄링"""
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()

            self._debounce_timer = threading.Timer(
                self._debounce_seconds, self._execute_callback
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _execute_callback(self):
        """
        디바운스 후 콜백 실행

        디바운스 동안 변경된 모든 디렉토리의 캐시를 무효화합니다.
        """
        try:
            # 변경된 디렉토리 수집 후 클리어 (Lock 보호)
            with self._lock:
                dirs_to_invalidate = self._changed_dirs.copy()
                self._changed_dirs.clear()

            # 변경된 모든 디렉토리의 캐시 무효화
            try:
                from z_pulse.config.env_handler import _entry_count_cache, _trading_info_cache

                for changed_dir in dirs_to_invalidate:
                    _entry_count_cache.invalidate(changed_dir)
                    _trading_info_cache.invalidate(changed_dir)

                dir_names = ", ".join(d.name for d in dirs_to_invalidate)
                logger.debug(f"DB 파일 변경 감지 [{dir_names}] → 캐시 무효화 완료")
            except Exception as cache_err:
                logger.warning(f"캐시 무효화 중 오류 (무시됨): {cache_err}")

            self._on_change_callback(dirs_to_invalidate)
        except Exception as e:
            logger.error(f"DB 파일 변경 콜백 오류: {e}")

    def cancel_pending(self):
        """대기 중인 디바운스 타이머 취소"""
        with self._lock:
            if self._debounce_timer:
                self._debounce_timer.cancel()
                self._debounce_timer = None


class DBFileWatcher:
    """
    봇 디렉토리의 *_DB.json 파일 감시자.

    통합 대상:
    - ProcessMonitor.target_paths: 감시할 봇 디렉토리
    - DashboardHandler.trigger_refresh(): 갱신 콜백
    - BotMonitoringThread: 생명주기 관리
    """

    def __init__(
        self,
        main_loop: Optional["asyncio.AbstractEventLoop"] = None,
        debounce_seconds: float = DEBOUNCE_SECONDS,
    ):
        self._main_loop = main_loop
        self._debounce_seconds = debounce_seconds
        self._observer: Optional[BaseObserver] = None
        self._event_handler: Optional[DBFileEventHandler] = None
        self._watched_directories: Set[Path] = set()
        self._dashboard_refresh_callback: Optional[Callable] = None
        self._entry_count_log_callback: Optional[Callable] = None  # 콘솔 진입 횟수 갱신
        self._lock = threading.Lock()

    @staticmethod
    def _normalize_directory(dir_path: Path) -> Path:
        return Path(realpath(normpath(str(dir_path))))

    def set_main_loop(self, loop: "asyncio.AbstractEventLoop") -> None:
        """asyncio 메인 루프 설정"""
        self._main_loop = loop

    def set_dashboard_refresh_callback(self, callback: Callable) -> None:
        """
        대시보드 갱신 콜백 설정.

        Args:
            callback: DashboardHandler.trigger_refresh 메서드
        """
        self._dashboard_refresh_callback = callback

    def set_entry_count_log_callback(
        self, callback: Callable[[Set[Path]], None]
    ) -> None:
        """
        DB 파일 변경 시 콘솔 진입 횟수 갱신 콜백 설정.

        Args:
            callback: ProcessMonitor.log_entry_counts 메서드
        """
        self._entry_count_log_callback = callback

    def _on_file_change(self, changed_dirs: Set[Path]) -> None:
        """파일 변경 감지 시 호출 (디바운스 후)"""
        # 콘솔 진입 횟수 즉시 갱신 (동기 - Timer 스레드에서 직접 호출)
        if self._entry_count_log_callback:
            try:
                self._entry_count_log_callback(changed_dirs)
            except Exception as e:
                logger.warning(f"콘솔 진입 횟수 갱신 실패: {e}")

        # 텔레그램 대시보드 갱신 (비동기 - 메인 루프로 위임)
        if not self._dashboard_refresh_callback:
            logger.warning("대시보드 갱신 콜백이 설정되지 않음")
            return

        if self._main_loop and self._main_loop.is_running():
            import asyncio

            asyncio.run_coroutine_threadsafe(
                self._async_trigger_refresh(), self._main_loop
            )
        else:
            logger.warning("메인 루프 미실행, 대시보드 갱신 건너뜀")

    async def _async_trigger_refresh(self) -> None:
        """비동기 대시보드 갱신 트리거"""
        try:
            # trigger_refresh(query=None)을 호출하여 추적 중인 대시보드 갱신
            callback = self._dashboard_refresh_callback
            if callback is None:
                return
            callback(query=None, force_rescan=False)
        except Exception as e:
            logger.error(f"대시보드 갱신 실패: {e}")

    def start(self, directories: Set[Path]) -> None:
        """
        지정된 디렉토리 감시 시작.

        Args:
            directories: 감시할 봇 디렉토리 경로 집합
        """
        with self._lock:
            if self._observer and self._observer.is_alive():
                logger.warning("파일 감시자가 이미 실행 중")
                return

            normalized_directories = {
                self._normalize_directory(dir_path) for dir_path in directories
            }

            self._event_handler = DBFileEventHandler(
                on_change_callback=self._on_file_change,
                debounce_seconds=self._debounce_seconds,
            )

            self._observer = Observer()
            self._watched_directories.clear()
            observer = self._observer
            event_handler = self._event_handler

            if event_handler is None:
                return

            for dir_path in sorted(normalized_directories):
                if dir_path.exists() and dir_path.is_dir():
                    try:
                        observer.schedule(
                            event_handler,
                            str(dir_path),
                            recursive=False,  # 해당 디렉토리만 감시
                        )
                    except RuntimeError as exc:
                        if "already scheduled" in str(exc):
                            logger.warning(f"중복 디렉토리 감시 스킵: {dir_path}")
                            continue
                        raise

                    self._watched_directories.add(dir_path)
                    logger.debug(f"디렉토리 감시 시작: {dir_path.name}")

            observer.start()
            logger.info(
                f"DB 파일 감시자 시작: {len(self._watched_directories)}개 디렉토리"
            )

    def stop(self) -> None:
        """파일 감시 중지"""
        with self._lock:
            if self._event_handler:
                self._event_handler.cancel_pending()

            if self._observer:
                self._observer.stop()
                self._observer.join(timeout=5.0)
                self._observer = None

            self._watched_directories.clear()
            logger.info("DB 파일 감시자 중지")

    def update_watch_list(self, new_directories: Set[Path]) -> None:
        """
        감시 디렉토리 목록 업데이트.
        봇 추가/제거 시 호출됩니다.

        Args:
            new_directories: 새로운 감시 대상 디렉토리 집합
        """
        with self._lock:
            normalized_directories = {
                self._normalize_directory(dir_path) for dir_path in new_directories
            }

            added = normalized_directories - self._watched_directories
            removed = self._watched_directories - normalized_directories

            if not added and not removed:
                return  # 변경 없음

            logger.info(f"감시 목록 변경: +{len(added)}, -{len(removed)}")

            # 간단한 구현: 재시작
            self.stop()
            if normalized_directories:
                self.start(normalized_directories)

    @property
    def is_running(self) -> bool:
        """감시자 실행 여부"""
        return self._observer is not None and self._observer.is_alive()
