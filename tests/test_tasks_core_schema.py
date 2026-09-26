"""B.1: reject actual schema drift without repairing or writing the database."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.tasks import migrations
from app.tasks.repository import FOUNDATION_SIGNATURE, CORE_SIGNATURE, SCHEMA_SIGNATURE, TasksRepository
from app.tasks.schema import LEDGER_DDL


class TasksCoreSchemaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def drifted_database(self, before, after, table=0):
        path = self.root / str(len(list(self.root.iterdir()))) / "tasks-module.db"
        path.parent.mkdir()
        ddl = list(migrations.CORE_DDL)
        self.assertIn(before, ddl[table])
        ddl[table] = ddl[table].replace(before, after, 1)
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(LEDGER_DDL)
            for statement in ddl:
                connection.execute(statement)
            connection.executemany("INSERT INTO tasks_module_migrations VALUES(?,?,'now','fixture')",
                                   ((1, FOUNDATION_SIGNATURE), (2, CORE_SIGNATURE), (3, SCHEMA_SIGNATURE)))
            connection.commit()
        finally:
            connection.close()
        return path

    def rejected(self, before, after, table=0):
        path = self.drifted_database(before, after, table)
        original = path.read_bytes()
        with self.assertRaises(ValueError):
            TasksRepository(path).status()
        self.assertEqual(path.read_bytes(), original)
        with self.assertRaises(ValueError):
            migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), original)

    def test_missing_status_not_null_is_rejected(self):
        self.rejected("status TEXT NOT NULL", "status TEXT")

    def test_changed_critical_column_types_are_rejected(self):
        for name in ("version", "created_by", "assigned_to"):
            with self.subTest(column=name):
                self.rejected(name + " INTEGER", name + " TEXT")
        for name in ("status", "priority", "task_type", "title", "created_at", "updated_at",
                     "deleted_at", "completed_at", "deadline_date"):
            with self.subTest(column=name):
                self.rejected(name + " TEXT", name + " INTEGER")

    def test_changed_defaults_are_rejected(self):
        for before, after in (("DEFAULT 'new'", "DEFAULT 'waiting'"),
                              ("DEFAULT 'normal'", "DEFAULT 'micro'"),
                              ("DEFAULT 1", "DEFAULT 2")):
            with self.subTest(default=before):
                self.rejected(before, after)

    def test_missing_and_weakened_checks_are_rejected(self):
        for check in ("CHECK(task_type='normal')", "CHECK(version>0)", "CHECK(created_by>0)",
                      "CHECK(assigned_to>0)", "CHECK(length(trim(title)) BETWEEN 1 AND 500)",
                      "CHECK(status IN ('new','in_progress','waiting','done'))",
                      "CHECK(priority IN ('low','normal','high'))"):
            with self.subTest(check=check):
                self.rejected(check, "")
        self.rejected("CHECK(version>0)", "CHECK(version>0 OR 1=1)")
        self.rejected("status!='done' AND completed_at IS NULL", "status!='done'")
        self.rejected("length(deadline_date)=10", "length(deadline_date)>=0")

    def test_missing_primary_key_is_rejected(self):
        for table in (0, 1):
            with self.subTest(table=table):
                self.rejected("id INTEGER PRIMARY KEY", "id INTEGER", table)

    def test_missing_and_changed_foreign_key_are_rejected(self):
        self.rejected(" REFERENCES tasks(id)", "", table=1)
        self.rejected("REFERENCES tasks(id)", "REFERENCES tasks(id) ON DELETE CASCADE", table=1)

    def test_activity_constraints_are_checked(self):
        self.rejected("payload TEXT NOT NULL", "payload TEXT", table=1)
        self.rejected("CHECK(task_version>0)", "", table=1)

    def test_correct_schema_and_repeat_migration_remain_read_only(self):
        path = self.root / "tasks-module.db"
        migrations.migrate_database(path)
        original = path.read_bytes()
        self.assertEqual(TasksRepository(path).status()["schema_version"], 3)
        migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), original)

    def test_whitespace_is_ignored_but_literal_case_is_not(self):
        path = self.drifted_database("CREATE TABLE tasks (", "create table tasks\n(\n")
        self.assertEqual(TasksRepository(path).status()["schema_version"], 3)
        self.rejected("task_type='normal'", "task_type='NORMAL'")


if __name__ == "__main__":
    unittest.main()
