"""
환경 설정 파일(.env) 처리 핸들러

Phase 3.2 리팩토링: EnvConfigHandler 클래스 및 관련 함수 분리
"""

from __future__ import annotations

import ast
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Optional, Tuple, Union


# ─── 공용 콤마 구분 리스트 파싱 유틸 ──────────────────────────────
def parse_int_list(raw: str, default: list[int]) -> list[int]:
    """콤마 구분 정수 리스트 파싱. 실패 시 default 반환."""
    if not raw or not raw.strip():
        return default
    try:
        return [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default


def parse_float_list(raw: str, default: list[float]) -> list[float]:
    """콤마 구분 실수 리스트 파싱. 실패 시 default 반환."""
    if not raw or not raw.strip():
        return default
    try:
        return [float(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError:
        return default


from z_pulse.constants import FileConfig
from .runtime_settings import runtime_settings

logger = logging.getLogger(__name__)

IGNORE_FILE = FileConfig.IGNORED_DIRS
PROJECT_ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# Phase 1.1 & 1.2: 성능 최적화 - 캐싱 클래스
# ============================================================================


class EntryCountCache:
    """
    Entry count 결과 캐싱 (TTL 기반)

    Phase 1.1: Entry Count 캐싱 구현
    - 대시보드 갱신당 5-30회 반복 호출 문제 해결
    - TTL 10초, 파일 변경 시 무효화
    """

    def __init__(self, ttl=10):
        self._cache = {}  # key: (directory, trading_type), value: (result, timestamp)
        self._ttl = ttl
        self._lock = Lock()
        # [검증] 캐시 통계 추가
        self._stats = {"hits": 0, "misses": 0, "invalidations": 0}

    @property
    def stats(self):
        """[검증] 캐시 통계 반환 (히트율 모니터링용)"""
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            hit_rate = (self._stats["hits"] / total * 100) if total > 0 else 0
            return {
                **self._stats,
                "total_requests": total,
                "hit_rate_percent": round(hit_rate, 2),
                "cache_size": len(self._cache),
            }

    def get(self, directory, trading_type):
        """캐시에서 조회하거나 계산 후 캐싱"""
        key = (str(directory), trading_type or "")

        with self._lock:
            if key in self._cache:
                value, timestamp = self._cache[key]
                if time.time() - timestamp < self._ttl:
                    self._stats["hits"] += 1  # [검증] 통계 업데이트
                    logger.debug(f"Entry count cache HIT: {key}")
                    return value
                else:
                    del self._cache[key]  # 만료된 항목 제거

        # 캐시 미스 - 계산 수행
        with self._lock:
            self._stats["misses"] += 1  # [검증] 통계 업데이트
        logger.debug(f"Entry count cache MISS: {key}")
        result = self._compute_entry_count(directory, trading_type)

        with self._lock:
            self._cache[key] = (result, time.time())

        return result

    def invalidate(self, directory=None):
        """특정 디렉토리 또는 전체 캐시 무효화"""
        with self._lock:
            if directory:
                keys_to_delete = [k for k in self._cache if k[0] == str(directory)]
                count = len(keys_to_delete)
                for key in keys_to_delete:
                    del self._cache[key]
                self._stats["invalidations"] += count  # [검증] 통계 업데이트
                logger.debug(
                    f"Entry count cache invalidated: {directory} ({count} entries)"
                )
            else:
                count = len(self._cache)
                self._cache.clear()
                self._stats["invalidations"] += count  # [검증] 통계 업데이트
                logger.debug(f"Entry count cache cleared ({count} entries)")

    def _compute_entry_count(self, directory, trading_type):
        """기존 get_entry_count_generic() 로직"""
        # 이 함수는 나중에 구현 (기존 함수에서 이동)
        return _get_entry_count_generic_impl(Path(directory), trading_type)


class TradingInfoCache:
    """
    Trading info 캐싱 (파일 mtime 기반)

    Phase 1.2: Trading Info 캐싱 구현
    - 대시보드 갱신당 5-10회 setting.env 읽기 문제 해결
    - mtime 기반 자동 무효화
    """

    def __init__(self, ttl=10):
        self._cache = {}  # key: directory, value: ((trading_type, limit), mtime, timestamp)
        self._ttl = ttl
        self._lock = Lock()
        # [검증] 캐시 통계 추가
        self._stats = {"hits": 0, "misses": 0, "invalidations": 0, "mtime_changes": 0}

    @property
    def stats(self):
        """[검증] 캐시 통계 반환"""
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            hit_rate = (self._stats["hits"] / total * 100) if total > 0 else 0
            return {
                **self._stats,
                "total_requests": total,
                "hit_rate_percent": round(hit_rate, 2),
                "cache_size": len(self._cache),
            }

    def get(self, directory_path):
        """캐시 조회 (파일 mtime 확인)"""
        key = str(directory_path)
        env_file = Path(directory_path) / FileConfig.SETTING_ENV

        if not env_file.exists():
            return None, None

        try:
            current_mtime = env_file.stat().st_mtime
        except OSError:
            return None, None

        with self._lock:
            if key in self._cache:
                cached_data, cached_mtime, timestamp = self._cache[key]

                # TTL 체크 & mtime 체크
                if (
                    time.time() - timestamp < self._ttl
                    and cached_mtime == current_mtime
                ):
                    self._stats["hits"] += 1  # [검증] 통계 업데이트
                    logger.debug(f"Trading info cache HIT: {key}")
                    return cached_data
                elif cached_mtime != current_mtime:
                    self._stats["mtime_changes"] += 1  # [검증] mtime 변경 감지

        # 캐시 미스 - 파일 읽기
        with self._lock:
            self._stats["misses"] += 1  # [검증] 통계 업데이트
        logger.debug(f"Trading info cache MISS: {key}")
        result = self._load_trading_info(directory_path)

        with self._lock:
            self._cache[key] = (result, current_mtime, time.time())

        return result

    def invalidate(self, directory=None):
        """특정 디렉토리 또는 전체 캐시 무효화"""
        with self._lock:
            if directory:
                key = str(directory)
                if key in self._cache:
                    del self._cache[key]
                    self._stats["invalidations"] += 1  # [검증] 통계 업데이트
                logger.debug(f"Trading info cache invalidated: {directory}")
            else:
                count = len(self._cache)
                self._cache.clear()
                self._stats["invalidations"] += count  # [검증] 통계 업데이트
                logger.debug(f"Trading info cache cleared ({count} entries)")

    def _load_trading_info(self, directory_path):
        """기존 get_trading_info_from_env() 로직"""
        config = EnvConfigHandler.parse(directory_path)
        try:
            limit = (
                int(config.get("TRADING_LIMIT_COUNT", 0))
                if "TRADING_LIMIT_COUNT" in config
                else None
            )
        except ValueError:
            limit = None
        return config.get("TRADING_TYPE"), limit


# 전역 캐시 인스턴스 생성
_entry_count_cache = EntryCountCache(ttl=10)
_trading_info_cache = TradingInfoCache(ttl=10)


class EnvConfigHandler:
    """환경 설정 파일(.env) 처리 핸들러"""

    @staticmethod
    def get_env_path(path_input: Union[Path, str]) -> Path:
        """입력이 디렉토리면 setting.env를 붙이고, 파일이면 그대로 반환"""
        path_obj = Path(path_input)
        if path_obj.is_dir():
            return path_obj / FileConfig.SETTING_ENV
        return path_obj

    @staticmethod
    def parse(path_input: Union[Path, str]) -> dict:
        """setting.env 파일을 파싱하여 딕셔너리로 반환"""
        env_file = EnvConfigHandler.get_env_path(path_input)
        config = {}

        if not env_file.exists():
            return config

        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        key, value = line.split("=", 1)
                        config[key.strip()] = value.strip().strip("\"' ")
        except Exception as e:
            logger.warning(f"{env_file} 파싱 중 오류: {e}")
        return config

    @staticmethod
    def load_to_environ(path_input: Union[Path, str] = "setting.env"):
        """파일에서 환경변수를 로드하여 os.environ에 설정"""
        env_file = EnvConfigHandler.get_env_path(path_input)
        if not env_file.exists():
            return

        try:
            config = EnvConfigHandler.parse(env_file)
            count = 0
            for key, value in config.items():
                # 환경변수가 이미 설정되어 있지 않은 경우만 설정
                if key and not os.getenv(key):
                    os.environ[key] = value
                    print(f"환경변수 로드: {key} = {value}")
                    count += 1

            print(f"환경변수 파일 로드 완료: {env_file} ({count}개 변수 설정)")
        except Exception as e:
            print(f"환경변수 파일 로드 실패 ({env_file}): {e}")

    @staticmethod
    def update_key(
        path_input: Union[Path, str], key_to_change: str, new_value: str
    ) -> bool:
        """파일 내 특정 키의 값을 업데이트 (주석 유지)"""
        env_file = EnvConfigHandler.get_env_path(path_input)
        if not env_file.exists():
            return False

        lines = []
        key_found = False
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    stripped_line = line.strip()
                    if stripped_line.startswith(f"{key_to_change}="):
                        # 값 포맷팅 (리스트나 숫자가 아니면 따옴표 처리)
                        # ast.literal_eval을 사용하여 안전하게 리터럴 값만 평가
                        try:
                            ast.literal_eval(new_value)
                            new_value_formatted = new_value
                        except (ValueError, SyntaxError):
                            new_value_formatted = f'"{new_value}"'

                        lines.append(f"{key_to_change}={new_value_formatted}\n")
                        key_found = True
                    else:
                        lines.append(line)

            if not key_found:
                return False

            with open(env_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return True
        except Exception as e:
            logger.error(f"설정 파일 업데이트 실패: {e}")
            return False


def _guess_state_json_file(
    dir_path: Path, trading_type: Optional[str]
) -> Optional[Path]:
    """
    trading_type에 따라 알려진 상태파일을 우선 시도하고,
    없으면 디렉토리 내 *_DB.json, *state*.json 후보를 스캔해 'orders'/'entries' 등을 가진 파일을 반환.
    """
    KNOWN_MAP = {
        "FLIPSTER_ONE_WAY_MARTINGALE": "FLIPSTER_ONE_WAY_MARTINGALE_DB.json",
    }
    if trading_type and trading_type in KNOWN_MAP:
        p = dir_path / KNOWN_MAP[trading_type]
        if p.exists():
            return p

    candidates = []
    for pat in ["*_DB.json", "*state*.json", "*.json"]:
        candidates.extend(dir_path.glob(pat))
    return candidates[0] if candidates else None


# [호환성 유지] 기존 함수들을 EnvConfigHandler 래퍼로 변경
def parse_env_file(dir_path: Path) -> dict:
    return EnvConfigHandler.parse(dir_path)


def get_coin_from_env(dir_path: Path) -> Optional[str]:
    return EnvConfigHandler.parse(dir_path).get("COIN")


def get_trading_info_from_env(dir_path: Path) -> Tuple[Optional[str], Optional[int]]:
    """
    [Phase 1.2 최적화] 캐시 적용된 trading info 조회

    기존: 매번 setting.env 파일 읽기 + 파싱
    개선: TTL 10초 + mtime 기반 캐시 사용
    """
    # # [Phase 1.2] 기존 코드 (주석 처리)
    # config = EnvConfigHandler.parse(dir_path)
    # try:
    #     limit = int(config.get("TRADING_LIMIT_COUNT", 0)) if "TRADING_LIMIT_COUNT" in config else None
    # except ValueError:
    #     limit = None
    # return config.get("TRADING_TYPE"), limit

    # [Phase 1.2] 캐시 사용
    return _trading_info_cache.get(dir_path)


def _bot_ops_db_path() -> Path:
    p = runtime_settings.get_str("BOT_OPS_DB_PATH", "").strip()
    if p:
        return Path(p)
    from z_pulse.integration.z_flow_bridge import ZFlowBridge  # lazy — avoids circular
    return ZFlowBridge.default_bot_operations_db_path()


def _bot_ops_enabled_for_read() -> bool:
    if not runtime_settings.get_bool("BOT_OPS_DB_ENABLED", True):
        return False
    if runtime_settings.get_bool("BOT_OPS_DB_WRITE_ONLY", True):
        return False
    return True


def _get_entry_count_from_bot_ops_db(dir_path: Path) -> Optional[int]:
    db = _bot_ops_db_path()
    if not db.exists():
        return None
    bot_name = dir_path.name
    try:
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute(
            "SELECT current_round FROM bot_status WHERE bot_name = ?", (bot_name,)
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0] is not None:
            return int(row[0])
    except Exception:
        return None
    return None


def _upsert_entry_count_to_bot_ops_db(
    dir_path: Path, entry_count: Optional[int], trading_type: Optional[str]
) -> None:
    if entry_count is None:
        return
    if not runtime_settings.get_bool("BOT_OPS_DB_ENABLED", True):
        return

    db = _bot_ops_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    bot_name = dir_path.name
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_status (
                bot_name TEXT PRIMARY KEY,
                slot_type TEXT,
                current_pair TEXT,
                current_round INTEGER,
                process_state TEXT,
                last_updated TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            INSERT INTO bot_status (bot_name, slot_type, current_pair, current_round, process_state, last_updated)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_name) DO UPDATE SET
                slot_type=COALESCE(excluded.slot_type, bot_status.slot_type),
                current_pair=COALESCE(excluded.current_pair, bot_status.current_pair),
                current_round=excluded.current_round,
                process_state=COALESCE(excluded.process_state, bot_status.process_state),
                last_updated=excluded.last_updated
            """,
            (bot_name, trading_type, None, int(entry_count), "RUNNING", now),
        )
        conn.commit()
        conn.close()
    except Exception:
        # 본 경로는 보조 동기화이므로 본 함수 실패는 무시
        pass


def get_entry_count_generic(
    dir_path: Path,
    trading_type: Optional[str] = None,
    prefer_db: bool = True,
) -> Optional[int]:
    """
    [Phase 2 확장] entry count 조회

    우선순위:
    1) (Step B/C) bot_operations.db 읽기 (prefer_db=True인 경우)
    2) 기존 파일 스캔/캐시 fallback
    3) fallback 결과를 bot_operations.db로 보조 동기화
    """
    if prefer_db and _bot_ops_enabled_for_read():
        db_val = _get_entry_count_from_bot_ops_db(dir_path)
        if db_val is not None:
            return db_val

        # Cutover 완전 모드: DB miss 시 파일 fallback 금지
        no_fallback = runtime_settings.get_bool("BOT_OPS_DB_NO_FILE_FALLBACK", False)
        if no_fallback:
            return None

    result = _entry_count_cache.get(dir_path, trading_type)
    _upsert_entry_count_to_bot_ops_db(dir_path, result, trading_type)
    return result


def _get_entry_count_generic_impl(
    dir_path: Path, trading_type: Optional[str] = None
) -> Optional[int]:
    """
    [내부 구현] Entry count 실제 계산 로직

    전략 타입에 따른 파일 또는 디렉토리 내 JSON 파일들을 순회하며
    '현재 진입 회차(엔트리/오더 개수)'를 추정해 반환합니다.

    1. 탐색 순서: KNOWN_MAP -> *_DB.json -> *state*.json -> *.json
    2. 파싱 우선순위:
       - List형 키 ('orders', 'entries', 'trades', 'positions') -> 길이 반환
       - Suffix 매칭 ('*EntryPriceStack') -> 길이 반환 (사용자 요청 구조 반영)
       - Int형 키 ('current_entry', 'current_round', 'round', 'entry_count') -> 값 반환
    """
    # [핵심 원칙] trading_type이 있으면 {trading_type}_DB.json 하나만 본다.
    # 구버전 DB, 복사본 등 디렉토리 내 다른 파일은 일절 무시.
    if trading_type:
        candidates = []
        db_file = dir_path / f"{trading_type}_DB.json"
        if db_file.exists():
            candidates.append(db_file)
        # DB 파일이 없거나 비어 있으면 카운트 없음으로 처리
    else:
        # trading_type 미지정 시 기존 glob 탐색 사용
        candidates = []
        for pat in ["*_DB.json", "*state*.json", "*.json"]:
            candidates.extend(dir_path.glob(pat))
        # 중복 제거 (순서 유지)
        seen = set()
        unique = []
        for c in candidates:
            p = c.resolve()
            if p not in seen:
                seen.add(p)
                unique.append(c)
        candidates = unique

    # 후보 파일들을 순회하며 실제 데이터 파싱
    for state_file in candidates:
        # 설정 파일 등은 명확히 제외 (오탐 방지)
        if state_file.name.lower() in [
            "setting.json",
            "package.json",
            "tsconfig.json",
            "settings.json",
        ]:
            continue

        try:
            with open(state_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # 파일 내용이 딕셔너리가 아니면 패스
            if not isinstance(data, dict):
                continue

            # (A) 표준 리스트 타입 확인 (orders, entries 등) -> 길이 반환
            for key in ("orders", "entries", "trades", "positions"):
                val = data.get(key)
                if isinstance(val, list):
                    return len(val)
                elif isinstance(
                    val, dict
                ):  # 딕셔너리인 경우도 길이 반환 (드물지만 대비)
                    return len(val)

            # (B) [사용자 요청] 동적 키 매칭 (*EntryPriceStack)
            # 예: "coin1EntryPriceStack": [...], "coin2EntryPriceStack": [...]
            for key, val in data.items():
                if key.endswith("EntryStack") and isinstance(val, list):
                    return len(val)

            # (C) 정수/문자열 값 확인 (current_entry 등) -> 값 반환
            for key in (
                "current_entry",
                "current_round",
                "round",
                "entry_count",
                "entry_cnt",
            ):
                val = data.get(key)
                if val is not None:
                    if isinstance(val, int):
                        return val
                    if isinstance(val, str) and val.isdigit():
                        return int(val)

            # (D) 최후의 수단: 데이터 루트에 리스트가 있는지 확인 (기존 로직 보존)
            # 단, error 키 등이 있는 경우 오탐 가능성이 있으므로 신중해야 함
            # 위 (B) 단계에서 대부분 걸러질 것이므로, 여기서는 정말 순수 리스트 데이터만 찾음
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 0:
                    # 리스트 내부 요소가 딕셔너리나 숫자일 경우만 유효한 엔트리로 간주
                    if isinstance(v[0], (dict, int, float)):
                        return len(v)

        except Exception:
            # 읽기 실패나 파싱 오류 시 다음 후보 파일 확인
            continue

    # 모든 후보를 확인했으나 정보를 찾지 못한 경우 (표시하지 않음 or 0)
    # 0을 반환하면 (0/20) 처럼 표시되고, None을 반환하면 개수 표시가 생략됨
    return None


def load_env_file(filename="setting.env"):
    """EnvConfigHandler를 사용하여 환경변수 로드"""
    EnvConfigHandler.load_to_environ(filename)


def load_ignored_dirs():
    if not os.path.exists(IGNORE_FILE):
        return set()
    with open(IGNORE_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def save_ignored_dirs(dirs):
    with open(IGNORE_FILE, "w") as f:
        for d in sorted(dirs):
            f.write(d + "\n")


# ============================================================================
# Phase C: Pair Trading Stop-Loss Configuration
# ============================================================================


def get_stop_loss_config() -> dict:
    """
    Stop-loss 설정 읽기 (setting.env)

    Returns:
        dict: {
            'enabled': bool,           # Stop-loss 활성화 여부
            'z_score_threshold': float, # Z-Score 발산 임계값 (기본: 3.5)
            'time_multiplier': float    # 시간 제한 배수 (기본: 3.0)
        }

    Example:
        >>> config = get_stop_loss_config()
        >>> if config['enabled']:
        ...     monitor = StopLossMonitor(
        ...         config=StopLossConfig(
        ...             z_score_threshold=config['z_score_threshold'],
        ...             time_multiplier=config['time_multiplier']
        ...         )
        ...     )

    설정 파일 (setting.env):
        STOP_LOSS_ENABLED=true
        STOP_LOSS_Z_THRESHOLD=3.5
        STOP_LOSS_TIME_MULTIPLIER=3.0
    """
    enabled = runtime_settings.get_bool("STOP_LOSS_ENABLED", False)

    z_threshold = runtime_settings.get_float("STOP_LOSS_Z_THRESHOLD", 3.5)
    time_mult = runtime_settings.get_float("STOP_LOSS_TIME_MULTIPLIER", 3.0)
    capital_loss_warn_ratio = runtime_settings.get_float(
        "RISK_CAPITAL_LOSS_WARN_RATIO", 1.0
    )

    return {
        "enabled": enabled,
        "z_score_threshold": z_threshold,
        "time_multiplier": time_mult,
        "capital_loss_warn_ratio": capital_loss_warn_ratio,  # [L3-2] 예상 손실 경보 비율
    }


# ============================================================================
# Phase B: Signal Delay Configuration
# ============================================================================


def get_signal_delay_config() -> dict:
    """
    시간차 신호 검증 설정 읽기 (setting.env)

    Returns:
        dict: {
            'enabled': bool,
            'delay_minutes': float,    # 신호 유지 대기 시간
            'max_age_minutes': float,  # 대기큐 항목 최대 유효 시간
        }

    설정 파일 (setting.env):
        SIGNAL_DELAY_ENABLED=true
        SIGNAL_DELAY_MINUTES=3
        SIGNAL_MAX_AGE_MINUTES=10
    """
    enabled = runtime_settings.get_bool("SIGNAL_DELAY_ENABLED", True)
    delay_minutes = runtime_settings.get_float("SIGNAL_DELAY_MINUTES", 3.0)
    max_age_minutes = runtime_settings.get_float("SIGNAL_MAX_AGE_MINUTES", 10.0)

    return {
        "enabled": enabled,
        "delay_minutes": delay_minutes,
        "max_age_minutes": max_age_minutes,
    }


# Phase C: Volume Check Configuration
# ============================================================================


def get_volume_check_config() -> dict:
    """
    거래량 크로스체크 설정 읽기 (setting.env)

    Returns:
        dict: {
            'enabled': bool,
            'window_minutes': int,   # 거래량 윈도우 크기 (분)
            'min_zscore': float,     # 차단 Z-Score 임계치
        }

    설정 파일 (setting.env):
        VOLUME_CHECK_ENABLED=true
        VOLUME_WINDOW_MINUTES=60
        VOLUME_MIN_ZSCORE=-1.0
    """
    enabled = runtime_settings.get_bool("VOLUME_CHECK_ENABLED", True)
    window_minutes = runtime_settings.get_int("VOLUME_WINDOW_MINUTES", 60)
    min_zscore = runtime_settings.get_float("VOLUME_MIN_ZSCORE", -1.0)

    return {
        "enabled": enabled,
        "window_minutes": window_minutes,
        "min_zscore": min_zscore,
    }


# ============================================================================
# Z-Flow Configuration
# ============================================================================


def get_z_flow_config() -> dict:
    """Z-Flow 설정은 canonical z_flow accessor에 위임한다."""
    from z_pulse.integration.z_flow_bridge import ZFlowBridge  # lazy — avoids circular

    return ZFlowBridge.get_z_flow_config()

