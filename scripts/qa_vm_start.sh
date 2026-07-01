#!/usr/bin/env bash
set -euo pipefail

# ── 변수 ────────────────────────────────────────────────────────────────────
VM_NAME="z-pulse-test"
HOST_SRC="$HOME/crypto/2oolkit-monitor/z_pulse"
QA_ENV_SRC="$HOST_SRC/setting.env.qa"
TEST_BOT_SRC="$HOME/crypto/_TEST"
STAGING_DIR="/tmp/z_pulse_qa"
VM_MOUNT_DEST="/Volumes/My Shared Files/z_pulse"
BOT_MOUNT_NAME="2oolkit-bot"

# ── 색상 ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'

err()  { echo -e "${RED}❌ $*${NC}" >&2; exit 1; }
warn() { echo -e "${YELLOW}⚠️  $*${NC}"; }
info() { echo -e "${GREEN}✅ $*${NC}"; }

# ── 사전 조건 검사 ──────────────────────────────────────────────────────────
check_prerequisites() {
    command -v tart  >/dev/null 2>&1 || err "tart 가 설치되어 있지 않습니다."
    command -v rsync >/dev/null 2>&1 || err "rsync 가 설치되어 있지 않습니다."

    [[ -d "$HOST_SRC" ]]    || err "소스 디렉토리를 찾을 수 없습니다: $HOST_SRC"
    [[ -f "$QA_ENV_SRC" ]]  || err "QA 설정 파일을 찾을 수 없습니다: $QA_ENV_SRC"
    [[ -d "$TEST_BOT_SRC" ]] || err "테스트 봇 디렉토리를 찾을 수 없습니다: $TEST_BOT_SRC"

    # VM 존재 여부 (Source 컬럼 유무와 무관하게 이름으로 확인)
    tart list 2>/dev/null | grep -q "$VM_NAME" \
        || err "Tart VM '$VM_NAME' 를 찾을 수 없습니다. (tart list 로 확인)"

    # VM 실행 중이면 자동 종료
    if tart list 2>/dev/null | grep -q "$VM_NAME.*running"; then
        warn "VM '$VM_NAME' 실행 중 — 자동 종료 후 재시작합니다."
        tart stop "$VM_NAME"
        info "VM 종료 완료"
    fi
}

# ── 스테이징 구성 ────────────────────────────────────────────────────────────
setup_staging() {
    # ① 항상 fresh — 이전 QA 결과물 전체 삭제
    echo "🗑️  스테이징 초기화: $STAGING_DIR"
    rm -rf "$STAGING_DIR"
    mkdir -p "$STAGING_DIR"

    # ② git-tracked 파일만 복사 (z_pulse/ 한정, .venv·*.log 등 제외됨)
    echo "📂 git-tracked 파일 복사 중..."
    (
        cd "$HOST_SRC"
        git ls-files | rsync -a --ignore-missing-args --files-from=- . "$STAGING_DIR/"
    )
    info "git-tracked 파일 복사 완료"

    # ③ QA 전용 설정 파일 주입 (운영 크리덴셜 덮어쓰기)
    echo "🔑 QA 설정 파일 주입: setting.env.qa → setting.env"
    cp "$QA_ENV_SRC" "$STAGING_DIR/setting.env"
    info "setting.env 주입 완료"

    # ④ 테스트 봇 디렉토리 복사 (별도 마운트 → VM: /Volumes/My Shared Files/$BOT_MOUNT_NAME/GRVT-TEST)
    echo "🤖 테스트 봇 복사: _TEST → TARGET/GRVT-TEST"
    mkdir -p "$STAGING_DIR/TARGET/GRVT-TEST"
    cp -r "$TEST_BOT_SRC/." "$STAGING_DIR/TARGET/GRVT-TEST/"
    info "TARGET/GRVT-TEST 복사 완료"
}

# ── VM 시작 ──────────────────────────────────────────────────────────────────
start_vm() {
    echo "🚀 VM '$VM_NAME' 시작 중..."
    echo "   마운트: $STAGING_DIR → $VM_MOUNT_DEST (rw)"
    echo "   마운트: $STAGING_DIR/TARGET → /Volumes/My Shared Files/$BOT_MOUNT_NAME (rw)"
    tart run "$VM_NAME" \
        --dir "z_pulse:$STAGING_DIR" \
        --dir "$BOT_MOUNT_NAME:$STAGING_DIR/TARGET" &
}

# ── 완료 안내 ────────────────────────────────────────────────────────────────
print_instructions() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    info "QA VM 준비 완료"
    echo ""
    echo "  VM 내에서 실행:"
    echo "    cd $VM_MOUNT_DEST && ./run_all.sh"
    echo ""
    echo "  호스트에서 로그 실시간 확인:"
    echo "    tail -f $STAGING_DIR/z_pulse.log"
    echo ""
    echo "  QA 반복 시:"
    echo "    1. VM 종료 후 원본 수정"
    echo "    2. 이 스크립트 재실행"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# ── SSH 접속 ─────────────────────────────────────────────────────────────────
connect_ssh() {
    echo ""
    echo "⏳ VM 부팅 중 — IP 확인 대기..."
    local ip="" n=0
    until [[ -n "$ip" ]]; do
        ip=$(tart ip "$VM_NAME" 2>/dev/null || true)
        [[ -n "$ip" ]] && break
        n=$(( n + 1 ))
        [[ $n -ge 30 ]] && err "IP 확인 타임아웃 (60초). 상태 확인: tart list"
        sleep 2
    done
    info "VM IP: $ip"

    echo "⏳ SSH 포트 대기 중..."
    n=0
    until ssh -q \
              -o ConnectTimeout=3 \
              -o StrictHostKeyChecking=no \
              -o BatchMode=yes \
              "admin@$ip" true 2>/dev/null; do
        n=$(( n + 1 ))
        [[ $n -ge 30 ]] && err "SSH 타임아웃. 수동 접속: ssh admin@$ip"
        sleep 2
    done
    info "SSH 접속 중: admin@$ip → $VM_MOUNT_DEST"
    echo ""

    ssh -o StrictHostKeyChecking=no \
        "admin@$ip" \
        -t "cd \"$VM_MOUNT_DEST\" && exec \$SHELL -l"
}

# ── main ─────────────────────────────────────────────────────────────────────
main() {
    check_prerequisites
    setup_staging
    start_vm
    print_instructions
    connect_ssh
}

main "$@"
