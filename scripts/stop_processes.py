"""
stop_all 스크립트에서 호출되는 프로세스 종료 스크립트.

z_pulse/app.py 와 ws_kline_collector.py 를 실행 중인
프로세스 트리를 탐색하여 graceful terminate → force kill 순으로 종료한다.
(Windows: stop_all.bat, macOS/Linux: stop_all.sh 에서 호출)

Z-Flow 런타임(z_flow/run_bot.py)은 기본적으로 종료하지 않음 — 독립 프로세스로 관리됨.
Z-Flow 런타임도 종료하려면 --all 또는 --include-slot 플래그 사용.
"""

import argparse
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import psutil

from z_pulse.integration.z_flow_bridge import ZFlowBridge

# 기본 타겟: z_pulse, kline_collector (Z-Flow 제외)
TARGETS = [
    "z_pulse/app.py",
    "z_pulse/__main__",
    "__main__.py",
    "ws_kline_collector.py",
]

# Z-Flow 런타임 타겟: --all 또는 --include-slot 플래그 시에만 포함
SLOT_TARGETS = list(ZFlowBridge.get_runtime_entry_targets())

_SHELL_PARENTS_WIN = {"cmd.exe", "conhost.exe"}
_SHELL_PARENTS_UNIX = {"bash", "zsh", "sh", "terminal"}
_SHELL_PARENTS = _SHELL_PARENTS_WIN if sys.platform == "win32" else _SHELL_PARENTS_UNIX


def _normalize_cmdline(cmdline: list[str]) -> tuple[list[str], str]:
    cmd_parts = [part.replace("\\", "/").lower() for part in cmdline if part]
    return cmd_parts, " ".join(cmd_parts)


def _matches_z_flow_runtime_cmdline(cmdline: list[str]) -> bool:
    return ZFlowBridge.matches_runtime_cmdline(cmdline)


def _match_target(cmdline: list[str], targets: list[str]) -> Optional[str]:
    _, cmd_str = _normalize_cmdline(cmdline)
    runtime_targets = set(ZFlowBridge.get_runtime_entry_targets())
    for target in targets:
        if target in runtime_targets:
            if _matches_z_flow_runtime_cmdline(cmdline):
                return target
            continue
        if target.lower() in cmd_str:
            return target
    return None


def find_root_shell(proc: psutil.Process) -> psutil.Process:
    """python 프로세스 → 부모 체인을 따라 최상위 쉘 프로세스 반환.
    해당 쉘이 없으면 python 프로세스 자체를 반환한다.
    """
    try:
        p = proc
        while True:
            parent = p.parent()
            if parent is None:
                break
            if parent.name().lower() in _SHELL_PARENTS:
                p = parent
            else:
                break
        return p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return proc


def kill_tree(root: psutil.Process, label: str, include_zflow: bool = False) -> None:
    """root 프로세스 트리 전체를 종료한다.
    순서: terminate (SIGTERM) → 5초 대기 → 살아남은 것 kill (SIGKILL)

    Args:
        root: 종료할 루트 프로세스
        label: 로그 출력용 라벨
        include_zflow: True이면 z_flow 런타임 자손도 종료 대상에 포함.
                       False(기본)이면 z_flow 런타임 자손을 보호한다.
    """
    try:
        children = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []

    if not include_zflow:
        protected = []
        to_kill = []
        for child in children:
            try:
                child_cmd = child.cmdline()
                if _matches_z_flow_runtime_cmdline(child_cmd):
                    protected.append(child)
                    continue
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                # cmdline 판별 불가 → 보수적으로 보호
                protected.append(child)
                continue
            to_kill.append(child)
        children = to_kill
        if protected:
            pids = [p.pid for p in protected]
            print(f"[GUARD] z_flow 런타임 프로세스 보호: PID {pids}")

    all_procs = children + [root]

    # 1단계: terminate
    for p in all_procs:
        try:
            p.terminate()
            print(f"  [TERMINATE] PID {p.pid} ({p.name()})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # 2단계: 최대 5초 대기
    _, alive = psutil.wait_procs(all_procs, timeout=5)

    # 3단계: 살아남은 프로세스 강제 kill
    for p in alive:
        try:
            p.kill()
            print(f"  [KILL] PID {p.pid} ({p.name()}) 강제 종료")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    print(f"[DONE] {label} 종료 완료")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="프로세스 종료 스크립트 (Z-Flow 런타임은 기본 제외)"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Z-Flow 런타임(z_flow/run_bot.py)도 포함하여 종료 (기본: 제외)",
    )
    parser.add_argument(
        "--include-slot",
        action="store_true",
        dest="include_slot",
        help="--all 과 동일",
    )
    parser.add_argument(
        "--exclude-pid",
        type=int,
        default=0,
        dest="exclude_pid",
        help="이 PID를 포함하는 프로세스 트리는 종료하지 않음 (run_all.sh 자기자신 보호용)",
    )
    args = parser.parse_args(argv)

    # 타겟 결정: 기본 또는 슬롯 포함
    targets = TARGETS
    if args.all or args.include_slot:
        targets = TARGETS + SLOT_TARGETS

    killed_roots: set[int] = set()

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
            matched = _match_target(cmdline, targets)
            if not matched:
                continue

            root = find_root_shell(proc)
            if root.pid in killed_roots:
                continue

            # --exclude-pid: run_all.sh에서 자기 자신의 PID 트리를 보호
            if args.exclude_pid and args.exclude_pid > 0:
                try:
                    # root 트리에 exclude_pid가 포함되어 있으면 스킵
                    tree_pids = {p.pid for p in root.children(recursive=True)} | {root.pid}
                    if args.exclude_pid in tree_pids:
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            print(f"[FOUND] {matched}  →  root PID {root.pid} ({root.name()})")
            include_zflow = args.all or args.include_slot
            kill_tree(root, matched, include_zflow=include_zflow)
            killed_roots.add(root.pid)

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if not killed_roots:
        print("[INFO] 실행 중인 대상 프로세스 없음")
    else:
        print(f"[DONE] 총 {killed_roots.__len__()}개 프로세스 트리 종료")


if __name__ == "__main__":
    main()
