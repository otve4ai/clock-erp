"""Local persistence only. No authorization decisions or other database access."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .domain import conflict


SCHEMA_VERSION = 2
SCHEMA_SIGNATURE = "tasks-module-core-v2"
FOUNDATION_SIGNATURE = "tasks-module-foundation-v1"
TASK_COLUMNS = ("id", "task_type", "title", "description", "status", "priority",
                "created_by", "assigned_to", "deadline_date", "created_at", "updated_at",
                "completed_at", "version", "deleted_at", "related_entity_type",
                "related_entity_id", "related_entity_label")
ACTIVITY_COLUMNS = ("id", "task_id", "actor", "event_type", "timestamp", "task_version", "payload")
INDEXES = {"tasks_assignee_active": ("assigned_to", "deleted_at", "deadline_date", "id"),
           "tasks_creator_active": ("created_by", "deleted_at", "deadline_date", "id"),
           "tasks_active_deadline": ("deleted_at", "deadline_date", "id"),
           "task_activity_task_version": ("task_id", "task_version", "id")}


def database_path(path):
    resolved = Path(path).resolve()
    if resolved.name != "tasks-module.db":
        raise ValueError("Tasks module requires its own tasks-module.db file")
    return resolved


def validate_connection(connection, version=SCHEMA_VERSION):
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    expected = {"tasks_module_migrations"} if version == 1 else {"tasks_module_migrations", "tasks", "task_activity"}
    if tables != expected:
        raise ValueError("Tasks module schema is missing or unknown")
    columns = tuple(row[1] for row in connection.execute("PRAGMA table_info(tasks_module_migrations)"))
    if columns != ("version", "signature", "applied_at", "app_commit"):
        raise ValueError("Tasks module migration ledger differs from contract")
    rows = [tuple(row) for row in connection.execute(
        "SELECT version,signature FROM tasks_module_migrations ORDER BY version LIMIT 3")]
    expected_versions = [(1, FOUNDATION_SIGNATURE)]
    if version == 2:
        expected_versions.append((2, SCHEMA_SIGNATURE))
    if rows != expected_versions:
        raise ValueError("Tasks module migration version is missing or unknown")
    if connection.execute("SELECT name FROM sqlite_master WHERE type IN ('view','trigger')").fetchone():
        raise ValueError("Unexpected Tasks schema objects")
    if version == 2:
        for table, fields in (("tasks", TASK_COLUMNS), ("task_activity", ACTIVITY_COLUMNS)):
            if tuple(row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")) != fields:
                raise ValueError("Tasks table differs from contract")
        for index, fields in INDEXES.items():
            if tuple(row[2] for row in connection.execute("PRAGMA index_info(" + index + ")")) != fields:
                raise ValueError("Tasks index differs from contract")
        foreign_keys = list(connection.execute("PRAGMA foreign_key_list(task_activity)"))
        if len(foreign_keys) != 1 or tuple(foreign_keys[0][2:5]) != ("tasks", "task_id", "id"):
            raise ValueError("Tasks activity foreign key differs from contract")


class TasksRepository:
    def __init__(self, path):
        self.path = database_path(path)

    @contextmanager
    def transaction(self, write=False):
        # mode=rw never creates a missing DB. Schema changes remain offline.
        connection = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                     uri=True, timeout=0.25, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.set_authorizer(_authorize)
            connection.create_function("tasks_casefold", 1, lambda value: value.casefold())
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            validate_connection(connection)
            yield TaskSession(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def status(self):
        with self.transaction():
            return {"schema_version": SCHEMA_VERSION, "stage": "core"}


def _authorize(action, first, second, database, trigger):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class TaskSession:
    def __init__(self, connection):
        self.connection = connection

    def get(self, task_id):
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def create(self, values):
        fields = TASK_COLUMNS[1:]
        cursor = self.connection.execute(
            "INSERT INTO tasks (" + ",".join(fields) + ") VALUES(" + ",".join("?" for _ in fields) + ")",
            tuple(values[field] for field in fields))
        return self.get(cursor.lastrowid)

    def update(self, task_id, expected_version, values):
        fields = sorted(values)
        if set(fields) - set(TASK_COLUMNS[1:]) or "version" in fields:
            raise ValueError("Invalid repository update fields")
        cursor = self.connection.execute(
            "UPDATE tasks SET " + ",".join(field + "=?" for field in fields) +
            ",version=version+1 WHERE id=? AND version=?",
            tuple(values[field] for field in fields) + (task_id, expected_version))
        if cursor.rowcount != 1:
            raise conflict()
        return self.get(task_id)

    def add_activity(self, task, actor, event_type, timestamp, payload):
        self.connection.execute(
            "INSERT INTO task_activity(task_id,actor,event_type,timestamp,task_version,payload) VALUES(?,?,?,?,?,?)",
            (task["id"], actor, event_type, timestamp, task["version"],
             json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def activity(self, task_id, limit, offset):
        rows = self.connection.execute(
            "SELECT * FROM task_activity WHERE task_id=? ORDER BY id LIMIT ? OFFSET ?",
            (task_id, limit, offset)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            result.append(event)
        return result

    @staticmethod
    def _where(scope, options, today):
        clauses, parameters = ["deleted_at IS NULL"], []
        for field in ("assigned_to", "created_by"):
            if field in scope:
                clauses.append(field + "=?")
                parameters.append(scope[field])
        for field in ("status", "priority", "deadline_date"):
            if field in options:
                clauses.append(field + (" IS NULL" if options[field] is None else "=?"))
                if options[field] is not None:
                    parameters.append(options[field])
        for field, expression in (("overdue", "deadline_date < ? AND status != 'done'"),
                                  ("today", "deadline_date = ?")):
            if field in options:
                # COALESCE gives undated tasks a false computed value.
                clauses.append("COALESCE((" + expression + "),0)=" + ("1" if options[field] else "0"))
                parameters.append(today)
        if options.get("search"):
            escaped = options["search"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(tasks_casefold(title) LIKE ? ESCAPE '\\' OR tasks_casefold(description) LIKE ? ESCAPE '\\')")
            parameters.extend(("%" + escaped + "%", "%" + escaped + "%"))
        return " AND ".join(clauses), parameters

    def list(self, scope, options, today):
        where, parameters = self._where(scope, options, today)
        total = self.connection.execute("SELECT COUNT(*) FROM tasks WHERE " + where, parameters).fetchone()[0]
        rows = self.connection.execute(
            "SELECT * FROM tasks WHERE " + where +
            " ORDER BY deadline_date IS NULL,deadline_date,id LIMIT ? OFFSET ?",
            parameters + [options["limit"], options["offset"]]).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "limit": options["limit"], "offset": options["offset"]}

    def summary(self, scope, options, today):
        where, parameters = self._where(scope, options, today)
        row = self.connection.execute(
            "SELECT COUNT(*) AS total,"
            "COALESCE(SUM(status='new'),0) AS new,"
            "COALESCE(SUM(status='in_progress'),0) AS in_progress,"
            "COALESCE(SUM(status='waiting'),0) AS waiting,"
            "COALESCE(SUM(status='done'),0) AS done,"
            "COALESCE(SUM(deadline_date < ? AND status!='done'),0) AS overdue,"
            "COALESCE(SUM(deadline_date = ?),0) AS today FROM tasks WHERE " + where,
            [today, today] + parameters).fetchone()
        return dict(row)
