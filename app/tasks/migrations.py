"""Explicit offline bootstrap only. Never imported by Tasks HTTP handlers."""

import sqlite3
from datetime import datetime, timezone

from .repository import (FOUNDATION_SIGNATURE, SCHEMA_SIGNATURE, SCHEMA_VERSION,
                         database_path, validate_connection)
from .schema import CORE_DDL, LEDGER_DDL


def migrate_database(path, app_commit=""):
    path = database_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=1, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if not objects:
            connection.execute(LEDGER_DDL)
            connection.execute(
                "INSERT INTO tasks_module_migrations(version,signature,applied_at,app_commit) "
                "VALUES(?,?,?,?)",
                (1, FOUNDATION_SIGNATURE,
                 datetime.now(timezone.utc).isoformat(), str(app_commit)),
            )
        # This upgrades only our empty Stage A foundation. It never reads any
        # legacy database, records or identifiers.
        if objects and ("tasks_module_migrations",) not in objects:
            raise ValueError("Tasks module schema is unknown")
        versions = connection.execute("SELECT version FROM tasks_module_migrations ORDER BY version").fetchall()
        if versions == [(1,)]:
            validate_connection(connection, version=1)
            for statement in CORE_DDL:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO tasks_module_migrations(version,signature,applied_at,app_commit) VALUES(?,?,?,?)",
                (SCHEMA_VERSION, SCHEMA_SIGNATURE, datetime.now(timezone.utc).isoformat(), str(app_commit)))
        validate_connection(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"schema_version": SCHEMA_VERSION, "stage": "core"}
