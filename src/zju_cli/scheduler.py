"""Fixed interval scheduling; waiting never occupies an executor thread."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import threading
import time
import json
import sqlite3
import math
from pathlib import Path
from contextlib import closing


@dataclass(frozen=True)
class NextRun:
    delay: float


@dataclass
class PeriodicTask:
    task_type: str
    parameters: dict
    interval: float
    next_run: float
    enabled: bool = False
    running: bool = False
    failures: int = 0
    version: int = 1
    resume_on_restart: bool = False


class PeriodicScheduler:
    def __init__(self, *, clock=time.monotonic, workers=2, max_backoff=3600, database=None):
        self.clock = clock
        self.max_backoff = max_backoff
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="periodic")
        self.capacity = workers
        self.functions = {}
        self.read_only = {}
        self.versions = {}
        self.tasks = {}
        self.lock = threading.RLock()
        self.closed = False
        self.database = Path(database) if database is not None else None
        if self.database is not None:
            self.database.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.execute("CREATE TABLE IF NOT EXISTS periodic_tasks (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
                for task_id, document in connection.execute("SELECT id, document FROM periodic_tasks"):
                    value = json.loads(document)
                    self.tasks[task_id] = PeriodicTask(value["task_type"], value["parameters"],
                        value["interval"], self.clock() + value["interval"], failures=value.get("failures", 0),
                        version=value.get("version", 1),
                        resume_on_restart=value.get("resume_on_restart", False),
                        enabled=bool(value.get("resume_on_restart") and value.get("enabled")))

    def _save(self):
        if self.database is None:
            return
        from dataclasses import asdict
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for task_id, task in self.tasks.items():
                connection.execute("INSERT OR REPLACE INTO periodic_tasks VALUES (?, ?)",
                    (task_id, json.dumps(asdict(task), allow_nan=False)))

    def register(self, task_type, function, *, read_only=True, version=1):
        with self.lock:
            if task_type in self.functions:
                raise ValueError("Task type already registered")
            self.functions[task_type] = function
            self.read_only[task_type] = read_only
            self.versions[task_type] = version

    def create(self, task_id, task_type, parameters, interval, *, version=1, resume_on_restart=False):
        import json
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Interval must be positive")
        parameters = json.loads(json.dumps(parameters, allow_nan=False))
        with self.lock:
            if (self.closed or task_id in self.tasks or task_type not in self.functions
                    or self.versions[task_type] != version):
                raise ValueError("Invalid periodic task")
            self.tasks[task_id] = PeriodicTask(task_type, parameters, interval, self.clock() + interval,
                version=version, resume_on_restart=resume_on_restart)
            self._save()

    def enable(self, task_id):
        with self.lock:
            if self.closed:
                raise RuntimeError("Scheduler stopped")
            task = self.tasks[task_id]
            if task.task_type not in self.functions or self.versions[task.task_type] != task.version:
                raise ValueError("Unknown periodic task type or version; cannot resume")
            task.enabled = True
            task.next_run = self.clock() + task.interval
            self._save()

    def pause(self, task_id):
        with self.lock:
            self.tasks[task_id].enabled = False
            self._save()

    def status(self, task_id):
        from dataclasses import asdict
        with self.lock:
            return asdict(self.tasks[task_id])

    def configure(self, task_id, *, interval):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Interval must be positive")
        with self.lock:
            self.tasks[task_id].interval = interval
            self._save()

    def tick(self):
        with self.lock:
            if self.closed:
                return
            available = self.capacity - sum(t.running for t in self.tasks.values())
            for task in self.tasks.values():
                if available <= 0:
                    break
                if (task.enabled and task.task_type in self.functions and
                        self.versions[task.task_type] == task.version and
                        not task.running and task.next_run <= self.clock()):
                    task.running = True
                    available -= 1
                    self.executor.submit(self._run, task)

    def _run(self, task):
        failed = False
        result = None
        try:
            result = self.functions[task.task_type](**task.parameters)
        except Exception:
            failed = True
        finally:
            with self.lock:
                task.failures = task.failures + 1 if failed else 0
                if failed and not self.read_only.get(task.task_type, False):
                    task.enabled = False
                delay = task.interval
                if failed:
                    delay = min(self.max_backoff, delay * 2 ** min(task.failures, 20))
                elif isinstance(result, NextRun) and math.isfinite(result.delay) and result.delay > 0:
                    delay = result.delay
                task.next_run = self.clock() + delay
                task.running = False
                self._save()

    def close(self):
        with self.lock:
            self.closed = True
            for task in self.tasks.values():
                if not task.resume_on_restart:
                    task.enabled = False
            self._save()
        self.executor.shutdown(wait=True)
