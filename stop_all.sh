#!/bin/bash

# ============================================================
# Z-Pulse Stopper (macOS/Linux)
# stop_all.bat 대응 — stop_processes.py 호출
# ============================================================

echo ""
echo "Z-Pulse Stopper"
echo "======================"
echo ""

# 프로젝트 루트로 이동
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 가상환경 활성화 (필요 시)
if [ -z "$VIRTUAL_ENV" ] && [ -d "z_pulse/.venv" ]; then
    source z_pulse/.venv/bin/activate
fi

python3 z_pulse/scripts/stop_processes.py

echo ""
echo "Done. Press Enter to exit."
read -r
