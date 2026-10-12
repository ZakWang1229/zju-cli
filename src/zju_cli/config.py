from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError


def read_dotenv(path: Path, *, required: bool = False) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError as exc:
        if required:
            raise ConfigError(f"配置文件不存在：{path}") from exc
        return {}
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"配置文件无法读取：{path}") from exc
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True)
class Settings:
    username: str
    password: str = field(repr=False)
    session_ttl_seconds: float = 900
    timeout_seconds: float = 30
    ffmpeg_bin: str = "ffmpeg"

    @classmethod
    def load(cls, config_path: Path | None = None, timeout: float | None = None, *, load_cwd=True) -> Settings:
        values = read_dotenv(Path.home() / ".secrets" / "zju-cli" / ".env")
        if config_path is not None:
            values.update(read_dotenv(config_path, required=True))
        elif load_cwd:
            values.update(read_dotenv(Path.cwd() / ".env"))

        def value(key: str, default: str = "") -> str:
            return os.environ.get(key, values.get(key, default))

        user, password = value("ZJU_USER"), value("ZJU_PASS")
        if not user or not password:
            raise ConfigError(
                "未配置 ZJU_USER / ZJU_PASS；可设置环境变量，或写入 "
                "~/.secrets/zju-cli/.env"
            )
        try:
            ttl = float(value("ZJU_SESSION_TTL_MS", "900000")) / 1000
            request_timeout = timeout if timeout is not None else float(value("ZJU_HTTP_TIMEOUT", "30"))
            if not all(math.isfinite(n) and n > 0 for n in (ttl, request_timeout)):
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise ConfigError("会话时长和 HTTP 超时必须是有限的正数") from exc
        return cls(user, password, ttl, request_timeout, value("FFMPEG_BIN", "ffmpeg"))
