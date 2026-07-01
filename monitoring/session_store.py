from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Iterable

import psutil

logger = logging.getLogger(__name__)


def _normalize_path(value: Path | str) -> str:
    return str(Path(value).expanduser().resolve(strict=False)).replace("\\", "/").lower()


def _unique_paths(paths: Iterable[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _normalize_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(path)
    return unique


def _merge_evidence(
    current: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if incoming is None:
        return current
    if current is None:
        return dict(incoming)
    return {**current, **incoming}


class IdentityGenerationMismatchError(RuntimeError):
    pass


def _candidate_session_dirs(target_dir: Path, dir_name: str) -> list[Path]:
    preferred = target_dir / dir_name
    candidates = [preferred]
    try:
        for path in target_dir.glob("*/session.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if str(payload.get("dir_name") or "").strip() != dir_name:
                continue
            data_dir = str(payload.get("data_dir") or "").strip()
            if data_dir:
                candidates.append(Path(data_dir))
            candidates.append(path.parent)
    except OSError:
        pass
    return _unique_paths(candidates)


def _resolve_session_dir(target_dir: Path, dir_name: str) -> Path:
    for candidate in _candidate_session_dirs(target_dir, dir_name):
        if (candidate / "session.json").exists():
            return candidate
    return target_dir / dir_name




@dataclass(frozen=True)
class SessionRef:
    session_id: str
    dir_name: str
    runtime_kind: str
    platform: str
    status: str
    source: str
    pid: int | None = None
    pgid: int | None = None
    tty: str | None = None
    window_id: str | None = None
    identity_generation: str | None = None
    operation_id: str | None = None
    pid_create_time: float | None = None
    identity_captured_at: str | None = None
    window_captured_at: str | None = None
    terminal_app_pid: int | None = None
    validation_status: str = "unknown"
    validation_checked_at: str | None = None
    validation_reason: str | None = None
    custom_title: str | None = None
    data_dir: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_seen_at: str | None = None
    last_running_at: str | None = None
    last_exit_at: str | None = None
    last_exit_reason: str | None = None
    last_state_signal: str | None = None
    evidence: dict[str, Any] | None = None
    auto_restart_24h: bool | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionRef":
        known_fields = {field.name for field in fields(cls)}
        return cls(**{key: value for key, value in payload.items() if key in known_fields})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def has_terminal_stopped_evidence(self) -> bool:
        """Return True only for persisted terminal/exit evidence of a stopped session."""
        stopped_statuses = {"stopped", "manual_stopped", "exited", "terminated", "dead"}
        if self.status not in stopped_statuses:
            return False
        if self.last_exit_at or self.last_exit_reason:
            return True
        if self.last_state_signal in {"MANUAL_STOP", "ON_MANUAL_STOP", "EXIT_RESERVATION", "PROCESS_EXIT"}:
            return True
        evidence = self.evidence or {}
        return bool(evidence.get("manual_stop") or evidence.get("terminal_stopped"))


class SessionStore:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "session.json"
        self.temp_path = self.data_dir / "session.json.tmp"
        self._write_lock = threading.Lock()

    @classmethod
    def from_dir_name(cls, target_dir: Path | str, dir_name: str) -> "SessionStore":
        resolved_dir = _resolve_session_dir(Path(target_dir), dir_name)
        return cls(resolved_dir)

    def save(self, session: SessionRef) -> SessionRef | None:
        with self._write_lock:
            current = self._load_unlocked()
            if current is not None and current.session_id == session.session_id:
                session = replace(
                    session,
                    pid=session.pid if session.pid is not None else current.pid,
                    pgid=session.pgid if session.pgid is not None else current.pgid,
                    tty=session.tty if session.tty is not None else current.tty,
                    window_id=session.window_id if session.window_id is not None else current.window_id,
                    identity_generation=session.identity_generation
                    if session.identity_generation is not None
                    else current.identity_generation,
                    operation_id=session.operation_id if session.operation_id is not None else current.operation_id,
                    pid_create_time=session.pid_create_time
                    if session.pid_create_time is not None
                    else current.pid_create_time,
                    identity_captured_at=session.identity_captured_at
                    if session.identity_captured_at is not None
                    else current.identity_captured_at,
                    window_captured_at=session.window_captured_at
                    if session.window_captured_at is not None
                    else current.window_captured_at,
                    terminal_app_pid=session.terminal_app_pid
                    if session.terminal_app_pid is not None
                    else current.terminal_app_pid,
                    validation_status=session.validation_status
                    if session.validation_status != "unknown"
                    else current.validation_status,
                    validation_checked_at=session.validation_checked_at
                    if session.validation_checked_at is not None
                    else current.validation_checked_at,
                    validation_reason=session.validation_reason
                    if session.validation_reason is not None
                    else current.validation_reason,
                    custom_title=session.custom_title if session.custom_title is not None else current.custom_title,
                    data_dir=session.data_dir if session.data_dir is not None else current.data_dir,
                    created_at=session.created_at if session.created_at is not None else current.created_at,
                    updated_at=session.updated_at if session.updated_at is not None else current.updated_at,
                    last_seen_at=session.last_seen_at if session.last_seen_at is not None else current.last_seen_at,
                    last_running_at=session.last_running_at if session.last_running_at is not None else current.last_running_at,
                    last_exit_at=session.last_exit_at if session.last_exit_at is not None else current.last_exit_at,
                    last_exit_reason=session.last_exit_reason if session.last_exit_reason is not None else current.last_exit_reason,
                    last_state_signal=session.last_state_signal if session.last_state_signal is not None else current.last_state_signal,
                    evidence=_merge_evidence(current.evidence, session.evidence),
                    auto_restart_24h=session.auto_restart_24h if session.auto_restart_24h is not None else current.auto_restart_24h,
                )
            return self._write_unlocked(session)

    def _write_unlocked(self, session: SessionRef) -> SessionRef | None:
        """Perform the atomic disk write. Caller MUST hold self._write_lock."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)

        with open(self.temp_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        try:
            os.replace(self.temp_path, self.path)
        except FileNotFoundError as exc:
            if not self.temp_path.exists():
                # .tmp 파일이 존재하지 않음 — 경합이 아닌 파일 소멸
                # (네트워크 드라이브 eviction, 쓰기 실패 후 cleanup 경로 등)
                logger.debug(
                    "[SESSION_STORE] os.replace 건너뜀 — .tmp 파일 없음 (%s): %s",
                    self.path.name,
                    exc,
                )
            else:
                # .tmp는 있으나 대상 경로에 문제 — 진짜 경합 또는 디렉토리 소멸
                logger.warning(
                    "[SESSION_STORE] os.replace 실패 (경합 또는 디렉토리 소멸) — %s: %s",
                    self.path,
                    exc,
                )
            return None

        try:
            dir_fd = os.open(self.data_dir, os.O_RDONLY)
        except OSError:
            dir_fd = None

        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)

        return session

    def _write(self, session: SessionRef) -> SessionRef | None:
        """Acquire the write lock and perform the atomic disk write."""
        with self._write_lock:
            return self._write_unlocked(session)

    def _load_unlocked(self) -> SessionRef | None:
        """Read session from disk. Caller MUST hold self._write_lock when a
        read-modify-write is in progress; safe to call without the lock for
        pure reads (see load())."""
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            logger.warning("[SESSION_STORE] JSON 파싱 실패 — %s: %s", self.path, e)
            return None
        return SessionRef.from_dict(payload)

    def load(self) -> SessionRef | None:
        return self._load_unlocked()

    def _identity_process_alive(self, session: "SessionRef") -> bool:
        """Return True when the session's recorded process is still running.

        Compares ``pid_create_time`` to guard against PID reuse.  When
        ``pid_create_time`` is unknown the PID is treated as alive (conservative).
        Any psutil error is treated as *not alive* so clearing is allowed.
        """
        pid = session.pid
        if pid is None:
            return False
        try:
            if not psutil.pid_exists(pid):
                return False
            recorded = session.pid_create_time
            if recorded is None:
                return True
            actual = psutil.Process(pid).create_time()
            return abs(actual - recorded) < 1.0
        except (psutil.Error, OSError):
            return False

    def clear_runtime_identity(self, *, force: bool = False) -> SessionRef | None:
        """Explicitly clear volatile runtime identity fields.

        ``save`` and ``merge`` intentionally preserve ``None`` values so callers can
        update partial session metadata without accidentally deleting identity.  Use
        this API when a lifecycle path has positively determined that the previous
        process/window identity is no longer valid.

        Pass ``force=True`` to bypass the liveness guard and clear even when the
        recorded process is still alive.
        """
        with self._write_lock:
            current = self._load_unlocked()
            if current is None:
                return None

            if not force and self._identity_process_alive(current):
                logger.info(
                    "[IDENTITY][CLEAR_SKIP] reason=process_still_alive dir=%s pid=%s pid_create_time=%s",
                    current.dir_name, current.pid, current.pid_create_time,
                )
                return current

            return self._write_unlocked(
                replace(
                    current,
                    pid=None,
                    pgid=None,
                    tty=None,
                    window_id=None,
                    identity_generation=None,
                    operation_id=None,
                    pid_create_time=None,
                    identity_captured_at=None,
                    window_captured_at=None,
                    terminal_app_pid=None,
                    validation_status="unknown",
                    validation_checked_at=None,
                    validation_reason=None,
                )
            )

    # 새 세션 시작 시 이전 종료 근거로 오판될 수 있는 evidence 키
    _EXIT_EVIDENCE_KEYS: frozenset[str] = frozenset(
        {"normal_exit_logged", "exit_reservation", "process_exit_detected"}
    )

    def begin_runtime_identity(
        self,
        session: SessionRef,
        *,
        identity_generation: str,
        operation_id: str | None = None,
        identity_captured_at: str | None = None,
        window_captured_at: str | None = None,
        validation_status: str = "unknown",
    ) -> SessionRef | None:
        with self._write_lock:
            current = self._load_unlocked()
            base = current if current is not None else session

            # 수정 5 (defense-in-depth): 새 세션 시작 시 이전 종료 신호 무효화.
            # last_state_signal: 이전 종료 시그널(EXIT_RESERVATION/PROCESS_EXIT)이 이월되면
            #   process_monitor 감지 전 경쟁창에서 is_assignable 오판 유발 → None으로 초기화.
            # evidence: 종료 관련 키(normal_exit_logged/exit_reservation/process_exit_detected)를
            #   False로 덮어써 새 세션 시작 = 이전 종료 근거 무효 의미론을 명시.
            #   manual_stop·last_exit_reason은 유지(replace에 미지정) → MANUAL_STOP 판정 보존.
            cleared_evidence = (
                _merge_evidence(base.evidence, {k: False for k in self._EXIT_EVIDENCE_KEYS})
                if base.evidence
                else None
            )

            return self._write_unlocked(
                replace(
                    base,
                    identity_generation=identity_generation,
                    operation_id=operation_id,
                    pid=None,
                    pgid=None,
                    tty=None,
                    window_id=None,
                    pid_create_time=None,
                    identity_captured_at=identity_captured_at,
                    window_captured_at=window_captured_at,
                    terminal_app_pid=None,
                    validation_status=validation_status,
                    validation_checked_at=None,
                    validation_reason=None,
                    # 이전 종료 신호 초기화
                    last_state_signal=None,
                    evidence=cleared_evidence,
                    # config fields: incoming session 우선, 기존값 fallback
                    runtime_kind=session.runtime_kind if session.runtime_kind is not None else base.runtime_kind,
                    status=session.status if session.status is not None else base.status,
                    source=session.source if session.source is not None else base.source,
                    auto_restart_24h=session.auto_restart_24h if session.auto_restart_24h is not None else base.auto_restart_24h,
                )
            )

    def update_runtime_identity(
        self,
        *,
        identity_generation: str,
        status: str | None = None,
        source: str | None = None,
        pid: int | None = None,
        pgid: int | None = None,
        tty: str | None = None,
        window_id: str | None = None,
        pid_create_time: float | None = None,
        identity_captured_at: str | None = None,
        window_captured_at: str | None = None,
        terminal_app_pid: int | None = None,
        validation_status: str | None = None,
        validation_checked_at: str | None = None,
        validation_reason: str | None = None,
        last_seen_at: str | None = None,
        last_running_at: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> SessionRef | None:
        with self._write_lock:
            current = self._load_unlocked()
            if current is None:
                raise IdentityGenerationMismatchError("cannot update missing session identity")
            if current.identity_generation is not None and current.identity_generation != identity_generation:
                raise IdentityGenerationMismatchError(
                    f"stored identity generation {current.identity_generation!r} does not match {identity_generation!r}"
                )

            return self._write_unlocked(
                replace(
                    current,
                    status=status if status is not None else current.status,
                    source=source if source is not None else current.source,
                    pid=pid if pid is not None else current.pid,
                    pgid=pgid if pgid is not None else current.pgid,
                    tty=tty if tty is not None else current.tty,
                    window_id=window_id if window_id is not None else current.window_id,
                    pid_create_time=pid_create_time if pid_create_time is not None else current.pid_create_time,
                    identity_captured_at=identity_captured_at
                    if identity_captured_at is not None
                    else current.identity_captured_at,
                    window_captured_at=window_captured_at if window_captured_at is not None else current.window_captured_at,
                    terminal_app_pid=terminal_app_pid if terminal_app_pid is not None else current.terminal_app_pid,
                    validation_status=validation_status if validation_status is not None else current.validation_status,
                    validation_checked_at=validation_checked_at
                    if validation_checked_at is not None
                    else current.validation_checked_at,
                    validation_reason=validation_reason if validation_reason is not None else current.validation_reason,
                    last_seen_at=last_seen_at if last_seen_at is not None else current.last_seen_at,
                    last_running_at=last_running_at if last_running_at is not None else current.last_running_at,
                    evidence=_merge_evidence(current.evidence, evidence),
                )
            )

    def clear_manual_stop_evidence(self) -> SessionRef | None:
        """Clear manual-stop resolver inputs while preserving assignment metadata."""
        with self._write_lock:
            current = self._load_unlocked()
            if current is None:
                return None

            evidence = dict(current.evidence or {})
            evidence.pop("manual_stop", None)

            return self._write_unlocked(
                replace(
                    current,
                    last_exit_reason=(
                        None if current.last_exit_reason == "manual_stop" else current.last_exit_reason
                    ),
                    last_state_signal=(
                        None
                        if current.last_state_signal in {"MANUAL_STOP", "ON_MANUAL_STOP"}
                        else current.last_state_signal
                    ),
                    evidence=evidence or None,
                )
            )

    def merge(self, incoming: SessionRef) -> SessionRef | None:
        with self._write_lock:
            current = self._load_unlocked()
            if current is None:
                # No existing session — write incoming directly (mirrors save() path).
                return self._write_unlocked(incoming)

            if current.session_id != incoming.session_id:
                orphaned = replace(
                    current,
                    status="orphaned",
                    source=incoming.source,
                    pid=None,
                    pgid=None,
                    tty=None,
                    window_id=None,
                    identity_generation=None,
                    operation_id=None,
                    pid_create_time=None,
                    identity_captured_at=None,
                    window_captured_at=None,
                    terminal_app_pid=None,
                    validation_status="orphaned",
                    validation_checked_at=None,
                    validation_reason="session_id_mismatch",
                    last_seen_at=None,
                    last_running_at=None,
                    last_exit_at=None,
                    last_exit_reason=None,
                    last_state_signal=None,
                    evidence=None,
                )
                return self._write_unlocked(orphaned)

            merged = replace(
                current,
                runtime_kind=incoming.runtime_kind or current.runtime_kind,
                platform=incoming.platform or current.platform,
                status=incoming.status or current.status,
                source=incoming.source or current.source,
                pid=incoming.pid if incoming.pid is not None else current.pid,
                pgid=incoming.pgid if incoming.pgid is not None else current.pgid,
                tty=incoming.tty if incoming.tty is not None else current.tty,
                window_id=incoming.window_id if incoming.window_id is not None else current.window_id,
                identity_generation=incoming.identity_generation
                if incoming.identity_generation is not None
                else current.identity_generation,
                operation_id=incoming.operation_id if incoming.operation_id is not None else current.operation_id,
                pid_create_time=incoming.pid_create_time if incoming.pid_create_time is not None else current.pid_create_time,
                identity_captured_at=incoming.identity_captured_at
                if incoming.identity_captured_at is not None
                else current.identity_captured_at,
                window_captured_at=incoming.window_captured_at
                if incoming.window_captured_at is not None
                else current.window_captured_at,
                terminal_app_pid=incoming.terminal_app_pid
                if incoming.terminal_app_pid is not None
                else current.terminal_app_pid,
                validation_status=incoming.validation_status
                if incoming.validation_status != "unknown"
                else current.validation_status,
                validation_checked_at=incoming.validation_checked_at
                if incoming.validation_checked_at is not None
                else current.validation_checked_at,
                validation_reason=incoming.validation_reason
                if incoming.validation_reason is not None
                else current.validation_reason,
                custom_title=incoming.custom_title if incoming.custom_title is not None else current.custom_title,
                data_dir=incoming.data_dir if incoming.data_dir is not None else current.data_dir,
                created_at=incoming.created_at if incoming.created_at is not None else current.created_at,
                updated_at=incoming.updated_at if incoming.updated_at is not None else current.updated_at,
                last_seen_at=incoming.last_seen_at if incoming.last_seen_at is not None else current.last_seen_at,
                last_running_at=incoming.last_running_at if incoming.last_running_at is not None else current.last_running_at,
                last_exit_at=incoming.last_exit_at if incoming.last_exit_at is not None else current.last_exit_at,
                last_exit_reason=incoming.last_exit_reason if incoming.last_exit_reason is not None else current.last_exit_reason,
                last_state_signal=incoming.last_state_signal if incoming.last_state_signal is not None else current.last_state_signal,
                evidence=_merge_evidence(current.evidence, incoming.evidence),
            )
            return self._write_unlocked(merged)
