"""
Dashboard Handler Module

대시보드(메인 상태판) 및 프로세스 상세 정보 뷰를 담당하는 핸들러입니다.
ZPulse의 UI 로직을 분리하여 관리합니다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import psutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.error import TelegramError

# 유틸리티 및 설정 임포트 (Z-Pulse와 동일한 환경 가정)
from z_pulse.utils import escape_markdown, format_process_detail, format_pair_trading_detail, format_slot_detail, format_error_message, format_dashboard_summary
from z_pulse.utils.error_handler import is_transient_telegram_error
from z_pulse.utils.async_helpers import safe_send_message_with_result
from z_pulse.config import (
    load_ignored_dirs,
    get_trading_info_from_env,
    get_entry_count_generic
)
from z_pulse.monitoring.bot_state import PairBotState, is_pair_trading_type, resolve_pair_bot_state
from z_pulse.integration.z_flow_bridge import SlotLivePhase
from z_pulse.monitoring.session_store import SessionStore
from z_pulse.monitoring.process_monitor import process_uptime
from z_pulse.utils.telegram_gateway import (
    DASHBOARD_TIMEOUT,
    TelegramPriority,
    get_telegram_gateway,
)

logger = logging.getLogger(__name__)

INTERACTIVE_DASHBOARD_TIMEOUT = 8.0



def _find_z_flow_processes(monitor: Any, bridge_owner: Any | None = None) -> list[tuple[Any | None, str, Any]]:
    bridge = getattr(bridge_owner, "find_runtime_processes", None)
    if callable(bridge):
        runtime_processes = bridge(monitor)
        if isinstance(runtime_processes, list):
            return runtime_processes
        return []

    find_runtime_processes = getattr(monitor, "find_z_flow_processes", None)
    if not callable(find_runtime_processes):
        return []
    runtime_processes = find_runtime_processes()
    if isinstance(runtime_processes, list):
        return runtime_processes
    return []


class DashboardHandler:
    def __init__(self, bot_instance, monitor):
        """
        DashboardHandler 초기화

        Args:
            bot_instance: 메인 ZPulse 인스턴스 (또는 필요한 컨텍스트)
            monitor: ProcessMonitor 인스턴스
        """
        self.bot = bot_instance
        self.monitor = monitor

        # 디바운싱용 태스크 변수
        self._dashboard_refresh_task = None
        self._dashboard_refresh_running = False
        self._dashboard_refresh_pending: Optional[tuple[Any, bool]] = None

        # 마지막 대시보드 메시지 추적
        self._last_dashboard_chat_id = None
        self._last_dashboard_message_id = None

        # 현재 화면 상태 추적 ("dashboard" 또는 "detail:{bot_name}")
        self._current_view_state = "dashboard"

    def _is_rotation_enabled(self, target: str) -> bool:
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is None:
            return False
        if not bridge.is_pair_trading_ui_enabled():
            return False
        return bridge.is_rotation_enabled(target)

    def _get_rotation_slot_type(self, target: str):
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is None:
            return None
        if not bridge.is_pair_trading_ui_enabled():
            return None
        return bridge.get_slot_type(target)

    def _resolve_pair_bot_state(self, dir_path: Any, process_running: bool) -> PairBotState:
        path = Path(dir_path)
        try:
            session = SessionStore(path).load()
        except json.JSONDecodeError as exc:
            logger.warning("[DASHBOARD] session.json 파싱 실패 — %s: %s", path, exc)
            session = None
        return resolve_pair_bot_state(
            path,
            process_running=process_running,
            session=session,
        )

    def _get_pair_assignment_status(self, dir_path, rotation_on: bool, proc) -> tuple[str, str]:
        """외부 페어트레이딩 봇의 자동 배정 상태/설명 계산"""
        if not rotation_on:
            if proc is not None:
                return (
                    "⏸️ 자동 배정 OFF · 실행 중",
                    "현재는 실행 중이지만 다음 자동 배정 대상에서는 제외됩니다.",
                )
            return (
                "⏸️ 자동 배정 OFF",
                "자동 배정이 꺼져 있어 배정 후보에서는 제외되며, 시작/종료 버튼이 즉시 프로세스를 제어합니다.",
            )

        resolved_state = self._resolve_pair_bot_state(dir_path, process_running=proc is not None)
        if resolved_state is PairBotState.RUNNING:
            return (
                "🟢 ON_RUNNING",
                "자동 관리 하에서 현재 프로세스가 실행 중입니다. 종료만 직접 허용됩니다.",
            )
        if resolved_state is PairBotState.MANUAL_STOP:
            return (
                "🛑 ON_MANUAL_STOP",
                "자동 관리 상태에서 수동 정지되었습니다. 필요 시 자동 배정 재개로 배정 상태를 초기화할 수 있습니다.",
            )
        if resolved_state is PairBotState.BLOCKED:
            return ("⚠️ 재개 필요", "비정상 종료로 추정되어 자동 관리가 보류됩니다. 자동 배정 재개를 사용하세요.")
        if resolved_state is PairBotState.WAITING_WITH_WARNING:
            return ("⏳ ON_WAITING", "자동 관리가 켜져 있으며 state 유실 경고와 함께 다음 진입 신호를 기다리는 상태입니다.")
        return ("⏳ ON_WAITING", "자동 관리가 켜져 있으며 최초 진입 신호를 기다리는 상태입니다. 아직 시작 버튼은 없습니다.")

    def _get_slot_assignment_status(
        self,
        rotation_on: bool,
        proc,
        slot_type: str,
        exchange_id: str,
        ui_state: "str | None" = None,
        phase: "SlotLivePhase | None" = None,
    ) -> tuple[str, str, str]:
        """Z-Flow의 슬롯 관리 상태/설명 계산.

        ui_state: _resolve_ui_state 결과 ("ON_WAITING"|"ON_RUNNING"|"ON_IDLE"|"ON_MANUAL_STOP"|"ON_BLOCKED"|None)
        phase:    SlotLivePhase (current_level 등 표시용), None이면 레벨 표시 생략
        ui_state=None 폴백은 proc 유무 기반 (기존 호환).
        """
        assignment_label = f"ON · {'BTC/ETH' if slot_type == 'BTC_ETH' else slot_type}"
        if not rotation_on:
            if proc is not None:
                return (
                    "⏸️ 자동 배정 OFF · 실행 중",
                    "현재는 실행 중이지만 자동 배정 대상에서는 제외됩니다.",
                    "OFF",
                )
            return (
                "⏸️ 자동 배정 OFF",
                "자동 관리가 꺼져 있어 배정 후보에서는 제외되며, 시작/종료 버튼이 즉시 프로세스를 제어합니다.",
                "OFF",
            )

        # rotation_on=True 아래: ui_state 기반 분기
        if ui_state == "ON_WAITING":
            return (
                f"⏳ 시그널 대기 · {exchange_id}",
                "자동 관리 하에서 진입 신호를 기다리는 상태입니다.",
                assignment_label,
            )

        if ui_state == "ON_RUNNING":
            level_text = f" · L{phase.current_level}" if phase and phase.current_level > 0 else ""
            return (
                f"🟢 실행 중{level_text} · {exchange_id}",
                "자동 관리 하에서 슬롯 런타임이 진입 후 실행 중입니다. 종료만 직접 허용됩니다.",
                assignment_label,
            )

        if ui_state == "ON_MANUAL_STOP":
            return (
                f"🛑 수동 정지 · {exchange_id}",
                "정상 종료됩니다. 자동 배정 재시작이 가능합니다.",
                assignment_label,
            )

        if ui_state == "ON_BLOCKED":
            return (
                f"⚠️ 재개 필요 · {exchange_id}",
                "비정상 종료로 추정되어 자동 관리가 보류됩니다. 자동 배정 재개를 사용하세요.",
                assignment_label,
            )

        # ON_IDLE 또는 ui_state=None 폴백 — proc 기반 (기존 호환)
        if proc is not None:
            return (
                f"🟢 ON_RUNNING · {exchange_id}",
                "자동 관리 하에서 슬롯 런타임이 실행 중입니다. 종료만 직접 허용됩니다.",
                assignment_label,
            )

        return (
            f"⏳ 시그널 대기 · {exchange_id}",
            "자동 관리가 켜져 있으며 진입 신호/명령을 기다리는 상태입니다. 아직 시작 버튼은 없습니다.",
            assignment_label,
        )

    def _resolve_pair_detail_context(
        self,
        target: str,
        target_path: Optional[Any],
        proc: Any,
    ) -> Optional[dict[str, Any]]:
        dir_path = self._target_dir_from_path(target_path)
        if dir_path is None and target_path is not None:
            dir_path = getattr(target_path, "parent", None)
        if dir_path is None:
            dir_path = self._find_dir_path(target)
        if dir_path is None:
            return None

        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is None or not bridge.is_pair_trading_ui_enabled():
            return None

        trading_type, _ = get_trading_info_from_env(dir_path)
        if not is_pair_trading_type(trading_type):
            return None

        rotation_on = self._is_rotation_enabled(target)
        slot_type = self._get_rotation_slot_type(target) if rotation_on else None
        assignment_label = f"ON · {'BTC/ETH' if slot_type == 'BTC_ETH' else '기타'}" if rotation_on else "OFF"
        assignment_state, assignment_description = self._get_pair_assignment_status(dir_path, rotation_on, proc)
        resolved_state = self._resolve_pair_bot_state(dir_path, process_running=proc is not None)

        from z_pulse.config.env_handler import EnvConfigHandler
        from z_pulse.bot.handlers.settings import SettingsHandler

        env_config = EnvConfigHandler.parse(dir_path)
        coin1 = (env_config.get("COIN1") or "").strip()
        coin2 = (env_config.get("COIN2") or "").strip()

        return {
            "dir_path": dir_path,
            "trading_type": trading_type,
            "rotation_on": rotation_on,
            "slot_type": slot_type,
            "assignment_label": assignment_label,
            "assignment_state": assignment_state,
            "assignment_description": assignment_description,
            "resolved_state": resolved_state,
            "current_pair": f"{coin1} / {coin2}" if coin1 and coin2 else None,
            "port": env_config.get("PORT"),
            "trading_type_label": SettingsHandler.EDITABLE_SETTINGS.get("TRADING_TYPE", "TRADING TYPE"),
            "port_label": SettingsHandler.EDITABLE_SETTINGS.get("PORT", "Port No."),
        }

    def _target_dir_from_path(self, target_path: Optional[Any]) -> Optional[Any]:
        if target_path is None:
            return None
        if self._is_slot_target(getattr(target_path, "name", ""), target_path):
            return target_path
        if getattr(target_path, "parent", None) is not None:
            return target_path.parent
        return target_path

    def _is_slot_target(self, dir_name: str, dir_path: Optional[Any] = None) -> bool:
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if dir_path is not None:
            try:
                data_dir = Path(dir_path)
            except TypeError:
                data_dir = None
            checker = getattr(bridge, "is_runtime_data_dir", None)
            if data_dir is not None and callable(checker):
                result = checker(data_dir)
                if result is True:
                    return True

        z_flow_dirs = getattr(self.monitor, "z_flow_dirs", {})
        if isinstance(z_flow_dirs, dict) and dir_name in z_flow_dirs:
            return True
        if not dir_name.upper().startswith("SLOT-"):
            return False

        checker = getattr(bridge, "is_runtime_target", None)
        if callable(checker):
            result = checker(dir_name, self.monitor)
            if result is True:
                return True
        return dir_name.upper().startswith("SLOT-")

    def _find_dir_path(self, dir_name: str, running_by_dir: Optional[dict[str, tuple[Any, Any]]] = None) -> Optional[Any]:
        if running_by_dir and dir_name in running_by_dir:
            _, target_path = running_by_dir[dir_name]
            return self._target_dir_from_path(target_path)
        for target_path in self.monitor.all_program_paths:
            if target_path.parent.name == dir_name:
                return target_path.parent
        for slot_dir in self._list_z_flow_runtime_targets():
            if getattr(slot_dir, "name", None) == dir_name:
                return slot_dir
        return None

    def _list_z_flow_runtime_targets(self) -> list[Any]:
        bridge = getattr(self.bot, "z_flow_bridge", None)
        list_targets = getattr(bridge, "list_runtime_targets", None)
        if callable(list_targets):
            runtime_targets = list_targets(
                getattr(self.monitor, "target_dir", None),
                getattr(self.monitor, "ignore_list", set()),
            )
            if isinstance(runtime_targets, list):
                return runtime_targets
        z_flow_dirs = getattr(self.monitor, "z_flow_dirs", {})
        if isinstance(z_flow_dirs, dict):
            return [pid_file.parent for pid_file in z_flow_dirs.values()]
        return []

    def _resolve_slot_data_dir(self, target: str, target_path: Optional[Any]) -> Optional[Any]:
        resolved_from_target = self._target_dir_from_path(target_path)
        if resolved_from_target is not None:
            return resolved_from_target

        z_flow_dirs = getattr(self.monitor, "z_flow_dirs", {})
        if isinstance(z_flow_dirs, dict):
            pid_file = z_flow_dirs.get(target)
            if pid_file is not None:
                return pid_file.parent

        for _, dir_name, data_dir in _find_z_flow_processes(
            self.monitor, getattr(self.bot, "z_flow_bridge", None)
        ):
            if dir_name == target:
                return data_dir

        for slot_dir in self._list_z_flow_runtime_targets():
            if getattr(slot_dir, "name", None) == target:
                return slot_dir

        return None

    def _get_slot_runtime_metadata(self, data_dir: Optional[Any]) -> dict[str, Any]:
        if data_dir is None:
            return {}
        bridge = getattr(self.bot, "z_flow_bridge", None)
        getter = getattr(bridge, "get_slot_runtime_metadata", None)
        if not callable(getter):
            return {}
        metadata = getter(data_dir)
        if isinstance(metadata, dict):
            return metadata
        return {}

    def _get_list_badge(self, dir_name: str, is_running: bool, is_slot: bool) -> str:
        _ = (dir_name, is_running, is_slot)
        return ""

    def _get_slot_phase_from_map(self, dir_name: str, dir_path: Optional[Any], phase_map: dict) -> Optional[SlotLivePhase]:
        """phase_map(dict[int, SlotLivePhase])에서 슬롯 phase를 조회한다."""
        if not phase_map:
            return None
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is None:
            return None
        data_dir = self._resolve_slot_data_dir(dir_name, dir_path)
        if data_dir is None:
            return None
        try:
            meta = bridge.get_slot_runtime_metadata(Path(data_dir))
            slot_id = int(meta.get("slot_id", 0)) if meta else 0
            if slot_id <= 0:
                return None
            return phase_map.get(slot_id)
        except Exception:
            return None

    def _resolve_ui_state(self, dir_name: str, dir_path: Optional[Any], is_slot: bool, proc: Any, phase_map: Optional[dict] = None) -> str:
        rotation_on = self._is_rotation_enabled(dir_name)

        if not rotation_on:
            return "OFF_RUNNING" if proc is not None else "OFF_IDLE"

        if proc is not None:
            # 슬롯이고 phase_map이 있으면 phase에 따라 ON_WAITING / ON_RUNNING 구분
            if is_slot and phase_map is not None:
                live_phase = self._get_slot_phase_from_map(dir_name, dir_path, phase_map)
                if live_phase is not None and live_phase.is_fresh:
                    if live_phase.phase == "PRE_ENTRY":
                        return "ON_WAITING"
                    return "ON_RUNNING"
            return "ON_RUNNING"

        if is_slot and dir_path is None:
            return "ON_IDLE"

        if is_slot:
            # 슬롯이 멈춰있으면 pair_bot_state fallback으로 BLOCKED/MANUAL_STOP/WAITING 판별
            if dir_path is not None:
                resolved_state = self._resolve_pair_bot_state(dir_path, process_running=False)
                if resolved_state is PairBotState.MANUAL_STOP:
                    return "ON_MANUAL_STOP"
                if resolved_state is PairBotState.BLOCKED:
                    return "ON_BLOCKED"
                if resolved_state is PairBotState.WAITING:
                    return "ON_BLOCKED"   # auto-on + dead + WAITING = abnormal absence → blocked
            return "ON_IDLE"

        if dir_path is None:
            return "ON_WAITING"

        resolved_state = self._resolve_pair_bot_state(dir_path, process_running=False)
        if resolved_state is PairBotState.MANUAL_STOP:
            return "ON_MANUAL_STOP"
        if resolved_state is PairBotState.BLOCKED:
            return "ON_BLOCKED"
        return "ON_WAITING"

    def _get_action_button(self, dir_name: str, dir_path: Optional[Any], is_slot: bool, proc: Any, phase_map: Optional[dict] = None) -> InlineKeyboardButton:
        ui_state = self._resolve_ui_state(dir_name, dir_path, is_slot, proc, phase_map=phase_map)
        if ui_state == "OFF_RUNNING":
            return InlineKeyboardButton("종료", callback_data=f"kill:{dir_name}")
        if ui_state == "OFF_IDLE":
            return InlineKeyboardButton("시작", callback_data=f"run:{dir_name}")
        if ui_state == "ON_RUNNING":
            return InlineKeyboardButton("🔄 종료", callback_data=f"kill:{dir_name}")
        if ui_state == "ON_IDLE":
            return InlineKeyboardButton("시작", callback_data=f"run:{dir_name}")
        if ui_state == "ON_MANUAL_STOP":
            return InlineKeyboardButton("🔄 시작", callback_data=f"run:{dir_name}")
        if ui_state == "ON_BLOCKED":
            return InlineKeyboardButton("⚠️ 재개 필요", callback_data=f"detail:{dir_name}")
        return InlineKeyboardButton("🔄 시그널 대기", callback_data=f"detail:{dir_name}")

    async def update_dashboard(self, query: Optional[CallbackQuery] = None, update: Optional[Update] = None, force_rescan: bool = True):
        """
        인터랙티브 프로세스 대시보드 업데이트

        Args:
            query: 콜백 쿼리 (버튼 클릭 시)
            update: 업데이트 객체 (명령어 실행 시)
            force_rescan: 디렉토리 목록 재스캔 여부 (기본 True)
        """
        # Bot application 체크는 호출하는 쪽이나 라이브러리 레벨에서 보장된다고 가정,
        # 혹은 self.bot.application 사용
        if not (self.bot.application and self.bot.application.bot):
            return

        started_at = time.monotonic()
        logger.info(
            "[DASHBOARD][UPDATE_START] force_rescan=%s query_id=%s message_id=%s source=%s",
            force_rescan,
            getattr(query, "id", None) if query else None,
            getattr(getattr(query, "message", None), "message_id", None) if query else None,
            "query" if query else ("update" if update else "internal"),
        )
        try:
            _, summary_text, keyboard = await asyncio.to_thread(
                self._prepare_dashboard_payload,
                force_rescan,
            )
            prepare_elapsed = time.monotonic() - started_at
            if prepare_elapsed >= 1.0:
                logger.warning(
                    "[DASHBOARD][SLOW][PREPARE] force_rescan=%s elapsed=%.2fs",
                    force_rescan,
                    prepare_elapsed,
                )
            await self._send_dashboard_message(query, update, summary_text, keyboard)
            total_elapsed = time.monotonic() - started_at
            logger.info(
                "[DASHBOARD][UPDATE_DONE] force_rescan=%s elapsed=%.2fs source=%s",
                force_rescan,
                total_elapsed,
                "query" if query else ("update" if update else "internal"),
            )
            if total_elapsed >= 2.0:
                logger.warning(
                    "[DASHBOARD][SLOW][TOTAL] force_rescan=%s elapsed=%.2fs",
                    force_rescan,
                    total_elapsed,
                )
        except TelegramError as e:
            if "Message is not modified" in str(e):
                return
            logger.exception("Telegram API 오류: %s: %s", type(e).__name__, e)
        except Exception as e:
            logger.exception("대시보드 업데이트 치명적 오류: %s: %s", type(e).__name__, e)

    async def safe_update_dashboard(self, query: Optional[CallbackQuery] = None, update: Optional[Update] = None, force_rescan: bool = True):
        """안전한 대시보드 업데이트 (에러 시 로그만 출력)"""
        try:
            await self.update_dashboard(query, update, force_rescan)
        except Exception as e:
            logger.warning(f"대시보드 업데이트 건너뜀 (봇 종료 중일 수 있음): {e}")

    async def send_initial_dashboard(self, chat_id: int):
        """봇 시작 시 초기 대시보드 전송 (메시지 추적 활성화)"""
        if not (self.bot.application and self.bot.application.bot):
            return

        started_at = time.monotonic()
        try:
            logger.info("초기 대시보드 준비 시작")
            _, summary_text, keyboard = await asyncio.to_thread(
                self._prepare_dashboard_payload,
                True,
            )
            reply_markup = InlineKeyboardMarkup(keyboard)
            prepare_elapsed = time.monotonic() - started_at

            # 대시보드 메시지 전송
            # safe_send_message_with_result 사용 (메시지 추적 필요)
            logger.info(
                "초기 대시보드 전송 시작: prepare_elapsed=%.2fs text_len=%d",
                prepare_elapsed,
                len(summary_text),
            )
            sent_message = await safe_send_message_with_result(
                self.bot.application.bot,
                chat_id=chat_id,
                text=summary_text,
                reply_markup=reply_markup,
                parse_mode='MarkdownV2',
                priority=TelegramPriority.DASHBOARD,
                timeout=DASHBOARD_TIMEOUT,
                max_retries=3,
                base_delay=0.7,
            )

            # 메시지 추적 설정
            if sent_message:
                self._last_dashboard_chat_id = chat_id
                self._last_dashboard_message_id = sent_message.message_id
                self._current_view_state = "dashboard"
                logger.info(
                    "초기 대시보드 전송 완료: message_id=%s elapsed=%.2fs",
                    sent_message.message_id,
                    time.monotonic() - started_at,
                )
            else:
                logger.warning(
                    "초기 대시보드 전송 실패: message=None elapsed=%.2fs",
                    time.monotonic() - started_at,
                )
                try:
                    asyncio.create_task(self._retry_initial_dashboard_send(chat_id))
                except RuntimeError:
                    logger.warning("초기 대시보드 재시도 스케줄 실패: loop unavailable")

        except TelegramError as e:
            logger.exception("초기 대시보드 전송 실패: %s: %s", type(e).__name__, e)

    async def _retry_initial_dashboard_send(self, chat_id: int) -> None:
        """초기 대시보드 전송 실패 시 1회 지연 재시도."""
        await asyncio.sleep(5.0)
        if not (self.bot.application and self.bot.application.bot):
            return
        started_at = time.monotonic()
        try:
            logger.info("초기 대시보드 지연 재시도 시작")
            _, summary_text, keyboard = await asyncio.to_thread(
                self._prepare_dashboard_payload,
                True,
            )
            reply_markup = InlineKeyboardMarkup(keyboard)
            sent_message = await safe_send_message_with_result(
                self.bot.application.bot,
                chat_id=chat_id,
                text=summary_text,
                reply_markup=reply_markup,
                parse_mode='MarkdownV2',
                priority=TelegramPriority.DASHBOARD,
                timeout=DASHBOARD_TIMEOUT,
                max_retries=2,
                base_delay=0.7,
            )
            if sent_message:
                self._last_dashboard_chat_id = chat_id
                self._last_dashboard_message_id = sent_message.message_id
                self._current_view_state = "dashboard"
                logger.info(
                    "초기 대시보드 지연 재시도 성공: message_id=%s elapsed=%.2fs",
                    sent_message.message_id,
                    time.monotonic() - started_at,
                )
            else:
                logger.warning(
                    "초기 대시보드 지연 재시도 실패: elapsed=%.2fs",
                    time.monotonic() - started_at,
                )
        except Exception as exc:
            logger.warning("초기 대시보드 지연 재시도 예외: %s", exc)

    def trigger_refresh(self, query, force_rescan: bool = False):
        """대시보드 갱신 트리거 (Debounce 적용)

        Args:
            query: 콜백 쿼리
            force_rescan: True면 디렉토리 구조 재스캔 (새로고침 버튼 클릭 시)
        """
        if self._dashboard_refresh_running:
            pending = self._dashboard_refresh_pending
            if pending is None or force_rescan or pending[0] is None:
                self._dashboard_refresh_pending = (query, force_rescan or (pending[1] if pending else False))
            return

        try:
            self._dashboard_refresh_task = asyncio.create_task(self._dashboard_refresh_impl(query, force_rescan))
        except RuntimeError:
            pass

    async def _dashboard_refresh_impl(self, query, force_rescan: bool = False):
        """대시보드 갱신 실행 (디바운싱 적용 구현)

        Args:
            query: 콜백 쿼리 (None이면 파일 감시자에서 호출된 것)
            force_rescan: True면 디렉토리 구조 재스캔 (새로고침 버튼 클릭 시)
        """
        started_at = time.monotonic()
        try:
            self._dashboard_refresh_running = True
            while True:
                self._dashboard_refresh_pending = None
                await asyncio.sleep(0.1)

                if force_rescan:
                    logger.debug("📊 대시보드 새로고침: 디렉토리 구조 재스캔 수행 중...")
                else:
                    logger.debug("📊 대시보드 상태 변화 감지: 자동 갱신 수행 중...")

                if query is None:
                    await self._refresh_tracked_dashboard(force_rescan)
                else:
                    await self.safe_update_dashboard(query, force_rescan=force_rescan)

                pending = self._dashboard_refresh_pending
                if pending is None:
                    break
                query, force_rescan = pending

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception("대시보드 갱신 실패: %s: %s", type(e).__name__, e)
        finally:
            self._dashboard_refresh_running = False
            logger.info(
                "[HANDLER][TIMING] operation=dashboard_refresh force_rescan=%s elapsed=%.3fs",
                force_rescan,
                time.monotonic() - started_at,
            )

    async def _refresh_tracked_dashboard(self, force_rescan: bool = False):
        """추적 중인 대시보드 메시지 직접 갱신 (파일 감시자용)

        Args:
            force_rescan: True면 디렉토리 구조 재스캔
        """
        if not (self._last_dashboard_chat_id and self._last_dashboard_message_id):
            logger.debug("추적 중인 대시보드 없음, 갱신 건너뜀")
            return

        # 상세 정보 화면에 있을 때는 자동 갱신 건너뜀
        if self._current_view_state != "dashboard":
            logger.debug(f"상세 정보 화면 중이므로 자동 갱신 건너뜀: {self._current_view_state}")
            return

        if not (self.bot.application and self.bot.application.bot):
            return

        try:
            _, summary_text, keyboard = await asyncio.to_thread(
                self._prepare_dashboard_payload,
                force_rescan,
            )
            reply_markup = InlineKeyboardMarkup(keyboard)

            await get_telegram_gateway().bot_edit_message_text(
                self.bot.application.bot,
                chat_id=self._last_dashboard_chat_id,
                message_id=self._last_dashboard_message_id,
                text=summary_text,
                reply_markup=reply_markup,
                parse_mode='MarkdownV2',
                timeout=DASHBOARD_TIMEOUT,
            )
            logger.debug("파일 감시자에 의한 대시보드 갱신 완료")

        except TelegramError as e:
            if "Message is not modified" in str(e):
                return
            logger.warning(f"파일 감시자 대시보드 갱신 실패: {e}")

    def _prepare_dashboard_payload(
        self,
        force_rescan: bool,
    ) -> tuple[dict[str, Any], str, list[list[InlineKeyboardButton]]]:
        """대시보드 데이터 수집 + UI 구성 (동기 작업 묶음)"""
        dashboard_data = self._collect_dashboard_data(force_rescan)
        summary_text, keyboard = self._build_dashboard_ui(dashboard_data)
        return dashboard_data, summary_text, keyboard

    def _build_detail_text(
        self,
        target: str,
        is_ignored: bool,
        is_slot: bool,
        proc: Any,
        target_path: Optional[Any],
        pair_context: Optional[dict[str, Any]],
        phase_map: Optional[dict] = None,
    ) -> str:
        """상세 화면 텍스트 생성 (동기 작업 묶음)."""
        if is_ignored:
            return format_process_detail(target, is_ignored=True)

        if is_slot:
            data_dir = self._resolve_slot_data_dir(target, target_path)
            metadata = self._get_slot_runtime_metadata(data_dir)
            slot_id = metadata.get("slot_id", "?")
            slot_type = metadata.get("slot_type", "?")
            exchange_id = metadata.get("exchange_id", "?")
            margin = metadata.get("margin", "?")
            use_testnet = bool(metadata.get("use_testnet", False))
            net_label = "TESTNET" if use_testnet else "MAINNET"
            rotation_on = self._is_rotation_enabled(target)
            _phase_map = phase_map or {}
            ui_state = self._resolve_ui_state(target, data_dir, True, proc, phase_map=_phase_map)
            ph = self._get_slot_phase_from_map(target, data_dir, _phase_map)
            assignment_state, assignment_description, assignment_label = (
                self._get_slot_assignment_status(
                    rotation_on,
                    proc,
                    str(slot_type),
                    str(exchange_id),
                    ui_state=ui_state,
                    phase=ph,
                )
            )
            slot_id_text = str(slot_id) if slot_id not in (None, "") else "?"
            slot_type_text = str(slot_type) if slot_type not in (None, "") else "?"
            exchange_text = str(exchange_id) if exchange_id not in (None, "") else "?"
            margin_text = str(margin) if margin not in (None, "") else "?"

            pid = None
            cpu: Optional[float] = None
            mem_mb: Optional[float] = None
            uptime = None
            if proc is not None:
                with proc.oneshot():
                    uptime = process_uptime(proc)
                    cpu = proc.cpu_percent()
                    mem_mb = proc.memory_info().rss / 1e6
                pid = proc.pid

            return format_slot_detail(
                target=target,
                assignment_enabled=rotation_on,
                assignment_label=assignment_label,
                assignment_state=assignment_state,
                assignment_description=assignment_description,
                slot_id=slot_id_text,
                slot_type=slot_type_text,
                exchange_id=exchange_text,
                net_label=net_label,
                margin=margin_text,
                pid=pid,
                cpu_percent=cpu,
                memory_mb=mem_mb,
                uptime=uptime,
            )

        if proc is not None:
            dir_path = target_path.parent if target_path else None
            if dir_path is None:
                for tp in self.monitor.all_program_paths:
                    if tp.parent.name == target:
                        dir_path = tp.parent
                        break

            if dir_path is None:
                return format_process_detail(target, is_running=True)

            with proc.oneshot():
                uptime = process_uptime(proc)
                trading_type, _ = get_trading_info_from_env(dir_path)
                entry_count = get_entry_count_generic(dir_path, trading_type)

                from z_pulse.config.env_handler import EnvConfigHandler

                config = EnvConfigHandler.parse(dir_path)
                port = config.get("PORT")

                from z_pulse.bot.handlers.settings import SettingsHandler

                trading_type_label = SettingsHandler.EDITABLE_SETTINGS.get(
                    "TRADING_TYPE", "TRADING TYPE"
                )
                port_label = SettingsHandler.EDITABLE_SETTINGS.get("PORT", "Port No.")

                if pair_context is not None:
                    return format_pair_trading_detail(
                        target=target,
                        assignment_enabled=pair_context["rotation_on"],
                        assignment_label=pair_context["assignment_label"],
                        assignment_state=pair_context["assignment_state"],
                        assignment_description=pair_context["assignment_description"],
                        pid=proc.pid,
                        cpu_percent=proc.cpu_percent(),
                        memory_mb=proc.memory_info().rss / 1e6,
                        uptime=uptime,
                        entry_count=entry_count,
                        trading_type=pair_context["trading_type"],
                        port=pair_context["port"],
                        current_pair=pair_context["current_pair"],
                        trading_type_label=pair_context["trading_type_label"],
                        port_label=pair_context["port_label"],
                    )
                if is_pair_trading_type(trading_type):
                    return format_pair_trading_detail(
                        target=target,
                        assignment_enabled=False,
                        assignment_label="OFF",
                        assignment_state="⏸️ 자동 배정 OFF · 실행 중",
                        assignment_description="현재는 실행 중이지만 다음 자동 배정 대상에서는 제외됩니다.",
                        pid=proc.pid,
                        cpu_percent=proc.cpu_percent(),
                        memory_mb=proc.memory_info().rss / 1e6,
                        uptime=uptime,
                        entry_count=entry_count,
                        trading_type=trading_type,
                        port=port,
                        current_pair=None,
                        trading_type_label=trading_type_label,
                        port_label=port_label,
                    )
                return format_process_detail(
                    target=target,
                    pid=proc.pid,
                    cpu_percent=proc.cpu_percent(),
                    memory_mb=proc.memory_info().rss / 1e6,
                    uptime=uptime,
                    entry_count=entry_count,
                    trading_type=trading_type,
                    port=port,
                    trading_type_label=trading_type_label,
                    port_label=port_label,
                )

        detail_text = format_process_detail(target, is_running=False)
        if pair_context is not None:
            return format_pair_trading_detail(
                target=target,
                assignment_enabled=pair_context["rotation_on"],
                assignment_label=pair_context["assignment_label"],
                assignment_state=pair_context["assignment_state"],
                assignment_description=pair_context["assignment_description"],
                trading_type=pair_context["trading_type"],
                port=pair_context["port"],
                current_pair=pair_context["current_pair"],
                trading_type_label=pair_context["trading_type_label"],
                port_label=pair_context["port_label"],
            )
        return detail_text

    def _collect_dashboard_data(self, force_rescan: bool) -> dict[str, Any]:
        """대시보드 데이터 수집"""
        # 필요한 경우에만 디렉토리 구조 재스캔
        if force_rescan:
            self.monitor.find_target_programs()

        # 프로세스 상태 확인 (캐싱 적용됨)
        process_tuples = self.monitor.find_processes()

        running_by_dir = {}
        valid_process_count = 0

        for proc, target_path in process_tuples:
            info = self.monitor.get_process_info(proc)
            if info:
                dir_name = target_path.parent.name
                running_by_dir[dir_name] = (proc, target_path)
                valid_process_count += 1

        # Z-Flow 프로세스 상태 통합 (bridge runtime lookup 우선)
        slot_processes = _find_z_flow_processes(
            self.monitor, getattr(self.bot, "z_flow_bridge", None)
        )

        for proc, dir_name, data_dir in slot_processes:
            if proc is not None:
                running_by_dir[dir_name] = (proc, data_dir)
                valid_process_count += 1

        # 모든 대상 디렉토리 목록 (일반 봇 + Z-Flow 런타임 대상 통합)
        all_target_dirs = set(target_path.parent.name for target_path in self.monitor.all_program_paths)
        all_target_dirs.update(
            slot_dir.name for slot_dir in self._list_z_flow_runtime_targets()
        )
        all_target_dirs_sorted = sorted(all_target_dirs)

        # 무시 목록 갱신
        self.monitor.ignore_list = load_ignored_dirs()

        auto_assignment_on_count = 0
        auto_assignment_off_count = 0
        slot_management_on_count = 0
        slot_management_off_count = 0

        # capability gate 1회 평가 (bridge 없거나 비활성 → False)
        _bridge = getattr(self.bot, "z_flow_bridge", None)
        pair_trading_ui_enabled = _bridge is not None and _bridge.is_pair_trading_ui_enabled()

        # 디렉토리 분류
        active_dirs = []
        ignored_dirs_list = []
        for dir_name in all_target_dirs_sorted:
            if dir_name in self.monitor.ignore_list:
                ignored_dirs_list.append(dir_name)
            else:
                active_dirs.append(dir_name)
                is_slot = self._is_slot_target(dir_name)
                rotation_on = self._is_rotation_enabled(dir_name) if pair_trading_ui_enabled else False
                if is_slot:
                    if rotation_on:
                        slot_management_on_count += 1
                    else:
                        slot_management_off_count += 1
                else:
                    if rotation_on:
                        auto_assignment_on_count += 1
                    else:
                        auto_assignment_off_count += 1

        total_target_count = len(active_dirs)

        # 시스템 리소스 정보
        # CPU 블로킹 제거: interval=0.1 (100ms 블로킹) → interval=None (즉시 반환)
        # 주의: interval=None은 이전 호출 이후 누적 값 반환, 봇 시작 시 초기화 필요
        total_cpu = psutil.cpu_percent(interval=None)
        total_mem = psutil.virtual_memory().percent

        # 상태 아이콘 결정
        status_icon = "✅" if (
            valid_process_count == total_target_count and
            total_target_count > 0
        ) else "⚠️"
        if not all_target_dirs_sorted:
            status_icon = "🤷"

        return {
            'running_by_dir': running_by_dir,
            'valid_process_count': valid_process_count,
            'total_target_count': total_target_count,
            'total_cpu': total_cpu,
            'total_mem': total_mem,
            'status_icon': status_icon,
            'auto_assignment_on_count': auto_assignment_on_count,
            'auto_assignment_off_count': auto_assignment_off_count,
            'slot_management_on_count': slot_management_on_count,
            'slot_management_off_count': slot_management_off_count,
            'active_dirs': active_dirs,
            'ignored_dirs': ignored_dirs_list,
            'pair_trading_ui_enabled': pair_trading_ui_enabled,
        }

    def _build_dashboard_ui(self, data: dict[str, Any]) -> tuple[str, list[list[InlineKeyboardButton]]]:
        """대시보드 UI 구성 (요약 텍스트 + 키보드)"""
        # 요약 텍스트는 format_dashboard_summary에서 일관되게 생성한다.
        summary_text = format_dashboard_summary(
            valid_count=data['valid_process_count'],
            total_count=data.get('total_target_count', len(self.monitor.target_paths)),
            ignored_count=len(self.monitor.ignore_list),
            cpu_percent=data['total_cpu'],
            memory_percent=data['total_mem'],
            status_icon=data['status_icon'],
            auto_assignment_on_count=data.get('auto_assignment_on_count', 0),
            auto_assignment_off_count=data.get('auto_assignment_off_count', 0),
            slot_management_on_count=data.get('slot_management_on_count', 0),
            slot_management_off_count=data.get('slot_management_off_count', 0),
            show_auto_assignment=data.get('pair_trading_ui_enabled', False),
        )

        # 키보드 생성
        keyboard = []

        # 슬롯 live phase를 1회 batch read (bridge가 없으면 빈 dict)
        phase_map: dict = {}
        bridge = getattr(self.bot, "z_flow_bridge", None)
        if bridge is not None:
            try:
                phase_map = bridge.get_all_slot_live_phases()
            except Exception:
                phase_map = {}

        # 전체 봇 목록 (활성 + 무시) 통합 표시
        all_dirs_sorted = sorted(set(data['active_dirs'] + data['ignored_dirs']))

        for dir_name in all_dirs_sorted:
            row = []
            is_ignored = dir_name in data['ignored_dirs']

            if is_ignored:
                # 무시된 봇: 회색 아이콘 + 상세 진입
                status_label = f"⚪ {dir_name}"
                row.append(InlineKeyboardButton(status_label, callback_data=f"detail:{dir_name}"))
            elif dir_name in data['running_by_dir']:
                # 실행 중인 활성 봇
                _, target_path = data['running_by_dir'][dir_name]
                dir_path = target_path.parent
                is_slot = self._is_slot_target(dir_name, dir_path)

                # Entry Count 정보 가져오기
                trading_type, trading_limit_count = get_trading_info_from_env(dir_path)
                entry_count = None if is_slot else get_entry_count_generic(dir_path, trading_type)

                status_label = f"🟢 {dir_name}{self._get_list_badge(dir_name, True, is_slot)}"
                if trading_limit_count is not None and entry_count is not None:
                    status_label += f" ({entry_count}/{trading_limit_count})"
                elif entry_count is not None:
                    status_label += f" ({entry_count})"

                row.append(InlineKeyboardButton(status_label, callback_data=f"detail:{dir_name}"))
                row.append(self._get_action_button(dir_name, dir_path, is_slot, data['running_by_dir'][dir_name][0], phase_map=phase_map))
            else:
                # 실행 안 된 활성 봇
                is_slot = self._is_slot_target(dir_name)
                dir_path = self._find_dir_path(dir_name)
                status_label = f"🔴 {dir_name}{self._get_list_badge(dir_name, False, is_slot)}"
                row.append(InlineKeyboardButton(status_label, callback_data=f"detail:{dir_name}"))
                row.append(self._get_action_button(dir_name, dir_path, is_slot, None, phase_map=phase_map))

            keyboard.append(row)

        # 제어 버튼
        keyboard.append([
            InlineKeyboardButton("🔥 재시작(전체)", callback_data="restart_all_confirm"),
            InlineKeyboardButton("▶️ 재시작(실행중)", callback_data="restart_running_only")
        ])
        keyboard.append([
            InlineKeyboardButton("📜 운영봇 로그(전체)", callback_data="mainlog"),
            InlineKeyboardButton("📄 운영봇 로그(100줄)", callback_data="mainlog_tail")
        ])
        keyboard.append([
            InlineKeyboardButton("🔄 새로고침", callback_data="refresh_dashboard")
        ])

        return summary_text, keyboard

    async def _send_dashboard_message(self, query: Optional[CallbackQuery], update: Optional[Update], summary_text: str, keyboard: list[list[InlineKeyboardButton]]):
        """대시보드 메시지 전송 또는 수정"""
        # 대시보드 메인 화면으로 상태 업데이트
        self._current_view_state = "dashboard"

        reply_markup = InlineKeyboardMarkup(keyboard)

        # 메시지 업데이트 (내용 변경 시에만)
        if query and query.message:
            current_message = query.message
            current_text = getattr(current_message, 'text', None)
            current_markup = getattr(current_message, 'reply_markup', None)

            if current_text == summary_text and current_markup == reply_markup:
                logger.debug("[DASHBOARD] 메시지 변경 없음: edit 생략")
                return

            try:
                await get_telegram_gateway().edit_message_text(
                    query,
                    text=summary_text,
                    reply_markup=reply_markup,
                    parse_mode='MarkdownV2',
                    priority=TelegramPriority.DASHBOARD,
                    timeout=INTERACTIVE_DASHBOARD_TIMEOUT,
                )
            except Exception as exc:
                if not is_transient_telegram_error(exc):
                    raise
                logger.warning(
                    "[DASHBOARD][EDIT_TIMEOUT] chat_id=%s message_id=%s timeout=%.1fs error=%s -> fallback_send",
                    getattr(current_message, "chat_id", None),
                    getattr(current_message, "message_id", None),
                    INTERACTIVE_DASHBOARD_TIMEOUT,
                    type(exc).__name__,
                )
                await self._send_dashboard_fallback(
                    current_message=current_message,
                    summary_text=summary_text,
                    reply_markup=reply_markup,
                    reason=f"edit_{type(exc).__name__.lower()}",
                )
                return

            # 마지막 대시보드 메시지 추적 업데이트
            self._last_dashboard_chat_id = getattr(current_message, 'chat_id', None)
            self._last_dashboard_message_id = getattr(current_message, 'message_id', None)

        elif update and update.message:
            chat_id = update.message.chat_id

            # 기존 대시보드 메시지가 있으면 삭제
            await self._delete_previous_dashboard(chat_id)

            # 새 대시보드 메시지 전송
            sent_message = await get_telegram_gateway().enqueue(
                lambda: update.message.reply_text(
                    summary_text,
                    reply_markup=reply_markup,
                    parse_mode='MarkdownV2'
                ),
                priority=TelegramPriority.DASHBOARD,
                timeout=DASHBOARD_TIMEOUT,
                label="dashboard_reply_text",
            )

            # 마지막 대시보드 메시지 추적
            self._last_dashboard_chat_id = chat_id
            self._last_dashboard_message_id = sent_message.message_id

    async def _send_dashboard_fallback(
        self,
        *,
        current_message: Any,
        summary_text: str,
        reply_markup: InlineKeyboardMarkup,
        reason: str,
    ) -> None:
        chat_id = getattr(current_message, "chat_id", None)
        if chat_id is None:
            logger.warning("[DASHBOARD][FALLBACK_SEND_SKIP] reason=%s chat_id=None", reason)
            return

        logger.info(
            "[DASHBOARD][FALLBACK_SEND_START] reason=%s chat_id=%s",
            reason,
            chat_id,
        )
        sent_message = await get_telegram_gateway().send_message(
            self.bot.application.bot,
            chat_id=chat_id,
            text=summary_text,
            reply_markup=reply_markup,
            parse_mode='MarkdownV2',
            priority=TelegramPriority.DASHBOARD,
            timeout=DASHBOARD_TIMEOUT,
        )
        if sent_message is None:
            logger.warning("[DASHBOARD][FALLBACK_SEND_EMPTY] reason=%s chat_id=%s", reason, chat_id)
            return

        self._last_dashboard_chat_id = chat_id
        self._last_dashboard_message_id = sent_message.message_id
        logger.info(
            "[DASHBOARD][FALLBACK_SEND_DONE] reason=%s chat_id=%s message_id=%s",
            reason,
            chat_id,
            sent_message.message_id,
        )

    async def _delete_previous_dashboard(self, chat_id: int):
        """이전 대시보드 메시지 삭제"""
        if (self._last_dashboard_message_id and
            self._last_dashboard_chat_id == chat_id):
            try:
                await get_telegram_gateway().delete_message(
                    self.bot.application.bot,
                    chat_id=self._last_dashboard_chat_id,
                    message_id=self._last_dashboard_message_id
                )
                logger.debug(f"이전 대시보드 메시지 삭제됨: {self._last_dashboard_message_id}")
            except TelegramError as e:
                # 메시지가 이미 삭제되었거나 찾을 수 없는 경우 무시
                logger.debug(f"이전 대시보드 삭제 실패 (이미 삭제됨): {e}")

    async def _edit_query_message_with_retry(
        self,
        query: CallbackQuery,
        text: str,
        *,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        parse_mode: Optional[str] = "MarkdownV2",
        max_attempts: int = 3,
        base_delay: float = 0.35,
    ) -> Any:
        attempts = max(1, int(max_attempts))
        for attempt in range(attempts):
            try:
                return await query.edit_message_text(
                    text,
                    reply_markup=reply_markup,
                    parse_mode=parse_mode,
                )
            except Exception as exc:
                if not is_transient_telegram_error(exc):
                    raise
                if attempt >= attempts - 1:
                    raise
                delay = base_delay * (2**attempt)
                logger.warning(
                    "[DETAIL][EDIT_RETRY] target=%s attempt=%d/%d delay=%.2fs error=%s",
                    getattr(query, "data", None),
                    attempt + 1,
                    attempts,
                    delay,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)

    async def show_process_detail(self, query: CallbackQuery, target: str):
        """프로세스 상세 정보 표시"""
        # 상세 정보 화면 진입 시 상태 업데이트
        self._current_view_state = f"detail:{target}"

        try:
            proc, target_path, is_ignored = None, None, target in load_ignored_dirs()
            is_slot = self._is_slot_target(target)

            if not is_ignored:
                if is_slot:
                    for slot_proc, dir_name, data_dir in _find_z_flow_processes(
                        self.monitor, getattr(self.bot, "z_flow_bridge", None)
                    ):
                        if dir_name == target:
                            proc = slot_proc
                            target_path = data_dir
                            break
                else:
                    process_pairs = await asyncio.to_thread(self.monitor.find_processes)
                    for process, path in process_pairs:
                        if path.parent.name == target:
                            proc, target_path = process, path
                            break

            keyboard = []
            pair_context = None
            if not is_slot and not is_ignored:
                pair_context = await asyncio.to_thread(
                    self._resolve_pair_detail_context,
                    target,
                    target_path,
                    proc,
                )

            # Ignore 상태가 아닐 때 표시될 버튼들 (Z-Flow/외부봇 공통 레이아웃)
            if not is_ignored:
                # 1행: 설정 변경, 키워드 알림 (공통)
                keyboard.append([
                    InlineKeyboardButton("⚙️ 설정 변경", callback_data=f"edit_settings:{target}"),
                    InlineKeyboardButton("🔔 키워드 알림 설정", callback_data=f"keyword_menu:{target}")
                ])

                # 2행: 재시작 액션 (공통)
                keyboard.append([
                    InlineKeyboardButton("✨ 재시작(DB삭제)", callback_data=f"clean_run:{target}"),
                    InlineKeyboardButton("🔄 재시작(DB유지)", callback_data=f"confirm_simple_restart:{target}")
                ])

                # 4행: 봇 로그 전체, 봇 로그 100줄 (공통)
                keyboard.append([
                    InlineKeyboardButton("📜 로그(전체)", callback_data=f"log:{target}"),
                    InlineKeyboardButton("📄 로그(100줄)", callback_data=f"log_tail:{target}")
                ])
            else:
                pass  # Ignore 상태: 버튼 없음

            # [NEW] 페어 트레이딩 봇: 페어 로테이션 ON/OFF 버튼
            if not is_ignored:
                bridge = getattr(self.bot, "z_flow_bridge", None)
                pt_ui_enabled = bridge is not None and bridge.is_pair_trading_ui_enabled()
                if is_slot and pt_ui_enabled:
                    rotation_on = self._is_rotation_enabled(target)
                    slot_type = self._get_rotation_slot_type(target) if rotation_on else None
                    if rotation_on:
                        slot_label = "BTC/ETH" if slot_type == "BTC_ETH" else "기타"
                        rotation_text = f"🤖 자동 배정 ON ({slot_label})"
                    else:
                        rotation_text = "⏸️ 자동 배정 OFF"
                    keyboard.append([
                        InlineKeyboardButton(rotation_text, callback_data=f"toggle_rotation:{target}")
                    ])
                elif pair_context is not None:
                    slot_label = "BTC/ETH" if pair_context["slot_type"] == "BTC_ETH" else "기타"
                    rotation_text = (
                        f"🤖 자동 배정 ON · {slot_label}"
                        if pair_context["rotation_on"]
                        else "⏸️ 자동 배정 OFF"
                    )
                    rotation_row = [
                        InlineKeyboardButton(rotation_text, callback_data=f"toggle_rotation:{target}")
                    ]
                    if (
                        pair_context["rotation_on"]
                        and proc is None
                        and pair_context["resolved_state"] in (PairBotState.BLOCKED, PairBotState.MANUAL_STOP)
                    ):
                        rotation_row.append(
                            InlineKeyboardButton("⚡ 자동 배정 재개", callback_data=f"force_assign:{target}")
                        )
                    keyboard.append(rotation_row)

            # 5행: 뒤로가기 버튼 (항상 표시)
            keyboard.append([InlineKeyboardButton("🔙 돌아가기", callback_data="refresh_dashboard")])

            detail_started_at = time.monotonic()
            detail_text = await asyncio.to_thread(
                self._build_detail_text,
                target,
                is_ignored,
                is_slot,
                proc,
                target_path,
                pair_context,
            )
            detail_elapsed = time.monotonic() - detail_started_at
            if detail_elapsed >= 1.0:
                logger.warning(
                    "[DETAIL][SLOW][BUILD] target=%s elapsed=%.2fs",
                    target,
                    detail_elapsed,
                )
            
            await self._edit_query_message_with_retry(
                query,
                detail_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="MarkdownV2",
            )
            await get_telegram_gateway().answer_callback_query(query)

        except Exception as e:
            if "Message is not modified" in str(e):
                logger.debug(f"프로세스 상세 정보: 변경 없음 (무시)")
                return
            logger.error(f"프로세스 상세 정보 표시 중 오류: {e}")
            try:
                await self._edit_query_message_with_retry(
                    query,
                    format_error_message(e, "상세 정보 조회"),
                    parse_mode=None,
                    max_attempts=2,
                    base_delay=0.25,
                )
            except Exception as edit_error:
                logger.debug(f"프로세스 상세 오류 메시지 전송 실패: {edit_error}")

    async def send_escape_termination_dashboard(self, terminated_dirs, authorized_chat_id):
        """Escape 모드에 의해 종료된 프로세스가 있을 때 알림 전송"""
        try:
            if not authorized_chat_id:
                return
            
            # 내부적으로 데이터 수집 및 UI 구성 재사용
            dashboard_data = await asyncio.to_thread(self._collect_dashboard_data, True)
            summary_text, _ = self._build_dashboard_ui(dashboard_data) # 키보드는 아래에서 새로 만듦? 아니면 재사용?
            # 기존 로직은 키보드도 포함함.
            
            # 여기서 키보드 재생성 (기존 로직 따름)
            _, keyboard = self._build_dashboard_ui(dashboard_data)
            reply_markup = InlineKeyboardMarkup(keyboard)

            esc_dirs = [escape_markdown(d) for d in terminated_dirs]
            joined_dirs = '`, `'.join(esc_dirs)
            notification_text = (f"🚨 *Escape 모드 자동 종료 알림*\n\n"
                                 f"다음 프로세스가 종료되어 Escape 모드가 자동으로 OFF 처리되었습니다:\n"
                                 f" \\- `{joined_dirs}`\n\n{escape_markdown('-'*20)}\n\n")

            # safe_send_message_with_result 사용
            await get_telegram_gateway().send_message(
                self.bot.application.bot,
                chat_id=authorized_chat_id,
                text=notification_text + summary_text,
                reply_markup=reply_markup,
                parse_mode='MarkdownV2',
                priority=TelegramPriority.DASHBOARD,
                timeout=DASHBOARD_TIMEOUT,
            )
        except Exception as e:
            logger.error(f"Escape 종료 대시보드 전송 실패: {e}")
