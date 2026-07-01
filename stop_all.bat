@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

:: ============================================================
:: Z-Pulse Stopper (Windows)
:: stop_all.sh 대응 — stop_processes.py 호출 + daemon/watchdog 추가 정리
:: ============================================================

echo.
echo Z-Pulse Stopper
echo ======================
echo.

:: 프로젝트 루트로 이동 (z_pulse 폴더 안 또는 프로젝트 루트에서 실행 모두 지원)
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%.."

:: 가상환경 활성화 (필요 시)
if not defined VIRTUAL_ENV (
    if exist "z_pulse\.venv" (
        call z_pulse\.venv\Scripts\activate.bat
    )
)

:: stop_processes.py: z_pulse, ws_kline_collector 종료
python z_pulse\scripts\stop_processes.py

:: [보강] watchdog / daemon 프로세스 추가 종료
:: stop_all.sh 대응: run_all.sh가 기동하는 모든 백그라운드 프로세스를 종료한다.
::   - start_grvt_market_data_daemon.py
::   - start_binance_market_data_daemon.py
::   - daemon_watchdog.py (GRVT / Binance)
echo [INFO] Stopping watchdog / daemon processes...
taskkill /FI "WINDOWTITLE eq Z-Pulse-grvt-watchdog" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Z-Pulse-binance-watchdog" /T /F >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'daemon_watchdog|start_grvt_market_data_daemon|start_binance_market_data_daemon|market_data_daemon\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('  [KILL] PID ' + $_.ProcessId + ' (' + $_.CommandLine + ')') } catch {} }"
echo [SUCCESS] All processes stopped.

echo.
echo Done. Press Enter to exit.
pause >nul
