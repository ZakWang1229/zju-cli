"""One bounded executor for requests, downloads, and scheduled rounds."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar
from copy import deepcopy
from pathlib import Path
import inspect
import json
import logging
import math
import os
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from zju_cli.shared.errors import BusyError, CancelledError, InputError, RemoteError, ZjuError

logger = logging.getLogger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
current_task = ContextVar("zju_current_task", default=None)


class Interrupted(Exception):
    pass


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                if process.poll() is None:
                    process.terminate()
                else:
                    return
            except OSError:
                process.terminate()
        else:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


class TaskContext:
    def __init__(self, tasks, task_id):
        self.tasks, self.id = tasks, task_id
        self.job_id = task_id
        row = tasks._rows[task_id]
        self.attempt, self.recovery, self.deadline = row["attempt"], row["recovery"], row.get("deadline")
        self.http_timeout = row.get("http_timeout") or tasks.config.timeout_seconds
        self.cancelled = threading.Event()
        self.processes = set()
        self.lock = threading.Lock()

    def check_cancelled(self):
        if self.tasks.stopping.is_set():
            raise Interrupted()
        if self.cancelled.is_set():
            raise CancelledError("任务已取消")
        if self.deadline is not None and time.time() >= self.deadline:
            raise CancelledError("任务超过截止时间")

    check = check_cancelled

    def should_stop(self):
        self.check_cancelled()
        return False

    def report(self, stage=None, progress=None):
        self.check_cancelled()
        changes = {"progress": progress}
        if stage is not None:
            changes["stage"] = stage
        self.tasks._update(self.id, **changes)

    def checkpoint(self, data):
        self.check_cancelled()
        data = json.loads(json.dumps(data, allow_nan=False))
        self.tasks._update(self.id, recovery=data)
        self.recovery = data

    def artifact_path(self, name):
        if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."} or any(c in name for c in "/\\:"):
            raise InputError("无效任务文件名")
        configured = self.tasks._rows[self.id].get("artifact_dir")
        directory = Path(configured) if configured else (
            Path(self.tasks._rows[self.id].get("artifact_root") or self.tasks.root) / "tasks" / self.id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory / name

    def register_process(self, process):
        with self.lock:
            self.processes.add(process)
        try:
            self.check_cancelled()
        except BaseException:
            self.stop_processes()
            raise
        return process

    def unregister_process(self, process):
        with self.lock:
            self.processes.discard(process)

    def stop_processes(self):
        with self.lock:
            processes = tuple(self.processes)
        for process in processes:
            stop_process(process)


class TasksModule:
    def __init__(self, daemon, config):
        self.daemon, self.config = daemon, config
        self.root = daemon.state_dir
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database = self.root / "tasks.sqlite3"
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self._rows, self._parameters, self._contexts, self._futures = {}, {}, {}, {}
        self._groups = {}
        self._serial_locks = {}
        self.slots = threading.BoundedSemaphore(20)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="task")
        with closing(sqlite3.connect(self.database)) as db, db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise InputError("未知任务数据库版本")
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            db.execute("PRAGMA user_version=1")
            for task_id, raw in db.execute("SELECT id, document FROM tasks"):
                row = json.loads(raw)
                if row.get("username") != config.username:
                    continue
                if row["state"] in {"queued", "running"}:
                    row.update(state="interrupted", completed_at=time.time())
                self._rows[task_id] = row
        for row in self._rows.values():
            self._save(row)
        if os.name != "nt":
            self.root.chmod(0o700)
            self.database.chmod(0o600)
        for name in ("submit", "get", "list", "cancel", "resume"):
            daemon.command_registry.register("task." + name, getattr(self, name))

    def _save(self, row):
        if row["durable"]:
            with closing(sqlite3.connect(self.database, timeout=10)) as db, db:
                db.execute("INSERT OR REPLACE INTO tasks VALUES (?,?)", (row["id"], json.dumps(row, ensure_ascii=False, allow_nan=False)))

    def _update(self, task_id, **changes):
        changes = json.loads(json.dumps(changes, allow_nan=False))
        with self.lock:
            row = self._rows[task_id]
            row.update(changes, updated_at=time.time())
            self._save(row)

    def _purge(self):
        cutoff = time.time() - 1800
        for key, row in tuple(self._rows.items()):
            if not row["durable"] and row["state"] in TERMINAL and row["completed_at"] < cutoff:
                self._rows.pop(key)
                self._futures.pop(key, None)

    def submit_request(self, command, parameters, *, username,
                       deadline=None, timeout=None, artifact_root=None):
        if username != self.config.username:
            raise RemoteError("后台绑定其他主账号，请先停止后台")
        with self.daemon.lock:
            if not self.daemon.accepting:
                raise RemoteError("后台正在停止")
            return self.submit(
                command,
                parameters,
                deadline=deadline,
                http_timeout=timeout,
                artifact_root=artifact_root,
            )

    def submit(self, command, parameters, *, internal=False, durable=None, deadline=None, http_timeout=None, artifact_root=None):
        if not isinstance(command, str):
            raise InputError("任务命令名必须是字符串")
        spec = self.daemon.command_registry.command(command)
        if spec is None or (spec.internal_only and not internal) or command.startswith(("task.", "schedule.")) or command in {"daemon.stop", "command.list"}:
            raise InputError("该命令不可作为业务任务提交")
        if not isinstance(parameters, dict) or any(not isinstance(k, str) for k in parameters):
            raise InputError("任务参数必须是 JSON 对象")
        try:
            parameters = json.loads(json.dumps(parameters, allow_nan=False))
            inspect.signature(spec.handler).bind(**parameters)
        except (TypeError, ValueError) as exc:
            raise InputError("任务参数无效") from exc
        if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
            raise InputError("任务截止时间无效")
        if http_timeout is not None and (type(http_timeout) not in (int, float) or not math.isfinite(http_timeout) or http_timeout <= 0):
            raise InputError("请求超时必须是有限正数")
        with self.lock:
            self._purge()
            if self.stopping.is_set() or not self.slots.acquire(blocking=False):
                raise BusyError("后台正在停止或任务队列已满")
            task_id = uuid.uuid4().hex
            persisted = {k: v for k, v in parameters.items() if k not in spec.transient_fields}
            now = time.time()
            row = {"id": task_id, "command": command, "version": spec.version,
                "username": self.config.username, "parameters": persisted, "state": "queued",
                "stage": "queued", "progress": None, "recovery": None, "attempt": 1,
                "durable": spec.durable if durable is None else bool(durable),
                "created_at": now, "updated_at": now, "completed_at": None,
                "result": None, "error": None, "deadline": deadline, "http_timeout": http_timeout,
                "artifact_root": str(Path(artifact_root).expanduser().absolute()) if artifact_root else None}
            try:
                self._rows[task_id] = row
                self._parameters[task_id] = parameters
                self._contexts[task_id] = TaskContext(self, task_id)
                if spec.serial_group:
                    self._groups.setdefault(spec.serial_group, []).append(task_id)
                self._save(row)
                self._futures[task_id] = self.executor.submit(self._run, task_id, spec)
            except BaseException:
                self.slots.release()
                self._rows.pop(task_id, None)
                self._parameters.pop(task_id, None)
                self._contexts.pop(task_id, None)
                raise
            return task_id

    def _run(self, task_id, spec):
        context = self._contexts[task_id]
        token = current_task.set(context)
        group = self._serial_locks.setdefault(spec.serial_group, threading.Lock()) if spec.serial_group else None
        locked = False
        try:
            if group:
                while not group.acquire(timeout=0.1):
                    context.check_cancelled()
                locked = True
            context.check_cancelled()
            self._update(task_id, state="running", stage="running", started_at=time.time())
            logger.info("task=%s command=%s started", task_id, spec.name)
            parameters = self._parameters[task_id]
            result = spec.handler(**parameters)
            json.dumps(result, allow_nan=False)
            if context.cancelled.is_set():
                raise CancelledError("任务已取消")
            if context.deadline is not None and time.time() >= context.deadline:
                raise CancelledError("任务超过截止时间")
            self._update(task_id, state="succeeded", stage="finished", result=result, completed_at=time.time())
            logger.info("task=%s command=%s succeeded", task_id, spec.name)
        except Interrupted:
            self._update(task_id, state="interrupted", completed_at=time.time())
        except CancelledError:
            self._update(task_id, state="cancelled", completed_at=time.time())
        except Exception as exc:
            if context.cancelled.is_set():
                self._update(task_id, state="cancelled", completed_at=time.time())
                return
            if self.stopping.is_set():
                self._update(task_id, state="interrupted", completed_at=time.time())
                return
            error = {"type": type(exc).__name__, "message": str(exc) if isinstance(exc, ZjuError) else "任务执行失败",
                     "exit_code": getattr(exc, "exit_code", 1)}
            self._update(task_id, state="failed", stage="finished", error=error, completed_at=time.time())
            logger.error("task=%s command=%s failed reason=%s", task_id, spec.name, type(exc).__name__)
        finally:
            if locked:
                group.release()
            context.stop_processes()
            current_task.reset(token)
            logger.info("task=%s command=%s finished state=%s", task_id, spec.name, self._rows[task_id]["state"])
            with self.lock:
                self._parameters.pop(task_id, None)
                self._contexts.pop(task_id, None)
                self.slots.release()

    def get(self, task_id):
        if not isinstance(task_id, str):
            raise InputError("无效任务 ID")
        with self.lock:
            self._purge()
            if task_id not in self._rows:
                raise InputError("任务不存在")
            return deepcopy(self._rows[task_id])
    def list(self, command=None, state=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100 or state is not None and state not in TERMINAL | {"queued", "running"}:
            raise InputError("任务筛选参数无效，limit 必须为 1–100")
        with self.lock:
            self._purge()
            rows = [row for row in self._rows.values() if (command is None or row["command"] == command)
                    and (state is None or row["state"] == state)]
            return deepcopy(sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit])

    def cancel(self, task_id):
        with self.lock:
            row = self.get(task_id)
            if row["state"] in TERMINAL:
                return row
            context = self._contexts.get(task_id)
            if context:
                context.cancelled.set()
            future = self._futures.get(task_id)
            if future is not None and future.cancel():
                self._update(task_id, state="cancelled", completed_at=time.time())
                self._parameters.pop(task_id, None)
                self._contexts.pop(task_id, None)
                self.slots.release()
            elif future is None or future.done():
                self._update(task_id, state="cancelled", completed_at=time.time())
        if context:
            context.stop_processes()
        return self.get(task_id)

    def resume(self, task_id, *, internal=False):
        with self.lock:
            row = self.get(task_id)
            spec = self.daemon.command_registry.command(row["command"])
            if row["state"] != "interrupted" or not row["durable"] or spec is None or not spec.resumable or spec.version != row["version"] or (spec.internal_only and not internal):
                raise InputError("任务状态、命令版本或恢复能力不允许恢复")
            previous = self._futures.get(task_id)
            if previous is not None and not previous.done():
                raise BusyError("任务尚未退出")
            if self.stopping.is_set() or not self.slots.acquire(blocking=False):
                raise BusyError("后台正在停止或任务队列已满")
            self._parameters[task_id] = deepcopy(row["parameters"])
            self._update(task_id, state="queued", completed_at=None, error=None, attempt=row["attempt"] + 1)
            self._contexts[task_id] = TaskContext(self, task_id)
            self._futures[task_id] = self.executor.submit(self._run, task_id, spec)
        return task_id

    def begin_stop(self):
        self.stopping.set()
        with self.lock:
            for task_id, future in self._futures.items():
                if task_id in self._contexts and future.cancel():
                    self._update(task_id, state="interrupted", completed_at=time.time())
                    self._parameters.pop(task_id, None)
                    self._contexts.pop(task_id, None)
                    self.slots.release()
            contexts = tuple(self._contexts.values())
        for context in contexts:
            context.stop_processes()

    def close(self):
        self.begin_stop()
        self.executor.shutdown(wait=True)
