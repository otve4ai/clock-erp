"""Explicit offline bootstrap only. Never imported by Tasks HTTP handlers."""

import sqlite3
from datetime import datetime, timezone

from .repository import SCHEMA_SIGNATURE, SCHEMA_VERSION, database_path, validate_connection


def migrate_database(path, app_commit=""):
    path = database_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=1)
    try:
        connection.execute("BEGIN IMMEDIATE")
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if not objects:
            connection.execute(
                "CREATE TABLE tasks_module_migrations ("
                "version INTEGER PRIMARY KEY, signature TEXT NOT NULL, "
                "applied_at TEXT NOT NULL, app_commit TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO tasks_module_migrations(version,signature,applied_at,app_commit) "
                "VALUES(?,?,?,?)",
                (SCHEMA_VERSION, SCHEMA_SIGNATURE,
                 datetime.now(timezone.utc).isoformat(), str(app_commit)),
            )
        validate_connection(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"schema_version": SCHEMA_VERSION, "stage": "foundation"}
