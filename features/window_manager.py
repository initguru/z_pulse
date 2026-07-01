"""
Window Manager: 터미널 창 정렬 관리
"""

import asyncio
import logging
import platform
from typing import TYPE_CHECKING, Callable, List, Optional, Set, Union
from asyncio import Task, Future

if TYPE_CHECKING:
    from z_pulse.monitoring.process_monitor import ProcessMonitor
    from z_pulse.monitoring.session_store import SessionRef
    from z_pulse.platforms.base import PlatformHandler

logger = logging.getLogger(__name__)


class WindowManager:
    """터미널 창 정렬을 담당하는 클래스"""

    def __init__(
        self,
        monitor: "ProcessMonitor",
        platform_handler: "PlatformHandler",
        process_name: str,
        z_flow_session_loader: Optional[Callable[[], List["SessionRef"]]] = None,
    ):
        """
        Args:
            monitor: ProcessMonitor 인스턴스 (target_paths 접근용)
            platform_handler: PlatformHandler 인스턴스 (플랫폼별 정렬 실행)
            process_name: 봇 프로세스 이름 (키워드 생성용)
            z_flow_session_loader: Z-Flow 활성 세션 목록 로더 (창 정렬 키워드 보강용)
        """
        self.monitor = monitor
        self.platform_handler = platform_handler
        self.process_name = process_name
        self._z_flow_session_loader = z_flow_session_loader
        self._arrange_task: Optional[Union[Task, Future]] = None  # type: ignore[var-annotated]
        self._arrange_lock: Optional[asyncio.Lock] = None
        self._arrange_pending = False
        self._arrange_settling = False
        self.main_loop = None  # asyncio 메인 루프 참조

    def set_loop(self, loop):
        """
        asyncio 메인 루프를 설정합니다.
        동기 컨텍스트에서 비동기 작업 실행에 필요합니다.

        Args:
            loop: asyncio 이벤트 루프
        """
        self.main_loop = loop
        logger.info("WindowManager에 asyncio 메인 루프 설정 완료")

    def set_z_flow_session_loader(
        self, loader: Optional[Callable[[], List["SessionRef"]]]
    ) -> None:
        """Z-Flow 활성 세션 로더를 주입합니다."""
        self._z_flow_session_loader = loader

    def _build_target_keywords(self) -> Set[str]:
        """정렬 대상 창 필터링을 위한 키워드 집합 생성"""
        keywords = {self.process_name}
        # monitor.target_paths는 런타임에 변경될 수 있음
        # 정렬 시점에 최신 값을 가져와야 함
        for path in self.monitor.target_paths:
            keywords.add(path.parent.name)
        # Z-Flow 활성 세션의 dir_name/custom_title도 키워드에 추가
        if self._z_flow_session_loader:
            try:
                for session in self._z_flow_session_loader():
                    if session.status in ("starting", "running"):
                        keywords.add(session.dir_name)
                        if session.custom_title:
                            keywords.add(session.custom_title)
            except Exception as e:
                logger.warning(f"Z-Flow 세션 키워드 로드 실패 (무시): {e}")
        return keywords

    async def arrange_windows(self) -> int:
        """
        터미널 창 정렬 (플랫폼별 핸들러에 위임)

        Returns:
            정렬된 창의 개수
            -1: macOS에서 접근성 권한 없음 (권한 설정 필요)
            0: 창을 찾지 못함 또는 오류
            >0: 정렬된 창의 개수
        """
        target_keywords = self._build_target_keywords()
        count = 0

        current_platform = platform.system()
        if current_platform == "Windows":
            loop = asyncio.get_running_loop()
            count = await loop.run_in_executor(
                None,
                self.platform_handler.arrange_windows,
                target_keywords,
                self.process_name,
            )
        elif current_platform == "Darwin":
            # macOS handler의 arrange_windows는 async
            count = await self.platform_handler.arrange_windows(  # type: ignore[misc]
                target_keywords, self.process_name
            )
            if count == -1:
                logger.error(
                    "[WindowManager] macOS 접근성 권한 없음. "
                    "MacOSHandler.get_permission_setup_command()를 확인하세요."
                )
                return -1
        else:
            logger.warning(f"지원하지 않는 플랫폼: {current_platform}")
            return 0

        if count > 0:
            await asyncio.sleep(1.0)

        return count

    def trigger_auto_arrange(self) -> None:
        """
        자동 정렬 트리거.

        실행 중인 정렬은 취소하지 않습니다.
        settle delay 중 추가 요청은 현재 예정된 정렬에 흡수하고, 실제 정렬 실행 이후
        들어온 요청만 pending 플래그로 합쳐 마지막 상태를 한 번 더 정렬합니다.
        """
        if self._arrange_task and not self._arrange_task.done():
            if self._arrange_settling:
                return
            self._arrange_pending = True
            return

        # main_loop가 설정되어 있고 실행 중이면 run_coroutine_threadsafe 사용
        if self.main_loop and self.main_loop.is_running():
            future = asyncio.run_coroutine_threadsafe(
                self._auto_arrange_impl(), self.main_loop
            )
            # future를 _arrange_task로 저장 (취소용)
            self._arrange_task = future
            return

        # RuntimeError 방지 (이벤트 루프가 없는 경우)
        coro = self._auto_arrange_impl()
        try:
            self._arrange_task = asyncio.create_task(coro)
        except RuntimeError as e:
            coro.close()
            # 이벤트 루프가 없는 경우 (정상적인 상황)
            logger.debug(f"이벤트 루프가 없어 자동 정렬 태스크 생성 생략: {e}")

    async def _auto_arrange_impl(self) -> None:
        """자동 정렬 실행 (지연 실행, 직렬화 및 pending coalescing)."""
        if self._arrange_lock is None:
            self._arrange_lock = asyncio.Lock()

        async with self._arrange_lock:
            try:
                while True:
                    self._arrange_pending = False
                    await self._run_arrange_once()
                    if not self._arrange_pending:
                        break
            except asyncio.CancelledError:
                logger.debug("자동 창 정렬 태스크 취소됨")

    async def _run_arrange_once(self) -> None:
        """자동 정렬 1회 실행."""
        try:
            # 창이 완전히 뜰 때까지 3초 대기
            self._arrange_settling = True
            try:
                await asyncio.sleep(3)
            finally:
                self._arrange_settling = False

            logger.debug("🖥️ 새 창 감지됨: 자동 정렬 수행 중...")
            count = 0
            missing_attempts = 0
            for attempt in range(1, 4):
                count = await self.arrange_windows()
                if count > 0 or count == -1:
                    break
                missing_attempts = attempt
                await asyncio.sleep(0.5)

            if count > 0:
                logger.debug(f"✅ {count}개 창 자동 정렬 완료")
            elif missing_attempts:
                logger.debug(f"자동 창 정렬 대상 없음 (정렬 생략): retries={missing_attempts}")

        except asyncio.CancelledError:
            logger.debug("자동 창 정렬 태스크 취소됨")
            raise
        except Exception as e:
            logger.error(f"자동 창 정렬 실패: {e}")
