"""
Keyword Handler Module (Fixed Argument Passing)
"""

import logging
from typing import Any
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery, Message
from telegram.ext import ContextTypes

from z_pulse.utils.markdown_utils import escape_markdown

logger = logging.getLogger(__name__)


def _user_data(context: ContextTypes.DEFAULT_TYPE) -> dict[Any, Any]:
    data = context.user_data
    if data is None:
        raise ValueError("context.user_data is required")
    return data


def _message_from_update(update: Update) -> Message:
    message = update.message
    if message is None:
        raise ValueError("update.message is required")
    return message


class KeywordHandler:
    def __init__(self, bot_instance, log_keyword_monitor, dashboard_handler):
        self.bot = bot_instance
        self.log_keyword_monitor = log_keyword_monitor
        self.dashboard_handler = dashboard_handler

    async def show_menu(self, update_or_query, context: ContextTypes.DEFAULT_TYPE, target: str):
        """키워드 관리 메뉴"""
        try:
            user_data = _user_data(context)
            user_data['target_bot'] = target
            user_data['keyword_action_state'] = None
            user_data.pop('editing_index', None)  # [Fix] 이전 편집 상태 초기화
            user_data.pop('pending_keyword', None)

            keywords = self.log_keyword_monitor.get_keywords(target)

            title = f"🔔 *{escape_markdown(target)} 키워드 관리*"
            message_text = f"{title}\n\n"

            if not keywords:
                message_text += escape_markdown("등록된 키워드가 없습니다.") + "\n"
            else:
                for idx, kw in enumerate(keywords):
                    phrase = escape_markdown(kw.get('phrase', ''))
                    is_json = "JSON" if kw.get('is_json_block') else "TEXT"
                    cooldown = kw.get('cooldown_seconds', 0)
                    message_text += f"{idx+1}\\. `{phrase}` \\({is_json}, {cooldown}s\\)\n"

            message_text += "\n" + escape_markdown("원하는 작업을 선택하세요.")

            keyboard = [
                [
                    InlineKeyboardButton("➕ 추가", callback_data="kw_add_start"),
                    InlineKeyboardButton("✏️ 변경", callback_data="kw_edit_start"),
                    InlineKeyboardButton("🗑️ 삭제", callback_data="kw_delete_start"),
                ]
            ]
            keyboard.append([InlineKeyboardButton(f"🔙 {target} 상세", callback_data=f"detail:{target}")])

            reply_markup = InlineKeyboardMarkup(keyboard)

            if isinstance(update_or_query, CallbackQuery):
                await update_or_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
            else:
                await update_or_query.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='MarkdownV2')
        except Exception as e:
            logger.error(f"키워드 메뉴 오류: {e}")
            try: await update_or_query.answer("❌ 메뉴 로딩 실패")
            except: pass

    async def process_callback(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE):
        try:
            action = query.data
            if action is None:
                return
            if action == "kw_add_start": await self._handle_kw_add_start(query, context)
            elif action == "kw_edit_start": await self._handle_kw_edit_start(query, context)
            elif action == "kw_delete_start": await self._handle_kw_delete_start(query, context)
            elif action.startswith("kw_sel_"):
                idx = int(action.split("_")[2])
                await self._handle_kw_item_selection(query, context, idx)
            elif action.startswith("kw_json_"):
                is_json = action.split("_")[2] == "yes"
                await self._handle_kw_json_selection(query, context, is_json)
            elif action == "kw_confirm": await self._handle_kw_confirmation(query, context)
            elif action == "kw_back": await self._handle_kw_back_menu(query, context)
        except Exception as e:
            logger.error(f"콜백 처리 오류 ({query.data}): {e}")
            try: await query.answer("❌ 오류 발생")
            except: pass

    async def _handle_kw_add_start(self, query, context):
        user_data = _user_data(context)
        user_data['keyword_action_state'] = 'ADD_PHRASE'
        user_data['pending_keyword'] = {}
        user_data.pop('editing_index', None)  # [Fix] 추가 모드에서는 editing_index 제거
        await query.edit_message_text(
            "➕ *새 키워드 추가*\n\n" + escape_markdown("감지할 텍스트(Phrase)를 입력해주세요:"),
            parse_mode='MarkdownV2',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 취소", callback_data="kw_back")]])
        )

    async def _handle_kw_edit_start(self, query, context):
        await self._show_keyword_selection(query, context, mode='EDIT')

    async def _handle_kw_delete_start(self, query, context):
        await self._show_keyword_selection(query, context, mode='DELETE')

    async def _show_keyword_selection(self, query, context, mode):
        user_data = _user_data(context)
        target = user_data.get('target_bot')
        if not target:
            return
        keywords = self.log_keyword_monitor.get_keywords(target)
        
        if not keywords:
            await query.answer("선택할 키워드가 없습니다.")
            return

        user_data['keyword_action_state'] = f'{mode}_SELECT'
        keyboard = []
        for idx, kw in enumerate(keywords):
            btn_text = f"{kw.get('phrase')} ({'JSON' if kw.get('is_json_block') else 'TEXT'})"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"kw_sel_{idx}")])
        keyboard.append([InlineKeyboardButton("🔙 취소", callback_data="kw_back")])
        
        action_text = "수정" if mode == 'EDIT' else "삭제"
        await query.edit_message_text(
            f"❓ *{escape_markdown(action_text)}할 키워드를 선택하세요:*",
            parse_mode='MarkdownV2',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def _handle_kw_item_selection(self, query, context, idx):
        user_data = _user_data(context)
        state = user_data.get('keyword_action_state')
        target = user_data.get('target_bot')
        if not target:
            return
        keywords = self.log_keyword_monitor.get_keywords(target)
        
        if idx < 0 or idx >= len(keywords):
            await query.answer("잘못된 선택입니다.")
            return

        selected_kw = keywords[idx]
        
        if state == 'DELETE_SELECT':
            # [수정] 인자 전달 확인 (index, bot_name)
            if self.log_keyword_monitor.delete_keyword(idx, bot_name=target):
                await query.answer("🗑️ 삭제 완료")
            else:
                await query.answer("❌ 삭제 실패")
            await self.show_menu(query, context, target)
            
        elif state == 'EDIT_SELECT':
            user_data['pending_keyword'] = selected_kw.copy()
            user_data['editing_index'] = idx
            user_data['keyword_action_state'] = 'EDIT_PHRASE'
            phrase = escape_markdown(selected_kw.get('phrase'))
            await query.edit_message_text(
                f"✏️ *키워드 수정*\n\n현재 값: `{phrase}`\n" + escape_markdown("새 텍스트를 입력하세요 (변경 없으면 동일 입력):"),
                parse_mode='MarkdownV2',
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 취소", callback_data="kw_back")]])
            )

    async def handle_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        user_data = _user_data(context)
        state = user_data.get('keyword_action_state')
        if not state:
            return False

        message = _message_from_update(update)
        text = (message.text or "").strip()
        pending = user_data.get('pending_keyword', {})

        if state in ['ADD_PHRASE', 'EDIT_PHRASE']:
            pending['phrase'] = text
            user_data['pending_keyword'] = pending
            user_data['keyword_action_state'] = 'WAIT_JSON'

            p1 = escape_markdown("JSON 블록 내부인가요?")
            p2 = escape_markdown("JSON 형식 로그면 Yes, 일반 텍스트면 No 선택.")
            await message.reply_text(
                f"📋 *{p1}*\n\n{p2}",
                parse_mode='MarkdownV2',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Yes (JSON)", callback_data="kw_json_yes"),
                     InlineKeyboardButton("No (Text)", callback_data="kw_json_no")]
                ])
            )
            return True

        if state == 'WAIT_COOLDOWN':
            try:
                cooldown = int(text)
                if cooldown < 0:
                    raise ValueError
                pending['cooldown_seconds'] = cooldown
                user_data['pending_keyword'] = pending
                user_data['keyword_action_state'] = 'CONFIRM'

                phrase = escape_markdown(str(pending.get('phrase', '')))
                is_json = "Yes" if pending.get('is_json_block') else "No"

                msg = (f"✅ *확인*\n"
                       f"키워드: `{phrase}`\n"
                       f"JSON: {is_json}\n"
                       f"쿨다운: {cooldown}초\n\n" + escape_markdown("저장하시겠습니까?"))

                await message.reply_text(
                    msg,
                    parse_mode='MarkdownV2',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("💾 저장", callback_data="kw_confirm"),
                         InlineKeyboardButton("🔙 취소", callback_data="kw_back")]
                    ])
                )
            except ValueError:
                await message.reply_text(
                    escape_markdown("❌ 0 이상의 숫자를 입력하세요."),
                    parse_mode='MarkdownV2'
                )
            return True
        return False

    async def _handle_kw_json_selection(self, query, context, is_json):
        user_data = _user_data(context)
        pending = user_data.get('pending_keyword', {})
        pending['is_json_block'] = is_json
        user_data['pending_keyword'] = pending
        user_data['keyword_action_state'] = 'WAIT_COOLDOWN'
        
        t1 = escape_markdown("쿨다운 설정 (초)")
        t2 = escape_markdown("알림 후 몇 초간 무시할까요? (0 = 즉시 재알림)")
        await query.edit_message_text(f"⏲️ *{t1}*\n\n{t2}", parse_mode='MarkdownV2')

    async def _handle_kw_confirmation(self, query, context):
        user_data = _user_data(context)
        pending = user_data.get('pending_keyword')
        target = user_data.get('target_bot') # 봇 이름 또는 None
        if not target:
            return

        logger.info(f"키워드 저장 시도. Target: {target}, Data: {pending}")

        if not pending:
            await self._handle_kw_back_menu(query, context)
            return

        # [핵심 수정] 딕셔너리를 풀어서 개별 인자로 전달
        phrase = pending.get('phrase')
        is_json = pending.get('is_json_block', False)
        cooldown = pending.get('cooldown_seconds', 0)

        success = False
        if 'editing_index' in user_data:
            idx = user_data['editing_index']
            # update_keyword(index, phrase, is_json, cooldown, bot_name)
            success = self.log_keyword_monitor.update_keyword(
                idx, phrase, is_json, cooldown, bot_name=target
            )
            msg = "✅ 수정 완료" if success else "❌ 수정 실패"
        else:
            # add_keyword(phrase, is_json, cooldown, bot_name)
            success = self.log_keyword_monitor.add_keyword(
                phrase, is_json, cooldown, bot_name=target
            )
            msg = "✅ 추가 완료" if success else "❌ 추가 실패"
        
        await query.answer(msg)
        await self.show_menu(query, context, target)

    async def _handle_kw_back_menu(self, query, context):
        user_data = _user_data(context)
        target = user_data.get('target_bot')
        if not target:
            return
        await self.show_menu(query, context, target)

