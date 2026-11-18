"""Local attendance history and control when the daemon is offline."""
from contextlib import closing
import json
import sqlite3


def _schedule(database, *, enabled=None):
    if not database.is_file():
        return None
    try:
        with closing(sqlite3.connect(database)) as db, db:
            row = db.execute(
                "SELECT document FROM schedules WHERE id=?",
                ("autosign",),
            ).fetchone()
            if row is None:
                return None
            document = json.loads(row[0])
            if enabled is not None:
                document["enabled"] = bool(enabled)
                db.execute(
                    "UPDATE schedules SET document=? WHERE id=?",
                    (json.dumps(document, allow_nan=False), "autosign"),
                )
            return document
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return None


def offline_view(command, parameters=None):
    """Local queries and disabling do not start a daemon or require credentials."""
    from zju_cli.shared.records import RecordStore
    from zju_cli.shared.storage import default_data_dir
    state_database = default_data_dir()
    tasks_database = state_database / "tasks.sqlite3"
    database = state_database / "scheduler.sqlite3"
    empty = {"enabled": False, "running": False, "last_successful_check": None,
             "next_check": None, "error": None}
    if not database.is_file():
        return {"query_count": 0, "events": []} if command == "history" else {"activities": []} if command == "list" else empty
    records = RecordStore(database)
    owner = records.get("autosign", "owner")
    if not owner:
        return {"query_count": 0, "events": []} if command == "history" else {"activities": []} if command == "list" else empty
    namespace = "autosign:" + owner["username"]
    control = records.get(namespace,"control",{})
    control.pop("enabled", None)
    schedule = _schedule(tasks_database)
    if command == "stop":
        schedule = _schedule(tasks_database, enabled=False)
        records.put(namespace,"control",control)
    if command == "history":
        date = (parameters or {}).get("date")
        return {"query_count": 0, "events": [e for e in records.events(namespace) if date is None or e["confirmed_at"].startswith(date)]}
    if command == "list":
        return {"activities": [v for k,v in records.items(namespace) if k != "control"]}
    return {**empty, "enabled": bool(schedule and schedule.get("enabled")),
        "last_successful_check": control.get("last_successful_check"), "error": control.get("error"),
        "failures": control.get("failures",0), "daemon_running": False}
