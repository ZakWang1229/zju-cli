"""Business-independent leased task execution shared by all data directories."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
import uuid

from .errors import BusyError, CancelledError, InputError
from .storage import JobStore
from .scheduler import PeriodicScheduler


class JobRecords:
    """Transactional task-owned documents and append-only export records."""
    def __init__(self, database):
        import sqlite3
        from pathlib import Path
        from contextlib import closing
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE IF NOT EXISTS task_records (namespace TEXT, id TEXT, document TEXT NOT NULL, PRIMARY KEY(namespace,id))")
            connection.execute("CREATE TABLE IF NOT EXISTS task_events (namespace TEXT, id TEXT, document TEXT NOT NULL, exported INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(namespace,id))")

    def get(self, namespace, key, default=None):
        return next((v for k, v in self.items(namespace) if k == key), default)

    def items(self, namespace):
        import sqlite3, json
        from contextlib import closing
        with closing(sqlite3.connect(self.database)) as connection:
            return [(k, json.loads(v)) for k, v in connection.execute(
                "SELECT id,document FROM task_records WHERE namespace=?", (namespace,))]

    def put(self, namespace, key, document, *, event=None):
        import sqlite3, json
        from contextlib import closing
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT OR REPLACE INTO task_records VALUES (?,?,?)",
                (namespace, key, json.dumps(document, ensure_ascii=False, allow_nan=False)))
            if event:
                connection.execute("INSERT OR IGNORE INTO task_events(namespace,id,document) VALUES (?,?,?)",
                    (namespace, event["event_id"], json.dumps(event, ensure_ascii=False, allow_nan=False)))

    def events(self, namespace, *, pending=False):
        import sqlite3, json
        from contextlib import closing
        with closing(sqlite3.connect(self.database)) as connection:
            return [json.loads(v) for (v,) in connection.execute(
                "SELECT document FROM task_events WHERE namespace=?" + (" AND exported=0" if pending else "") + " ORDER BY rowid",
                (namespace,))]

    def exported(self, namespace, event_id):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE task_events SET exported=1 WHERE namespace=? AND id=?", (namespace,event_id))

    def disable_periodic(self, task_id):
        import sqlite3, json
        from contextlib import closing
        with closing(sqlite3.connect(self.database)) as connection, connection:
            row = connection.execute("SELECT document FROM periodic_tasks WHERE id=?", (task_id,)).fetchone()
            if row:
                document = json.loads(row[0])
                document["enabled"] = False
                connection.execute("UPDATE periodic_tasks SET document=? WHERE id=?", (json.dumps(document),task_id))


class Paused(Exception):
    pass


class ExecutionContext:
    def __init__(self, store, job_id, token, stopping):
        self.store, self.job_id, self.token = store, job_id, token
        self.stopping = stopping
        self.lost = threading.Event()
        self.pausing = threading.Event()

    def check(self):
        if self.lost.is_set():
            raise BusyError("任务租约失效")
        if self.stopping.is_set() or self.pausing.is_set():
            raise Paused()
        if self.store.get(self.job_id).get("cancel_requested"):
            raise CancelledError("任务已取消")

    def should_stop(self):
        self.check()
        return False

    def artifact_path(self, name):
        from pathlib import Path
        if Path(name).name != name or name in {"", ".", ".."}:
            raise InputError("无效任务文件名")
        return self.store.job_dir(self.job_id) / name

    def report(self, progress=None, *, stage=None):
        self.check()
        changes = {"progress": progress}
        if stage is not None:
            changes["stage"] = stage
        self.store.update(self.job_id, changes, token=self.token)

    def checkpoint(self, information):
        self.check()
        self.store.update(self.job_id, {"recovery": information}, token=self.token)


class JobManager:
    def __init__(self, *, periodic_database=None, clock=time.monotonic):
        self.functions = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="download")
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.active = {}
        self.periodic = PeriodicScheduler(database=periodic_database, clock=clock)
        self.records = JobRecords(periodic_database) if periodic_database is not None else None
        self.heartbeat_done = threading.Event()
        self.heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.heartbeat_thread.start()

    def _heartbeat(self):
        while not self.heartbeat_done.wait(10):
            with self.lock:
                contexts = list(self.active.values())
            for context in contexts:
                try:
                    context.store.heartbeat(context.job_id, context.token)
                except Exception:
                    context.lost.set()

    def register(self, task_type, version, function):
        key = (task_type, version)
        if key in self.functions:
            raise ValueError("Task already registered")
        self.functions[key] = function

    def register_periodic(self, task_type, function, *, read_only=True, version=1):
        self.periodic.register(task_type, function, read_only=read_only, version=version)

    def create_periodic(self, task_id, task_type, parameters, interval, *, version=1, resume_on_restart=False):
        self.periodic.create(task_id, task_type, parameters, interval, version=version,
                             resume_on_restart=resume_on_restart)

    def enable_periodic(self, task_id):
        self.periodic.enable(task_id)

    def pause_periodic(self, task_id):
        self.periodic.pause(task_id)

    def periodic_status(self, task_id):
        return self.periodic.status(task_id)

    def create(self, root, document):
        import json
        document = json.loads(json.dumps(document, allow_nan=False))
        key = (document["task_type"], document["task_version"])
        if key not in self.functions:
            raise InputError("未知任务类型或版本")
        with self.lock:
            if self.stopping.is_set():
                raise BusyError("后台正在停止")
            JobStore(root).create(document)

    def pause(self, root, job_id):
        store = JobStore(root)
        identity = (str(store.root.resolve()), job_id)
        with self.lock:
            context = self.active.get(identity)
            if context is not None:
                context.pausing.set()
                return
            store.pause_unleased(job_id)

    def cancel(self, root, job_id):
        return JobStore(root).cancel(job_id)

    def resume(self, root, job_id):
        store = JobStore(root)
        job = store.get(job_id)
        key = (job.get("task_type", job.get("kind")), job.get("task_version", 1))
        if key not in self.functions:
            raise InputError("未知任务类型或版本，不可恢复")
        identity = (str(store.root.resolve()), job_id)
        with self.lock:
            if self.stopping.is_set():
                raise BusyError("后台正在停止")
            token = uuid.uuid4().hex
            store.reserve(job_id, token)
            context = ExecutionContext(store, job_id, token, self.stopping)
            self.active[identity] = context
            try:
                self.executor.submit(self._run, identity, context, self.functions[key], job)
            except BaseException:
                self.active.pop(identity, None)
                store.release(job_id, token)
                raise

    def _run(self, identity, context, function, job):
        changes = {}
        try:
            context.check()
            context.store.update(context.job_id, {"status": "running", "started_at": time.time()}, token=context.token)
            result = function(context, job.get("parameters", job), job.get("recovery"))
            context.check()
            changes = {"status": "succeeded", "stage": "finished", "progress": None,
                       "result": result, "completed_at": time.time(), "error": None, "error_exit_code": None}
        except Paused:
            changes = {"status": "paused", "completed_at": None}
        except CancelledError:
            changes = {"status": "cancelled", "stage": "finished", "progress": None, "completed_at": time.time()}
        except BusyError:
            pass
        except Exception as exc:
            changes = {"status": "failed", "stage": "finished", "progress": None,
                       "error": "任务执行失败", "error_type": type(exc).__name__,
                       "error_exit_code": getattr(exc, "exit_code", 1), "completed_at": time.time()}
        finally:
            try:
                if changes:
                    context.store.update(context.job_id, changes, token=context.token)
            finally:
                context.store.release(context.job_id, context.token)
                with self.lock:
                    self.active.pop(identity, None)

    def close(self):
        with self.lock:
            self.stopping.set()
        self.periodic.close()
        self.executor.shutdown(wait=True)
        self.heartbeat_done.set()
        self.heartbeat_thread.join()
