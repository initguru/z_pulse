from __future__ import annotations

"""
ZFlowBridge — z_flow 연동 레이어

파일시스템 IPC(공유 JSON 파일)만 사용하여 z_flow와 통신.
z_flow 패키지를 Python import하지 않고 독립 동작한다.
Z_FLOW_PATH 미설정 시 모든 연동 기능이 graceful하게 비활성화된다.
"""

import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, cast

import psutil

from .telegram_extensions import ZFlowTelegramExtension
from z_pulse.config.env_handler import EnvConfigHandler
from z_pulse.config.paths import REPO_ROOT
from z_pulse.config.runtime_settings import runtime_settings
from z_pulse.monitoring.bot_state import PairBotState, is_pair_trading_type, resolve_pair_bot_state
from z_pulse.monitoring.session_store import SessionStore
from z_pulse.features.process_control import write_bot_state

logger = logging.getLogger(__name__)


class PairTradingConfigError(RuntimeError):
    """pair_trading 전략 설정 파일이 없거나 필수 키가 누락된 경우."""


class ZFlowBridgeError(RuntimeError):
    """Raised when ZFlowBridge cannot complete a requested operation."""


def _read_z_flow_pid_file(pid_file: Path) -> int | None:
    """z_flow.pid 파일에서 PID를 읽어 반환한다. 파일 없거나 파싱 실패 시 None."""
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def _get_market_data_provider_cls() -> type[Any] | None:
    """Z-Flow 선택 의존성인 MarketDataProvider class를 가능한 경우에만 반환한다."""
    try:
        from z_flow.data.market_data_provider import MarketDataProvider
    except ModuleNotFoundError as exc:
        missing_name = getattr(exc, "name", None)
        if missing_name and not missing_name.startswith("z_flow"):
            raise
        logger.debug("ZFlowBridge: MarketDataProvider import 불가 — source 라우팅 비활성화")
        return None
    return MarketDataProvider


# pair_trading 전략 설정 파일 경로 (z_flow 파일시스템이 존재하면 접근 가능)
_PAIR_TRADING_ENV_PATH = REPO_ROOT / "z_flow" / "strategy" / "pair_trading" / "setting.env"

_PAIR_TRADING_REQUIRED_KEYS = [
    "BTC_ETH_ENTRY_TRIGGER_PERCENT",
    "BTC_ETH_CLOSE_TRIGGER_PERCENT",
    "BTC_ETH_TRADING_LIMIT_COUNT",
    "BTC_ETH_Z_ENTRY_THRESHOLD",
    "BTC_ETH_ROUND_TRIP_COST",
]


# ---------------------------------------------------------------------------
# SlotLivePhase — slot bot 생애주기 phase (ZFlowBridge 경계 내 전용)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SlotLivePhase:
    """Slot bot의 현재 생애주기 phase 스냅샷.

    phase: "PRE_ENTRY" | "IN_POSITION" | "UNKNOWN"
    current_level: 보유 레벨 수 (0이면 포지션 없음)
    unrealized_pnl: 표시 전용 float (계산 없음)
    is_fresh: last_heartbeat age < SLOT_HEARTBEAT_STALE_THRESHOLD_SEC
    """

    phase: str
    current_level: int
    unrealized_pnl: float
    is_fresh: bool


# heartbeat interval: HEARTBEAT_INTERVAL_SEC=5s (z_flow/core/slot_runtime.py:92)
SLOT_HEARTBEAT_STALE_THRESHOLD_SEC = 30

# PRE_ENTRY 상태 집합 — z_flow/core/state_machine.py ZFlowState 정본
_SLOT_PRE_ENTRY_STATES: frozenset[str] = frozenset({"IDLE", "ASSIGNED"})


def _classify_slot_phase(
    state: str, current_level: int, last_heartbeat_str: str
) -> tuple[str, bool]:
    """slot_heartbeat 행 값으로 (phase, is_fresh) 튜플을 반환한다 (bridge 내부 전용)."""
    # phase 분류
    if state in _SLOT_PRE_ENTRY_STATES and current_level == 0:
        phase = "PRE_ENTRY"
    elif state in {"ENTERING", "RUNNING", "DCA_ENTERING", "CLOSING", "LOCKED", "ERROR_RECOVERY"}:
        phase = "IN_POSITION"
    elif current_level > 0:
        phase = "IN_POSITION"  # 안전판: level>0은 항상 포지션 보유
    else:
        phase = "UNKNOWN"

    # freshness
    try:
        hb_dt = datetime.fromisoformat(last_heartbeat_str)
        if hb_dt.tzinfo is None:
            hb_dt = hb_dt.replace(tzinfo=timezone.utc)
        else:
            hb_dt = hb_dt.astimezone(timezone.utc)
        age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
        is_fresh = age < SLOT_HEARTBEAT_STALE_THRESHOLD_SEC
    except Exception:
        is_fresh = False

    return phase, is_fresh


class ZFlowBridge:
    """z_flow 파일시스템 IPC 클라이언트.

    연동 활성화 조건:
    - Z_FLOW_PATH 환경변수가 설정된 경우
    - 해당 경로가 실제로 존재하는 경우

    비활성화 시 모든 메서드는 False를 반환하고 조용히 무시된다.
    """

    _pair_manager: Any | None
    _pair_managers: dict[str, Any]
    _integration_active: bool = False

    @classmethod
    def from_env(cls) -> "ZFlowBridge":
        return cls(os.getenv("Z_FLOW_PATH"))

    def __init__(self, z_flow_path: str | None):
        self._path: Path | None = None
        self._enabled = False
        self._pair_manager = None
        self._pair_managers = {}
        self._pair_scheduler: Any | None = None  # PairTradingSchedulerMixin 호스트 (lazy init)
        self._market_data_thread: threading.Thread | None = None
        self._market_data_coordinator = None

        if z_flow_path:
            candidate = Path(z_flow_path)
            if candidate.exists():
                self._path = candidate
                self._enabled = True
                logger.info(f"ZFlowBridge 활성화: {self._path}")
            else:
                logger.warning(f"ZFlowBridge: Z_FLOW_PATH 경로 없음 — 연동 비활성화 ({z_flow_path})")
        else:
            logger.info("ZFlowBridge: Z_FLOW_PATH 미설정 — 연동 비활성화")
        if self._enabled:
            type(self)._integration_active = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def get_pair_manager(self, bot=None):
        """ZFlowBridge가 보유한 PairTradingManager 인스턴스를 반환한다.

        bot이 제공된 경우:
            bot의 slot_type(TRADING_TYPE)으로 소스를 결정하여 _pair_managers에서 반환.
        bot=None + _pair_managers 1개:
            그 유일한 manager를 반환 (하위호환).
        bot=None + _pair_managers 2개 이상:
            ValueError 발생 (silent fallback 금지).
        _pair_managers가 비어있을 때:
            _pair_manager로 fallback (기존 init_pair_manager 하위호환).
        """
        if self._pair_managers:
            if bot is not None:
                # bot은 메타데이터 dict (slot_type 키 보유) 또는 trading_type 속성 보유 객체
                if isinstance(bot, dict):
                    trading_type = str(bot.get("slot_type") or "").strip()
                else:
                    trading_type = str(
                        getattr(bot, "trading_type", None)
                        or getattr(bot, "slot_type", None)
                        or ""
                    ).strip()

                if not trading_type:
                    raise ValueError(
                        f"ZFlowBridge.get_pair_manager: bot에서 trading_type을 결정할 수 없습니다. "
                        f"bot={bot!r}"
                    )

                from z_flow.data.market_data_provider import MarketDataProvider

                provider = MarketDataProvider.for_trading_type(trading_type)
                data_source = provider.data_source

                if data_source not in self._pair_managers:
                    raise ValueError(
                        f"ZFlowBridge.get_pair_manager: data_source={data_source!r}에 해당하는 "
                        f"manager가 없습니다. 초기화된 소스: {list(self._pair_managers)}"
                    )
                return self._pair_managers[data_source]

            # bot=None
            if len(self._pair_managers) == 1:
                return next(iter(self._pair_managers.values()))

            raise ValueError(
                f"ZFlowBridge.get_pair_manager: bot 인수 없이 다중 manager에 접근할 수 없습니다. "
                f"초기화된 소스: {list(self._pair_managers)}. bot을 지정하세요."
            )

        # _pair_managers 비어있음 → 기존 _pair_manager fallback (하위호환)
        return self._pair_manager

    def get_pair_managers(self) -> dict:
        """소스 키별 PairTradingManager dict의 복사본을 반환한다.

        Returns:
            dict[str, PairTradingManager] — 초기화 전이면 빈 dict.
        """
        return dict(self._pair_managers)

    def get_pair_manager_snapshot(self) -> dict:
        """모든 manager의 aggregate 정보를 반환 (Telegram/UI용).

        Returns:
            {
                "sources": list[str],           # 초기화된 data_source 목록
                "managers": dict[str, object],  # source → manager 객체
            }
            초기화 전이면 빈 dict 반환.
        """
        if not self._pair_managers:
            return {}

        return {
            "sources": list(self._pair_managers.keys()),
            "managers": dict(self._pair_managers),
        }

    def init_pair_managers(
        self,
        symbols: list[str],
        trading_types: list[str],
    ) -> bool:
        """trading_types별로 MarketDataProvider를 통해 PairTradingManager를 소스별로 생성한다.

        같은 data_source에 해당하는 여러 trading_type은 하나의 manager를 공유한다.
        생성된 manager는 _pair_managers[data_source]에 저장된다.
        하위호환을 위해 _pair_manager도 grvt 우선 또는 첫 번째 manager로 설정된다.

        Args:
            symbols: 거래 심볼 리스트
            trading_types: 거래 유형 목록 (예: ["GRVT_PAIR", "VARIATIONAL_PAIR"])

        Returns:
            bool — 하나 이상의 manager가 생성되면 True, 아니면 False.
        """
        if not self._enabled:
            return False

        if not self.is_pair_trading_enabled():
            logger.info("ZFlowBridge: 페어 트레이딩 비활성 — init_pair_managers 생략")
            return False

        if not symbols or len(symbols) < 2:
            logger.warning(
                f"ZFlowBridge: 심볼 부족 ({len(symbols)}개) — init_pair_managers 생략"
            )
            return False

        from z_flow.data.market_data_provider import MarketDataProvider
        from z_flow.strategy.pair_trading.manager import PairTradingManager

        # trading_type → data_source 그룹핑 (중복 소스는 하나의 manager만 생성)
        source_to_provider: dict[str, MarketDataProvider] = {}
        for tt in trading_types:
            try:
                provider = MarketDataProvider.for_trading_type(tt)
                source = provider.data_source
                if source not in source_to_provider:
                    source_to_provider[source] = provider
            except (ValueError, KeyError) as e:
                logger.warning(f"ZFlowBridge: trading_type={tt!r} 처리 실패, 건너뜀: {e}")

        if not source_to_provider:
            logger.warning("ZFlowBridge: 유효한 data_source가 없어 init_pair_managers 중단")
            return False

        new_managers: dict = {}
        canonical_state_dir = self.get_pair_trading_state_dir()
        for source, provider in source_to_provider.items():
            kwargs = provider.build_pair_manager_kwargs()
            kwargs["cache_dir"] = canonical_state_dir
            try:
                mgr = PairTradingManager(
                    symbols=symbols,
                    data_dir=kwargs["data_dir"],
                    cache_dir=kwargs["cache_dir"],
                )
                new_managers[source] = mgr
                logger.info(
                    f"ZFlowBridge: PairTradingManager 생성 완료 "
                    f"(source={source}, {len(symbols)}개 심볼)"
                )
            except Exception as e:
                logger.error(
                    f"ZFlowBridge: PairTradingManager 생성 실패 (source={source}): {e}"
                )

        if not new_managers:
            return False

        self._pair_managers = new_managers

        # 하위호환: grvt 우선, 없으면 첫 번째 manager를 _pair_manager에도 설정
        self._pair_manager = new_managers.get("grvt") or next(iter(new_managers.values()))

        # 각 manager의 rotation_enabled_bots를 해당 source 담당 봇으로만 필터링
        bots_by_src = self._bots_by_source()
        if bots_by_src:
            for _src, _mgr in self._pair_managers.items():
                _allowed = bots_by_src.get(_src, set())
                _mgr._allowed_rotation_bots = _allowed  # initialize() 재호출 시에도 필터 유지
                _mgr.rotation_enabled_bots &= _allowed
                _mgr.slot_types = {k: v for k, v in _mgr.slot_types.items() if k in _allowed}
                logger.info(
                    f"ZFlowBridge: [{_src}] rotation 필터링 완료 → "
                    f"{len(_mgr.rotation_enabled_bots)}개: {sorted(_mgr.rotation_enabled_bots)}"
                )

        return True

    def init_pair_manager(self, symbols: list[str], data_dir: Path, cache_dir: Path | None = None) -> bool:
        """PairTradingManager를 조건부로 생성한다.

        z_flow 패키지가 import 가능한 경우에만 생성.
        외부봇 자동 배정 통제를 위해 Z-Pulse 프로세스 내에서 동작.
        """
        if not self._enabled:
            return False

        if not self.is_pair_trading_enabled():
            logger.info("ZFlowBridge: 페어 트레이딩 비활성 — PairTradingManager 생성 생략")
            return False

        if not symbols or len(symbols) < 2:
            logger.warning(f"ZFlowBridge: 심볼 부족 ({len(symbols)}개) — PairTradingManager 생성 생략")
            return False

        try:
            from z_flow.strategy.pair_trading.manager import PairTradingManager

            self._pair_manager = PairTradingManager(
                symbols=symbols,
                data_dir=data_dir,
                cache_dir=cache_dir or data_dir,
            )
            logger.info(
                f"ZFlowBridge: PairTradingManager 생성 완료 "
                f"({len(symbols)}개 심볼, data_dir={data_dir})"
            )
            return True
        except Exception as e:
            logger.error(f"ZFlowBridge: PairTradingManager 생성 실패: {e}")
            self._pair_manager = None
            return False

    def get_pair_trading_env_path(self) -> Path:
        return _PAIR_TRADING_ENV_PATH

    def is_pair_trading_enabled(self) -> bool:
        """pair_trading 전략 마스터 키 단일 주입점.

        Z_FLOW_PATH가 설정되지 않았거나(bridge 비활성) z_flow가 설치되지 않은
        환경에서는 즉시 False를 반환한다.  활성화 값은
        z_flow/strategy/pair_trading/setting.env 의 페어 트레이딩 마스터 키에서만 읽는다.
        """
        if not self._enabled:
            return False
        try:
            from z_flow.config.env_handler import is_pair_trading_enabled as _zf_ipe
            return _zf_ipe()
        except Exception:
            return False

    def get_pair_trading_setting_keys(self) -> dict[str, frozenset[str]]:
        """Return pair-trading strategy key taxonomy from z_flow.

        Graceful fallback: returns empty frozensets when z_flow is not installed
        or raises an exception. Callers must handle empty sets safely.
        """
        _empty: dict[str, frozenset[str]] = {
            "strategy": frozenset(),
            "applied": frozenset(),
            "rotation_protected": frozenset(),
        }
        if not self._enabled:
            return _empty
        try:
            from z_flow.config.env_handler import (  # pyright: ignore[reportMissingImports]
                get_pair_trading_setting_keys as _z_flow_keys,
            )
            return _z_flow_keys()
        except Exception as exc:
            logger.warning("get_pair_trading_setting_keys: z_flow unavailable (%s)", exc)
            return _empty

    def require_pair_trading_env(self, required_keys: list[str] | None = None) -> dict[str, str]:
        env_path = _PAIR_TRADING_ENV_PATH
        if not env_path.exists():
            raise PairTradingConfigError(
                f"pair_trading 설정 파일을 찾을 수 없습니다: {env_path}"
            )

        config = EnvConfigHandler.parse(env_path)
        keys = required_keys or _PAIR_TRADING_REQUIRED_KEYS
        missing = [key for key in keys if not str(config.get(key, "")).strip()]
        if missing:
            missing_text = ", ".join(missing)
            raise PairTradingConfigError(
                f"pair_trading 설정 누락: {missing_text} ({env_path})"
            )
        return config

    def get_pair_trading_config_error(self):
        return PairTradingConfigError

    @staticmethod
    def get_runtime_entry_targets() -> tuple[str, ...]:
        return ("z_flow/run_bot.py",)

    @staticmethod
    def matches_runtime_cmdline(cmdline: list[str]) -> bool:
        cmd_parts = [part.replace("\\", "/").lower() for part in cmdline if part]
        runtime_targets = tuple(target.lower() for target in ZFlowBridge.get_runtime_entry_targets())
        return any(
            any(part == target or part.endswith(f"/{target}") for target in runtime_targets)
            for part in cmd_parts
        )

    def is_runtime_cmdline(self, cmdline: list[str]) -> bool:
        return self.matches_runtime_cmdline(cmdline)

    @staticmethod
    def extract_runtime_data_dir(cmdline: list[str]) -> Path | None:
        for index, part in enumerate(cmdline):
            if not part:
                continue
            candidate: str | None = None
            if part in {"--slot-dir", "--data-dir"} and index + 1 < len(cmdline):
                candidate = cmdline[index + 1]
            elif part.startswith("--slot-dir=") or part.startswith("--data-dir="):
                candidate = part.split("=", 1)[1]
            if candidate:
                normalized = candidate.strip().strip('"').strip("'")
                if normalized:
                    return Path(normalized)
        return None

    @staticmethod
    def normalize_runtime_target_path(target: str | Path) -> str:
        return str(Path(target).expanduser().resolve(strict=False)).replace("\\", "/").lower()

    @classmethod
    def matches_runtime_target(cls, cmdline: list[str], target: str | Path) -> bool:
        data_dir = cls.extract_runtime_data_dir(cmdline)
        if data_dir is None:
            return False

        target_text = str(target).strip()
        if not target_text:
            return False

        if isinstance(target, Path) or "/" in target_text or "\\" in target_text:
            return cls.normalize_runtime_target_path(data_dir) == cls.normalize_runtime_target_path(target)
        return data_dir.name.lower() == target_text.lower()

    def get_runtime_root(self) -> Path:
        if self._path is not None:
            return self._path
        return REPO_ROOT / "z_flow"

    def get_market_data_dir(self, exchange_id: str = "") -> Path:
        runtime_root = self.get_runtime_root()
        exchange = exchange_id.strip().lower()
        if exchange:
            return runtime_root / "data" / "market" / exchange
        return runtime_root / "data" / "market"

    def get_market_duckdb_path(self, exchange_id: str = "grvt") -> Path:
        return self.get_market_data_dir(exchange_id) / "market_1m.duckdb"

    def start_market_data_coordinator(
        self,
        *,
        symbols: list[str],
        owner_kind: str = "z_pulse",
        use_testnet: bool = False,
    ) -> bool:
        """Start GRVT market data coordinator through the bridge boundary."""
        if runtime_settings.get_bool("Z_FLOW_MARKET_DATA_DAEMON_MODE", True):
            logger.info(
                "ZFlowBridge: Z_FLOW_MARKET_DATA_DAEMON_MODE=true — "
                "내부 market data coordinator 시작 생략"
            )
            return False
        if not self._enabled or self._market_data_thread is not None:
            return False
        if not symbols:
            return False
        try:
            import asyncio
            from z_flow.data.market_data_coordinator import MarketDataCoordinator

            owner_id = f"{owner_kind}:{os.getpid()}"
            coordinator = MarketDataCoordinator(
                control_db_path=Path(self.get_runtime_control_db_path()),
                owner_id=owner_id,
                owner_kind=owner_kind,
                symbols=symbols,
                csv_dir=self.get_market_data_dir("grvt"),
                duckdb_path=self.get_market_duckdb_path("grvt"),
                use_testnet=use_testnet,
            )
            self._market_data_coordinator = coordinator

            def _run() -> None:
                asyncio.run(coordinator.run())

            self._market_data_thread = threading.Thread(
                target=_run,
                name="z-flow-market-data-coordinator",
                daemon=True,
            )
            self._market_data_thread.start()
            logger.info("ZFlowBridge: GRVT market data coordinator started")
            return True
        except Exception as e:
            logger.error(f"ZFlowBridge: market data coordinator 시작 실패: {e}")
            self._market_data_coordinator = None
            self._market_data_thread = None
            return False

    def get_pair_trading_root(self) -> Path:
        return self.get_runtime_root() / "strategy" / "pair_trading"

    def get_pair_trading_state_dir(self) -> Path:
        return self.get_pair_trading_root() / "state"

    def get_bot_operations_db_path(self) -> Path:
        return self.get_pair_trading_state_dir() / "bot_operations.db"

    def get_runtime_python_path(self) -> str | None:
        explicit_python = runtime_settings.get_str("Z_FLOW_PYTHON", "").strip()
        if explicit_python:
            explicit_path = Path(explicit_python)
            if explicit_path.exists() and os.access(explicit_path, os.X_OK):
                return explicit_python

        explicit_venv = runtime_settings.get_str("Z_FLOW_VENV", "").strip()
        venv_root = Path(explicit_venv) if explicit_venv else self.get_runtime_root() / ".venv"
        if os.name == "nt":
            return str(venv_root / "Scripts" / "python.exe")

        python_path = venv_root / "bin" / "python"
        if python_path.exists() and os.access(python_path, os.X_OK):
            return str(python_path)

        python3_path = venv_root / "bin" / "python3"
        if python3_path.exists() and os.access(python3_path, os.X_OK):
            return str(python3_path)

        if not os.access(python_path, os.X_OK):
            logger.warning(
                "[BRIDGE][PYTHON_PATH] Python 경로가 실행 불가: %s — venv 재생성 필요", python_path
            )
            return None
        return str(python_path)

    def get_runtime_control_db_path(self) -> str:
        control_db = runtime_settings.get_str("Z_FLOW_CONTROL_DB", "").strip()
        if control_db:
            return control_db
        return str(self.get_runtime_root() / "strategy" / "db" / "pair_control.db")

    def list_runtime_targets(
        self,
        target_dir: Path,
        ignore_list: set[str],
    ) -> list[Path]:
        runtime_targets: list[Path] = []
        for candidate in sorted(target_dir.iterdir()):
            if not candidate.is_dir():
                continue
            if candidate.name.startswith("_") or candidate.name in ignore_list:
                continue
            if self.get_slot_runtime_metadata(candidate) is not None:
                runtime_targets.append(candidate)
        return runtime_targets

    def get_runtime_pid_files(
        self,
        target_dir: Path,
        ignore_list: set[str],
    ) -> dict[str, Path]:
        return {
            slot_dir.name: slot_dir / "z_flow.pid"
            for slot_dir in self.list_runtime_targets(target_dir, ignore_list)
        }

    def get_slot_runtime_metadata(self, data_dir: Path) -> dict[str, object] | None:
        env_path = data_dir / "setting.env"
        if not env_path.exists():
            return None

        parsed = EnvConfigHandler.parse(env_path)
        try:
            slot_id = int(str(parsed.get("SLOT_ID") or "").strip())
        except (TypeError, ValueError):
            return None
        if slot_id <= 0:
            return None

        exchange_id = str(parsed.get("EXCHANGE_ID") or "").strip().lower()
        if not exchange_id:
            from z_flow.strategy import get_exchange_for_trading_type
            exchange_id = str(
                get_exchange_for_trading_type(str(parsed.get("TRADING_TYPE") or "").strip())
                or ""
            ).strip().lower()
        if not exchange_id:
            return None

        alias = str(parsed.get("ALIAS") or data_dir.name)
        slot_type = str(parsed.get("TRADING_TYPE") or "")
        margin = str(parsed.get("TRADING_MARGIN") or "")
        use_testnet = str(parsed.get("USE_TESTNET") or "false").lower() == "true"
        return {
            "slot_id": slot_id,
            "exchange_id": exchange_id,
            "alias": alias,
            "data_dir": data_dir,
            "slot_type": slot_type,
            "margin": margin,
            "use_testnet": use_testnet,
        }

    def get_external_pair_runtime_metadata(self, data_dir: Path) -> dict[str, object] | None:
        """SLOT_ID 없는 외부 pair-trading 봇의 resume 검증용 metadata를 반환한다."""
        env_path = data_dir / "setting.env"
        if not env_path.exists():
            return None

        parsed = EnvConfigHandler.parse(env_path)
        slot_id_raw = str(parsed.get("SLOT_ID") or "").strip()
        if slot_id_raw:
            return None
        slot_type = str(parsed.get("TRADING_TYPE") or "").strip()
        if not is_pair_trading_type(slot_type):
            return None

        exchange_id = str(parsed.get("EXCHANGE_ID") or "").strip().lower()
        if not exchange_id:
            from z_flow.strategy import get_exchange_for_trading_type
            exchange_id = str(get_exchange_for_trading_type(slot_type) or "").strip().lower()
        alias = str(parsed.get("ALIAS") or data_dir.name)
        margin = str(parsed.get("TRADING_MARGIN") or "")
        use_testnet = str(parsed.get("USE_TESTNET") or "false").lower() == "true"
        return {
            "slot_id": None,
            "exchange_id": exchange_id,
            "alias": alias,
            "data_dir": data_dir,
            "slot_type": slot_type,
            "margin": margin,
            "use_testnet": use_testnet,
        }

    def is_runtime_data_dir(self, data_dir: Path | None) -> bool:
        if data_dir is None:
            return False
        return self.get_slot_runtime_metadata(data_dir) is not None

    def resolve_runtime_data_dir(
        self,
        target: str,
        monitor=None,
        *,
        target_dir: Path | None = None,
        ignore_list: set[str] | None = None,
    ) -> Path | None:
        if monitor is not None:
            z_flow_dirs = getattr(monitor, "z_flow_dirs", {})
            if isinstance(z_flow_dirs, dict):
                pid_file = z_flow_dirs.get(target)
                if pid_file is not None:
                    return pid_file.parent

            runtime_processes = self.find_runtime_processes(monitor)
            for _, dir_name, data_dir in runtime_processes:
                if dir_name == target:
                    return data_dir

            if target_dir is None:
                candidate_dir = getattr(monitor, "target_dir", None)
                if isinstance(candidate_dir, Path):
                    target_dir = candidate_dir
            if ignore_list is None:
                candidate_ignore_list = getattr(monitor, "ignore_list", None)
                if isinstance(candidate_ignore_list, set):
                    ignore_list = candidate_ignore_list

        if target_dir is None:
            return None

        for data_dir in self.list_runtime_targets(target_dir, ignore_list or set()):
            if data_dir.name == target:
                return data_dir
        session_data_dir = self._resolve_runtime_data_dir_from_sessions(
            target_dir,
            target,
        )
        if session_data_dir is not None:
            return session_data_dir
        return None

    def _resolve_runtime_data_dir_from_sessions(
        self,
        target_dir: Path,
        target: str,
    ) -> Path | None:
        """target_dir 주변 session.json에서 실제 Z-Flow data_dir를 복구한다."""
        candidates: list[tuple[int, Path]] = []
        try:
            session_paths = list(target_dir.glob("*/session.json"))
        except OSError:
            return None

        for session_path in session_paths:
            try:
                payload = json.loads(session_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if str(payload.get("dir_name") or "").strip() != target:
                continue
            if str(payload.get("runtime_kind") or "").strip() != "z_flow_runtime":
                continue

            raw_data_dir = str(payload.get("data_dir") or "").strip()
            session_dir = session_path.parent
            possible_dirs = []
            if raw_data_dir:
                possible_dirs.append(Path(raw_data_dir))
            possible_dirs.append(session_dir)

            status = str(payload.get("status") or "").strip().lower()
            status_rank = 0 if status == "running" else 10
            for data_dir in possible_dirs:
                if self.get_slot_runtime_metadata(data_dir) is None:
                    continue
                candidates.append((status_rank, data_dir))

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], str(item[1])))
        return candidates[0][1]

    def is_runtime_target(
        self,
        target: str,
        monitor=None,
        *,
        target_dir: Path | None = None,
        ignore_list: set[str] | None = None,
    ) -> bool:
        return (
            self.resolve_runtime_data_dir(
                target,
                monitor,
                target_dir=target_dir,
                ignore_list=ignore_list,
            )
            is not None
        )

    def find_runtime_processes(self, monitor) -> list[tuple[object | None, str, Path]]:
        z_flow_dirs = getattr(monitor, "z_flow_dirs", {})
        if isinstance(z_flow_dirs, dict) and z_flow_dirs:
            runtime_processes: list[tuple[object | None, str, Path]] = []
            for dir_name, pid_file in z_flow_dirs.items():
                data_dir = pid_file.parent

                # Phase 2A: PID-우선 인식 (PID-aliveness-first)
                # z_flow.pid 파일을 PRIMARY PID 소스로 사용.
                # session.status 문자열은 더 이상 하드 게이트가 아니다.
                # session.json은 runtime_kind 확인(보조)과 session.pid 폴백에만 사용.

                # 1단계: z_flow.pid 파일에서 PID 읽기 (1순위)
                pid_from_file = _read_z_flow_pid_file(pid_file)

                # 2단계: session.json에서 PID 읽기 (2순위 폴백)
                session = SessionStore(data_dir).load()
                pid_from_session = session.pid if session is not None else None

                # runtime_kind 가드: z_flow_runtime 이 아닌 session은 두 PID 모두 없는 것으로 취급
                # (session 자체가 없으면 pid_from_file 만으로 시도)
                if session is not None and session.runtime_kind != "z_flow_runtime":
                    runtime_processes.append((None, dir_name, data_dir))
                    continue

                # 3단계: 살아있는 PID 결정 (z_flow.pid 우선, session.pid 폴백)
                active_pid: int | None = None
                if pid_from_file is not None and psutil.pid_exists(pid_from_file):
                    active_pid = pid_from_file
                elif pid_from_session is not None and psutil.pid_exists(pid_from_session):
                    active_pid = pid_from_session

                if active_pid is None:
                    runtime_processes.append((None, dir_name, data_dir))
                    continue

                # 4단계: PID가 실제로 z_flow 런타임인지 cmdline + data_dir 확인 (PID 재사용 방어)
                try:
                    proc = psutil.Process(active_pid)
                    cmdline = proc.cmdline()
                    is_python = "python" in (proc.name() or "").lower()
                    is_z_flow = self.is_runtime_cmdline(cmdline)
                    data_dir_match = self.matches_runtime_target(cmdline, data_dir)
                    if is_python and is_z_flow and data_dir_match:
                        runtime_processes.append((proc, dir_name, data_dir))
                    else:
                        runtime_processes.append((None, dir_name, data_dir))
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    runtime_processes.append((None, dir_name, data_dir))
            # z_flow_dirs가 있으면 PID 결과와 관계없이 항상 직접 반환.
            # 폴백(monitor.find_z_flow_processes)은 z_flow_dirs 자체가 비었을 때만 진입.
            return runtime_processes

        runtime_processes = monitor.find_z_flow_processes()
        if not isinstance(runtime_processes, list):
            return []

        revalidated: list[tuple[object | None, str, Path]] = []
        for proc, dir_name, data_dir in runtime_processes:
            if proc is None:
                revalidated.append((None, dir_name, data_dir))
                continue
            try:
                proc = cast(psutil.Process, proc)
                cmdline = proc.cmdline()
                is_python = "python" in (proc.name() or "").lower()
                is_z_flow = self.is_runtime_cmdline(cmdline)
                data_dir_match = self.matches_runtime_target(cmdline, data_dir)
                if is_python and is_z_flow and data_dir_match:
                    revalidated.append((proc, dir_name, data_dir))
                else:
                    revalidated.append((None, dir_name, data_dir))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                revalidated.append((None, dir_name, data_dir))
        return revalidated

    def find_all_runtime_os_processes(self, monitor, target: str | Path) -> list[object]:
        runtime_processes = monitor.find_all_z_flow_os_processes(target)
        if isinstance(runtime_processes, list):
            return runtime_processes
        return []

    def cleanup_runtime_artifacts(self, data_dir: Path) -> list[str]:
        deleted: list[str] = []
        for name in ("z_flow.pid", "z_flow.lock"):
            path = data_dir / name
            try:
                path.unlink(missing_ok=True)
                if not path.exists():
                    deleted.append(name)
            except Exception:
                pass
        return deleted

    def get_runtime_artifact_names(self) -> tuple[str, ...]:
        return ("z_flow.pid", "z_flow.lock")

    def reset_locked_slot_state(self, *, slot_id: int) -> bool:
        control_db_path = Path(self.get_runtime_control_db_path())
        if not control_db_path.exists():
            return False
        conn = sqlite3.connect(control_db_path)
        try:
            conn.execute(
                "UPDATE slots SET state='IDLE', updated_at=datetime('now') WHERE slot_id=? AND state='LOCKED'",
                (slot_id,),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_runtime_launch_spec(
        self,
        *,
        slot_id: int,
        data_dir: Path,
        exchange_id: str,
    ) -> dict[str, object]:
        runtime_root = self.get_runtime_root()
        script = runtime_root / "run_bot.py"
        wrapper_script = runtime_root / "scripts" / "start_z_flow.sh"
        python_exec = self.get_runtime_python_path()
        if python_exec is None:
            logger.warning("[BRIDGE][LAUNCH] Python 실행 경로 없음 — venv 재생성 필요")
            raise RuntimeError("Z-Flow Python 실행 경로를 확인할 수 없음 — venv 재생성 필요")
        cmd = [
            "bash",
            str(wrapper_script),
            python_exec,
            str(script),
            "--slot-dir",
            str(data_dir),
        ]
        return {"cmd": cmd, "cwd": str(runtime_root)}

    def get_runtime_launch_spec_for_target(
        self,
        target: str,
        monitor=None,
    ) -> dict[str, object] | None:
        """target 이름 → launch_spec 편의 메서드. runtime_target이 아니면 None."""
        data_dir = self.resolve_runtime_data_dir(target, monitor)
        if data_dir is None:
            target_dir_raw = os.getenv("TARGET_DIR", "").strip()
            if target_dir_raw:
                candidate = Path(target_dir_raw).expanduser() / target
                if candidate.exists() and candidate.is_dir():
                    data_dir = candidate
        if data_dir is None:
            return None
        metadata = self.get_slot_runtime_metadata(data_dir)
        if metadata is None:
            return None
        metadata_dict = cast(dict[str, object], metadata)
        return self.get_runtime_launch_spec(
            slot_id=int(cast(int, metadata_dict["slot_id"])),
            data_dir=data_dir,
            exchange_id=str(metadata_dict.get("exchange_id") or ""),
        )

    def is_rotation_enabled(self, target: str) -> bool:
        """rotation_config.json 파일에서 rotation 상태를 읽어온다."""
        if not self._enabled:
            return False
        config = self._load_rotation_config()
        if config is None:
            return False
        return target in config.get("enabled_bots", [])

    def get_slot_type(self, target: str):
        """rotation_config.json 파일에서 slot_type을 읽어온다."""
        if not self._enabled:
            return None
        config = self._load_rotation_config()
        if config is None:
            return None
        return config.get("slot_types", {}).get(target)

    def _get_source_for_bot(self, bot_name: str) -> str | None:
        """TARGET_DIR/bot_name/setting.env의 TRADING_TYPE → data_source 반환. 파악 불가 시 None."""
        target_dir_raw = os.getenv("TARGET_DIR", "").strip()
        if not target_dir_raw:
            return None
        env_path = Path(target_dir_raw).expanduser() / bot_name / "setting.env"
        if not env_path.exists():
            return None
        trading_type = str(EnvConfigHandler.parse(env_path).get("TRADING_TYPE") or "").strip()
        if not trading_type:
            return None
        market_data_provider = _get_market_data_provider_cls()
        if market_data_provider is None:
            return None
        try:
            return market_data_provider.for_trading_type(trading_type).data_source
        except (ValueError, KeyError):
            return None

    def _bots_by_source(self) -> dict[str, set[str]]:
        """TARGET_DIR 봇들을 data_source별로 그룹핑해 반환한다."""
        target_dir_raw = os.getenv("TARGET_DIR", "").strip()
        if not target_dir_raw:
            return {}
        target_dir = Path(target_dir_raw).expanduser()
        if not target_dir.is_dir():
            return {}
        market_data_provider = _get_market_data_provider_cls()
        if market_data_provider is None:
            return {}
        result: dict[str, set[str]] = {}
        for bot_dir in target_dir.iterdir():
            if not bot_dir.is_dir():
                continue
            env_path = bot_dir / "setting.env"
            if not env_path.exists():
                continue
            trading_type = str(EnvConfigHandler.parse(env_path).get("TRADING_TYPE") or "").strip()
            if not trading_type:
                continue
            try:
                source = market_data_provider.for_trading_type(trading_type).data_source
                result.setdefault(source, set()).add(bot_dir.name)
            except (ValueError, KeyError):
                pass
        return result

    def _load_rotation_config(self) -> dict | None:
        """rotation_config.json 파일을 읽고, 누락/손상 시 즉시 복구한다."""
        if not self._enabled:
            return None
        config_path = self.get_pair_trading_state_dir() / "rotation_config.json"
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    enabled_bots = loaded.get("enabled_bots", [])
                    slot_types = loaded.get("slot_types", {})
                    if isinstance(enabled_bots, list) and isinstance(slot_types, dict):
                        # 유효하지 않은 slot_type 정화
                        dirty = False
                        for bot in list(slot_types):
                            if slot_types[bot] not in ("BTC_ETH",):
                                logger.warning(
                                    f"ZFlowBridge: slot_type '{slot_types[bot]}' → 'BTC_ETH' 정화 (bot={bot})"
                                )
                                slot_types[bot] = "BTC_ETH"
                                dirty = True
                        if dirty:
                            loaded["slot_types"] = slot_types
                            self._save_rotation_config(set(enabled_bots), slot_types)
                        return loaded
            except Exception:
                pass
        return self._recover_rotation_config_if_missing()

    def _recover_rotation_config_if_missing(self) -> dict | None:
        enabled_bots, slot_types = self._collect_rotation_recovery_snapshot()
        if not self._save_rotation_config(enabled_bots, slot_types):
            return None
        return {
            "enabled_bots": sorted(enabled_bots),
            "slot_types": dict(sorted(slot_types.items())),
        }

    def _collect_rotation_recovery_snapshot(self) -> tuple[set[str], dict[str, str]]:
        known_targets = self._list_known_pair_targets()
        slot_types = self._recover_slot_types(known_targets)
        enabled_bots = self._recover_enabled_bots(known_targets)
        return enabled_bots, slot_types

    def _list_known_pair_targets(self) -> set[str]:
        from z_pulse.monitoring.bot_state import is_pair_trading_type

        target_dir_raw = os.getenv("TARGET_DIR", "").strip()
        if not target_dir_raw:
            return set()

        target_dir = Path(target_dir_raw).expanduser()
        if not target_dir.exists() or not target_dir.is_dir():
            return set()

        known_targets: set[str] = set()
        for candidate in target_dir.iterdir():
            if not candidate.is_dir() or candidate.name.startswith("_"):
                continue
            env_path = candidate / "setting.env"
            if not env_path.exists():
                continue
            trading_type = str(EnvConfigHandler.parse(env_path).get("TRADING_TYPE") or "").strip()
            if is_pair_trading_type(trading_type):
                known_targets.add(candidate.name)
        return known_targets

    def _recover_slot_types(self, known_targets: set[str]) -> dict[str, str]:
        if not known_targets:
            return {}

        db_path = self.get_bot_operations_db_path()
        if not db_path.exists():
            return {}

        _VALID = ("BTC_ETH",)
        slot_types: dict[str, str] = {}
        try:
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    "SELECT bot_name, slot_type FROM bot_status WHERE slot_type IS NOT NULL AND TRIM(slot_type) != ''"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return {}

        for bot_name, slot_type in rows:
            if bot_name in known_targets and slot_type:
                raw = str(slot_type)
                if raw not in _VALID:
                    logger.warning(
                        f"ZFlowBridge: 복구 중 유효하지 않은 slot_type '{raw}' → 'BTC_ETH' 로 대체 (bot={bot_name})"
                    )
                    raw = "BTC_ETH"
                slot_types[str(bot_name)] = raw
        return slot_types

    def _recover_enabled_bots(self, known_targets: set[str]) -> set[str]:
        if not known_targets:
            return set()

        state_cache_path = self.get_pair_trading_state_dir() / "state_cache.json"
        if not state_cache_path.exists():
            return set()

        try:
            loaded = json.loads(state_cache_path.read_text(encoding="utf-8"))
        except Exception:
            return set()

        if not isinstance(loaded, dict):
            return set()

        active_assignments = loaded.get("active_assignments", {})
        if not isinstance(active_assignments, dict):
            return set()

        return {
            str(bot_name)
            for bot_name in active_assignments.keys()
            if str(bot_name) in known_targets
        }

    def _save_rotation_config(self, enabled_bots: set[str], slot_types: dict[str, str]) -> bool:
        """rotation_config.json 파일에 rotation 설정을 기록한다."""
        if not self._enabled:
            return False
        config_path = self.get_pair_trading_state_dir() / "rotation_config.json"
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "enabled_bots": sorted(enabled_bots),
                "slot_types": dict(sorted(slot_types.items())),
            }
            config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            return True
        except Exception as e:
            logger.error(f"ZFlowBridge: rotation_config 쓰기 실패: {e}")
            return False

    def toggle_rotation(self, target: str) -> bool:
        """대상 봇의 rotation 상태를 토글한다."""
        config = self._load_rotation_config() or {"enabled_bots": [], "slot_types": {}}
        enabled_bots = set(config.get("enabled_bots", []))
        slot_types = dict(config.get("slot_types", {}))
        if target in enabled_bots:
            enabled_bots.discard(target)
        else:
            enabled_bots.add(target)
        return self._save_rotation_config(enabled_bots, slot_types)

    def _sync_target_auto_managed(self, target: str, auto_managed: int) -> None:
        data_dir = self.resolve_runtime_data_dir(target)
        if data_dir is None:
            target_dir_raw = os.getenv("TARGET_DIR", "").strip()
            if target_dir_raw:
                candidate = Path(target_dir_raw).expanduser() / target
                if candidate.exists() and candidate.is_dir():
                    data_dir = candidate
        metadata = self.get_slot_runtime_metadata(data_dir) if data_dir is not None else None
        if metadata is None:
            return
        metadata_dict = cast(dict[str, object], metadata)
        slot_id = int(cast(int, metadata_dict["slot_id"]))
        control_db_path = Path(self.get_runtime_control_db_path())
        if not control_db_path.exists():
            return
        conn = sqlite3.connect(control_db_path)
        try:
            if auto_managed:
                conn.execute(
                    """
                    UPDATE slots
                    SET auto_managed=1,
                        pair_long=NULL,
                        pair_short=NULL,
                        pinned_pair_long=NULL,
                        pinned_pair_short=NULL,
                        updated_at=datetime('now')
                    WHERE slot_id=?
                    """,
                    (slot_id,),
                )
                conn.execute(
                    """
                    UPDATE assignment_log
                    SET released_at=datetime('now'), release_reason='AUTO_MANAGED_ON'
                    WHERE slot_id=? AND released_at IS NULL
                    """,
                    (slot_id,),
                )
                conn.execute(
                    """
                    DELETE FROM commands
                    WHERE slot_id=? AND command_type='ASSIGN' AND status='PENDING'
                    """,
                    (slot_id,),
                )
            else:
                conn.execute(
                    "UPDATE slots SET auto_managed=?, pinned_pair_long=NULL, pinned_pair_short=NULL, updated_at=datetime('now') WHERE slot_id=?",
                    (auto_managed, slot_id),
                )
                conn.execute(
                    """
                    DELETE FROM commands
                    WHERE slot_id=? AND command_type='ASSIGN' AND status='PENDING'
                    """,
                    (slot_id,),
                )
            conn.commit()
        finally:
            conn.close()

    def enable_rotation(self, target: str, slot_type: str) -> bool:
        """대상 봇의 rotation을 활성화하고 slot_type을 설정한다."""
        config = self._load_rotation_config() or {"enabled_bots": [], "slot_types": {}}
        enabled_bots = set(config.get("enabled_bots", []))
        slot_types = dict(config.get("slot_types", {}))
        enabled_bots.add(target)
        slot_types[target] = slot_type
        saved = self._save_rotation_config(enabled_bots, slot_types)
        if saved:
            self._sync_target_auto_managed(target, 1)
            _target_src = self._get_source_for_bot(target)
            for _src, _mgr in self._pair_managers.items():
                if _target_src is None or _src == _target_src:
                    _mgr.rotation_enabled_bots.add(target)
                    _mgr.slot_types[target] = slot_type
            # _pair_manager는 _pair_managers 중 하나와 동일 인스턴스이므로 별도 처리 불필요
            if not self._pair_managers and self._pair_manager is not None:
                self._pair_manager.rotation_enabled_bots.add(target)
                self._pair_manager.slot_types[target] = slot_type
        return saved

    def disable_rotation(self, target: str) -> bool:
        """대상 봇의 rotation을 비활성화하고 EXIT_RESERVATION 신호를 기록한다.

        - enabled_bots에서 target을 discard하고 rotation_config.json을 저장한다.
        - write_bot_state(dir, "EXIT_RESERVATION")으로 봇에게 종료 신호를 전달한다.
        - auto_managed 컬럼은 변경하지 않는다 (수동 핀 슬롯과의 충돌 방지).
        """
        config = self._load_rotation_config() or {"enabled_bots": [], "slot_types": {}}
        enabled_bots = set(config.get("enabled_bots", []))
        slot_types = dict(config.get("slot_types", {}))
        enabled_bots.discard(target)
        slot_types.pop(target, None)
        saved = self._save_rotation_config(enabled_bots, slot_types)
        if saved:
            _target_src = self._get_source_for_bot(target)
            for _src, _mgr in self._pair_managers.items():
                if _target_src is None or _src == _target_src:
                    _mgr.rotation_enabled_bots.discard(target)
                    _mgr.slot_types.pop(target, None)
            if not self._pair_managers and self._pair_manager is not None:
                self._pair_manager.rotation_enabled_bots.discard(target)
                self._pair_manager.slot_types.pop(target, None)
            target_dir_raw = os.getenv("TARGET_DIR", "").strip()
            if not target_dir_raw:
                logger.warning(
                    f"[BRIDGE][DISABLE_ROTATION] TARGET_DIR 미설정 — {target} EXIT_RESERVATION 신호 전달 건너뜀"
                )
                return saved
            bot_dir = Path(target_dir_raw).expanduser() / target
            if bot_dir.exists() and bot_dir.is_dir():
                write_bot_state(bot_dir, "EXIT_RESERVATION")
        return saved

    def disable_rotation_to_manual(self, target: str) -> bool:
        """대상 봇의 rotation을 비활성화하고 수동 즉시진입(one-shot) 모드로 전환한다.

        - enabled_bots에서 target을 discard하고 rotation_config를 저장한다.
        - _sync_target_auto_managed(target, 0)으로 auto_managed=0을 설정하고
          PENDING ASSIGN 커맨드를 삭제해 레이스 컨디션을 차단한다.
        - EXIT_RESERVATION을 쓰지 않는다. 대신 write_bot_state(bot_dir, "WAITING")으로
          rotation_state를 중립화한다 (_check_rotation_stop이 WAITING을 no-op으로 처리).
        """
        config = self._load_rotation_config() or {"enabled_bots": [], "slot_types": {}}
        enabled_bots = set(config.get("enabled_bots", []))
        slot_types = dict(config.get("slot_types", {}))
        enabled_bots.discard(target)
        slot_types.pop(target, None)
        saved = self._save_rotation_config(enabled_bots, slot_types)
        if saved:
            _target_src = self._get_source_for_bot(target)
            for _src, _mgr in self._pair_managers.items():
                if _target_src is None or _src == _target_src:
                    _mgr.rotation_enabled_bots.discard(target)
                    _mgr.slot_types.pop(target, None)
            if not self._pair_managers and self._pair_manager is not None:
                self._pair_manager.rotation_enabled_bots.discard(target)
                self._pair_manager.slot_types.pop(target, None)
            self._sync_target_auto_managed(target, 0)
            target_dir_raw = os.getenv("TARGET_DIR", "").strip()
            if not target_dir_raw:
                logger.warning(
                    f"[BRIDGE][DISABLE_ROTATION_TO_MANUAL] TARGET_DIR 미설정 — {target} WAITING 신호 전달 건너뜀"
                )
                return saved
            bot_dir = Path(target_dir_raw).expanduser() / target
            if bot_dir.exists() and bot_dir.is_dir():
                write_bot_state(bot_dir, "WAITING")
        return saved

    def force_assign_bot(self, target: str) -> list[str]:
        return self.resume_auto_assign_to_waiting(target)

    def resume_auto_assign_to_waiting(self, target: str) -> list[str]:
        """대상 봇을 강제할당 가능 상태로 전환한다.

        우선 PairTradingManager 구현을 사용하고, 매니저를 사용할 수 없으면
        TARGET_DIR 기준으로 rotation_state를 WAITING으로 재설정한다.
        """
        target_dir_raw = os.getenv("TARGET_DIR", "").strip()
        if not target_dir_raw:
            raise RuntimeError(f"ZFlowBridge: TARGET_DIR 미설정으로 자동 배정 재개 실패 ({target})")

        bot_dir = Path(target_dir_raw).expanduser() / target
        if not bot_dir.exists() or not bot_dir.is_dir():
            raise RuntimeError(f"ZFlowBridge: 자동 배정 대상 디렉터리가 없습니다 ({target}: {bot_dir})")

        metadata = self.get_slot_runtime_metadata(bot_dir)
        if metadata is None:
            metadata = self.get_external_pair_runtime_metadata(bot_dir)
        if metadata is None:
            raise RuntimeError(f"ZFlowBridge: 자동 배정 대상이 아닙니다 ({target}: pair_trading metadata 없음)")
        if not is_pair_trading_type(str(metadata.get("slot_type") or "")):
            raise RuntimeError(f"ZFlowBridge: 페어 매매 봇이 아니어서 자동 배정 재개를 거부합니다 ({target})")

        session_store = SessionStore(bot_dir)
        session = session_store.load()
        if session is not None and session.status == "running" and session.pid is not None:
            try:
                if psutil.pid_exists(session.pid):
                    raise RuntimeError(f"ZFlowBridge: 실행 중인 봇은 대기 상태로 재설정하지 않습니다 ({target})")
            except psutil.Error as e:
                logger.warning(f"ZFlowBridge: 실행 상태 확인 실패, resume 계속 진행 ({target}): {e}")
        resolved_state = resolve_pair_bot_state(
            bot_dir,
            process_running=False,
            session=session,
        )
        if resolved_state is PairBotState.RUNNING:
            raise RuntimeError(f"ZFlowBridge: 실행 중인 봇은 대기 상태로 재설정하지 않습니다 ({target})")

        manager = self.get_pair_manager(metadata)
        manager_force_assign = getattr(manager, "force_assign_bot", None)
        if callable(manager_force_assign):
            try:
                result = manager_force_assign(target)
                deleted_files = result if isinstance(result, list) else []
                try:
                    session_store.clear_manual_stop_evidence()
                except Exception as e:
                    logger.warning(f"ZFlowBridge: manual-stop evidence 정리 실패 ({target}): {e}")
                # manager가 bot_selector 부재 등으로 rotation_state를 리셋 못했을 경우 직접 보장
                _state_file = bot_dir / "rotation_state"
                try:
                    _state_file.write_text("WAITING", encoding="utf-8")
                except Exception as _e:
                    logger.warning(f"ZFlowBridge: rotation_state 직접 재설정 실패 ({target}): {_e}")
                return deleted_files
            except Exception as e:
                logger.warning(f"ZFlowBridge: manager force_assign_bot 실패, fallback 사용 ({target}): {e}")

        deleted_files: list[str] = []

        state_file = bot_dir / "rotation_state"
        try:
            state_file.write_text("WAITING", encoding="utf-8")
            logger.info(f"[FORCE_ASSIGN][FALLBACK] {target}: rotation_state=WAITING 재설정")
        except Exception as e:
            raise RuntimeError(f"ZFlowBridge: rotation_state 재설정 실패 ({target}): {e}") from e

        try:
            session_store.clear_manual_stop_evidence()
        except Exception as e:
            raise RuntimeError(f"ZFlowBridge: manual-stop evidence 정리 실패 ({target}): {e}") from e

        for db_file in bot_dir.glob("*_DB.json"):
            try:
                db_file.unlink()
                deleted_files.append(db_file.name)
            except Exception as e:
                logger.warning(f"ZFlowBridge: DB 파일 삭제 실패 ({db_file}): {e}")

        return deleted_files

    def rename_rotation_bot(self, old_dir: str, new_dir: str) -> bool:
        """rotation_config.json에서 봇 이름을 변경한다."""
        config = self._load_rotation_config() or {"enabled_bots": [], "slot_types": {}}
        enabled_bots = set(config.get("enabled_bots", []))
        slot_types = dict(config.get("slot_types", {}))
        if old_dir in enabled_bots:
            enabled_bots.discard(old_dir)
            enabled_bots.add(new_dir)
        if old_dir in slot_types:
            slot_types[new_dir] = slot_types.pop(old_dir)
        return self._save_rotation_config(enabled_bots, slot_types)

    def get_telegram_capabilities(self) -> dict[str, bool]:
        strategy_path = self._path / "strategy" / "pair_trading" if self._path else None
        z_flow_enabled = os.getenv("Z_FLOW_ENABLED", "false").lower() == "true"
        pair_trading_ready = bool(
            self._enabled
            and z_flow_enabled
            and self.is_pair_trading_enabled()
            and strategy_path
            and strategy_path.exists()
        )
        return {"pair_trading": pair_trading_ready}

    def is_pair_trading_ui_enabled(self) -> bool:
        """UI 게이팅 헬퍼: pair trading 관련 UI 버튼/컨텍스트 표시 여부."""
        return self.get_telegram_capabilities().get("pair_trading", False)

    def build_telegram_extensions(self, bot) -> list[object]:
        capabilities = self.get_telegram_capabilities()
        if not capabilities.get("pair_trading"):
            return []
        return [ZFlowTelegramExtension(bot)]

    # ── Restart Intent ──────────────────────────────────────────────────────

    def signal_restart_intent(self, bot_dirs: list[str]) -> bool:
        """의도적 재시작 신호 파일 기록.

        z_flow가 모니터링하는 봇이 종료되었을 때 비정상 종료로 오인하지 않도록
        재시작 의도를 파일로 알린다.

        Args:
            bot_dirs: 재시작 대상 봇 디렉토리명 목록

        Returns:
            True if written successfully, False if bridge is disabled or write failed.
        """
        if not self._enabled:
            return False

        assert self._path is not None
        intent_file = self._path / "pair_trading" / "cache" / "restart_intent.json"
        try:
            intent_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "bot_dirs": bot_dirs,
                "signaled_at": datetime.now(timezone.utc).isoformat(),
                "ttl_seconds": 30,
            }
            intent_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            logger.info(f"ZFlowBridge: restart_intent 기록 ({len(bot_dirs)}개 봇)")
            return True
        except Exception as e:
            logger.error(f"ZFlowBridge: restart_intent 파일 쓰기 실패: {e}")
            return False

    # ── Auto-Manage Flag ────────────────────────────────────────────────────

    @staticmethod
    def set_bot_manager(bot_dir: Path, manager: Literal["z_flow", "z_pulse"]) -> bool:
        """봇 디렉토리에 관리주체 플래그 파일 기록/삭제.

        z_flow는 자신이 관리하는 봇 목록 조회 시 이 파일을 기준으로 필터링.
        z_pulse 대시보드는 이 파일 유무로 '🤖 자동관리' 뱃지를 표시.

        Args:
            bot_dir: 봇 디렉토리 경로
            manager: "z_flow" (자동관리) 또는 "z_pulse" (수동관리)

        Returns:
            True on success.
        """
        flag_file = bot_dir / ".bot_manager.json"
        try:
            if manager == "z_flow":
                data = {
                    "manager": "z_flow",
                    "since": datetime.now(timezone.utc).isoformat(),
                }
                flag_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
                logger.info(f"ZFlowBridge: {bot_dir.name} → z_flow 자동관리 등록")
            else:
                if flag_file.exists():
                    flag_file.unlink()
                    logger.info(f"ZFlowBridge: {bot_dir.name} → z_pulse 수동관리 복귀")
            return True
        except Exception as e:
            logger.error(f"ZFlowBridge: bot_manager 파일 처리 실패: {e}")
            return False

    @staticmethod
    def get_bot_manager(bot_dir: Path) -> str:
        """봇의 현재 관리주체 조회.

        Returns:
            "z_flow" 또는 "z_pulse"
        """
        flag_file = bot_dir / ".bot_manager.json"
        if flag_file.exists():
            try:
                data = json.loads(flag_file.read_text())
                return data.get("manager", "z_pulse")
            except Exception:
                pass
        return "z_pulse"

    @staticmethod
    def normalize_trading_type(value: str) -> str:
        """Return canonical trading type key, applying aliases."""
        if not ZFlowBridge._integration_active:
            return value
        try:
            from z_flow.strategy import normalize_trading_type as _norm
            return _norm(value)
        except ImportError:
            logger.warning("ZFlowBridge: z_flow.strategy not importable — normalize_trading_type returning input unchanged")
            return value

    @staticmethod
    def resolve_trading_type(value: str) -> None:
        """Validate trading type; raise ZFlowBridgeError if unknown."""
        if not ZFlowBridge._integration_active:
            return None
        try:
            from z_flow.strategy import resolve, StrategyRegistryError
        except ImportError:
            logger.warning("ZFlowBridge: z_flow.strategy not importable — resolve_trading_type skipping validation")
            return None
        try:
            resolve(value)
        except StrategyRegistryError as exc:
            raise ZFlowBridgeError(str(exc)) from exc

    @staticmethod
    def is_pair_trading_type(trading_type: str | None) -> bool:
        """Return True if trading_type resolves to the PAIR strategy family."""
        if not ZFlowBridge._integration_active:
            return False
        try:
            from z_flow.strategy import is_pair_trading_type as _is_pair
            return _is_pair(trading_type)
        except ImportError:
            logger.warning("ZFlowBridge: z_flow.strategy not importable — is_pair_trading_type returning False")
            return False

    @staticmethod
    def get_exchange_for_trading_type(trading_type: str) -> str | None:
        """TRADING_TYPE에 해당하는 거래소 이름을 반환. z_flow 비활성 또는 실패 시 None."""
        if not ZFlowBridge._integration_active:
            return None
        try:
            from z_flow.strategy import get_exchange_for_trading_type as _get
            return _get(trading_type)
        except ImportError:
            logger.warning("ZFlowBridge: z_flow.strategy not importable — get_exchange_for_trading_type returning None")
            return None

    @staticmethod
    def default_bot_operations_db_path() -> Path | None:
        """Return the default bot operations DB path (z_flow canonical path)."""
        try:
            from z_flow.config.paths import BOT_OPERATIONS_DB_PATH
            return BOT_OPERATIONS_DB_PATH
        except ImportError:
            logger.warning("ZFlowBridge: z_flow.config.paths not importable — default_bot_operations_db_path returning None")
            return None

    @staticmethod
    def get_z_flow_config() -> dict:
        """Return z_flow configuration dict."""
        try:
            from z_flow.config.env_handler import get_z_flow_config as _get
            return _get()
        except ImportError:
            logger.warning("ZFlowBridge: z_flow.config.env_handler not importable — get_z_flow_config returning {}")
            return {}

    @staticmethod
    def read_bot_assignment_equity(bot_dir: Path):
        """봇 디렉토리의 할당 시점 equity 스냅샷을 읽는다 (z_flow 헬퍼 위임).

        z_pulse → z_flow 단일주입점. z_flow 미설치/오류 시 graceful하게 None을 반환.
        """
        try:
            from z_flow.features.assignment_equity import read_assignment_equity
            return read_assignment_equity(bot_dir)
        except Exception as e:
            logger.warning(f"ZFlowBridge: read_assignment_equity 실패 ({bot_dir}): {e}")
            return None

    def get_grvt_transfer_service(self):
        """z_flow GRVTTransferManager 인스턴스를 반환한다.

        Bridge 비활성 또는 z_flow 미설치 시 None.
        """
        if not self.enabled:
            return None
        try:
            from z_flow.backends.exchanges.grvt_transfer import GRVTTransferManager  # pyright: ignore[reportMissingImports]
            return GRVTTransferManager()
        except Exception:
            return None

    def wire_z_flow_runtime_di(self) -> None:
        """z_flow 런타임 DI 레지스트리에 z_pulse 구현체를 주입한다.

        z_pulse bootstrap이 ZFlowBridge.enabled 확인 후 호출해야 한다.
        """
        from z_pulse.integration.z_flow_runtime_di import wire_z_flow_bot_state_checker, wire_z_flow_alert_send_fn  # pyright: ignore[reportMissingImports]
        wire_z_flow_bot_state_checker()
        wire_z_flow_alert_send_fn()

    # -----------------------------------------------------------------------
    # PairTrading 스케줄러 위임 메서드 (z_pulse → z_flow 경계 단일 주입점)
    # -----------------------------------------------------------------------

    def setup_pair_trading_schedule(
        self,
        process_controller,
        monitor,
        main_loop=None,
        application=None,
        authorized_chat_id=None,
    ) -> None:
        """PairTradingSchedulerMixin 의존성 주입. Z_FLOW_ENABLED=false 시 no-op.

        ZFlowBridge 내부에서 PairTradingSchedulerMixin 인스턴스(_pair_scheduler)를
        lazy 초기화하고 TIER 4 의존성(process_controller, monitor)을 주입한다.
        z_flow 미설치 또는 _pair_managers 미초기화 시 조용히 무시된다.
        """
        if not self._enabled:
            return
        if not self._pair_managers:
            return
        try:
            from z_flow.strategy.pair_trading.scheduler import (  # pyright: ignore[reportMissingImports]
                PairTradingSchedulerMixin,
                _BACKFILL_SENTINEL,
            )
        except ImportError:
            logger.debug(
                "ZFlowBridge: PairTradingSchedulerMixin import 불가 — pair 스케줄러 비활성"
            )
            return

        if self._pair_scheduler is None:
            class _PairSchedulerHost(PairTradingSchedulerMixin):  # type: ignore[misc]
                """ZFlowBridge 내부 전용 PairTrading 스케줄러 호스트."""

            host = _PairSchedulerHost()
            host._source_managers = dict(self._pair_managers)
            host._pair_enabled = True
            host._pair_initialized = False
            host._last_tier3_time = None
            host._last_ranking_hour = -1
            host._last_coint_date = None
            host._backfill_sentinel = _BACKFILL_SENTINEL
            host._backfill_ready = _BACKFILL_SENTINEL.exists()
            host._backfill_wait_ticks = 0
            host._main_loop = main_loop
            host._alert_send_fn = None
            host._application = application
            host._authorized_chat_id = authorized_chat_id
            self._pair_scheduler = host
        else:
            # 메인 루프가 나중에 설정된 경우 갱신
            if main_loop is not None:
                self._pair_scheduler._main_loop = main_loop  # type: ignore[union-attr]
            if application is not None:
                self._pair_scheduler._application = application  # type: ignore[union-attr]
            if authorized_chat_id is not None:
                self._pair_scheduler._authorized_chat_id = authorized_chat_id  # type: ignore[union-attr]

        # TIER 4 의존성 주입 (process_controller, monitor → GRVTBotApplicator 등)
        PairTradingSchedulerMixin.set_pair_trading_dependencies(
            self._pair_scheduler, process_controller, monitor
        )

    def run_pair_trading_schedule(self) -> None:
        """모니터링 루프 매 사이클 호출. Z_FLOW_ENABLED=false 또는 미초기화 시 no-op."""
        if not self._enabled:
            return
        if self._pair_scheduler is None:
            return
        self._pair_scheduler.run_pair_trading_schedule()  # type: ignore[union-attr]

    def is_backfill_ready(self) -> bool:
        """_BACKFILL_SENTINEL.exists() 래핑. Z_FLOW_ENABLED=false 또는 미초기화 시 False."""
        if not self._enabled or self._pair_scheduler is None:
            return False
        try:
            sentinel = getattr(self._pair_scheduler, "_backfill_sentinel", None)
            return bool(sentinel is not None and sentinel.exists())
        except Exception:
            return False

    # -----------------------------------------------------------------------
    # Slot live phase — heartbeat 기반 PRE_ENTRY / IN_POSITION 판별
    # -----------------------------------------------------------------------

    def get_slot_live_phase(self, data_dir: Path) -> SlotLivePhase | None:
        """슬롯 런타임의 현재 생애주기 phase를 heartbeat DB에서 read-only로 읽는다.

        Returns:
            SlotLivePhase if row exists and DB reachable; None otherwise.
        Note:
            pair_control.db의 slot_heartbeat 테이블 (z_flow/core/db_schema.py:86-95)
        """
        try:
            meta = self.get_slot_runtime_metadata(data_dir)
            if meta is None:
                return None
            slot_id = meta.get("slot_id")
            if not slot_id:
                return None
            db_path = self.get_runtime_control_db_path()
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            con.row_factory = sqlite3.Row
            try:
                cur = con.execute(
                    "SELECT state, current_level, unrealized_pnl, last_heartbeat "
                    "FROM slot_heartbeat WHERE slot_id=?",
                    (int(slot_id),),
                )
                row = cur.fetchone()
            finally:
                con.close()
            if row is None:
                return None
            phase, is_fresh = _classify_slot_phase(
                row["state"],
                row["current_level"] or 0,
                row["last_heartbeat"] or "",
            )
            return SlotLivePhase(
                phase=phase,
                current_level=row["current_level"] or 0,
                unrealized_pnl=float(row["unrealized_pnl"] or 0.0),
                is_fresh=is_fresh,
            )
        except Exception:
            return None

    def get_all_slot_live_phases(self) -> dict[int, SlotLivePhase]:
        """pair_control.db의 모든 슬롯 heartbeat를 한 번에 읽어 {slot_id: SlotLivePhase} 반환.

        렌더 루프에서 1회만 호출해 N개 슬롯을 batch read한다.
        예외 시 {} 반환 (대시보드 무중단).
        """
        try:
            db_path = self.get_runtime_control_db_path()
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT slot_id, state, current_level, unrealized_pnl, last_heartbeat "
                    "FROM slot_heartbeat"
                ).fetchall()
            finally:
                con.close()
            result: dict[int, SlotLivePhase] = {}
            for row in rows:
                phase, is_fresh = _classify_slot_phase(
                    row["state"],
                    row["current_level"] or 0,
                    row["last_heartbeat"] or "",
                )
                result[int(row["slot_id"])] = SlotLivePhase(
                    phase=phase,
                    current_level=row["current_level"] or 0,
                    unrealized_pnl=float(row["unrealized_pnl"] or 0.0),
                    is_fresh=is_fresh,
                )
            return result
        except Exception:
            return {}
