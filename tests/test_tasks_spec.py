from pathlib import Path
from tempfile import TemporaryDirectory
import json
import threading
import time
import unittest
import os
import subprocess
import sys
import sqlite3
from contextlib import closing

from zju_cli.backend.command_registry import CommandRegistry
from zju_cli.shared.config import Settings
from zju_cli.shared.errors import BusyError, InputError, RemoteError
from zju_cli.backend.modules.tasks import TasksModule, TERMINAL, current_task
from zju_cli.backend.modules.schedules import SchedulesModule


class Runtime(CommandRegistry):
    def __init__(self, root):
        super().__init__()
        self.command_registry = self
        self.state_dir = Path(root)
        self.config = Settings("unit-test", "private-password")
        self.tasks = TasksModule(self, self.config)
        self.schedules = SchedulesModule(self, self.config)

    def close(self):
        self.schedules.close()
        self.tasks.close()


def wait_task(runtime, task_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = runtime.tasks.get(task_id)
        if row["state"] in TERMINAL:
            return row
        time.sleep(0.01)
    raise AssertionError(runtime.tasks.get(task_id))


class TasksSpecTests(unittest.TestCase):
    def test_restore_only_writes_interrupted_tasks_for_current_account(self):
        with TemporaryDirectory() as root:
            database = Path(root) / "tasks.sqlite3"
            documents = {}
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
                db.execute("CREATE TABLE writes (id TEXT)")
                db.execute("CREATE TRIGGER track_update AFTER UPDATE ON tasks "
                           "BEGIN INSERT INTO writes VALUES (new.id); END")
                db.execute("CREATE TRIGGER track_insert AFTER INSERT ON tasks "
                           "BEGIN INSERT INTO writes VALUES (new.id); END")
                for key, state, user in (
                    ("queued", "queued", "unit-test"),
                    ("running", "running", "unit-test"),
                    ("done", "succeeded", "unit-test"),
                    ("failed", "failed", "unit-test"),
                    ("other", "running", "other-account"),
                ):
                    documents[key] = json.dumps({"id": key, "username": user,
                        "state": state, "durable": True, "completed_at": None})
                    db.execute("INSERT INTO tasks VALUES (?,?)", (key, documents[key]))
                db.execute("DELETE FROM writes")
            runtime = Runtime(root)
            try:
                self.assertEqual(runtime.tasks._rows, {})
                with closing(sqlite3.connect(database)) as db:
                    rows = dict(db.execute("SELECT id,document FROM tasks"))
                    self.assertEqual(set(db.execute("SELECT id FROM writes")),
                                     {("queued",), ("running",)})
                for key in ("done", "failed", "other"):
                    self.assertEqual(rows[key], documents[key])
                for key in ("queued", "running"):
                    restored = json.loads(rows[key])
                    self.assertEqual(restored["state"], "interrupted")
                    self.assertIsNotNone(restored["completed_at"])
                    self.assertEqual(runtime.tasks.get(key)["state"], "interrupted")
            finally:
                runtime.close()

    def test_history_is_queried_on_demand_and_merged_with_memory_tasks(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            runtime.register("saved", lambda: {}, durable=True)
            old_id = runtime.tasks.submit("saved", {})
            wait_task(runtime, old_id)
            runtime.close()
            runtime = Runtime(root)
            try:
                self.assertEqual(runtime.tasks._rows, {})
                runtime.register("live", lambda: {})
                live_id = runtime.tasks.submit("live", {})
                wait_task(runtime, live_id)
                self.assertEqual([row["id"] for row in runtime.tasks.list(limit=1)], [live_id])
                self.assertEqual([row["id"] for row in runtime.tasks.list(command="saved")], [old_id])
                self.assertNotIn(old_id, runtime.tasks._rows)
                self.assertEqual(runtime.tasks.get(old_id)["state"], "succeeded")
                self.assertNotIn(old_id, runtime.tasks._rows)
                with closing(sqlite3.connect(runtime.tasks.database)) as db:
                    plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM tasks WHERE "
                        "json_extract(document,'$.username')=? AND "
                        "json_extract(document,'$.state') IN ('queued','running')", ("unit-test",)).fetchall()
                    self.assertTrue(any("SEARCH" in row[3] and "INDEX" in row[3] for row in plan), plan)
            finally:
                runtime.close()

    def test_completed_persistent_tasks_release_futures_and_cached_rows(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            runtime.register("saved", lambda: {"ok": True}, durable=True, serial_group="saved")
            try:
                ids = [runtime.tasks.submit("saved", {}) for _ in range(10)]
                for task_id in ids:
                    self.assertEqual(wait_task(runtime, task_id)["result"], {"ok": True})
                runtime.tasks.executor.shutdown(wait=True)
                self.assertEqual(runtime.tasks._futures, {})
                self.assertEqual(runtime.tasks._rows, {})
                self.assertEqual({row["id"] for row in runtime.tasks.list()}, set(ids))
            finally:
                runtime.close()

    def test_internal_schedule_has_no_persisted_internal_field(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            runtime.register(
                "autosign.check", lambda: {}, mode="job",
                durable=True, resumable=True, internal_only=True)
            try:
                runtime.schedules.create_internal(
                    "autosign.check", {}, 60,
                    schedule_id="autosign")
                self.assertTrue(runtime.schedules.enable("autosign")["enabled"])
                self.assertFalse(runtime.schedules.disable("autosign")["enabled"])
                self.assertNotIn("internal",
                                 runtime.schedules.get("autosign"))
                runtime.schedules.delete("autosign")
                with self.assertRaises(InputError):
                    runtime.schedules.get("autosign")
            finally:
                runtime.close()

    def test_registered_child_is_reaped_on_cancellation(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            ready = threading.Event()
            children = []
            def external():
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                children.append(child)
                context = current_task.get()
                context.register_process(child)
                ready.set()
                child.wait(timeout=10)
                context.check_cancelled()
                return {}
            runtime.register("external", external, durable=True, resumable=True)
            try:
                task_id = runtime.tasks.submit("external", {})
                self.assertTrue(ready.wait(3))
                runtime.tasks.cancel(task_id)
                self.assertEqual(wait_task(runtime, task_id)["state"], "cancelled")
                self.assertIsNotNone(children[0].poll())
            finally:
                runtime.close()
    def test_request_is_memory_only_and_transient_fields_never_persist(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            try:
                runtime.register("read", lambda: {"count": 1})
                runtime.register("answer", lambda code: {"ok": True}, durable=True, transient_fields=("code",))
                query = runtime.tasks.submit("read", {})
                answer = runtime.tasks.submit("answer", {"code": "VERY_PRIVATE_CODE"})
                self.assertEqual(wait_task(runtime, query)["result"], {"count": 1})
                self.assertEqual(wait_task(runtime, answer)["parameters"], {})
                self.assertNotIn(b"VERY_PRIVATE_CODE", runtime.tasks.database.read_bytes())
            finally:
                runtime.close()
            restarted = Runtime(root)
            try:
                with self.assertRaises(InputError):
                    restarted.tasks.get(query)
                self.assertEqual(restarted.tasks.get(answer)["state"], "succeeded")
            finally:
                restarted.close()

    def test_four_running_sixteen_waiting_and_cancel_releases_only_after_exit(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            ready, release = threading.Barrier(5), threading.Event()
            def block():
                ready.wait(timeout=3)
                release.wait(3)
                return {}
            runtime.register("block", block)
            runtime.register("fast", lambda: {})
            try:
                active = [runtime.tasks.submit("block", {}) for _ in range(4)]
                ready.wait(timeout=3)
                pending = [runtime.tasks.submit("fast", {}) for _ in range(16)]
                with self.assertRaises(BusyError):
                    runtime.tasks.submit("fast", {})
                runtime.tasks.cancel(active[0])
                with self.assertRaises(BusyError):
                    runtime.tasks.submit("fast", {})
                runtime.tasks.cancel(pending[0])
                self.assertEqual(runtime.tasks.get(pending[0])["state"], "cancelled")
                extra = runtime.tasks.submit("fast", {})
                release.set()
                self.assertEqual(wait_task(runtime, extra)["state"], "succeeded")
                self.assertEqual(wait_task(runtime, active[0])["state"], "cancelled")
            finally:
                release.set()
                runtime.close()

    def test_checkpoint_interrupt_resume_same_id_and_version_rejection(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            entered = threading.Event()
            def download():
                context = current_task.get()
                if not context.recovery:
                    context.checkpoint({"piece": 1})
                    entered.set()
                    while True:
                        context.check_cancelled()
                        time.sleep(0.01)
                return {"piece": context.recovery["piece"], "attempt": context.attempt}
            runtime.register("download", download, mode="job", durable=True, resumable=True)
            task_id = runtime.tasks.submit("download", {})
            self.assertTrue(entered.wait(3))
            runtime.close()
            resumed = Runtime(root)
            resumed.register("download", download, mode="job", durable=True, resumable=True)
            try:
                self.assertEqual(resumed.tasks.get(task_id)["state"], "interrupted")
                self.assertEqual(resumed.tasks.resume(task_id), task_id)
                row = wait_task(resumed, task_id)
                self.assertEqual(row["result"], {"piece": 1, "attempt": 2})
            finally:
                resumed.close()
            versioned = Runtime(root)
            versioned.register("download", download, mode="job", durable=True, resumable=True, version=2)
            try:
                versioned.tasks._update(task_id, state="interrupted")
                with self.assertRaises(InputError):
                    versioned.tasks.resume(task_id)
            finally:
                versioned.close()

    def test_schedule_round_is_durable_coalesced_and_restored(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            runtime.register("read", lambda: {"count": 1})
            schedule_id = runtime.schedules.create("read", {}, 60, enabled=True)
            runtime.schedules._rows[schedule_id]["next_run_at"] = 0
            runtime.schedules.tick()
            first = runtime.schedules.get(schedule_id)["current_task_id"]
            self.assertEqual(wait_task(runtime, first)["state"], "succeeded")
            runtime.schedules.tick()
            self.assertTrue(runtime.tasks.get(first)["durable"])
            self.assertEqual(runtime.schedules.get(schedule_id)["last_result"], {"count": 1})
            runtime.schedules.tick()
            self.assertEqual(len(runtime.tasks.list(command="read")), 1)
            runtime.close()
            restarted = Runtime(root)
            restarted.register("read", lambda: {})
            try:
                restarted.schedules.restore()
                self.assertTrue(restarted.schedules.get(schedule_id)["enabled"])
                restarted.schedules.disable(schedule_id)
                restarted.schedules.delete(schedule_id)
                with self.assertRaises(InputError):
                    restarted.schedules.get(schedule_id)
            finally:
                restarted.close()

    def test_schedule_running_round_does_not_overlap_and_version_is_checked(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            entered, release = threading.Event(), threading.Event()
            def work():
                entered.set()
                release.wait(3)
                return {"ok": True}
            runtime.register("work", work)
            try:
                schedule_id = runtime.schedules.create("work", {}, 10, enabled=True)
                runtime.schedules._rows[schedule_id]["next_run_at"] = 0
                runtime.schedules.tick()
                self.assertTrue(entered.wait(3))
                task_id = runtime.schedules.get(schedule_id)["current_task_id"]
                runtime.schedules.tick()
                self.assertEqual(runtime.schedules.get(schedule_id)["current_task_id"], task_id)
                self.assertEqual(len(runtime.tasks.list(command="work")), 1)
                release.set()
                self.assertEqual(wait_task(runtime, task_id)["state"], "succeeded")
                runtime.schedules.tick()
                runtime.schedules.disable(schedule_id)
                runtime.schedules._rows[schedule_id]["version"] = 99
                with self.assertRaises(InputError):
                    runtime.schedules.enable(schedule_id)
                self.assertFalse(runtime.schedules.get(schedule_id)["enabled"])
            finally:
                release.set()
                runtime.close()

    def test_internal_commands_and_invalid_parameters_are_rejected(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            runtime.register("internal", lambda: {}, internal_only=True)
            runtime.register("required", lambda x: {})
            try:
                for command, values in (("internal", {}), ("required", {}), ("missing", {})):
                    with self.assertRaises(InputError):
                        runtime.tasks.submit(command, values)
                with self.assertRaises(InputError):
                    runtime.tasks.list(limit=0)
            finally:
                runtime.close()

    def test_business_failure_keeps_schedule_enabled_internal_failure_pauses(self):
        with TemporaryDirectory() as root:
            runtime = Runtime(root)
            def business():
                raise RemoteError("temporary failure")
            def internal():
                raise RuntimeError("private internal details")
            runtime.register("business", business)
            runtime.register("internal-error", internal)
            try:
                for command, expected in (("business", True), ("internal-error", False)):
                    schedule_id = runtime.schedules.create(command, {}, 60, enabled=True)
                    runtime.schedules._rows[schedule_id]["next_run_at"] = 0
                    runtime.schedules.tick()
                    task_id = runtime.schedules.get(schedule_id)["current_task_id"]
                    row = wait_task(runtime, task_id)
                    self.assertEqual(row["state"], "failed")
                    self.assertNotIn("private", json.dumps(row))
                    runtime.schedules.tick()
                    plan = runtime.schedules.get(schedule_id)
                    self.assertEqual(plan["enabled"], expected)
                    self.assertEqual(plan["last_state"], "failed")
            finally:
                runtime.close()
