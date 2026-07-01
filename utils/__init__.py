"""
Utility functions for telegram_bot
"""

from .time_utils import TimezoneConverter
from .grid_calculator import calculate_grid_layout
from .async_helpers import safe_run_async, safe_send_message, safe_edit_message
from .markdown_utils import escape_markdown, safe_code_block, truncate_message
from .error_handler import (
    safe_file_operation,
    safe_json_operation,
    safe_process_operation,
    safe_telegram_operation,
    with_retry,
    is_transient_telegram_error,
    TRANSIENT_TELEGRAM_ERRORS,
)
from .cache import SmartCache, SingleValueCache
from .log_setup import setup_logging
from .json_handler import load_json, save_json, update_json, load_json_with_schema
from .formatters import (
    format_process_detail,
    format_pair_trading_detail,
    format_slot_detail,
    format_dashboard_summary,
    format_status_message,
    format_error_message,
    format_log_caption,
    format_keyword_header,
    format_batch_result,
    format_confirm_message
)

__all__ = [
    # Time
    'TimezoneConverter',
    # Grid
    'calculate_grid_layout',
    # Async helpers
    'safe_run_async',
    'safe_send_message',
    'safe_edit_message',
    # Markdown
    'escape_markdown',
    'safe_code_block',
    'truncate_message',
    # Error handling
    'safe_file_operation',
    'safe_json_operation',
    'safe_process_operation',
    'safe_telegram_operation',
    'with_retry',
    'is_transient_telegram_error',
    'TRANSIENT_TELEGRAM_ERRORS',
    # Cache
    'SmartCache',
    'SingleValueCache',
    # Logging
    'setup_logging',
    # JSON handler
    'load_json',
    'save_json',
    'update_json',
    'load_json_with_schema',
    # Formatters
    'format_process_detail',
    'format_pair_trading_detail',
    'format_slot_detail',
    'format_dashboard_summary',
    'format_status_message',
    'format_error_message',
    'format_log_caption',
    'format_keyword_header',
    'format_batch_result',
    'format_confirm_message',
]
