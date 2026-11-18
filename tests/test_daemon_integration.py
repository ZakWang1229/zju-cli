import tempfile
import threading
import unittest
import time
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.backend.daemon import Daemon
from zju_cli.shared.config import Settings
from zju_cli.shared.errors import RemoteError
from zju_cli.backend.modules.classroom import extract_audio


def run_task(app, command, parameters, username):
    task_id = app.tasks.submit_request(
        command, parameters, username=username)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = app.tasks.get(task_id)
        if task["state"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            break
        time.sleep(0.01)
    if task["state"] != "succeeded":
        raise AssertionError(task)
    return task["result"]


class DaemonIntegrationTests(unittest.TestCase):
    def test_audio_conflict_preserves_target_and_allows_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Daemon(Settings("one", "secret"), state_dir=Path(temporary) / "state")
            target = Path(temporary) / "output.wav"
            def audio(sub_id, course_id, source, **kwargs):
                target.write_bytes(b"existing")
                source.write_bytes(b"complete-audio")
                return {"ok": True, "bytes": 14, "out_path": str(source)}
            app.classroom.service.audio = audio
            try:
                task_id = app.tasks.submit_request("classroom.audio.download", {"sub_id": "42", "course_id": "1",
                    "out": str(target)}, username="one")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    row = app.tasks.get(task_id)
                    if row["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(row["state"], "succeeded", row)
                self.assertIn("output_error", row["result"])
                self.assertEqual(target.read_bytes(), b"existing")
                exported = Path(temporary) / "recovered.wav"
                result = run_task(
                    app,
                    "classroom.audio.export",
                    {"source_task_id": task_id, "out": str(exported)},
                    "one")
                self.assertEqual(exported.read_bytes(), b"complete-audio")
                self.assertEqual(result["out_path"], str(exported))
                self.assertNotIn("output_error", result)
            finally:
                app.close()

    def test_cancelling_audio_during_extraction_does_not_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Daemon(Settings("one", "secret"), state_dir=Path(temporary) / "state")
            entered, release = threading.Event(), threading.Event()
            target = Path(temporary) / "output.wav"
            def audio(sub_id, course_id, source, **kwargs):
                entered.set()
                release.wait(3)
                kwargs["should_stop"]()
                source.write_bytes(b"complete-audio")
                return {"ok": True, "bytes": 14}
            app.classroom.service.audio = audio
            try:
                task_id = app.tasks.submit_request("classroom.audio.download", {"sub_id": "42", "course_id": "1",
                    "out": str(target)}, username="one")
                self.assertTrue(entered.wait(3))
                app.tasks.cancel(task_id)
                release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    row = app.tasks.get(task_id)
                    if row["state"] == "cancelled":
                        break
                    time.sleep(0.01)
                self.assertEqual(row["state"], "cancelled")
                self.assertFalse(target.exists())
            finally:
                release.set()
                app.close()

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_stop_reaps_real_ffmpeg_and_pauses_download(self):
        started = threading.Event()
        children = []
        real_popen = subprocess.Popen
        def spawn(arguments, **kwargs):
            # Replace only input discovery with an infinite local test signal.
            command = [arguments[0], "-nostdin", "-hide_banner", "-loglevel", "error",
                "-re", "-f", "lavfi", "-i", "sine=frequency=440", "-progress", "pipe:1", "-f", "null", "-"]
            child = real_popen(command, **kwargs)
            children.append(child)
            started.set()
            return child
        with tempfile.TemporaryDirectory() as temporary:
            app = Daemon(Settings("one", "secret"), state_dir=Path(temporary) / "state")
            app.courses.service.todos = lambda: {"count": 0}
            app.classroom.service.audio = lambda sub_id, course_id, target, **kwargs: extract_audio(
                "unused-local-input", target, should_stop=kwargs["should_stop"],
                on_media_progress=kwargs["on_media_progress"])
            try:
                with patch("zju_cli.backend.modules.classroom.subprocess.Popen", side_effect=spawn):
                    task_id = app.tasks.submit_request("classroom.audio.download", {"sub_id": "42", "course_id": "1",
                        "out": str(Path(temporary) / "audio.wav"), "overwrite": False,
                        "ffmpeg_bin": "ffmpeg", "timeout": None}, username="one")
                    self.assertTrue(started.wait(3))
                    self.assertIsNone(children[0].poll())
                    self.assertEqual(run_task(app, "courses.todos", {}, "one"),
                                     {"count": 0})
                    app.close()
                self.assertIsNotNone(children[0].poll())
                task = app.tasks.get(task_id)
                self.assertEqual(task["state"], "interrupted")
            finally:
                app.close()
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)

    def test_audio_creation_execution_and_persistence_through_app(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Daemon(Settings("one", "secret"), state_dir=Path(temporary) / "state")
            def audio(sub_id, course_id, target, **kwargs):
                kwargs["on_media_progress"]({"media_position": "00:00:01"})
                target.write_bytes(b"complete-audio")
                return {"ok": True, "bytes": 14, "sub_id": sub_id, "course_id": course_id}
            app.classroom.service.audio = audio
            target = Path(temporary) / "output.wav"
            try:
                task_id = app.tasks.submit_request("classroom.audio.download", {"sub_id": "42", "course_id": "1",
                    "out": str(target), "overwrite": False, "ffmpeg_bin": "ffmpeg", "timeout": None}, username="one")
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = app.tasks.get(task_id)
                    if job["state"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["state"], "succeeded", job)
                self.assertEqual(target.read_bytes(), b"complete-audio")
                self.assertEqual(job["command"], "classroom.audio.download")
                self.assertEqual(job["version"], 1)
                self.assertEqual(job["recovery"]["audio"]["bytes"], 14)
                self.assertNotIn(b"secret", (Path(temporary) / "state" / "tasks.sqlite3").read_bytes())
                target.unlink()
                app.tasks._update(task_id, state="interrupted")
                with patch.object(app.classroom.service, "audio", side_effect=AssertionError("must reuse complete audio")):
                    app.tasks.resume(task_id)
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        resumed = app.tasks.get(task_id)
                        if resumed["state"] in {"succeeded", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(resumed["state"], "succeeded", resumed)
                    self.assertEqual(target.read_bytes(), b"complete-audio")
            finally:
                app.close()

    def test_slow_business_does_not_hold_status_lock_and_stop_waits(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as temporary:
            app = Daemon(Settings("one", "secret"), state_dir=Path(temporary) / "state")
            def todos():
                entered.set()
                release.wait(5)
                return {"count": 0}
            app.courses.service.todos = todos
            with ThreadPoolExecutor() as executor:
                future = executor.submit(
                    run_task, app, "courses.todos", {}, "one")
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(app.status()["accepting"])
                    with self.assertRaises(RemoteError):
                        app.tasks.submit_request(
                            "courses.todos", {}, username="other")
                    app.begin_stop()
                    self.assertFalse(app.status()["accepting"])
                    with self.assertRaises(RemoteError):
                        app.tasks.submit_request(
                            "courses.todos", {}, username="one")
                finally:
                    release.set()
                    app.close()
                self.assertEqual(future.result(), {"count": 0})
