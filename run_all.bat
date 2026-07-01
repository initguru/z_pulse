@echo off
title Z-Pulse-starting
setlocal enabledelayedexpansion
chcp 65001 >nul

:: ============================================================
:: Z-Pulse Runner (Windows)
:: run_all.sh 대응 — Windows 완전 일치 포팅
:: ============================================================

echo.
echo Z-Pulse Runner
echo =====================
echo.

:: 스크립트 위치 기준으로 경로 고정 (cwd 또는 직접 경로 실행 모두 지원)
set "Z_PULSE_ROOT=%~dp0"
if "%Z_PULSE_ROOT:~-1%"=="\" set "Z_PULSE_ROOT=%Z_PULSE_ROOT:~0,-1%"
for %%I in ("%Z_PULSE_ROOT%\..") do set "REPO_ROOT=%%~fI"
set "Z_FLOW_ROOT=%REPO_ROOT%\z_flow"
set "Z_FLOW_SCRIPTS_DIR=%Z_FLOW_ROOT%\scripts"
pushd "%Z_PULSE_ROOT%"

:: 1. 필수 파일 확인
if not exist "app.py" (
    echo [ERROR] app.py not found.
    pause
    exit /b 1
)

:: 2. uv 확인 및 가상환경 확인/생성 (uv 관리형 Python 3.11 — brew 독립)
:: run_all.sh: uv 존재 확인 후 .venv 생성/self-heal, uv pip install
where uv >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] uv가 설치되어 있지 않습니다. https://docs.astral.sh/uv/ 를 참조하세요.
    pause
    exit /b 1
)

set "Z_PULSE_VENV=%Z_PULSE_ROOT%\.venv"

:: Self-heal: .venv가 없거나 python이 없으면 재생성
set "_needs_rebuild=false"
if not exist "!Z_PULSE_VENV!\Scripts\python.exe" (
    set "_needs_rebuild=true"
)

if "!_needs_rebuild!"=="true" (
    if exist "!Z_PULSE_VENV!" rmdir /s /q "!Z_PULSE_VENV!"
    echo [INFO] Creating Z-Pulse .venv with uv (Python 3.11^)...
    uv venv --seed --python 3.11 "!Z_PULSE_VENV!"
    if !ERRORLEVEL! NEQ 0 (
        echo [ERROR] .venv 생성 실패.
        pause
        exit /b 1
    )
)

call "!Z_PULSE_VENV!\Scripts\activate.bat"

if not exist "!Z_PULSE_VENV!\.requirements_installed" (
    if exist "%Z_PULSE_ROOT%\requirements.txt" (
        echo [INFO] Installing requirements...
        uv pip install --python "!Z_PULSE_VENV!\Scripts\python.exe" -r "%Z_PULSE_ROOT%\requirements.txt"
        echo. > "!Z_PULSE_VENV!\.requirements_installed"
    )
)
:venv_ready

:: 3. setting.env 로드
:: run_all.sh: IFS='=' 루프, # 주석/빈줄 스킵, 이미 설정된 변수 덮어쓰기 안 함
if exist "%Z_PULSE_ROOT%\setting.env" (
    echo [INFO] Loading setting.env...
    for /f "usebackq eol=# tokens=1* delims==" %%a in ("%Z_PULSE_ROOT%\setting.env") do (
        set "KEY=%%a"
        set "VAL=%%b"
        set "KEY=!KEY: =!"
        if defined VAL (
            set "VAL=!VAL:"=!"
            if not defined !KEY! (
                set "!KEY!=!VAL!"
                echo    Load: !KEY! = !VAL!
            )
        )
    )
    echo [SUCCESS] Settings loaded.
) else (
    echo [WARNING] setting.env not found. Run z_pulse\setup_bot.bat first.
)

:: PYTHONPATH / Z_PULSE_ROOT 설정 (run_all.sh: export Z_PULSE_ROOT, Z_PULSE_PYTHON)
set "Z_PULSE_PYTHON=%Z_PULSE_ROOT%\.venv\Scripts\python.exe"
set "PYTHONPATH=%REPO_ROOT%"

:: LOG_DIR / 날짜별 로그 파일 (run_all.sh: LOG_DIR, LOG_DATE)
for /f "tokens=1-3 delims=/-. " %%a in ('date /t') do (
    set "LOG_DATE=%%c%%a%%b"
)
:: date 포맷이 환경마다 다를 수 있으므로 PowerShell로 안전하게 취득
for /f %%d in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "LOG_DATE=%%d"
set "LOG_DIR=%Z_PULSE_ROOT%\logs"
if not exist "!LOG_DIR!" mkdir "!LOG_DIR!"
set "GRVT_MARKET_DATA_DAEMON_LOG=!LOG_DIR!\grvt_market_data_daemon"
set "BINANCE_MARKET_DATA_DAEMON_LOG=!LOG_DIR!\binance_market_data_daemon"
set "WATCHDOG_PIDFILE=!LOG_DIR!\.grvt_market_data_watchdog.pid"
set "BINANCE_WATCHDOG_PIDFILE=!LOG_DIR!\.binance_market_data_watchdog.pid"

:: 4. 텔레그램 토큰 확인
if "%TELEGRAM_BOT_TOKEN%"=="" (
    echo.
    echo [ERROR] TELEGRAM_BOT_TOKEN is missing.
    echo Please run z_pulse\setup_bot.bat or set it manually.
    echo.
    set /p RETRY="Continue without token? (y/N): "
    if /i "!RETRY!" neq "y" exit /b 1
)

:: 5. TARGET_DIR 기본값 설정
if "%TARGET_DIR%"=="" (
    set "TARGET_DIR=C:\Users\%USERNAME%\Documents\toolkit"
    echo [INFO] Set default TARGET_DIR: !TARGET_DIR!
) else (
    echo [INFO] TARGET_DIR: %TARGET_DIR%
)

:: 6. 기존 프로세스 정리 + 구 CMD 창 닫기
:: run_all.sh: stop_processes.py --exclude-pid $$  +  _check_daemon_lease_alive  +  pkill fallback
echo.
echo [INFO] Cleaning up old processes...

:: 6a. stop_processes.py 호출 (run_all.sh: stop_processes.py --exclude-pid $$)
::     Windows에서는 자기 자신의 PID를 넘기기 어려우므로 bat 자체 PID를 제외
for /f "tokens=2" %%p in ('tasklist /FI "IMAGENAME eq cmd.exe" /FI "STATUS eq running" /NH 2^>nul ^| find "cmd.exe"') do (
    set "SELF_PID=%%p"
    goto :found_self_pid
)
:found_self_pid
"%Z_PULSE_PYTHON%" "%Z_PULSE_ROOT%\scripts\stop_processes.py"
:: (sh과 달리 Windows에서 --exclude-pid는 생략: cmd.exe 자체는 stop_processes.py의 타겟이 아님)

:: 6b. [보강] stop_processes 누락 대비: CommandLine 매칭으로 2차 강제 종료
::     (run_all.sh: pkill fallback for app.py, __main__.py, ws_kline_collector.py)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'z_pulse\\app\.py|z_pulse/__main__|ws_kline_collector\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('  [KILL] PID ' + $_.ProcessId + ' (' + $_.Name + ')') } catch {} }"

:: 6c. market_data_daemon lease 확인 후 조건부 pkill
::     (run_all.sh: _check_daemon_lease_alive() heredoc Python → pkill if no healthy lease)
"%Z_PULSE_PYTHON%" "%Z_FLOW_SCRIPTS_DIR%\check_daemon_lease.py" >nul 2>&1
if !ERRORLEVEL! EQU 0 (
    echo [INFO] 건강한 market_data_daemon lease holder 감지 — 기존 데몬 유지, 강제종료 스킵
) else (
    echo [INFO] lease holder 없음 또는 사망 — market_data_daemon 정리 후 재기동
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and $_.CommandLine -match 'market_data_daemon\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('  [KILL] market_data_daemon PID ' + $_.ProcessId) } catch {} }"
)

:: 1초 대기 (run_all.sh: sleep 1)
timeout /t 1 /nobreak >nul

:: 6d. 구 Z-Pulse CMD 창 닫기 (run_all.sh: macOS osascript 대응 — Windows: taskkill by window title)
::     첫 실행 시에는 해당 창이 없으므로 오류 무시
taskkill /FI "WINDOWTITLE eq Z-Pulse" /T /F >nul 2>&1
echo [SUCCESS] Cleanup done.

:: 7. [Z-FLOW] market_data_daemon watchdog 조건부 실행
:: run_all.sh: Z_FLOW_ENABLED 확인 → GRVT watchdog + Binance watchdog
echo.
set "Z_FLOW_ENABLED_LOWER=!Z_FLOW_ENABLED!"
if /i "!Z_FLOW_ENABLED_LOWER!"=="true" (
    echo [Z-FLOW] Z_FLOW_ENABLED=true - Z-Flow 분봉 수집기 시작...
    set "Z_FLOW_MARKET_DATA_DAEMON_MODE=true"
    set PYTHONIOENCODING=utf-8

    :: 7a. GRVT watchdog 기동 (단일 인스턴스 가드: 기존 watchdog PID 확인 후 중복 방지)
    ::     (run_all.sh: GRVT_MARKET_DATA_WATCHDOG_PIDFILE 가드 + _daemon_watchdog() 백그라운드)
    if exist "!WATCHDOG_PIDFILE!" (
        set /p OLD_WD_PID=<"!WATCHDOG_PIDFILE!"
        tasklist /FI "PID eq !OLD_WD_PID!" /NH 2>nul | find /i "python.exe" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            echo [Z-FLOW] GRVT watchdog already running (PID !OLD_WD_PID!), skipping
            goto :skip_grvt_watchdog
        )
    )
    start "Z-Pulse-grvt-watchdog" "%Z_PULSE_PYTHON%" -u "%Z_PULSE_ROOT%\scripts\daemon_watchdog.py" ^
        "%Z_FLOW_SCRIPTS_DIR%\start_grvt_market_data_daemon.py" ^
        "!GRVT_MARKET_DATA_DAEMON_LOG!" ^
        --pidfile "!WATCHDOG_PIDFILE!"
    echo [Z-FLOW] GRVT market_data_daemon watchdog 시작 요청 완료
    echo [Z-FLOW] TIER 스케줄은 backfill 완료 후 자동 활성화됩니다.
    :skip_grvt_watchdog

    :: 7b. Binance watchdog 기동 (run_all.sh: _binance_daemon_watchdog() 백그라운드)
    if exist "!BINANCE_WATCHDOG_PIDFILE!" (
        set /p OLD_BIN_PID=<"!BINANCE_WATCHDOG_PIDFILE!"
        tasklist /FI "PID eq !OLD_BIN_PID!" /NH 2>nul | find /i "python.exe" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            echo [Z-FLOW] Binance watchdog already running (PID !OLD_BIN_PID!), skipping
            goto :skip_binance_watchdog
        )
    )
    start "Z-Pulse-binance-watchdog" "%Z_PULSE_PYTHON%" -u "%Z_PULSE_ROOT%\scripts\daemon_watchdog.py" ^
        "%Z_FLOW_SCRIPTS_DIR%\start_binance_market_data_daemon.py" ^
        "!BINANCE_MARKET_DATA_DAEMON_LOG!" ^
        --pidfile "!BINANCE_WATCHDOG_PIDFILE!"
    echo [Z-FLOW] Binance market_data_daemon watchdog 시작 요청 완료
    :skip_binance_watchdog

) else (
    echo [Z-FLOW] Z_FLOW_ENABLED != true - Z-Flow 스킵
)

:: 7.5 정식 타이틀 복원 (봇 실행 직전)
:: run_all.sh: 창 제목 "Z-Pulse" 설정 (macOS: osascript, Windows: title 명령)
title Z-Pulse

:: 8. Z-Pulse 실행 (포그라운드)
:: run_all.sh: python -u -m z_pulse (포그라운드)  +  cleanup_terminal trap
echo.
echo Starting Z-Pulse...
echo.
echo ----------------------------------------
echo    Log: z_pulse\logs\z_pulse.log.!LOG_DATE!
echo    Stop: Ctrl+C or close window
echo ----------------------------------------
echo.

set PYTHONIOENCODING=utf-8
"%Z_PULSE_PYTHON%" -m z_pulse

:: 비정상 종료 시 5초 대기 (오류 확인 기회 부여) — run_all.sh: sleep 5 on abnormal exit
:: 정상 종료(exit code 0) 시 즉시 닫기
if !ERRORLEVEL! neq 0 (
    echo.
    echo [WARN] Z-Pulse exited with code !ERRORLEVEL! - closing in 5 sec...
    timeout /t 5 /nobreak >nul
)

:: [정리] Z-Pulse 종료 시 watchdog / daemon 함께 정리
:: run_all.sh: cleanup_terminal() trap — watchdog/ZFLOW/daemon PID 종료
:: Windows: 창 타이틀로 워치독 CMD 창 종료 + Python 프로세스 sweeping
echo [INFO] Cleaning up watchdog / daemon processes...
taskkill /FI "WINDOWTITLE eq Z-Pulse-grvt-watchdog" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Z-Pulse-binance-watchdog" /T /F >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'python.exe') -and $_.CommandLine -match 'daemon_watchdog|start_grvt_market_data_daemon|start_binance_market_data_daemon' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; Write-Host ('  [CLEANUP] PID ' + $_.ProcessId) } catch {} }"
echo [INFO] Cleanup done.
popd
exit
