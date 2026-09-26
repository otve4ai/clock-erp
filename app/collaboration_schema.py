"""Read-only contract for shared assignments; deliberately ignores Tasks tables."""

import sqlite3
from pathlib import Path


REQUIRED_COLUMNS = {
    "entity_assignments": {
        "entity_type", "entity_id", "responsible_user_id", "updated_at", "updated_by",
    },
    "assignment_history": {
        "id", "entity_type", "entity_id", "previous_user_id", "new_user_id",
        "actor_user_id", "comment", "operation_key", "created_at",
    },
    "inbox_events": {
        "id", "recipient_user_id", "actor_user_id", "event_type", "entity_type",
        "entity_id", "created_at", "read_at", "metadata_json", "operation_key",
    },
}
REQUIRED_UNIQUE = {
    "entity_assignments": ("entity_type", "entity_id"),
    "assignment_history": ("operation_key",),
    "inbox_events": ("recipient_user_id", "event_type", "operation_key"),
}


def validate_collaboration_database(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        for table, required in REQUIRED_COLUMNS.items():
            columns = connection.execute("PRAGMA table_info({})".format(table)).fetchall()
            if not required.issubset({row[1] for row in columns}):
                raise sqlite3.DatabaseError("Collaboration schema missing columns: " + table)
            unique = set()
            for index in connection.execute("PRAGMA index_list({})".format(table)).fetchall():
                if index[2]:
                    name = str(index[1]).replace('"', '""')
                    unique.add(tuple(row[2] for row in connection.execute(
                        'PRAGMA index_info("{}")'.format(name)
                    ).fetchall()))
            if REQUIRED_UNIQUE[table] not in unique:
                raise sqlite3.DatabaseError("Collaboration unique constraint missing: " + table)
    finally:
        connection.close()
