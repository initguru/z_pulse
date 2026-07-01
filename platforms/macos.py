"""
macOS 플랫폼 핸들러

macOS 전용 창 정렬, 터미널 정리, 프로세스 시작 로직
"""

import asyncio
import logging
import os
import shlex
import subprocess
from typing import Any, List, Optional, Set, Tuple, Union

from .base import PlatformHandler
from z_pulse.config.paths import PROJECT_ROOT
from z_pulse.utils.grid_calculator import calculate_grid_layout

logger = logging.getLogger(__name__)
SHELL_COMMAND_TIMEOUT_SECONDS = 15.0


_SELF_PROTECTED_CMDLINE_MARKERS = ("z_pulse", "2oolkit-monitor")


def _escape_applescript_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _normalize_ps_field(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    return " ".join(str(value or "").strip().split())


def _get_process_command(pid: str) -> str:
    ps_r = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        capture_output=True,
        text=True,
    )
    return _normalize_ps_field(ps_r.stdout)


def _get_process_pgid(pid: str) -> Optional[str]:
    ps_r = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pgid="],
        capture_output=True,
        text=True,
    )
    pgid = _normalize_ps_field(ps_r.stdout)
    return pgid or None


def _get_process_cwd(pid: str) -> str:
    lsof_r = subprocess.run(
        ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        capture_output=True,
        text=True,
    )
    for line in str(lsof_r.stdout or "").splitlines():
        if line.startswith("n"):
            return line[1:].strip()
    return ""


def _current_ancestor_pids() -> Set[str]:
    pids: Set[str] = set()
    pid = os.getpid()
    while pid and pid > 0 and str(pid) not in pids:
        pids.add(str(pid))
        try:
            pid = os.getppid() if pid == os.getpid() else int(_normalize_ps_field(subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid="],
                capture_output=True,
                text=True,
            ).stdout) or "0")
        except (OSError, ValueError):
            break
    return pids


def _current_session_id() -> Optional[int]:
    try:
        return os.getsid(0)
    except (AttributeError, OSError):
        return None


def _process_session_id(pid: str) -> Optional[int]:
    try:
        return os.getsid(int(pid))
    except (OSError, ValueError):
        return None


def _tty_kill_skip_reasons(
    pid: str, cmdline: str, cwd: str, pgid: Optional[str], ancestor_pids: Set[str]
) -> List[str]:
    reasons: List[str] = []
    current_pid = str(os.getpid())
    current_pgid = str(os.getpgrp())
    current_sid = _current_session_id()
    process_sid = _process_session_id(pid)

    if str(pid) == current_pid:
        reasons.append("current_process")
    if str(pid) in ancestor_pids:
        reasons.append("ancestor_process")
    if pgid and str(pgid) == current_pgid:
        reasons.append("current_process_group")
    if current_sid is not None and process_sid is not None and process_sid == current_sid:
        reasons.append("current_session")

    marker_text = f"{cmdline}\n{cwd}".lower()
    for marker in _SELF_PROTECTED_CMDLINE_MARKERS:
        if marker.lower() in marker_text:
            reasons.append(f"operational_marker:{marker}")
    return reasons


def resolve_terminal_window_id_for_identity(
    *, tty: Optional[str], custom_title: Optional[str]
) -> Tuple[Optional[str], str, int, Optional[str]]:
    """Resolve a Terminal window id only from explicit start identity markers.

    This helper is intentionally narrow: it does not inspect the front/current
    window and does not use broad title fallback.  A window id is returned only
    when Terminal's window list contains exactly one safe match for the supplied
    tty, or (if no tty is available) for the exact/prefix custom title marker.

    Returns a 4-tuple: (window_id, reason, match_count, window_tty).
    window_tty is the tty string parsed from the matched Terminal window, or
    None when no unique match was found.  Callers may use window_tty as a
    fallback when psutil proc.terminal() returns None due to a timing race.
    """
    normalized_tty = (tty or "").strip()
    normalized_title = (custom_title or "").strip()
    if not normalized_tty and not normalized_title:
        return None, "missing_tty_and_custom_title", 0, None

    field_sep = "|||FIELD|||"
    script = f'''
tell application "Terminal"
    set windowRecords to ""
    repeat with w in windows
        try
            set windowId to (id of w as integer)
            set windowTty to (tty of front tab of w as string)
            set windowCustom to (custom title of w as string)
            set windowName to (name of w as string)
            set windowRecords to windowRecords & (windowId as string) & "{field_sep}" & windowTty & "{field_sep}" & windowCustom & "{field_sep}" & windowName & linefeed
        end try
    end repeat
    return windowRecords
end tell
'''
    completed = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=2,
    )
    if completed.returncode != 0:
        return None, "terminal_window_query_failed", 0, None

    matches: list[tuple[str, Optional[str]]] = []
    for line in (completed.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(field_sep)]
        if len(parts) != 4:
            continue
        window_id, window_tty, window_custom, window_name = parts
        tty_matches = bool(normalized_tty and window_tty == normalized_tty)
        title_matches = bool(
            normalized_title
            and (
                window_custom == normalized_title
                or window_custom.startswith(f"{normalized_title} [")
                or window_name == normalized_title
                or window_name.startswith(f"{normalized_title} [")
            )
        )
        if tty_matches or (not normalized_tty and title_matches):
            raw_tty = window_tty if window_tty else None
            normalized_window_tty: Optional[str] = (
                str(raw_tty).strip() or None if raw_tty is not None else None
            )
            matches.append((window_id, normalized_window_tty))

    unique_matches = sorted({pair[0] for pair in matches if pair[0]})
    if len(unique_matches) == 1:
        matched_id = unique_matches[0]
        matched_tty = next((t for wid, t in matches if wid == matched_id), None)
        return matched_id, "matched", 1, matched_tty
    if len(unique_matches) > 1:
        return None, "ambiguous", len(unique_matches), None
    return None, "not_found", 0, None


class MacOSHandler(PlatformHandler):
    """macOS 플랫폼 전용 핸들러"""

    _terminal_gui_close_lock: Optional[asyncio.Lock] = None
    _terminal_gui_close_lock_loop: Optional[asyncio.AbstractEventLoop] = None

    def __init__(self):
        self._accessibility_checked = False
        self._has_accessibility_permission = False
        self._cleanup_tasks: dict[str, asyncio.Task[dict[str, object]]] = {}

    @classmethod
    def _get_terminal_gui_close_lock(cls) -> asyncio.Lock:
        current_loop = asyncio.get_running_loop()
        if (
            cls._terminal_gui_close_lock is None
            or cls._terminal_gui_close_lock_loop is not current_loop
        ):
            cls._terminal_gui_close_lock = asyncio.Lock()
            cls._terminal_gui_close_lock_loop = current_loop
        return cls._terminal_gui_close_lock

    async def _check_accessibility_permission(self) -> Tuple[bool, Optional[str]]:
        """
        System Events 접근 권한 확인

        Returns:
            (has_permission, error_message): 권한 있으면 (True, None), 없으면 (False, error_msg)
        """
        check_script = """
        tell application "System Events"
            try
                return (name of first process) is not ""
            on error errMsg
                return "ERROR: " & errMsg
            end try
        end tell
        """
        ret, stdout, stderr = await self.run_shell_command(
            check_script, is_applescript=True
        )

        if ret != 0:
            return False, f"AppleScript 실행 실패: {stderr}"

        if stdout.startswith("ERROR:"):
            return False, "System Events 접근 권한이 없습니다"

        return True, None

    async def _request_accessibility_permission(self) -> bool:
        """
        접근성 권한 요청 및 설정 안내

        Returns:
            권한 획득 여부
        """
        # 권한 요청 다이얼로그 표시
        request_script = """
        tell application "System Events"
            display dialog "창 정렬 기능을 사용하려면 Accessibility 권한이 필요합니다." & return & return & "1. 시스템 환경설정이 열리면 '손쉬운 사용'을 선택하세요." & return & "2. 왼쪽 목록에서 '터미널' 또는 'Python'을 체크하세요." & return & return & "참고: 권한을 부여한 후에는 프로그램을 재시작해야 합니다." buttons {"지금 설정하기", "나중에"} default button "지금 설정하기" with icon caution
            set buttonPressed to button returned of result
            if buttonPressed is "지금 설정하기" then
                return "OPEN_SETTINGS"
            else
                return "CANCELLED"
            end if
        end try
        """

        ret, stdout, _ = await self.run_shell_command(
            request_script, is_applescript=True
        )

        if stdout == "OPEN_SETTINGS":
            # 시스템 환경설정 열기
            open_script = 'do shell script "open x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"'
            await self.run_shell_command(open_script, is_applescript=True)

        return False

    async def _get_menu_bar_height(self) -> int:
        """
        메뉴 바 높이 동적 감지

        Returns:
            메뉴 바 높이 (픽셀)
        """
        script = """
        tell application "System Events"
            tell application process "SystemUIServer"
                try
                    set menuBarHeight to height of menu bar 1
                    return menuBarHeight
                on error
                    return 25
                end try
            end tell
        end tell
        """
        ret, stdout, _ = await self.run_shell_command(script, is_applescript=True)
        if ret == 0 and stdout:
            try:
                height = int(stdout)
                return height
            except ValueError:
                pass
        return 25  # 기본값

    async def _get_work_area(self) -> Tuple[int, int, int, int]:
        """
        [Windows 방식] 작업 영역(Work Area) 동적 감지

        Windows의 SystemParametersInfoW(0x0030)와 동일하게
        메뉴 바와 Dock을 제외한 실제 사용 가능한 영역을 반환합니다.

        Returns:
            (x, y, width, height): 작업 영역의 좌표와 크기
        """
        # 메인 화면의 visible frame 가져오기 (메뉴 바, Dock 제외)
        script = """
        tell application "System Events"
            tell application process "Finder"
                try
                    -- AXVisibleFrame: 메뉴 바와 Dock을 제외한 실제 사용 영역
                    set visibleFrame to value of attribute "AXVisibleFrame" of scroll area 1 of window 1
                    set x1 to item 1 of visibleFrame
                    set y1 to item 2 of visibleFrame
                    set x2 to item 3 of visibleFrame
                    set y2 to item 4 of visibleFrame
                    set width to x2 - x1
                    set height to y2 - y1
                    return x1 & "," & y1 & "," & width & "," & height
                on error
                    -- Fallback: 메뉴 바 높이만 고려
                    try
                        set menuBarHeight to 25
                        try
                            set menuBarHeight to height of menu bar 1 of application process "SystemUIServer"
                        end try
                        tell application "Finder"
                            set screenBounds to bounds of window of desktop
                            set screenWidth to item 3 of screenBounds
                            set screenHeight to (item 4 of screenBounds) - menuBarHeight
                            return "0," & menuBarHeight & "," & screenWidth & "," & screenHeight
                        end tell
                    on error
                        return "0,25,1512,957"
                    end try
                end try
            end tell
        end tell
        """
        ret, stdout, _ = await self.run_shell_command(script, is_applescript=True)

        if ret == 0 and stdout and "," in stdout:
            try:
                parts = stdout.split(",")
                if len(parts) >= 4:
                    return int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3])
            except (ValueError, IndexError):
                pass

        return 0, 25, 1512, 957  # 기본값 (메뉴 바 25px 제외)

    async def arrange_windows(  # type: ignore[override]
        self, target_keywords: Set[str], process_name: str
    ) -> int:
        """
        [macOS] 터미널 창 정렬 로직 (개선된 버전)

        개선사항:
        - System Events 접근 권한 체크 및 요청
        - bounds 대신 position/size 분리 설정 (더 안정적)
        - 메뉴 바 높이 동적 감지
        - 모든 target_keywords 사용 (Windows와 동일한 동작)

        Args:
            target_keywords: 창 제목에서 검색할 키워드 집합
            process_name: 프로세스 이름 (추가 키워드)

        Returns:
            정렬된 창의 개수
        """
        # 1. 권한 체크 (최초 1회 또는 실패 시)
        if not self._accessibility_checked or not self._has_accessibility_permission:
            has_perm, error_msg = await self._check_accessibility_permission()
            self._accessibility_checked = True
            self._has_accessibility_permission = has_perm

            if not has_perm:
                logger.warning(f"[macOS] 접근성 권한 없음: {error_msg}")
                # 권한 요청 및 안내
                await self._request_accessibility_permission()
                return -1  # 권한 없음을 나타내는 특별한 반환값

        # 2. Terminal 실행 확인
        check_script = (
            'tell application "System Events" to return (exists process "Terminal")'
        )
        ret, stdout, _ = await self.run_shell_command(check_script, is_applescript=True)
        if ret != 0 or "true" not in stdout.lower():
            logger.warning("[macOS] Terminal.app이 실행 중이 아닙니다")
            return 0

        # 3. 검색 키워드 준비 (Windows와 동일하게 모든 키워드 사용)
        all_keywords = target_keywords | {process_name}
        logger.debug(f"[macOS] 창 검색 키워드: {all_keywords}")

        # 4. 봇 창 제목 목록 가져오기 (모든 키워드로 검색, 죽은 창 제외)
        keywords_script = "|".join(all_keywords)
        row_sep = "|||ROW|||"
        field_sep = "|||FIELD|||"
        get_titles_script = f'''
        tell application "Terminal"
            set window_titles to {{}}
            repeat with w in windows
                try
                    -- 죽은 창(프로세스 없는 창)은 정렬 대상에서 제외
                    if processes of w is {{}} then
                        -- [프로세스 완료됨] 상태의 창은 건너뜀
                        log "창 정렬: 죽은 창 건너뜀 - " & (name of w as string)
                    else
                        set w_name to (name of w as string)
                        set w_custom to (custom title of w as string)
                        set matched to false

                        -- 모든 키워드 체크
                        set kw_list to "{keywords_script}"
                        set old_delim to AppleScript's text item delimiters
                        set AppleScript's text item delimiters to "|"
                        set kw_items to text items of kw_list
                        set AppleScript's text item delimiters to old_delim

                        repeat with kw in kw_items
                            if w_name contains kw or w_custom contains kw then
                                set matched to true
                                exit repeat
                            end if
                        end repeat

                        if matched then
                            set end of window_titles to w_name & "|||FIELD|||" & w_custom
                        end if
                    end if
                end try
            end repeat

            set old_delimiters to AppleScript's text item delimiters
            set AppleScript's text item delimiters to "|||ROW|||"
            set titles_str to window_titles as string
            set AppleScript's text item delimiters to old_delimiters
            return titles_str
        end tell
        '''
        ret, stdout, stderr = await self.run_shell_command(
            get_titles_script, is_applescript=True
        )
        if ret != 0:
            logger.error(f"[macOS] 창 제목 가져오기 실패: {stderr}")
            return 0

        if not stdout:
            logger.warning(f"[macOS] '{all_keywords}' 키워드로 창을 찾을 수 없습니다")
            return 0

        window_records: list[tuple[str, str]] = []
        for record in stdout.split(row_sep):
            title = record.strip()
            if not title:
                continue
            if field_sep in title:
                window_name, custom_title = title.split(field_sep, 1)
            else:
                window_name, custom_title = title, ""
            if window_name.strip() or custom_title.strip():
                window_records.append((window_name.strip(), custom_title.strip()))

        # 5. Python에서 정렬 (run_all 창을 맨 뒤로, 나머지는 대소문자 무시 오름차순)
        window_records.sort(key=lambda item: item[0].lower())

        logger.info(f"[macOS] 정렬된 창 순서 ({len(window_records)}개): {window_records}")

        window_count = len(window_records)
        if window_count == 0:
            return 0

        # 6. [Windows 방식] 작업 영역(Work Area) 가져오기
        base_x, base_y, work_width, work_height = await self._get_work_area()

        logger.debug(
            f"[macOS] 작업 영역: ({base_x},{base_y}) {work_width}x{work_height}"
        )

        # 7. 격자 계산 (유틸리티 함수 사용)
        cols, rows = calculate_grid_layout(window_count)

        cell_width = work_width // cols
        cell_height = work_height // rows

        # 8. 정렬된 순서대로 창 배치 (Windows와 동일한 방식)
        applescript_records = ", ".join(
            f'"{_escape_applescript_string(window_name)}|||{_escape_applescript_string(custom_title)}"'
            for window_name, custom_title in window_records
        )
        arrange_script = f"""
        tell application "Terminal"
            set sorted_records to {{{applescript_records}}}
            set windowIndex to 1
            set baseX to {base_x}
            set baseY to {base_y}
            set cellW to {cell_width}
            set cellH to {cell_height}
            set cols to {cols}
            set successCount to 0
            set errorList to ""

            repeat with recordItem in sorted_records
                try
                    set recordText to recordItem as string
                    set old_delim to AppleScript's text item delimiters
                    set AppleScript's text item delimiters to "|||"
                    set recordParts to text items of recordText
                    set AppleScript's text item delimiters to old_delim
                    set targetName to item 1 of recordParts
                    set targetCustom to ""
                    if (count of recordParts) ≥ 2 then
                        set targetCustom to item 2 of recordParts
                    end if
                    set the_window to missing value
                    repeat with candidateWindow in windows
                        set candidateName to (name of candidateWindow as string)
                        set candidateCustom to (custom title of candidateWindow as string)
                        if candidateName is targetName and candidateCustom is targetCustom then
                            set the_window to candidateWindow
                            exit repeat
                        end if
                    end repeat
                    if the_window is missing value then error "window not found"

                    set rowIndex to ((windowIndex - 1) div cols)
                    set colIndex to ((windowIndex - 1) mod cols)

                    -- Windows와 동일하게 작업 영역 내에서 계산
                    set x1 to baseX + (colIndex * cellW)
                    set y1 to baseY + (rowIndex * cellH)
                    set x2 to x1 + cellW
                    set y2 to y1 + cellH

                    -- bounds로 설정
                    set bounds of the_window to {{x1, y1, x2, y2}}
                    set visible of the_window to true

                    set successCount to successCount + 1
                    set windowIndex to windowIndex + 1
                on error errMsg
                    set errorList to errorList & recordText & ": " & errMsg & "; "
                end try
            end repeat

            -- [Windows 방식] 모든 창을 Foreground로 가져오기 (역순으로 처리하여 첫 번째 창이 최상단)
            set windowCount to count of sorted_records
            repeat with i from windowCount to 1 by -1
                try
                    set recordText to item i of sorted_records
                    set old_delim to AppleScript's text item delimiters
                    set AppleScript's text item delimiters to "|||"
                    set recordParts to text items of recordText
                    set AppleScript's text item delimiters to old_delim
                    set targetName to item 1 of recordParts
                    set targetCustom to ""
                    if (count of recordParts) ≥ 2 then
                        set targetCustom to item 2 of recordParts
                    end if
                    repeat with candidateWindow in windows
                        set candidateName to (name of candidateWindow as string)
                        set candidateCustom to (custom title of candidateWindow as string)
                        if candidateName is targetName and candidateCustom is targetCustom then
                            set frontmost of candidateWindow to true
                            exit repeat
                        end if
                    end repeat
                end try
            end repeat
            
            activate
            
            return "SUCCESS:" & successCount & "; ERRORS:" & errorList
        end tell
        """

        # AppleScript 실행 (stdin 모드로 전달하여 큰 스크립트도 안정적)
        ret, stdout, stderr = await self.run_shell_command(
            arrange_script, is_applescript=True
        )

        if ret != 0:
            logger.error(f"[macOS] 창 정렬 실행 오류 (returncode={ret}): {stderr}")
            return 0

        # AppleScript 반환값 로깅
        if stdout:
            logger.info(f"[macOS] 창 정렬 결과: {stdout}")
        if stderr:
            logger.debug(f"[macOS] 창 정렬 로그: {stderr}")

        # 성공 카운트 파싱하여 실제로 동작했는지 확인
        if "SUCCESS:0" in stdout:
            # ERRORS 부분을 파싱해 권한 오류인지 구분
            # "Not authorized" / "errAEEventNotPermitted" / "(-1743)" 은 Automation 권한 오류
            # "Can't make «class titl»" 등은 창이 닫히는 중 발생하는 일반 오류 (권한과 무관)
            errors_part = stdout.split("; ERRORS:", 1)[1] if "; ERRORS:" in stdout else ""
            _PERM_KEYWORDS = ("Not authorized", "errAEEventNotPermitted", "(-1743)")
            is_permission_error = any(kw in errors_part for kw in _PERM_KEYWORDS)
            if is_permission_error:
                logger.warning(
                    "[macOS] 창 정렬: 성공한 창이 0개입니다. Automation 권한을 확인하세요."
                )
                logger.warning(
                    "[macOS] 시스템 환경설정 → 개인정보 보호 및 보안 → Automation → Python이 Terminal 제어 허용"
                )
            else:
                logger.debug(
                    "[macOS] 창 정렬: 성공한 창 없음 (권한 오류 아님) — %s",
                    errors_part or "오류 없음",
                )
        else:
            logger.info(f"[macOS] {window_count}개 창 정렬 완료")

        return window_count

    async def cleanup_terminal(self, filter_keyword: str) -> dict[str, object]:
        existing_task = self._cleanup_tasks.get(filter_keyword)
        if existing_task is not None:
            return await existing_task

        cleanup_task = asyncio.create_task(self._cleanup_terminal_once(filter_keyword))
        self._cleanup_tasks[filter_keyword] = cleanup_task
        try:
            return await cleanup_task
        finally:
            if self._cleanup_tasks.get(filter_keyword) is cleanup_task:
                self._cleanup_tasks.pop(filter_keyword, None)

    async def terminate_process_group(self, session) -> Any:
        pgid = getattr(session, "pgid", None)
        if pgid is None:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": ["missing_pgid"],
            }

        completed = subprocess.run(["kill", "-TERM", f"-{pgid}"], capture_output=True)
        returncode = getattr(completed, "returncode", 0)
        stderr = getattr(completed, "stderr", b"")
        if isinstance(stderr, bytes):
            error_text = stderr.decode("utf-8", errors="ignore")
        else:
            error_text = str(stderr)
        return {
            "ok": returncode == 0,
            "matched_windows": [],
            "closed_windows": [],
            "remaining_windows": [],
            "killed_pids": [f"-{pgid}"] if returncode == 0 else [],
            "errors": [] if returncode == 0 else [error_text],
        }

    async def terminate_tty_processes(self, session) -> Any:
        tty = getattr(session, "tty", None)
        if not tty:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": ["missing_tty"],
            }

        dir_name = str(getattr(session, "dir_name", ""))
        tty_short = str(tty).replace("/dev/", "")
        ps_result = subprocess.run(
            ["ps", "-t", tty_short, "-o", "pid="],
            capture_output=True,
            text=True,
        )
        pids = [pid.strip() for pid in ps_result.stdout.strip().split() if pid.strip()]
        if not pids:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": ["no_tty_processes"],
            }

        ancestor_pids = _current_ancestor_pids()
        candidates: List[dict[str, object]] = []
        kill_pids: List[str] = []
        skipped: List[dict[str, object]] = []
        for pid in pids:
            cmdline = _get_process_command(pid)
            cwd = _get_process_cwd(pid)
            pgid = _get_process_pgid(pid)
            skip_reasons = _tty_kill_skip_reasons(pid, cmdline, cwd, pgid, ancestor_pids)
            candidate = {
                "pid": pid,
                "cmdline": cmdline,
                "cwd": cwd,
                "pgid": pgid,
                "skip_reasons": skip_reasons,
            }
            candidates.append(candidate)
            logger.info(
                "[macOS][CLEANUP][TTY_CANDIDATE] dir=%s tty=%s pid=%s pgid=%s cwd=%s cmdline=%s skip_reasons=%s",
                dir_name,
                tty,
                pid,
                pgid,
                cwd or "<unknown>",
                cmdline or "<unknown>",
                skip_reasons,
            )
            if skip_reasons:
                skipped.append(candidate)
            else:
                kill_pids.append(pid)

        if not kill_pids:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "candidate_pids": candidates,
                "skipped_pids": skipped,
                "errors": ["no_safe_tty_processes"],
            }

        for pid in kill_pids:
            subprocess.run(["kill", "-TERM", pid], capture_output=True)

        remaining_pids = list(kill_pids)
        for _ in range(10):
            check_result = subprocess.run(
                ["ps", "-t", tty_short, "-o", "pid="],
                capture_output=True,
                text=True,
            )
            remaining_pids = [
                pid.strip()
                for pid in check_result.stdout.strip().split()
                if pid.strip() in kill_pids
                and not _tty_kill_skip_reasons(
                    pid.strip(),
                    _get_process_command(pid.strip()),
                    _get_process_cwd(pid.strip()),
                    _get_process_pgid(pid.strip()),
                    ancestor_pids,
                )
            ]
            if not remaining_pids:
                return {
                    "ok": True,
                    "matched_windows": [],
                    "closed_windows": [],
                    "remaining_windows": [],
                    "killed_pids": kill_pids,
                    "candidate_pids": candidates,
                    "skipped_pids": skipped,
                    "errors": [],
                }
            await asyncio.sleep(0.1)

        for pid in remaining_pids:
            subprocess.run(["kill", "-KILL", pid], capture_output=True)

        return {
            "ok": True,
            "matched_windows": [],
            "closed_windows": [],
            "remaining_windows": remaining_pids,
            "killed_pids": kill_pids,
            "candidate_pids": candidates,
            "skipped_pids": skipped,
            "errors": [],
        }

    async def close_window(self, session) -> Any:
        window_id = getattr(session, "window_id", None)
        if not window_id:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": ["missing_window_id"],
            }

        window_field_sep = "|||FIELD|||"
        close_script = f'''
tell application "Terminal"
    set closeResults to ""
    repeat with w in windows
        try
            set windowId to (id of w as integer)
            if windowId is in {{{window_id}}} then
                try
                    close w without saving
                    set closeResults to closeResults & "closed:id" & "{window_field_sep}" & (windowId as string) & "{window_field_sep}" & "" & "{window_field_sep}" & "" & linefeed
                on error errMsg
                    set closeResults to closeResults & "failed:id:" & errMsg & "{window_field_sep}" & (windowId as string) & "{window_field_sep}" & "" & "{window_field_sep}" & "" & linefeed
                end try
            end if
        on error errMsg
            set closeResults to closeResults & "inspect_failed:" & errMsg & "{window_field_sep}" & "" & "{window_field_sep}" & "" & "{window_field_sep}" & "" & linefeed
        end try
    end repeat
    return closeResults
end tell
'''
        _, stdout, _ = await self.run_shell_command(close_script, is_applescript=True)
        closed = any(
            line.startswith("closed:id" + window_field_sep + str(window_id))
            for line in (stdout or "").splitlines()
        )
        return {
            "ok": closed,
            "matched_windows": [{"window_id": str(window_id)}],
            "closed_windows": [{"window_id": str(window_id)}] if closed else [],
            "remaining_windows": [] if closed else [{"window_id": str(window_id)}],
            "killed_pids": [],
            "errors": [] if closed else ["window_not_closed"],
        }

    async def close_window_by_window_id(self, window_id: str) -> dict[str, object]:
        if not window_id:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": ["missing_window_id"],
            }

        class _WindowSession:
            def __init__(self, wid: str):
                self.window_id = wid

        session = _WindowSession(window_id)
        return await self.close_window(session)

    async def close_window_by_title_tty(self, custom_title: str, tty: Optional[str]) -> Union[dict[str, object], bool]:
        _ = tty
        if not custom_title:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": ["missing_custom_title"],
            }
        result = await self.cleanup_terminal(custom_title)
        if result is None:
            return True
        return result if isinstance(result, dict) else bool(result)

    async def cleanup_terminal_broad(self, filter_keyword: str) -> Union[dict[str, object], bool]:
        if not filter_keyword:
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": ["missing_filter_keyword"],
            }
        result = await self.cleanup_terminal(filter_keyword)
        if result is None:
            return True
        return result if isinstance(result, dict) else bool(result)

    async def _cleanup_terminal_once(self, filter_keyword: str) -> dict[str, object]:
        """
        [macOS] Terminal 창의 프로세스를 종료하고 창을 닫음.

        전략:
        1. 개별 봇 종료는 exact match 우선 ("dir_name" 또는 "dir_name [executable]")
        2. exact match가 없을 때만 broad contains match fallback 사용
        3. 해당 TTY의 프로세스를 SIGTERM → SIGKILL 후 창 닫기
        """
        try:
            import subprocess as _sp

            exact_match_expr = (
                f'wCustom is "{filter_keyword}" '
                f'or wCustom starts with "{filter_keyword} [" '
                f'or wName is "{filter_keyword}" '
                f'or wName starts with "{filter_keyword} ["'
            )
            broad_match_expr = (
                f'wCustom contains "{filter_keyword}" '
                f'or wName contains "{filter_keyword}"'
            )
            window_field_sep = "|||FIELD|||"

            async def _collect_window_matches(
                match_expr: str,
            ) -> list[tuple[str, str, str, str]]:
                window_script = f'''
tell application "Terminal"
    set windowRecords to ""
    repeat with w in windows
        try
            set windowId to (id of w as integer)
            set wName to (name of w as string)
            set wCustom to (custom title of w as string)
            if {match_expr} then
                set t to (tty of front tab of w as string)
                set windowRecords to windowRecords & (windowId as string) & "{window_field_sep}" & t & "{window_field_sep}" & wCustom & "{window_field_sep}" & wName & linefeed
            end if
        end try
    end repeat
    return windowRecords
end tell
'''
                _, stdout, _ = await self.run_shell_command(
                    window_script, is_applescript=True
                )
                matches: list[tuple[str, str, str, str]] = []
                for line in (stdout or "").splitlines():
                    if not line.strip():
                        continue
                    parts = [part.strip() for part in line.split(window_field_sep)]
                    if len(parts) == 4:
                        window_id, tty, custom_title, window_name = parts
                    elif len(parts) == 3:
                        window_id = ""
                        tty, custom_title, window_name = parts
                    else:
                        continue
                    if tty:
                        matches.append((window_id, tty, custom_title, window_name))
                return matches

            result: dict[str, object] = {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": [],
            }

            matched_windows = await _collect_window_matches(exact_match_expr)
            match_expr = exact_match_expr
            match_mode = "exact"
            if not matched_windows:
                matched_windows = await _collect_window_matches(broad_match_expr)
                match_expr = broad_match_expr
                match_mode = "fallback"

            ttys = [tty for _, tty, _, _ in matched_windows]
            window_ids = [window_id for window_id, _, _, _ in matched_windows if window_id]
            window_metadata = {
                window_id: (tty, custom_title, window_name)
                for window_id, tty, custom_title, window_name in matched_windows
                if window_id
            }
            logger.info(
                f"[macOS][CLEANUP][MATCH] bot={filter_keyword} mode={match_mode} windows={len(matched_windows)} ttys={ttys}"
            )
            for window_id, tty, custom_title, window_name in matched_windows:
                logger.info(
                    f"[macOS][CLEANUP][WINDOW] bot={filter_keyword} mode={match_mode} tty={tty} window_id={window_id or '<none>'} custom={custom_title!r} name={window_name!r}"
                )
            logger.info(f"[macOS] cleanup_terminal: '{filter_keyword}' TTYs={ttys}")
            result["matched_windows"] = [
                {
                    "window_id": window_id,
                    "tty": tty,
                    "custom_title": custom_title,
                    "name": window_name,
                    "match_mode": match_mode,
                }
                for window_id, tty, custom_title, window_name in matched_windows
            ]

            def _collect_pids_by_tty(current_ttys: list[str]) -> dict[str, list[str]]:
                pid_map: dict[str, list[str]] = {}
                for tty in current_ttys:
                    tty_short = tty.replace("/dev/", "")
                    ps_r = _sp.run(
                        ["ps", "-t", tty_short, "-o", "pid="],
                        capture_output=True,
                        text=True,
                    )
                    pid_map[tty] = [
                        pid.strip() for pid in ps_r.stdout.strip().split() if pid.strip()
                    ]
                return pid_map

            use_window_id_close = bool(window_ids) and len(window_ids) == len(matched_windows)

            def _collect_pids(current_ttys: list[str]) -> list[str]:
                current_pids: list[str] = []
                pid_map = _collect_pids_by_tty(current_ttys)
                for tty in current_ttys:
                    current_pids.extend(pid_map.get(tty, []))
                return current_pids

            def _get_pid_command(pid: str) -> str:
                ps_r = _sp.run(
                    ["ps", "-p", pid, "-o", "command="],
                    capture_output=True,
                    text=True,
                )
                command = " ".join(ps_r.stdout.strip().split())
                return command or "<unknown>"

            pid_map: dict[str, list[str]] = {}
            pids: list[str] = []
            if not use_window_id_close:
                pid_map = _collect_pids_by_tty(ttys)
                pids = [pid for tty in ttys for pid in pid_map.get(tty, [])]
                logger.info(f"[macOS] cleanup_terminal: PIDs={pids}")
                for tty in ttys:
                    tty_pids = pid_map.get(tty, [])
                    cmdlines = [f"{pid}:{_get_pid_command(pid)}" for pid in tty_pids]
                    logger.info(
                        f"[macOS][CLEANUP][TTY] bot={filter_keyword} tty={tty} pids={tty_pids} cmdlines={cmdlines}"
                    )

                if pids:
                    for pid in pids:
                        _sp.run(["kill", "-TERM", pid], capture_output=True)

                    remaining_pids = pids
                    for _ in range(10):
                        remaining_pids = _collect_pids(ttys)
                        if not remaining_pids:
                            break
                        await asyncio.sleep(0.1)

                    if remaining_pids:
                        logger.warning(
                            f"[macOS][CLEANUP][ESCALATE] bot={filter_keyword} remaining_after_term={remaining_pids}"
                        )
                        for pid in remaining_pids:
                            _sp.run(["kill", "-KILL", pid], capture_output=True)

                        for _ in range(5):
                            if not _collect_pids(ttys):
                                break
                            await asyncio.sleep(0.1)
                    result["killed_pids"] = pids
            else:
                logger.info("[macOS] cleanup_terminal: window id close 경로로 PID 종료를 건너뜀")

            if not ttys:
                logger.warning(f"[macOS] cleanup_terminal: '{filter_keyword}' 창 없음")
                return result

            if use_window_id_close:
                close_script = f'''
tell application "Terminal"
    set closeResults to ""
    repeat with w in windows
        try
            set windowId to (id of w as integer)
            if windowId is in {{{", ".join(window_ids)}}} then
                try
                    close w without saving
                    set closeResults to closeResults & "closed:id" & "{window_field_sep}" & (windowId as string) & "{window_field_sep}" & "" & "{window_field_sep}" & "" & linefeed
                on error errMsg
                    set closeResults to closeResults & "failed:id:" & errMsg & "{window_field_sep}" & (windowId as string) & "{window_field_sep}" & "" & "{window_field_sep}" & "" & linefeed
                end try
            end if
        on error errMsg
            set closeResults to closeResults & "inspect_failed:" & errMsg & "{window_field_sep}" & "" & "{window_field_sep}" & "" & "{window_field_sep}" & "" & linefeed
        end try
    end repeat
    return closeResults
end tell
'''
            else:
                close_script = f'''
tell application "Terminal"
    set closeResults to ""
    repeat with w in windows
        try
            set wName to (name of w as string)
            set wCustom to (custom title of w as string)
            if {match_expr} then
                try
                    close w without saving
                    set closeResults to closeResults & "closed" & "{window_field_sep}" & "" & "{window_field_sep}" & wCustom & "{window_field_sep}" & wName & linefeed
                on error errMsg
                    set closeResults to closeResults & "failed:" & errMsg & "{window_field_sep}" & "" & "{window_field_sep}" & wCustom & "{window_field_sep}" & wName & linefeed
                end try
            end if
        on error errMsg
            set closeResults to closeResults & "inspect_failed:" & errMsg & "{window_field_sep}" & "" & "{window_field_sep}" & "<unknown>" & "{window_field_sep}" & "<unknown>" & linefeed
        end try
    end repeat
    return closeResults
end tell
'''
            close_lock = self.__class__._get_terminal_gui_close_lock()
            async with close_lock:
                _, stdout, _ = await self.run_shell_command(close_script, is_applescript=True)
            close_results: list[tuple[str, str, str, str]] = []
            for line in (stdout or "").splitlines():
                if not line.strip():
                    continue
                parts = [part.strip() for part in line.split(window_field_sep)]
                if len(parts) == 4:
                    close_results.append((parts[0], parts[1], parts[2], parts[3]))
                elif len(parts) == 3:
                    close_results.append((parts[0], "", parts[1], parts[2]))

            closed_window_keys = {
                window_id or f"{custom_title}\0{window_name}"
                for status, window_id, custom_title, window_name in close_results
                if status.startswith("closed")
            }
            closed_count = len(closed_window_keys)
            result["closed_windows"] = [
                {
                    "status": status,
                    "window_id": window_id,
                    "custom_title": custom_title,
                    "name": window_name,
                }
                for status, window_id, custom_title, window_name in close_results
                if status.startswith("closed")
            ]
            result["errors"] = [
                status
                for status, _, _, _ in close_results
                if not status.startswith("closed")
            ]
            for status, window_id, custom_title, window_name in close_results:
                if window_id and window_id in window_metadata:
                    _, fallback_custom_title, fallback_window_name = window_metadata[window_id]
                    custom_title = custom_title or fallback_custom_title
                    window_name = window_name or fallback_window_name
                log_message = (
                    f"[macOS][CLEANUP][CLOSE] bot={filter_keyword} status={status} "
                    f"window_id={window_id or '<none>'} custom={custom_title!r} name={window_name!r}"
                )
                if status.startswith("closed"):
                    logger.info(log_message)
                else:
                    logger.warning(log_message)

            if len(matched_windows) != closed_count:
                closed_ids = {
                    window_id
                    for status, window_id, _, _ in close_results
                    if status.startswith("closed") and window_id
                }
                if closed_ids:
                    result["remaining_windows"] = [
                        {
                            "window_id": window_id,
                            "tty": tty,
                            "custom_title": custom_title,
                            "name": window_name,
                        }
                        for window_id, tty, custom_title, window_name in matched_windows
                        if window_id not in closed_ids
                    ]
                logger.warning(
                    f"[macOS][CLEANUP][CLOSE_MISMATCH] bot={filter_keyword} expected={len(matched_windows)} actual={closed_count} "
                    f"matched={[(window_id, custom_title, window_name) for window_id, _, custom_title, window_name in matched_windows]}"
                )
            result["ok"] = closed_count > 0 and closed_count >= len(matched_windows)

            logger.info(
                f"[macOS] cleanup_terminal 완료: '{filter_keyword}' 닫힌 창={closed_count}개"
            )
            return result

        except Exception as e:
            logger.warning(f"창 정리 실패: {e}")
            return {
                "ok": False,
                "matched_windows": [],
                "closed_windows": [],
                "remaining_windows": [],
                "killed_pids": [],
                "errors": [str(e)],
            }

    def generate_start_command(
        self, dir_name: str, bot_path: str, executable: str
    ) -> str:
        """
        [macOS] 봇 시작 AppleScript 생성

        Args:
            dir_name: 디렉토리 이름 (봇 이름)
            bot_path: 봇 실행 파일이 있는 경로
            executable: 실행 파일명

        Returns:
            AppleScript 문자열
        """
        if executable.endswith(".py"):
            exec_part = f"python3 -u {executable}"
        else:
            exec_part = f"./{executable}"

        wrapper = PROJECT_ROOT / "scripts" / "log_wrapper.sh"
        wrapper_q = shlex.quote(str(wrapper))
        run_cmd = f"{exec_part} 2>&1 | bash {wrapper_q}"

        return f'''
        tell application "Terminal"
            activate
            try
                set newWindow to do script "cd '{bot_path}'"
                delay 1
                do script "echo '🤖 {dir_name} 봇 시작 중...'" in newWindow
                delay 1
                do script "{run_cmd}" in newWindow

                set bounds of front window to {{100, 100, 800, 600}}
                set visible of front window to true

                set custom title of newWindow to "{dir_name} [{executable}]"
            on error errMsg
                log "Error starting bot: " & errMsg
            end try
        end tell
        '''

    async def start_bot_process(
        self,
        dir_name: str,
        bot_path: str,
        executable: str,
        *,
        launch_spec: Optional[dict] = None,
    ) -> bool:
        """
        [macOS] 봇 프로세스 시작

        Args:
            dir_name: 디렉토리 이름 (봇 이름)
            bot_path: 봇 실행 파일이 있는 경로
            executable: 실행 파일명
            launch_spec: launch_spec dict (cmd, cwd). 제공 시 AppleScript 대신 직접 subprocess 기동.

        Returns:
            시작 성공 여부
        """
        try:
            if launch_spec is not None:
                return await self._start_process_with_spec(launch_spec)
            script = self.generate_start_command(dir_name, bot_path, executable)
            ret, _, err = await self.run_shell_command(script, is_applescript=True)

            if ret == 0:
                return True
            else:
                logger.error(f"macOS 실행 오류: {err}")
                return False
        except Exception as e:
            logger.error(f"macOS 실행 오류: {e}")
            return False

    async def _start_process_with_spec(self, spec: dict) -> bool:
        """launch_spec의 cmd/cwd로 백그라운드 프로세스를 직접 기동 (AppleScript 불필요)."""
        cmd = list(spec["cmd"])
        cwd = spec.get("cwd")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        return proc.pid is not None

    async def run_shell_command(
        self, command: str, is_applescript: bool = False
    ) -> Tuple[int, str, str]:
        """
        [macOS] 쉘 명령어를 비동기로 실행

        Args:
            command: 실행할 명령어 (또는 AppleScript 코드)
            is_applescript: True일 경우 osascript -e로 실행

        Returns:
            (returncode, stdout, stderr) 튜플
        """
        try:
            if is_applescript:
                # AppleScript는 stdin으로 전달 (-e 대신)하여 큰 스크립트도 안정적으로 처리
                cmd = ["osascript", "-"]
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                communicate = process.communicate(command.encode("utf-8"))
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                communicate = process.communicate()

            try:
                stdout, stderr = await asyncio.wait_for(
                    communicate, timeout=SHELL_COMMAND_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning(
                    "[macOS][SUBPROCESS][TIMEOUT] applescript=%s timeout=%.1fs",
                    is_applescript,
                    SHELL_COMMAND_TIMEOUT_SECONDS,
                )
                return -1, "", f"command timed out after {SHELL_COMMAND_TIMEOUT_SECONDS:.1f}s"

            decoded_out = stdout.decode("utf-8").strip()
            decoded_err = stderr.decode("utf-8").strip()

            return_code = process.returncode if process.returncode is not None else -1
            return return_code, decoded_out, decoded_err

        except Exception as e:
            logger.error(f"비동기 커맨드 실행 오류: {e}")
            return -1, "", str(e)

    @staticmethod
    def get_permission_setup_command() -> str:
        """
        접근성 권한 설정을 위한 CLI 명령어 반환

        Returns:
            사용자가 실행해야 할 명령어 문자열
        """
        return """
# macOS 접근성 권한 수동 설정 방법:

## 방법 1: GUI 설정 (권장)
1. 터미널에서 다음 명령어 실행:
   open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'

2. 왼쪽 하단 자물쇠 클릭 → 관리자 비밀번호 입력

3. 오른쪽 목록에서 체크:
   - Terminal (또는 iTerm)
   - Python (또는 python3)
   - Visual Studio Code (VS Code 터미널 사용 시)

## 방법 2: CLI로 빠른 확인
# 현재 권한 상태 확인:
sqlite3 ~/Library/Application\\ Support/com.apple.TCC/TCC.db \\
  "SELECT service, client, allowed FROM access WHERE service='kTCCServiceAccessibility' AND client LIKE '%terminal%' OR client LIKE '%python%';"

## 방법 3: tccutil 사용 (개발자용)
# 권한 초기화 (재설정 필요):
sudo tccutil reset Accessibility

# 특정 앱 권한 추가 (macOS 10.14-10.15에서만 작동, SIP 비활성화 필요):
# 보안상 권장하지 않음

## 참고사항:
# - macOS 11(Big Sur) 이상: SIP(System Integrity Protection) 때문에 직접 DB 수정 불가
# - 권한 변경 후: Python 프로그램 재시작 필요
# - 첫 실행 시 권한 요청 팝업이 자동으로 표시됩니다
"""
