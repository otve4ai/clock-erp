"""Passive schema contract; no connections, migrations or import side effects."""

import re


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

LEDGER_DDL = ("CREATE TABLE tasks_module_migrations ("
              "version INTEGER PRIMARY KEY, signature TEXT NOT NULL, "
              "applied_at TEXT NOT NULL, app_commit TEXT NOT NULL)")
TABLE_DDL = {"tasks_module_migrations": LEDGER_DDL,
             "tasks": CORE_DDL[0], "task_activity": CORE_DDL[1]}

# PRAGMA table_info: name, declared type, notnull, default SQL, pk position.
# SQLite reports notnull=0 for INTEGER PRIMARY KEY, including SQLite 3.7.17.
COLUMN_CONTRACTS = {
    "tasks_module_migrations": (
        ("version", "INTEGER", 0, None, 1),
        ("signature", "TEXT", 1, None, 0),
        ("applied_at", "TEXT", 1, None, 0),
        ("app_commit", "TEXT", 1, None, 0),
    ),
    "tasks": (
        ("id", "INTEGER", 0, None, 1),
        ("task_type", "TEXT", 1, "'normal'", 0),
        ("title", "TEXT", 1, None, 0),
        ("description", "TEXT", 1, "''", 0),
        ("status", "TEXT", 1, "'new'", 0),
        ("priority", "TEXT", 1, "'normal'", 0),
        ("created_by", "INTEGER", 1, None, 0),
        ("assigned_to", "INTEGER", 1, None, 0),
        ("deadline_date", "TEXT", 0, None, 0),
        ("created_at", "TEXT", 1, None, 0),
        ("updated_at", "TEXT", 1, None, 0),
        ("completed_at", "TEXT", 0, None, 0),
        ("version", "INTEGER", 1, "1", 0),
        ("deleted_at", "TEXT", 0, None, 0),
        ("related_entity_type", "TEXT", 0, None, 0),
        ("related_entity_id", "TEXT", 0, None, 0),
        ("related_entity_label", "TEXT", 0, None, 0),
    ),
    "task_activity": (
        ("id", "INTEGER", 0, None, 1),
        ("task_id", "INTEGER", 1, None, 0),
        ("actor", "INTEGER", 1, None, 0),
        ("event_type", "TEXT", 1, None, 0),
        ("timestamp", "TEXT", 1, None, 0),
        ("task_version", "INTEGER", 1, None, 0),
        ("payload", "TEXT", 1, None, 0),
    ),
}
FOREIGN_KEY_CONTRACTS = {
    "tasks_module_migrations": (), "tasks": (),
    "task_activity": ((0, 0, "tasks", "task_id", "id", "NO ACTION", "NO ACTION", "NONE"),),
}


def sql_tokens(sql):
    # Compare the full table definition, not CHECK substrings (which an OR or
    # comment could bypass). Ignore whitespace and keyword case only; preserve
    # string literals exactly. Unknown/equivalent rewrites fail closed.
    tokens = re.findall(r"'(?:''|[^'])*'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|<=|>=|!=|<>|[^\s]", sql)
    return tuple(token if token.startswith("'") else token.lower() for token in tokens)
