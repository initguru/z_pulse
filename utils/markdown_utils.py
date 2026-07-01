"""
Markdown 유틸리티
"""

import re


def escape_markdown(text: str) -> str:
    """
    MarkdownV2용 특수 문자를 이스케이프

    Args:
        text: 이스케이프할 텍스트

    Returns:
        이스케이프된 텍스트

    Examples:
        >>> escape_markdown("Hello (World)")
        'Hello \\\\(World\\\\)'
    """
    # 일부 특수 문자들: _ * [ ] ( ) ~ ` > # + - = | { } . !
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))


def safe_code_block(content: str, language: str = "text") -> str:
    """
    안전한 코드 블록 생성 (백틱 방어 포함)

    Args:
        content: 코드 블록 내용
        language: 언어 지정 (기본값: text)

    Returns:
        Markdown 코드 블록 문자열

    Examples:
        >>> safe_code_block("SELECT * FROM users", "sql")
        '```sql\\nSELECT * FROM users\\n```'
    """
    # 백틱을 작은따옴표로 대체하여 코드 블록 깨짐 방지
    safe_content = content.replace("`", "'")
    return f"```{language}\n{safe_content}\n```"


def truncate_message(
    message: str,
    max_length: int = 4096,
    safe_length: int = 4000,
    suffix: str = "\n... (내용이 너무 길어 잘림)"
) -> str:
    """
    텔레그램 메시지 길이 제한에 맞게 잘라내기

    Args:
        message: 원본 메시지
        max_length: 최대 길이 (기본값: 4096)
        safe_length: 안전 길이 (기본값: 4000)
        suffix: 잘릴 때 추가할 접미사

    Returns:
        길이 제한에 맞게 조정된 메시지

    Examples:
        >>> long_msg = "A" * 5000
        >>> truncated = truncate_message(long_msg)
        >>> len(truncated) < 4096
        True
    """
    if len(message) <= max_length:
        return message

    # 코드 블록이 열려있는지 확인
    code_block_open = message[:safe_length].count("```") % 2 == 1

    truncated = message[:safe_length] + suffix

    # 코드 블록이 열려있으면 닫아주기
    if code_block_open:
        truncated += "```"

    return truncated
