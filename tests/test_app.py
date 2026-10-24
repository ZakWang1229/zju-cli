import tempfile
import threading
import unittest
import time
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.app import BackgroundApp
from zju_cli.config import Settings
from zju_cli.errors import RemoteError
from zju_cli.storage import JobStore
from zju_cli.media import extract_audio


class AppTests(unittest.TestCase):
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
        class Service:
            def __init__(self, *args, **kwargs):
                pass
            def audio(self, sub_id, course_id, target, **kwargs):
                extract_audio("unused-local-input", target, should_stop=kwargs["should_stop"],
                              on_media_progress=kwargs["on_media_progress"])
            def todos(self):
                return {"count": 0}
        with tempfile.TemporaryDirectory() as temporary, patch("zju_cli.app.runtime_dir", return_value=Path(temporary) / "run"):
            app = BackgroundApp(Settings("one", "secret"), auth=Mock(), service_factory=Service)
            root = Path(temporary) / "jobs"
            try:
                with patch("zju_cli.media.subprocess.Popen", side_effect=spawn):
                    result = app.submit_audio(str(root), {"sub_id": "42", "course_id": "1",
                        "out": str(Path(temporary) / "audio.wav")}, username="one")
                    self.assertTrue(started.wait(3))
                    self.assertIsNone(children[0].poll())
                    self.assertEqual(app.dispatch("todos", {}, username="one"), {"count": 0})
                    app.close()
                self.assertIsNotNone(children[0].poll())
                job = JobStore(root).get(result["job_id"])
                self.assertEqual(job["status"], "paused")
                self.assertFalse(job["worker_active"])
            finally:
                app.close()
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                        child.wait(timeout=5)

    def test_audio_creation_execution_and_persistence_through_app(self):
        class Service:
            def __init__(self, *args, **kwargs):
                pass
            def audio(self, sub_id, course_id, target, **kwargs):
                kwargs["on_media_progress"]({"media_position": "00:00:01"})
                target.write_bytes(b"complete-audio")
                return {"ok": True, "bytes": 14, "sub_id": sub_id, "course_id": course_id}
        with tempfile.TemporaryDirectory() as temporary, patch("zju_cli.app.runtime_dir", return_value=Path(temporary) / "runtime"):
            app = BackgroundApp(Settings("one", "secret"), auth=Mock(), service_factory=Service)
            root = Path(temporary) / "jobs"
            target = Path(temporary) / "output.wav"
            try:
                result = app.submit_audio(str(root), {"sub_id": "42", "course_id": "1", "out": str(target)}, username="one")
                store = JobStore(root)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    job = store.get(result["job_id"])
                    if job["status"] in {"succeeded", "failed"}:
                        break
                    time.sleep(0.01)
                self.assertEqual(job["status"], "succeeded", job)
                self.assertEqual(target.read_bytes(), b"complete-audio")
                self.assertEqual(job["task_type"], "class-audio")
                self.assertEqual(job["task_version"], 1)
                self.assertEqual(job["recovery"]["audio"]["bytes"], 14)
                self.assertNotIn(b"secret", (root / "jobs.sqlite3").read_bytes())
                deadline = time.monotonic() + 2
                while store.get(result["job_id"])["worker_active"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                target.unlink()
                store.update(result["job_id"], {"status": "paused"})
                with patch.object(Service, "audio", side_effect=AssertionError("must reuse complete audio")):
                    app.resume_job(str(root), result["job_id"], username="one")
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        resumed = store.get(result["job_id"])
                        if resumed["status"] in {"succeeded", "failed"}:
                            break
                        time.sleep(0.01)
                    self.assertEqual(resumed["status"], "succeeded", resumed)
                    self.assertEqual(target.read_bytes(), b"complete-audio")
            finally:
                app.close()

    def test_slow_business_does_not_hold_status_lock_and_stop_waits(self):
        entered, release = threading.Event(), threading.Event()
        class Service:
            def __init__(self, *args, **kwargs):
                pass
            def todos(self):
                entered.set()
                release.wait(5)
                return {"count": 0}
        with tempfile.TemporaryDirectory() as temporary, patch("zju_cli.app.runtime_dir", return_value=Path(temporary)):
            app = BackgroundApp(Settings("one", "secret"), auth=Mock(), service_factory=Service)
            with ThreadPoolExecutor() as executor:
                future = executor.submit(app.dispatch, "todos", {}, username="one")
                try:
                    self.assertTrue(entered.wait(2))
                    self.assertTrue(app.status()["accepting"])
                    with self.assertRaises(RemoteError):
                        app.dispatch("todos", {}, username="other")
                    app.begin_stop()
                    self.assertFalse(app.status()["accepting"])
                    with self.assertRaises(RemoteError):
                        app.dispatch("todos", {}, username="one")
                finally:
                    release.set()
                    app.close()
                self.assertEqual(future.result(), {"count": 0})
