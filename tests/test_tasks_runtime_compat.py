"""Execute the actual Stage A APIs/SQL, including on Python 3.6 / SQLite 3.7."""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.navigation_badges import _count
from app.tasks import migrations
from app.tasks.repository import TasksRepository


EVIDENCE = {}


class TasksRuntimeCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_uri_mode_ro_escapes_paths_and_rejects_writes(self):
        folder = "space # percent% \u0431\u0430\u0437\u0430"
        if os.name != "nt":
            folder += " question?"
        parent = self.root / folder
        path = parent / "tasks-module.db"
        migrations.migrate_database(path)
        before = path.read_bytes()
        uri = path.resolve().as_uri() + "?mode=ro"
        self.assertIn("%23", uri)
        if os.name != "nt":
            self.assertIn("%3F", uri)
        self.assertIn("%25", uri)
        connection = sqlite3.connect(uri, uri=True, timeout=0.05)
        try:
            EVIDENCE["sqlite_source_id"] = connection.execute("SELECT sqlite_source_id()").fetchone()[0]
            self.assertEqual(connection.execute("SELECT version FROM tasks_module_migrations").fetchone(), (1,))
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("INSERT INTO tasks_module_migrations VALUES(2,'bad','now','test')")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE forbidden(value TEXT)")
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "delete")
        finally:
            connection.close()
        self.assertEqual(TasksRepository(path).status()["schema_version"], 1)
        self.assertEqual(_count(path, "SELECT COUNT(*) FROM tasks_module_migrations", ()), 1)
        self.assertEqual(path.read_bytes(), before)
        EVIDENCE["uri_percent_encoding_readonly_dml_ddl"] = "passed"

    def test_missing_readonly_file_is_never_created(self):
        path = self.root / "tasks-module.db"
        with self.assertRaises(sqlite3.OperationalError):
            TasksRepository(path).status()
        with self.assertRaises(sqlite3.OperationalError):
            _count(path, "SELECT 1", ())
        self.assertFalse(path.exists())
        self.assertEqual(list(self.root.iterdir()), [])
        EVIDENCE["readonly_missing_file"] = "not created"

    def test_offline_cli_uses_actual_interpreter_without_importing_erp(self):
        path = self.root / "tasks-module.db"
        script = Path(__file__).resolve().parents[1] / "scripts" / "migrate_tasks_module.py"
        bootstrap = """
import builtins, runpy, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name in ('app.web', 'app.catalog_db'):
        raise AssertionError('offline migration imported ERP')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
        completed = subprocess.run(
            [sys.executable, "-B", "-c", bootstrap, str(script), "--database", str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"schema_version": 1, "stage": "foundation"})
        self.assertEqual(TasksRepository(path).status()["schema_version"], 1)
        EVIDENCE["offline_cli_without_erp_import"] = "passed"

    def test_migration_exception_rolls_back_ddl_and_can_be_retried(self):
        path = self.root / "tasks-module.db"
        with mock.patch.object(migrations, "validate_connection", side_effect=RuntimeError("injected after DDL")):
            with self.assertRaisesRegex(RuntimeError, "injected after DDL"):
                migrations.migrate_database(path)
        connection = sqlite3.connect(str(path))
        try:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        finally:
            connection.close()
        migrations.migrate_database(path)
        before = path.read_bytes()
        migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)
        EVIDENCE["begin_immediate_ddl_rollback_retry"] = "passed"

    def test_pragmas_indexes_foreign_keys_and_row_factory(self):
        connection = sqlite3.connect(str(self.root / "sql-features.db"))
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone(), (1,))
            connection.execute("PRAGMA busy_timeout=50")
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone(), (50,))
            for unused in range(2):
                connection.execute("CREATE TABLE IF NOT EXISTS parent(id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS value_unique ON parent(value)")
            connection.execute("CREATE TABLE child(parent_id INTEGER REFERENCES parent(id))")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO child(parent_id) VALUES(99)")
            connection.rollback()
            connection.execute("INSERT OR IGNORE INTO parent VALUES(1,'one')")
            connection.commit()
            indexes = connection.execute("PRAGMA index_list(parent)").fetchall()
            self.assertTrue(any(row[1] == "value_unique" and row[2] == 1 for row in indexes))
            self.assertEqual(connection.execute('PRAGMA index_info("value_unique")').fetchone()[2], "value")
            self.assertEqual(connection.execute("PRAGMA table_info(parent)").fetchone()[1], "id")
            connection.row_factory = sqlite3.Row
            self.assertEqual(dict(connection.execute("SELECT * FROM parent").fetchone()), {"id": 1, "value": "one"})
        finally:
            connection.close()
        EVIDENCE["pragma_indexes_fk_row_factory"] = "passed"

    def test_progress_handler_bounds_the_real_badge_query(self):
        path = self.root / "expensive-count.db"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("CREATE TABLE numbers(value INTEGER)")
            connection.executemany("INSERT INTO numbers VALUES(?)", ((i,) for i in range(1000)))
            connection.commit()
        finally:
            connection.close()
        started = time.monotonic()
        with self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
            _count(path, "SELECT COUNT(*) FROM numbers a,numbers b,numbers c", ())
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.0)
        EVIDENCE["progress_handler_elapsed_seconds"] = round(elapsed, 4)

    def test_readonly_wal_with_existing_writer_and_without_writer(self):
        path = self.root / "wal-probe.db"
        writer = sqlite3.connect(str(path))
        try:
            self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
            writer.execute("CREATE TABLE sample(value INTEGER)")
            writer.execute("INSERT INTO sample VALUES(1)")
            writer.commit()
            self.assertEqual(_count(path, "SELECT COUNT(*) FROM sample", ()), 1)
            EVIDENCE["readonly_wal_existing_writer"] = "passed"
        finally:
            writer.close()
        # Old SQLite may require writable sidecars for WAL even with mode=ro.
        # Record the capability; no fallback to writable database access.
        try:
            self.assertEqual(_count(path, "SELECT COUNT(*) FROM sample", ()), 1)
            EVIDENCE["readonly_wal_without_writer"] = "read succeeded"
        except sqlite3.OperationalError:
            EVIDENCE["readonly_wal_without_writer"] = "unavailable; badge handles as local 503"


if __name__ == "__main__":
    unittest.main()
