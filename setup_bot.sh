#!/bin/bash

echo "Z-Pulse 모니터링 시스템 설정 도구"
echo "========================================"

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# 함수: 색상 출력
print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_section() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

resolve_python_bin() {
    if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
        echo "$SCRIPT_DIR/.venv/bin/python3"
        return 0
    fi

    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        echo "$SCRIPT_DIR/.venv/bin/python"
        return 0
    fi

    if command -v python3 >/dev/null 2>&1; then
        command -v python3
        return 0
    fi

    return 1
}

run_economic_calendar_init() {
    if [ "$ECONOMIC_ENABLED" != "true" ]; then
        return 0
    fi

    local python_bin
    if ! python_bin="$(resolve_python_bin)"; then
        print_warning "경제지표 초기화를 건너뜁니다. 사용 가능한 Python 환경을 찾지 못했습니다. 런타임 부트스트랩에서 다시 시도합니다."
        return 0
    fi

    print_section "경제지표 캘린더 초기화"
    print_info "초기 캘린더 수집을 진행합니다..."

    if PYTHONPATH="$SCRIPT_DIR/..${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" - <<'PYEOF'
import asyncio
import sys

from z_pulse.features.economic_calendar import EconomicCalendarManager


async def main():
    manager = EconomicCalendarManager('economic_calendar.db')

    try:
        results = await manager.update_calendar(days_ahead=14)
        status = manager.get_status_summary()
        event_count = len(manager.get_upcoming_events(14))

        print(f"total: {results.get('total', 0)}")
        print(f"last_attempt_at: {status.get('last_attempt_at', '')}")
        print(f"last_success_at: {status.get('last_success_at', '')}")
        print(f"event_count: {event_count}")
        if status.get('last_error'):
            print(f"last_error: {status['last_error']}")
            return 1
        return 0
    except Exception as exc:
        print("total: 0")
        print("last_attempt_at: ")
        print("last_success_at: ")
        print("event_count: 0")
        print(f"last_error: {exc}")
        return 1
    finally:
        await manager.close()


sys.exit(asyncio.run(main()))
PYEOF
    then
        print_success "경제지표 캘린더 초기화가 완료되었습니다."
    else
        print_warning "경제지표 캘린더 초기화 중 경고가 발생했습니다. 런타임 부트스트랩에서 다시 시도합니다."
    fi
}

# ── 기본값 초기화 (빈 문자열 = 파일에서 미발견, 프롬프트 후 기본값 적용) ──
SETTING_ENV_DIR=""
BOT_TOKEN=""
CHAT_ID=""
TARGET_DIR=""
PROCESS_NAME=""
ECONOMIC_ENABLED=""
ECONOMIC_UPDATE_HOUR=""
MEMORY_ALERT_ENABLED=""

# Z-Flow 기본값 (빈 문자열 = 파일에서 미발견)
Z_FLOW_ENABLED=""
Z_FLOW_PATH=""

# ── 프로젝트 루트 ──
# 항상 스크립트 자체의 디렉토리(= Z-Pulse 루트)를 사용
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "app.py" ]; then
    print_error "app.py 파일을 찾을 수 없습니다."
    print_info "이 스크립트는 Z-Pulse 루트 디렉토리에서 실행해야 합니다."
    exit 1
fi

print_success "Z-Pulse 루트에서 실행 중입니다."

# ── 기존 설정 읽기 ──
if [ -f "setting.env" ]; then
    print_info "기존 setting.env 파일을 발견했습니다. 기존 값을 기본값으로 사용합니다."
    # 기존 값 읽기 (간단한 파서)
    while IFS='=' read -r key value; do
        key=$(echo "$key" | xargs)
        value=$(echo "$value" | sed 's/^"//' | sed 's/"$//')
        if [[ -n "$key" && ! "$key" =~ ^# && -n "$value" ]]; then
            case "$key" in
                TELEGRAM_BOT_TOKEN) BOT_TOKEN="$value" ;;
                TELEGRAM_CHAT_ID) CHAT_ID="$value" ;;
                TARGET_DIR) TARGET_DIR="$value" ;;
                PROCESS_NAME) PROCESS_NAME="$value" ;;
                ECONOMIC_CALENDAR_ENABLED) ECONOMIC_ENABLED="$value" ;;
                ECONOMIC_UPDATE_HOUR) ECONOMIC_UPDATE_HOUR="$value" ;;
                MEMORY_ALERT_ENABLED) MEMORY_ALERT_ENABLED="$value" ;;
                Z_FLOW_ENABLED) Z_FLOW_ENABLED="$value" ;;
            esac
        fi
    done < <(grep -v '^#' setting.env | grep '=')
fi

# ══════════════════════════════════════════════════════════════════════
# Section 1: 필수 설정
# ══════════════════════════════════════════════════════════════════════
print_section "Section 1/4: 필수 설정"

# 1-1. 텔레그램 봇 토큰
echo ""
print_info "1-1. 텔레그램 봇 토큰을 입력하세요:"
echo "@BotFather에게 /newbot 명령어를 보내서 봇을 생성하세요"
if [ -n "$BOT_TOKEN" ]; then
    _HINT=" [현재 ${BOT_TOKEN:0:10}...${BOT_TOKEN: -5}]"
else
    _HINT=""
fi
read -p "봇 토큰${_HINT}: " INPUT
if [ -n "$INPUT" ]; then
    BOT_TOKEN="$INPUT"
fi

if [ -z "$BOT_TOKEN" ]; then
    print_error "봇 토큰이 입력되지 않았습니다."
    exit 1
fi

if [[ ! "$BOT_TOKEN" =~ ^[0-9]+:[A-Za-z0-9_-]+$ ]]; then
    print_warning "봇 토큰 형식이 올바르지 않을 수 있습니다."
    print_info "올바른 형식: 숫자:문자열 (예: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz)"
    read -p "계속하시겠습니까? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# 1-2. 텔레그램 채팅 ID
echo ""
print_info "1-2. 텔레그램 채팅 ID를 입력하세요:"
echo "@userinfobot에게 메시지를 보내서 자신의 ID를 확인하세요"
if [ -n "$CHAT_ID" ]; then
    _HINT=" [현재 $CHAT_ID]"
else
    _HINT=""
fi
read -p "채팅 ID${_HINT}: " INPUT
if [ -n "$INPUT" ]; then
    CHAT_ID="$INPUT"
fi

if [ -z "$CHAT_ID" ]; then
    print_error "채팅 ID가 입력되지 않았습니다."
    exit 1
fi

if [[ ! "$CHAT_ID" =~ ^[0-9]+$ ]]; then
    print_warning "채팅 ID는 숫자여야 합니다."
fi

# 1-3. TARGET_DIR
echo ""
print_info "1-3. 모니터링할 프로그램들이 있는 디렉토리를 입력하세요:"
echo "Z-Pulse가 감시할 실행 파일들이 있는 상위 디렉토리입니다"
if [ -n "$TARGET_DIR" ]; then
    _HINT=" [현재 $TARGET_DIR]"
else
    _HINT=" [기본값 ~/2oolkit]"
fi
read -p "디렉토리 경로${_HINT}: " INPUT
if [ -n "$INPUT" ]; then
    TARGET_DIR="$INPUT"
fi

if [ -z "$TARGET_DIR" ]; then
    TARGET_DIR="$HOME/2oolkit"
fi

# 1-4. PROCESS_NAME
echo ""
print_info "1-4. 감시할 프로세스 이름을 입력하세요:"
if [ -n "$PROCESS_NAME" ]; then
    _HINT=" [현재 $PROCESS_NAME]"
else
    _HINT=" [기본값 2oolkit-bot-macos-arm64]"
fi
read -p "프로세스 이름${_HINT}: " INPUT
if [ -n "$INPUT" ]; then
    PROCESS_NAME="$INPUT"
fi
if [ -z "$PROCESS_NAME" ]; then
    PROCESS_NAME="2oolkit-bot-macos-arm64"
fi

# ══════════════════════════════════════════════════════════════════════
# Section 2: 기능 토글
# ══════════════════════════════════════════════════════════════════════
print_section "Section 2/4: 기능 토글"

# 2-1. 경제지표 캘린더
echo ""
print_info "2-1. 경제지표 캘린더"
echo "investing.com에서 경제지표 일정을 가져옵니다."
if [ -n "$ECONOMIC_ENABLED" ]; then
    if [ "$ECONOMIC_ENABLED" = "true" ]; then
        _ECON_HINT="(Y/n) [현재 Y]"
    else
        _ECON_HINT="(y/N) [현재 N]"
    fi
else
    _ECON_HINT="(Y/n) [기본값 Y]"
fi
read -p "활성화 $_ECON_HINT: " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    ECONOMIC_ENABLED="true"
elif [[ $REPLY =~ ^[Nn]$ ]]; then
    ECONOMIC_ENABLED="false"
fi
if [ -z "$ECONOMIC_ENABLED" ]; then
    ECONOMIC_ENABLED="true"
fi

if [ "$ECONOMIC_ENABLED" = "true" ]; then
    if [ -n "$ECONOMIC_UPDATE_HOUR" ]; then
        _HOUR_HINT=" [현재 ${ECONOMIC_UPDATE_HOUR}시]"
    else
        _HOUR_HINT=" [기본값 06시]"
    fi
    read -p "업데이트 시간 (0-23)${_HOUR_HINT}: " INPUT_HOUR
    if [ -n "$INPUT_HOUR" ]; then
        ECONOMIC_UPDATE_HOUR="$INPUT_HOUR"
    fi
    if [ -z "$ECONOMIC_UPDATE_HOUR" ]; then
        ECONOMIC_UPDATE_HOUR="06"
    fi
    print_info "매일 ${ECONOMIC_UPDATE_HOUR}시에 업데이트됩니다."
fi

# 2-2. 메모리 경고
echo ""
print_info "2-2. 메모리 경고 알림"
echo "메모리 사용량 임계치 초과 시 텔레그램 알림"
if [ -n "$MEMORY_ALERT_ENABLED" ]; then
    if [ "$MEMORY_ALERT_ENABLED" = "true" ]; then
        _MEM_HINT="(Y/n) [현재 Y]"
    else
        _MEM_HINT="(y/N) [현재 N]"
    fi
else
    _MEM_HINT="(y/N) [기본값 N]"
fi
read -p "활성화 $_MEM_HINT: " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    MEMORY_ALERT_ENABLED="true"
elif [[ $REPLY =~ ^[Nn]$ ]]; then
    MEMORY_ALERT_ENABLED="false"
fi
if [ -z "$MEMORY_ALERT_ENABLED" ]; then
    MEMORY_ALERT_ENABLED="false"
fi

# ══════════════════════════════════════════════════════════════════════
# Section 3: Z-Flow 연동 설정 (선택)
# ══════════════════════════════════════════════════════════════════════
print_section "Section 3/4: Z-Flow 연동 설정 (선택)"

echo ""
print_info "Z-Flow 매매전략을 연동하려면 z_pulse/ 와 z_flow/ 를 같은 상위 디렉토리에 둔 뒤 활성화하세요."
print_info "활성화 시 sibling z_flow 경로를 자동 감지해 Z_FLOW_PATH로 기록합니다."
echo ""
if [ -n "$Z_FLOW_ENABLED" ]; then
    if [ "$Z_FLOW_ENABLED" = "true" ]; then
        _ZFLOW_HINT="(Y/n) [현재 Y]"
    else
        _ZFLOW_HINT="(y/N) [현재 N]"
    fi
else
    _ZFLOW_HINT="(y/N) [기본값 N]"
fi
read -p "활성화 $_ZFLOW_HINT: " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    Z_FLOW_ENABLED="true"
elif [[ $REPLY =~ ^[Nn]$ ]]; then
    print_info "Z-Flow 연동을 비활성화합니다."
    Z_FLOW_ENABLED="false"
fi
# 빈 입력: 현재값 유지 또는 기본값(N) 적용
if [ -z "$Z_FLOW_ENABLED" ]; then
    Z_FLOW_ENABLED="false"
fi
if [ "$Z_FLOW_ENABLED" = "true" ]; then
    CANDIDATE_PATH="$REPO_ROOT/z_flow"
    if [ -d "$CANDIDATE_PATH" ]; then
        Z_FLOW_PATH="$CANDIDATE_PATH"
        print_success "Z-Flow sibling 디렉토리 확인됨: $Z_FLOW_PATH"
    else
        print_error "z_flow 디렉토리를 찾을 수 없습니다: $CANDIDATE_PATH"
        print_info "z_pulse/ 와 z_flow/ 는 같은 상위 디렉토리 아래에 있어야 합니다."
        print_info "Z-Flow 연동을 비활성화합니다."
        Z_FLOW_ENABLED="false"
        Z_FLOW_PATH=""
    fi
fi

# ══════════════════════════════════════════════════════════════════════
# Section 4: Python 환경 설정
# ══════════════════════════════════════════════════════════════════════
print_section "Section 4/4: Python 환경 설정"

# 가상환경 확인 및 생성
echo ""
print_info "Python 가상환경을 확인합니다..."

if ! command -v uv >/dev/null 2>&1; then
    print_error "uv가 설치되어 있지 않습니다. 다음 명령으로 설치하세요: brew install uv"
    exit 1
fi

if [ ! -d ".venv" ]; then
    print_warning "가상환경이 없습니다. 생성하시겠습니까?"
    read -p "가상환경을 생성할까요? (Y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        print_info "가상환경 생성을 건너뜁니다."
    else
        print_info "가상환경을 생성합니다 (uv, Python 3.11)..."
        uv venv --seed --python 3.11 .venv
        print_success "가상환경이 생성되었습니다."
    fi
else
    print_success "가상환경이 이미 존재합니다."
fi

# 의존성 패키지 설치
echo ""
print_info "필요한 패키지를 확인합니다..."

if [ -d ".venv" ]; then
    print_info "가상환경을 활성화합니다..."
    source .venv/bin/activate

    if [ -f "requirements.txt" ]; then
        print_info "requirements.txt에서 패키지를 설치합니다..."
        UV_LINK_MODE=copy uv pip install --python .venv/bin/python -r requirements.txt
        print_success "패키지 설치가 완료되었습니다."
    else
        print_warning "requirements.txt 파일이 없습니다."
    fi
else
    print_warning "가상환경이 없습니다. 시스템 Python을 사용합니다."
fi

# ══════════════════════════════════════════════════════════════════════
# setting.env 생성
# ══════════════════════════════════════════════════════════════════════
print_section "setting.env 파일 생성"

# 데이터 디렉토리 생성
mkdir -p "$SCRIPT_DIR/data"

cat > setting.env << ENVEOF
# Z-Pulse 설정 파일
# 생성일: $(date '+%Y-%m-%d %H:%M')
# 이 파일은 z_pulse/setup_bot.sh 또는 수동으로 편집할 수 있습니다.

TARGET_DIR="$TARGET_DIR"
PROCESS_NAME="$PROCESS_NAME"
TELEGRAM_BOT_TOKEN="$BOT_TOKEN"
TELEGRAM_CHAT_ID="$CHAT_ID"
ECONOMIC_CALENDAR_ENABLED=$ECONOMIC_ENABLED
ECONOMIC_UPDATE_HOUR=$ECONOMIC_UPDATE_HOUR
MEMORY_ALERT_ENABLED=$MEMORY_ALERT_ENABLED

# pair_trading 전략 전용 키는 z_flow/strategy/pair_trading/setting.env 에서 관리합니다.
# (품질 필터 / Backtest Quality 키는 z_pulse/setting.env에 재주입하지 않습니다.)

RAPID_ENTRY_GUARD_FORCE_PHRASES="최초 진입,추가 진입"

# ========================================
# [SLOT BOT] 독립 프로세스 Z-Flow 설정
# ========================================
Z_FLOW_ENABLED=$Z_FLOW_ENABLED
ENVEOF

if [ "$Z_FLOW_ENABLED" = "true" ]; then
    cat >> setting.env << ENVEOF
Z_FLOW_PATH="$Z_FLOW_PATH"
ENVEOF
fi

print_success "setting.env 파일이 생성되었습니다."

run_economic_calendar_init

# 파일 내용 확인
echo ""
print_info "📄 생성된 setting.env 주요 설정:"
echo "========================================"
grep -E '^(TARGET_DIR|PROCESS_NAME|TELEGRAM_|ECONOMIC_|MEMORY_ALERT|Z_FLOW_ENABLED|Z_FLOW_PATH|MARGIN_MAX)=' setting.env
echo "========================================"

# 실행 권한 확인
echo ""
print_info "실행 권한을 확인합니다..."

if [ -d "$TARGET_DIR" ]; then
    find "$TARGET_DIR" -name "$PROCESS_NAME" -type f -exec chmod +x {} \; 2>/dev/null
    print_success "실행 파일 권한을 설정했습니다."
fi

# 최종 안내
echo ""
print_success "설정이 완료되었습니다!"
echo ""
print_info "다음 명령어로 Z-Pulse를 실행할 수 있습니다:"
echo ""
echo "   # 통합 실행 (권장) — Z-Pulse + ws_kline_collector"
echo "   ./run_all.sh"
echo ""
echo "   # 또는 개별 실행"
echo "   python app.py"
echo ""
print_info "📱 텔레그램 봇 사용법:"
echo "   /status  - 프로세스 상태 확인"
echo "   /restart - 중단된 프로세스 재시작"
echo "   /help    - 전체 명령어 목록"
echo ""
print_info "설정 변경:"
echo "설정 변경: 이 스크립트를 다시 실행하거나 setting.env 직접 수정"
echo ""
