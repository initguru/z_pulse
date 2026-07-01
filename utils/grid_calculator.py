"""
격자 레이아웃 계산 유틸리티
"""

from typing import Tuple


def calculate_grid_layout(window_count: int) -> Tuple[int, int]:
    """
    창 개수에 따른 격자 레이아웃 계산

    Args:
        window_count: 배치할 창의 개수

    Returns:
        (cols, rows): 열과 행 개수

    Examples:
        >>> calculate_grid_layout(1)
        (1, 1)
        >>> calculate_grid_layout(4)
        (2, 2)
        >>> calculate_grid_layout(6)
        (3, 2)
    """
    if window_count <= 0:
        return 0, 0
    elif window_count == 1:
        return 1, 1
    elif window_count <= 2:
        return 2, 1
    elif window_count <= 4:
        return 2, 2
    elif window_count <= 6:
        return 3, 2
    elif window_count <= 9:
        return 3, 3
    elif window_count <= 12:
        return 4, 3
    elif window_count <= 16:
        return 4, 4
    elif window_count <= 20:
        return 5, 4
    else:
        return 5, 5
