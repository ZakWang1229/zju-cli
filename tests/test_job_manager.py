import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from zju_cli.job_manager import JobManager
from zju_cli.storage import JobStore
from zju_cli.errors import BusyError, InputError


class JobManagerTests(unittest.TestCase):
    def test_cancel_running_job_releases_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            job_id = uuid.uuid4().hex
            store.create({"job_id": job_id, "kind": "fake", "status": "queued"})
            entered = threading.Event()
            def execute(context, parameters, recovery):
                entered.set()
                while True:
                    context.check()
                    time.sleep(0.005)
            manager = JobManager()
            manager.register("fake", 1, execute)
            try:
                manager.resume(store.root, job_id)
                self.assertTrue(entered.wait(2))
                store.cancel(job_id)
                deadline = time.monotonic() + 2
                while store.get(job_id)["worker_active"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(store.get(job_id)["status"], "cancelled")
                self.assertFalse(store.get(job_id)["worker_active"])
            finally:
                manager.close()

    def test_legacy_lease_is_preserved_then_expired_job_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            job_id = uuid.uuid4().hex
            store.create({"job_id": job_id, "kind": "legacy", "status": "paused"})
            store.reserve(job_id, "legacy-worker")
            manager = JobManager()
            executed = threading.Event()
            manager.register("legacy", 1, lambda *args: executed.set())
            try:
                with self.assertRaises(BusyError):
                    manager.resume(store.root, job_id)
                self.assertFalse(executed.is_set())
                with store.connection() as connection:
                    connection.execute("UPDATE jobs SET lease_until=0 WHERE id=?", (job_id,))
                manager.resume(store.root, job_id)
                self.assertTrue(executed.wait(2))
            finally:
                manager.close()
            self.assertFalse(store.get(job_id)["worker_active"])

    def test_global_concurrency_and_stop_pause_queued_and_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = JobManager()
            entered = threading.Event()
            lock = threading.Lock()
            running = [0]
            maximum = [0]
            def execute(context, parameters, recovery):
                with lock:
                    running[0] += 1
                    maximum[0] = max(maximum[0], running[0])
                    if running[0] == 2:
                        entered.set()
                try:
                    while not context.stopping.wait(0.01):
                        context.check()
                    context.check()
                finally:
                    with lock:
                        running[0] -= 1
            manager.register("fake", 1, execute)
            records = []
            try:
                for index in range(3):
                    store = JobStore(Path(temporary) / str(index % 2))
                    job_id = uuid.uuid4().hex
                    store.create({"job_id": job_id, "kind": "fake", "status": "queued"})
                    records.append((store, job_id))
                    manager.resume(store.root, job_id)
                self.assertTrue(entered.wait(5))
                self.assertEqual(maximum[0], 2)
                with self.assertRaises(BusyError):
                    manager.resume(records[0][0].root, records[0][1])
            finally:
                manager.close()
            self.assertEqual(maximum[0], 2)
            for store, job_id in records:
                job = store.get(job_id)
                self.assertEqual(job["status"], "paused")
                self.assertFalse(job["worker_active"])

    def test_unknown_version_never_claims_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = JobStore(Path(temporary))
            job_id = uuid.uuid4().hex
            store.create({"job_id": job_id, "kind": "fake", "task_version": 99, "status": "paused"})
            manager = JobManager()
            try:
                manager.register("fake", 1, lambda *args: None)
                with self.assertRaises(InputError):
                    manager.resume(store.root, job_id)
                self.assertFalse(store.get(job_id)["worker_active"])
            finally:
                manager.close()
