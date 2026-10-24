from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import BusyError, InputError, LocalFileError

TERMINAL = {"succeeded", "failed", "cancelled"}
LEASE_SECONDS = 60


def default_data_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "zju-cli"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "zju-cli"


class JobStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().absolute()
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self.connection() as connection:
                connection.execute("""CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL,
                    lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0)""")
        except OSError as exc:
            raise LocalFileError("无法创建任务存储目录") from exc

    @contextmanager
    def connection(self):
        connection = None
        try:
            connection = sqlite3.connect(self.root / "jobs.sqlite3", timeout=10)
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection
        except sqlite3.Error as exc:
            raise LocalFileError("无法读取或更新任务数据库") from exc
        finally:
            if connection is not None:
                connection.close()

    def job_dir(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise InputError("无效的 job_id")
        return self.root / "jobs" / job_id

    def create(self, document: dict):
        try:
            self.job_dir(document["job_id"]).mkdir(parents=True, mode=0o700)
        except OSError as exc:
            raise LocalFileError("无法创建任务目录") from exc
        with self.connection() as connection:
            connection.execute("INSERT INTO jobs (id, document) VALUES (?, ?)",
                               (document["job_id"], json.dumps(document, ensure_ascii=False)))

    def _row(self, connection, job_id):
        self.job_dir(job_id)
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise InputError("任务不存在")
        return row

    def _write(self, connection, job_id, document):
        document["updated_at"] = time.time()
        connection.execute("UPDATE jobs SET document=? WHERE id=?",
                           (json.dumps(document, ensure_ascii=False), job_id))

    def get(self, job_id: str) -> dict:
        with self.connection() as connection:
            row = self._row(connection, job_id)
            document = json.loads(row["document"])
            document["worker_active"] = row["lease_until"] > time.time()
            return document

    def list(self, limit=20):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
            return [{**json.loads(row["document"]), "worker_active": row["lease_until"] > time.time()} for row in rows]

    def update(self, job_id, changes, *, token=None):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            if token is not None and (row["lease_token"] != token or row["lease_until"] <= time.time()):
                raise BusyError("Worker 已失去任务租约")
            document = json.loads(row["document"])
            document.update(changes)
            self._write(connection, job_id, document)

    def reserve(self, job_id, token, *, changes=None):
        # Reserve before spawning so concurrent resume commands cannot launch duplicate workers.
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            if row["lease_until"] > time.time():
                raise BusyError("已有 Worker 运行中；崩溃后租约最多需 60 秒过期")
            document = json.loads(row["document"])
            if document["status"] in {"succeeded", "cancelled"}:
                raise InputError("任务已结束；成功结果使用 jobs result")
            document.update(changes or {})
            document.update({"status": "queued", "cancel_requested": False, "error": None,
                             "completed_at": None, "attempts": document.get("attempts", 0) + 1})
            if not document.get("result"):
                document.update({"stage": "queued", "progress": None})
            connection.execute("UPDATE jobs SET lease_token=?, lease_until=? WHERE id=?",
                               (token, time.time() + LEASE_SECONDS, job_id))
            self._write(connection, job_id, document)

    def adopt(self, job_id, token):
        self.heartbeat(job_id, token)
        self.update(job_id, {"worker_pid": os.getpid()}, token=token)

    def heartbeat(self, job_id, token):
        with self.connection() as connection:
            now = time.time()
            changed = connection.execute("""UPDATE jobs SET lease_until=?
                WHERE id=? AND lease_token=? AND lease_until>?""",
                (now + LEASE_SECONDS, job_id, token, now)).rowcount
            if not changed:
                raise BusyError("Worker 已失去任务租约")

    def release(self, job_id, token):
        with self.connection() as connection:
            connection.execute("UPDATE jobs SET lease_token=NULL, lease_until=0 WHERE id=? AND lease_token=?",
                               (job_id, token))

    def cancel(self, job_id):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            document = json.loads(row["document"])
            if document["status"] in TERMINAL:
                raise InputError("任务已经结束")
            document["cancel_requested"] = True
            if row["lease_until"] <= time.time():
                document.update({"status": "cancelled", "stage": "finished", "completed_at": time.time()})
            self._write(connection, job_id, document)
        return self.get(job_id)

    def pause_unleased(self, job_id):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            if row["lease_until"] > time.time():
                raise BusyError("任务由其他有效租约持有")
            document = json.loads(row["document"])
            if document["status"] not in TERMINAL:
                document["status"] = "paused"
                self._write(connection, job_id, document)
