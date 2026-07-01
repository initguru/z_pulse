"""
Setting Definitions Module

각 설정 항목의 타입, 제약조건, 표시명 등을 정의합니다.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional
from dataclasses import dataclass


class SettingType(Enum):
    """설정 값 타입"""
    INT = "int"
    FLOAT = "float"
    STRING = "string"
    ARRAY = "array"          # 배열형: [1,2,3]
    BOOLEAN = "boolean"      # True/False
    DIRECTION = "direction"  # BUY/SELL


class ReloadMode(Enum):
    """설정 반영 방식"""
    HOT = "hot"
    HOT_WITH_REINIT = "hot_with_reinit"
    COLD = "cold"


@dataclass
class SettingDefinition:
    """
    설정 항목 정의

    Attributes:
        key: 설정 키 (setting.env 파일의 키)
        display_name: 사용자에게 표시되는 이름
        setting_type: 설정 타입
        min_value: 최소값 (숫자형만)
        max_value: 최대값 (숫자형만)
        allowed_values: 허용 값 목록 (양자택일형)
        description: 설정 설명 (선택사항)
    """
    key: str
    display_name: str
    setting_type: SettingType
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    allowed_values: Optional[list] = None
    description: Optional[str] = None
    reload_mode: ReloadMode = ReloadMode.COLD

    def get_type_hint(self) -> str:
        """사용자에게 보여줄 타입 힌트 반환"""
        if self.setting_type == SettingType.INT:
            return "정수"
        elif self.setting_type == SettingType.FLOAT:
            return "소수"
        elif self.setting_type == SettingType.STRING:
            return "문자열"
        elif self.setting_type == SettingType.ARRAY:
            return "배열 (예: [1,2,3])"
        elif self.setting_type == SettingType.BOOLEAN:
            return "BOOLEAN"
        elif self.setting_type == SettingType.DIRECTION:
            return "DIRECTION"
        return "알 수 없음"

    def get_reload_badge(self) -> str:
        """UI 표시용 반영 방식 배지 문자열"""
        if self.reload_mode == ReloadMode.HOT:
            return "HOT"
        if self.reload_mode == ReloadMode.HOT_WITH_REINIT:
            return "HOT_REINIT"
        return "COLD"


# 설정 항목 정의 (키 순서는 UI 표시 순서)
SETTING_DEFINITIONS: dict[str, SettingDefinition] = {
    # 문자형
    "ALIAS": SettingDefinition(
        key="ALIAS",
        display_name="알림 별명",
        setting_type=SettingType.STRING,
        description="봇 알림에 표시될 별명",
        reload_mode=ReloadMode.HOT,
    ),
    "COIN": SettingDefinition(
        key="COIN",
        display_name="코인 심볼",
        setting_type=SettingType.STRING,
        description="거래 코인 심볼"
    ),
    "COIN1": SettingDefinition(
        key="COIN1",
        display_name="코인 심볼 1",
        setting_type=SettingType.STRING,
        description="페어/헷징 거래 코인 1"
    ),
    "COIN2": SettingDefinition(
        key="COIN2",
        display_name="코인 심볼 2",
        setting_type=SettingType.STRING,
        description="페어/헷징 거래 코인 2"
    ),

    # 숫자형 (정수)
    "LEVERAGE": SettingDefinition(
        key="LEVERAGE",
        display_name="레버리지",
        setting_type=SettingType.INT,
        min_value=1,
        max_value=50,
        description="레버리지 배율"
    ),
    "TRADING_LIMIT_COUNT": SettingDefinition(
        key="TRADING_LIMIT_COUNT",
        display_name="최대 매매 횟수",
        setting_type=SettingType.INT,
        min_value=1,
        description="최대 매매 진입 횟수",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "CHART_TIME": SettingDefinition(
        key="CHART_TIME",
        display_name="RSI 차트 시간",
        setting_type=SettingType.INT,
        min_value=1,
        description="RSI 변곡 판단용 차트 시간(분)"
    ),
    "RSI_REVERSAL_THRESHOLD": SettingDefinition(
        key="RSI_REVERSAL_THRESHOLD",
        display_name="RSI 변곡값",
        setting_type=SettingType.INT,
        min_value=0,
        max_value=10,
        description="RSI 변곡점 기준값"
    ),
    "PORT": SettingDefinition(
        key="PORT",
        display_name="Port 번호",
        setting_type=SettingType.INT,
        min_value=1024,
        max_value=65535,
        description="봇 통신 포트 번호"
    ),
    "BROWSER_DELAY_SECOND": SettingDefinition(
        key="BROWSER_DELAY_SECOND",
        display_name="로그인 대기 시간",
        setting_type=SettingType.INT,
        min_value=0,
        description="브라우저 로그인 대기 시간(초)"
    ),

    # 숫자형 (소수)
    "TRADING_AMOUNT": SettingDefinition(
        key="TRADING_AMOUNT",
        display_name="매매 수량",
        setting_type=SettingType.FLOAT,
        min_value=0.0,
        description="1회 매매당 거래 수량"
    ),
    "TRADING_MARGIN": SettingDefinition(
        key="TRADING_MARGIN",
        display_name="최대 매매 마진",
        setting_type=SettingType.FLOAT,
        min_value=0.0,
        description="최대 사용 가능한 마진",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "ENTRY_TRIGGER_PERCENT": SettingDefinition(
        key="ENTRY_TRIGGER_PERCENT",
        display_name="진입 %",
        setting_type=SettingType.FLOAT,
        min_value=0.05,
        max_value=1.00,
        description="페어 매매 진입 기준 스프레드 %",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "CLOSE_TRIGGER_PERCENT": SettingDefinition(
        key="CLOSE_TRIGGER_PERCENT",
        display_name="매도 %",
        setting_type=SettingType.FLOAT,
        min_value=0.05,
        max_value=1.00,
        description="페어 매매 종료 기준 스프레드 %",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "STOP_LOSS_PERCENT": SettingDefinition(
        key="STOP_LOSS_PERCENT",
        display_name="손절 %",
        setting_type=SettingType.FLOAT,
        min_value=0.0,
        description="사용 마진 기준 손절 % (PAIR V2: TRADING_MARGIN 기준)",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "SPREAD_BPS": SettingDefinition(
        key="SPREAD_BPS",
        display_name="스프레드 간격(<10)",
        setting_type=SettingType.FLOAT,
        min_value=0.0,
        max_value=10.0,
        description="스프레드 거래 간격 (BPS)"
    ),

    # 배열형
    "SELL_TRIGGER_PERCENT_LIST": SettingDefinition(
        key="SELL_TRIGGER_PERCENT_LIST",
        display_name="회차별 매도 %",
        setting_type=SettingType.ARRAY,
        description="회차별 매도 기준 % 리스트 (예: [1.0, 2.0, 3.0])",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "BUY_TRIGGER_PERCENT_LIST": SettingDefinition(
        key="BUY_TRIGGER_PERCENT_LIST",
        display_name="회차별 매수 %",
        setting_type=SettingType.ARRAY,
        description="회차별 매수 기준 % 리스트 (예: [1.0, 2.0, 3.0])",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "RSI_RANGE": SettingDefinition(
        key="RSI_RANGE",
        display_name="RSI 매매 구간",
        setting_type=SettingType.ARRAY,
        description="RSI 매매 범위 (예: [30, 70])",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),

    # 양자택일 1형 (True/False)
    "MOMENTUM_OPTION": SettingDefinition(
        key="MOMENTUM_OPTION",
        display_name="모멘텀 지표 사용",
        setting_type=SettingType.BOOLEAN,
        allowed_values=["true", "false"],
        description="롱/숏 스위칭 판단용 모멘텀 지표 사용",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "MANUAL_CLOSE_OPTION": SettingDefinition(
        key="MANUAL_CLOSE_OPTION",
        display_name="포지션/봇 종료",
        setting_type=SettingType.BOOLEAN,
        allowed_values=["true", "false"],
        description="포지션 분할 정리 후 종료",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),
    "EXIT_RESERVATION": SettingDefinition(
        key="EXIT_RESERVATION",
        display_name="익절/봇 종료",
        setting_type=SettingType.BOOLEAN,
        allowed_values=["true", "false"],
        description="최종 익절 후 봇 종료 여부",
        reload_mode=ReloadMode.HOT,
    ),
    "AUTO_AMOUNT_SYNC": SettingDefinition(
        key="AUTO_AMOUNT_SYNC",
        display_name="물량 동기화",
        setting_type=SettingType.BOOLEAN,
        allowed_values=["true", "false"],
        description="헷징 물량 괴리 시 자동 물량 동기화",
        reload_mode=ReloadMode.HOT_WITH_REINIT,
    ),

    # 양자택일 2형 (BUY/SELL)
    "HEDGE_DIRECTION": SettingDefinition(
        key="HEDGE_DIRECTION",
        display_name="BUY/SELL",
        setting_type=SettingType.DIRECTION,
        allowed_values=["BUY", "SELL"],
        description="1번 거래소 거래 방향"
    ),

    # 기타
    "TRADING_TYPE": SettingDefinition(
        key="TRADING_TYPE",
        display_name="시그니쳐",
        setting_type=SettingType.STRING,
        description="거래소/봇 종류 명시 (예: GRVT_PAIR, GRVT_PAIR_V2)"
    ),
}


def get_editable_settings() -> dict[str, str]:
    """
    기존 EDITABLE_SETTINGS 형식으로 반환
    (하위 호환성 유지용)

    Returns:
        {key: display_name} 딕셔너리
    """
    return {key: definition.display_name for key, definition in SETTING_DEFINITIONS.items()}
