from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from z_pulse.utils import escape_markdown

logger = logging.getLogger(__name__)


class ZFlowTelegramExtension:
    """Z-Flow 연동 시 선택적으로 주입되는 Telegram 확장."""

    def __init__(self, bot):
        self.bot = bot

    @property
    def handler(self):
        return self.bot.bot_command_handler

    def _get_pair_manager(self):
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is None:
            return None
        try:
            return bridge.get_pair_manager()
        except ValueError:
            # 다중 manager 환경에서 bot 없이 호출 — snapshot에서 첫 번째 manager 반환
            snap = bridge.get_pair_manager_snapshot()
            managers: dict = snap.get("managers", {}) if snap else {}
            if not managers:
                return None
            return next(iter(managers.values()))

    def get_command_handlers(self) -> list[tuple[str, object]]:
        return [
            ("transfer", self.transfer_command),
            ("pair_trading", self.pair_trading_command),
        ]

    def get_bot_commands(self) -> list[BotCommand]:
        try:
            from z_flow.integration.telegram_surface import get_command_surface  # pyright: ignore[reportMissingImports]
            return [BotCommand(c.name, c.description) for c in get_command_surface()]
        except Exception:
            return []

    def get_help_lines(self) -> list[str]:
        try:
            from z_flow.integration.telegram_surface import get_command_surface  # pyright: ignore[reportMissingImports]
            return [c.help_line for c in get_command_surface()]
        except Exception:
            return []

    def get_text_routes(self) -> dict[str, object]:
        return {
            "pair_trading": self.pair_trading_command,
            "transfer": self.transfer_command,
        }

    async def handle_text_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        text = update.message.text
        pending = context.user_data.get("pending_transfer")
        if not pending:
            return False
        del context.user_data["pending_transfer"]
        if text == "YES":
            await self._execute_transfer(update, pending)
        else:
            await update.effective_message.reply_text("취소합니다.")
        return True

    def get_callback_routes(self, router) -> dict[str, object]:
        return {
            "pair_tier1_run": self.handle_pair_tier1_run,
            "panic_override": self.handle_panic_override,
            "ignore_divergence": self.handle_ignore_divergence,
        }

    async def transfer_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        """자산 이전 (GRVT_PAIR 계열 전용) - GRVT API를 통한 실제 자산 전송"""
        if not await self.handler.check_authorization(update):
            return
        msg = update.effective_message

        if not self.handler.grvt_manager:
            await msg.reply_text("❌ GRVT Transfer 기능이 초기화되지 않았습니다.")
            return

        if not context.args or len(context.args) < 3:
            await msg.reply_text(
                "❌ 3개의 인자를 입력해주세요.\n"
                "사용법: /transfer <FROM> <TO> <이전 금액>\n"
                "예시: /transfer GRVT-P-1 GRVT-P-2 500\n\n"
                "FROM에서 TO로 GRVT API를 통해 실제 자산을 이전합니다.\n"
                "⚠️ 대소문자를 정확히 구분하여 입력하세요."
            )
            return

        dir_name_1, dir_name_2 = context.args[0], context.args[1]

        try:
            transfer_amount = float(context.args[2])
            if transfer_amount <= 0:
                await msg.reply_text("❌ 이전 금액은 0보다 커야 합니다.")
                return
        except ValueError:
            await msg.reply_text(f"❌ '{context.args[2]}'는 유효한 숫자가 아닙니다.")
            return

        target_path_1, suggestions_1 = self.handler._find_directory_or_suggest(dir_name_1)
        target_path_2, suggestions_2 = self.handler._find_directory_or_suggest(dir_name_2)

        for name, target, suggestions in [
            (dir_name_1, target_path_1, suggestions_1),
            (dir_name_2, target_path_2, suggestions_2),
        ]:
            if not target:
                error_msg = f"❌ '{name}' 디렉토리를 찾을 수 없습니다."
                if suggestions:
                    error_msg += "\n\n💡 혹시 이 이름을 의미하셨나요?\n"
                    error_msg += "\n".join(f"  → {s}" for s in suggestions)
                else:
                    error_msg += "\n\n/status 명령어로 사용 가능한 디렉토리를 확인하세요."
                await msg.reply_text(error_msg)
                return

        dir_path_1 = target_path_1.parent
        dir_path_2 = target_path_2.parent

        from z_pulse.config.env_handler import (
            _guess_state_json_file,
            get_trading_info_from_env,
        )
        from z_pulse.monitoring.bot_state import is_pair_trading_type

        trading_type_1, _ = get_trading_info_from_env(dir_path_1)
        trading_type_2, _ = get_trading_info_from_env(dir_path_2)

        errors = []
        if not is_pair_trading_type(trading_type_1):
            errors.append(f"  • {dir_name_1}: TRADING_TYPE={trading_type_1 or '없음'}")
        if not is_pair_trading_type(trading_type_2):
            errors.append(f"  • {dir_name_2}: TRADING_TYPE={trading_type_2 or '없음'}")

        if errors:
            await msg.reply_text(
                "❌ GRVT_PAIR 계열 타입만 지원합니다.\n\n"
                + "\n".join(errors)
            )
            return

        try:
            self.handler.grvt_manager.extract_credentials(dir_path_1)
            self.handler.grvt_manager.extract_credentials(dir_path_2)
        except ValueError as e:
            await msg.reply_text(
                f"❌ GRVT 계정 정보 오류:\n{e}\n\n"
                "setting.env에 GRVT_API_KEY, GRVT_SECRET_KEY, "
                "GRVT_SUB_ACCOUNT_ID가 모두 있는지 확인하세요."
            )
            return

        db_files = {}
        equities = {}
        for name, dir_path in [(dir_name_1, dir_path_1), (dir_name_2, dir_path_2)]:
            db_file = _guess_state_json_file(dir_path, "GRVT_PAIR")
            if not db_file or not db_file.exists():
                await msg.reply_text(
                    f"❌ '{name}' DB 파일(*_DB.json)을 찾을 수 없습니다."
                )
                return
            try:
                with open(db_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                equity = data.get("firstTotalEquity")
                if equity is None:
                    await msg.reply_text(
                        f"❌ '{name}' DB 파일에 firstTotalEquity 키가 없습니다.\n"
                        f"파일: {db_file.name}"
                    )
                    return
                equities[name] = float(equity)
                db_files[name] = db_file
            except (json.JSONDecodeError, ValueError) as e:
                await msg.reply_text(f"❌ '{name}' DB 파일 파싱 오류: {e}")
                return

        equity_1 = equities[dir_name_1]
        equity_2 = equities[dir_name_2]
        new_equity_1 = equity_1 - transfer_amount
        new_equity_2 = equity_2 + transfer_amount

        await msg.reply_text(
            f"💰 GRVT API 자산 이전 미리보기\n\n"
            f"📂 {dir_name_1}: {equity_1:,.4f} → {new_equity_1:,.4f} (-{transfer_amount:,.4f})\n"
            f"📂 {dir_name_2}: {equity_2:,.4f} → {new_equity_2:,.4f} (+{transfer_amount:,.4f})\n\n"
            f"⚠️ 실제 GRVT API를 통해 자산이 이동됩니다.\n"
            f"⚠️ 봇 종료 → API 전송 → DB 수정 → 봇 재시작 순으로 진행됩니다.\n"
            f"⚠️ 진행하시려면 정확히 YES 를 입력하세요."
        )

        context.user_data["pending_transfer"] = {
            "dir_name_1": dir_name_1,
            "dir_name_2": dir_name_2,
            "dir_path_1": str(dir_path_1),
            "dir_path_2": str(dir_path_2),
            "db_file_1": str(db_files[dir_name_1]),
            "db_file_2": str(db_files[dir_name_2]),
            "new_equity_1": new_equity_1,
            "new_equity_2": new_equity_2,
            "transfer_amount": transfer_amount,
        }

    async def _execute_transfer(self, update: Update, transfer_info: dict):
        """자산 이전 실행 (YES 확인 후): 봇 종료 → GRVT API 전송 → DB 수정 → 봇 재시작"""
        msg = update.effective_message

        dir_name_1 = transfer_info["dir_name_1"]
        dir_name_2 = transfer_info["dir_name_2"]
        dir_path_1 = Path(transfer_info["dir_path_1"])
        dir_path_2 = Path(transfer_info["dir_path_2"])
        db_file_1 = Path(transfer_info["db_file_1"])
        db_file_2 = Path(transfer_info["db_file_2"])
        new_equity_1 = transfer_info["new_equity_1"]
        new_equity_2 = transfer_info["new_equity_2"]
        transfer_amount = transfer_info["transfer_amount"]

        try:
            await msg.reply_text(f"🛑 {dir_name_1}, {dir_name_2} 봇을 종료합니다...")
            for dir_name in [dir_name_1, dir_name_2]:
                is_running, _ = self.handler.process_controller.is_process_running(dir_name)
                if is_running:
                    self.handler.process_controller.kill_specific_process(dir_name)
            await asyncio.sleep(3)

            await msg.reply_text("🔐 GRVT 계정 정보를 읽습니다...")
            try:
                creds_from = self.handler.grvt_manager.extract_credentials(dir_path_1)
                creds_to = self.handler.grvt_manager.extract_credentials(dir_path_2)
            except ValueError as e:
                await msg.reply_text(f"❌ 계정 정보 추출 실패: {e}")
                return

            await msg.reply_text(
                f"💸 GRVT API 전송을 시작합니다...\n"
                f"  {dir_name_1} → {dir_name_2}\n"
                f"  금액: {transfer_amount} USDT"
            )

            result = await self.handler.grvt_manager.execute_transfer(
                from_creds=creds_from,
                to_creds=creds_to,
                amount=str(transfer_amount),
                currency="USDT",
            )

            if not result.success:
                await msg.reply_text(
                    f"❌ GRVT API 전송 실패:\n{result.message}\n\n"
                    f"⚠️ DB는 수정되지 않았습니다."
                )
                return

            bal_msg = "📡 GRVT API 전송 성공!\n"
            if result.balances_before.get("from") or result.balances_after.get("from"):
                bal_before_from = result.balances_before.get("from", {})
                bal_after_from = result.balances_after.get("from", {})
                bal_before_to = result.balances_before.get("to", {})
                bal_after_to = result.balances_after.get("to", {})

                def _fmt_bal(bal: dict) -> str:
                    if not bal:
                        return "조회 실패"
                    return ", ".join(f"{amt} {cur}" for cur, amt in bal.items())

                bal_msg += (
                    f"\n📂 {dir_name_1} 잔고:\n"
                    f"  Before: {_fmt_bal(bal_before_from)}\n"
                    f"  After:  {_fmt_bal(bal_after_from)}\n"
                    f"\n📂 {dir_name_2} 잔고:\n"
                    f"  Before: {_fmt_bal(bal_before_to)}\n"
                    f"  After:  {_fmt_bal(bal_after_to)}"
                )
            await msg.reply_text(bal_msg)

            await msg.reply_text("💾 DB 파일을 업데이트합니다...")
            try:
                for db_file, new_equity in [
                    (db_file_1, new_equity_1),
                    (db_file_2, new_equity_2),
                ]:
                    shutil.copy2(db_file, str(db_file) + ".bak")
                    with open(db_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    data["firstTotalEquity"] = new_equity
                    with open(db_file, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            except Exception as e:
                logger.error(f"DB 수정 실패 (API 전송은 성공): {e}")
                await msg.reply_text(
                    f"⚠️ API 전송은 성공했으나 DB 수정 실패: {e}\n"
                    f"백업 파일(.bak)로 수동 복구하세요."
                )
                return

            await msg.reply_text(f"🔄 {dir_name_1}, {dir_name_2} 봇을 재시작합니다...")
            for dir_name in [dir_name_1, dir_name_2]:
                await self.handler.process_controller.start_bot_process(dir_name)
            await asyncio.sleep(3)

            await msg.reply_text(
                f"✅ 전체 작업 완료!\n\n"
                f"📡 GRVT API 전송: 성공\n"
                f"💾 DB 업데이트: 완료\n"
                f"🔄 봇 재시작: 완료\n\n"
                f"📂 {dir_name_1}\n"
                f"  firstTotalEquity: {new_equity_1:,.4f}\n\n"
                f"📂 {dir_name_2}\n"
                f"  firstTotalEquity: {new_equity_2:,.4f}"
            )
        except Exception as e:
            logger.error(f"자산 이전 실패: {e}", exc_info=True)
            await msg.reply_text(
                f"❌ 오류 발생: {e}\n\n"
                f"⚠️ GRVT 거래소에서 전송 상태를 직접 확인하세요.\n"
                f"DB 백업 파일(.bak)이 있으면 복구 가능합니다."
            )

    @staticmethod
    def build_pair_cointegration_message(manager) -> tuple[str, InlineKeyboardMarkup]:
        state = manager.state
        esc = escape_markdown

        assignments_text = f"🤖 *{esc('GRVT 봇 배정')}*\n\n"
        if not state.active_assignments:
            assignments_text += f"_{esc('아직 배정된 봇이 없습니다.')}_\n"
        else:
            for bot_dir, (long, short) in state.active_assignments.items():
                assignments_text += f"\\- `{bot_dir}`: {esc(str(long))}/{esc(str(short))}\n"

        zscore_text = f"\n📈 *{esc('BTC/ETH 현황')}*\n"
        if state.current_rankings:
            pr = state.current_rankings[0]
            signal_emoji = {
                "STRONG_BUY": "🟢",
                "BUY": "🔵",
                "NEUTRAL": "⚪",
                "SELL": "🟡",
                "STRONG_SELL": "🔴",
            }.get(pr.signal, "⚪")
            zscore_text += (
                f"\\- {signal_emoji} Z\\-Score: `{pr.z_score:+.3f}` \\({esc(pr.signal)}\\)\n"
                f"\\- Score: `{pr.score:.4f}`\n"
            )
        if state.last_zscore_update:
            zscore_text += f"\\- {esc('마지막 업데이트')}: `{esc(str(state.last_zscore_update))}`\n"

        tier1_text = f"\n🔬 *Cointegration \\(TIER 1\\)*\n"
        if state.cointegration_results:
            sample = next(iter(state.cointegration_results.values()), None)
            if sample:
                ghe = getattr(sample, "ghe_value", None)
                degraded = getattr(sample, "stability_degraded", False)
                tier1_text += (
                    f"\\- spread\\_std: `{sample.spread_std:.6f}`\n"
                    f"\\- hedge\\_ratio: `{sample.hedge_ratio:.4f}`\n"
                    f"\\- half\\_life: `{sample.half_life:.2f}d`\n"
                )
                if ghe is not None:
                    status = "⚠️ 발산" if degraded else "✅ 평균회귀"
                    tier1_text += f"\\- GHE: `{ghe:.3f}` {esc(status)}\n"
        tier1_text += f"\\- {esc('마지막 실행')}: `{esc(str(state.last_cointegration_date or '없음'))}`\n"
        if getattr(state, "_tier1_running", False) if hasattr(state, "_tier1_running") else False:
            tier1_text += f"⏳ _{esc('현재 실행 중...')}_\n"

        full_message = assignments_text + zscore_text + tier1_text
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔬 TIER 1 재실행 (공적분 검사)", callback_data="pair_tier1_run")]]
        )
        return full_message, keyboard

    async def pair_trading_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        if not await self.handler.check_authorization(update):
            return

        manager = self._get_pair_manager()
        if not manager:
            await update.message.reply_text(
                "❌ 페어 매매가 활성화되지 않았습니다.\n\n"
                "활성화 방법:\n"
                "1. z_flow/data/SYMBOLS.cfg 또는 WS_KLINE_SYMBOL_FILE에 심볼 목록 설정\n"
                "2. 봇 재시작"
            )
            return

        try:
            full_message, keyboard = self.build_pair_cointegration_message(manager)
            await update.message.reply_text(
                full_message,
                parse_mode="MarkdownV2",
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error(f"페어 매매 현황 조회 실패: {e}", exc_info=True)
            await update.message.reply_text(f"❌ 오류 발생: {e}")

    async def handle_panic_override(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        params: list[str],
    ) -> None:
        manager = self._get_pair_manager()
        if not manager:
            await query.edit_message_text("❌ 페어 매매 매니저가 비활성 상태입니다.")
            return

        btc_monitor = getattr(manager, "btc_monitor", None)
        if not btc_monitor:
            await query.edit_message_text("❌ BTC 패닉 모니터가 비활성 상태입니다.")
            return

        released = btc_monitor.force_exit_panic()
        if released:
            logger.info("BTC panic mode manually overridden by user")
            await query.edit_message_text(
                "✅ *BTC 패닉 모드 해제 완료*\n"
                "• 신규 진입이 재개됩니다\n"
                "• BTC 변동성이 재급등하면 다시 발동됩니다",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                "ℹ️ 패닉 모드가 이미 해제 상태입니다.",
                parse_mode="Markdown",
            )

    async def handle_ignore_divergence(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        params: list[str],
    ) -> None:
        if not params:
            await query.answer("❌ 페어 정보가 없습니다.", show_alert=True)
            return

        pair_name = params[0]
        if "/" not in pair_name:
            await query.answer("❌ 잘못된 페어 형식입니다.", show_alert=True)
            return

        long_sym, short_sym = pair_name.split("/", 1)
        pair_key = (long_sym, short_sym)

        manager = self._get_pair_manager()
        if not manager or not getattr(manager, "stop_loss_monitor", None):
            await query.answer("❌ 위험 감시 모듈이 비활성 상태입니다.", show_alert=True)
            return

        until = manager.stop_loss_monitor.ignore_pair_for(pair_key, minutes=60)
        logger.info(f"divergence ignore set for {pair_key} until={until.isoformat(timespec='seconds')}")

        await query.answer("✅ 1시간 무시 적용", show_alert=False)
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

    async def handle_pair_tier1_run(
        self,
        query,
        context: ContextTypes.DEFAULT_TYPE,
        params: list[str],
    ) -> None:
        manager = self._get_pair_manager()
        if not manager:
            await query.edit_message_text("❌ 페어 매매 매니저가 활성화되지 않았습니다.")
            return

        if getattr(manager, "_tier1_running", False):
            await query.edit_message_text("⏳ TIER 1이 이미 실행 중입니다. 완료까지 기다려주세요.")
            return

        await query.edit_message_text("🔬 TIER 1 공적분 전수 검사를 시작합니다...\n\n⏳ 수 분이 소요될 수 있습니다.")

        try:
            await manager.run_cointegration_test()
            n_coint = len(manager.state.cointegration_results)
            sample = next(iter(manager.state.cointegration_results.values()), None)
            std_ok = sample and sample.spread_std > 0.01
            await query.edit_message_text(
                f"✅ TIER 1 완료\n\n"
                f"- 공적분 페어: {n_coint}개\n"
                f"- spread_std: {'✅ 보정됨' if std_ok else '⚠️ 미보정'}\n"
                f"- 다음 TIER 3에서 Score 반영됩니다."
            )
        except Exception as e:
            logger.error(f"TIER 1 수동 실행 실패: {e}", exc_info=True)
            await query.edit_message_text(f"❌ TIER 1 실행 실패: {e}")
