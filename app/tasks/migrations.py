"""Explicit offline bootstrap only. Never imported by Tasks HTTP handlers."""

import sqlite3
from datetime import datetime, timezone

from .repository import (FOUNDATION_SIGNATURE, CORE_SIGNATURE, PROJECT_SIGNATURE, SCHEMA_SIGNATURE, SCHEMA_VERSION,
                         database_path, validate_connection)
from .schema import CORE_DDL, LEDGER_DDL, V2_CORE_DDL, V3_CORE_DDL


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
            for statement in V2_CORE_DDL:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO tasks_module_migrations(version,signature,applied_at,app_commit) VALUES(?,?,?,?)",
                (2, CORE_SIGNATURE, datetime.now(timezone.utc).isoformat(), str(app_commit)))
            versions.append((2,))
        if versions == [(1,), (2,)]:
            validate_connection(connection, version=2)
            # Rebuild only this module's v2 tables, inside the same transaction.
            # FK remains ON. Children are copied/dropped first; no rename quirks,
            # writable_schema, external database or legacy records are involved.
            connection.execute("CREATE TEMP TABLE tasks_v2_copy AS SELECT * FROM tasks")
            connection.execute("CREATE TEMP TABLE activity_v2_copy AS SELECT * FROM task_activity")
            connection.execute("DROP TABLE task_activity")
            connection.execute("DROP TABLE tasks")
            for statement in V3_CORE_DDL:
                connection.execute(statement)
            fields = ",".join(row[1] for row in connection.execute("PRAGMA table_info(tasks_v2_copy)"))
            connection.execute("INSERT INTO tasks (" + fields + ") SELECT " + fields + " FROM tasks_v2_copy")
            connection.execute("INSERT INTO task_activity SELECT * FROM activity_v2_copy")
            connection.execute("DROP TABLE activity_v2_copy")
            connection.execute("DROP TABLE tasks_v2_copy")
            connection.execute(
                "INSERT INTO tasks_module_migrations(version,signature,applied_at,app_commit) VALUES(?,?,?,?)",
                (3, PROJECT_SIGNATURE, datetime.now(timezone.utc).isoformat(), str(app_commit)))
            versions.append((3,))
        if versions == [(1,), (2,), (3,)]:
            validate_connection(connection, version=3)
            connection.execute("CREATE TEMP TABLE tasks_v3_copy AS SELECT * FROM tasks")
            connection.execute("CREATE TEMP TABLE activity_v3_copy AS SELECT * FROM task_activity")
            connection.execute("DROP TABLE task_activity")
            connection.execute("DROP TABLE tasks")
            # Existing projects/members/history remain intact; rebuild only
            # tables whose CHECK contracts changed and their indexes.
            for statement in CORE_DDL:
                if statement.startswith("CREATE TABLE task_project") or statement.startswith("CREATE INDEX task_project"):
                    continue
                connection.execute(statement)
            fields = ",".join(row[1] for row in connection.execute("PRAGMA table_info(tasks_v3_copy)"))
            connection.execute("INSERT INTO tasks (" + fields + ") SELECT " + fields + " FROM tasks_v3_copy")
            connection.execute("INSERT INTO task_activity SELECT * FROM activity_v3_copy")
            connection.execute("DROP TABLE activity_v3_copy")
            connection.execute("DROP TABLE tasks_v3_copy")
            connection.execute(
                "INSERT INTO tasks_module_migrations(version,signature,applied_at,app_commit) VALUES(?,?,?,?)",
                (4, SCHEMA_SIGNATURE, datetime.now(timezone.utc).isoformat(), str(app_commit)))
        validate_connection(connection)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {"schema_version": SCHEMA_VERSION, "stage": "microtasks"}
