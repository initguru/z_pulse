#!/bin/bash

# ============================================================
# Z-Pulse Runner (macOS/Linux) — 독립 실행
# ============================================================
# - 가상환경 확인 및 활성화
# - setting.env 로드
# - 기존 프로세스 정리 (stop_processes.py 연동)
# - market_data_daemon watchdog 조건부 실행 (Z_FLOW_ENABLED)
# - Z-Pulse 포그라운드 실행
#
# 사용법:
#   cd z_pulse && ./run_all.sh
#   또는: /path/to/z_pulse/run_all.sh

set -e
set -o pipefail

# z_pulse 디렉토리로 이동 (어디서 실행하든 동일하게 동작)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
Z_PULSE_ROOT="$SCRIPT_DIR"
REPO_ROOT="$(cd "$Z_PULSE_ROOT/.." && pwd)"
cd "$Z_PULSE_ROOT"

LOG_DIR="$Z_PULSE_ROOT/logs"
LOG_DATE="$(date +%Y%m%d)"
LOG_FILE="$LOG_DIR/z_pulse.log.$LOG_DATE"
LATEST_LOG_LINK="$Z_PULSE_ROOT/z_pulse.log"

mkdir -p "$LOG_DIR"
touch "$LOG_FILE"
ln -sfn "logs/z_pulse.log.$LOG_DATE" "$LATEST_LOG_LINK"

exec > >(tee -a "$LOG_FILE")
exec 2>&1

# macOS: 현재 Terminal 창 닫기
# bash 가 exit 한 뒤 프로세스가 없는 상태에서 닫아야 팝업이 없음.
# disown 으로 job table 에서 제거한 고아 프로세스가 bash 종료 후 창을 닫는다.
_rotate_log() {
    # stdin을 매 줄 현재 날짜의 <base>.log.YYYYMMDD 파일에 append.
    # 호출: > >(_rotate_log "/path/to/logfile_base") 2>&1
    # process substitution을 통해 Python PID·exit code를 보존한다.
    local base="$1"
    local cur_date="" file=""
    while IFS= read -r line; do
        local today; today=$(date +%Y%m%d)
        if [[ "$today" != "$cur_date" ]]; then
            cur_date="$today"
            file="${base}.log.${cur_date}"
        fi
        printf '%s\n' "$line" >> "$file"
    done
    # EOF (데몬 종료) 시 자연 종료
}

_close_this_terminal() {
    if [[ "$(uname -s)" != "Darwin" ]]; then
        return
    fi
    _close_own="${Z_PULSE_CLOSE_OWN_TERMINAL:-false}"
    _close_own=$(echo "$_close_own" | tr '[:upper:]' '[:lower:]')
    if [[ "$_close_own" != "true" && "$_close_own" != "1" && "$_close_own" != "yes" ]]; then
        return
    fi
    # TTY 기반으로 자기 창을 찾아 닫는다
    # (custom title은 bash 종료 시 초기화될 수 있으므로 TTY 사용)
    # python3 fork+setsid로 bash와 완전히 분리된 프로세스가 bash 종료 후 창을 닫는다
    local _tty
    _tty=$(tty 2>/dev/null || true)
    [[ -z "$_tty" ]] && return

    python3 - "$_tty" <<'PYEOF' &
import os, sys, time, subprocess

my_tty = sys.argv[1]

pid = os.fork()
if pid != 0:
    sys.exit(0)

os.setsid()
time.sleep(3.0)

scpt = f'''tell application "Terminal"
    repeat with w in windows
        try
            if (tty of front tab of w) = "{my_tty}" then
                close w without saving
            end if
        end try
    end repeat
end tell'''

subprocess.run(["osascript", "-e", scpt])
os._exit(0)
PYEOF
    wait $! 2>/dev/null || true
}

echo ""
echo "Z-Pulse Runner"
echo "====================="
echo ""

# 1. 필수 파일 확인
if [ ! -f "app.py" ]; then
    echo "[ERROR] app.py not found."
    echo "z_pulse/ 디렉토리에서 실행해주세요."
    exit 1
fi

# 2. Z-Pulse 전용 Python 3.11+ 가상환경 확인 및 생성 (uv 관리형 — brew 독립)
if ! command -v uv >/dev/null 2>&1; then
    echo "[ERROR] uv가 설치되어 있지 않습니다. 다음 명령으로 설치하세요: brew install uv"
    exit 1
fi

Z_PULSE_VENV="$Z_PULSE_ROOT/.venv"
Z_PULSE_PYTHON="$Z_PULSE_VENV/bin/python"

# Self-heal: .venv가 없거나 인터프리터가 깨진 경우 uv로 재생성
_z_pulse_needs_rebuild=false
if [ ! -d "$Z_PULSE_VENV" ]; then
    _z_pulse_needs_rebuild=true
elif ! "$Z_PULSE_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    echo "[INFO] .venv 인터프리터 검증 실패 (깨진 환경 감지) — uv로 재생성합니다..."
    rm -rf "$Z_PULSE_VENV"
    _z_pulse_needs_rebuild=true
fi

if [ "$_z_pulse_needs_rebuild" = true ]; then
    echo "[INFO] Creating Z-Pulse .venv with uv (Python 3.11)..."
    uv venv --seed --python 3.11 "$Z_PULSE_VENV"
fi

source "$Z_PULSE_VENV/bin/activate"

if ! "$Z_PULSE_PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    echo "[ERROR] Z-Pulse .venv는 Python 3.11+ 이어야 합니다: $Z_PULSE_PYTHON"
    exit 1
fi

if [ ! -f "$Z_PULSE_VENV/.requirements_installed" ] && [ -f "requirements.txt" ]; then
    echo "[INFO] Installing requirements..."
    uv pip install --python "$Z_PULSE_PYTHON" -r requirements.txt
    touch "$Z_PULSE_VENV/.requirements_installed"
fi

export Z_PULSE_PYTHON

# 3. setting.env 로드
if [ -f "setting.env" ]; then
    echo "[INFO] Loading setting.env..."
    while IFS='=' read -r key value; do
        # 주석과 빈 줄 건너뛰기
        if [[ ! $key =~ ^# && -n $key && -n $value ]]; then
            # 앞뒤 공백 제거
            key=$(echo "$key" | xargs)
            # 따옴표 제거
            value=$(echo "$value" | sed 's/^"//' | sed 's/"$//')
            # 환경변수가 이미 설정되어 있지 않은 경우만 설정
            if [ -z "${!key}" ]; then
                export "$key"="$value"
                echo "   Load: $key = $value"
            fi
        fi
    done < setting.env
    echo "[SUCCESS] Settings loaded."
else
    echo "[WARNING] setting.env not found. Run setup_bot.sh first."
fi

Z_FLOW_ROOT="$REPO_ROOT/z_flow"
Z_FLOW_SCRIPTS_DIR="$Z_FLOW_ROOT/scripts"

# 4. 텔레그램 토큰 확인
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo ""
    echo "[ERROR] TELEGRAM_BOT_TOKEN is missing."
    echo "Please run setup_bot.sh or set it manually."
    echo ""
    read -p "Continue without token? (y/N): " -n 1 -r REPLY
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# 5. TARGET_DIR 기본값 설정
if [ -z "$TARGET_DIR" ]; then
    TARGET_DIR="$HOME/Documents/toolkit"
    export TARGET_DIR
    echo "[INFO] Set default TARGET_DIR: $TARGET_DIR"
else
    echo "[INFO] TARGET_DIR: $TARGET_DIR"
fi

# 6. 기존 프로세스 정리 (z_flow는 독립 관리 — 종료하지 않음)
echo ""
echo "[INFO] Cleaning up old processes (excluding z_flow)..."
"$Z_PULSE_PYTHON" scripts/stop_processes.py --exclude-pid $$

# [보강] stop_processes 누락 대비: 2차 강제 종료
pkill -f "z_pulse/app.py" 2>/dev/null || true
pkill -f "z_pulse/__main__.py" 2>/dev/null || true
pkill -f "ws_kline_collector.py" 2>/dev/null || true
# market_data_daemon.py: 건강한 lease holder가 있으면 기존 데몬 유지 (데몬 교체 공백 방지)
_check_daemon_lease_alive() {
    local _ctrl_db
    _ctrl_db="${Z_FLOW_CONTROL_DB:-${Z_PULSE_ROOT}/../z_flow/strategy/db/pair_control.db}"
    "$Z_PULSE_PYTHON" - "$_ctrl_db" <<'PYEOF' 2>/dev/null
import sys, os, datetime, sqlite3
db = sys.argv[1]
if not os.path.exists(db):
    sys.exit(1)
try:
    conn = sqlite3.connect(db, timeout=3)
    row = conn.execute(
        "SELECT pid FROM market_data_leases WHERE expires_at > ? LIMIT 1",
        (datetime.datetime.now(datetime.timezone.utc).isoformat(),),
    ).fetchone()
    conn.close()
    if not row:
        sys.exit(1)
    pid = int(row[0])
    os.kill(pid, 0)
    sys.exit(0)
except Exception:
    sys.exit(1)
PYEOF
}
if _check_daemon_lease_alive; then
    echo "[INFO] 건강한 market_data_daemon lease holder 감지 — 기존 데몬 유지, pkill 스킵"
else
    echo "[INFO] lease holder 없음 또는 사망 — market_data_daemon 정리 후 재기동"
    pkill -f "market_data_daemon.py --exchange grvt" 2>/dev/null || true
    pkill -f "market_data_daemon.py --exchange binance" 2>/dev/null || true
fi
sleep 1
echo "[SUCCESS] Cleanup done."

# 6-1. [macOS] 기존 Z-Pulse 창의 프로세스 종료 + 창 닫기
#   - 자기 TTY는 제외하여 신규 창 보호
#   - 기존 창의 TTY 프로세스를 모두 kill한 후 창 닫기 (팝업 방지)
if [[ "$(uname -s)" = "Darwin" ]]; then
    _MY_TTY=$(tty 2>/dev/null || true)
    _MY_TTY_SHORT="${_MY_TTY#/dev/}"
    _OLD_TTYS=$(osascript - "${_MY_TTY:-none}" "${_MY_TTY_SHORT:-none}" <<'APPLE' 2>/dev/null || true
on run argv
    set myTTY to item 1 of argv
    set myTTYShort to item 2 of argv
    tell application "Terminal"
        set ttyList to ""
        repeat with w in windows
            try
                if (custom title of w) = "Z-Pulse" then
                    set t to tty of front tab of w
                    if t is not myTTY and t is not myTTYShort and ("/dev/" & t) is not myTTY then
                        set ttyList to ttyList & t & linefeed
                    end if
                end if
            end try
        end repeat
        return ttyList
    end tell
end run
APPLE
    )
    if [[ -n "$_OLD_TTYS" ]]; then
        echo "[INFO] 기존 Z-Pulse 창 TTY: $_OLD_TTYS"
        # 기존 창 TTY의 모든 프로세스 강제 종료 (bash 포함)
        # set -o pipefail 환경에서 ps 가 프로세스 없음(rc=1)을 반환해도
        # 파이프라인이 안전하도록 || true 로 보호
        while IFS= read -r _tty; do
            [[ -z "$_tty" ]] && continue
            if [[ "$_tty" = "$_MY_TTY" || "$_tty" = "$_MY_TTY_SHORT" || "/dev/$_tty" = "$_MY_TTY" ]]; then
                echo "[WARN] 현재 Z-Pulse 실행 창 TTY는 정리 대상에서 제외: $_tty"
                continue
            fi
            _tty_short="${_tty#/dev/}"
            ps -t "$_tty_short" -o pid= 2>/dev/null | while read -r _pid; do
                [[ -n "$_pid" ]] && kill -KILL "$_pid" 2>/dev/null || true
            done || true
        done <<< "$_OLD_TTYS"
        sleep 1
        _CLOSE_TTYS=""
        while IFS= read -r _tty; do
            [[ -z "$_tty" ]] && continue
            if [[ "$_tty" = "$_MY_TTY" || "$_tty" = "$_MY_TTY_SHORT" || "/dev/$_tty" = "$_MY_TTY" ]]; then
                continue
            fi
            _tty_short="${_tty#/dev/}"
            _remaining_pids="$(ps -t "$_tty_short" -o pid= 2>/dev/null || true)"
            if [[ -n "$_remaining_pids" ]]; then
                echo "[WARN] 기존 Z-Pulse 창 TTY에 프로세스가 남아 창 닫기를 건너뜀: $_tty"
                continue
            fi
            _CLOSE_TTYS="${_CLOSE_TTYS}${_tty}"$'\n'
        done <<< "$_OLD_TTYS"
        # 수집한 기존 TTY 창만 닫음 (제목 하드코딩 대신 exact target 사용)
        if [[ -n "$_CLOSE_TTYS" ]]; then
            osascript - "$_CLOSE_TTYS" <<'APPLE' 2>/dev/null || true
on run argv
    set oldTtysText to item 1 of argv
    set oldTtys to paragraphs of oldTtysText
    tell application "Terminal"
        repeat with w in windows
            try
                set t to tty of front tab of w
                if oldTtys contains t then
                    close w without saving
                end if
            end try
        end repeat
    end tell
end run
APPLE
        fi
    fi
    echo "[INFO] 기존 Z-Pulse 창 정리 완료"
fi

# 7. [Z-FLOW] Z-Flow (ws_kline_collector 내장) 조건부 실행
echo ""
# macOS bash 3.2 호환을 위해 tr 사용 (bash 4.0+ ${var,,} 대신)
Z_FLOW_ENABLED_LOWER=$(echo "${Z_FLOW_ENABLED}" | tr '[:upper:]' '[:lower:]')
if [ "$Z_FLOW_ENABLED_LOWER" = "true" ]; then
    echo "[Z-FLOW] Z_FLOW_ENABLED=true - Z-Flow 분봉 수집기 시작..."
    export Z_FLOW_MARKET_DATA_DAEMON_MODE="${Z_FLOW_MARKET_DATA_DAEMON_MODE:-true}"

    # GRVT market_data_daemon watchdog 변수
    # 베이스 경로만 정의 — _rotate_log()가 날짜 suffix(.log.YYYYMMDD)를 붙임
    GRVT_MARKET_DATA_DAEMON_LOG="$LOG_DIR/grvt_market_data_daemon"
    GRVT_MARKET_DATA_WATCHDOG_PIDFILE="$LOG_DIR/.grvt_market_data_watchdog.pid"

    # Binance market_data_daemon watchdog 변수
    # 베이스 경로만 정의 — _rotate_log()가 날짜 suffix(.log.YYYYMMDD)를 붙임
    BINANCE_MARKET_DATA_DAEMON_LOG="$LOG_DIR/binance_market_data_daemon"
    BINANCE_WATCHDOG_PID=""
    BINANCE_WATCHDOG_PIDFILE="$LOG_DIR/.binance_market_data_watchdog.pid"

    # 이전 GRVT watchdog 단일 인스턴스 강제
    if [ -f "$GRVT_MARKET_DATA_WATCHDOG_PIDFILE" ]; then
        _prev_watchdog_pid=$(cat "$GRVT_MARKET_DATA_WATCHDOG_PIDFILE" 2>/dev/null)
        if [ -n "$_prev_watchdog_pid" ] && kill -0 "$_prev_watchdog_pid" 2>/dev/null; then
            echo "[Z-FLOW][PRE-FLIGHT] Stopping previous GRVT watchdog PID $_prev_watchdog_pid"
            kill "$_prev_watchdog_pid" 2>/dev/null || true
            sleep 2
            kill -0 "$_prev_watchdog_pid" 2>/dev/null && kill -9 "$_prev_watchdog_pid" 2>/dev/null || true
        fi
    fi

    # 이전 Binance watchdog 단일 인스턴스 강제
    if [ -f "$BINANCE_WATCHDOG_PIDFILE" ]; then
        _prev_binance_watchdog_pid=$(cat "$BINANCE_WATCHDOG_PIDFILE" 2>/dev/null)
        if [ -n "$_prev_binance_watchdog_pid" ] && kill -0 "$_prev_binance_watchdog_pid" 2>/dev/null; then
            echo "[Z-FLOW][PRE-FLIGHT] Stopping previous Binance watchdog PID $_prev_binance_watchdog_pid"
            kill "$_prev_binance_watchdog_pid" 2>/dev/null || true
            sleep 2
            kill -0 "$_prev_binance_watchdog_pid" 2>/dev/null && kill -9 "$_prev_binance_watchdog_pid" 2>/dev/null || true
        fi
    fi

    # 이 프로젝트의 고아 market_data_daemon 정리 (exchange별 정밀화)
    # 런처들은 os.execve로 자신을 market_data_daemon.py로 교체하므로,
    # 실제 실행 중인 프로세스의 cmdline은 z_flow/data/market_data_daemon.py다.
    # 건강한 lease holder가 있으면 pkill 스킵 (앞서 _check_daemon_lease_alive가 유지 판정한 데몬 보호)
    if ! _check_daemon_lease_alive; then
        pkill -f "market_data_daemon.py --exchange grvt" 2>/dev/null || true
        pkill -f "market_data_daemon.py --exchange binance" 2>/dev/null || true
        _orphan_pids=$(pgrep -f "z_flow/data/market_data_daemon.py" 2>/dev/null || true)
        if [ -n "$_orphan_pids" ]; then
            echo "[Z-FLOW][PRE-FLIGHT] Stopping orphan market_data_daemon PIDs: $_orphan_pids"
            echo "$_orphan_pids" | xargs kill 2>/dev/null || true
            sleep 2
            _orphan_pids=$(pgrep -f "z_flow/data/market_data_daemon.py" 2>/dev/null || true)
            [ -n "$_orphan_pids" ] && echo "$_orphan_pids" | xargs kill -9 2>/dev/null || true
        fi
    else
        echo "[Z-FLOW][PRE-FLIGHT] 건강한 lease holder 감지 — orphan pkill 스킵, 데몬 보호"
    fi

    echo "[Z-FLOW] TIER 스케줄은 backfill 완료 후 자동 활성화됩니다."

    # market_data_daemon watchdog: 데몬을 직접 spawn하고 exit code로 재기동 여부 판단.
    # - exit 75 (YIELD_EXIT_CODE): healthy holder에게 양보 → 재기동 없이 watchdog 종료
    # - exit 0  (clean exit)     : 정상 종료 → 재기동 없이 watchdog 종료
    # - 기타 (crash)             : 비정상 종료 → max_restarts 횟수까지 재기동
    #
    # 설계 원칙: watchdog가 데몬의 부모(parent shell)가 되어 wait $pid 블로킹 수신.
    # 이전 kill -0 폴링 방식은 exit code를 볼 수 없어 yield-exit를 crash로 오분류했음.
    # 재기동 시 이전 lease는 acquire_or_heartbeat()의 stale PID takeover 로직으로 즉시 인수됨.
    _daemon_watchdog() {
        local interval=${DAEMON_WATCHDOG_INTERVAL_SEC:-30}
        local max_restarts=${DAEMON_WATCHDOG_MAX_RESTARTS:-10}
        local restart_count=0
        local _watchdog_shutting_down=0
        local YIELD_EXIT_CODE=75
        local pid=""

        set +e  # set -e 상속 해제 — wait 외부 신호로 루프가 비정상 종료되는 것을 방지

        _watchdog_log() {
            local level="$1" msg="$2"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] MARKET_DATA_DAEMON.watchdog | $(printf '%-9s' "$level") | $msg" >&2
        }

        # 최초 spawn (watchdog 내부 — 부모-자식 관계 확보, wait $pid 사용 가능)
        # process substitution: Python PID·exit code 보존, _rotate_log가 날짜별 파일 생성
        "$Z_PULSE_PYTHON" -u "$Z_FLOW_SCRIPTS_DIR/start_grvt_market_data_daemon.py" > >(_rotate_log "$GRVT_MARKET_DATA_DAEMON_LOG") 2>&1 &
        pid=$!
        echo "[Z-FLOW] GRVT market_data_daemon PID: $pid"

        # watchdog 종료 시 현재 추적 중인 daemon PID도 함께 종료 (고아 프로세스 방지)
        _watchdog_cleanup() {
            _watchdog_shutting_down=1  # 정상 종료임을 표시
            _watchdog_log INFO "stopping, cleaning up daemon PID ${pid:-}"
            kill "${pid:-}" 2>/dev/null || true
            rm -f "$GRVT_MARKET_DATA_WATCHDOG_PIDFILE" 2>/dev/null || true
        }
        trap '_watchdog_cleanup' EXIT TERM INT

        while true; do
            wait "$pid"
            local code=$?
            if [ "$_watchdog_shutting_down" -eq 1 ]; then
                break  # trap 경유 정상 종료
            fi
            if [ "$code" -eq "$YIELD_EXIT_CODE" ]; then
                _watchdog_log INFO "yielded to healthy holder (exit $YIELD_EXIT_CODE), stopping watchdog"
                break  # yield-exit — 재기동하지 않음
            fi
            if [ "$code" -eq 0 ]; then
                _watchdog_log INFO "daemon exited cleanly (exit 0), stopping watchdog"
                break  # clean exit — 재기동하지 않음
            fi
            # 비정상 exit (crash)
            if [ "$restart_count" -ge "$max_restarts" ]; then
                _watchdog_log WARNING "max restarts ($max_restarts) reached, stopping watchdog"
                break
            fi
            _watchdog_log WARNING "crash detected (exit $code, attempt $((restart_count+1))/$max_restarts), restarting..."
            "$Z_PULSE_PYTHON" -u "$Z_FLOW_SCRIPTS_DIR/start_grvt_market_data_daemon.py" > >(_rotate_log "$GRVT_MARKET_DATA_DAEMON_LOG") 2>&1 &
            pid=$!
            restart_count=$((restart_count+1))
            _watchdog_log INFO "restarted with PID $pid"
        done
    }

    # GRVT watchdog 기동 (spawn은 watchdog 내부에서 처리)
    _daemon_watchdog &
    WATCHDOG_PID=$!
    echo "$WATCHDOG_PID" > "$GRVT_MARKET_DATA_WATCHDOG_PIDFILE"
    echo "[Z-FLOW] GRVT market_data_daemon watchdog PID: $WATCHDOG_PID (interval=${DAEMON_WATCHDOG_INTERVAL_SEC:-30}s, max_restarts=${DAEMON_WATCHDOG_MAX_RESTARTS:-10})"

    # Binance market_data_daemon watchdog
    # 구조는 GRVT watchdog와 동일하며 start_binance_market_data_daemon.py를 호출한다.
    _binance_daemon_watchdog() {
        local interval=${DAEMON_WATCHDOG_INTERVAL_SEC:-30}
        local max_restarts=${DAEMON_WATCHDOG_MAX_RESTARTS:-10}
        local restart_count=0
        local _binance_watchdog_shutting_down=0
        local YIELD_EXIT_CODE=75
        local pid=""

        set +e  # set -e 상속 해제

        _binance_watchdog_log() {
            local level="$1" msg="$2"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] BINANCE_DAEMON.watchdog | $(printf '%-9s' "$level") | $msg" >&2
        }

        # 최초 spawn
        # process substitution: Python PID·exit code 보존, _rotate_log가 날짜별 파일 생성
        "$Z_PULSE_PYTHON" -u "$Z_FLOW_SCRIPTS_DIR/start_binance_market_data_daemon.py" > >(_rotate_log "$BINANCE_MARKET_DATA_DAEMON_LOG") 2>&1 &
        pid=$!
        echo "[Z-FLOW] Binance market_data_daemon PID: $pid"

        _binance_watchdog_cleanup() {
            _binance_watchdog_shutting_down=1
            _binance_watchdog_log INFO "stopping, cleaning up daemon PID ${pid:-}"
            kill "${pid:-}" 2>/dev/null || true
            rm -f "$BINANCE_WATCHDOG_PIDFILE" 2>/dev/null || true
        }
        trap '_binance_watchdog_cleanup' EXIT TERM INT

        while true; do
            wait "$pid"
            local code=$?
            if [ "$_binance_watchdog_shutting_down" -eq 1 ]; then
                break
            fi
            if [ "$code" -eq "$YIELD_EXIT_CODE" ]; then
                _binance_watchdog_log INFO "yielded to healthy holder (exit $YIELD_EXIT_CODE), stopping watchdog"
                break
            fi
            if [ "$code" -eq 0 ]; then
                _binance_watchdog_log INFO "daemon exited cleanly (exit 0), stopping watchdog"
                break
            fi
            if [ "$restart_count" -ge "$max_restarts" ]; then
                _binance_watchdog_log WARNING "max restarts ($max_restarts) reached, stopping watchdog"
                break
            fi
            _binance_watchdog_log WARNING "crash detected (exit $code, attempt $((restart_count+1))/$max_restarts), restarting..."
            "$Z_PULSE_PYTHON" -u "$Z_FLOW_SCRIPTS_DIR/start_binance_market_data_daemon.py" > >(_rotate_log "$BINANCE_MARKET_DATA_DAEMON_LOG") 2>&1 &
            pid=$!
            restart_count=$((restart_count+1))
            _binance_watchdog_log INFO "restarted with PID $pid"
        done
    }

    # Binance watchdog 기동
    _binance_daemon_watchdog &
    BINANCE_WATCHDOG_PID=$!
    echo "$BINANCE_WATCHDOG_PID" > "$BINANCE_WATCHDOG_PIDFILE"
    echo "[Z-FLOW] Binance market_data_daemon watchdog PID: $BINANCE_WATCHDOG_PID (interval=${DAEMON_WATCHDOG_INTERVAL_SEC:-30}s, max_restarts=${DAEMON_WATCHDOG_MAX_RESTARTS:-10})"

    ZFLOW_PID=""
else
    echo "[Z-FLOW] Z_FLOW_ENABLED != true - Z-Flow 스킵"
    ZFLOW_PID=""
    MARKET_DATA_DAEMON_PID=""
    WATCHDOG_PID=""
    BINANCE_WATCHDOG_PID=""
fi

# 8. Z-Pulse 실행 (포그라운드)
echo ""
echo "Starting Z-Pulse..."
echo ""
echo "----------------------------------------"
echo "   Log: z_pulse.log"
echo "   Stop: Ctrl+C or close window"
echo "----------------------------------------"
echo ""

# macOS: 현재 창(TTY 기반)의 제목을 "Z-Pulse"로 설정
# cleanup_terminal이 이 이름으로 창을 찾아 프로세스 종료 + 창 닫기 수행
if [[ "$(uname -s)" = "Darwin" ]]; then
    _MY_TTY=$(tty 2>/dev/null || true)
    if [[ -n "$_MY_TTY" ]]; then
        osascript - "$_MY_TTY" <<'APPLE' 2>/dev/null || true
on run argv
    set myTTY to item 1 of argv
    tell application "Terminal"
        repeat with w in windows
            try
                if (tty of front tab of w) = myTTY then
                    set custom title of w to "Z-Pulse"
                    exit repeat
                end if
            end try
        end repeat
    end tell
end run
APPLE
    fi
fi

# 종료 시그널 처리 (Z-Flow, watchdog도 함께 종료)
cleanup() {
    echo ""
    echo "[INFO] Shutting down..."
    if [ -n "$WATCHDOG_PID" ] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
        kill "$WATCHDOG_PID" 2>/dev/null || true
        echo "[INFO] GRVT market_data_daemon watchdog (PID: $WATCHDOG_PID) 종료"
    fi
    rm -f "${GRVT_MARKET_DATA_WATCHDOG_PIDFILE:-}" 2>/dev/null || true
    if [ -n "$BINANCE_WATCHDOG_PID" ] && kill -0 "$BINANCE_WATCHDOG_PID" 2>/dev/null; then
        kill "$BINANCE_WATCHDOG_PID" 2>/dev/null || true
        echo "[INFO] Binance market_data_daemon watchdog (PID: $BINANCE_WATCHDOG_PID) 종료"
    fi
    rm -f "${BINANCE_WATCHDOG_PIDFILE:-}" 2>/dev/null || true
    if [ -n "$ZFLOW_PID" ] && kill -0 "$ZFLOW_PID" 2>/dev/null; then
        kill "$ZFLOW_PID" 2>/dev/null || true
        echo "[INFO] Z-Flow collector (PID: $ZFLOW_PID) 종료"
    fi
    if [ -n "$MARKET_DATA_DAEMON_PID" ] && kill -0 "$MARKET_DATA_DAEMON_PID" 2>/dev/null; then
        kill "$MARKET_DATA_DAEMON_PID" 2>/dev/null || true
        echo "[INFO] Market data daemon (PID: $MARKET_DATA_DAEMON_PID) 종료"
    fi
    _close_this_terminal
    exit 0
}
trap cleanup SIGINT SIGTERM

export PYTHONIOENCODING=utf-8
"$Z_PULSE_PYTHON" -u __main__.py
EXIT_CODE=$?

# 비정상 종료 시 5초 대기 (오류 확인 기회 부여)
if [ $EXIT_CODE -ne 0 ]; then
    echo ""
    echo "[WARN] Z-Pulse exited with code $EXIT_CODE - closing in 5 sec..."
    sleep 5
fi

# Z-Flow collector / watchdog 정리
if [ -n "$WATCHDOG_PID" ] && kill -0 "$WATCHDOG_PID" 2>/dev/null; then
    kill "$WATCHDOG_PID" 2>/dev/null || true
fi
if [ -n "$BINANCE_WATCHDOG_PID" ] && kill -0 "$BINANCE_WATCHDOG_PID" 2>/dev/null; then
    kill "$BINANCE_WATCHDOG_PID" 2>/dev/null || true
fi
if [ -n "$ZFLOW_PID" ] && kill -0 "$ZFLOW_PID" 2>/dev/null; then
    kill "$ZFLOW_PID" 2>/dev/null || true
fi
if [ -n "$MARKET_DATA_DAEMON_PID" ] && kill -0 "$MARKET_DATA_DAEMON_PID" 2>/dev/null; then
    kill "$MARKET_DATA_DAEMON_PID" 2>/dev/null || true
fi

_close_this_terminal
exit $EXIT_CODE
