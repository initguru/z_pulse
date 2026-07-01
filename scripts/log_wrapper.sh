#!/bin/bash
# ============================================================
# monitor.log wrapper — daily rotation + append mode
# ============================================================
#
# Windows C# BotLogger 와 동등한 macOS 구현:
#   1. Append 모드 — 기존 로그를 절대 덮어쓰지 않음
#   2. 시작 시 로테이션 — mtime 이 이전 날짜면 monitor.log.YYYYMMDD 로 이동
#   3. 런타임 로테이션 — 자정(날짜 변경) 시 자동 이동 + 새 파일 시작
#   4. stdout 패스스루 — 모든 라인을 터미널에도 출력
#
# Usage:
#   봇실행명령 2>&1 | bash /path/to/log_wrapper.sh [logfile]
#
# Example:
#   python3 -u bot.py 2>&1 | bash scripts/log_wrapper.sh
#   ./z-pulse-bot 2>&1 | bash scripts/log_wrapper.sh /tmp/custom.log
# ============================================================

set -u

LOG_FILE="${1:-monitor.log}"
CURRENT_DATE=$(date +%Y%m%d)

# --- 시작 시 로테이션 ---
# 기존 파일의 mtime 이 오늘이 아니면 날짜 suffix 를 붙여 이동
if [ -f "$LOG_FILE" ]; then
    # macOS BSD stat: -f %Sm = modification time, -t = format
    FILE_DATE=$(stat -f %Sm -t %Y%m%d "$LOG_FILE" 2>/dev/null)
    if [ -n "$FILE_DATE" ] && [ "$FILE_DATE" != "$CURRENT_DATE" ]; then
        mv "$LOG_FILE" "${LOG_FILE}.${FILE_DATE}" 2>/dev/null || true
    fi
fi

# --- 라인별 처리: stdout 패스스루 + 파일 append + 런타임 로테이션 ---
while IFS= read -r line || [ -n "$line" ]; do
    # 런타임 로테이션: 날짜가 바뀌었는지 체크
    NEW_DATE=$(date +%Y%m%d)
    if [ "$NEW_DATE" != "$CURRENT_DATE" ]; then
        # 자정 경과 — 현재 파일을 이전 날짜로 이동
        if [ -f "$LOG_FILE" ]; then
            mv "$LOG_FILE" "${LOG_FILE}.${CURRENT_DATE}" 2>/dev/null || true
        fi
        CURRENT_DATE="$NEW_DATE"
    fi

    # stdout 패스스루 (터미널 출력)
    printf '%s\n' "$line"
    # 파일에 append
    printf '%s\n' "$line" >> "$LOG_FILE"
done
