"""Explicit offline bootstrap only. Never imported by Tasks HTTP handlers."""

import sqlite3
from datetime import datetime, timezone

from .repository import (FOUNDATION_SIGNATURE, SCHEMA_SIGNATURE, SCHEMA_VERSION,
                         database_path, validate_connection)


CORE_DDL = (
    """CREATE TABLE tasks (
        id INTEGER PRIMARY KEY,
        task_type TEXT NOT NULL DEFAULT 'normal' CHECK(task_type='normal'),
        title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 500),
        description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=50000),
        status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','in_progress','waiting','done')),
        priority TEXT NOT NULL DEFAULT 'normal' CHECK(priority IN ('low','normal','high')),
        created_by INTEGER NOT NULL CHECK(created_by>0),
        assigned_to INTEGER NOT NULL CHECK(assigned_to>0),
        deadline_date TEXT CHECK(deadline_date IS NULL OR
            (length(deadline_date)=10 AND deadline_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        completed_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
        deleted_at TEXT,
        related_entity_type TEXT,
        related_entity_id TEXT,
        related_entity_label TEXT,
        CHECK((status='done' AND completed_at IS NOT NULL) OR (status!='done' AND completed_at IS NULL))
    )""",
    """CREATE TABLE task_activity (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        actor INTEGER NOT NULL CHECK(actor>0),
        event_type TEXT NOT NULL CHECK(event_type IN
            ('created','content_changed','reassigned','deadline_changed','priority_changed',
             'status_changed','completed','reopened','related_reference_changed','deleted','restored')),
        timestamp TEXT NOT NULL,
        task_version INTEGER NOT NULL CHECK(task_version>0),
        payload TEXT NOT NULL
    )""",
    "CREATE INDEX tasks_assignee_active ON tasks(assigned_to,deleted_at,deadline_date,id)",
    "CREATE INDEX tasks_creator_active ON tasks(created_by,deleted_at,deadline_date,id)",
    "CREATE INDEX tasks_active_deadline ON tasks(deleted_at,deadline_date,id)",
    "CREATE INDEX task_activity_task_version ON task_activity(task_id,task_version,id)",
)


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
            connection.execute(
                "CREATE TABLE tasks_module_migrations ("
                "version INTEGER PRIMARY KEY, signature TEXT NOT NULL, "
                "applied_at TEXT NOT NULL, app_commit TEXT NOT NULL)"
            )
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
