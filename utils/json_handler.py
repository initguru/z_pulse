"""
JSON 파일 I/O 유틸리티
"""

import json
import logging
from pathlib import Path
from typing import Any, Optional, Union
from datetime import datetime

logger = logging.getLogger(__name__)


def load_json(
    file_path: Union[str, Path],
    default: Optional[Any] = None
) -> Any:
    """
    JSON 파일 로드 (에러 처리 포함)

    Args:
        file_path: JSON 파일 경로
        default: 파일이 없거나 오류 시 반환할 기본값

    Returns:
        파싱된 JSON 데이터 또는 기본값

    Examples:
        >>> config = load_json("config.json", default={})
        >>> keywords = load_json(Path("keywords.json"), default={"global": [], "bots": {}})
    """
    path = Path(file_path)

    if not path.exists():
        logger.debug(f"JSON 파일 없음: {path}")
        return default if default is not None else {}

    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 파싱 오류 ({path}): {e}")
        return default if default is not None else {}
    except Exception as e:
        logger.error(f"JSON 로드 실패 ({path}): {e}")
        return default if default is not None else {}


def save_json(
    file_path: Union[str, Path],
    data: Any,
    ensure_ascii: bool = False,
    indent: int = 2,
    add_timestamp: bool = False
) -> bool:
    """
    JSON 파일 저장 (에러 처리 포함)

    Args:
        file_path: 저장할 파일 경로
        data: 저장할 데이터
        ensure_ascii: ASCII 인코딩 강제 여부 (기본값: False)
        indent: 들여쓰기 공백 수 (기본값: 2)
        add_timestamp: last_updated 타임스탬프 추가 여부

    Returns:
        저장 성공 여부

    Examples:
        >>> save_json("config.json", {"enabled": True})
        True
        >>> save_json("settings.json", {"mode": "auto"}, add_timestamp=True)
        True
    """
    path = Path(file_path)

    try:
        # 부모 디렉토리가 없으면 생성
        path.parent.mkdir(parents=True, exist_ok=True)

        # 타임스탬프 추가 옵션
        if add_timestamp and isinstance(data, dict):
            data = {**data, 'last_updated': datetime.now().isoformat()}

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent)

        logger.debug(f"JSON 저장 완료: {path}")
        return True
    except Exception as e:
        logger.error(f"JSON 저장 실패 ({path}): {e}")
        return False


def update_json(
    file_path: Union[str, Path],
    updates: dict,
    create_if_missing: bool = True
) -> bool:
    """
    기존 JSON 파일을 부분 업데이트

    Args:
        file_path: 업데이트할 파일 경로
        updates: 업데이트할 키-값 쌍
        create_if_missing: 파일이 없을 때 생성 여부

    Returns:
        업데이트 성공 여부

    Examples:
        >>> update_json("config.json", {"enabled": True})
        True
    """
    path = Path(file_path)

    if not path.exists():
        if create_if_missing:
            return save_json(path, updates)
        logger.warning(f"JSON 파일 없음 (업데이트 불가): {path}")
        return False

    try:
        data = load_json(path, default={})
        data.update(updates)
        return save_json(path, data)
    except Exception as e:
        logger.error(f"JSON 업데이트 실패 ({path}): {e}")
        return False


def load_json_with_schema(
    file_path: Union[str, Path],
    schema: dict
) -> dict:
    """
    스키마 기반 JSON 로드 (누락된 키에 기본값 적용)

    Args:
        file_path: JSON 파일 경로
        schema: 키별 기본값 스키마

    Returns:
        스키마가 적용된 데이터

    Examples:
        >>> schema = {"enabled": False, "interval": 60, "keywords": []}
        >>> config = load_json_with_schema("config.json", schema)
    """
    data = load_json(file_path, default={})

    # 스키마의 기본값 적용
    result = {**schema}
    if isinstance(data, dict):
        result.update(data)

    return result
