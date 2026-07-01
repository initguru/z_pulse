"""
텔레그램 인라인 키보드 빌더
"""

from typing import List, Optional, Tuple
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


class KeyboardBuilder:
    """인라인 키보드 빌더 클래스"""

    def __init__(self):
        self.rows: List[List[InlineKeyboardButton]] = []

    def add_button(
        self,
        label: str,
        callback_data: str,
        new_row: bool = True
    ) -> 'KeyboardBuilder':
        """
        단일 버튼 추가

        Args:
            label: 버튼 라벨
            callback_data: 콜백 데이터
            new_row: 새 행에 추가 여부

        Returns:
            self (체이닝 지원)
        """
        button = InlineKeyboardButton(label, callback_data=callback_data)

        if new_row or not self.rows:
            self.rows.append([button])
        else:
            self.rows[-1].append(button)

        return self

    def add_row(self, *buttons: Tuple[str, str]) -> 'KeyboardBuilder':
        """
        버튼 여러 개를 한 행에 추가

        Args:
            *buttons: (라벨, callback_data) 튜플들

        Returns:
            self (체이닝 지원)

        Examples:
            >>> kb.add_row(("시작", "run:bot1"), ("종료", "kill:bot1"))
        """
        row = [
            InlineKeyboardButton(label, callback_data=data)
            for label, data in buttons
        ]
        self.rows.append(row)
        return self

    def add_separator(self, text: str = "---") -> 'KeyboardBuilder':
        """
        구분선 (noop 버튼) 추가

        Args:
            text: 구분선 텍스트

        Returns:
            self (체이닝 지원)
        """
        self.rows.append([
            InlineKeyboardButton(text, callback_data="noop")
        ])
        return self

    def add_back_button(
        self,
        label: str = "🔙 뒤로가기",
        callback_data: str = "refresh_dashboard"
    ) -> 'KeyboardBuilder':
        """
        뒤로가기 버튼 추가

        Args:
            label: 버튼 라벨
            callback_data: 콜백 데이터

        Returns:
            self (체이닝 지원)
        """
        return self.add_button(label, callback_data, new_row=True)

    def build(self) -> InlineKeyboardMarkup:
        """
        InlineKeyboardMarkup 객체 생성

        Returns:
            완성된 키보드 마크업
        """
        return InlineKeyboardMarkup(self.rows)

    def get_rows(self) -> List[List[InlineKeyboardButton]]:
        """
        버튼 행 리스트 반환 (raw)

        Returns:
            버튼 행 리스트
        """
        return self.rows

    def clear(self) -> 'KeyboardBuilder':
        """
        키보드 초기화

        Returns:
            self (체이닝 지원)
        """
        self.rows = []
        return self


def build_process_row(
    dir_name: str,
    is_running: bool,
    entry_info: Optional[str] = None
) -> List[InlineKeyboardButton]:
    """
    프로세스 상태 행 생성

    Args:
        dir_name: 디렉토리명
        is_running: 실행 중 여부
        entry_info: 진입 정보 문자열 (예: "(2/5)")

    Returns:
        버튼 리스트 (한 행)
    """
    if is_running:
        status_label = f"🟢 {dir_name}"
        if entry_info:
            status_label += f" {entry_info}"
        return [
            InlineKeyboardButton(status_label, callback_data=f"detail:{dir_name}"),
            InlineKeyboardButton("종료", callback_data=f"kill:{dir_name}")
        ]
    else:
        return [
            InlineKeyboardButton(f"🔴 {dir_name}", callback_data=f"detail:{dir_name}"),
            InlineKeyboardButton("시작", callback_data=f"run:{dir_name}")
        ]


def build_ignored_row(dir_name: str) -> List[InlineKeyboardButton]:
    """
    무시된 프로세스 행 생성

    Args:
        dir_name: 디렉토리명

    Returns:
        버튼 리스트 (한 행)
    """
    return [InlineKeyboardButton(f"💀 {dir_name}", callback_data=f"detail:{dir_name}")]


def build_detail_keyboard(
    target: str,
    is_running: bool,
    is_ignored: bool
) -> KeyboardBuilder:
    """
    프로세스 상세 페이지 키보드 생성

    Args:
        target: 대상 디렉토리명
        is_running: 실행 중 여부
        is_ignored: 무시 목록 여부

    Returns:
        KeyboardBuilder 인스턴스
    """
    kb = KeyboardBuilder()

    if not is_ignored:
        # 프로세스 제어
        if is_running:
            kb.add_row(
                ("🔴 종료", f"kill:{target}"),
                ("🔄 재시작", f"restart:{target}")
            )
        else:
            kb.add_button("🟢 시작", f"run:{target}")

        # 추가 기능
        kb.add_button("🧹 클린 실행", f"confirm_clean_run:{target}")

        # 로그 기능
        kb.add_row(
            ("📄 로그 (100줄)", f"log:{target}"),
            ("📄 전체 로그", f"log_full:{target}")
        )

        # 디렉토리 관리
        kb.add_row(
            ("📂 디렉토리 변경", f"change_dir:{target}"),
            ("🔔 키워드 알림", f"keywords_menu:{target}")
        )

    # 뒤로가기
    kb.add_back_button("🔙 대시보드로 돌아가기", "refresh_dashboard")

    return kb


def build_confirm_keyboard(
    confirm_action: str,
    cancel_action: str = "refresh_dashboard",
    confirm_label: str = "✅ 확인",
    cancel_label: str = "❌ 취소"
) -> KeyboardBuilder:
    """
    확인/취소 키보드 생성

    Args:
        confirm_action: 확인 버튼 콜백
        cancel_action: 취소 버튼 콜백
        confirm_label: 확인 버튼 라벨
        cancel_label: 취소 버튼 라벨

    Returns:
        KeyboardBuilder 인스턴스
    """
    return KeyboardBuilder().add_row(
        (confirm_label, confirm_action),
        (cancel_label, cancel_action)
    )


def build_pagination_keyboard(
    current_page: int,
    total_pages: int,
    action_prefix: str,
    extra_buttons: Optional[List[Tuple[str, str]]] = None
) -> KeyboardBuilder:
    """
    페이지네이션 키보드 생성

    Args:
        current_page: 현재 페이지 (0-based)
        total_pages: 전체 페이지 수
        action_prefix: 콜백 접두사 (예: "log_page")
        extra_buttons: 추가 버튼들

    Returns:
        KeyboardBuilder 인스턴스
    """
    kb = KeyboardBuilder()

    # 페이지 네비게이션
    nav_buttons = []
    if current_page > 0:
        nav_buttons.append(("⬅️ 이전", f"{action_prefix}:{current_page - 1}"))
    nav_buttons.append((f"{current_page + 1}/{total_pages}", "noop"))
    if current_page < total_pages - 1:
        nav_buttons.append(("다음 ➡️", f"{action_prefix}:{current_page + 1}"))

    if nav_buttons:
        kb.add_row(*nav_buttons)

    # 추가 버튼
    if extra_buttons:
        for label, data in extra_buttons:
            kb.add_button(label, data)

    return kb
