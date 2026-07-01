"""
daemon_watchdog.py — 크로스플랫폼 데몬 워치독 헬퍼

run_all.bat (Windows)에서 run_all.sh의 _daemon_watchdog() / _binance_daemon_watchdog()
shell 함수를 대체하는 Python 워치독.

기능:
  - 지정된 Python 스크립트를 서브프로세스로 기동
  - YIELD_EXIT_CODE(75) → 건강한 holder가 있음: 워치독 정상 종료
  - exit 0           → 클린 셧다운: 워치독 정상 종료
  - 그 외(크래시)     → 재기동 (최대 max_restarts 회)
  - Ctrl+C / SIGTERM → 현재 추적 중인 데몬 PID 함께 종료 후 종료
  - 재기동 간격: DAEMON_WATCHDOG_INTERVAL_SEC (기본 30초)
  - 최대 재기동: DAEMON_WATCHDOG_MAX_RESTARTS (기본 10회)

사용법 (run_all.bat에서):
    start "GRVT-watchdog" python z_pulse\\scripts\\daemon_watchdog.py ^
        z_flow\\scripts\\start_grvt_market_data_daemon.py ^
        z_pulse\\logs\\grvt_market_data_daemon ^
        --pidfile z_pulse\\logs\\.grvt_market_data_watchdog.pid

인자:
    argv[1]  — 실행할 Python 스크립트 경로
    argv[2]  — 날짜 suffix를 붙이기 전 로그 베이스 경로
    --pidfile — (optional) 워치독 자신의 PID를 기록할 파일 경로
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# market_data_daemon.py의 YIELD_EXIT_CODE와 동일 (exit 75 = healthy holder에게 양보)
YIELD_EXIT_CODE = 75


def _log(label: str, level: str, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {label}.watchdog | {level:<9} | {msg}", flush=True)


def _log_writer(proc_stdout, base_path: str) -> None:
    """날짜 인지 로그 writer — stdout 줄을 읽어 <base_path>.log.YYYYMMDD 에 append.

    proc_stdout: 줄 단위로 읽을 수 있는 iterable (subprocess stdout 또는 io.StringIO).
    base_path:   날짜 suffix를 붙이기 전 기본 경로 (예: /path/to/grvt_market_data_daemon).

    EOF (데몬 종료 또는 StopIteration) 시 열린 파일을 닫고 자연 종료.
    daemon=True 스레드로 실행하면 워치독 종료 시 자동 회수됨.
    """
    cur_date: str = ""
    cur_file = None
    try:
        for line in proc_stdout:
            today = datetime.now().strftime("%Y%m%d")
            if today != cur_date:
                if cur_file is not None:
                    cur_file.close()
                cur_date = today
                Path(base_path).parent.mkdir(parents=True, exist_ok=True)
                cur_file = open(f"{base_path}.log.{today}", "a", encoding="utf-8", buffering=1)
            if cur_file is not None:
                cur_file.write(line)
    finally:
        if cur_file is not None:
            cur_file.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="daemon_watchdog: 데몬 프로세스 감시 및 재기동")
    parser.add_argument("script", help="실행할 Python 스크립트 경로")
    parser.add_argument("log_file", nargs="?", default=None, help="로그 파일 경로 (stdout+stderr 리다이렉트)")
    parser.add_argument("--pidfile", default=None, help="워치독 자신의 PID를 기록할 파일 경로 (단일 인스턴스 가드용)")
    args = parser.parse_args()

    script_path = args.script
    log_file = args.log_file

    # 단일 인스턴스 가드: 워치독 자신의 PID를 파일에 기록
    if args.pidfile:
        Path(args.pidfile).parent.mkdir(parents=True, exist_ok=True)
        Path(args.pidfile).write_text(str(os.getpid()))

    label = Path(script_path).stem.replace("start_", "").replace("_market_data_daemon", "_daemon").upper()

    interval = int(os.environ.get("DAEMON_WATCHDOG_INTERVAL_SEC", "30"))
    max_restarts = int(os.environ.get("DAEMON_WATCHDOG_MAX_RESTARTS", "10"))

    restart_count = 0
    shutting_down = False
    current_proc: Optional[subprocess.Popen] = None

    def _cleanup(signum=None, frame=None) -> None:
        nonlocal shutting_down
        shutting_down = True
        _log(label, "INFO", f"stopping (signal {signum}), cleaning up daemon")
        if current_proc is not None:
            try:
                current_proc.terminate()
                current_proc.wait(timeout=5)
            except Exception:
                try:
                    current_proc.kill()
                except Exception:
                    pass

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    def _spawn() -> subprocess.Popen:
        if log_file:
            # log_file을 베이스 경로로 재해석 — _log_writer가 날짜 suffix를 붙임.
            # stdout=PIPE + daemon thread로 Python PID·exit code를 보존 (process-substitution과 동일 효과).
            proc = subprocess.Popen(
                args=[sys.executable, "-u", script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
            )
            t = threading.Thread(target=_log_writer, args=(proc.stdout, log_file), daemon=True)
            t.start()
        else:
            proc = subprocess.Popen(
                args=[sys.executable, "-u", script_path],
            )
        return proc

    # 최초 기동
    _log(label, "INFO", f"starting {script_path} (interval={interval}s, max_restarts={max_restarts})")
    current_proc = _spawn()
    _log(label, "INFO", f"daemon PID: {current_proc.pid}")

    while True:
        # 프로세스 종료 대기
        code = current_proc.wait()

        if shutting_down:
            _log(label, "INFO", "watchdog stopped (shutdown requested)")
            break

        # YIELD_EXIT_CODE: 건강한 holder가 이미 있음 → 워치독 정상 종료
        if code == YIELD_EXIT_CODE:
            _log(label, "INFO", f"yielded to healthy holder (exit {YIELD_EXIT_CODE}), stopping watchdog")
            break

        # 클린 셧다운
        if code == 0:
            _log(label, "INFO", "daemon exited cleanly (exit 0), stopping watchdog")
            break

        # 크래시 → 재기동 판단
        if restart_count >= max_restarts:
            _log(label, "WARNING", f"max restarts ({max_restarts}) reached, stopping watchdog")
            break

        _log(label, "WARNING",
             f"crash detected (exit {code}, attempt {restart_count + 1}/{max_restarts}), "
             f"restarting in {interval}s...")
        time.sleep(interval)

        if shutting_down:
            break

        current_proc = _spawn()
        restart_count += 1
        _log(label, "INFO", f"restarted with PID {current_proc.pid}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
