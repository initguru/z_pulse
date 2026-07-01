"""
인증 모듈

Telegram 봇의 사용자 인증을 담당하는 모듈입니다.
환경변수에서 인증된 채팅 ID를 로드하고, 사용자 인증 여부를 확인합니다.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AuthManager:
    """
    Telegram 봇 사용자 인증 관리자

    환경변수에서 인증된 채팅 ID를 로드하고,
    요청한 사용자가 인증된 사용자인지 확인합니다.

    Example:
        auth = AuthManager()
        auth.load_authorized_chat_id()
        if auth.is_authorized(user_id):
            # 인증된 사용자 처리
    """

    def __init__(self, env_var_name: str = 'TELEGRAM_CHAT_ID'):
        """
        AuthManager 초기화

        Args:
            env_var_name: 인증된 채팅 ID가 저장된 환경변수 이름
        """
        self._env_var_name = env_var_name
        self._authorized_chat_id: Optional[int] = None

    @property
    def authorized_chat_id(self) -> Optional[int]:
        """인증된 채팅 ID를 반환합니다."""
        return self._authorized_chat_id

    @authorized_chat_id.setter
    def authorized_chat_id(self, value: Optional[int]) -> None:
        """인증된 채팅 ID를 설정합니다."""
        self._authorized_chat_id = value

    def load_authorized_chat_id(self) -> Optional[int]:
        """
        환경변수에서 인증된 채팅 ID를 로드합니다.

        Returns:
            로드된 채팅 ID 또는 None (실패 시)
        """
        try:
            chat_id = os.getenv(self._env_var_name)
            if chat_id:
                self._authorized_chat_id = int(chat_id)
                logger.info(f"인증된 채팅 ID 로드: {self._authorized_chat_id}")
                return self._authorized_chat_id
            else:
                logger.warning(f"{self._env_var_name} 환경 변수가 설정되지 않았습니다.")
                self._authorized_chat_id = None
                return None
        except ValueError as e:
            logger.error(f"채팅 ID 변환 실패 (숫자가 아님): {e}")
            self._authorized_chat_id = None
            return None
        except Exception as e:
            logger.error(f"인증된 채팅 ID 로드 실패: {e}")
            self._authorized_chat_id = None
            return None

    def is_authorized(self, user_id: int) -> bool:
        """
        사용자가 인증되었는지 확인합니다.

        Args:
            user_id: 확인할 사용자 ID

        Returns:
            인증된 사용자인 경우 True, 아니면 False
        """
        if self._authorized_chat_id is None:
            logger.warning("인증된 채팅 ID가 설정되지 않았습니다.")
            return False

        return user_id == self._authorized_chat_id
