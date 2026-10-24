"""Current-user Windows logon startup; registry values never contain credentials."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from .config import Settings
from .errors import ConfigError, LocalFileError

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "zju-cli"


def configure(action, *, config=None, timeout=None):
    if os.name != "nt":
        raise ConfigError("开机自启动当前仅支持 Windows 用户登录启动")
    import winreg
    if action == "enable":
        candidate = Path(config).expanduser().resolve() if config else Path.cwd() / ".env"
        config = candidate if config or candidate.is_file() else None
        # Validate now, without contacting either university platform.
        Settings.load(config, timeout, load_cwd=False)
        executable = Path(sys.executable).with_name("pythonw.exe")
        if not executable.is_file():
            raise ConfigError("未找到 pythonw.exe，无法配置隐藏启动")
        arguments = [str(executable), "-m", "zju_cli.autostart"]
        if config:
            arguments += ["--config", str(config)]
        if timeout is not None:
            arguments += ["--http-timeout", str(timeout)]
        command = subprocess.list2cmdline(arguments)
    try:
        if action == "status":
            try:
                key_handle = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
            except FileNotFoundError:
                return {"enabled": False, "scope": "current-user-logon", "command": None}
        else:
            key_handle = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                           winreg.KEY_READ | winreg.KEY_WRITE)
        with key_handle as key:
            if action == "enable":
                winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
            elif action == "disable":
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                except FileNotFoundError:
                    pass
            try:
                value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            except FileNotFoundError:
                value = None
        return {"enabled": value is not None, "scope": "current-user-logon",
                "command": value}
    except OSError as exc:
        raise LocalFileError("无法读取或修改当前用户自启动项") from exc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    parser.add_argument("--http-timeout", type=float)
    args = parser.parse_args()
    from .session_daemon import DaemonClient
    client = DaemonClient(Settings.load(args.config, args.http_timeout, load_cwd=False))
    if not client.status()["running"]:
        client._start()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Hidden bootstrap must not emit credentials or platform responses.
        sys.exit(1)
