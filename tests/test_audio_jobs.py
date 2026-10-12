import argparse
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.cli import main, wait_audio_job
from zju_cli.config import Settings
from zju_cli.errors import BusyError, ConfigError, InputError, LocalFileError
from zju_cli.services.audio_jobs import AudioJobService
from zju_cli.storage import JobStore
from zju_cli.worker import Worker


class AudioJobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = JobStore(self.root / "state")
        self.settings = Settings("test-user", "test-password")
        self.service = AudioJobService(self.store)
        self.target = self.root / "nested" / "lesson.wav"
        self.platform = Mock()
        self.platform.audio.side_effect = self.extract

    def tearDown(self):
        self.temporary.cleanup()

    def extract(self, sub_id, course_id, target, **kwargs):
        kwargs["on_media_progress"]({"elapsed_seconds": 1, "media_position": "00:00:01.000000"})
        target.write_bytes(b"test-audio")
        return {"ok": True, "sub_id": sub_id, "course_id": course_id, "out_path": str(target), "bytes": 10}

    def create(self, **kwargs):
        return self.service.submit("42", "1", self.target, settings=self.settings, launch=False, **kwargs)["job_id"]

    def worker(self, job_id, **kwargs):
        return Worker(self.store, job_id, service=self.platform, **kwargs)

    def test_success_persists_audio_and_exports_target(self):
        job_id = self.create()
        self.worker(job_id).run()
        job = self.store.get(job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertFalse(job["worker_active"])
        self.assertEqual(self.target.read_bytes(), b"test-audio")
        self.assertEqual((self.store.job_dir(job_id) / "audio.wav").read_bytes(), b"test-audio")
        self.assertEqual(self.service.result(job_id)["out_path"], str(self.target))
        self.assertNotIn(b"test-password", (self.store.root / "jobs.sqlite3").read_bytes())

    def test_progress_is_visible_while_extraction_runs(self):
        job_id = self.create()
        observed = []
        def extract(*args, **kwargs):
            metadata = self.extract(*args, **kwargs)
            observed.append(self.store.get(job_id))
            return metadata
        self.platform.audio.side_effect = extract
        self.worker(job_id).run()
        self.assertEqual(observed[0]["status"], "running")
        self.assertEqual(observed[0]["stage"], "extracting")
        self.assertEqual(observed[0]["progress"]["media_position"], "00:00:01.000000")

    def test_output_race_preserves_existing_target_and_can_export_result(self):
        job_id = self.create()
        self.target.parent.mkdir()
        self.target.write_bytes(b"existing")
        self.worker(job_id).run()
        self.assertEqual(self.target.read_bytes(), b"existing")
        self.assertEqual(self.store.get(job_id)["status"], "succeeded")
        self.assertIn("output_error", self.service.result(job_id))
        exported = self.root / "recovered.wav"
        result = self.service.result(job_id, out=exported)
        self.assertEqual(exported.read_bytes(), b"test-audio")
        self.assertEqual(result["out_path"], str(exported))
        self.assertNotIn("output_error", self.service.result(job_id))
        with self.assertRaises(InputError):
            self.service.result(job_id, out=self.root / "renamed.m4a")

    def test_extract_failure_keeps_existing_target_and_redacts_media_url(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b"original")
        job_id = self.create(overwrite=True)
        self.platform.audio.side_effect = LocalFileError("failed: https://example.test/media?token=private")
        self.worker(job_id).run()
        self.assertEqual(self.store.get(job_id)["status"], "failed")
        self.assertEqual(self.target.read_bytes(), b"original")
        self.assertNotIn("token=private", self.store.get(job_id)["error"])

    def test_resume_failed_download_reuses_job_id(self):
        job_id = self.create()
        self.platform.audio.side_effect = LocalFileError("network failure")
        self.worker(job_id).run()
        with patch("zju_cli.services.audio_jobs.Settings.load", return_value=self.settings), \
             patch("zju_cli.services.audio_jobs.subprocess.Popen") as spawn:
            spawn.return_value.pid = 123
            self.service.resume(job_id)
        token = spawn.call_args.args[0][-1]
        self.platform.audio.side_effect = self.extract
        self.worker(job_id, token=token).run()
        self.assertEqual(self.store.get(job_id)["attempts"], 2)
        self.assertEqual(self.store.get(job_id)["status"], "succeeded")
        self.assertEqual(len(self.store.list()), 1)

    def test_completed_internal_audio_is_reused_after_worker_crash(self):
        job_id = self.create()
        source = self.store.job_dir(job_id) / "audio.wav"
        source.write_bytes(b"test-audio")
        self.store.update(job_id, {"status": "running", "stage": "writing", "result": {
            "ok": True, "sub_id": "42", "course_id": "1", "out_path": str(source), "bytes": 10}})
        self.worker(job_id).run()
        self.platform.audio.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"test-audio")

    def test_lease_excludes_duplicate_launch_and_expired_worker_updates(self):
        job_id = self.create()
        self.store.reserve(job_id, "owner")
        with self.assertRaises(BusyError):
            self.store.reserve(job_id, "duplicate")
        self.store.release(job_id, "duplicate")
        self.assertTrue(self.store.get(job_id)["worker_active"])
        with self.store.connection() as connection:
            connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
        with self.assertRaises(BusyError):
            self.store.update(job_id, {"status": "failed"}, token="owner")
        self.store.reserve(job_id, "replacement")
        self.store.release(job_id, "owner")
        self.assertTrue(self.store.get(job_id)["worker_active"])
        self.store.release(job_id, "replacement")

    def test_cancel_active_job_stops_before_extraction(self):
        job_id = self.create()
        self.store.reserve(job_id, "owner")
        self.store.cancel(job_id)
        self.worker(job_id, token="owner").run()
        self.assertEqual(self.store.get(job_id)["status"], "cancelled")
        self.assertFalse(self.target.exists())
        self.platform.audio.assert_not_called()

    def test_cancel_during_extraction_does_not_publish(self):
        job_id = self.create()
        def extract(*args, **kwargs):
            self.store.cancel(job_id)
            kwargs["on_media_progress"]({"elapsed_seconds": 1, "media_position": "00:00:00"})
        self.platform.audio.side_effect = extract
        self.worker(job_id).run()
        self.assertEqual(self.store.get(job_id)["status"], "cancelled")
        self.assertFalse(self.target.exists())

    def test_launch_failure_preserves_recoverable_job_and_releases_lease(self):
        job_id = self.create()
        with patch("zju_cli.services.audio_jobs.subprocess.Popen", side_effect=OSError("spawn failed")):
            with self.assertRaisesRegex(LocalFileError, job_id):
                self.service.launch(job_id)
        self.assertEqual(self.store.get(job_id)["status"], "paused")
        self.assertFalse(self.store.get(job_id)["worker_active"])

    def test_config_failure_pauses_task(self):
        job_id = self.create()
        with patch("zju_cli.worker.Settings.load", side_effect=ConfigError("missing credentials")):
            Worker(self.store, job_id).run()
        self.assertEqual(self.store.get(job_id)["status"], "paused")
        self.assertFalse(self.store.get(job_id)["worker_active"])

    def test_local_status_needs_no_credentials(self):
        job_id = self.create()
        output = io.StringIO()
        with patch("sys.stdout", output), patch("zju_cli.cli.Settings.load") as load:
            self.assertEqual(main(["class-audio", "jobs", "status", job_id, "--data-dir", str(self.store.root), "--json"]), 0)
        load.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["job_id"], job_id)

    def test_wait_timeout_keeps_background_task(self):
        job_id = self.create()
        args = argparse.Namespace(wait_timeout=0.01, quiet=True)
        with patch("zju_cli.cli.time.monotonic", side_effect=[0, 1]):
            result, code = wait_audio_job(self.store, job_id, args)
        self.assertEqual(code, 7)
        self.assertTrue(result["wait_timed_out"])
        self.assertEqual(self.store.get(job_id)["status"], "queued")

    def test_invalid_wait_timeout_does_not_create_job(self):
        with patch("sys.stderr", io.StringIO()), patch("zju_cli.cli.Settings.load") as load:
            self.assertEqual(main(["class-audio", "extract", "--sub-id", "42", "--course-id", "1",
                "--out", str(self.target), "--wait-timeout", "nan", "--data-dir", str(self.store.root)]), 2)
        self.assertEqual(self.store.list(), [])
        load.assert_not_called()

    def test_submit_and_detach_return_job_without_waiting(self):
        for command, options in (("submit", []), ("extract", ["--detach"])):
            with self.subTest(command=command):
                output = io.StringIO()
                with patch("sys.stdout", output), patch("zju_cli.cli.Settings.load", return_value=self.settings), \
                     patch("zju_cli.services.audio_jobs.AudioJobService.launch") as launch, \
                     patch("zju_cli.cli.wait_audio_job") as wait:
                    code = main(["class-audio", command, "--sub-id", "42", "--course-id", "1", "--out",
                                 str(self.target), "--data-dir", str(self.store.root), "--json", *options])
                self.assertEqual(code, 0)
                job_id = json.loads(output.getvalue())["job_id"]
                launch.assert_called_once_with(job_id)
                wait.assert_not_called()
                self.assertEqual(self.store.get(job_id)["status"], "queued")

    def test_extract_waits_by_default_and_emits_final_result(self):
        output = io.StringIO()
        expected = {"ok": True, "out_path": str(self.target), "bytes": 10}
        with patch("sys.stdout", output), patch("sys.stderr", io.StringIO()), \
             patch("zju_cli.cli.Settings.load", return_value=self.settings), \
             patch("zju_cli.services.audio_jobs.AudioJobService.launch"), \
             patch("zju_cli.cli.wait_audio_job", return_value=(expected, 0)) as wait:
            code = main(["class-audio", "extract", "--sub-id", "42", "--course-id", "1", "--out",
                         str(self.target), "--data-dir", str(self.store.root), "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        self.assertEqual(wait.call_count, 1)
        self.assertEqual(wait.call_args.args[1], self.store.list()[0]["job_id"])
        self.assertIsNone(wait.call_args.args[2].wait_timeout)

    def test_wait_ctrl_c_keeps_submitted_task(self):
        with patch("sys.stderr", io.StringIO()), patch("zju_cli.cli.Settings.load", return_value=self.settings), \
             patch("zju_cli.services.audio_jobs.AudioJobService.launch"), \
             patch("zju_cli.cli.wait_audio_job", side_effect=KeyboardInterrupt):
            code = main(["class-audio", "extract", "--sub-id", "42", "--course-id", "1", "--out",
                         str(self.target), "--data-dir", str(self.store.root)])
        self.assertEqual(code, 130)
        self.assertEqual(len(self.store.list()), 1)
        self.assertEqual(self.store.list()[0]["status"], "queued")

    def test_resume_active_job_does_not_read_or_change_config(self):
        job_id = self.create()
        self.store.reserve(job_id, "owner")
        with patch("zju_cli.services.audio_jobs.Settings.load") as load:
            with self.assertRaises(BusyError):
                self.service.resume(job_id, config_path=self.root / "new.env")
        load.assert_not_called()
        self.assertIsNone(self.store.get(job_id)["config_path"])
        self.store.release(job_id, "owner")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_detached_worker_and_real_ffmpeg_survive_wait_timeout(self):
        source = self.root / "source.wav"
        with wave.open(str(source), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\x00\x00" * 8000)
        body = source.read_bytes()
        request_started, release_response = threading.Event(), threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                request_started.set()
                release_response.wait(10)
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
        url = f"http://127.0.0.1:{server.server_port}/source.wav?token=private"
        children = []
        real_popen = subprocess.Popen
        # Patch only URL discovery inside the test subprocess; all job and ffmpeg code is real.
        bootstrap = ("import sys; from unittest.mock import patch; from zju_cli.worker import main; "
                     "context = patch('zju_cli.services.platform.ZjuService._read', return_value=sys.argv[1]); "
                     "context.start(); sys.exit(main(sys.argv[2:]))")

        def spawn(command, **kwargs):
            command = [command[0], "-c", bootstrap, url, *command[3:]]
            child = real_popen(command, **kwargs)
            children.append(child)
            return child

        try:
            with patch.dict(os.environ, {"ZJU_USER": "test-user", "ZJU_PASS": "test-password"}), \
                 patch("zju_cli.services.audio_jobs.subprocess.Popen", side_effect=spawn):
                submitted = self.service.submit("42", "1", self.target, settings=self.settings)
            job_id = submitted["job_id"]
            self.assertTrue(request_started.wait(10), "worker did not start ffmpeg")
            result, code = wait_audio_job(self.store, job_id, argparse.Namespace(wait_timeout=0.05, quiet=True))
            self.assertEqual(code, 7)
            self.assertIsNone(children[0].poll())
            self.assertTrue(result["worker_active"])
            self.assertFalse(self.target.exists())
            release_response.set()
            self.assertEqual(children[0].wait(timeout=15), 0)
            self.assertEqual(self.store.get(job_id)["status"], "succeeded")
            self.assertFalse(self.store.get(job_id)["worker_active"])
            with wave.open(str(self.target), "rb") as audio:
                self.assertEqual(audio.getnframes(), 8000)
            database = (self.store.root / "jobs.sqlite3").read_bytes()
            self.assertNotIn(b"token=private", database)
            self.assertNotIn(b"test-password", database)
        finally:
            release_response.set()
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
