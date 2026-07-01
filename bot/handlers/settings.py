"""
Settings Handler Module

설정 파일(setting.env) 편집, 디렉토리 이름 변경 등
설정 및 메타데이터 관리 기능을 담당합니다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

# [수정] load_json, save_json은 utils 패키지에서 가져오도록 수정
from z_pulse.config import EnvConfigHandler, get_trading_info_from_env, load_ignored_dirs, save_ignored_dirs
from z_pulse.config.setting_definitions import SETTING_DEFINITIONS, ReloadMode, get_editable_settings
from z_pulse.utils import escape_markdown, load_json, save_json
from z_pulse.config.validators import SettingValidator

logger = logging.getLogger(__name__)

class SettingsHandler:
    # 편집 가능한 설정 항목 정의 (setting_definitions.py에서 가져옴)
    EDITABLE_SETTINGS = {
        **get_editable_settings(),
    }
    def _pair_trading_keys(self) -> dict[str, frozenset[str]]:
        """Fetch pair-trading key taxonomy from ZFlowBridge.
        Falls back to empty sets if bridge is unavailable."""
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is None:
            return {"strategy": frozenset(), "applied": frozenset(), "rotation_protected": frozenset()}
        return bridge.get_pair_trading_setting_keys()

    def _is_external_pair_trading_bot(self, target: str) -> bool:
        target_path = self.process_controller.find_target_directory(target)
        if not target_path:
            return False
        try:
            from z_pulse.monitoring.bot_state import is_pair_trading_type

            trading_type, _ = get_trading_info_from_env(target_path.parent)
            return is_pair_trading_type(trading_type)
        except Exception:
            return False

    def _is_pair_trading_rotation_on(self, target: str) -> bool:
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is None:
            return False
        if not bridge.is_pair_trading_ui_enabled():
            return False
        return bridge.is_rotation_enabled(target)

    def _should_use_pair_trading_split_view(self, target: str) -> bool:
        return self._is_external_pair_trading_bot(target) and self._is_pair_trading_rotation_on(target)

    def _build_settings_sections(self, target: str, settings: dict[str, str]) -> list[tuple[str | None, list[tuple[str, str, str]]]]:
        if self._is_external_pair_trading_bot(target):
            strategy_keys = self._pair_trading_keys()["strategy"]
            allowed_keys = set(self.EDITABLE_SETTINGS) - strategy_keys
        else:
            allowed_keys = set(self.EDITABLE_SETTINGS)
        local_items = [
            (key, self.EDITABLE_SETTINGS[key], settings[key])
            for key in self.EDITABLE_SETTINGS
            if key in allowed_keys and key in settings
        ]
        return [(None, local_items)] if local_items else []

    def _render_settings_editor(self, target: str, settings: dict[str, str]) -> tuple[str, list[tuple[str, str]]]:
        esc_target = escape_markdown(target)
        message_text = (
            f"⚙️ *{esc_target} 설정 변경*\n\n"
            "`HOT`\\=즉시 반영, `HOT_REINIT`\\=부분 재초기화, `COLD`\\=재시작 필요\n\n"
        )
        valid_keys: list[tuple[str, str]] = []

        for section_title, items in self._build_settings_sections(target, settings):
            if not items:
                continue
            if section_title:
                message_text += f"*{escape_markdown(section_title)}*\n"
            for key, button_name, raw_value in items:
                value = escape_markdown(raw_value)
                esc_button_name = escape_markdown(button_name)
                definition = SETTING_DEFINITIONS.get(key)
                reload_badge = definition.get_reload_badge() if definition else "COLD"
                esc_reload_badge = escape_markdown(reload_badge)
                message_text += f"[{esc_reload_badge}] *{esc_button_name}* : `{value}`\n"
                valid_keys.append((key, button_name))
            message_text += "\n"

        return message_text.rstrip(), valid_keys

    def _build_settings_keyboard(self, target: str, valid_keys: list[tuple[str, str]]) -> InlineKeyboardMarkup:
        keyboard = []
        row = []
        for i, (key, name) in enumerate(valid_keys):
            row.append(InlineKeyboardButton(name, callback_data=f"change_setting:{target}:{key}"))
            if (i + 1) % 2 == 0:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("🔙 돌아가기", callback_data=f"detail:{target}")])
        return InlineKeyboardMarkup(keyboard)

    def _settings_edit_block_message(self, target: str, key: str) -> str | None:
        if self._is_pair_trading_rotation_on(target) and key in self._pair_trading_keys()["rotation_protected"]:
            return "❌ 자동 배정 ON 상태에서는 해당 설정을 변경할 수 없습니다."
        return None


    def __init__(self, bot_instance, process_controller, dashboard_handler, monitor):
        self.bot = bot_instance
        self.process_controller = process_controller
        self.dashboard_handler = dashboard_handler
        self.monitor = monitor

    @staticmethod
    async def _reply_update_message(update: Update, text: str, **kwargs: Any) -> bool:
        """Safely reply to update message when available."""
        message = update.message
        if message is None:
            logger.warning("update.message is None; reply skipped")
            return False
        await message.reply_text(text, **kwargs)
        return True

    @staticmethod
    async def _reply_query_message(query: CallbackQuery, text: str, **kwargs: Any) -> bool:
        """Safely reply to callback query message when accessible."""
        message = query.message
        if isinstance(message, Message):
            await message.reply_text(text, **kwargs)
            return True
        await query.answer(text[:200], show_alert=True)
        return False

    @staticmethod
    def _reload_mode_notice(definition) -> str:
        """반영 방식별 추가 안내 문구"""
        if definition and definition.reload_mode == ReloadMode.COLD:
            return "\n⚠️ 재시작 후 적용됩니다\\."
        if definition and definition.reload_mode == ReloadMode.HOT_WITH_REINIT:
            return "\nℹ️ 다음 사이클에서 부분 재초기화 후 반영됩니다\\."
        return ""

    # ── 설정 읽기/쓰기 ──

    def _read_settings(self, target_dir: str) -> dict[str, str]:
        """setting.env에서 편집 가능한 모든 설정을 읽어옴"""
        target_path = self.process_controller.find_target_directory(target_dir)
        if not target_path:
            return {}
        slot_config = EnvConfigHandler.parse(target_path.parent)
        settings: dict[str, str] = {}
        for key in self.EDITABLE_SETTINGS:
            if key in slot_config:
                settings[key] = slot_config[key]
        return settings

    @staticmethod
    def _normalize_slot_coin(coin_value: str) -> str | None:
        """
        Z-Flow 코인 값을 정규화합니다.
        - 'BTC' -> 'BTC_USDT_Perp'
        - 'btc' -> 'BTC_USDT_Perp'
        - 'BTC_USDT_Perp' -> 'BTC_USDT_Perp' (unchanged)
        - '' or 'UNASSIGNED' -> None
        """
        if not coin_value or coin_value.strip().upper() == "UNASSIGNED":
            return None
        coin = coin_value.strip()
        if coin.upper().endswith("_USDT_PERP"):
            base = coin[:-10].strip().upper()
            return f"{base}_USDT_Perp"
        return f"{coin.upper()}_USDT_Perp"

    def _write_setting(self, target_dir: str, key_to_change: str, new_value: str) -> bool:
        """setting.env의 특정 키 값을 변경한다."""
        target_path = self.process_controller.find_target_directory(target_dir)
        if not target_path:
            return False
        if key_to_change in self._pair_trading_keys()["strategy"]:
            bridge = getattr(self.bot, "z_flow_bridge", None)
            if bridge is None:
                return False
            target_dir_path = bridge.get_pair_trading_env_path().parent
        else:
            target_dir_path = target_path.parent
        return EnvConfigHandler.update_key(target_dir_path, key_to_change, new_value)

    async def show_editor(self, query: CallbackQuery, target: str):
        """설정 편집 화면을 표시"""
        try:
            settings = self._read_settings(target)
            
            if not settings:
                await query.edit_message_text("❌ 설정 파일을 읽을 수 없거나 변경 가능한 항목이 없습니다.")
                return

            message_text, valid_keys = self._render_settings_editor(target, settings)

            if not valid_keys:
                await query.edit_message_text("⚠️ 변경 가능한 설정값이 파일에 존재하지 않습니다.")
                return

            await query.edit_message_text(
                message_text,
                reply_markup=self._build_settings_keyboard(target, valid_keys),
                parse_mode='MarkdownV2'
            )

        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass  # 이미 동일한 내용이 표시 중이므로 무시
            else:
                logger.error(f"설정 편집기 표시 오류: {e}")
                try:
                    await query.edit_message_text(f"❌ 설정 편집기 오류: {e}")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"설정 편집기 표시 오류: {e}")
            try:
                await query.edit_message_text(f"❌ 설정 편집기 오류: {e}")
            except Exception:
                pass

    async def request_new_value(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, target: str, key: str):
        """새로운 설정 값을 사용자에게 요청"""
        block_message = self._settings_edit_block_message(target, key)
        if block_message:
            await self._reply_query_message(query, block_message)
            return

        # 설정 정의 가져오기
        definition = SETTING_DEFINITIONS.get(key)
        button_name = self.EDITABLE_SETTINGS.get(key, key)
        esc_button_name = escape_markdown(button_name)
        esc_key = escape_markdown(key)

        # BOOLEAN 또는 DIRECTION 타입인 경우: 버튼 선택 방식
        if definition and definition.setting_type.value in ['boolean', 'direction'] and definition.allowed_values:
            keyboard = []
            for value in definition.allowed_values:
                keyboard.append([InlineKeyboardButton(
                    value,
                    callback_data=f"set_value:{target}:{key}:{value}"
                )])
            keyboard.append([InlineKeyboardButton("🔙 취소", callback_data=f"edit_settings:{target}")])

            await query.edit_message_text(
                f"🔹 *{esc_button_name}* \\(`{esc_key}`\\) 선택:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='MarkdownV2'
            )
            return

        # 나머지 타입: 기존 텍스트 입력 방식
        user_data = context.user_data
        if user_data is None:
            logger.warning("context.user_data is None; setting change request skipped")
            return

        message_id = query.message.message_id if isinstance(query.message, Message) else None
        user_data['pending_setting_change'] = {'target': target, 'key': key, 'message_id': message_id}

        # 타입 힌트 생성
        if definition:
            type_hint = definition.get_type_hint()
            esc_type_hint = escape_markdown(type_hint)
            reload_badge = escape_markdown(definition.get_reload_badge())

            # 추가 힌트 생성
            extra_hints = [f"반영: {reload_badge}"]
            if definition.min_value is not None or definition.max_value is not None:
                if definition.min_value is not None and definition.max_value is not None:
                    esc_min = escape_markdown(str(definition.min_value))
                    esc_max = escape_markdown(str(definition.max_value))
                    extra_hints.append(f"범위: {esc_min}\\~{esc_max}")
                elif definition.min_value is not None:
                    esc_min = escape_markdown(str(definition.min_value))
                    extra_hints.append(f"최소값: {esc_min}")
                elif definition.max_value is not None:
                    esc_max = escape_markdown(str(definition.max_value))
                    extra_hints.append(f"최대값: {esc_max}")

            if definition.allowed_values:
                allowed = "/".join(definition.allowed_values)
                extra_hints.append(f"허용값: {allowed}")

            # MarkdownV2에서 줄바꿈: \n\n 사용
            hint_text = f"\n\n타입: {esc_type_hint}"
            if extra_hints:
                hint_text += "\n" + "\n".join(extra_hints)
        else:
            hint_text = "\n\n\\(예: 리스트는 `[1,2,3]` 형식으로 입력\\)"

        await self._reply_query_message(
            query,
            f"🔹 *{esc_button_name}* \\(`{esc_key}`\\)의 새로운 값을 입력하세요\\."
            f"{hint_text}",
            parse_mode='MarkdownV2'
        )

    async def set_value_from_button(self, query: CallbackQuery, target: str, key: str, value: str):
        """
        버튼 선택으로 설정 값 저장 (BOOLEAN, DIRECTION 타입용)

        Args:
            query: CallbackQuery 객체
            target: 대상 디렉토리
            key: 설정 키
            value: 선택된 값
        """
        # 값 저장
        if self._write_setting(target, key, value):
            button_name = self.EDITABLE_SETTINGS.get(key, key)
            esc_setting_name = escape_markdown(button_name)
            esc_value = escape_markdown(value)
            definition = SETTING_DEFINITIONS.get(key)
            reload_badge = escape_markdown(definition.get_reload_badge() if definition else "COLD")
            reload_notice = self._reload_mode_notice(definition)

            await query.edit_message_text(
                f"✅ *{esc_setting_name}* 설정이 `{esc_value}` \\(으\\)로 저장되었습니다\\.\n"
                f"반영 방식: `{reload_badge}`"
                f"{reload_notice}",
                parse_mode='MarkdownV2'
            )

            # 잠시 대기 후 설정 편집 화면으로 복귀
            await asyncio.sleep(0.5)

            # 설정 편집 화면 다시 표시
            try:
                settings = self._read_settings(target)

                if not settings:
                    logger.warning(f"설정 파일을 다시 읽을 수 없습니다: {target}")
                    return

                message_text, valid_keys = self._render_settings_editor(target, settings)

                await self._reply_query_message(
                    query,
                    message_text,
                    reply_markup=self._build_settings_keyboard(target, valid_keys),
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.error(f"설정 편집 화면 복귀 중 오류: {e}")
        else:
            await query.edit_message_text("❌ 설정 저장에 실패했습니다.")

    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """
        사용자 텍스트 입력을 처리 (설정 변경 값인 경우)
        Returns:
            bool: 처리되었으면 True, 아니면 False
        """
        user_data = context.user_data
        if user_data is None or 'pending_setting_change' not in user_data:
            return False

        message = update.message
        if message is None:
            logger.warning("update.message is None; text input handling skipped")
            return False

        pending_data = user_data['pending_setting_change']  # pop 하지 않고 유지 (validation 실패 시 재입력 위해)
        target = pending_data['target']
        key = pending_data['key']
        new_value = message.text
        if new_value is None:
            logger.warning("update.message.text is None; text input handling skipped")
            return False

        # === Validation 추가 ===
        definition = SETTING_DEFINITIONS.get(key)
        if definition:
            is_valid, error_message, validated_value = SettingValidator.validate(definition, new_value)

            if not is_valid:
                # Validation 실패 - 에러 메시지 표시하고 재입력 요청
                esc_error = escape_markdown(error_message or "유효하지 않은 입력입니다.")
                await self._reply_update_message(
                    update,
                    f"❌ *입력 값 오류*\n\n"
                    f"{esc_error}\n\n"
                    f"다시 입력해주세요\\.",
                    parse_mode='MarkdownV2'
                )
                logger.warning(f"설정 값 검증 실패: {key}={new_value}, 오류: {error_message}")
                return True  # 처리는 완료했지만 재입력 대기 (pending_data 유지)

            # Validation 성공 - 검증된 값 사용
            if validated_value is None:
                logger.warning(f"검증 성공이지만 값이 None입니다: {key}")
                return True
            new_value = validated_value
            logger.info(f"설정 값 검증 성공: {key}={new_value}")

        # pending_data를 이제 제거
        user_data.pop('pending_setting_change')

        if self._write_setting(target, key, new_value):
            setting_name = self.EDITABLE_SETTINGS.get(key) or key
            esc_setting_name = escape_markdown(setting_name)
            esc_new_value = escape_markdown(new_value)
            definition = SETTING_DEFINITIONS.get(key)
            reload_badge = escape_markdown(definition.get_reload_badge() if definition else "COLD")
            reload_notice = self._reload_mode_notice(definition)

            # 성공 메시지 표시
            await self._reply_update_message(
                update,
                f"✅ *{esc_setting_name}* 설정이 `{esc_new_value}` \\(으\\)로 저장되었습니다\\.\n"
                f"반영 방식: `{reload_badge}`"
                f"{reload_notice}",
                parse_mode='MarkdownV2'
            )

            # 이전 설정 편집 메시지 삭제 시도
            try:
                message_id = pending_data.get('message_id')
                if isinstance(message_id, int) and update.effective_chat is not None:
                    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=message_id)
            except Exception as e:
                logger.warning(f"메시지 삭제 실패: {e}")

            # 잠시 대기 후 설정 편집 화면으로 복귀
            await asyncio.sleep(0.5)

            # CallbackQuery 객체를 생성하여 show_editor 호출
            # (update.message를 사용하여 새 메시지로 설정 화면 표시)
            try:
                # 설정 편집 화면 다시 표시
                settings = self._read_settings(target)

                if not settings:
                    logger.warning(f"설정 파일을 다시 읽을 수 없습니다: {target}")
                    return True

                message_text, valid_keys = self._render_settings_editor(target, settings)

                await self._reply_update_message(
                    update,
                    message_text,
                    reply_markup=self._build_settings_keyboard(target, valid_keys),
                    parse_mode='MarkdownV2'
                )
            except Exception as e:
                logger.error(f"설정 편집 화면 복귀 중 오류: {e}")
        else:
            await self._reply_update_message(update, "❌ 설정 저장에 실패했습니다.")

        return True

    async def rename_directory_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """디렉토리명 변경 명령어"""
        args = context.args or []
        if len(args) < 2:
            await self._reply_update_message(
                update,
                "사용법: /rename <old_dir> <new_dir>\n"
                "예: /rename 24-G-PAIR2 25-G-PAIR2"
            )
            return

        old_dir = args[0]
        new_dir = args[1]

        target_path = self.process_controller.find_target_directory(old_dir)
        if not target_path:
            await self._reply_update_message(update, f"❌ 디렉토리를 찾을 수 없습니다: {old_dir}")
            return

        new_path = target_path.parent.parent / new_dir
        if new_path.exists():
            await self._reply_update_message(update, f"❌ 새 디렉토리가 이미 존재합니다: {new_dir}")
            return

        is_running, _ = self.process_controller.is_process_running(old_dir)

        keyboard = [
            [InlineKeyboardButton("✅ 예", callback_data=f"confirm_rename:{old_dir}:{new_dir}:{1 if is_running else 0}")],
            [InlineKeyboardButton("❌ 아니오", callback_data="cancel_rename")]
        ]

        if is_running:
            message = (
                f"⚠️ 디렉토리 변경을 위해 봇 종료가 필요합니다.\n\n"
                f"변경: {old_dir} → {new_dir}\n\n"
                f"📋 수행 작업:\n"
                f"  1. 봇 프로세스 종료\n"
                f"  2. 디렉토리명 변경\n"
                f"  3. 키워드 알림 설정 업데이트\n"
                f"  4. 무시 목록 업데이트\n"
                f"  5. 봇 재시작\n\n"
                f"계속 하시겠습니까?"
            )
        else:
            message = (
                f"📁 디렉토리명을 변경합니다.\n\n"
                f"변경: {old_dir} → {new_dir}\n\n"
                f"📋 수행 작업:\n"
                f"  1. 디렉토리명 변경\n"
                f"  2. 키워드 알림 설정 업데이트\n"
                f"  3. 무시 목록 업데이트\n\n"
                f"계속 하시겠습니까?"
            )

        await self._reply_update_message(update, message, reply_markup=InlineKeyboardMarkup(keyboard))

    async def process_rename(self, query: CallbackQuery, old_dir: str, new_dir: str, was_running: bool):
        """디렉토리 변경 실제 처리"""
        try:
            await query.edit_message_text(f"🔄 디렉토리 변경 작업을 시작합니다...\n{old_dir} → {new_dir}")

            if was_running:
                total_steps = 5
                step = 1
                await self._reply_query_message(query, f"{step}/{total_steps} 봇 프로세스 종료 중...")
                self.process_controller.kill_specific_process(old_dir)
                await asyncio.sleep(2)
                step += 1
            else:
                total_steps = 3
                step = 1

            await self._reply_query_message(query, f"{step}/{total_steps} 디렉토리명 변경 중...")
            target_path = self.process_controller.find_target_directory(old_dir)
            if not target_path:
                await self._reply_query_message(query, f"❌ 디렉토리를 찾을 수 없습니다: {old_dir}")
                return

            old_path = target_path.parent
            new_path = old_path.parent / new_dir

            try:
                old_path.rename(new_path)
            except Exception as e:
                await self._reply_query_message(query, f"❌ 디렉토리명 변경 실패: {e}")
                return
            step += 1

            await self._reply_query_message(query, f"{step}/{total_steps} 키워드 알림 설정 업데이트 중...")
            await self._update_log_keywords(old_dir, new_dir)
            step += 1

            await self._reply_query_message(query, f"{step}/{total_steps} 무시 목록 업데이트 중...")
            await self._update_ignored_dirs(old_dir, new_dir)

            # 페어 로테이션 설정 업데이트
            await self._update_rotation_config(old_dir, new_dir)

            # 모니터 재스캔
            self.monitor.find_target_programs()

            # 로그 감시 대상 재초기화
            if hasattr(self.bot, 'log_keyword_monitor') and self.bot.log_keyword_monitor:
                self.bot.log_keyword_monitor._initialize_file_positions()
                self.bot.log_keyword_monitor._load_keywords()
                logger.info(f"로그 감시 대상 및 키워드가 갱신되었습니다: {old_dir} → {new_dir}")

            if was_running:
                step += 1
                await self._reply_query_message(query, f"{step}/{total_steps} 봇 재시작 중...")
                await asyncio.sleep(1)

                # ActionHandler 없이 직접 controller 호출 (순환 참조 방지 및 단순화)
                success = await self.process_controller.start_bot_process(new_dir)

                if success:
                    await self._reply_query_message(
                        query,
                        f"✅ 디렉토리 변경 완료!\n\n"
                        f"{old_dir} → {new_dir}\n\n"
                        f"봇이 새 터미널 창에서 실행되었습니다."
                    )
                else:
                    await self._reply_query_message(
                        query,
                        f"⚠️ 디렉토리 변경은 완료되었으나 봇 시작에 실패했습니다.\n"
                        f"수동으로 /restart {new_dir} 명령을 실행해주세요."
                    )
            else:
                await self._reply_query_message(
                    query,
                    f"✅ 디렉토리 변경 완료!\n\n"
                    f"{old_dir} → {new_dir}\n\n"
                )

        except Exception as e:
            logger.error(f"디렉토리 변경 중 오류: {e}")
            await self._reply_query_message(query, f"❌ 디렉토리 변경 중 오류 발생: {e}")

    async def _update_log_keywords(self, old_dir: str, new_dir: str):
        """log_keywords.json 파일의 봇 이름 업데이트"""
        keywords_file = Path("log_keywords.json")
        data = load_json(keywords_file, default={})

        if "bots" in data and old_dir in data["bots"]:
            data["bots"][new_dir] = data["bots"].pop(old_dir)
            if save_json(keywords_file, data):
                logger.info(f"log_keywords.json 업데이트: {old_dir} → {new_dir}")

    async def _update_ignored_dirs(self, old_dir: str, new_dir: str):
        """ignored_dir 파일의 디렉토리명 업데이트"""
        try:
            dirs = load_ignored_dirs()
            if old_dir in dirs:
                dirs.remove(old_dir)
                dirs.add(new_dir)
                save_ignored_dirs(dirs)
                self.monitor.ignore_list = load_ignored_dirs()
                logger.info(f"ignored_dir 업데이트: {old_dir} → {new_dir}")
        except Exception as e:
            logger.error(f"ignored_dir 업데이트 중 오류: {e}")

    async def _update_rotation_config(self, old_dir: str, new_dir: str):
        """페어 로테이션 설정의 봇 이름 업데이트"""
        try:
            bridge = getattr(self.bot, "z_flow_bridge", None)
            if bridge:
                bridge.rename_rotation_bot(old_dir, new_dir)
                logger.info(f"페어 로테이션 설정 업데이트: {old_dir} → {new_dir}")
        except Exception as e:
            logger.error(f"페어 로테이션 설정 업데이트 중 오류: {e}")
