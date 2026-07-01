"""
bot/factory.py - 봇 애플리케이션 팩토리

Z-Pulse의 run() 메서드 내 핸들러 등록 로직 분리
- Application 생성 및 설정
- 핸들러 등록 (Command, Callback, Message, Error)
- post_init 훅 생성
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Callable, Any, Optional

from telegram import (
    Update,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from z_pulse.bot.keyboard_helper import get_main_keyboard
from z_pulse.config.runtime_settings import runtime_settings
from z_pulse.utils.error_handler import is_transient_telegram_error

if TYPE_CHECKING:
    from z_pulse.app import ZPulse

logger = logging.getLogger(__name__)


# ========== 에러 핸들러 ==========
async def error_handler(update: Update, context) -> None:
    """글로벌 에러 처리"""
    error = context.error
    error_msg = str(error)

    # 일시적 네트워크 blip(long-polling read 끊김 등)은 PTB가 자체 백오프 재시도하므로
    # traceback 없이 WARNING으로 다운그레이드한다(ERROR 오탐 방지). 봇 동작엔 영향 없음.
    if is_transient_telegram_error(error):
        logger.warning(
            "일시적 텔레그램 네트워크 오류 (자동 재시도): %s: %s",
            type(error).__name__,
            error_msg,
        )
        return

    logger.error(f"업데이트 처리 중 오류: {error_msg}", exc_info=error)

    # Conflict 에러는 중복 봇 인스턴스 문제이므로 무시
    if "Conflict" in error_msg and "getUpdates" in error_msg:
        logger.warning("중복 봇 인스턴스 감지 - 이 인스턴스를 종료합니다.")
        return

    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해주세요."
            )
    except Exception as e:
        logger.error(f"에러 메시지 전송 실패: {e}")


# ========== 핸들러 등록 테이블 ==========
# 형식: (command_name, handler_attr, method_name)
# handler_attr: bot_instance의 핸들러 객체 속성명
# method_name: 해당 핸들러 객체의 메서드명

COMMAND_HANDLERS = [
    # 기본 명령어
    ("start", "bot_command_handler", "start_command"),
    ("status", "bot_command_handler", "status_command"),
    ("restart", "bot_command_handler", "restart_command"),
    ("restart_all", "bot_command_handler", "restart_all_command"),
    ("restart_clean", "bot_command_handler", "restart_clean_command"),
    ("restart_running", "bot_command_handler", "restart_running_command"),
    ("kill", "bot_command_handler", "kill_command"),
    ("screenshot", "bot_command_handler", "screenshot_command"),
    ("log", "bot_command_handler", "log_command"),
    ("update_bot", "bot_command_handler", "update_bot_command"),
    ("rename", "bot_command_handler", "rename_directory_command"),
    ("arrange_windows", "bot_command_handler", "arrange_windows_command"),
    ("restart_main", "bot_command_handler", "restart_main_command"),
    ("help", "bot_command_handler", "help_command"),
]

# 조건부 명령어 (ECONOMIC_CALENDAR_AVAILABLE 시)
ECONOMIC_COMMAND_HANDLERS = [
    ("economic", "bot_command_handler", "economic_command"),
]

# 봇 명령어 메뉴 정의
BOT_COMMANDS = [
    BotCommand("start", "봇 시작(다시 로딩)"),
    BotCommand("help", "도움말"),
    BotCommand("status", "대시보드"),
    BotCommand("restart_main", "운영봇 재시작"),
    BotCommand("restart", "봇 재시작 (DB 유지)"),
    BotCommand("restart_all", "봇 재시작 (전체)"),
    BotCommand("restart_clean", "봇 재시작 (DB 삭제)"),
    BotCommand("restart_running", "봇 재시작 (실행중)"),
    BotCommand("kill", "봇 종료"),
    BotCommand("screenshot", "스크린샷"),
    BotCommand("log", "봇 로그 (전체)"),
    BotCommand("arrange_windows", "터미널 정렬"),
    BotCommand("update_bot", "봇 업데이트"),
    BotCommand("rename", "디렉토리명 변경"),
]


class BotFactory:
    """텔레그램 봇 애플리케이션 팩토리"""

    @staticmethod
    def _iter_extensions(bot: "ZPulse") -> list[object]:
        return list(getattr(bot, "telegram_extensions", []))

    @staticmethod
    def build_command_handlers(
        bot: "ZPulse", economic_calendar_available: bool = False
    ) -> list[tuple]:
        handlers = list(COMMAND_HANDLERS)
        if economic_calendar_available:
            handlers.extend(ECONOMIC_COMMAND_HANDLERS)
        for extension in BotFactory._iter_extensions(bot):
            get_handlers = getattr(extension, "get_command_handlers", None)
            if callable(get_handlers):
                handlers.extend(get_handlers())  # pyright: ignore[reportArgumentType]
        return handlers

    @staticmethod
    def build_bot_commands(
        bot: "ZPulse", economic_calendar_available: bool = False
    ) -> list[BotCommand]:
        commands = list(BOT_COMMANDS)
        if economic_calendar_available:
            commands.append(BotCommand("economic", "경제지표 확인"))
        for extension in BotFactory._iter_extensions(bot):
            get_commands = getattr(extension, "get_bot_commands", None)
            if callable(get_commands):
                commands.extend(get_commands())  # pyright: ignore[reportArgumentType]
        return commands

    @staticmethod
    def create_application(
        token: str,
        connection_pool_size: int = 16,
        connect_timeout: Optional[float] = None,
        read_timeout: Optional[float] = None,
        write_timeout: Optional[float] = None,
    ) -> Application:
        """
        Application 객체 생성

        Args:
            token: 텔레그램 봇 토큰
            connection_pool_size: 연결 풀 크기
            connect_timeout: 연결 타임아웃 (초)
            read_timeout: 읽기 타임아웃 (초)
            write_timeout: 쓰기 타임아웃 (초)

        Returns:
            설정된 Application 객체
        """
        connect_timeout = (
            connect_timeout
            if connect_timeout is not None
            else runtime_settings.get_float("TELEGRAM_CONNECT_TIMEOUT_SEC", 8.0)
        )
        read_timeout = (
            read_timeout
            if read_timeout is not None
            else runtime_settings.get_float("TELEGRAM_READ_TIMEOUT_SEC", 10.0)
        )
        write_timeout = (
            write_timeout
            if write_timeout is not None
            else runtime_settings.get_float("TELEGRAM_WRITE_TIMEOUT_SEC", 10.0)
        )
        pool_timeout = runtime_settings.get_float("TELEGRAM_POOL_TIMEOUT_SEC", 5.0)
        get_updates_connect_timeout = runtime_settings.get_float(
            "TELEGRAM_GET_UPDATES_CONNECT_TIMEOUT_SEC", 5.0
        )
        get_updates_read_timeout = runtime_settings.get_float(
            "TELEGRAM_GET_UPDATES_READ_TIMEOUT_SEC", 15.0
        )
        get_updates_write_timeout = runtime_settings.get_float(
            "TELEGRAM_GET_UPDATES_WRITE_TIMEOUT_SEC", 10.0
        )

        request = HTTPXRequest(
            connection_pool_size=connection_pool_size,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            write_timeout=write_timeout,
            pool_timeout=pool_timeout,
        )
        get_updates_request = HTTPXRequest(
            connection_pool_size=max(1, min(4, connection_pool_size)),
            connect_timeout=get_updates_connect_timeout,
            read_timeout=get_updates_read_timeout,
            write_timeout=get_updates_write_timeout,
            pool_timeout=pool_timeout,
        )
        logger.info(
            "Telegram request timeout 설정: api(connect=%.1fs read=%.1fs write=%.1fs pool=%.1fs) "
            "get_updates(connect=%.1fs read=%.1fs write=%.1fs)",
            connect_timeout,
            read_timeout,
            write_timeout,
            pool_timeout,
            get_updates_connect_timeout,
            get_updates_read_timeout,
            get_updates_write_timeout,
        )
        return (
            Application.builder()
            .token(token)
            .request(request)
            .get_updates_request(get_updates_request)
            .concurrent_updates(True)
            .build()
        )

    @staticmethod
    def register_handlers(
        app: Application,
        bot: "ZPulse",
        economic_calendar_available: bool = False,
    ) -> None:
        """
        모든 핸들러 등록

        Args:
            app: Application 객체
            bot: ZPulse 인스턴스
            economic_calendar_available: 경제지표 기능 사용 가능 여부
        """
        # 1. Command 핸들러 등록
        BotFactory._register_command_handlers(
            app,
            bot,
            BotFactory.build_command_handlers(bot, economic_calendar_available),
        )

        # 3. Callback Query 핸들러
        app.add_handler(CallbackQueryHandler(bot.button_handler))

        # 4. Message 핸들러
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                bot.settings_handler.handle_text_input,
            ),
            group=1,
        )
        app.add_handler(MessageHandler(filters.TEXT, bot.handle_button_text))

        # 5. Error 핸들러
        app.add_error_handler(error_handler)  # pyright: ignore[reportArgumentType]

        logger.info("핸들러 등록 완료")

    @staticmethod
    def _register_command_handlers(
        app: Application,
        bot: "ZPulse",
        handlers: list[tuple],
    ) -> None:
        """
        Command 핸들러 일괄 등록

        Args:
            app: Application 객체
            bot: ZPulse 인스턴스
            handlers: (command, handler_attr, method_name) 튜플 리스트
        """
        for handler_def in handlers:
            if len(handler_def) == 3:
                cmd, handler_attr, method_name = handler_def
                handler_obj = getattr(bot, handler_attr)
                callback = getattr(handler_obj, method_name)
            else:
                cmd, callback = handler_def
            app.add_handler(CommandHandler(cmd, callback))

    @staticmethod
    def create_post_init_hook(bot: "ZPulse") -> Callable:
        """
        post_init 훅 생성

        Args:
            bot: ZPulse 인스턴스

        Returns:
            post_init 콜백 함수
        """

        async def post_init_hook(application: Application) -> None:
            """봇 시작 후 초기화 작업"""
            try:
                # 1. 메인 이벤트 루프 설정
                bot.main_loop = asyncio.get_running_loop()  # pyright: ignore[reportAttributeAccessIssue]
                bot.monitor.set_loop(bot.main_loop)
                bot.window_manager.set_loop(bot.main_loop)

                # 2. 프로세스 감소 시 대시보드 새로고침 콜백 등록
                bot.monitor.set_process_decrease_callback(
                    bot.dashboard_handler.safe_update_dashboard
                )

                # 3. 프로세스 감소 시 창 정렬을 위한 WindowManager 등록
                bot.monitor.set_window_manager(bot.window_manager)

                # 4. 프로세스 자동 종료 시 cleanup 경로 등록
                bot.monitor.set_cleanup_orchestrator(bot.cleanup_orchestrator)
                bot.monitor.set_cleanup_terminal_callback(
                    bot.platform_handler.cleanup_terminal
                )

                # 5. 봇 명령어 메뉴 설정
                await BotFactory.setup_bot_commands(application, bot)

                # 6. 매니저 의존성 설정
                BotFactory._setup_manager_dependencies(bot)

                # 7. 모니터링 스레드 런타임 의존성 설정
                bot.monitoring_thread.set_runtime_dependencies(
                    main_loop=bot.main_loop,  # pyright: ignore[reportArgumentType]
                    application=bot.application,
                    authorized_chat_id=bot.authorized_chat_id,  # pyright: ignore[reportArgumentType]
                )
                bot.monitoring_thread.set_pair_trading_dependencies(
                    bot.process_controller,
                    bot.monitor,
                )
                bot.monitoring_thread.set_uptime_restart_dependencies(
                    bot.process_action_handler,
                )
                bot.monitoring_thread.start()

                # 8. Telegram/외부 I/O가 Application 시작을 막지 않도록 후속 작업으로 분리
                asyncio.create_task(
                    BotFactory._run_post_start_tasks(bot, application),
                    name="z_pulse_post_start_tasks",
                )
                logger.info("post_init 완료: 후속 Telegram 작업은 백그라운드에서 실행")

            except Exception as e:
                logger.exception("봇 초기화 훅 실행 실패: %s: %s", type(e).__name__, e)

        return post_init_hook

    @staticmethod
    async def setup_bot_commands(application: Application, bot: "ZPulse") -> None:
        """봇 명령어 메뉴 설정 (이전 잔재 purge 후 재등록)"""
        tg = application.bot
        authorized_chat_id: Optional[int] = getattr(bot, "authorized_chat_id", None)

        # 1단계: 모든 scope 잔재 삭제 (옛 명령어 메뉴 클린)
        for scope, label in [
            (BotCommandScopeDefault(), "default"),
            (BotCommandScopeAllPrivateChats(), "all_private_chats"),
        ]:
            try:
                await tg.delete_my_commands(scope=scope)
                logger.debug(f"봇 명령어 메뉴 삭제 완료: scope={label}")
            except Exception as e:
                logger.warning(f"봇 명령어 메뉴 삭제 건너뜀 (scope={label}): {e}")

        if authorized_chat_id:
            try:
                await tg.delete_my_commands(scope=BotCommandScopeChat(chat_id=authorized_chat_id))
                logger.debug(f"봇 명령어 메뉴 삭제 완료: scope=chat({authorized_chat_id})")
            except Exception as e:
                logger.warning(f"봇 명령어 메뉴 삭제 건너뜀 (scope=chat): {e}")

        # 2단계: 클린 목록으로 재등록
        try:
            clean_commands = BotFactory.build_bot_commands(
                bot, getattr(bot, "economic_manager", None) is not None
            )
            await tg.set_my_commands(clean_commands, scope=BotCommandScopeDefault())
            if authorized_chat_id:
                await tg.set_my_commands(
                    clean_commands, scope=BotCommandScopeChat(chat_id=authorized_chat_id)
                )
            logger.info("봇 명령어 메뉴 설정 완료")
        except Exception as e:
            logger.error(f"봇 명령어 메뉴 설정 실패: {e}")

    @staticmethod
    async def _restore_escape_snipers(bot: "ZPulse") -> None:
        """이스케이프 스나이퍼 복원 (stub)"""
        pass

    @staticmethod
    def _setup_manager_dependencies(bot: "ZPulse") -> None:
        """매니저 의존성 설정"""
        pass  # salsal/escape 매니저 제거됨

    @staticmethod
    async def _send_startup_message(
        bot: "ZPulse", application: Application
    ) -> None:
        """봇 시작 알림 전송"""
        if not bot.authorized_chat_id:
            return

        try:
            await application.bot.send_message(
                chat_id=bot.authorized_chat_id,
                text="🤖 Z-Pulse 시작, 기능을 로딩합니다...",
                reply_markup=get_main_keyboard(),
            )
        except Exception as e:
            logger.warning("시작 메시지 전송 실패: %s: %s", type(e).__name__, e)

    @staticmethod
    async def _run_post_start_tasks(
        bot: "ZPulse", application: Application
    ) -> None:
        """Application 시작 이후 수행해도 되는 Telegram/외부 I/O 작업."""
        try:
            await bot.monitoring_thread.bootstrap_economic_update_if_needed()

            tasks = [
                asyncio.create_task(
                    BotFactory._send_startup_message(bot, application),
                    name="z_pulse_startup_message",
                )
            ]
            if bot.authorized_chat_id:
                tasks.append(
                    asyncio.create_task(
                        bot.dashboard_handler.send_initial_dashboard(
                            bot.authorized_chat_id
                        ),
                        name="z_pulse_initial_dashboard",
                    )
                )

            results = await asyncio.gather(*tasks, return_exceptions=True)
            for task, result in zip(tasks, results):
                if isinstance(result, Exception):
                    logger.warning(
                        "시작 후 Telegram 작업 실패: task=%s error=%s: %s",
                        task.get_name(),
                        type(result).__name__,
                        result,
                    )

        except Exception as e:
            logger.exception("시작 후 작업 실패: %s: %s", type(e).__name__, e)
