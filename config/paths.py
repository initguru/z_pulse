"""
z-pulse 경로 상수

z_pulse 패키지 루트와 repo 루트 경로를 중앙 관리합니다.
"""

from pathlib import Path

# z_pulse/config/paths.py → z_pulse/config/ → z_pulse/ (패키지 루트)
Z_PULSE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Z_PULSE_ROOT  # 호환성 유지

# repo 루트 (z_pulse/ → repo/)
REPO_ROOT = Path(__file__).resolve().parents[2]
