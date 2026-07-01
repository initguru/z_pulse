"""
메시지 포맷팅 유틸리티
"""

import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from .markdown_utils import escape_markdown

# ANSI 이스케이프 시퀀스 패턴 (색상, 스타일 등)
_ANSI_ESCAPE_PATTERN = re.compile(r'\x1b\[[0-9;]*m')


def strip_ansi(text: str) -> str:
    """
    ANSI 이스케이프 시퀀스 제거 (텔레그램 전송용)

    Args:
        text: ANSI 코드가 포함된 문자열

    Returns:
        ANSI 코드가 제거된 문자열
    """
    return _ANSI_ESCAPE_PATTERN.sub('', text)


def format_process_detail(
    target: str,
    pid: Optional[int] = None,
    cpu_percent: Optional[float] = None,
    memory_mb: Optional[float] = None,
    uptime: Optional[timedelta] = None,
    entry_count: Optional[int] = None,
    is_ignored: bool = False,
    is_running: bool = True,
    trading_type: Optional[str] = None,
    port: Optional[str] = None,
    trading_type_label: str = "TRADING TYPE",
    port_label: str = "Port No."
) -> str:
    """
    프로세스 상세 정보 포맷팅 (MarkdownV2)

    Args:
        target: 디렉토리명
        pid: 프로세스 ID
        cpu_percent: CPU 사용률
        memory_mb: 메모리 사용량 (MB)
        uptime: 실행 시간
        entry_count: 진입 회차
        is_ignored: 무시 목록 여부
        is_running: 실행 중 여부
        trading_type: 거래 타입 (예: "UPBIT", "BINANCE")
        port: 포트 번호
        trading_type_label: TRADING_TYPE 필드 표시 레이블
        port_label: PORT 필드 표시 레이블

    Returns:
        MarkdownV2 포맷 텍스트
    """
    escaped_target = escape_markdown(target)
    text = f"📄 *프로세스 상세 정보*\n\n📁 *디렉토리*: `{escaped_target}`\n"

    # 거래 타입 정보 (디렉토리 바로 아래에 표시)
    if trading_type and is_running and not is_ignored:
        escaped_trading_type = escape_markdown(trading_type)
        escaped_label = escape_markdown(trading_type_label)
        text += f"💱 *{escaped_label}*: `{escaped_trading_type}`\n"

    # 포트 정보 (거래 타입 아래에 표시)
    if port and is_running and not is_ignored:
        escaped_port = escape_markdown(port)
        escaped_label = escape_markdown(port_label)
        text += f"🔌 *{escaped_label}*: `{escaped_port}`\n"

    if is_ignored:
        text += "💀 이 디렉토리는 현재 모니터링에서 제외되어 있습니다\\."
    elif not is_running:
        text += "❌ 프로세스가 현재 실행 중이 아닙니다\\."
    else:
        if pid is not None:
            text += f"🆔 *PID*: `{pid}`\n"
        if cpu_percent is not None:
            cpu_str = escape_markdown(f"{cpu_percent:.1f}")
            text += f"⚙️ *CPU*: {cpu_str}%\n"
        if memory_mb is not None:
            mem_str = escape_markdown(f"{memory_mb:.1f}")
            text += f"🧠 *Memory*: {mem_str} MB\n"
        if uptime is not None:
            uptime_str = escape_markdown(str(uptime).split('.')[0])
            text += f"⏱️ *실행 시간*: {uptime_str}\n"
        if entry_count is not None:
            text += f"📊 *진입 회차*: {entry_count}\n"

    return text


def format_pair_trading_detail(
    target: str,
    assignment_enabled: bool,
    assignment_label: str,
    assignment_state: str,
    assignment_description: str,
    pid: Optional[int] = None,
    cpu_percent: Optional[float] = None,
    memory_mb: Optional[float] = None,
    uptime: Optional[timedelta] = None,
    entry_count: Optional[int] = None,
    trading_type: Optional[str] = None,
    port: Optional[str] = None,
    current_pair: Optional[str] = None,
    trading_type_label: str = "TRADING TYPE",
    port_label: str = "Port No.",
) -> str:
    """외부 페어트레이딩 봇 상세 정보 포맷팅 (MarkdownV2)."""
    escaped_target = escape_markdown(target)
    text = f"📄 *프로세스 상세 정보*\n\n📁 *디렉토리*: `{escaped_target}`\n"

    if trading_type:
        escaped_trading_type = escape_markdown(trading_type)
        escaped_label = escape_markdown(trading_type_label)
        text += f"💱 *{escaped_label}*: `{escaped_trading_type}`\n"

    if port:
        escaped_port = escape_markdown(port)
        escaped_label = escape_markdown(port_label)
        text += f"🔌 *{escaped_label}*: `{escaped_port}`\n"

    assignment_icon = "🤖" if assignment_enabled else "⏸️"
    text += f"{assignment_icon} *자동 배정*: {escape_markdown(assignment_label)}\n"
    text += f"🧭 *자동 배정 상태*: {escape_markdown(assignment_state)}\n"

    if current_pair:
        text += f"🔗 *현재 페어*: `{escape_markdown(current_pair)}`\n"

    if pid is None:
        text += "⚪ *프로세스*: 정지\n"
    else:
        text += f"🟢 *프로세스*: 실행 중 \\(PID `{pid}`\\)\n"
        if cpu_percent is not None:
            cpu_str = escape_markdown(f"{cpu_percent:.1f}")
            text += f"⚙️ *CPU*: {cpu_str}%\n"
        if memory_mb is not None:
            mem_str = escape_markdown(f"{memory_mb:.1f}")
            text += f"🧠 *Memory*: {mem_str} MB\n"
        if uptime is not None:
            uptime_str = escape_markdown(str(uptime).split('.')[0])
            text += f"⏱️ *실행 시간*: {uptime_str}\n"
        if entry_count is not None:
            text += f"📊 *진입 회차*: {entry_count}\n"

    text += f"\n📝 *설명*: {escape_markdown(assignment_description)}"
    return text


def format_slot_detail(
    target: str,
    assignment_enabled: bool,
    assignment_label: str,
    assignment_state: str,
    assignment_description: str,
    slot_id: str,
    slot_type: str,
    exchange_id: str,
    net_label: str,
    margin: str,
    pid: Optional[int] = None,
    cpu_percent: Optional[float] = None,
    memory_mb: Optional[float] = None,
    uptime: Optional[timedelta] = None,
) -> str:
    """슬롯 런타임 봇 상세 정보 포맷팅 (MarkdownV2). 외부봇 format_pair_trading_detail과 구조 통일."""
    escaped_target = escape_markdown(target)
    text = f"📄 *프로세스 상세 정보*\n\n📁 *디렉토리*: `{escaped_target}`\n"

    assignment_icon = "🤖" if assignment_enabled else "⏸️"
    text += f"{assignment_icon} *자동 배정*: {escape_markdown(assignment_label)}\n"
    text += f"🧭 *자동 배정 상태*: {escape_markdown(assignment_state)}\n"

    text += f"🎰 *슬롯*: ID `{escape_markdown(slot_id)}` · 타입 `{escape_markdown(slot_type)}`\n"
    text += f"🏦 *거래소*: `{escape_markdown(exchange_id)}` \\({escape_markdown(net_label)}\\)\n"
    text += f"💰 *마진*: `{escape_markdown(margin)} USDT`\n"

    if pid is None:
        text += "⚪ *프로세스*: 정지\n"
    else:
        text += f"🟢 *프로세스*: 실행 중 \\(PID `{pid}`\\)\n"
        if cpu_percent is not None:
            cpu_str = escape_markdown(f"{cpu_percent:.1f}")
            text += f"⚙️ *CPU*: {cpu_str}%\n"
        if memory_mb is not None:
            mem_str = escape_markdown(f"{memory_mb:.1f}")
            text += f"🧠 *Memory*: {mem_str} MB\n"
        if uptime is not None:
            uptime_str = escape_markdown(str(uptime).split('.')[0])
            text += f"⏱️ *실행 시간*: {uptime_str}\n"

    text += f"\n📝 *설명*: {escape_markdown(assignment_description)}"
    return text


def format_dashboard_summary(
    valid_count: int,
    total_count: int,
    ignored_count: int,
    cpu_percent: float,
    memory_percent: float,
    status_icon: str = "🟢",
    auto_assignment_on_count: int = 0,
    auto_assignment_off_count: int = 0,
    slot_management_on_count: int = 0,
    slot_management_off_count: int = 0,
    show_auto_assignment: bool = True,
) -> str:
    """
    대시보드 요약 포맷팅 (MarkdownV2)

    Args:
        valid_count: 실행 중인 프로세스 수
        total_count: 전체 대상 수
        ignored_count: 무시된 수
        cpu_percent: 시스템 CPU 사용률
        memory_percent: 시스템 메모리 사용률
        status_icon: 상태 아이콘
        show_auto_assignment: False면 자동배정 레이블 줄을 생략한다

    Returns:
        MarkdownV2 포맷 텍스트
    """
    timestamp = escape_markdown(datetime.now().strftime('%H:%M:%S'))

    auto_lines = ""
    if show_auto_assignment:
        auto_lines = (
            f"🤖 *외부봇 자동 배정*: ON {auto_assignment_on_count} \\| OFF {auto_assignment_off_count}\n"
            f"🤖 *슬롯봇 자동 배정*: ON {slot_management_on_count} \\| OFF {slot_management_off_count}\n"
        )

    return (
        f"📊 *Z\\-Pulse 모니터링 대시보드*\n\n"
        f"{status_icon} *전체 상태*: {valid_count} / "
        f"{total_count}개 실행 중 \\({ignored_count}개 무시\\)\n"
        f"{auto_lines}"
        f"🖥️ *시스템 부하*: CPU {escape_markdown(str(cpu_percent))}% \\| "
        f"Memory {escape_markdown(str(memory_percent))}%\n"
        f"⏰ *마지막 업데이트*: {timestamp}"
    )


def format_status_message(
    icon: str,
    title: str,
    message: str,
    use_markdown: bool = False
) -> str:
    """
    상태 메시지 포맷팅

    Args:
        icon: 이모지 아이콘
        title: 제목
        message: 메시지 본문
        use_markdown: MarkdownV2 사용 여부

    Returns:
        포맷된 메시지
    """
    if use_markdown:
        return f"{icon} *{escape_markdown(title)}*\n\n{escape_markdown(message)}"
    return f"{icon} {title}\n\n{message}"


def format_error_message(error: Exception, context: str = "") -> str:
    """
    에러 메시지 포맷팅

    Args:
        error: 예외 객체
        context: 에러 컨텍스트

    Returns:
        포맷된 에러 메시지
    """
    if context:
        return f"❌ {context} 중 오류가 발생했습니다:\n{str(error)}"
    return f"❌ 오류가 발생했습니다:\n{str(error)}"


def format_log_caption(
    dir_name: str,
    tail_lines: Optional[int] = None,
    timestamp: Optional[datetime] = None
) -> str:
    """
    로그 파일 캡션 포맷팅 (MarkdownV2)

    Args:
        dir_name: 디렉토리명
        tail_lines: 마지막 줄 수 (None이면 전체)
        timestamp: 타임스탬프 (기본: 현재 시간)

    Returns:
        MarkdownV2 포맷 캡션
    """
    if timestamp is None:
        timestamp = datetime.now()

    time_str = escape_markdown(timestamp.strftime('%H:%M:%S'))
    esc_dir_name = escape_markdown(dir_name)

    if tail_lines:
        return f"📄 *{esc_dir_name}* 로그 \\(마지막 {tail_lines}줄\\)\n⏰ {time_str}"
    return f"📄 *{esc_dir_name}* 전체 로그\n⏰ {time_str}"


def format_keyword_header(
    target_bot: Optional[str] = None,
    use_markdown: bool = True
) -> str:
    """
    키워드 관련 헤더 포맷팅

    Args:
        target_bot: 대상 봇명 (None이면 글로벌)
        use_markdown: MarkdownV2 사용 여부

    Returns:
        포맷된 헤더 텍스트
    """
    if use_markdown:
        if target_bot:
            return f"\\({escape_markdown(target_bot)}\\)"
        return "\\(글로벌\\)"
    else:
        if target_bot:
            return f"({target_bot})"
        return "(글로벌)"


def format_batch_result(
    action: str,
    success_count: int,
    total_count: int
) -> str:
    """
    배치 작업 결과 포맷팅

    Args:
        action: 수행한 작업명
        success_count: 성공 개수
        total_count: 전체 개수

    Returns:
        포맷된 결과 메시지
    """
    if success_count == total_count:
        return f"✅ {total_count}개 프로세스 {action} 완료!"
    elif success_count > 0:
        return f"⚠️ {success_count}/{total_count}개 프로세스 {action} 완료"
    return f"❌ {action} 실패"


def format_confirm_message(
    action: str,
    target: str,
    warning: Optional[str] = None
) -> str:
    """
    확인 메시지 포맷팅

    Args:
        action: 수행할 작업
        target: 대상
        warning: 경고 메시지 (선택)

    Returns:
        포맷된 확인 메시지
    """
    msg = f"⚠️ {target}을(를) {action}하시겠습니까?"
    if warning:
        msg += f"\n\n{warning}"
    return msg
