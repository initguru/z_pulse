"""
bot/keyboard_helper.py - 키보드/명령 헬퍼 모듈

Z-Pulse에서 분리된 UI 헬퍼 함수
- get_main_keyboard(): 메인 ReplyKeyboardMarkup 생성
- handle_command_error(): 명령어 에러 처리
- validate_directory_argument(): 디렉토리 인자 검증
"""

import logging
from typing import Optional

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def get_main_keyboard() -> ReplyKeyboardMarkup:
    """
    메인 키보드 버튼 생성

    Returns:
        ReplyKeyboardMarkup: 메인 메뉴 키보드
    """
    keyboard = [
        [KeyboardButton("대시보드"), KeyboardButton("터미널 정렬")],
        [KeyboardButton("스크린샷"), KeyboardButton("봇 업데이트")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, is_persistent=False)


async def handle_command_error(
    update: Update,
    operation: str,
    error: Exception
) -> None:
    """
    명령어 에러 처리 헬퍼 함수

    Args:
        update: 텔레그램 Update 객체
        operation: 수행 중이던 작업명
        error: 발생한 예외
    """
    logger.error(f"{operation} 중 오류: {error}")
    await update.message.reply_text(f"❌ {operation} 중 오류가 발생했습니다:\n{str(error)}")


async def validate_directory_argument(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    command_name: str
) -> Optional[str]:
    """
    디렉토리 인자 검증 헬퍼 함수

    Args:
        update: 텔레그램 Update 객체
        context: 텔레그램 Context 객체
        command_name: 명령어 이름 (에러 메시지용)

    Returns:
        Optional[str]: 검증된 디렉토리명 또는 None (인자가 없을 경우)
    """
    if not context.args:
        await update.message.reply_text(
            f"❌ 디렉토리명을 입력해주세요.\n"
            f"사용법: /{command_name} <디렉토리명>\n"
            f"예시: /{command_name} lighter\n\n"
            "/status 명령어로 사용 가능한 디렉토리를 확인하세요."
        )
        return None
    return context.args[0]
