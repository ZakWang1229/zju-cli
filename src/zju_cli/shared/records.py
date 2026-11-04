"""Transactional module documents and repairable event exports."""
class RecordStore:
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
        import os
        if os.name != "nt":
            self.database.chmod(0o600)

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
            exists = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='periodic_tasks'").fetchone()
            if not exists:
                return
            row = connection.execute("SELECT document FROM periodic_tasks WHERE id=?", (task_id,)).fetchone()
            if row:
                document = json.loads(row[0])
                document["enabled"] = False
                connection.execute("UPDATE periodic_tasks SET document=? WHERE id=?", (json.dumps(document),task_id))
