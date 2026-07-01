"""
플랫폼별 추상 인터페이스

OS별 로직 분리 (창 정렬, 터미널 정리, 프로세스 시작)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Tuple, Optional, Set, Any, Union
import logging

logger = logging.getLogger(__name__)


class PlatformHandler(ABC):
    """
    플랫폼별 작업을 처리하는 추상 기본 클래스

    Windows와 macOS에서 각각 다르게 동작해야 하는 기능들을 추상화합니다:
    - 창 정렬 (Window Arrangement)
    - 터미널 정리 (Terminal Cleanup)
    - 봇 프로세스 시작 (Bot Process Start)
    """

    @abstractmethod
    def arrange_windows(
        self,
        target_keywords: Set[str],
        process_name: str
    ) -> int:
        """
        터미널 창을 격자 형태로 정렬

        Args:
            target_keywords: 창 제목에서 검색할 키워드 집합
            process_name: 프로세스 이름 (추가 키워드)

        Returns:
            정렬된 창의 개수
        """
        pass

    @abstractmethod
    async def cleanup_terminal(self, filter_keyword: str) -> dict[str, object]:
        """
        터미널 창 정리 (종료)

        Args:
            filter_keyword:
                - 개별 봇 종료 시: 'target_dir' (예: flipster-btc)
                - 전체 봇 종료 시: 'process_name' (예: Z-Pulse 실행 파일명)
                이 키워드가 창 제목에 포함된 모든 터미널 탭/창을 종료합니다.
        """
        pass

    async def terminate_process_group(self, session) -> Union[bool, dict[str, Any]]:
        target = getattr(session, "dir_name", None)
        if not target:
            return False
        result = await self.cleanup_terminal(target)
        return bool(result.get("ok", False)) if isinstance(result, dict) else bool(result)

    async def terminate_tty_processes(self, session) -> Union[bool, dict[str, Any]]:
        target = getattr(session, "dir_name", None)
        if not target:
            return False
        result = await self.cleanup_terminal(target)
        return bool(result.get("ok", False)) if isinstance(result, dict) else bool(result)

    async def close_window(self, session) -> Union[bool, dict[str, Any]]:
        target = getattr(session, "custom_title", None) or getattr(session, "dir_name", None)
        if not target:
            return False
        result = await self.cleanup_terminal(target)
        return bool(result.get("ok", False)) if isinstance(result, dict) else bool(result)


    @abstractmethod
    def generate_start_command(
        self,
        dir_name: str,
        bot_path: str,
        executable: str
    ) -> str:
        """
        봇 시작 명령어/스크립트 생성

        Args:
            dir_name: 디렉토리 이름 (봇 이름)
            bot_path: 봇 실행 파일이 있는 경로
            executable: 실행 파일명 (예: Z-Pulse 실행 파일 또는 main.py)

        Returns:
            실행 가능한 명령어 문자열
        """
        pass

    @abstractmethod
    async def start_bot_process(
        self,
        dir_name: str,
        bot_path: str,
        executable: str,
        *,
        launch_spec: Optional[dict] = None,
    ) -> bool:
        """
        봇 프로세스 시작

        Args:
            dir_name: 디렉토리 이름 (봇 이름)
            bot_path: 봇 실행 파일이 있는 경로
            executable: 실행 파일명
            launch_spec: launch_spec dict (cmd, cwd). 제공 시 AppleScript 대신 직접 subprocess 기동.

        Returns:
            시작 성공 여부
        """
        pass

    @abstractmethod
    async def run_shell_command(
        self,
        command: str,
        is_applescript: bool = False
    ) -> Tuple[int, str, str]:
        """
        쉘 명령어를 비동기로 실행

        Args:
            command: 실행할 명령어 (또는 AppleScript 코드)
            is_applescript: True일 경우 osascript로 실행 (macOS 전용)

        Returns:
            (returncode, stdout, stderr) 튜플
        """
        pass

    def get_platform_name(self) -> str:
        """플랫폼 이름 반환"""
        return self.__class__.__name__.replace("Handler", "")
