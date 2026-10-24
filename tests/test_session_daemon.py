import json
import hashlib
import hmac
import os
import subprocess
import sys
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.clients.auth import CasAuth, COURSES, TGMEDIA
from zju_cli.config import Settings
from zju_cli.errors import AuthenticationError, RemoteError
from zju_cli.session_daemon import DaemonClient, endpoint


class SessionPoolTests(unittest.TestCase):
    def test_bad_response_signature_is_rejected(self):
        client = DaemonClient(Settings("test", "fake"))
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"result":{"running":true}}'
        response.getheader.return_value = "wrong-signature"
        with patch("zju_cli.session_daemon.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(RemoteError, "校验失败"):
                client._exchange({"action": "status"})
        connection.close.assert_called_once()

    def setUp(self):
        self.settings = Settings("test-student", "test-only-password")
        self.auth = Mock()
        self.session = Mock()
        self.session.json.return_value = (200, {"ok": True})
        self.auth.courses.return_value = self.session
        self.auth.cmc.return_value = (self.session, "user-1")

    def test_one_cas_session_handles_both_platforms(self):
        session = Mock()
        session.request.side_effect = [
            (200, b'<form><input name="execution" value="one"></form>',
             "https://zjuam.zju.edu.cn/cas/login"),
            (200, b"", COURSES), (200, b"", TGMEDIA)]
        session.json.return_value = (200, {"modulus": "ca1", "exponent": "11"})
        with patch("zju_cli.clients.auth.HttpSession", return_value=session) as factory:
            auth = CasAuth(self.settings)
            self.assertIs(auth.login(COURSES + "/login"), session)
            self.assertIs(auth.login(TGMEDIA + "/"), session)
        factory.assert_called_once()
        self.assertEqual(sum(c.kwargs.get("method") == "POST" for c in session.request.call_args_list), 1)

    def test_expired_cas_can_show_password_form_again(self):
        session = Mock()
        form = (200, b'<form><input name="execution" value="one"></form>',
                "https://zjuam.zju.edu.cn/cas/login")
        session.request.side_effect = [form, (200, b"", COURSES), form, (200, b"", TGMEDIA)]
        session.json.return_value = (200, {"modulus": "ca1", "exponent": "11"})
        with patch("zju_cli.clients.auth.HttpSession", return_value=session):
            auth = CasAuth(self.settings)
            auth.login(COURSES)
            auth.login(TGMEDIA)
        self.assertEqual(sum(c.kwargs.get("method") == "POST" for c in session.request.call_args_list), 2)






    def test_account_password_timeout_and_installation_isolation(self):
        original = endpoint(self.settings)
        self.assertEqual(endpoint(replace(self.settings, timeout_seconds=30.0)), original)
        for settings in (replace(self.settings, username="other"),
                         replace(self.settings, password="other"),
                         replace(self.settings, timeout_seconds=15)):
            self.assertEqual(endpoint(settings), original)
        with patch("zju_cli.session_daemon.__file__", str(Path(__file__).with_name("other.py"))):
            self.assertEqual(endpoint(self.settings), original)

    def test_lost_write_response_is_not_replayed(self):
        client = DaemonClient(self.settings)
        with patch.object(client, "status", return_value={"running": True}), \
                patch.object(client, "_exchange", side_effect=ConnectionResetError) as exchange, \
                patch.object(client, "_start") as start:
            with self.assertRaises(RemoteError):
                client.call({"action": "json", "method": "POST"})
        exchange.assert_called_once()
        start.assert_not_called()


# Runs in a real detached process but never contacts the university.
FAKE_SERVER = '''
import json, sys
from zju_cli import session_daemon as d
class FakeSession:
    def json(self, url, **kwargs):
        return 200, {"pid": __import__("os").getpid(), "todo_list": [], "courses": [], "result": {"list": []}}
class FakeAuth:
    def __init__(self, settings): pass
    def courses(self): return FakeSession()
    def cmc(self): return FakeSession(), "fake-user"
from zju_cli import app
class FakeService:
    def __init__(self, *args, **kwargs): pass
    def todos(self): return {"pid": __import__("os").getpid()}
    def courses(self, **kwargs): return {"count": 0, "courses": []}
original_init = app.BackgroundApp.__init__
def init(self, settings, **kwargs):
    original_init(self, settings, service_factory=FakeService, **kwargs)
    class FakeAttendance:
        def activities(self):
            return [{"rollcall_id": "900", "course_title": "Fake course", "title": "Already complete",
                     "is_number": True, "status": "on_call", "rollcall_status": "in_progress"}]
    self.autosign.adapter_factory = FakeAttendance
app.BackgroundApp.__init__ = init
d.serve(d.Settings(**json.loads(sys.stdin.buffer.readline())))
'''


class DaemonProcessTests(unittest.TestCase):
    def test_autosign_rpc_explicit_enable_restart_and_disable(self):
        def sign(command, parameters=None):
            return self.client.call({"action":"autosign", "command":command,
                "parameters":parameters or {}, "username":self.settings.username})
        self.assertFalse(sign("status")["enabled"])
        self.assertTrue(sign("start", {"config":{"interval":1000,"desktop":False}})["enabled"])
        sign("start")
        self.assertEqual(sign("discover")["activities"][0]["state"],"success")
        self.assertEqual(sign("history")["events"][0]["source"],"platform_existing")
        with self.assertRaises(RemoteError):
            self.client.call({"action":"autosign","command":"start","parameters":{},"username":"other"})
        self.client.stop()
        for process in self.processes:process.wait(timeout=10)
        self.assertTrue(sign("status")["enabled"])
        self.assertFalse(sign("stop")["enabled"])
        self.client.stop()
        for process in self.processes:process.wait(timeout=10)
        self.assertFalse(sign("status")["enabled"])

    def test_url_proxy_and_account_mismatch_are_refused(self):
        self.client.call({"action": "status"})
        with self.assertRaises(RemoteError):
            self.client.call({"action": "json", "platform": "courses", "url": COURSES + "/api/todos"})
        with self.assertRaises(RemoteError):
            self.client.call({"action": "business", "operation": "todos", "parameters": {},
                              "username": "different-account"})
        self.assertTrue(self.client.status()["running"])

    def setUp(self):
        import tempfile
        self.runtime_temporary = tempfile.TemporaryDirectory()
        self.runtime_environment = patch.dict(os.environ, {
            "LOCALAPPDATA": self.runtime_temporary.name,
            "XDG_RUNTIME_DIR": self.runtime_temporary.name})
        self.runtime_environment.start()
        self.addCleanup(self.runtime_temporary.cleanup)
        self.addCleanup(self.runtime_environment.stop)
        self.settings = Settings("test-" + uuid.uuid4().hex, "fake-password", timeout_seconds=2)
        self.client = DaemonClient(self.settings)
        self.processes = []
        popen = subprocess.Popen

        def start_fake(command, **kwargs):
            if command != [sys.executable, "-m", "zju_cli.session_daemon"]:
                return popen(command, **kwargs)
            self.assertEqual(command, [sys.executable, "-m", "zju_cli.session_daemon"])
            self.assertNotIn("ZJU_PASS", kwargs["env"])
            process = popen([sys.executable, "-c", FAKE_SERVER], **kwargs)
            self.processes.append(process)
            return process

        self.patcher = patch("zju_cli.session_daemon.subprocess.Popen", side_effect=start_fake)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        try:
            self.client.stop()
        finally:
            for process in self.processes:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def test_separate_cli_processes_share_daemon_and_stop_restart(self):
        self.assertFalse(self.client.status()["running"])
        first = self.client.call({"action": "business", "operation": "todos", "parameters": {}, "username": self.settings.username})
        # Another Python interpreter makes a complete CLI query using the same daemon.
        env = {**os.environ, "ZJU_USER": self.settings.username, "ZJU_PASS": self.settings.password}
        completed = subprocess.run([sys.executable, "-m", "zju_cli", "courses", "--json",
                                    "--http-timeout", "2"], env=env, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(completed.stdout)["count"], 0)
        second = self.client.call({"action": "business", "operation": "todos", "parameters": {}, "username": self.settings.username})
        self.assertEqual(first["pid"], second["pid"])
        self.assertTrue(self.client.status()["accepting"])
        self.client.stop()
        self.processes[0].wait(timeout=10)
        self.assertFalse(self.client.status()["running"])
        restarted = self.client.call({"action": "business", "operation": "todos", "parameters": {}, "username": self.settings.username})
        self.assertNotEqual(first["pid"], restarted["pid"])

    def test_concurrent_start_converges_on_one_daemon(self):
        def query(_):
            return self.client.call({"action": "business", "operation": "todos", "parameters": {}, "username": self.settings.username})["pid"]
        with ThreadPoolExecutor(max_workers=4) as executor:
            self.assertEqual(len(set(executor.map(query, range(4)))), 1)

    def test_unauthenticated_local_request_is_refused(self):
        self.client.call({"action": "status"})
        connection = HTTPConnection("127.0.0.1", self.client.port, timeout=5)
        try:
            connection.request("POST", "/rpc", b'{"action":"stop"}',
                               {"X-Zju-Nonce": "a" * 64, "X-Zju-Time": str(time.time())})
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()
        self.assertTrue(self.client.status()["running"])

    def test_authenticated_request_cannot_be_replayed(self):
        self.client.call({"action": "status"})
        raw = b'{"action":"status"}'
        nonce, stamp = "b" * 64, str(time.time())
        headers = {"X-Zju-Nonce": nonce, "X-Zju-Time": stamp,
                   "Authorization": hmac.new(self.client.key.encode(),
                       (nonce + stamp).encode() + raw, hashlib.sha256).hexdigest()}
        for expected in (200, 403):
            connection = HTTPConnection("127.0.0.1", self.client.port, timeout=5)
            try:
                connection.request("POST", "/rpc", raw, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
            finally:
                connection.close()

    def test_real_daemon_bootstrap_status_and_shutdown_without_login(self):
        # Exercise the production entry point, with dummy credentials and no login request.
        self.patcher.stop()
        original = subprocess.Popen

        def record(*args, **kwargs):
            process = original(*args, **kwargs)
            self.processes.append(process)
            return process

        with patch("zju_cli.session_daemon.subprocess.Popen", side_effect=record):
            self.assertEqual(self.client.call({"action": "status"})["platforms"], [])
