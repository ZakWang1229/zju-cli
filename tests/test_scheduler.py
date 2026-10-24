import unittest
import tempfile
from pathlib import Path
from zju_cli.scheduler import PeriodicScheduler


class ManualExecutor:
    def __init__(self):
        self.pending = []

    def submit(self, function, *args):
        self.pending.append((function, args))

    def finish(self):
        function, args = self.pending.pop(0)
        function(*args)

    def shutdown(self, **kwargs):
        pass


class SchedulerTests(unittest.TestCase):
    def test_unknown_persisted_version_cannot_enable(self):
        scheduler = PeriodicScheduler()
        try:
            scheduler.register("check", lambda: None)
            scheduler.create("one", "check", {}, 10)
            scheduler.tasks["one"].version = 99
            with self.assertRaisesRegex(ValueError, "version"):
                scheduler.enable("one")
            self.assertFalse(scheduler.status("one")["enabled"])
        finally:
            scheduler.close()

    def test_failed_write_is_paused_without_retry(self):
        now = [0]
        scheduler = PeriodicScheduler(clock=lambda: now[0])
        scheduler.executor.shutdown()
        scheduler.executor = ManualExecutor()
        calls = []
        def write():
            calls.append(1)
            raise RuntimeError("response lost")
        scheduler.register("write", write, read_only=False)
        scheduler.create("one", "write", {}, 10)
        scheduler.enable("one")
        now[0] = 10
        scheduler.tick()
        scheduler.executor.finish()
        self.assertFalse(scheduler.status("one")["enabled"])
        now[0] = 1000
        scheduler.tick()
        self.assertEqual(calls, [1])
        self.assertFalse(scheduler.executor.pending)
        scheduler.close()

    def test_restart_restores_paused_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "periodic.sqlite3"
            scheduler = PeriodicScheduler(database=database)
            scheduler.register("check", lambda: None)
            scheduler.create("one", "check", {}, 10)
            scheduler.enable("one")
            scheduler.close()
            restored = PeriodicScheduler(database=database)
            try:
                self.assertFalse(restored.status("one")["enabled"])
                self.assertFalse(restored.status("one")["running"])
                self.assertEqual(restored.status("one")["task_type"], "check")
            finally:
                restored.close()

    def test_coalescing_pause_and_backoff(self):
        now = [0]
        scheduler = PeriodicScheduler(clock=lambda: now[0], workers=1, max_backoff=25)
        scheduler.executor.shutdown()
        scheduler.executor = ManualExecutor()
        outcomes = [False, False, True]
        def execute():
            if not outcomes.pop(0):
                raise RuntimeError("simulated")
        scheduler.register("check", execute)
        scheduler.create("one", "check", {}, 10)
        scheduler.enable("one")
        now[0] = 100
        scheduler.tick()
        scheduler.tick()
        self.assertEqual(len(scheduler.executor.pending), 1)
        scheduler.executor.finish()
        self.assertEqual(scheduler.status("one")["next_run"], 120)
        now[0] = 120
        scheduler.tick()
        scheduler.executor.finish()
        self.assertEqual(scheduler.status("one")["next_run"], 145)
        now[0] = 145
        scheduler.tick()
        scheduler.executor.finish()
        self.assertEqual(scheduler.status("one")["next_run"], 155)
        self.assertEqual(scheduler.status("one")["failures"], 0)
        scheduler.pause("one")
        now[0] = 1000
        scheduler.tick()
        self.assertFalse(scheduler.executor.pending)
        scheduler.close()
