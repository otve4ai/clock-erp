"""Stage A failure injection against real ERP handlers and rendered templates."""

import builtins
import os
import runpy
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from app import web
from app.domain_schema_migrations import apply_domain_migrations
from app.services.collaboration import CollaborationStore
from app.services.tasks import TaskStore
from app.sms_migrations import migrate_database as migrate_sms
from app.tasks.migrations import migrate_database
from app.tasks.repository import TasksRepository
from app.tasks.schema import CORE_DDL, LEDGER_DDL


class TasksIsolationTest(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(web.app.config)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth.db"
        self.legacy = self.root / "tasks.db"
        self.module = self.root / "tasks-module.db"
        # Startup must not depend on the shared SMS fixture left by other tests.
        self.sms = self.root / "sms.db"
        migrate_sms(self.sms)
        environment = mock.patch.dict(os.environ, {"ERP_SMS_DATABASE": str(self.sms)})
        environment.start()
        self.addCleanup(environment.stop)
        apply_domain_migrations(self.auth, "auth", "isolation-test")
        apply_domain_migrations(self.legacy, "tasks", "isolation-test")
        connection = sqlite3.connect(str(self.auth))
        try:
            now = int(time.time())
            for user_id in (1, 2):
                connection.execute(
                    "INSERT INTO users(id,first_name,last_name,email,email_normalized,password_hash,"
                    "role,active,created_at,email_verified_at,updated_at,session_version) "
                    "VALUES(?,?,'',?,?,'hash','admin',1,?,?,?,1)",
                    (user_id, "Test", "{}@example.test".format(user_id),
                     "{}@example.test".format(user_id), now, now, now),
                )
            connection.commit()
        finally:
            connection.close()
        web.app.config.update(TESTING=True, AUTH_TESTING=True, AUTH_ENABLED=True,
            AUTH_DATABASE=str(self.auth), TASKS_DATABASE=str(self.legacy),
            TASKS_MODULE_DATABASE=str(self.module), TASKS_MODULE_ENABLED=False,
            SESSION_COOKIE_SECURE=False)
        self.client = web.app.test_client()
        self.login(self.client)
        # External integrations are not part of this test. Do not mock routes,
        # templates, sidebar, auth or the catalog services under examination.
        self.orders = mock.patch.object(web, "get_orders", return_value=[])
        self.orders.start()

    def tearDown(self):
        self.orders.stop()
        web.app.config.clear()
        web.app.config.update(self.original_config)
        self.temp.cleanup()

    @staticmethod
    def login(client):
        with client.session_transaction() as session:
            session["user_id"] = 1
            session["session_version"] = 1
            session["_csrf_token"] = "isolation-csrf"

    def render_core(self, client=None):
        client = client or self.client
        for url in ("/app/orders", "/app/products", "/app/sales", "/app/receipts", "/app/inventory"):
            with self.subTest(url=url):
                response = client.get(url)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True)[:700])
                self.assertIn('class="sidebar-nav"', response.get_data(as_text=True))

    def reject_task_connections(self):
        calls = []
        original = sqlite3.connect

        def guarded(database, *args, **kwargs):
            if "tasks.db" in str(database) or "tasks-module.db" in str(database):
                calls.append(str(database))
                raise AssertionError("ERP request attempted to open Tasks storage")
            return original(database, *args, **kwargs)

        return calls, mock.patch("sqlite3.connect", side_effect=guarded)

    def test_a_g_missing_module_and_normal_renders_never_open_either_tasks_database(self):
        self.assertFalse(self.module.exists())
        calls, guard = self.reject_task_connections()
        with guard:
            self.render_core()
        self.assertEqual(calls, [])
        self.assertFalse(self.module.exists())

    def test_b_corrupt_new_and_legacy_storage_does_not_break_core_pages(self):
        self.module.write_bytes(b"not a SQLite database")
        self.legacy.write_bytes(b"not a SQLite database")
        self.render_core()
        self.assertEqual(self.module.read_bytes(), b"not a SQLite database")

    def test_c_store_exceptions_do_not_reach_core_routes(self):
        with mock.patch.object(web, "TaskStore", side_effect=RuntimeError("legacy store failed")) as legacy, \
                mock.patch.object(TasksRepository, "status", side_effect=RuntimeError("module failed")) as repository, \
                mock.patch.object(CollaborationStore, "connect", side_effect=RuntimeError("shared DB failed")) as shared:
            self.render_core()
        legacy.assert_not_called()
        repository.assert_not_called()
        shared.assert_not_called()

    def test_d_real_flask_startup_survives_optional_module_import_failure(self):
        original_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name in {"app.services.tasks", "app.task_errors"} or name.startswith("app.tasks."):
                raise ImportError("injected Tasks import failure")
            return original_import(name, *args, **kwargs)

        with mock.patch.dict(os.environ, {
                "ERP_TASKS_MODULE_ENABLED": "1", "ERP_TASKS_MODULE_DATABASE": str(self.module),
                "ERP_AUTH_DATABASE": str(self.auth), "ERP_TASKS_DATABASE": str(self.legacy)}), \
                mock.patch("builtins.__import__", side_effect=guarded_import), \
                mock.patch("app.tasks_boundary.importlib.import_module", side_effect=ImportError("injected route import failure")):
            namespace = runpy.run_path(web.__file__, run_name="tasks_isolation_startup")
        isolated = namespace["app"]
        isolated.config.update(TESTING=True, AUTH_TESTING=True, AUTH_ENABLED=True, SESSION_COOKIE_SECURE=False)
        self.assertFalse(isolated.extensions["tasks_module"]["registered"])
        client = isolated.test_client()
        self.login(client)
        with mock.patch.dict(namespace["orders_page"].__globals__, {"get_orders": lambda *a, **k: []}):
            self.render_core(client)
        self.assertEqual(client.get("/api/v1/tasks-module/status").status_code, 404)

    def test_e_badge_failure_is_local_to_its_request(self):
        with mock.patch("app.navigation_badges._count", side_effect=RuntimeError("badge failed")):
            response = self.client.get("/api/v1/tasks/badge")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["code"], "BADGE_UNAVAILABLE")
            self.render_core()

    def test_b1_schema_and_http_failures_remain_local_to_enabled_tasks(self):
        connection = sqlite3.connect(str(self.module))
        try:
            connection.execute(LEDGER_DDL)
            for statement in CORE_DDL:
                connection.execute(statement.replace("status TEXT NOT NULL", "status TEXT"))
            connection.executemany("INSERT INTO tasks_module_migrations VALUES(?,?,'now','fixture')",
                                   ((1, "tasks-module-foundation-v1"), (2, "tasks-module-core-v2")))
            connection.commit()
        finally:
            connection.close()
        before = self.module.read_bytes()
        with mock.patch.dict(os.environ, {"ERP_TASKS_MODULE_ENABLED": "1", "ERP_TASKS_MODULE_DATABASE": str(self.module),
                                          "ERP_AUTH_DATABASE": str(self.auth), "ERP_TASKS_DATABASE": str(self.legacy)}):
            namespace = runpy.run_path(web.__file__, run_name="tasks_b1_isolation")
        app = namespace["app"]
        app.config.update(TESTING=True, AUTH_TESTING=True, AUTH_ENABLED=True, SESSION_COOKIE_SECURE=False)
        client = app.test_client()
        self.login(client)
        headers = {"X-CSRF-Token": "isolation-csrf"}
        response = client.get("/api/v1/tasks-module/status")
        self.assertEqual((response.status_code, response.get_json()["code"]), (503, "TASKS_MODULE_UNAVAILABLE"))
        self.assertEqual(self.module.read_bytes(), before)
        response = client.post("/api/v1/tasks-module/tasks", data="{broken", content_type="application/json", headers=headers)
        self.assertEqual((response.status_code, response.get_json()["code"]), (400, "HTTP_ERROR"))
        app.config["MAX_CONTENT_LENGTH"] = 10
        response = client.post("/api/v1/tasks-module/tasks", json={"title": "x" * 100}, headers=headers)
        self.assertEqual((response.status_code, response.get_json()["code"]), (413, "HTTP_ERROR"))
        calls, guard = self.reject_task_connections()
        with guard, mock.patch.dict(namespace["orders_page"].__globals__, {"get_orders": lambda *a, **k: []}):
            self.render_core(client)
        self.assertEqual(calls, [])
        self.assertEqual(self.module.read_bytes(), before)

    def test_startup_never_runs_migration_even_when_its_code_would_raise(self):
        original_import = builtins.__import__
        migration_module = sys.modules[migrate_database.__module__]
        for enabled in ("0", "1"):
            for invalid_schema in (False, True):
                with self.subTest(enabled=enabled, invalid_schema=invalid_schema):
                    if self.module.exists():
                        self.module.unlink()
                    if invalid_schema:
                        connection = sqlite3.connect(str(self.module))
                        connection.execute("CREATE TABLE unrelated(value TEXT)")
                        connection.commit()
                        connection.close()
                    before = self.module.read_bytes() if invalid_schema else None
                    attempted = []

                    def guarded_import(name, *args, **kwargs):
                        if name == "app.tasks.migrations" or (enabled == "0" and name.startswith("app.tasks.")):
                            attempted.append(name)
                            raise ImportError("injected optional module/migration import failure")
                        return original_import(name, *args, **kwargs)

                    with mock.patch.dict(os.environ, {
                            "ERP_TASKS_MODULE_ENABLED": enabled,
                            "ERP_TASKS_MODULE_DATABASE": str(self.module),
                            "ERP_AUTH_DATABASE": str(self.auth), "ERP_TASKS_DATABASE": str(self.legacy)}), \
                            mock.patch("builtins.__import__", side_effect=guarded_import), \
                            mock.patch.object(migration_module, "migrate_database", side_effect=RuntimeError("migration failed")) as migration:
                        calls, guard = self.reject_task_connections()
                        with guard:
                            namespace = runpy.run_path(web.__file__, run_name="tasks_runtime_startup")
                            isolated = namespace["app"]
                            isolated.config.update(TESTING=True, AUTH_TESTING=True,
                                AUTH_ENABLED=True, SESSION_COOKIE_SECURE=False)
                            client = isolated.test_client()
                            self.login(client)
                            with mock.patch.dict(namespace["orders_page"].__globals__, {"get_orders": lambda *a, **k: []}):
                                self.render_core(client)
                        self.assertEqual(calls, [])
                        self.assertEqual(attempted, [])
                        migration.assert_not_called()
                        response = client.get("/api/v1/tasks-module/status")
                        self.assertEqual(response.status_code, 404 if enabled == "0" else 503)
                    if invalid_schema:
                        self.assertEqual(self.module.read_bytes(), before)
                    else:
                        self.assertFalse(self.module.exists())

    @unittest.skipUnless(sys.platform == "linux", "requires native Linux cross-process SQLite locks")
    def test_linux_separate_process_locks_do_not_delay_concurrent_orders_products(self):
        migrate_database(self.module)
        worker = """
import signal, sqlite3, sys
signal.alarm(20)
connections = [sqlite3.connect(path) for path in sys.argv[1:3]]
try:
    for connection in connections:
        connection.execute('BEGIN ' + sys.argv[3])
    print('LOCKED', flush=True)
    sys.stdin.readline()
finally:
    for connection in connections:
        connection.rollback()
        connection.close()
"""
        for mode in ("IMMEDIATE", "EXCLUSIVE"):
            with self.subTest(lock=mode):
                process = subprocess.Popen(
                    [sys.executable, "-B", "-c", worker, str(self.legacy), str(self.module), mode],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    universal_newlines=True,
                )
                try:
                    self.assertEqual(process.stdout.readline().strip(), "LOCKED")
                    original = sqlite3.connect
                    task_opens = []

                    def observed(database, *args, **kwargs):
                        if "tasks.db" in str(database) or "tasks-module.db" in str(database):
                            task_opens.append(str(database))
                        return original(database, *args, **kwargs)

                    clients = [web.app.test_client(), web.app.test_client()]
                    for client in clients:
                        self.login(client)
                    started = time.monotonic()
                    with mock.patch("sqlite3.connect", side_effect=observed), ThreadPoolExecutor(max_workers=2) as pool:
                        futures = [pool.submit(client.get, url) for client, url in zip(
                            clients, ("/app/orders", "/app/products"))]
                        responses = [future.result(timeout=5) for future in futures]
                    elapsed = time.monotonic() - started
                    self.assertEqual([response.status_code for response in responses], [200, 200])
                    self.assertEqual(task_opens, [])
                    self.assertLess(elapsed, 3.0)
                    self.assertIsNone(process.poll(), "lock owner must remain alive until after requests")
                    if mode == "EXCLUSIVE":
                        started = time.monotonic()
                        self.assertEqual(self.client.get("/api/v1/tasks/badge").status_code, 503)
                        self.assertLess(time.monotonic() - started, 1.0)
                    print("LINUX_TASKS_LOCK {}: Orders+Products {:.3f}s; task opens=0".format(mode, elapsed))
                finally:
                    if process.poll() is None:
                        process.communicate("release\n", timeout=5)
                    else:
                        process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)

    def test_f_locked_databases_are_not_opened_by_core_and_badge_fails_fast(self):
        migrate_database(self.module)
        locks = [sqlite3.connect(str(path)) for path in (self.legacy, self.module)]
        try:
            for connection in locks:
                connection.execute("BEGIN EXCLUSIVE")
            calls, guard = self.reject_task_connections()
            with guard:
                self.render_core()
            self.assertEqual(calls, [])
            started = time.monotonic()
            self.assertEqual(self.client.get("/api/v1/tasks/badge").status_code, 503)
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            for connection in locks:
                connection.rollback()
                connection.close()

    def test_badges_are_personal_read_only_and_do_not_generate_notifications(self):
        task, unused = TaskStore(self.legacy).create(
            {"title": "Badge fixture", "assignee_id": 1, "due_date": "2020-01-01"},
            1, lambda user_id: True)
        before = self.legacy.read_bytes()
        with mock.patch.object(TaskStore, "generate_notifications", side_effect=AssertionError("badge must not write")):
            response = self.client.get("/api/v1/tasks/badge")
            self.assertEqual(response.get_json()["data"], {"count": 1})
            self.assertEqual(self.client.get("/api/v1/inbox/badge").get_json()["data"], {"count": 0})
        self.assertEqual(self.legacy.read_bytes(), before)
        anonymous = web.app.test_client()
        self.assertEqual(anonymous.get("/api/v1/tasks/badge").status_code, 401)

    def test_h_real_catalog_and_orders_writes_do_not_open_module_storage(self):
        from app.catalog_db import CatalogDatabase
        from app.services.excel_product_catalog import ExcelProductCatalog
        from app.services.orders_snapshot import OrdersSnapshotStore

        migrate_database(self.module)
        before = self.module.read_bytes()
        orders_path = self.root / "orders.db"
        apply_domain_migrations(orders_path, "orders", "isolation-test")
        calls, guard = self.reject_task_connections()
        with guard:
            with CatalogDatabase().transaction() as connection:
                connection.execute(
                    "INSERT INTO catalog_excel_batches (id,file_sha256,source_filename,row_count,"
                    "total_stock,positive_rows,zero_rows,status,created_at,applied_at) "
                    "VALUES (?,?, 'isolation.xlsx',0,0,0,0,'active',?,?)",
                    (self.root.name, self.root.name, "2026-09-26", "2026-09-26"),
                )
            product = ExcelProductCatalog().create_product(
                "Isolation fixture " + self.root.name, article="ISO-" + self.root.name,
                brand="Isolation", category="Часы", stock=2,
            )
            self.assertEqual(ExcelProductCatalog().get_product(product["id"])["stock"], 2)
            headers = {"X-CSRF-Token": "isolation-csrf"}
            updated = self.client.patch("/api/v1/products/{}".format(product["id"]),
                json={"name": "Updated isolation " + self.root.name}, headers=headers)
            self.assertEqual(updated.status_code, 200, updated.get_json())
            sale = self.client.post("/api/sales", json={
                "created_at": "2026-09-26", "source": "Tictactoy",
                "product_id": str(product["id"]), "quantity": 1,
                "unit_price": 1000, "order_number": "isolation-" + self.root.name,
            }, headers=headers)
            self.assertEqual(sale.status_code, 201, sale.get_json())
            self.assertEqual(ExcelProductCatalog().get_product(product["id"])["stock"], 1)
            store = OrdersSnapshotStore(orders_path)
            store.replace([{"id": "isolation-order", "number": "test", "status": "N"}], 100)
            self.assertIsNotNone(store.get("isolation-order"))
            self.render_core()
        self.assertEqual(calls, [])
        self.assertEqual(self.module.read_bytes(), before)

    def test_j_shared_assignments_work_without_legacy_task_tables(self):
        connection = sqlite3.connect(str(self.legacy))
        try:
            for table in ("tasks", "task_links", "task_history", "task_notifications"):
                connection.execute("DROP TABLE " + table)
            connection.commit()
        finally:
            connection.close()
        store = web._collaboration_store()
        actor = {"id": 1, "first_name": "Test"}
        for entity_type in ("order", "customer", "purchase", "repair"):
            with self.subTest(entity_type=entity_type):
                assignment, created = store.assign(entity_type, "isolation", 2, actor,
                    "Fixture", operation_key="isolation-" + entity_type)
                self.assertTrue(created)
                self.assertEqual(assignment["assignment"]["responsible_user_id"], 2)
                self.assertEqual(store.assigned_entity_ids(entity_type, 2), {"isolation"})
        self.assertEqual(store.unread_count(2), 4)
        with mock.patch.object(web, "_collaboration_entity", return_value={"label": "Fixture", "href": ""}):
            response = self.client.post("/api/v1/responsibility/order/http-fixture",
                json={"responsible_user_id": 2}, headers={"X-CSRF-Token": "isolation-csrf"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.unread_count(2), 5)

    def test_collaboration_missing_own_columns_fails_without_schema_repair(self):
        connection = sqlite3.connect(str(self.legacy))
        connection.execute("DROP TABLE inbox_events")
        connection.commit()
        connection.close()
        before = self.legacy.read_bytes()
        with self.assertRaises(sqlite3.DatabaseError):
            web._collaboration_store().unread_count(1)
        self.assertEqual(self.legacy.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
