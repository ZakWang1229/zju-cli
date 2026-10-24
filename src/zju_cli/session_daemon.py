"""Authenticated loopback RPC. Credentials enter the child only through stdin.

The daemon owns all cookies; clients receive JSON API results, never a cookie jar.
There is deliberately no periodic platform expiry probe or TTL eviction.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .clients.auth import CasAuth, COURSES, TGMEDIA
from .config import Settings
from .errors import (AuthenticationError, RemoteError, SessionExpired, ZjuError,
                     InputError, ConfigError, LocalFileError, BusyError, CancelledError)

PROTOCOL = 1
MAX_REQUEST = 4 * 1024 * 1024


def endpoint(settings):
    from .runtime import local_endpoint
    return local_endpoint()


class RpcServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False
    block_on_close = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def serve(settings):
    from .app import BackgroundApp
    port, key = endpoint(settings)
    seen = {}
    authentication_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            nonce = self.headers.get("X-Zju-Nonce", "")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST or self.path != "/rpc":
                    self.send_error(400)
                    return
                body = self.rfile.read(length)
                stamp = self.headers.get("X-Zju-Time", "")
                signature = hmac.new(key.encode(), (nonce + stamp).encode() + body, "sha256").hexdigest()
                with authentication_lock:
                    now = time.time()
                    for old in [n for n, expiry in seen.items() if expiry < now]:
                        del seen[old]
                    if (len(nonce) != 64 or abs(now - float(stamp)) > 60 or nonce in seen or
                            not hmac.compare_digest(self.headers.get("Authorization", ""), signature)):
                        self.send_error(403)
                        return
                    seen[nonce] = now + 120
                message = json.loads(body)
                if not isinstance(message, dict):
                    raise ValueError
                if message.get("action") == "stop":
                    app.begin_stop()
                    result = {"running": False}
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif message.get("action") == "business":
                    result = app.dispatch(message.get("operation"), message.get("parameters"),
                                          username=message.get("username"), timeout=message.get("timeout"))
                elif message.get("action") == "job_resume":
                    result = app.resume_job(message["data_dir"], message["job_id"],
                                            username=message.get("username"))
                elif message.get("action") == "audio_submit":
                    result = app.submit_audio(message["data_dir"], message["parameters"],
                        username=message.get("username"), timeout=message.get("timeout"))
                elif message.get("action") == "job_cancel":
                    result = app.cancel_job(message["data_dir"], message["job_id"])
                elif message.get("action") == "autosign":
                    parameters = message.get("parameters", {})
                    if not isinstance(parameters, dict):
                        raise InputError("签到参数格式无效")
                    result = app.autosign_operation(message.get("command"), parameters,
                                                    username=message.get("username"))
                else:
                    if message.get("action") == "status":
                        result = {**app.status(), "protocol": PROTOCOL, "platforms": []}
                    else:
                        raise RemoteError("未知后台操作")
                response = {"result": result}
            except ZjuError as exc:
                response = {"error": type(exc).__name__, "message": str(exc)}
            except Exception:
                # Never return exception repr, requests, cookies or response bodies.
                response = {"error": "RemoteError", "message": "后台请求处理失败"}
            raw = json.dumps(response, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("X-Zju-Signature", hmac.new(key.encode(), nonce.encode() + raw, "sha256").hexdigest())
            self.end_headers()
            self.wfile.write(raw)

    try:
        server = RpcServer(("127.0.0.1", port), Handler)
    except OSError:
        # Concurrent starters converge on the process that acquired the port.
        return
    try:
        app = BackgroundApp(settings)
    except BaseException:
        server.server_close()
        raise
    from .runtime import runtime_dir
    address_file = runtime_dir() / "address.json"
    try:
        with server:
            address_file.write_text(json.dumps({"host": "127.0.0.1", "port": port, "protocol": PROTOCOL}), encoding="utf-8")
            server.serve_forever(poll_interval=0.2)
    finally:
        app.close()
        address_file.unlink(missing_ok=True)


class DaemonClient:
    def __init__(self, settings):
        self.settings = settings
        self.port, self.key = endpoint(settings)

    def _exchange(self, message):
        connection = HTTPConnection("127.0.0.1", self.port,
                                    timeout=max(10, self.settings.timeout_seconds * 10))
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
            return self._exchange({"action": "status"})
        except ConnectionRefusedError:
            return {"running": False, "protocol": PROTOCOL, "platforms": []}
        except (OSError, HTTPException, ValueError) as exc:
            raise RemoteError("无法查询认证后台状态") from exc

    def stop(self):
        if not self.status()["running"]:
            return {"running": False}
        return self.call({"action": "stop"}, start=False)

    def _start(self):
        environment = os.environ.copy()
        for name in ("ZJU_USER", "ZJU_PASS"):
            environment.pop(name, None)
        kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL,
                  "stderr": subprocess.DEVNULL, "close_fds": True, "env": environment}
        if os.name == "nt":
            kwargs["creationflags"] = (subprocess.DETACHED_PROCESS |
                                       subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen([sys.executable, "-m", "zju_cli.session_daemon"], **kwargs)
            try:
                process.stdin.write(json.dumps(asdict(self.settings)).encode() + b"\n")
                process.stdin.flush()
            finally:
                process.stdin.close()
        except OSError as exc:
            raise RemoteError("无法启动认证后台") from exc
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.status()["running"]:
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


if __name__ == "__main__":
    try:
        serve(Settings(**json.loads(sys.stdin.buffer.readline(MAX_REQUEST))))
    except Exception:
        # Detached process: never emit bootstrap credentials or a traceback.
        sys.exit(1)
