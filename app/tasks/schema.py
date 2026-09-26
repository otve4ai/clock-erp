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

# Immutable v2 contracts are used only by the explicit offline upgrade.
V2_CORE_DDL = CORE_DDL
V2_TABLE_DDL = TABLE_DDL
V2_COLUMN_CONTRACTS = COLUMN_CONTRACTS
V2_FOREIGN_KEY_CONTRACTS = FOREIGN_KEY_CONTRACTS

PROJECT_DDL = (
    """CREATE TABLE task_projects (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
        owner_id INTEGER NOT NULL CHECK(owner_id>0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        archived_at TEXT,
        version INTEGER NOT NULL DEFAULT 1 CHECK(version>0)
    )""",
    """CREATE TABLE task_project_members (
        project_id INTEGER NOT NULL REFERENCES task_projects(id),
        user_id INTEGER NOT NULL CHECK(user_id>0),
        created_at TEXT NOT NULL,
        PRIMARY KEY(project_id,user_id)
    )""",
    """CREATE TABLE task_project_activity (
        id INTEGER PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES task_projects(id),
        actor INTEGER NOT NULL CHECK(actor>0),
        event_type TEXT NOT NULL CHECK(event_type IN
            ('created','renamed','archived','restored','member_added','member_removed')),
        timestamp TEXT NOT NULL,
        project_version INTEGER NOT NULL CHECK(project_version>0),
        payload TEXT NOT NULL
    )""",
)
CORE_DDL = (
    V2_CORE_DDL[0].replace("related_entity_label TEXT,", "related_entity_label TEXT,\n        project_id INTEGER REFERENCES task_projects(id),"),
    V2_CORE_DDL[1].replace("'deleted','restored'", "'deleted','restored','project_changed'"),
) + PROJECT_DDL + V2_CORE_DDL[2:] + (
    "CREATE INDEX task_projects_owner_active ON task_projects(owner_id,archived_at,id)",
    "CREATE INDEX task_project_members_user ON task_project_members(user_id,project_id)",
    "CREATE INDEX tasks_project_active ON tasks(project_id,deleted_at,status,deadline_date,id)",
    "CREATE INDEX tasks_completed ON tasks(deleted_at,status,completed_at,id)",
    "CREATE INDEX task_project_activity_order ON task_project_activity(project_id,id)",
)
TABLE_DDL = dict(V2_TABLE_DDL, tasks=CORE_DDL[0], task_activity=CORE_DDL[1],
                 task_projects=PROJECT_DDL[0], task_project_members=PROJECT_DDL[1],
                 task_project_activity=PROJECT_DDL[2])
COLUMN_CONTRACTS = dict(V2_COLUMN_CONTRACTS)
COLUMN_CONTRACTS.update({
    "tasks": V2_COLUMN_CONTRACTS["tasks"] + (("project_id", "INTEGER", 0, None, 0),),
    "task_projects": (("id", "INTEGER", 0, None, 1), ("name", "TEXT", 1, None, 0),
                      ("owner_id", "INTEGER", 1, None, 0), ("created_at", "TEXT", 1, None, 0),
                      ("updated_at", "TEXT", 1, None, 0), ("archived_at", "TEXT", 0, None, 0),
                      ("version", "INTEGER", 1, "1", 0)),
    "task_project_members": (("project_id", "INTEGER", 1, None, 1), ("user_id", "INTEGER", 1, None, 2),
                             ("created_at", "TEXT", 1, None, 0)),
    "task_project_activity": (("id", "INTEGER", 0, None, 1), ("project_id", "INTEGER", 1, None, 0),
                              ("actor", "INTEGER", 1, None, 0), ("event_type", "TEXT", 1, None, 0),
                              ("timestamp", "TEXT", 1, None, 0), ("project_version", "INTEGER", 1, None, 0),
                              ("payload", "TEXT", 1, None, 0)),
})
FOREIGN_KEY_CONTRACTS = dict(V2_FOREIGN_KEY_CONTRACTS)
FOREIGN_KEY_CONTRACTS["task_projects"] = ()
for _table in ("tasks", "task_project_members", "task_project_activity"):
    FOREIGN_KEY_CONTRACTS[_table] = ((0, 0, "task_projects", "project_id", "id", "NO ACTION", "NO ACTION", "NONE"),)


def sql_tokens(sql):
    # Compare the full table definition, not CHECK substrings (which an OR or
    # comment could bypass). Ignore whitespace and keyword case only; preserve
    # string literals exactly. Unknown/equivalent rewrites fail closed.
    tokens = re.findall(r"'(?:''|[^'])*'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|<=|>=|!=|<>|[^\s]", sql)
    return tuple(token if token.startswith("'") else token.lower() for token in tokens)
