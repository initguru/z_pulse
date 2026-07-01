"""
Windows 플랫폼 핸들러

Phase 3 리팩토링: Windows 전용 창 정렬, 터미널 정리, 프로세스 시작 로직
"""

import asyncio
import ctypes
import logging
import subprocess
from ctypes import wintypes
from typing import Optional, Set, Tuple, List

from .base import PlatformHandler
from z_pulse.utils.grid_calculator import calculate_grid_layout

logger = logging.getLogger(__name__)


class WindowsHandler(PlatformHandler):
    """Windows 플랫폼 전용 핸들러"""

    def __init__(self):
        self.user32 = ctypes.windll.user32
        self.dwmapi = ctypes.windll.dwmapi

    def arrange_windows(
        self,
        target_keywords: Set[str],
        process_name: str
    ) -> int:
        """
        [Windows] 창 정렬 (여백 제거를 위한 DWM 보정 + 최소화 복구 + 맨 앞으로)

        Args:
            target_keywords: 창 제목에서 검색할 키워드 집합
            process_name: 프로세스 이름 (추가 키워드)

        Returns:
            정렬된 창의 개수
        """
        # 검색 키워드에 process_name 추가
        all_keywords = target_keywords | {process_name}

        found_windows: List[Tuple[int, str]] = []

        # 1. 윈도우 열거
        def enum_windows_callback(hwnd, _):
            if not self.user32.IsWindowVisible(hwnd):
                return True

            class_buff = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, class_buff, 256)
            class_name = class_buff.value

            if class_name not in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS"):
                return True

            length = self.user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            buff = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buff, length + 1)
            title = buff.value

            for keyword in all_keywords:
                if keyword in title:
                    found_windows.append((hwnd, title))
                    break
            return True

        CMPFUNC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.user32.EnumWindows(CMPFUNC(enum_windows_callback), 0)

        # 창 정렬 (run_all.* 창을 맨 뒤로, 나머지는 대소문자 무시 오름차순)
        found_windows.sort(key=lambda x: x[1].lower())

        logger.debug(f"정렬된 창 순서 ({len(found_windows)}개): {[t for _, t in found_windows]}")

        window_count = len(found_windows)
        if window_count == 0:
            return 0

        # 2. 작업 영역 가져오기
        RECT = wintypes.RECT
        work_area = RECT()
        self.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work_area), 0)

        screen_width = work_area.right - work_area.left
        screen_height = work_area.bottom - work_area.top
        base_x = work_area.left
        base_y = work_area.top

        # 3. 격자 계산 (유틸리티 함수 사용)
        cols, rows = calculate_grid_layout(window_count)

        # 격자 셀 하나의 크기 (보정 전)
        cell_width = screen_width // cols
        cell_height = screen_height // rows

        flags = 0x0040  # SWP_SHOWWINDOW
        SW_RESTORE = 9
        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        VK_MENU = 0x12  # Alt 키
        KEYEVENTF_KEYUP = 0x0002

        for i, (hwnd, _) in enumerate(found_windows):
            # 투명 테두리(Shadow) 보정 로직
            rect = RECT()
            frame = RECT()

            self.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            hr = self.dwmapi.DwmGetWindowAttribute(
                hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(frame), ctypes.sizeof(frame)
            )

            if hr == 0:  # 성공 시
                border_left = frame.left - rect.left
                border_right = rect.right - frame.right
                border_bottom = rect.bottom - frame.bottom
                border_top = frame.top - rect.top
            else:
                # 실패 시 기본값 (Windows 10/11 표준 근사치)
                border_left, border_right, border_bottom, border_top = 7, 7, 7, 0

            # 격자 위치 계산
            row = i // cols
            col = i % cols

            # 목표하는 '보이는' 위치
            target_visual_x = base_x + (col * cell_width)
            target_visual_y = base_y + (row * cell_height)
            target_visual_w = cell_width
            target_visual_h = cell_height

            # 보정된 시스템 윈도우 좌표 (테두리만큼 더 크고 왼쪽/위로 이동)
            final_x = target_visual_x - border_left
            final_y = target_visual_y - border_top
            final_w = target_visual_w + border_left + border_right
            final_h = target_visual_h + border_top + border_bottom

            # 최소화 복구
            if self.user32.IsIconic(hwnd):
                self.user32.ShowWindow(hwnd, SW_RESTORE)

            # 보정된 좌표로 이동
            self.user32.SetWindowPos(hwnd, 0, final_x, final_y, final_w, final_h, flags)

        # 모든 창을 전경으로 가져오기 (Z-order 역순으로 처리하여 첫 번째 창이 최상단)
        # Alt 키 트릭: Windows의 SetForegroundWindow 보안 제한 우회
        self.user32.keybd_event(VK_MENU, 0, 0, 0)  # Alt 키 누름
        try:
            for hwnd, _ in reversed(found_windows):
                self.user32.SetForegroundWindow(hwnd)
                self.user32.BringWindowToTop(hwnd)
        finally:
            self.user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)  # Alt 키 뗌

        return window_count

    async def cleanup_terminal(self, filter_keyword: str) -> dict[str, object]:
        """
        [Windows] 터미널 창 정리 - MainWindowTitle에 키워드가 포함된 프로세스 종료

        Args:
            filter_keyword: 창 제목에서 검색할 키워드
        """
        try:
            escaped_keyword = filter_keyword.replace("'", "''")
            ps_command = (
                "$matches = @(Get-Process | Where-Object { "
                f"$_.MainWindowTitle -eq '{escaped_keyword}' "
                f"-or $_.MainWindowTitle -like '*{escaped_keyword}*' "
                "}); "
                "$matches | ForEach-Object { Stop-Process -Id $_.Id -Force }; "
                "'MATCHED:' + $matches.Count"
            )
            _, stdout, stderr = await self.run_shell_command(f'powershell -Command "{ps_command}"')
            matched_count = 0
            for line in (stdout or "").splitlines():
                if line.startswith("MATCHED:"):
                    try:
                        matched_count = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        matched_count = 0
            logger.debug(f"Windows 창 정리 수행 (Filter: {filter_keyword}, matched={matched_count})")
            return {
                "ok": matched_count > 0,
                "matched_windows": matched_count,
                "closed_windows": matched_count,
                "remaining_windows": [],
                "killed_pids": [],
                "errors": [stderr] if stderr else [],
            }
        except Exception as e:
            logger.warning(f"창 정리 실패: {e}")
            return {
                "ok": False,
                "matched_windows": 0,
                "closed_windows": 0,
                "remaining_windows": [],
                "killed_pids": [],
                "errors": [str(e)],
            }


    def generate_start_command(
        self,
        dir_name: str,
        bot_path: str,
        executable: str
    ) -> str:
        """
        [Windows] 봇 시작 명령어 생성 (C# Embedding 방식)

        해결책:
        - PowerShell 스크립트 내부에 C# 클래스(BotLogger)를 정의하고 즉석 컴파일합니다.
        - C#은 타입이 명확하므로 파일 경로가 'System.IO.FileStream' 같은 문자열로 오인될 일이 없습니다.
        - 로그 기록, 로테이션, 파일 잠금 해제(FileShare) 등 모든 로직을 C# 내부에서 안전하게 처리합니다.
        """
        import base64

        # 실행 명령어
        run_cmd = f"python -X utf8 -u {executable}" if executable.endswith(".py") else f"./{executable}"

        # 1. QuickEdit 방지 + ANSI 이스케이프 시퀀스 활성화 (C# 코드)
        # STD_INPUT_HANDLE (-10): QuickEdit 비활성화
        # STD_OUTPUT_HANDLE (-11): ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004) 활성화
        console_fix = (
            "$code = '[DllImport(\"kernel32.dll\")] public static extern bool SetConsoleMode(IntPtr h, uint m); "
            "[DllImport(\"kernel32.dll\")] public static extern bool GetConsoleMode(IntPtr h, ref uint m); "
            "[DllImport(\"kernel32.dll\")] public static extern IntPtr GetStdHandle(int n);';"
            "try { "
                "$type = Add-Type -MemberDefinition $code -Name 'Win32' -Namespace 'ConFix' -PassThru;"
                "$hIn = $type::GetStdHandle(-10); $mIn = 0;"
                "$type::GetConsoleMode($hIn, [ref]$mIn);"
                "$type::SetConsoleMode($hIn, ($mIn -band -bnot 0x0040) -bor 0x0080);"
                "$hOut = $type::GetStdHandle(-11); $mOut = 0;"
                "$type::GetConsoleMode($hOut, [ref]$mOut);"
                "$type::SetConsoleMode($hOut, $mOut -bor 0x0004);"
            "} catch {};"
        )

        # 2. [핵심] 로그 처리를 전담할 C# 클래스 소스코드
        # PowerShell 변수나 함수를 쓰지 않고, 완벽한 C# 로직으로 동작합니다.
        csharp_logger_source = r"""
using System;
using System.IO;
using System.Text;

public static class BotLogger {
    private static StreamWriter _w;
    private static string _p;
    private static DateTime _d;

    // 초기화: 파일 열기
    public static void Init(string path) {
        _p = path;
        _d = DateTime.Now.Date;
        Open();
    }

    // 파일 스트림 열기 (공유 모드 설정으로 권한 오류 방지)
    private static void Open() {
        try {
            var fs = new FileStream(_p, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
            _w = new StreamWriter(fs, Encoding.UTF8) { AutoFlush = true };
        } catch {}
    }

    // 로그 기록 및 로테이션 체크
    public static void Log(string s) {
        if (DateTime.Now.Date != _d) Rotate();
        if (_w != null) {
            try { _w.WriteLine(s); } catch {}
        }
    }

    // 로테이션 로직 (닫기 -> 이동 -> 다시 열기, 재시도 포함)
    private static void Rotate() {
        try {
            if (_w != null) { _w.Close(); _w.Dispose(); _w = null; }
            string bak = _p + "." + _d.ToString("yyyyMMdd");
            if (File.Exists(bak)) File.Delete(bak);
            // 일시적 파일 잠금(백신, Indexer 등) 대비 최대 5회 재시도
            for (int i = 0; i < 5; i++) {
                try {
                    if (File.Exists(_p)) File.Move(_p, bak);
                    break;
                } catch {
                    System.Threading.Thread.Sleep(200);
                }
            }
        } catch {}
        _d = DateTime.Now.Date;
        Open();
    }
}
"""
        # 3. PowerShell 스크립트 조립
        ps_script = (
            f"{console_fix} "
            f"$env:PYTHONUNBUFFERED = '1'; "
            f"$env:PYTHONIOENCODING = 'utf-8'; "
            f"$env:PYTHONUTF8 = '1'; "
            # 외부 런타임의 로그 버퍼링 억제 힌트(지원 시 즉시 적용)
            f"$env:BUFFER_LOGS = 'false'; "
            f"$env:AUTO_FLUSH_LOGS = 'true'; "
            f"$env:LOG_BUFFER_MS = '0'; "
            f"$env:LOG_BUFFER_LINES = '1'; "
            f"$env:STDOUT_LINE_BUFFERED = '1'; "
            f"[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "

            # C# 코드 컴파일 (Add-Type)
            f"$loggerSource = @'{csharp_logger_source}'@; "
            f"Add-Type -TypeDefinition $loggerSource -Language CSharp; "

            # 경로 설정 (단순 문자열 조합)
            f"$logPath = \"$((Get-Location).Path)\\monitor.log\"; "

            # C# 로거 초기화
            f"[BotLogger]::Init($logPath); "

            f"Write-Host '🤖 {dir_name} 봇 시작 중...'; "

            # 메인 루프
            f"{run_cmd} 2>&1 | ForEach-Object {{ "
                f"Write-Host $_; " # 화면 출력
                f"[Console]::Out.Flush(); "
                f"[BotLogger]::Log($_); " # 파일 기록 (C# 위임)
            f"}}"
        )

        encoded = base64.b64encode(ps_script.encode('utf-16le')).decode('utf-8')

        return f'start "{dir_name}" /D "{bot_path}" cmd /c "chcp 65001 & powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand {encoded}"'


    async def start_bot_process(
        self,
        dir_name: str,
        bot_path: str,
        executable: str,
        *,
        launch_spec: Optional[dict] = None,
    ) -> bool:
        """
        [Windows] 봇 프로세스 시작

        Args:
            dir_name: 디렉토리 이름 (봇 이름)
            bot_path: 봇 실행 파일이 있는 경로
            executable: 실행 파일명
            launch_spec: launch_spec dict (cmd, cwd). Windows에서는 현재 미사용.

        Returns:
            시작 성공 여부
        """
        try:
            cmd = self.generate_start_command(dir_name, bot_path, executable)
            subprocess.Popen(cmd, shell=True)
            return True
        except Exception as e:
            logger.error(f"Windows 실행 오류: {e}")
            return False

    async def run_shell_command(
        self,
        command: str,
        is_applescript: bool = False
    ) -> Tuple[int, str, str]:
        """
        [Windows] 쉘 명령어를 비동기로 실행

        Args:
            command: 실행할 명령어
            is_applescript: Windows에서는 무시됨

        Returns:
            (returncode, stdout, stderr) 튜플
        """
        if is_applescript:
            return -1, "", "AppleScript is only supported on macOS."

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            # 인코딩 처리 (Windows cp949/euc-kr 자동 감지 강화)
            try:
                decoded_out = stdout.decode('cp949').strip()
                decoded_err = stderr.decode('cp949').strip()
            except UnicodeDecodeError:
                # 실패 시 utf-8 시도 (PowerShell이 utf-8을 뱉는 경우 등)
                decoded_out = stdout.decode('utf-8', errors='ignore').strip()
                decoded_err = stderr.decode('utf-8', errors='ignore').strip()

            return process.returncode, decoded_out, decoded_err

        except Exception as e:
            logger.error(f"비동기 커맨드 실행 오류: {e}")
            return -1, "", str(e)
