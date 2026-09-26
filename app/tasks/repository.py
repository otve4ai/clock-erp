"""Stage A storage: read-only schema status, no business operations yet."""

import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1
SCHEMA_SIGNATURE = "tasks-module-foundation-v1"


def database_path(path):
    resolved = Path(path).resolve()
    if resolved.name != "tasks-module.db":
        raise ValueError("Tasks module requires its own tasks-module.db file")
    return resolved


def validate_connection(connection):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    if tables != {"tasks_module_migrations"}:
        raise ValueError("Tasks module schema is missing or unknown")
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(tasks_module_migrations)"))
    if columns != ("version", "signature", "applied_at", "app_commit"):
        raise ValueError("Tasks module migration ledger differs from contract")
    rows = connection.execute("SELECT version,signature FROM tasks_module_migrations LIMIT 2").fetchall()
    if rows != [(SCHEMA_VERSION, SCHEMA_SIGNATURE)]:
        raise ValueError("Tasks module migration version is missing or unknown")


class TasksRepository:
    def __init__(self, path):
        self.path = database_path(path)

    def status(self):
        # mode=ro must not create a missing database or repair its schema.
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
        try:
            validate_connection(connection)
            return {"schema_version": SCHEMA_VERSION, "stage": "foundation"}
        finally:
            connection.close()
