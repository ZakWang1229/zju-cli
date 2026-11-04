"""Persistent fixed-delay plans; each round is a task in the shared queue."""
from contextlib import closing
from copy import deepcopy
import json
import inspect
import logging
import math
import sqlite3
import threading
import time
import uuid

from zju_cli.shared.errors import BusyError, InputError
from zju_cli.backend.modules.tasks import TERMINAL

logger = logging.getLogger(__name__)


class SchedulesModule:
    def __init__(self, daemon, config):
        self.daemon, self.config = daemon, config
        self.database = daemon.state_dir / "tasks.sqlite3"
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="schedules", daemon=True)
        self._rows = {}
        migrated = []
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS schedules (id TEXT PRIMARY KEY, document TEXT NOT NULL)")
            for key, raw in db.execute("SELECT id,document FROM schedules"):
                row = json.loads(raw)
                if row.get("username") == config.username:
                    if row.pop("internal", None) is not None:
                        migrated.append(key)
                    self._rows[key] = row
        for key in migrated:
            self._save(self._rows[key])
        for name in ("create", "get", "list", "enable", "disable", "delete"):
            daemon.command_registry.register("schedule." + name, getattr(self, name))

    def _save(self, row):
        with closing(sqlite3.connect(self.database, timeout=10)) as db, db:
            db.execute("INSERT OR REPLACE INTO schedules VALUES (?,?)", (row["id"], json.dumps(row, allow_nan=False)))

    def create(self, command, parameters, interval, enabled=False):
        return self._create(command, parameters, interval, enabled)

    def create_internal(self, command, parameters, interval, enabled=False, *,
                        schedule_id, resume_on_restart=False):
        if not isinstance(schedule_id, str) or not schedule_id:
            raise InputError("内部计划 ID 无效")
        return self._create(
            command,
            parameters,
            interval,
            enabled,
            schedule_id=schedule_id,
            allow_internal=True,
            resume_on_restart=resume_on_restart,
        )

    def _create(self, command, parameters, interval, enabled=False, *,
                schedule_id=None, allow_internal=False,
                resume_on_restart=False):
        if not isinstance(command, str):
            raise InputError("计划命令名必须是字符串")
        if type(interval) not in (int, float) or not math.isfinite(interval) or interval <= 0 or type(enabled) is not bool:
            raise InputError("计划间隔或启用参数无效")
        spec = self.daemon.command_registry.command(command)
        if spec is None or (spec.internal_only and not allow_internal) or command.startswith(("task.", "schedule.")):
            raise InputError("计划命令不存在或不可调度")
        if not isinstance(parameters, dict) or any(key in parameters for key in spec.transient_fields):
            raise InputError("计划参数无效，不能持久保存瞬态字段")
        try:
            inspect.signature(spec.handler).bind(**parameters)
            parameters = json.loads(json.dumps(parameters, allow_nan=False))
        except (ValueError, TypeError) as exc:
            raise InputError("计划参数无效") from exc
        with self.lock:
            if self.stopping.is_set():
                raise BusyError("调度器正在停止")
            key = schedule_id or uuid.uuid4().hex
            if key in self._rows:
                raise InputError("计划已存在")
            row = {"id": key, "username": self.config.username, "command": command,
                   "parameters": parameters, "interval": float(interval), "enabled": enabled,
                   "version": spec.version, "current_task_id": None, "last_task_id": None,
                   "next_run_at": time.time() + interval, "error": None, "last_result": None,
                   "last_error": None, "last_state": None,
                   "next_delay": None,
                   "resume_on_restart": bool(resume_on_restart)}
            self._rows[key] = row
            self._save(row)
            return key

    def get(self, schedule_id):
        with self.lock:
            if schedule_id not in self._rows:
                raise InputError("计划不存在")
            return deepcopy(self._rows[schedule_id])

    def list(self):
        with self.lock:
            return deepcopy(list(self._rows.values()))

    def enable(self, schedule_id):
        return self._enable(schedule_id)

    def _enable(self, schedule_id):
        with self.lock:
            row = self._rows.get(schedule_id)
            if row is None:
                raise InputError("计划不存在")
            spec = self.daemon.command_registry.command(row["command"])
            if self.stopping.is_set() or spec is None or spec.version != row["version"]:
                raise InputError("计划命令版本无效或后台正在停止")
            if row["current_task_id"]:
                task = self.daemon.tasks.get(row["current_task_id"])
                if task["state"] in TERMINAL:
                    row.update(current_task_id=None, last_task_id=task["id"])
            row.update(enabled=True, error=None, next_run_at=time.time() + row["interval"])
            self._save(row)
        return self.get(schedule_id)

    def disable(self, schedule_id):
        return self._disable(schedule_id)

    def _disable(self, schedule_id):
        with self.lock:
            row = self._rows.get(schedule_id)
            if row is None:
                raise InputError("计划不存在")
            row["enabled"] = False
            self._save(row)
        return self.get(schedule_id)

    def delete(self, schedule_id):
        with self.lock:
            row = self.get(schedule_id)
            if row["enabled"] or row["current_task_id"] and self.daemon.tasks.get(row["current_task_id"])["state"] not in TERMINAL:
                raise InputError("删除计划前须停用并等待当前轮结束")
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("DELETE FROM schedules WHERE id=?", (schedule_id,))
            del self._rows[schedule_id]
        return {"id": schedule_id, "deleted": True}

    def set_next_delay(self, schedule_id, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            raise InputError("等待时间必须是有限正数")
        with self.lock:
            row = self._rows[schedule_id]
            row["next_delay"] = float(seconds)
            if not row["current_task_id"]:
                row["next_run_at"] = time.time() + seconds
            self._save(row)

    def set_interval(self, schedule_id, seconds):
        if (type(seconds) not in (int, float)
                or not math.isfinite(seconds) or seconds <= 0):
            raise InputError("计划间隔必须是有限正数")
        with self.lock:
            row = self.get(schedule_id)
            row["interval"] = float(seconds)
            self._save(row)
        return self.get(schedule_id)

    def set_resume_on_restart(self, schedule_id, enabled=True):
        if type(enabled) is not bool:
            raise InputError("恢复策略必须是布尔值")
        with self.lock:
            row = self.get(schedule_id)
            row["resume_on_restart"] = enabled
            self._save(row)
        return self.get(schedule_id)

    def restore(self):
        with self.lock:
            for row in self._rows.values():
                spec = self.daemon.command_registry.command(row["command"])
                if spec is None or spec.version != row["version"]:
                    row.update(enabled=False, error="unknown_command_version")
                elif row["current_task_id"]:
                    task = self.daemon.tasks.get(row["current_task_id"])
                    if task["state"] == "interrupted":
                        if row["enabled"] and row.get("resume_on_restart", False):
                            self.daemon.tasks.resume(task["id"], internal=True)
                        else:
                            row.update(enabled=False, error="interrupted_round")
                self._save(row)

    def tick(self):
        with self.lock:
            if self.stopping.is_set():
                return
            for row in self._rows.values():
                if row["current_task_id"]:
                    task = self.daemon.tasks.get(row["current_task_id"])
                    if task["state"] not in TERMINAL:
                        continue
                    row.update(current_task_id=None, last_task_id=task["id"], last_result=task.get("result"),
                               last_error=task.get("error"), last_state=task["state"])
                    delay = row["next_delay"] or row["interval"]
                    row.update(next_run_at=time.time() + delay, next_delay=None)
                    if task["state"] == "failed":
                        from zju_cli.shared import errors
                        error_type = getattr(errors, (task.get("error") or {}).get("type", ""), None)
                        if not isinstance(error_type, type) or not issubclass(error_type, errors.ZjuError):
                            row.update(enabled=False, error="internal_error")
                    if task["state"] == "interrupted":
                        row.update(enabled=False, error="interrupted_round")
                    self._save(row)
                    continue
                if not row["enabled"] or row["next_run_at"] > time.time():
                    continue
                spec = self.daemon.command_registry.command(row["command"])
                if spec is None:
                    row.update(enabled=False, error="unknown_command_version")
                    self._save(row)
                    continue
                try:
                    task_id = self.daemon.tasks.submit(row["command"], row["parameters"],
                        internal=spec.internal_only, durable=True)
                except BusyError:
                    continue
                except InputError:
                    row.update(enabled=False, error="invalid_command")
                else:
                    row["current_task_id"] = task_id
                self._save(row)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stopping.wait(0.1):
            try:
                self.tick()
            except Exception as exc:
                logger.error("scheduler tick failed reason=%s", type(exc).__name__)

    def close(self):
        self.stopping.set()
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join()
