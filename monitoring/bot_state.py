from __future__ import annotations

"""
bot_state — 봇 상태 파일 유틸리티

pair_automation.py(z_flow)에서 이관된 봇 종료 상태 확인 함수들.
"""

from collections import deque
from enum import Enum
from pathlib import Path

from z_pulse.constants import FileConfig
from z_pulse.features.process_control import read_bot_state
from z_pulse.integration.strategy_registry import is_pair_trading_type as _is_pair_trading_type
from z_pulse.monitoring.session_store import SessionRef, SessionStore


class PairBotState(str, Enum):
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    WAITING_WITH_WARNING = "WAITING_WITH_WARNING"
    MANUAL_STOP = "MANUAL_STOP"
    BLOCKED = "BLOCKED"


def is_pair_trading_type(trading_type: str | None) -> bool:
    """주어진 trading_type이 페어 트레이딩 대상인지 확인."""
    return _is_pair_trading_type(trading_type)


def _load_session_ref(dir_path: Path) -> SessionRef | None:
    return SessionStore(dir_path).load()


def _has_recent_execution_trace(session: SessionRef | None) -> bool:
    if session is None:
        return False
    return bool(session.last_running_at or session.last_exit_at)


def _has_normal_exit_evidence(session: SessionRef | None) -> bool:
    if session is None:
        return False

    if session.last_state_signal in {"EXIT_RESERVATION", "WAITING", "WAITING_WITH_WARNING"}:
        return True

    # 비정상 종료 신호가 명시된 경우 evidence.normal_exit_logged 무시
    if session.last_state_signal == "PROCESS_EXIT":
        return False

    evidence = session.evidence or {}
    return bool(evidence.get("normal_exit_logged"))


def _has_manual_stop_evidence(session: SessionRef | None) -> bool:
    if session is None:
        return False
    if session.last_state_signal == "MANUAL_STOP" or session.last_exit_reason == "manual_stop":
        return True
    evidence = session.evidence or {}
    return bool(evidence.get("manual_stop"))


def _has_previous_run_logs(dir_path: Path) -> bool:
    log_files = [dir_path / FileConfig.MONITOR_LOG]
    if dir_path.name.upper().startswith("SLOT-"):
        log_files.insert(0, dir_path / "slot.log")
    return any(log_file.exists() for log_file in log_files)


def _has_recent_exit_reservation_log(dir_path: Path) -> bool:
    log_file = dir_path / FileConfig.MONITOR_LOG
    if not log_file.exists():
        return False

    try:
        with open(log_file, "r", encoding="utf-8", errors="ignore") as handle:
            lines = list(deque(handle, 50))
    except OSError:
        return False

    return any("EXIT_RESERVATION" in line for line in lines)


def resolve_pair_bot_state(
    dir_path: Path,
    process_running: bool,
    session: SessionRef | None,
    state_value: str | None = None,
) -> PairBotState:
    if process_running:
        return PairBotState.RUNNING

    state = state_value if state_value is not None else read_bot_state(dir_path)

    if state == "MANUAL_STOP":
        return PairBotState.MANUAL_STOP

    if _has_manual_stop_evidence(session):
        return PairBotState.MANUAL_STOP

    if state in {"WAITING", "EXIT_RESERVATION"}:
        return PairBotState.WAITING

    # 수정 3: 실행 의도 미완 세션 → BLOCKED (P1 차단)
    # begin_runtime_identity 시 identity_generation=<값> 설정,
    # 종료 완료(clear_runtime_identity) 시 None 초기화.
    # 값이 남아있으면 "재시작 중 or running-후-사망 경쟁창" — 신규 할당 차단.
    if session is not None and session.identity_generation is not None:
        return PairBotState.BLOCKED

    if _has_recent_execution_trace(session):
        # 수정 4: PROCESS_EXIT 확정 시 monitor.log fallback(옛 EXIT_RESERVATION 로그) 무시 (P2 차단)
        # state유실 fallback(_has_recent_exit_reservation_log)은 PROCESS_EXIT가 아닐 때만 동작.
        has_log_fallback = (
            False
            if (session is not None and session.last_state_signal == "PROCESS_EXIT")
            else _has_recent_exit_reservation_log(dir_path)
        )
        if _has_normal_exit_evidence(session) or has_log_fallback:
            return PairBotState.WAITING_WITH_WARNING
        return PairBotState.BLOCKED

    if not _has_previous_run_logs(dir_path):
        return PairBotState.WAITING

    return PairBotState.BLOCKED


def _check_normal_exit(dir_path: Path) -> bool:
    """봇 state/session 기준 정상 종료 여부 확인.

    정상 종료 상태:
    - WAITING: 신규 할당 대기
    - WAITING_WITH_WARNING: state 유실이 의심되지만 정상 종료 근거 존재
    - MANUAL_STOP: 대시보드 수동 종료
    """
    state = resolve_pair_bot_state(
        dir_path,
        process_running=False,
        session=_load_session_ref(dir_path),
    )
    return state in {
        PairBotState.WAITING,
        PairBotState.WAITING_WITH_WARNING,
        PairBotState.MANUAL_STOP,
    }


def _check_manual_stop(dir_path: Path) -> bool:
    """봇 state/session 기준 MANUAL_STOP 여부 확인.

    수동 종료(대시보드 '종료' 버튼)된 봇은 정상 종료이지만
    재할당 대상이 아님.
    """
    state = resolve_pair_bot_state(
        dir_path,
        process_running=False,
        session=_load_session_ref(dir_path),
    )
    return state is PairBotState.MANUAL_STOP
