@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul

:: ============================================================
:: Z-Pulse Setup (Windows)
:: ============================================================

echo.
echo Z-Pulse Setup
echo ====================
echo.

:: 스크립트 위치 기준으로 리포 루트 고정 (cwd 또는 직접 경로 실행 모두 지원)
set "Z_PULSE_ROOT=%~dp0"
if "%Z_PULSE_ROOT:~-1%"=="\" set "Z_PULSE_ROOT=%Z_PULSE_ROOT:~0,-1%"
for %%I in ("%Z_PULSE_ROOT%\..") do set "REPO_ROOT=%%~fI"
pushd "%REPO_ROOT%"

:: 현재 디렉토리 확인
if not exist "z_pulse\app.py" (
    echo [ERROR] z_pulse\app.py 파일이 없습니다.
    echo [INFO] Z-Pulse 프로젝트 폴더 안에서 실행해 주세요.
    pause
    exit /b 1
)

echo [SUCCESS] 위치 확인 완료.
echo.

:: 리포 루트
set "REPO_ROOT=%CD%"

:: ── 기존 설정 읽기 ──
set "BOT_TOKEN="
set "CHAT_ID="
set "TARGET_DIR="
set "PROCESS_NAME="
set "ECONOMIC_ENABLED="
set "ECONOMIC_UPDATE_HOUR="
set "Z_FLOW_ENABLED="
set "Z_FLOW_PATH="
set "MEMORY_ALERT_ENABLED="

if exist "z_pulse\setting.env" (
    echo [INFO] 기존 z_pulse\setting.env 파일을 발견했습니다. 기존 값을 기본값으로 사용합니다.
    for /f "usebackq eol=# tokens=1* delims==" %%a in ("z_pulse\setting.env") do (
        set "_K=%%a"
        set "_V=%%b"
        set "_K=!_K: =!"
        if defined _V (
            set "_V=!_V:"=!"
            if "!_K!"=="TELEGRAM_BOT_TOKEN" set "BOT_TOKEN=!_V!"
            if "!_K!"=="TELEGRAM_CHAT_ID" set "CHAT_ID=!_V!"
            if "!_K!"=="TARGET_DIR" set "TARGET_DIR=!_V!"
            if "!_K!"=="PROCESS_NAME" set "PROCESS_NAME=!_V!"
            if "!_K!"=="ECONOMIC_CALENDAR_ENABLED" set "ECONOMIC_ENABLED=!_V!"
            if "!_K!"=="ECONOMIC_UPDATE_HOUR" set "ECONOMIC_UPDATE_HOUR=!_V!"
            if "!_K!"=="MEMORY_ALERT_ENABLED" set "MEMORY_ALERT_ENABLED=!_V!"
            if "!_K!"=="Z_FLOW_ENABLED" set "Z_FLOW_ENABLED=!_V!"
        )
    )
)

:: ============================================================
:: Section 1: 필수 설정 (COLD LOAD — 재시작 필요)
:: ============================================================
echo.
echo ============================================================
echo  Section 1/4: 필수 설정 (COLD LOAD - 재시작 필요)
echo ============================================================

:: 1-1. 텔레그램 봇 토큰 입력
echo.
echo [INFO] 1-1. 텔레그램 봇 토큰 설정:
echo BotFather에게 /newbot 명령으로 봇을 만드세요.
echo 발급된 토큰 값을 아래에 입력하세요.
:ASK_TOKEN
set "INPUT_TOKEN="
if defined BOT_TOKEN (
    set /p INPUT_TOKEN="봇 토큰 [현재 설정됨]: "
) else (
    set /p INPUT_TOKEN="봇 토큰: "
)

if not defined INPUT_TOKEN (
    if not defined BOT_TOKEN (
        echo [ERROR] 봇 토큰이 입력되지 않았습니다.
        goto ASK_TOKEN
    )
) else (
    set "BOT_TOKEN=!INPUT_TOKEN!"
)

:: 토큰 형식 검증
echo "!BOT_TOKEN!" | findstr ":" >nul
if errorlevel 1 (
    echo [WARNING] 형식이 올바르지 않아 보입니다.
    echo [INFO] 예상 형식: 숫자:문자열
    set /p RETRY="진행하시겠습니까? (y/N): "
    if /i "!RETRY!" neq "y" exit /b 1
)

:: 1-2. 텔레그램 채팅 ID 입력
echo.
echo [INFO] 1-2. 텔레그램 채팅 ID 설정:
echo userinfobot 등을 통해 ID를 확인하세요.
:ASK_CHAT_ID
set "INPUT_CHAT="
if defined CHAT_ID (
    set /p INPUT_CHAT="채팅 ID [현재 !CHAT_ID!]: "
) else (
    set /p INPUT_CHAT="채팅 ID: "
)

if not defined INPUT_CHAT (
    if not defined CHAT_ID (
        echo [ERROR] 채팅 ID가 입력되지 않았습니다.
        goto ASK_CHAT_ID
    )
) else (
    set "CHAT_ID=!INPUT_CHAT!"
)

:: 채팅 ID 숫자 검증
set "INT_CHECK=!CHAT_ID!"
set /a INT_CHECK=INT_CHECK 2>nul
if not "!INT_CHECK!"=="!CHAT_ID!" (
    echo [WARNING] 숫자가 아닙니다.
    set /p RETRY="진행하시겠습니까? (y/N): "
    if /i "!RETRY!" neq "y" exit /b 1
)

:: 1-3. TARGET_DIR 입력
echo.
echo [INFO] 1-3. 감시할 폴더 경로 설정:
echo 실행 파일(.exe)이 있는 폴더 경로입니다.
set "INPUT_DIR="
if defined TARGET_DIR (
    set /p INPUT_DIR="디렉토리 경로 [현재 !TARGET_DIR!]: "
) else (
    set /p INPUT_DIR="디렉토리 경로 [기본값 C:\Users\%USERNAME%\2oolkit]: "
)
if defined INPUT_DIR set "TARGET_DIR=!INPUT_DIR!"
if not defined TARGET_DIR set "TARGET_DIR=C:\Users\%USERNAME%\2oolkit"

:: 디렉토리 존재 확인
if not exist "!TARGET_DIR!" (
    echo [WARNING] 폴더를 찾을 수 없습니다: "!TARGET_DIR!"
    set /p RETRY="진행하시겠습니까? (y/N): "
    if /i "!RETRY!" neq "y" exit /b 1
)

:: 1-4. PROCESS_NAME 입력
echo.
echo [INFO] 1-4. 감시할 파일 이름 설정:
echo 예: 봇 실행 파일명 (확장자 제외)
set "INPUT_PROCESS="
if defined PROCESS_NAME (
    set /p INPUT_PROCESS="프로세스 이름 [현재 !PROCESS_NAME!]: "
) else (
    set /p INPUT_PROCESS="프로세스 이름 [기본값 2oolkit-bot-win-x64]: "
)
if defined INPUT_PROCESS set "PROCESS_NAME=!INPUT_PROCESS!"
if not defined PROCESS_NAME set "PROCESS_NAME=2oolkit-bot-win-x64"

:: ============================================================
:: Section 2: 기능 토글 (COLD LOAD)
:: ============================================================
echo.
echo ============================================================
echo  Section 2/4: 기능 토글 (COLD LOAD - 재시작 필요)
echo ============================================================

:: 2-1. 경제지표 캘린더
echo.
echo [INFO] 2-1. 경제지표 캘린더
echo investing.com에서 경제지표 일정을 가져옵니다.
echo /economic 명령어로 오늘의 주요 지표를 확인할 수 있습니다.
set "USE_ECONOMIC="
if not defined ECONOMIC_ENABLED (
    set /p USE_ECONOMIC="사용하시겠습니까? (Y/n) [기본값 Y]: "
)
if defined ECONOMIC_ENABLED if "!ECONOMIC_ENABLED!"=="true" (
    set /p USE_ECONOMIC="사용하시겠습니까? (Y/n) [현재 Y]: "
)
if defined ECONOMIC_ENABLED if "!ECONOMIC_ENABLED!"=="false" (
    set /p USE_ECONOMIC="사용하시겠습니까? (y/N) [현재 N]: "
)

if /i "!USE_ECONOMIC!"=="n" (
    set "ECONOMIC_ENABLED=false"
) else if /i "!USE_ECONOMIC!"=="y" (
    set "ECONOMIC_ENABLED=true"
) else (
    if not defined ECONOMIC_ENABLED set "ECONOMIC_ENABLED=true"
)

if "!ECONOMIC_ENABLED!"=="true" (
    echo [INFO] 경제지표 캘린더 기능을 활성화합니다.

    echo [INFO] 경제지표 모듈 확인 중...
    python -c "from z_pulse.features.economic_calendar import EconomicCalendarManager; print('[SUCCESS] 모듈 로드 완료')" 2>nul
    if errorlevel 1 (
        echo [WARNING] 경제지표 모듈을 찾을 수 없습니다.
        echo [INFO] 봇 실행 시 자동으로 비활성화됩니다.
    )

    echo.
    echo [INFO] 경제지표 업데이트 시간 설정 (0-23 숫자):
    echo 매일 지정된 시간에 자동으로 업데이트합니다.
    set "INPUT_HOUR="
    if defined ECONOMIC_UPDATE_HOUR (
        set /p INPUT_HOUR="업데이트 시간 [현재 !ECONOMIC_UPDATE_HOUR!시]: "
    ) else (
        set /p INPUT_HOUR="업데이트 시간 [기본값 06시]: "
    )
    if defined INPUT_HOUR set "ECONOMIC_UPDATE_HOUR=!INPUT_HOUR!"
    if not defined ECONOMIC_UPDATE_HOUR set "ECONOMIC_UPDATE_HOUR=06"
    echo [INFO] 매일 !ECONOMIC_UPDATE_HOUR!시에 업데이트됩니다.
) else (
    echo [INFO] 경제지표 캘린더를 사용하지 않습니다.
    if not defined ECONOMIC_UPDATE_HOUR set "ECONOMIC_UPDATE_HOUR=06"
)

:: 2-2. 메모리 경고
echo.
echo [INFO] 2-2. 메모리 경고
echo 메모리 사용량 임계값 초과 시 텔레그램 경고
set "USE_MEMORY="
if not defined MEMORY_ALERT_ENABLED (
    set /p USE_MEMORY="활성화 (y/N) [기본값 N]: "
)
if defined MEMORY_ALERT_ENABLED if "!MEMORY_ALERT_ENABLED!"=="true" (
    set /p USE_MEMORY="활성화 (Y/n) [현재 Y]: "
)
if defined MEMORY_ALERT_ENABLED if "!MEMORY_ALERT_ENABLED!"=="false" (
    set /p USE_MEMORY="활성화 (y/N) [현재 N]: "
)
if /i "!USE_MEMORY!"=="y" (
    set "MEMORY_ALERT_ENABLED=true"
) else if /i "!USE_MEMORY!"=="n" (
    set "MEMORY_ALERT_ENABLED=false"
) else (
    if not defined MEMORY_ALERT_ENABLED set "MEMORY_ALERT_ENABLED=false"
)

:: ============================================================
:: Section 3: Z-Flow 연동 설정 (선택)
:: ============================================================
echo.
echo ============================================================
echo  Section 3/4: Z-Flow 연동 설정 (선택)
echo ============================================================
echo.
echo [INFO] Z-Flow 매매전략을 연동하려면 z_pulse\ 와 z_flow\ 를 같은 상위 디렉토리에 둔 뒤 활성화하세요.
echo [INFO] 활성화 시 sibling z_flow 경로를 자동 감지해 Z_FLOW_PATH로 기록합니다.
echo.
set "USE_ZFLOW="
if not defined Z_FLOW_ENABLED (
    set /p USE_ZFLOW="활성화 (y/N) [기본값 N]: "
)
if defined Z_FLOW_ENABLED if "!Z_FLOW_ENABLED!"=="true" (
    set /p USE_ZFLOW="활성화 (Y/n) [현재 Y]: "
)
if defined Z_FLOW_ENABLED if "!Z_FLOW_ENABLED!"=="false" (
    set /p USE_ZFLOW="활성화 (y/N) [현재 N]: "
)

if /i "!USE_ZFLOW!"=="y" (
    set "Z_FLOW_ENABLED=true"
) else if /i "!USE_ZFLOW!"=="n" (
    echo [INFO] Z-Flow 연동을 비활성화합니다.
    set "Z_FLOW_ENABLED=false"
) else (
    if not defined Z_FLOW_ENABLED set "Z_FLOW_ENABLED=false"
)
if "!Z_FLOW_ENABLED!"=="true" (
    set "CANDIDATE_PATH=!REPO_ROOT!\z_flow"
    if exist "!CANDIDATE_PATH!\" (
        set "Z_FLOW_PATH=!CANDIDATE_PATH!"
        echo [OK] Z-Flow sibling 디렉토리 확인됨: !Z_FLOW_PATH!
    ) else (
        echo [ERROR] z_flow 디렉토리를 찾을 수 없습니다: !CANDIDATE_PATH!
        echo [INFO] z_pulse\ 와 z_flow\ 는 같은 상위 디렉토리 아래에 있어야 합니다.
        echo [INFO] Z-Flow 연동을 비활성화합니다.
        set "Z_FLOW_ENABLED=false"
        set "Z_FLOW_PATH="
    )
)

:: ============================================================
:: Section 5: Python 환경 설정
:: ============================================================
echo.
echo ============================================================
echo  Section 4/4: Python 환경 설정
echo ============================================================

:: 가상환경 확인 및 생성
echo.
echo [INFO] Python 가상환경을 확인합니다...

where uv >nul 2>&1
if !ERRORLEVEL! NEQ 0 (
    echo [ERROR] uv가 설치되어 있지 않습니다. https://docs.astral.sh/uv/ 를 참조하세요.
    pause
    exit /b 1
)

if not exist "z_pulse\.venv" (
    set /p CREATE_VENV="가상환경을 생성할까요? (Y/n): "
    if /i "!CREATE_VENV!"=="n" (
        echo [INFO] 건너뜁니다.
    ) else (
        echo [INFO] 생성 중입니다 (uv, Python 3.11^)...
        uv venv --seed --python 3.11 z_pulse\.venv
        if errorlevel 1 (
            echo [ERROR] 생성 실패. uv 설치를 확인하세요.
            pause
            exit /b 1
        )
        echo [SUCCESS] 생성 완료.
    )
) else (
    echo [SUCCESS] 가상환경이 이미 존재합니다.
)

:: 의존성 패키지 설치
echo.
echo [INFO] 필요한 패키지를 확인합니다...

if exist "z_pulse\.venv" (
    echo [INFO] 가상환경 활성화...
    call z_pulse\.venv\Scripts\activate.bat

    if exist "z_pulse\requirements.txt" (
        echo [INFO] 패키지 설치 중...
        set UV_LINK_MODE=copy
        uv pip install --python z_pulse\.venv\Scripts\python.exe -r z_pulse\requirements.txt
    ) else (
        echo [WARNING] z_pulse\requirements.txt 파일이 없습니다.
    )
) else (
    if exist "z_pulse\requirements.txt" (
        set UV_LINK_MODE=copy
        uv pip install --python z_pulse\.venv\Scripts\python.exe -r z_pulse\requirements.txt
    )
)

:: ============================================================
:: setting.env 생성
:: ============================================================
echo.
echo ============================================================
echo  setting.env 파일 생성
echo ============================================================
echo.
echo [INFO] 설정 파일을 생성합니다...

:: 데이터 디렉토리 생성
if not exist "z_pulse\data" mkdir "z_pulse\data"

(
echo # Z-Pulse 설정 파일
echo # 이 파일은 z_pulse\setup_bot.bat 또는 수동으로 편집할 수 있습니다.
echo.
echo # ========================================
echo TARGET_DIR="!TARGET_DIR!"
echo PROCESS_NAME="!PROCESS_NAME!"
echo TELEGRAM_BOT_TOKEN="!BOT_TOKEN!"
echo TELEGRAM_CHAT_ID="!CHAT_ID!"
echo ECONOMIC_CALENDAR_ENABLED=!ECONOMIC_ENABLED!
echo ECONOMIC_UPDATE_HOUR=!ECONOMIC_UPDATE_HOUR!
echo.
echo # ========================================
echo MEMORY_ALERT_ENABLED=!MEMORY_ALERT_ENABLED!
echo.
echo # pair_trading 전략 전용 키는 z_flow\strategy\pair_trading\setting.env 에서 관리합니다.
echo # (품질 필터 / Backtest Quality 키는 z_pulse\setting.env에 재주입하지 않습니다.)
echo.
echo # ========================================
echo RAPID_ENTRY_GUARD_FORCE_PHRASES="최초 진입,추가 진입"
echo.
echo # ========================================
echo # [SLOT BOT] 독립 프로세스 Z-Flow 설정
echo # ========================================
echo Z_FLOW_ENABLED=!Z_FLOW_ENABLED!
if "!Z_FLOW_ENABLED!"=="true" echo Z_FLOW_PATH="!Z_FLOW_PATH!"
) > z_pulse\setting.env

echo [SUCCESS] z_pulse\setting.env 파일이 생성되었습니다.

:: 파일 내용 확인
echo.
echo [INFO] 생성된 setting.env 주요 설정:
echo ========================================
findstr /R "^TARGET_DIR= ^PROCESS_NAME= ^TELEGRAM_ ^ECONOMIC_ ^MEMORY_ALERT ^Z_FLOW_ENABLED ^Z_FLOW_PATH" z_pulse\setting.env
echo ========================================

:: 최종 안내
echo.
echo [SUCCESS] 모든 설정이 완료되었습니다.
echo.
echo [INFO] 실행 명령어:
echo    z_pulse\run_all.bat
echo.
echo [INFO] 설정 변경:
echo 설정 변경: 이 스크립트를 다시 실행하거나 setting.env 직접 수정
echo.
pause
