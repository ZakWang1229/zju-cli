from __future__ import annotations

import math
import re

from .errors import InputError, RemoteError


def identifier(value) -> str:
    if value is None or value == "":
        raise InputError("缺少 ID")
    text = str(value)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise InputError("ID 只能包含字母、数字、下划线和连字符")
    return text


def required_list(value: dict, key: str) -> list:
    rows = value.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RemoteError(f"接口返回格式异常：缺少有效的 {key} 列表")
    return rows


def validate_audio_timeout(timeout):
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise InputError("音频提取超时必须是有限的正数")
