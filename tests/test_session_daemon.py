import json
import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.backend.modules.zjuam import CasAuth, COURSES, TGMEDIA
from zju_cli.shared.config import Settings
from zju_cli.shared.errors import AuthenticationError, RemoteError
from zju_cli.frontend.rpc import DaemonClient, endpoint


class SessionPoolTests(unittest.TestCase):
    def test_restart_retries_connection_reset_during_shutdown(self):
        client = DaemonClient(Settings("test", "fake"))
        failure = RemoteError("status failed")
        failure.__cause__ = ConnectionResetError()
        ready = {"running": True, "accepting": True}
        with patch.object(client, "status", side_effect=[ready, failure,
                {"running": False}, {"running": False}, ready]), \
                patch.object(client, "call") as call, \
                patch.object(client, "_start") as start, \
                patch("zju_cli.frontend.rpc.time.sleep"):
            self.assertEqual(client.restart(), ready)
        call.assert_called_once_with({"action": "stop"}, start=False)
        start.assert_called_once_with()

    def test_start_waits_for_existing_initialization(self):
        client = DaemonClient(Settings("test", "fake"))
        failure = RemoteError("status failed")
        failure.__cause__ = TimeoutError()
        ready = {"running": True, "accepting": True}
        with patch.object(client, "status", side_effect=[failure, ready]), \
                patch.object(client, "_start") as start, \
                patch("zju_cli.frontend.rpc.time.sleep"):
            self.assertEqual(client.start(), ready)
        start.assert_not_called()

    def test_start_does_not_hide_identity_failure(self):
        client = DaemonClient(Settings("test", "fake"))
        with patch.object(client, "status", side_effect=RemoteError("identity failed")), \
                patch.object(client, "_start") as start:
            with self.assertRaisesRegex(RemoteError, "identity failed"):
                client.start()
        start.assert_not_called()

    def test_bad_response_signature_is_rejected(self):
        client = DaemonClient(Settings("test", "fake"))
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"result":{"running":true}}'
        response.getheader.return_value = "wrong-signature"
        with patch("zju_cli.frontend.rpc.HTTPConnection", return_value=connection):
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
        with patch("zju_cli.backend.modules.zjuam.HttpSession", return_value=session) as factory:
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
        with patch("zju_cli.backend.modules.zjuam.HttpSession", return_value=session):
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
        with patch("zju_cli.frontend.rpc.__file__", str(Path(__file__).with_name("other.py"))):
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
from zju_cli.backend import server as d
class FakeSession:
    timeout = 2
    def close(self): pass
    def json(self, url, **kwargs):
        return 200, {"pid": __import__("os").getpid(), "todo_list": [], "courses": [], "result": {"list": []}}
class FakeAuth:
    def __init__(self, settings): pass
    def courses(self, *args): return FakeSession()
    def cmc(self, *args): return FakeSession(), "fake-user"
    def status(self): return {"authenticated": False}
    def close(self): pass
from zju_cli.backend import daemon
class FakeAttendance:
    def activities(self):
        return [{"rollcall_id": "900", "course_title": "Fake course", "title": "Already complete",
                 "is_number": True, "status": "on_call", "rollcall_status": "in_progress"}]
original_init = daemon.Daemon.__init__
def init(self, settings, **kwargs):
    original_init(self, settings, auth=FakeAuth(settings), **kwargs)
    self.courses.service.todos = lambda: {"pid": __import__("os").getpid()}
    self.courses.service.courses = lambda keyword="", statuses=None: {"count": 0, "courses": []}
    self.autosign.service.adapter_factory = FakeAttendance
    def audio(sub_id, course_id, target, **values):
        target.write_bytes(b"complete-audio")
        values["on_media_progress"]({"media_position": "00:00:01"})
        return {"ok": True, "bytes": 14, "out_path": str(target)}
    media_url = __import__('os').environ.get('ZJU_TEST_PLAYBACK_URL')
    if media_url:
        self.classroom.service.playback_url = lambda *args: media_url
    else:
        self.classroom.service.audio = audio
daemon.Daemon.__init__ = init
d.serve(d.Settings(**json.loads(sys.stdin.buffer.readline())))
'''


class DaemonProcessTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_daemon_and_real_ffmpeg_continue_after_frontend_wait_timeout(self):
        root = Path(self.runtime_temporary.name)
        source, target = root / "source.wav", root / "download.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 8000)
        body = source.read_bytes()
        requested, release = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requested.set()
                release.wait(10)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/source.wav?token=private"
            with patch.dict(os.environ, {"ZJU_TEST_PLAYBACK_URL": url}):
                result = self.client.execute("classroom.audio.download",
                    {"sub_id": "42", "course_id": "1", "out": str(target)},
                    wait=True, wait_timeout=0.01)
            self.assertTrue(result["wait_timed_out"])
            self.assertTrue(requested.wait(5), "daemon did not start ffmpeg")
            self.assertFalse(target.exists())
            release.set()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                task = self.client.call({"action": "command", "command": "task.get",
                    "parameters": {"task_id": result["task_id"]}, "username": self.settings.username})
                if task["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(task["state"], "succeeded", task)
            with wave.open(str(target), "rb") as audio:
                self.assertEqual(audio.getnframes(), 8000)
            from zju_cli.shared.storage import default_data_dir
            self.assertNotIn(b"token=private", (default_data_dir() / "tasks.sqlite3").read_bytes())
            self.assertFalse((default_data_dir() / "jobs.sqlite3").exists())
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_audio_rpc_uses_new_tasks_and_exports_complete_artifact(self):
        target = Path(self.runtime_temporary.name) / "export.wav"
        result = self.client.call({"action": "submit", "command": "classroom.audio.download",
            "username": self.settings.username, "artifact_root": str(target.parent / "custom-artifacts"),
            "parameters": {"sub_id": "42", "course_id": "1", "out": str(target),
                "overwrite": False, "ffmpeg_bin": "ffmpeg", "timeout": None}})
        task_id = result["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            row = self.client.call({"action": "command", "command": "task.get",
                "parameters": {"task_id": task_id}, "username": self.settings.username})
            if row["state"] in {"succeeded", "failed"}:
                break
            time.sleep(0.05)
        self.assertEqual(row["state"], "succeeded", row)
        self.assertEqual(target.read_bytes(), b"complete-audio")
        self.assertEqual((target.parent / "custom-artifacts" / "tasks" / task_id / "audio.wav").read_bytes(), b"complete-audio")
        self.assertEqual(row["command"], "classroom.audio.download")

    def test_new_rpc_submits_and_polls_without_exposing_internal_commands(self):
        result = self.client.execute("courses.todos", {})
        self.assertIn("pid", result)
        registry = self.client.call({"action": "command.list"})
        self.assertIn("courses.materials.sync", registry["commands"])
        self.assertIn("cc98.download", registry["commands"])
        self.assertIn("webvpn.status", registry["commands"])
        self.assertNotIn("autosign.check", registry["commands"])
        with self.assertRaises(Exception):
            self.client.call({"action": "submit", "command": "autosign.check", "parameters": {},
                "username": self.settings.username, "internal": True})
        self.assertTrue(self.client.status()["running"])

    def test_scheduled_round_persists_and_plan_survives_process_restart(self):
        def control(name, **parameters):
            return self.client.call({"action": "command", "command": name,
                "parameters": parameters, "username": self.settings.username})
        schedule_id = control("schedule.create", command="courses.todos", parameters={}, interval=0.1, enabled=True)
        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            rows = control("task.list", command="courses.todos", state="succeeded")
            if rows:
                break
            time.sleep(0.05)
        self.assertTrue(rows)
        task_id = rows[0]["id"]
        control("schedule.disable", schedule_id=schedule_id)
        self.client.stop()
        for process in self.processes:
            process.wait(timeout=10)
        self.assertEqual(control("task.get", task_id=task_id)["state"], "succeeded")
        self.assertFalse(control("schedule.get", schedule_id=schedule_id)["enabled"])

    @unittest.skipIf(os.name == "nt", "POSIX SIGTERM behavior")
    def test_foreground_sigterm_closes_rpc_and_process(self):
        program = "from zju_cli.backend.server import serve; from zju_cli.shared.config import Settings; serve(Settings('foreground','unused'), handle_signals=True)"
        process = subprocess.Popen([sys.executable, "-c", program], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.client.status()["running"]:
            time.sleep(0.05)
        self.assertTrue(self.client.status()["running"])
        process.terminate()
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertFalse(self.client.status()["running"])

    def test_autosign_rpc_explicit_enable_restart_and_disable(self):
        def sign(command, parameters=None):
            parameters = parameters or {}
            if command in {"status", "list", "history", "stop"}:
                return self.client.call({
                    "action": "command",
                    "command": "autosign." + command,
                    "parameters": parameters,
                    "username": self.settings.username,
                })
            return self.client.execute("autosign." + command, parameters)
        self.assertFalse(sign("status")["enabled"])
        self.assertTrue(sign("start", {"config":{"interval":1000,"desktop":False}})["enabled"])
        sign("start")
        self.assertEqual(sign("discover")["activities"][0]["state"],"success")
        self.assertEqual(sign("history")["events"][0]["source"],"platform_existing")
        with self.assertRaises(RemoteError):
            self.client.call({
                "action": "submit",
                "command": "autosign.start",
                "parameters": {},
                "username": "other",
            })
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
            self.client.call({"action": "submit", "command": "courses.todos",
                              "parameters": {}, "username": "different-account"})
        self.assertTrue(self.client.status()["running"])

    def setUp(self):
        import tempfile
        self.runtime_temporary = tempfile.TemporaryDirectory()
        self.runtime_environment = patch.dict(os.environ, {
            "LOCALAPPDATA": self.runtime_temporary.name,
            "XDG_RUNTIME_DIR": self.runtime_temporary.name,
            "XDG_STATE_HOME": self.runtime_temporary.name,
            "XDG_CONFIG_HOME": self.runtime_temporary.name})
        self.runtime_environment.start()
        self.addCleanup(self.runtime_temporary.cleanup)
        self.addCleanup(self.runtime_environment.stop)
        self.settings = Settings("test-" + uuid.uuid4().hex, "fake-password", timeout_seconds=2)
        self.client = DaemonClient(self.settings)
        self.processes = []
        popen = subprocess.Popen

        def start_fake(command, **kwargs):
            if command != [sys.executable, "-m", "zju_cli.backend.server"]:
                return popen(command, **kwargs)
            self.assertEqual(command, [sys.executable, "-m", "zju_cli.backend.server"])
            self.assertNotIn("ZJU_PASS", kwargs["env"])
            process = popen([sys.executable, "-c", FAKE_SERVER], **kwargs)
            self.processes.append(process)
            return process

        self.patcher = patch("zju_cli.frontend.rpc.subprocess.Popen", side_effect=start_fake)
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
        first = self.client.execute("courses.todos", {})
        # Another Python interpreter makes a complete CLI query using the same daemon.
        env = {**os.environ, "ZJU_USER": self.settings.username, "ZJU_PASS": self.settings.password}
        completed = subprocess.run([sys.executable, "-m", "zju_cli", "courses", "--json",
                                    "--http-timeout", "2"], env=env, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr.decode(errors="replace"))
        self.assertEqual(json.loads(completed.stdout)["count"], 0)
        second = self.client.execute("courses.todos", {})
        self.assertEqual(first["pid"], second["pid"])
        self.assertTrue(self.client.status()["accepting"])
        self.client.stop()
        self.processes[0].wait(timeout=10)
        self.assertFalse(self.client.status()["running"])
        restarted = self.client.execute("courses.todos", {})
        self.assertNotEqual(first["pid"], restarted["pid"])

    def test_concurrent_start_converges_on_one_daemon(self):
        def query(_):
            return self.client.execute("courses.todos", {})["pid"]
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

        with patch("zju_cli.frontend.rpc.subprocess.Popen", side_effect=record):
            status = self.client.call({"action": "status"})
            self.assertTrue(status["running"])
            self.assertNotIn("platforms", status)
