"""Z-Pulse 진입점 — 어느 디렉토리에서든 독립 실행 가능"""
import sys
from pathlib import Path

# z_pulse/의 상위 디렉토리를 sys.path에 추가
# → 내부 모듈의 "from z_pulse.xxx import yyy" 구문이 정상 작동
_PARENT_DIR = str(Path(__file__).resolve().parent.parent)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

from z_pulse.app import main

if __name__ == "__main__":
    main()
