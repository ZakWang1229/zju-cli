"""Read persisted tasks for offline CLI compatibility; execution stays in daemon."""
from contextlib import closing
import json
import sqlite3

from zju_cli.shared.storage import default_data_dir


def read_tasks(*, task_id=None, command=None, root=None):
    root = root or default_data_dir()
    path = root / "tasks.sqlite3"
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        rows = [json.loads(raw) for (raw,) in db.execute("SELECT document FROM tasks")]
    return [row for row in rows if (task_id is None or row["id"] == task_id)
            and (command is None or row["command"] == command)]


def audio_summary(row):
    return {**row, "task_id": row["id"], "job_id": row["id"], "status": row["state"],
            "sub_id": row["parameters"].get("sub_id"), "course_id": row["parameters"].get("course_id"),
            "output_path": row["parameters"].get("out"), "attempts": row["attempt"]}
