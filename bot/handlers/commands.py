"""
Bot Command Handler Module

텔레그램 봇의 슬래시(/) 명령어 처리를 담당하는 모듈입니다.
ZPulse 클래스에서 명령어 로직을 분리하여 관리합니다.
"""

import asyncio
import logging
import os
import shutil
import subprocess
import platform
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from datetime import datetime
from typing import Any, Optional, Union, cast

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.ext import ContextTypes

from z_pulse.bot.factory import BotFactory
from z_pulse.config.runtime_settings import runtime_settings
from z_pulse.bot.keyboard_helper import (
    get_main_keyboard,
    validate_directory_argument as ui_validate_directory_argument,
)
from z_pulse.bot.utils import run_batch_operations
from z_pulse.utils.markdown_utils import escape_markdown
from z_pulse.utils.formatters import format_batch_result
from z_pulse.utils.async_helpers import safe_send_message_with_result
from z_pulse.utils.telegram_gateway import TelegramPriority

# 조건부 라이브러리 임포트
try:
    import gdown
except ImportError:
    gdown = None

try:
    import pyautogui

    SCREENSHOT_AVAILABLE = True
    PIL_AVAILABLE = True
    from PIL import ImageGrab
except ImportError:
    pyautogui = None
    SCREENSHOT_AVAILABLE = False
    PIL_AVAILABLE = False
    ImageGrab = None

logger = logging.getLogger(__name__)
SCREENSHOT_TIMEOUT_SECONDS = 30.0

TextRouteHandler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
TextRouteValue = Union[str, TextRouteHandler]


class _SlashCommandQueryAdapter:
    """슬래시 커맨드를 CallbackQuery 인터페이스로 연결하는 경량 어댑터.

    simple_restart / clean_run 같은 콜백 기반 함수를 슬래시 커맨드에서 재사용할 때 사용한다.
    - query.answer(text)         → message.reply_text(text)  (토스트 대신 일반 메시지)
    - query.edit_message_text()  → message.reply_text(text)  (기존 메시지 편집 대신 새 메시지)
    - query.message              → 원본 telegram.Message 반환
    """

    def __init__(self, message: Message) -> None:
        self.message = message

    async def answer(self, text: str = "", show_alert: bool = False, **_kwargs: Any) -> None:
        if text:
            await self.message.reply_text(text)

    async def edit_message_text(self, text: str, **_kwargs: Any) -> None:
        await self.message.reply_text(text)


def _require_message(update: Update) -> Message:
    message = update.message
    if message is None:
        raise ValueError("update.message is required")
    return message


def _require_user_id(update: Update) -> int:
    user = update.effective_user
    if user is None:
        raise ValueError("update.effective_user is required")
    return user.id


def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[Any, Any]:
    data = context.user_data
    if data is None:
        raise ValueError("context.user_data is required")
    return data


class BotCommandHandler:
    def __init__(
        self,
        bot_instance,
        monitor,
        process_controller,
        file_operations,
        economic_manager,
        window_manager,
        dashboard_handler,
        settings_handler,
        grvt_manager=None,
    ):
        self.bot = bot_instance
        self.monitor = monitor
        self.process_controller = process_controller
        self.file_operations = file_operations
        self.economic_manager = economic_manager
        self.window_manager = window_manager
        self.dashboard_handler = dashboard_handler
        self.settings_handler = settings_handler
        self.grvt_manager = grvt_manager
        self._screenshot_lock = asyncio.Lock()
        self._arrange_windows_lock = asyncio.Lock()

    async def _safe_reply_text(
        self,
        message: Message,
        text: str,
        *,
        parse_mode: Optional[str] = None,
        max_retries: int = 2,
        base_delay: float = 0.4,
    ) -> None:
        result = await safe_send_message_with_result(
            self.bot.application.bot,
            chat_id=message.chat_id,
            text=text,
            parse_mode=parse_mode or "MarkdownV2",
            priority=TelegramPriority.USER_ACTION,
            timeout=8.0,
            max_retries=max_retries,
            base_delay=base_delay,
        )
        if result is None:
            logger.warning(
                "명령 응답 전송 실패: chat_id=%s text_prefix=%s",
                message.chat_id,
                text[:32],
            )

    async def check_authorization(self, update: Update) -> bool:
        """인증 체크 헬퍼"""
        try:
            user_id = _require_user_id(update)
        except ValueError:
            logger.warning("effective_user 없이 인증 체크가 호출되었습니다.")
            return False

        if not self.bot.is_authorized(user_id):
            message = update.effective_message
            if message is not None:
                await message.reply_text(
                    "❌ 인증되지 않은 사용자입니다.\n\n"
                    "인증 방법:\n"
                    "• setting.env 파일의 TELEGRAM_CHAT_ID를 확인하세요"
                )
            return False
        return True

    # ========== UI/헬퍼 분리 - bot/keyboard_helper.py로 위임 ==========
    async def validate_directory_argument(
        self, update: Update, context, command_name: str
    ) -> Optional[str]:
        """디렉토리 인자 검증 - bot/keyboard_helper.py로 위임"""
        return await ui_validate_directory_argument(update, context, command_name)

    def get_main_keyboard(self):
        """메인 키보드 버튼 생성 - bot/keyboard_helper.py로 위임"""
        return get_main_keyboard()

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """시작 명령어"""
        if not await self.check_authorization(update):
            return

        welcome_text = (
            "🤖 Z-Pulse 모니터링 시스템에 오신 것을 환영합니다!\n\n"
            "📋 주요 기능:\n"
            "• 🔍 대시보드로 프로세스 상태 확인\n"
            "• 🔄 실행, 종료, 재시작 관리\n"
            "• 📸 스크린샷 및 로그 확인\n\n"
            "💡 사용법:\n"
            "• /help - 전체 명령어 목록\n"
            "• /대시보드 - 대시보드\n"
            "• 키보드 버튼으로 빠르게 접근"
        )

        message = _require_message(update)
        await message.reply_text(
            welcome_text, reply_markup=self.get_main_keyboard()
        )

        # Slash 명령 메뉴를 서버에 즉시 재등록 (purge → 클린 목록)
        try:
            await BotFactory.setup_bot_commands(self.bot.application, self.bot)
        except Exception as e:
            logger.warning(f"/start 중 슬래시 메뉴 갱신 실패: {e}")

        if self.economic_manager:
            await message.reply_text(
                "잠시 후 오늘의 주요 경제지표를 불러옵니다..."
            )
            await self.economic_command(update, context)

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """도움말 명령어"""
        if not await self.check_authorization(update):
            return

        help_lines = [
            "🤖 Z-Pulse 모니터링 시스템",
            "",
            "📋 사용 가능한 명령어:",
            "",
            "🚀 /start - 봇 시작(다시 로딩)",
            "❓ /help - 이 도움말",
            "🔍 /status - 대시보드",
            "🔁 /restart_main - 운영봇 재시작",
            "🔄 /restart <봇명> - 봇 재시작 (DB 유지)  예: /restart btc_eth",
            "🔥 /restart_all - 봇 재시작 (전체)",
            "✨ /restart_clean <봇명> - 봇 재시작 (DB 삭제)  예: /restart_clean btc_eth",
            "▶️ /restart_running - 봇 재시작 (실행중)",
            "🛑 /kill <봇명> - 봇 종료  예: /kill btc_eth",
            "📸 /screenshot - 스크린샷",
            "📄 /log - 봇 로그 (전체)",
            "📐 /arrange_windows - 터미널 정렬",
            "🔄 /update_bot - 봇 업데이트",
            "📁 /rename <old> <new> - 디렉토리명 변경  예: /rename old_name new_name",
        ]
        for extension in getattr(self.bot, "telegram_extensions", []):
            get_help_lines = getattr(extension, "get_help_lines", None)
            if callable(get_help_lines):
                extension_lines = cast(Optional[Iterable[str]], get_help_lines())
                if extension_lines:
                    help_lines.extend(list(extension_lines))
        help_text = "\n".join(help_lines)
        message = _require_message(update)
        await message.reply_text(
            help_text, reply_markup=self.get_main_keyboard()
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """상태 확인 (대시보드)"""
        if not await self.check_authorization(update):
            return
        await self.dashboard_handler.update_dashboard(update=update)

    async def restart_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """프로세스 재시작 (DB유지) — /restart <봇명>"""
        if not await self.check_authorization(update):
            return
        target_dir = await self.validate_directory_argument(update, context, "restart")
        if not target_dir:
            return
        adapter = _SlashCommandQueryAdapter(_require_message(update))
        await self.bot.process_action_handler.simple_restart(adapter, target_dir)

    async def restart_all_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """모든 프로세스 재시작 (확인 버튼)"""
        if not await self.check_authorization(update):
            return

        message = _require_message(update)
        keyboard = [
            [InlineKeyboardButton("✅ 확인", callback_data="restart_all_confirm")],
            [InlineKeyboardButton("❌ 취소", callback_data="cancel")],
        ]
        await message.reply_text(
            "⚠️ 모든 프로세스를 재시작하시겠습니까?\n\n"
            "이 작업은 현재 실행 중인 모든 프로세스를 강제로 종료하고 재시작합니다.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def kill_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """프로세스 종료"""
        if not await self.check_authorization(update):
            return
        message = _require_message(update)
        target_dir = await self.validate_directory_argument(update, context, "kill")
        if not target_dir:
            return

        try:
            is_running, _ = self.process_controller.is_process_running(target_dir)
            if not is_running:
                await message.reply_text(
                    f"❌ '{target_dir}' 디렉토리에서 실행 중인 프로세스가 없습니다."
                )
                return

            killed_count = self.process_controller.kill_specific_process(target_dir)
            if killed_count > 0:
                await message.reply_text(
                    f"✅ '{target_dir}' 프로세스 종료 완료!\n"
                    f"⏰ 10초 후 터미널 창에서 자동으로 빠져나갑니다..."
                )
                await asyncio.sleep(10)
                await self.bot._cleanup_terminal_for_dashboard(target_dir)
                await message.reply_text(
                    f"🧹 '{target_dir}' 터미널 창 정리 완료!"
                )
                if self.window_manager:
                    self.window_manager.trigger_auto_arrange()
            else:
                await message.reply_text(
                    f"❌ '{target_dir}' 프로세스 종료에 실패했습니다."
                )

            self.monitor.find_target_programs()
        except Exception as e:
            logger.error(f"종료 오류: {e}")
            await message.reply_text(f"❌ 오류: {e}")

    async def restart_running_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """실행 중인 봇만 재시작 — /restart_running"""
        if not await self.check_authorization(update):
            return
        adapter = _SlashCommandQueryAdapter(_require_message(update))
        await self.bot.process_action_handler.restart_running_only(adapter)

    async def restart_clean_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """DB 초기화 후 재시작 — /restart_clean <봇명>"""
        if not await self.check_authorization(update):
            return
        target_dir = await self.validate_directory_argument(update, context, "restart_clean")
        if not target_dir:
            return
        adapter = _SlashCommandQueryAdapter(_require_message(update))
        await self.bot.process_action_handler.clean_run(adapter, target_dir)

    async def screenshot_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """스크린샷 캡처"""
        if not await self.check_authorization(update):
            return
        message = _require_message(update)
        if not SCREENSHOT_AVAILABLE:
            await message.reply_text(
                "❌ 스크린샷 라이브러리(pyautogui, Pillow)가 없습니다."
            )
            return

        if self._screenshot_lock.locked():
            await message.reply_text("⏳ 스크린샷 생성이 이미 진행 중입니다.")
            return
        started_at = time.monotonic()
        try:
            await self._screenshot_lock.acquire()
            status_msg = await message.reply_text(
                "🪟 터미널 창 정렬 후 스크린샷을 생성합니다..."
            )

            try:
                await self.window_manager.arrange_windows()
            except Exception as e:
                logger.error(f"정렬 실패: {e}")
                await status_msg.edit_text(
                    f"⚠️ 정렬 실패 ({e}). 스크린샷만 생성합니다..."
                )

            temp_dir = tempfile.gettempdir()
            timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
            screenshot_path = os.path.join(temp_dir, f"screenshot_full_{timestamp}.png")

            def _take_screenshot():
                grab_error: Optional[Exception] = None
                if ImageGrab is not None:
                    try:
                        shot = ImageGrab.grab(all_screens=True)
                        shot.save(screenshot_path)
                        return shot.size
                    except Exception as e:
                        grab_error = e

                if pyautogui is None:
                    raise RuntimeError(
                        "screenshot backend unavailable"
                        + (f": {grab_error}" if grab_error else "")
                    )

                try:
                    shot = pyautogui.screenshot()
                except Exception as e:
                    raise RuntimeError(
                        "스크린샷 캡처 실패 — macOS Screen Recording 권한이 필요합니다.\n"
                        "시스템 설정 → 개인정보 보호 및 보안 → 화면 기록 → "
                        "Terminal(또는 Python)을 허용하세요."
                    ) from e

                shot.save(screenshot_path)
                return shot.size

            loop = asyncio.get_running_loop()
            width, height = await asyncio.wait_for(
                loop.run_in_executor(None, _take_screenshot),
                timeout=SCREENSHOT_TIMEOUT_SECONDS,
            )

            if os.path.exists(screenshot_path):
                esc_timestamp = escape_markdown(timestamp)
                caption = f"📸 전체 화면 스크린샷 \\({esc_timestamp}\\)\n🖥️ 크기: {width}x{height}"
                success = await self.file_operations.send_file_async(
                    update, screenshot_path, caption, file_type="photo"
                )
                if success:
                    os.remove(screenshot_path)
            else:
                await message.reply_text("❌ 파일 생성 실패.")
        except Exception as e:
            logger.error(f"스크린샷 오류: {e}")
            await message.reply_text(f"❌ 오류: {e}")
        finally:
            if self._screenshot_lock.locked():
                self._screenshot_lock.release()
            logger.info("[HANDLER][TIMING] operation=screenshot elapsed=%.3fs", time.monotonic() - started_at)

    async def log_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """로그 파일 수집 (실행 중인 봇만 대상)"""
        if not await self.check_authorization(update):
            return

        message = _require_message(update)
        status_msg = await message.reply_text(
            "🔍 실행 중인 봇의 로그 파일을 확인하고 있습니다..."
        )

        try:
            # 1. 실행 중인 프로세스 목록 확인 (최신 상태)
            running_target_paths = set()
            # force_refresh=True로 현재 실행 상태를 확실하게 조회
            for _, path in self.monitor.find_processes(force_refresh=True):
                running_target_paths.add(path)

            targets_to_send = []

            # 2. 실행 중인 봇만 필터링하여 로그 파일 확인
            for target_path in self.monitor.target_paths:
                # [수정] 실행 중이지 않은 봇은 건너뜀
                if target_path not in running_target_paths:
                    continue

                log_file_path = target_path.parent / "monitor.log"
                if (
                    log_file_path.exists()
                    and log_file_path.is_file()
                    and log_file_path.stat().st_size > 0
                ):
                    targets_to_send.append(target_path)

            if not targets_to_send:
                await status_msg.edit_text(
                    "⚠️ 현재 실행 중인 봇이 없거나 로그 파일이 비어있습니다."
                )
                return

            await status_msg.edit_text(
                f"🚀 실행 중인 {len(targets_to_send)}개 봇의 로그를 병렬 수집합니다..."
            )

            # 3. 로그 전송 수행
            async def send_log_operation(target_path: Path) -> bool:
                return await self.file_operations.send_log_helper(
                    update, target_path, tail=None
                )

            sent_count = await run_batch_operations(
                targets_to_send, send_log_operation, batch_size=5, delay=0.2
            )

            if sent_count > 0:
                await status_msg.edit_text(
                    f"✅ 총 {sent_count}개의 로그 파일을 전송했습니다."
                )
            else:
                await status_msg.edit_text("⚠️ 로그 파일 전송 실패.")
        except Exception as e:
            logger.error(f"로그 수집 오류: {e}")
            await message.reply_text(f"❌ 오류: {e}")

    async def arrange_windows_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """창 정렬"""
        if not await self.check_authorization(update):
            return

        message = _require_message(update)
        current_os = platform.system()
        if current_os not in ("Windows", "Darwin"):
            await message.reply_text("⛔️ Windows와 macOS만 지원합니다.")
            return

        if self._arrange_windows_lock.locked():
            await message.reply_text("⏳ 터미널 창 정렬이 이미 진행 중입니다.")
            return
        started_at = time.monotonic()
        try:
            await self._arrange_windows_lock.acquire()
            os_emoji = "🪟" if current_os == "Windows" else "🍏"
            await message.reply_text(f"{os_emoji} 터미널 창들을 정렬합니다...")
            count = await self.window_manager.arrange_windows()

            if count == -1 and current_os == "Darwin":
                # macOS 접근성 권한 없음
                from z_pulse.platforms.macos import MacOSHandler

                help_text = MacOSHandler.get_permission_setup_command()
                await message.reply_text(
                    "❌ macOS 접근성 권한이 필요합니다.\n\n"
                    "자동으로 권한 설정 화면이 열립니다.\n"
                    "권한을 부여한 후 프로그램을 재시작해주세요.\n\n"
                    f"```\n{help_text}\n```",
                    parse_mode="Markdown",
                )
            elif count > 0:
                await message.reply_text(f"✅ {count}개의 창을 정렬했습니다.")
            else:
                await message.reply_text("⚠️ 정렬할 창을 찾지 못했습니다.")
        except Exception as e:
            logger.error(f"정렬 오류: {e}")
            await message.reply_text(f"❌ 오류: {e}")
        finally:
            if self._arrange_windows_lock.locked():
                self._arrange_windows_lock.release()
            logger.info("[HANDLER][TIMING] operation=terminal_arrange elapsed=%.3fs", time.monotonic() - started_at)

    async def economic_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """경제지표 확인"""
        if not await self.check_authorization(update):
            return
        reply_message = _require_message(update)
        if not self.economic_manager:
            await reply_message.reply_text("❌ 경제지표 기능이 비활성화되어 있습니다.")
            return

        try:
            last_update = getattr(self.bot, "last_economic_update", None)
            if not isinstance(last_update, datetime):
                last_update = None
                get_status_summary = getattr(
                    self.economic_manager, "get_status_summary", None
                )
                if callable(get_status_summary):
                    status = get_status_summary()
                    last_success_at = (
                        status.get("last_success_at", "")
                        if isinstance(status, dict)
                        else ""
                    )
                    if isinstance(last_success_at, str) and last_success_at:
                        try:
                            last_update = datetime.fromisoformat(last_success_at)
                        except ValueError:
                            logger.warning(
                                f"last_success_at 파싱 실패: {last_success_at}"
                            )
            formatted_message = self.economic_manager.format_events_message(
                days=7, max_events=8, last_update=last_update
            )
            if formatted_message:
                await reply_message.reply_text(formatted_message, parse_mode="MarkdownV2")
            else:
                await reply_message.reply_text(
                    self.economic_manager.build_empty_reason_message()
                )
        except Exception as e:
            logger.error(f"경제지표 오류: {e}")
            await reply_message.reply_text("❌ 오류 발생.")

    def _get_update_binary_path(self) -> Optional[Path]:
        """.update 폴더의 updater 바이너리 경로 반환"""
        update_dir = Path(__file__).resolve().parents[2] / ".update"
        binary_path = update_dir / self.bot.process_name
        if not binary_path.exists():
            return None
        return binary_path

    async def _run_update_binary(
        self,
        binary_path: Path,
        on_progress: Optional[Callable[[str], Awaitable[None]]] = None,
        on_line: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> tuple[int, str]:
        """updater 바이너리 실행 후 출력 수집 (on_progress 지정 시 실시간 스트리밍)"""
        _EDIT_DEBOUNCE_SEC = 3.0
        _MAX_DISPLAY_CHARS = 3000

        process = await asyncio.create_subprocess_exec(
            str(binary_path),
            cwd=str(binary_path.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        lines: list[str] = []
        _last_edit_time: float = 0.0

        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="ignore").rstrip()
            if line:
                lines.append(line)
                if on_line is not None:
                    await on_line(line)

            if on_progress is not None:
                now = asyncio.get_event_loop().time()
                if now - _last_edit_time >= _EDIT_DEBOUNCE_SEC:
                    display = self._truncate_for_telegram(
                        "\n".join(lines), _MAX_DISPLAY_CHARS
                    )
                    await on_progress(display)
                    _last_edit_time = now

        await process.wait()
        full_output = "\n".join(lines)

        if on_progress is not None:
            display = self._truncate_for_telegram(full_output, _MAX_DISPLAY_CHARS)
            await on_progress(display)

        returncode = process.returncode
        if returncode is None:
            returncode = 1
        return returncode, full_output

    @staticmethod
    def _truncate_for_telegram(text: str, max_chars: int) -> str:
        """텔레그램 전송 전 텍스트를 max_chars 이하로 자른다 (마지막 N 줄 유지)."""
        if len(text) <= max_chars:
            return text
        truncated = text[-(max_chars - 2):]  # "…\n" 2자 예약
        nl = truncated.find("\n")
        if nl >= 0:
            truncated = truncated[nl + 1:]
        return "…\n" + truncated

    @staticmethod
    def _parse_update_output(output: str) -> tuple[str, str, Optional[str]]:
        """updater 출력에서 상태, 사용자 메시지, 버전 변경 안내 추출"""
        normalized = output.strip()
        lines = [line.strip() for line in normalized.splitlines() if line.strip()]
        last_line = lines[-1] if lines else "출력이 없습니다."

        current_version = None
        updated_version = None
        for line in lines:
            if line.startswith("[Updater] Current version:"):
                current_version = line.split(":", 1)[1].strip()
            if "[Updater] Updated to " in line:
                updated_version = line.split("Updated to ", 1)[1].split("!", 1)[0].strip()

        version_message: Optional[str] = None
        if current_version and updated_version:
            version_message = f"🆕 업데이트 버전 확인\n{current_version} -> {updated_version}"

        if "Already up to date." in normalized:
            return "up_to_date", "✅ 이미 최신 버전입니다.", None
        if "Updated to " in normalized:
            return "updated", last_line, version_message
        return "failed", f"❌ 업데이트 실행 실패\n{last_line}", None

    async def _announce_update_status(
        self,
        update: Update,
        status_msg,
        version_message: Optional[str],
        next_message: str,
    ):
        """버전 안내는 신규 메시지로 남기고 상태 메시지는 별도로 갱신"""
        message = _require_message(update)
        if version_message:
            await message.reply_text(version_message)
        await status_msg.edit_text(next_message)

    async def update_bot_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """봇 업데이트"""
        if not await self.check_authorization(update):
            return

        message = _require_message(update)
        update_binary = self._get_update_binary_path()
        if update_binary is None:
            logger.warning("[UPDATE] 바이너리 없음: .update/%s", self.bot.process_name)
            await message.reply_text(
                f"❌ .update/{self.bot.process_name} 파일을 찾을 수 없습니다."
            )
            return

        logger.info("[UPDATE] 봇 업데이트 확인 시작")
        status_msg = await message.reply_text("🚀 봇 업데이트 확인 중...")

        try:
            async def _on_update_progress(display_text: str) -> None:
                try:
                    await status_msg.edit_text(
                        f"🔄 업데이트 진행 중...\n\n```\n{display_text}\n```",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass  # 진행 상태 편집 실패는 업데이트를 중단하지 않음

            _version_lines_sent: set[str] = set()

            async def _on_update_line(line: str) -> None:
                if "[Updater] Current version:" in line or "[Updater] Updated to " in line:
                    if line not in _version_lines_sent:
                        _version_lines_sent.add(line)
                        try:
                            await message.reply_text(f"`{line}`", parse_mode="Markdown")
                        except Exception:
                            pass

            returncode, output = await self._run_update_binary(
                update_binary, on_progress=_on_update_progress, on_line=_on_update_line
            )
            status, user_message, version_message = self._parse_update_output(output)

            if returncode != 0:
                last_line = output.splitlines()[-1].strip() if output.strip() else "출력이 없습니다."
                logger.error("[UPDATE] 업데이트 실행 실패 (rc=%d): %s", returncode, last_line)
                await status_msg.edit_text(f"❌ 업데이트 실행 실패\n{last_line}")
                return

            if status == "up_to_date":
                logger.info("[UPDATE] 최신 버전 — 업데이트 불필요")
                await status_msg.edit_text(user_message)
                return

            running_dirs = list(
                set(path.parent.name for _, path in self.monitor.find_processes())
            )

            logger.info("[UPDATE] 업데이트 시작: 실행 중 프로세스 %d개", len(running_dirs))
            await self._announce_update_status(
                update,
                status_msg,
                version_message,
                f"🛑 프로세스 종료 중... ({len(running_dirs)}개)",
            )
            await self.process_controller.stop_all_processes()
            await self.bot._cleanup_terminal_for_dashboard(self.bot.process_name)
            await asyncio.sleep(3)

            unique_dirs = set(p.parent for p in self.monitor.all_program_paths)
            success_count = 0

            for dir_path in unique_dirs:
                dest_file = dir_path / self.bot.process_name
                try:
                    shutil.copy2(update_binary, dest_file)
                    success_count += 1
                except Exception as e:
                    logger.error(f"복사 실패 ({dir_path.name}): {e}")

            await status_msg.edit_text(
                f"🔄 프로세스 복구 중... ({len(running_dirs)}개)"
            )
            restored_count = await run_batch_operations(
                running_dirs,
                self.process_controller.start_bot_process,
                batch_size=5,
                delay=0.5,
            )

            logger.info(
                "[UPDATE] 업데이트 완료: 배포=%d 복구=%d/%d",
                success_count, restored_count, len(running_dirs),
            )
            await status_msg.edit_text(
                f"✅ 업데이트 완료\n"
                f"📂 배포: {success_count}개\n"
                f"🔄 복구: {restored_count}/{len(running_dirs)}개"
            )
            await self.dashboard_handler.safe_update_dashboard(update=update)

        except Exception as e:
            logger.error(f"업데이트 실패: {e}", exc_info=True)
            await status_msg.edit_text(f"❌ 오류: {e}")


    def _find_directory_or_suggest(self, dir_name: str) -> tuple:
        """
        디렉토리를 정확한 이름으로 찾고, 없으면 대소문자 유사 후보를 반환.

        Returns:
            (target_path, suggestion): target_path가 None이면 suggestion에 유사 이름 목록
        """
        target_path = self.process_controller.find_target_directory(dir_name)
        if target_path:
            return target_path, None

        # 대소문자 무시 매칭으로 후보 찾기
        suggestions = []
        for path in self.monitor.all_program_paths:
            actual_name = path.parent.name
            if actual_name.lower() == dir_name.lower():
                suggestions.append(actual_name)
        return None, suggestions


    async def restart_main_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """메인 봇 전체 재시작 (run_all 스크립트 호출)"""
        if not await self.check_authorization(update):
            return

        message = _require_message(update)
        # 플랫폼에 따라 적절한 스크립트 선택
        is_windows = platform.system() == "Windows"
        script_name = "run_all.bat" if is_windows else "run_all.sh"
        project_root = Path(__file__).resolve().parent.parent.parent
        script_path = project_root / script_name

        if not script_path.exists():
            await self._safe_reply_text(
                message,
                f"❌ {escape_markdown(script_name)} 를 찾을 수 없습니다\\.\n경로: {escape_markdown(str(script_path))}"
            )
            return

        await self._safe_reply_text(
            message,
            "🔄 메인 봇을 재시작합니다" r"\.\.\." "\n"
            f"• {escape_markdown(script_name)} 를 새 창에서 실행합니다" r"\." "\n"
            "• 기존 프로세스는 자동으로 정리됩니다" r"\."
        )

        try:
            if is_windows:
                subprocess.Popen(
                    f'start "Z-Pulse-Restart" cmd /c "{script_path}"',
                    shell=True,
                    cwd=str(project_root),
                )
            elif platform.system() == "Darwin":
                # macOS: 새 창에서 run_all.sh 실행
                # cleanup_terminal을 여기서 호출하지 않음 — run_all.sh 자체에
                # stop_processes.py + pkill + 기존 창 닫기 로직이 있으므로
                # 봇 자신을 kill하는 교착 상태를 방지
                root_q = str(project_root).replace("'", "'\\''")
                script_q = str(script_path).replace("'", "'\\''")
                applescript = f"""
tell application "Terminal"
    activate
    try
        set newWin to do script "cd '{root_q}' && exec bash '{script_q}'"
        set custom title of newWin to "Z-Pulse Starting..."
    on error errMsg
        log "Error restarting main bot: " & errMsg
    end try
end tell
"""
                await self.bot.platform_handler.run_shell_command(
                    applescript, is_applescript=True
                )
            else:
                # Linux 등
                subprocess.Popen(
                    ["open", "-a", "Terminal", str(script_path)],
                    cwd=str(project_root),
                )
            logger.info(f"restart_main: {script_name} 실행 요청 완료 ({script_path})")
        except Exception as e:
            logger.error(f"restart_main: {script_name} 실행 실패: {e}")
            await self._safe_reply_text(message, f"❌ 실행 실패: {escape_markdown(str(e))}")

    async def rename_directory_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """디렉토리 변경 (SettingsHandler 위임)"""
        if not await self.check_authorization(update):
            return
        await self.settings_handler.rename_directory_command(update, context)

    # ========== 텍스트 버튼 라우팅 ==========
    TEXT_BUTTON_ROUTES = {
        "대시보드": "status_command",
        "터미널 정렬": "arrange_windows_command",
        "스크린샷": "screenshot_command",
        "/update_bot": "update_bot_command",
        "봇 업데이트": "update_bot_command",
        "/economic": "economic_command",
        "/help": "help_command",
        "/restart_all": "restart_all_command",
    }

    async def handle_text_button(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """
        텍스트 버튼 입력을 라우팅 테이블 기반으로 처리합니다.

        Returns:
            True: 처리됨, False: 알 수 없는 명령어
        """
        message = _require_message(update)
        text = message.text or ""

        for extension in getattr(self.bot, "telegram_extensions", []):
            handle_text_input = getattr(extension, "handle_text_input", None)
            if callable(handle_text_input):
                handled = await cast(Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[bool]], handle_text_input)(update, context)
                if handled:
                    return True

        routes: dict[str, TextRouteValue] = dict(self.TEXT_BUTTON_ROUTES)
        for extension in getattr(self.bot, "telegram_extensions", []):
            get_routes = getattr(extension, "get_text_routes", None)
            if callable(get_routes):
                extension_routes = cast(Optional[dict[str, TextRouteValue]], get_routes())
                if extension_routes:
                    routes.update(extension_routes)

        handler_ref = routes.get(text)

        if callable(handler_ref):
            await cast(TextRouteHandler, handler_ref)(update, context)
            return True
        if isinstance(handler_ref, str):
            handler = getattr(self, handler_ref, None)
            if callable(handler):
                await cast(TextRouteHandler, handler)(update, context)
                return True
        return False
