"""Local persistence only. No authorization decisions or other database access."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .domain import conflict
from .schema import COLUMN_CONTRACTS, FOREIGN_KEY_CONTRACTS, TABLE_DDL, sql_tokens
from .schema import V2_COLUMN_CONTRACTS, V2_FOREIGN_KEY_CONTRACTS, V2_TABLE_DDL
from .schema import V3_COLUMN_CONTRACTS, V3_FOREIGN_KEY_CONTRACTS, V3_TABLE_DDL
from .project_repository import ProjectQueries
from .inbox_repository import InboxQueries
from .micro_repository import MicroQueries


SCHEMA_VERSION = 4
CORE_SIGNATURE = "tasks-module-core-v2"
PROJECT_SIGNATURE = "tasks-module-projects-v3"
SCHEMA_SIGNATURE = "tasks-module-microtasks-v4"
FOUNDATION_SIGNATURE = "tasks-module-foundation-v1"
TASK_COLUMNS = ("id", "task_type", "title", "description", "status", "priority",
                "created_by", "assigned_to", "deadline_date", "created_at", "updated_at",
                "completed_at", "version", "deleted_at", "related_entity_type",
                "related_entity_id", "related_entity_label", "project_id", "micro_deadline_at")
INDEXES = {"tasks_assignee_active": ("assigned_to", "deleted_at", "deadline_date", "id"),
           "tasks_creator_active": ("created_by", "deleted_at", "deadline_date", "id"),
           "tasks_active_deadline": ("deleted_at", "deadline_date", "id"),
           "task_activity_task_version": ("task_id", "task_version", "id")}
PROJECT_INDEXES = {"task_projects_owner_active": ("owner_id", "archived_at", "id"),
                   "task_project_members_user": ("user_id", "project_id"),
                   "tasks_project_active": ("project_id", "deleted_at", "status", "deadline_date", "id"),
                   "tasks_completed": ("deleted_at", "status", "completed_at", "id"),
                   "task_project_activity_order": ("project_id", "id")}
D_INDEXES = {"tasks_micro_deadline": ("task_type", "deleted_at", "micro_deadline_at", "id"),
             "task_inbox_recipient_pending": ("recipient_id", "handled_at", "id"),
             "task_inbox_recipient_toast": ("recipient_id", "notified_at", "id"),
             "task_inbox_assignment": ("task_id", "recipient_id", "handled_at")}


def database_path(path):
    resolved = Path(path).resolve()
    if resolved.name != "tasks-module.db":
        raise ValueError("Tasks module requires its own tasks-module.db file")
    return resolved


def validate_connection(connection, version=SCHEMA_VERSION):
    if version not in (1, 2, 3, 4):
        raise ValueError("Unknown Tasks schema version")
    column_contracts, table_ddl, fk_contracts = (
        (V2_COLUMN_CONTRACTS, V2_TABLE_DDL, V2_FOREIGN_KEY_CONTRACTS) if version < 3 else
        (V3_COLUMN_CONTRACTS, V3_TABLE_DDL, V3_FOREIGN_KEY_CONTRACTS) if version == 3 else
        (COLUMN_CONTRACTS, TABLE_DDL, FOREIGN_KEY_CONTRACTS))
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}
    expected = {"tasks_module_migrations"} if version == 1 else set(table_ddl)
    if tables != expected:
        raise ValueError("Tasks module schema is missing or unknown")
    for table in sorted(expected):
        columns = tuple(tuple(row[1:]) for row in connection.execute("PRAGMA table_info(" + table + ")"))
        if columns != column_contracts[table]:
            raise ValueError("Tasks column contract differs")
        sql = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        if not sql or sql_tokens(sql) != sql_tokens(table_ddl[table]):
            raise ValueError("Tasks table constraints differ from contract")
        foreign_keys = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_list(" + table + ")"))
        if foreign_keys != fk_contracts[table]:
            raise ValueError("Tasks foreign key contract differs")
    rows = [tuple(row) for row in connection.execute(
        "SELECT version,signature FROM tasks_module_migrations ORDER BY version LIMIT 5")]
    expected_versions = [(1, FOUNDATION_SIGNATURE)]
    if version >= 2:
        expected_versions.append((2, CORE_SIGNATURE))
    if version >= 3:
        expected_versions.append((3, PROJECT_SIGNATURE))
    if version >= 4:
        expected_versions.append((4, SCHEMA_SIGNATURE))
    if rows != expected_versions:
        raise ValueError("Tasks module migration version is missing or unknown")
    if connection.execute("SELECT name FROM sqlite_master WHERE type IN ('view','trigger')").fetchone():
        raise ValueError("Unexpected Tasks schema objects")
    if version >= 2:
        indexes = dict(INDEXES, **(PROJECT_INDEXES if version >= 3 else {}))
        if version >= 4:
            indexes.update(D_INDEXES)
        for index, fields in indexes.items():
            if tuple(row[2] for row in connection.execute("PRAGMA index_info(" + index + ")")) != fields:
                raise ValueError("Tasks index differs from contract")


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
            return {"schema_version": SCHEMA_VERSION, "stage": "microtasks"}


def _authorize(action, first, second, database, trigger):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class TaskSession(ProjectQueries, InboxQueries, MicroQueries):
    def __init__(self, connection):
        self.connection = connection

    def get(self, task_id):
        row = self.connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row is not None else None

    def get_visible(self, task_id, visibility):
        clause, parameters = visibility
        row = self.connection.execute("SELECT * FROM tasks WHERE id=? AND " + clause,
                                      [task_id] + parameters).fetchone()
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
        visibility, parameters = scope["visibility"]
        clauses, parameters = ["task_type='normal'", "deleted_at IS NULL", visibility], list(parameters)
        for field in ("assigned_to", "created_by"):
            if field in scope:
                clauses.append(field + "=?")
                parameters.append(scope[field])
        for field in ("status", "priority", "deadline_date", "project_id", "assigned_to", "created_by"):
            if field in options:
                clauses.append(field + (" IS NULL" if options[field] is None else "=?"))
                if options[field] is not None:
                    parameters.append(options[field])
        if options.get("project") == "none":
            clauses.append("project_id IS NULL")
        view = options.get("view")
        if view == "archive":
            clauses.append("status='done'")
        elif view == "delegated_waiting":
            clauses.append("created_by=? AND assigned_to!=? AND status!='done'")
            parameters.extend((scope["actor_id"], scope["actor_id"]))
        elif view in ("today", "overdue"):
            clauses.append("deadline_date " + ("=" if view == "today" else "<") + " ? AND status!='done'")
            parameters.append(today)
        for field, operator in (("date_from", ">="), ("date_to", "<")):
            if field in options:
                clauses.append("completed_at " + operator + " ?")
                parameters.append(options[field])
        for field, expression in (("overdue", "deadline_date < ? AND status != 'done'"),
                                  ("today", "deadline_date = ? AND status != 'done'")):
            if field in options:
                # COALESCE gives undated tasks a false computed value.
                clauses.append("COALESCE((" + expression + "),0)=" + ("1" if options[field] else "0"))
                parameters.append(today)
        if options.get("search"):
            escaped = options["search"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(tasks_casefold(title) LIKE ? ESCAPE '\\' OR tasks_casefold(description) LIKE ? ESCAPE '\\')")
            parameters.extend(("%" + escaped + "%", "%" + escaped + "%"))
        return " AND ".join(clauses), parameters

    def list(self, scope, options, today, include_inbox=False):
        where, parameters = self._where(scope, options, today)
        if "status" not in options and options.get("view") != "archive":
            where += " AND status!='done'"
        total = self.connection.execute("SELECT COUNT(*) FROM tasks WHERE " + where, parameters).fetchone()[0]
        order = "completed_at DESC,id DESC" if options.get("view") == "archive" else "deadline_date IS NULL,deadline_date,id"
        columns = "tasks.*"
        if include_inbox:
            columns += (",EXISTS(SELECT 1 FROM task_inbox_events e WHERE e.task_id=tasks.id "
                        "AND e.recipient_id=tasks.assigned_to AND e.handled_at IS NULL) AS inbox_pending")
        rows = self.connection.execute(
            "SELECT " + columns + " FROM tasks WHERE " + where +
            " ORDER BY " + order + " LIMIT ? OFFSET ?",
            parameters + [options["limit"], options["offset"]]).fetchall()
        return {"items": [dict(row) for row in rows], "total": total,
                "limit": options["limit"], "offset": options["offset"]}

    def dashboard_summary(self, my_scope, created_scope, today):
        # Personal workload and delegated work have deliberately different scopes.
        # Both reads share the same transaction/snapshot and visibility policy.
        result = self.filtered_summary(my_scope, {}, today)
        where, parameters = self._where(created_scope, {"view": "delegated_waiting"}, today)
        result["delegated_waiting"] = self.connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE " + where, parameters).fetchone()[0]
        return result

    def filtered_summary(self, scope, options, today):
        where, parameters = self._where(scope, options, today)
        row = self.connection.execute(
            "SELECT COUNT(*) AS total,"
            "COALESCE(SUM(status='new'),0) AS new,"
            "COALESCE(SUM(status='in_progress'),0) AS in_progress,"
            "COALESCE(SUM(status='waiting'),0) AS waiting,"
            "COALESCE(SUM(status='done'),0) AS done,"
            "COALESCE(SUM(deadline_date < ? AND status!='done'),0) AS overdue,"
            "COALESCE(SUM(deadline_date = ? AND status!='done'),0) AS today,"
            "COALESCE(SUM(created_by=? AND assigned_to!=? AND status!='done'),0) AS delegated_waiting "
            "FROM tasks WHERE " + where,
            [today, today, scope["actor_id"], scope["actor_id"]] + parameters).fetchone()
        result = dict(row)
        result["inbox_counts"] = self.inbox_counts(scope["actor_id"])
        result["inbox"] = result["inbox_counts"]["count"]
        return result
