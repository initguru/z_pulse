"""
시간대 변환 및 시간 관련 유틸리티
"""

from datetime import datetime, timezone, timedelta
from typing import Optional


class TimezoneConverter:
    """시간대 변환 유틸리티"""

    US_EASTERN = timezone(timedelta(hours=-4))
    KOREA = timezone(timedelta(hours=9))

    @classmethod
    def us_to_korea(cls, us_datetime: datetime) -> datetime:
        """
        미국 동부시간을 한국시간으로 변환

        Args:
            us_datetime: 미국 동부시간 datetime 객체

        Returns:
            한국시간으로 변환된 datetime 객체
        """
        if us_datetime.tzinfo is None:
            # 시간대 정보가 없으면 미국 동부시간으로 간주
            us_aware = us_datetime.replace(tzinfo=cls.US_EASTERN)
        else:
            us_aware = us_datetime
        return us_aware.astimezone(cls.KOREA)

    @classmethod
    def format_relative_date(cls, target_date, reference_date: Optional[datetime] = None) -> str:
        """
        오늘/내일/모레/날짜 형식으로 반환

        Args:
            target_date: 대상 날짜 (date 또는 datetime)
            reference_date: 기준 날짜 (기본값: 현재 날짜)

        Returns:
            "오늘", "내일", "모레" 또는 "MM/DD" 형식의 문자열
        """
        if reference_date is None:
            reference_date = datetime.now().date()
        elif isinstance(reference_date, datetime):
            reference_date = reference_date.date()

        if isinstance(target_date, datetime):
            target_date = target_date.date()

        delta = (target_date - reference_date).days

        if delta == 0:
            return "오늘"
        elif delta == 1:
            return "내일"
        elif delta == 2:
            return "모레"
        else:
            return target_date.strftime('%m/%d')

    @classmethod
    def get_korea_now(cls) -> datetime:
        """
        현재 한국 시간을 반환

        Returns:
            한국 시간대의 현재 datetime 객체
        """
        return datetime.now(cls.KOREA)

    @classmethod
    def get_us_eastern_now(cls) -> datetime:
        """
        현재 미국 동부 시간을 반환

        Returns:
            미국 동부 시간대의 현재 datetime 객체
        """
        return datetime.now(cls.US_EASTERN)
