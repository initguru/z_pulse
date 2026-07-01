"""
Setting Validators Module

설정 값 검증 로직을 담당합니다.
"""

import ast
import logging
from typing import Any, Optional

from z_pulse.config.setting_definitions import SettingDefinition, SettingType

logger = logging.getLogger(__name__)


class ValidationError(Exception):
    """설정 값 검증 실패 예외"""
    pass


class SettingValidator:
    """설정 값 검증기"""

    @staticmethod
    def validate(definition: SettingDefinition, value: str) -> tuple[bool, Optional[str], Any]:
        """
        설정 값을 검증합니다.

        Args:
            definition: 설정 정의
            value: 사용자가 입력한 값 (문자열)

        Returns:
            (성공 여부, 에러 메시지, 변환된 값) 튜플
            - 성공: (True, None, converted_value)
            - 실패: (False, error_message, None)
        """
        try:
            if definition.setting_type == SettingType.INT:
                return SettingValidator._validate_int(definition, value)
            elif definition.setting_type == SettingType.FLOAT:
                return SettingValidator._validate_float(definition, value)
            elif definition.setting_type == SettingType.STRING:
                return SettingValidator._validate_string(definition, value)
            elif definition.setting_type == SettingType.ARRAY:
                return SettingValidator._validate_array(definition, value)
            elif definition.setting_type == SettingType.BOOLEAN:
                return SettingValidator._validate_boolean(definition, value)
            elif definition.setting_type == SettingType.DIRECTION:
                return SettingValidator._validate_direction(definition, value)
            else:
                return False, f"알 수 없는 설정 타입: {definition.setting_type}", None

        except Exception as e:
            logger.error(f"검증 중 오류 발생: {e}")
            return False, f"검증 중 오류 발생: {str(e)}", None

    @staticmethod
    def _validate_int(definition: SettingDefinition, value: str) -> tuple[bool, Optional[str], Any]:
        """정수형 검증"""
        try:
            int_value = int(value)
        except ValueError:
            return False, f"'{value}'는 정수가 아닙니다. 정수를 입력해주세요.", None

        # 범위 검증
        if definition.min_value is not None and int_value < definition.min_value:
            return False, f"값은 {int(definition.min_value)} 이상이어야 합니다. (입력: {int_value})", None

        if definition.max_value is not None and int_value > definition.max_value:
            return False, f"값은 {int(definition.max_value)} 이하여야 합니다. (입력: {int_value})", None

        return True, None, str(int_value)

    @staticmethod
    def _validate_float(definition: SettingDefinition, value: str) -> tuple[bool, Optional[str], Any]:
        """소수형 검증"""
        try:
            float_value = float(value)
        except ValueError:
            return False, f"'{value}'는 숫자가 아닙니다. 숫자를 입력해주세요.", None

        # 범위 검증
        if definition.min_value is not None and float_value < definition.min_value:
            return False, f"값은 {definition.min_value} 이상이어야 합니다. (입력: {float_value})", None

        if definition.max_value is not None and float_value > definition.max_value:
            return False, f"값은 {definition.max_value} 이하여야 합니다. (입력: {float_value})", None

        return True, None, str(float_value)

    @staticmethod
    def _validate_string(definition: SettingDefinition, value: str) -> tuple[bool, Optional[str], Any]:
        """문자열형 검증"""
        if not value or not value.strip():
            return False, "빈 문자열은 입력할 수 없습니다.", None

        value_stripped = value.strip()
        if definition.key == "TRADING_TYPE":
            from z_pulse.integration.z_flow_bridge import ZFlowBridge, ZFlowBridgeError  # lazy — avoids circular
            try:
                ZFlowBridge.resolve_trading_type(value_stripped)
                normalized = ZFlowBridge.normalize_trading_type(value_stripped)
            except ZFlowBridgeError as exc:
                return False, str(exc), None
            except Exception as exc:  # lazy import failure or unexpected z_flow error
                return False, f"Trading type validation error: {exc}", None
            return True, None, normalized

        return True, None, value_stripped

    @staticmethod
    def _validate_array(definition: SettingDefinition, value: str) -> tuple[bool, Optional[str], Any]:
        """배열형 검증"""
        # 배열 파싱 시도
        try:
            # ast.literal_eval을 사용하여 안전하게 파싱
            parsed = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return False, (
                f"'{value}'는 올바른 배열 형식이 아닙니다.\n"
                "예시: [1, 2, 3] 또는 [1.5, 2.0, 3.5]"
            ), None

        # 리스트인지 확인
        if not isinstance(parsed, list):
            return False, "배열 형식 [값1, 값2, ...] 으로 입력해주세요.", None

        # 빈 배열 검증
        if len(parsed) == 0:
            return False, "빈 배열은 입력할 수 없습니다. 최소 1개 이상의 값을 입력해주세요.", None

        # 배열 요소 타입 검증 (숫자만 허용)
        for item in parsed:
            if not isinstance(item, (int, float)):
                return False, f"배열 요소는 숫자여야 합니다. 잘못된 값: {item}", None

        return True, None, str(parsed)

    @staticmethod
    def _validate_boolean(definition: SettingDefinition, value: str) -> tuple[bool, Optional[str], Any]:
        """Boolean형 검증 - allowed_values와 정확히 일치해야 함 (대소문자 구분)"""
        value_stripped = value.strip()

        if not definition.allowed_values:
            # allowed_values가 없으면 기본값으로 검증
            return False, "설정 정의에 허용값이 지정되지 않았습니다.", None

        if value_stripped not in definition.allowed_values:
            allowed = " 또는 ".join([f"'{v}'" for v in definition.allowed_values])
            return False, f"'{value}'는 올바른 값이 아닙니다. {allowed}를 입력해주세요.", None

        return True, None, value_stripped

    @staticmethod
    def _validate_direction(definition: SettingDefinition, value: str) -> tuple[bool, Optional[str], Any]:
        """Direction형 검증 - allowed_values와 정확히 일치해야 함 (대소문자 구분)"""
        value_stripped = value.strip()

        if not definition.allowed_values:
            # allowed_values가 없으면 기본값으로 검증
            return False, "설정 정의에 허용값이 지정되지 않았습니다.", None

        if value_stripped not in definition.allowed_values:
            allowed = " 또는 ".join([f"'{v}'" for v in definition.allowed_values])
            return False, f"'{value}'는 올바른 값이 아닙니다. {allowed}를 입력해주세요.", None

        return True, None, value_stripped
