"""
Z-Pulse 설정 상수
"""


class TimeoutConfig:
    """타임아웃 설정 (초 단위)"""
    TELEGRAM_MESSAGE = 3.0
    PROCESS_WAIT = 3.0
    PROCESS_TERMINATE = 5.0
    TERMINAL_CLEANUP = 10.0
    CHECK_INTERVAL = 10.0
    HTTP_TIMEOUT = 30.0
    FILE_TRANSFER = 120.0
    APPLESCRIPT = 20.0
    SLEEP_SHORT = 1.0
    SLEEP_MEDIUM = 2.0
    SLEEP_LONG = 3.0
    SLEEP_VERY_LONG = 10.0
    CPU_INTERVAL = 0.1


class SizeConfig:
    """크기 및 길이 제한"""
    MESSAGE_MAX_LENGTH = 4096
    MESSAGE_SAFE_LENGTH = 4000
    LOG_TAIL_LINES = 100
    LOG_HISTORY_LINES = 40
    IMAGE_MIN_WIDTH = 800
    IMAGE_MAX_WIDTH = 1200
    TEXT_LINE_MAX_LENGTH = 120
    BATCH_SIZE = 5
    BATCH_DELAY = 0.2


class FileConfig:
    """파일명 상수"""
    MONITOR_LOG = "monitor.log"
    SETTING_ENV = "setting.env"
    LOG_KEYWORDS = "log_keywords.json"
    IGNORED_DIRS = "ignored_dir"
    VERSION_FILE = "version.txt"
    BOT_STATE_FILE = "rotation_state"  # 봇 로테이션 상태 파일 (EXIT_RESERVATION / MANUAL_STOP / WAITING)


class DurationConfig:
    """기간 설정 (분 단위)"""
    LOG_STALL_MINUTES = 10
    LOG_STALL_GRACE_MINUTES = 3  # 프로세스 재기동 직후 stale log 오탐 방지 유예시간
    COOLDOWN_DEFAULT_SECONDS = 3600


class CacheConfig:
    """캐싱 설정"""
    CACHE_TTL_SECONDS = 2.0
    CACHE_MAX_SIZE = 128
    FONT_CACHE_SIZE = 10


class Constants:
    """기타 상수"""
    PARSE_MODE = "MarkdownV2"
    PROCESS_NAME_DEFAULT = "2oolkit-bot-macos-arm64"
    SEPARATOR_LINE = "-" * 60


class ColorConfig:
    """색상 설정 (RGB)"""
    BG_DARK = (40, 44, 52)  # 어두운 터미널 배경
    TEXT_BRIGHT = (235, 235, 235)  # 밝은 텍스트 색상
    FONT_SIZE_MULTIPLIER = 0.6


class Icons:
    """이모지 및 아이콘"""
    SUCCESS = "✅"
    FAILURE = "❌"
    WARNING = "⚠️"
    REFRESH = "🔄"
    CHART = "📊"
    STOP = "⏹"
    PLAY = "▶️"
    RUNNING = "🟢"
    STOPPED = "🔴"
    IGNORED = "💀"
    SHIELD = "🛡️"
    CIRCLE_WHITE = "⚪"
    BELL = "🔔"
    FIRE = "🔥"
    AMBULANCE = "🚑"
    BACK = "🔙"
    FOLDER = "📁"
    KEY = "🔑"
    ALERT = "🚨"
    DOCUMENT = "📄"
    CONFUSED = "🤷"
    COMPUTER = "🖥️"
    CLOCK = "⏰"
    ADD = "➕"
    EDIT = "✏️"
    DELETE = "🗑️"


class EventDaysConfig:
    """경제 이벤트 기간 설정 (일 단위)"""
    UPCOMING_DAYS = 7
    HIGH_IMPORTANCE_DAYS = 7
    TODAY = 0
    TOMORROW = 1
    DAY_AFTER_TOMORROW = 2


class TimezoneConfig:
    """시간대 설정"""
    US_EASTERN_OFFSET = -4
    KOREA_OFFSET = 9
