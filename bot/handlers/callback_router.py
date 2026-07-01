"""
콜백 라우터 모듈 (Phase 6.2)

Telegram 인라인 버튼 콜백을 라우팅하는 모듈입니다.
기존의 30개 이상 if-elif 분기문을 라우팅 테이블 방식으로 개선합니다.
"""

import asyncio
from contextlib import contextmanager
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional, cast
from telegram import CallbackQuery
from telegram.ext import ContextTypes
from z_pulse.utils.telegram_gateway import get_telegram_gateway

if TYPE_CHECKING:
    from z_pulse.app import ZPulse

logger = logging.getLogger(__name__)

CallbackHandler = Callable[[CallbackQuery, ContextTypes.DEFAULT_TYPE, list[str]], Awaitable[None]]


@contextmanager
def _callback_ack_timer(action: str, warn_seconds: float = 0.100):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        if elapsed >= warn_seconds:
            logger.warning(
                "[CALLBACK][ACK] action=%s elapsed=%.3fs threshold=%.3fs",
                action,
                elapsed,
                warn_seconds,
            )


class CallbackRouter:
    """
    Telegram 콜백 쿼리 라우터

    콜백 데이터를 파싱하고 적절한 핸들러로 라우팅합니다.

    Example:
        router = CallbackRouter(bot_instance)
        await router.route(query, context)
    """

    def __init__(self, bot: 'ZPulse'):
        """
        CallbackRouter 초기화

        Args:
            bot: ZPulse 인스턴스 (핸들러 접근용)
        """
        self.bot = bot
        self._route_table = self._build_route_table()

    def _build_route_table(self) -> dict[str, CallbackHandler]:
        """
        라우팅 테이블 생성

        Returns:
            action -> handler_method 매핑 딕셔너리
        """
        route_table: dict[str, CallbackHandler] = {
            # 대시보드 관련
            "refresh_dashboard": self._handle_refresh_dashboard,
            "detail": self._handle_detail,

            # 프로세스 액션
            "kill": self._handle_kill,
            "run": self._handle_run,
            "clean_run": self._handle_clean_run,

            # 로그 관련
            "log": self._handle_log,
            "log_tail": self._handle_log_tail,
            "mainlog": self._handle_mainlog,
            "mainlog_tail": self._handle_mainlog_tail,

            # 재시작/초기화 확인
            "confirm_reset_restart": self._handle_confirm_reset_restart,
            "confirm_simple_restart": self._handle_confirm_simple_restart,
            "cancel_clean_run": self._handle_cancel_clean_run,

            # 토글 기능
            "toggle_rotation": self._handle_toggle_rotation,
            "set_slot_type": self._handle_set_slot_type,
            "force_assign": self._handle_force_assign,

            # 설정
            "edit_settings": self._handle_edit_settings,
            "change_setting": self._handle_change_setting,
            "set_value": self._handle_set_value,

            # 전체 재시작
            "restart_all_confirm": self._handle_restart_all_confirm,
            "restart_running_only": self._handle_restart_running_only,

            # 취소
            "cancel": self._handle_cancel,

            # 리네임
            "confirm_rename": self._handle_confirm_rename,
            "cancel_rename": self._handle_cancel_rename,

            # 키워드 메뉴
            "keyword_menu": self._handle_keyword_menu,
        }
        for extension in getattr(self.bot, "telegram_extensions", []):
            get_routes = getattr(extension, "get_callback_routes", None)
            if callable(get_routes):
                extension_routes = cast(Optional[dict[str, CallbackHandler]], get_routes(self))
                if extension_routes:
                    route_table.update(extension_routes)
        return route_table

    @staticmethod
    def parse_callback_data(data: str) -> tuple[str, list[str]]:
        """
        콜백 데이터를 액션과 파라미터로 파싱

        Args:
            data: 콜백 데이터 문자열 (예: "action:param1:param2")

        Returns:
            (action, params) 튜플
        """
        parts = data.split(':')
        action = parts[0]
        params = parts[1:] if len(parts) > 1 else []
        return action, params

    async def route(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        콜백 쿼리를 적절한 핸들러로 라우팅

        Args:
            query: Telegram CallbackQuery 객체
            context: Telegram 컨텍스트
        """
        user_id = query.from_user.id

        # 인증 확인
        if not self.bot.is_authorized(user_id):
            await get_telegram_gateway().answer_callback_query(query, "❌ 인증되지 않은 사용자입니다.")
            return

        try:
            action, params = self.parse_callback_data(query.data or "")
            skip_pre_ack_actions = {
                "refresh_dashboard",
                "kill",
            }
            if action not in skip_pre_ack_actions:
                try:
                    with _callback_ack_timer(action):
                        await get_telegram_gateway().answer_callback_query(query)
                except Exception:
                    # 네트워크 에러 등으로 answer 실패해도 나머지 로직은 계속 진행
                    logger.debug("query.answer() 실패 (네트워크 일시 장애 가능)")

            # noop 처리 (무시 버튼) - 메시지 삭제로 명확한 피드백 제공
            if action == "noop":
                try:
                    delete_message = cast(Optional[Callable[[], Awaitable[Any]]], getattr(query.message, "delete", None))
                    if delete_message is not None:
                        await delete_message()
                    else:
                        raise AttributeError("message.delete unavailable")
                except Exception:
                    # 삭제 실패 시 (오래된 메시지 등) 버튼만 제거
                    try:
                        await query.edit_message_reply_markup(reply_markup=None)
                    except Exception:
                        pass
                return

            # kw_ 접두사는 키워드 핸들러로 위임
            if action.startswith("kw_"):
                await self.bot.keyword_handler.process_callback(query, context)
                return

            # 라우팅 테이블에서 핸들러 찾기
            handler = self._route_table.get(action)
            if handler:
                await handler(query, context, params)
            else:
                logger.warning(f"알 수 없는 콜백 액션: {action}")
                await query.edit_message_text(f"❌ 알 수 없는 작업입니다: {action}")

        except Exception as e:
            logger.error(f"버튼 핸들러 처리 중 오류: {e}")
            try:
                await query.edit_message_text(f"❌ 처리 중 오류가 발생했습니다:\n{str(e)}")
            except Exception:
                pass  # 네트워크 장애 시 에러 메시지 전송도 실패할 수 있음

    # ===== 대시보드 관련 핸들러 =====

    async def _handle_refresh_dashboard(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """대시보드 새로고침 (디렉토리 구조 재스캔 포함)"""
        logger.info(
            "[REFRESH_UI][CLICK] query_id=%s message_id=%s",
            getattr(query, "id", None),
            getattr(getattr(query, "message", None), "message_id", None),
        )
        try:
            with _callback_ack_timer("refresh_dashboard"):
                await get_telegram_gateway().answer_callback_query(
                    query,
                    "🔄 대시보드를 새로고침합니다...",
                )
        except Exception as exc:
            logger.warning(
                "[REFRESH_UI][ACK_FAIL] query_id=%s error=%s",
                getattr(query, "id", None),
                exc,
            )
        self.bot.dashboard_handler.trigger_refresh(query, force_rescan=True)

    async def _handle_detail(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """프로세스 상세 정보"""
        await self.bot.dashboard_handler.show_process_detail(query, params[0])

    # ===== 프로세스 액션 핸들러 =====

    async def _handle_kill(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """프로세스 종료"""
        target = params[0]
        message_id = getattr(getattr(query, "message", None), "message_id", None)
        logger.info(
            "[KILL_UI][CLICK] target=%s query_id=%s message_id=%s",
            target,
            getattr(query, "id", None),
            message_id,
        )
        try:
            with _callback_ack_timer("kill"):
                await get_telegram_gateway().answer_callback_query(
                    query,
                    f"🛑 {target} 종료 처리 중...",
                )
        except Exception as exc:
            logger.warning(
                "[KILL_UI][ACK_FAIL] target=%s query_id=%s error=%s",
                target,
                getattr(query, "id", None),
                exc,
            )
        asyncio.create_task(self.bot.process_action_handler.kill_process(query, target))

    async def _handle_run(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """프로세스 시작"""
        asyncio.create_task(self.bot.process_action_handler.start_process(query, params[0]))

    async def _handle_clean_run(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """클린 런"""
        asyncio.create_task(self.bot.process_action_handler.clean_run(query, params[0]))

    # ===== 로그 관련 핸들러 =====

    async def _handle_log(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """전체 로그 전송"""
        await self.bot.file_operations.send_program_log(query, params[0])

    async def _handle_log_tail(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """로그 tail 전송"""
        await self.bot.file_operations.send_program_log(query, params[0], tail=100)

    async def _handle_mainlog(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """메인봇 전체 로그 전송"""
        await self.bot.file_operations.send_main_bot_log(query)

    async def _handle_mainlog_tail(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """메인봇 로그 tail 전송"""
        await self.bot.file_operations.send_main_bot_log(query, tail=100)

    # ===== 재시작/초기화 확인 핸들러 =====

    async def _handle_confirm_reset_restart(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """DB 초기화 후 재시작 확인"""
        await get_telegram_gateway().edit_message_text(
            query,
            text=f"🧹 '{params[0]}'의 DB를 초기화하고 재시작합니다...",
            timeout=5.0,
            drop_ok=True,
        )
        asyncio.create_task(self.bot.process_action_handler.clean_run(query, params[0]))

    async def _handle_confirm_simple_restart(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """단순 재시작 확인"""
        target = params[0]
        asyncio.create_task(
            self.bot.process_action_handler.simple_restart(
                query,
                target,
                callback_pre_acked=True,
            )
        )

    async def _handle_cancel_clean_run(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """클린 런 취소 (설정 저장 후 메뉴 복귀)"""
        await query.edit_message_text("✅ 설정이 저장되었습니다. 이전 메뉴로 돌아갑니다...")
        await asyncio.sleep(1)
        await self.bot.settings_handler.show_editor(query, params[0])

    # ===== 토글 기능 핸들러 =====

    async def _handle_toggle_rotation(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """페어 로테이션 토글"""
        await self.bot.process_action_handler.toggle_rotation(query, params[0])

    async def _handle_set_slot_type(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """슬롯 타입 선택 후 로테이션 활성화"""
        await self.bot.process_action_handler.set_slot_type(query, params[0], params[1])

    async def _handle_force_assign(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """비정상 종료 봇 강제할당"""
        await self.bot.process_action_handler.force_assign(query, params[0])

    # ===== 설정 핸들러 =====

    async def _handle_edit_settings(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """설정 편집기 열기"""
        await self.bot.settings_handler.show_editor(query, params[0])

    async def _handle_change_setting(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """설정 값 변경 요청"""
        target, key = params[0], params[1]
        await self.bot.settings_handler.request_new_value(query, context, target, key)

    async def _handle_set_value(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """설정 값 저장 (버튼 선택으로부터)"""
        target, key, value = params[0], params[1], params[2]
        await self.bot.settings_handler.set_value_from_button(query, target, key, value)

    # ===== 전체 재시작 핸들러 =====

    async def _handle_restart_all_confirm(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """전체 재시작 확인"""
        asyncio.create_task(self.bot.process_action_handler.restart_all(query))

    async def _handle_restart_running_only(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """실행 중인 봇만 재시작"""
        asyncio.create_task(self.bot.process_action_handler.restart_running_only(query))

    # ===== 취소 핸들러 =====

    async def _handle_cancel(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """작업 취소"""
        await query.edit_message_text("❌ 작업이 취소되었습니다.")

    # ===== 리네임 핸들러 =====

    async def _handle_confirm_rename(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """디렉토리 리네임 확인"""
        old_dir, new_dir = params[0], params[1]
        was_running = bool(int(params[2])) if len(params) > 2 else True
        asyncio.create_task(self.bot.settings_handler.process_rename(query, old_dir, new_dir, was_running))

    async def _handle_cancel_rename(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """디렉토리 리네임 취소"""
        await query.edit_message_text("❌ 디렉토리 변경이 취소되었습니다.")

    # ===== 키워드 메뉴 핸들러 =====

    async def _handle_keyword_menu(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, params: list[str]) -> None:
        """개별 봇 키워드 메뉴"""
        await self.bot.keyword_handler.show_menu(query, context, target=params[0])


