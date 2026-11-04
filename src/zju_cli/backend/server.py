"""Authenticated local RPC server and daemon process entry point."""
import hmac
import json
import math
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zju_cli.shared.config import Settings
from zju_cli.shared.errors import RemoteError, ZjuError
from zju_cli.shared.rpc import PROTOCOL, MAX_REQUEST, endpoint

class RpcServer(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False
    block_on_close = True

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def serve(settings, *, handle_signals=False):
    from zju_cli.backend.daemon import Daemon
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
                    if (len(nonce) != 64 or not math.isfinite(float(stamp)) or abs(now - float(stamp)) > 60 or nonce in seen or
                            not hmac.compare_digest(self.headers.get("Authorization", ""), signature)):
                        self.send_error(403)
                        return
                    seen[nonce] = now + 120
                message = json.loads(body)
                if not isinstance(message, dict):
                    raise ValueError
                result, shutdown = app.handle_rpc(message)
                if shutdown:
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
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
        if handle_signals:
            raise RemoteError("后台端口已占用，无法以前台模式启动")
        return
    from zju_cli.backend.logger import configure_logging
    logger = None
    try:
        logger = configure_logging(level=settings.log_level, output=settings.log_output,
                                   log_dir=settings.log_dir, timezone=settings.log_timezone)
        logger.info("daemon starting")
        app = Daemon(settings)
    except BaseException:
        server.server_close()
        if logger is not None:
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
        raise
    from zju_cli.shared.runtime import runtime_dir
    address_file = runtime_dir() / "address.json"
    previous_signals = {}
    if handle_signals:
        import signal
        def request_stop(signum, frame):
            app.begin_stop()
            threading.Thread(target=server.shutdown, daemon=True).start()
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, request_stop)
    try:
        with server:
            address_file.write_text(json.dumps({"host": "127.0.0.1", "port": port, "protocol": PROTOCOL}), encoding="utf-8")
            server.serve_forever(poll_interval=0.2)
    finally:
        if previous_signals:
            import signal
            for signum, previous in previous_signals.items():
                signal.signal(signum, previous)
        app.close()
        address_file.unlink(missing_ok=True)
        if logger is not None:
            for handler in logger.handlers[:]:
                handler.close()
                logger.removeHandler(handler)
    return {"running": False}


if __name__ == "__main__":
    try:
        serve(Settings(**json.loads(sys.stdin.buffer.readline(MAX_REQUEST))))
    except Exception:
        # Detached process: never emit bootstrap credentials or a traceback.
        sys.exit(1)
