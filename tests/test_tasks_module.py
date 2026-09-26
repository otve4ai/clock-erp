"""Optional foundation, including fail-safe registration and offline migrations."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask

from app.tasks_boundary import register_tasks_module
from app.tasks.migrations import migrate_database
from app.tasks.repository import TasksRepository


class TasksModuleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks-module.db"
        self.user = {"id": 1, "role": "admin"}

    def tearDown(self):
        self.temp.cleanup()

    def application(self, enabled=True):
        app = Flask(__name__)
        app.config.update(TESTING=True, TASKS_MODULE_ENABLED=enabled, TASKS_MODULE_DATABASE=str(self.path))
        app.add_url_rule("/erp-probe", "erp_probe", lambda: "ERP works")
        register_tasks_module(app, self.path.parent, lambda: self.user)
        return app

    def test_disabled_module_does_not_import_or_initialize_storage(self):
        with mock.patch("app.tasks_boundary.importlib.import_module", side_effect=AssertionError("must not import")) as loader:
            app = self.application(False)
        loader.assert_not_called()
        self.assertEqual(app.test_client().get("/erp-probe").status_code, 200)
        self.assertEqual(app.test_client().get("/api/v1/tasks-module/status").status_code, 404)
        self.assertFalse(self.path.exists())

    def test_a_b_missing_or_corrupt_storage_only_disables_own_endpoint(self):
        app = self.application()
        client = app.test_client()
        self.assertEqual(client.get("/api/v1/tasks-module/status").status_code, 503)
        self.assertFalse(self.path.exists())
        self.path.write_bytes(b"corrupt")
        self.assertEqual(client.get("/api/v1/tasks-module/status").status_code, 503)
        self.assertEqual(client.get("/erp-probe").status_code, 200)
        self.assertEqual(self.path.read_bytes(), b"corrupt")

    def test_c_repository_exception_and_switch_after_registration_are_local(self):
        app = self.application()
        with mock.patch.object(TasksRepository, "status", side_effect=RuntimeError("failed")):
            self.assertEqual(app.test_client().get("/api/v1/tasks-module/status").status_code, 503)
        app.config["TASKS_MODULE_ENABLED"] = False
        with mock.patch.object(TasksRepository, "status") as repository:
            self.assertEqual(app.test_client().get("/api/v1/tasks-module/status").status_code, 503)
        repository.assert_not_called()
        self.assertEqual(app.test_client().get("/erp-probe").status_code, 200)

    def test_d_partial_registration_failure_keeps_registered_optional_views_closed(self):
        original = Flask.register_blueprint

        def fail_after_registration(app, blueprint, **kwargs):
            original(app, blueprint, **kwargs)
            raise RuntimeError("injected registration failure")

        with mock.patch.object(Flask, "register_blueprint", fail_after_registration):
            app = self.application()
        self.assertFalse(app.extensions["tasks_module"]["registered"])
        self.assertEqual(app.test_client().get("/erp-probe").status_code, 200)
        with mock.patch.object(TasksRepository, "status") as repository:
            self.assertEqual(app.test_client().get("/api/v1/tasks-module/status").status_code, 503)
        repository.assert_not_called()

    def test_i_module_bootstrap_and_operations_open_only_their_own_database(self):
        original = sqlite3.connect
        opened = []
        denied = []

        def authorize(action, first, second, database, trigger):
            if action == sqlite3.SQLITE_ATTACH:
                denied.append(first)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        def guard(database, *args, **kwargs):
            opened.append(str(database))
            if "tasks-module.db" not in str(database):
                raise AssertionError("Tasks operation opened another database")
            connection = original(database, *args, **kwargs)
            connection.set_authorizer(authorize)
            return connection

        with mock.patch("sqlite3.connect", side_effect=guard):
            migrate_database(self.path, "test")
            before = self.path.read_bytes()
            migrate_database(self.path, "test")
            self.assertEqual(self.path.read_bytes(), before)
            app = self.application()
            response = app.test_client().get("/api/v1/tasks-module/status")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["data"], {"schema_version": 4, "stage": "microtasks"})
            self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(opened)
        self.assertEqual(denied, [])

    def test_permissions_deny_unauthenticated_and_non_admin_before_storage_access(self):
        app = self.application()
        with mock.patch.object(TasksRepository, "status") as repository:
            self.user = {}
            self.assertEqual(app.test_client().get("/api/v1/tasks-module/status").status_code, 401)
            self.user = {"id": 2, "role": "employee"}
            self.assertEqual(app.test_client().get("/api/v1/tasks-module/status").status_code, 403)
        repository.assert_not_called()

    def test_unknown_database_is_rejected_without_repair_or_data_loss(self):
        connection = sqlite3.connect(str(self.path))
        connection.execute("CREATE TABLE unknown_data(value TEXT)")
        connection.execute("INSERT INTO unknown_data VALUES('preserve')")
        connection.commit()
        connection.close()
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            migrate_database(self.path)
        with self.assertRaises(ValueError):
            TasksRepository(self.path).status()
        self.assertEqual(self.path.read_bytes(), before)
        for name in ("catalog.db", "tasks.db", "orders.db"):
            with self.assertRaises(ValueError):
                migrate_database(self.path.parent / name)
            self.assertFalse((self.path.parent / name).exists())

    def test_sql_compatibility_gate_includes_new_module_migrations(self):
        from app.schema_migrations import MigrationError, validate_known_sql_compatibility

        source = self.path.parent / "source"
        (source / "app" / "tasks").mkdir(parents=True)
        (source / "scripts").mkdir()
        (source / "app" / "catalog_db.py").write_text("", encoding="utf-8")
        (source / "app" / "tasks" / "migrations.py").write_text(
            "SQL = 'INSERT INTO tasks VALUES(1) RETURNING id'\n", encoding="utf-8")
        with self.assertRaisesRegex(MigrationError, "RETURNING"):
            validate_known_sql_compatibility(source)


if __name__ == "__main__":
    unittest.main()
