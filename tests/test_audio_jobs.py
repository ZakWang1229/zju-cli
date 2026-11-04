import io
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from zju_cli.frontend.cli import main
from zju_cli.shared.config import Settings


class AudioCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.settings = Settings("test-user", "test-password")
        self.target = self.root / "nested" / "lesson.wav"

    def task(self, task_id="audio-task", state="interrupted", result=None):
        now = time.time()
        return {
            "id": task_id,
            "command": "classroom.audio.download",
            "version": 1,
            "username": self.settings.username,
            "parameters": {
                "sub_id": "42",
                "course_id": "1",
                "out": str(self.target),
                "overwrite": False,
                "ffmpeg_bin": "ffmpeg",
                "timeout": None,
            },
            "state": state,
            "stage": "finished",
            "progress": None,
            "recovery": None,
            "attempt": 1,
            "durable": True,
            "created_at": now,
            "updated_at": now,
            "completed_at": now if state != "interrupted" else None,
            "result": result,
            "error": None,
            "deadline": None,
            "http_timeout": 30,
            "artifact_root": str(self.root / "artifacts"),
        }

    def store_task(self, row):
        with sqlite3.connect(self.state / "tasks.sqlite3") as database:
            database.execute(
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            database.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?)",
                (row["id"], json.dumps(row)),
            )

    def test_list_and_status_read_tasks_without_credentials(self):
        row = self.task()
        self.store_task(row)
        output = io.StringIO()
        with patch("sys.stdout", output), \
             patch("zju_cli.frontend.task_store.default_data_dir", return_value=self.state), \
             patch("zju_cli.frontend.cli.Settings.load") as load:
            self.assertEqual(main([
                "class-audio", "jobs", "status", row["id"],
                "--data-dir", str(self.root), "--json",
            ]), 0)
        load.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["task_id"], row["id"])

    def test_missing_job_is_an_error(self):
        with patch("sys.stderr", io.StringIO()), \
             patch("zju_cli.frontend.task_store.default_data_dir", return_value=self.state):
            self.assertEqual(main([
                "class-audio", "jobs", "status", "missing", "--json",
            ]), 2)

    def test_resume_and_cancel_use_task_commands_without_legacy_path(self):
        row = self.task()
        self.store_task(row)
        response = {
            **row,
            "state": "cancelled",
        }
        for action in ("resume", "cancel"):
            with self.subTest(action=action), \
                 patch("sys.stdout", io.StringIO()), \
                 patch("zju_cli.frontend.task_store.default_data_dir", return_value=self.state), \
                 patch("zju_cli.frontend.cli.Settings.load", return_value=self.settings), \
                 patch("zju_cli.frontend.cli.DaemonClient.call",
                       return_value=response if action == "cancel" else row["id"]) as call:
                self.assertEqual(main([
                    "class-audio", "jobs", action, row["id"], "--json",
                ]), 0)
                request = call.call_args.args[0]
                self.assertEqual(request["command"], "task." + action)
                self.assertEqual(request["parameters"], {"task_id": row["id"]})

    def test_invalid_wait_timeout_does_not_submit(self):
        with patch("sys.stderr", io.StringIO()), \
             patch("zju_cli.frontend.cli.Settings.load") as load:
            self.assertEqual(main([
                "class-audio", "extract", "--sub-id", "42", "--course-id", "1",
                "--out", str(self.target), "--wait-timeout", "nan",
            ]), 2)
        load.assert_not_called()

    def test_submit_and_detach_send_unified_task(self):
        for command, options in (("submit", []), ("extract", ["--detach"])):
            with self.subTest(command=command), patch("sys.stdout", io.StringIO()) as output, \
                 patch("zju_cli.frontend.cli.Settings.load", return_value=self.settings), \
                 patch("zju_cli.frontend.cli.DaemonClient.call",
                       return_value={"task_id": "new-id"}) as call, \
                 patch("zju_cli.frontend.cli.wait_daemon_task") as wait:
                self.assertEqual(main([
                    "class-audio", command, "--sub-id", "42", "--course-id", "1",
                    "--out", str(self.target), "--data-dir", str(self.root), "--json",
                    *options,
                ]), 0)
                self.assertEqual(json.loads(output.getvalue())["job_id"], "new-id")
                request = call.call_args.args[0]
                self.assertEqual(request["action"], "submit")
                self.assertEqual(request["command"], "classroom.audio.download")
                self.assertEqual(request["artifact_root"], str(self.root.resolve()))
                wait.assert_not_called()

    def test_extract_waits_and_ctrl_c_leaves_task_running(self):
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()), \
             patch("zju_cli.frontend.cli.Settings.load", return_value=self.settings), \
             patch("zju_cli.frontend.cli.DaemonClient.call",
                   return_value={"task_id": "new-id"}), \
             patch("zju_cli.frontend.cli.wait_daemon_task",
                   side_effect=KeyboardInterrupt) as wait:
            code = main([
                "class-audio", "extract", "--sub-id", "42", "--course-id", "1",
                "--out", str(self.target),
            ])
        self.assertEqual(code, 130)
        self.assertEqual(wait.call_args.args[1], "new-id")


if __name__ == "__main__":
    unittest.main()
