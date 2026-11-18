"""CLI client for the authenticated local daemon."""
import hmac
import json
import math
import os
import secrets
import subprocess
import sys
import time
from dataclasses import asdict
from http.client import HTTPConnection, HTTPException
from zju_cli.shared.errors import AuthenticationError, RemoteError, SessionExpired, ZjuError, InputError, ConfigError, LocalFileError, BusyError, CancelledError
from zju_cli.shared.rpc import endpoint

class DaemonClient:
    def __init__(self, settings):
        self.settings = settings
        self.port, self.key = endpoint(settings)

    def _exchange(self, message, *, timeout=None):
        connection = HTTPConnection("127.0.0.1", self.port,
                                    timeout=timeout if timeout is not None else
                                    max(10, self.settings.timeout_seconds * 10))
        try:
            raw = json.dumps(message).encode()
            nonce, stamp = secrets.token_hex(32), str(time.time())
            signature = hmac.new(self.key.encode(), (nonce + stamp).encode() + raw, "sha256").hexdigest()
            connection.request("POST", "/rpc", raw,
                               {"Authorization": signature, "X-Zju-Nonce": nonce, "X-Zju-Time": stamp,
                                "Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RemoteError("后台接口拒绝请求；本地端口可能被占用")
            raw = response.read()
            expected = hmac.new(self.key.encode(), nonce.encode() + raw, "sha256").hexdigest()
            if not hmac.compare_digest(response.getheader("X-Zju-Signature", ""), expected):
                raise RemoteError("认证后台身份校验失败；本地端口可能被占用")
            value = json.loads(raw)
        finally:
            connection.close()
        if "error" in value:
            error = {"AuthenticationError": AuthenticationError, "SessionExpired": SessionExpired,
                     "RemoteError": RemoteError, "InputError": InputError, "ConfigError": ConfigError,
                     "LocalFileError": LocalFileError, "BusyError": BusyError,
                     "CancelledError": CancelledError}.get(value["error"], RemoteError)
            raise error(value.get("message") or "后台请求失败")
        return value["result"]

    def status(self):
        try:
            return self._exchange({"action": "status"}, timeout=5)
        except ConnectionRefusedError:
            return {"running": False}
        except (OSError, HTTPException, ValueError) as exc:
            raise RemoteError("无法查询认证后台状态") from exc

    def stop(self):
        if not self.status()["running"]:
            return {"running": False}
        return self.call({"action": "stop"}, start=False)

    def start(self):
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self._lifecycle_status()
            if status is not None:
                if not status["running"]:
                    self._start()
                    return self.status()
                if status.get("accepting", True):
                    return status
            time.sleep(0.05)
        raise RemoteError("认证后台启动超时")

    def _lifecycle_status(self):
        try:
            return self.status()
        except RemoteError as exc:
            # A closing listener may reset a connection accepted during shutdown.
            # Authentication/protocol errors must still reach the caller.
            if isinstance(exc.__cause__, (OSError, HTTPException)):
                return None
            raise

    def _wait_stopped(self):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            status = self._lifecycle_status()
            if status is not None and not status["running"]:
                return
            time.sleep(0.05)
        raise RemoteError("后台停止超时")

    def restart(self):
        status = self._lifecycle_status()
        if status is not None and status["running"] and status.get("accepting", True):
            self.call({"action": "stop"}, start=False)
        self._wait_stopped()
        return self.start()

    def execute(self, command, parameters, *, wait=None, wait_timeout=None):
        if wait_timeout is not None and (type(wait_timeout) not in (int, float) or not math.isfinite(wait_timeout) or wait_timeout <= 0):
            raise InputError("等待超时必须是有限正数")
        if wait is None:
            registry = self.call({"action": "command.list"})
            wait = registry["metadata"].get(command, {}).get("mode", "request") != "job"
        submitted = self.call({"action": "submit", "command": command, "parameters": parameters,
                               "username": self.settings.username, "timeout": self.settings.timeout_seconds})
        task_id = submitted["task_id"]
        if wait is False:
            return submitted
        started = time.monotonic()
        while True:
            row = self.call({"action": "command", "command": "task.get",
                             "parameters": {"task_id": task_id}, "username": self.settings.username}, start=False)
            if row["state"] == "succeeded":
                return row["result"]
            if row["state"] in {"failed", "cancelled", "interrupted"}:
                error = row.get("error") or {}
                from zju_cli.shared import errors
                cls = getattr(errors, error.get("type", "RemoteError"), RemoteError)
                if not isinstance(cls, type) or not issubclass(cls, ZjuError):
                    cls = RemoteError
                raise cls(error.get("message") or "任务取消或中断")
            if wait_timeout is not None and time.monotonic() - started >= wait_timeout:
                return {"task_id": task_id, "wait_timed_out": True}
            time.sleep(0.1)

    def _start(self):
        environment = os.environ.copy()
        for name in ("ZJU_USER", "ZJU_PASS", "CC98_USERNAME", "CC98_PASSWORD"):
            environment.pop(name, None)
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
                  "stderr": subprocess.DEVNULL, "close_fds": True, "env": environment}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS |
                                       subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen([sys.executable, "-m", "zju_cli.backend.server"], **kwargs)
            try:
                payload = asdict(self.settings)
                if payload.get("log_dir") is not None:
                    payload["log_dir"] = str(payload["log_dir"])
                process.stdin.write(json.dumps(payload).encode() + b"\n")
                process.stdin.flush()
            finally:
                process.stdin.close()
        except OSError as exc:
            raise RemoteError("无法启动认证后台") from exc
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            status = self._lifecycle_status()
            if status is not None and status["running"] and status.get("accepting", True):
                return
            time.sleep(0.05)
        raise RemoteError("认证后台启动超时")

    def call(self, message, *, start=True):
        # Only the harmless startup ping is retried. Never retry a lost response
        # to a write, even if the daemon has exited in the meantime.
        try:
            if start and not self.status()["running"]:
                self._start()
            return self._exchange(message)
        except (OSError, HTTPException, ValueError) as exc:
            raise RemoteError("认证后台通信失败；写入结果可能已生效，请查询后再操作") from exc
