"""Legacy retirement contracts, using real Flask auth, routes and navigation."""
import os
import re
import runpy
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from app import auth, web
from app.domain_schema_migrations import apply_domain_migrations
from app.sms_migrations import migrate_database as migrate_sms
from app.tasks.migrations import migrate_database


class TasksRetirementApiTest(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(web.app.config)
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.auth_path = root / "auth.db"
        self.tasks_path = root / "tasks.db"
        apply_domain_migrations(self.auth_path, "auth", "test")
        apply_domain_migrations(self.tasks_path, "tasks", "test")
        self.store = auth.AuthStore(self.auth_path)
        now = int(time.time())
        with self.store.connect() as connection:
            self.user_id = connection.execute(
                "INSERT INTO users(first_name,last_name,email,email_normalized,password_hash,role,active,"
                "created_at,email_verified_at,updated_at,session_version) VALUES('Анна','Тест',"
                "'anna@example.test','anna@example.test','hash','employee',1,?,?,?,1)",
                (now, now, now),
            ).lastrowid
            self.owner_id = connection.execute(
                "INSERT INTO users(first_name,last_name,email,email_normalized,password_hash,role,active,"
                "created_at,email_verified_at,updated_at,session_version) VALUES('Олег','Владелец',"
                "'owner@example.test','owner@example.test','hash','employee',1,?,?,?,1)",
                (now, now, now),
            ).lastrowid
            self.outsider_id = connection.execute(
                "INSERT INTO users(first_name,last_name,email,email_normalized,password_hash,role,active,"
                "created_at,email_verified_at,updated_at,session_version) VALUES('Игорь','Посторонний',"
                "'outsider@example.test','outsider@example.test','hash','employee',1,?,?,?,1)",
                (now, now, now),
            ).lastrowid
            self.admin_id = connection.execute(
                "INSERT INTO users(first_name,last_name,email,email_normalized,password_hash,role,active,"
                "created_at,email_verified_at,updated_at,session_version) VALUES('Ада','Админ',"
                "'admin@example.test','admin@example.test','hash','admin',1,?,?,?,1)",
                (now, now, now),
            ).lastrowid
        web.app.config.update(
            TESTING=True, AUTH_TESTING=True, AUTH_DATABASE=str(self.auth_path),
            TASKS_DATABASE=str(self.tasks_path), SESSION_COOKIE_SECURE=False,
        )
        self.client = web.app.test_client()

    def tearDown(self):
        web.app.config.clear()
        web.app.config.update(self.original_config)
        self.temporary.cleanup()

    def login(self, user_id=None):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id or self.user_id
            session["session_version"] = 1
            session["_csrf_token"] = "tasks-csrf"

    def test_authentication_still_precedes_retirement(self):
        self.assertEqual(self.client.get("/app/tasks").status_code, 302)
        self.assertIn("/login", self.client.get("/app/tasks").location)
        for path in ("/api/v1/tasks", "/api/v1/tasks/1", "/api/v1/tasks/badge"):
            self.assertEqual(self.client.get(path).status_code, 401)

    def test_every_legacy_api_is_gone_without_opening_either_task_database(self):
        self.login()
        before = self.tasks_path.read_bytes()
        original = sqlite3.connect
        attempted = []
        def guarded(database, *args, **kwargs):
            if "tasks.db" in str(database) or "tasks-module.db" in str(database):
                attempted.append(str(database))
                raise AssertionError("retired API opened storage")
            return original(database, *args, **kwargs)
        paths = ("", "/1", "/1/complete", "/1/reopen", "/1/status", "/1/move",
                 "/1/reschedule", "/1/calendar-reschedule", "/calendar", "/counts",
                 "/notifications", "/assignees", "/badge", "/entities", "/by-entity")
        for enabled in (False, True):
            web.app.config["TASKS_MODULE_ENABLED"] = enabled
            with patch("sqlite3.connect", side_effect=guarded):
                for path in paths:
                    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                        response = self.client.open("/api/v1/tasks" + path, method=method,
                            json={"title": "Should not be saved"}, headers={"X-CSRF-Token": "tasks-csrf"})
                        self.assertEqual(response.status_code, 410, (method, path, response.get_json()))
                        self.assertEqual(response.get_json()["code"], "LEGACY_TASKS_RETIRED")
        self.assertEqual(attempted, [])
        self.assertEqual(self.tasks_path.read_bytes(), before)

    def test_old_links_drop_all_parameters_even_with_flag_off(self):
        self.login()
        for enabled in (False, True):
            web.app.config["TASKS_MODULE_ENABLED"] = enabled
            for query in ("", "?task=1", "?task_id=1", "?task=1&view=calendar&entity_type=order"):
                response = self.client.get("/app/tasks" + query)
                self.assertEqual(response.status_code, 302)
                destination = urlsplit(response.location)
                self.assertEqual(destination.path, "/app/tasks-module")
                self.assertEqual(destination.query, "")
                self.assertEqual(destination.fragment, "")

    def test_task_responsibility_cannot_reach_shared_database(self):
        self.login()
        with patch.object(web, "_collaboration_store", side_effect=AssertionError("legacy task assignment")) as store:
            for method in ("GET", "POST"):
                response = self.client.open("/api/v1/responsibility/task/1", method=method,
                    json={"responsible_user_id": self.user_id}, headers={"X-CSRF-Token": "tasks-csrf"})
                self.assertEqual(response.status_code, 404)
            store.assert_not_called()

    def test_one_navigation_entry_per_desktop_mobile_and_preferences_preserved(self):
        self.login()
        with patch.object(web, "get_orders", return_value=[]):
            html = self.client.get("/app/orders").get_data(as_text=True)
        for navigation_class in ("sidebar-nav", "mobile-erp-more"):
            # All task anchors use the same stable key, never a second new entry.
            self.assertIn(navigation_class, html)
        anchors = re.findall(r'<a\b[^>]*data-navigation-key="tasks"[^>]*>', html)
        self.assertEqual(len(anchors), 2)  # Desktop and mobile-more, each once.
        for anchor in anchors:
            self.assertIn('href="/app/tasks-module"', anchor)
        self.assertNotIn('href="/app/tasks"', html)
        self.assertNotIn('Новые задачи', html)
        saved = self.client.put("/api/v1/navigation-preferences", json={"order": [item["key"] for item in self.client.get("/api/v1/navigation-preferences").get_json()["data"]], "hidden": ["tasks"]},
            headers={"X-CSRF-Token": "tasks-csrf"})
        self.assertEqual(saved.status_code, 200)
        with patch.object(web, "get_orders", return_value=[]):
            hidden = self.client.get("/app/orders").get_data(as_text=True)
        self.assertNotIn('data-navigation-key="tasks"', hidden)

    def test_shared_inbox_badge_excludes_old_tasks_and_preserves_rows(self):
        self.login()
        with sqlite3.connect(str(self.tasks_path)) as connection:
            for kind, recipient in (("task", self.user_id), ("order", self.user_id), ("repair", self.owner_id)):
                connection.execute("INSERT INTO inbox_events(recipient_user_id,actor_user_id,event_type,"
                    "entity_type,entity_id,created_at,metadata_json,operation_key) VALUES(?,?,'assigned',?,'1','now','{}',?)",
                    (recipient, self.admin_id, kind, kind))
        before = self.tasks_path.read_bytes()
        self.assertEqual(self.client.get("/api/v1/inbox/badge").get_json()["data"]["count"], 1)
        feed = self.client.get("/api/v1/inbox").get_json()["data"]
        self.assertEqual((feed["total"], feed["unread_count"]), (1, 1))
        self.assertEqual(feed["rows"][0]["entity_type"], "order")
        self.assertEqual(self.tasks_path.read_bytes(), before)

    def test_new_namespace_and_crud_are_unaffected_by_legacy_tombstone(self):
        module = Path(self.temporary.name) / "tasks-module.db"
        migrate_database(module)
        # A fresh ERP import must not reuse an SMS fixture altered by other tests.
        sms = Path(self.temporary.name) / "sms.db"
        migrate_sms(sms)
        with patch.dict(os.environ, {"ERP_TASKS_MODULE_ENABLED": "1", "ERP_TASKS_MODULE_DATABASE": str(module),
                "ERP_AUTH_DATABASE": str(self.auth_path), "ERP_TASKS_DATABASE": str(self.tasks_path),
                "ERP_SMS_DATABASE": str(sms)}):
            namespace = runpy.run_path(web.__file__, run_name="tasks_retirement")
        app = namespace["app"]
        app.config.update(TESTING=True, AUTH_TESTING=True, AUTH_ENABLED=True, SESSION_COOKIE_SECURE=False)
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=self.user_id, session_version=1, _csrf_token="tasks-csrf")
        original = sqlite3.connect
        attempted = []
        def guarded(database, *args, **kwargs):
            if "tasks.db" in str(database) or "catalog.db" in str(database):
                attempted.append(str(database))
                raise AssertionError("new CRUD crossed boundary")
            return original(database, *args, **kwargs)
        with patch("sqlite3.connect", side_effect=guarded):
            created = client.post("/api/v1/tasks-module/tasks", json={"title": "New only", "assigned_to": self.user_id},
                                  headers={"X-CSRF-Token": "tasks-csrf"})
            self.assertEqual(created.status_code, 201, created.get_json())
            task = created.get_json()["data"]
            self.assertEqual(client.get("/api/v1/tasks-module/tasks/{}".format(task["id"])).status_code, 200)
            self.assertEqual(client.get("/api/v1/tasks/{}".format(task["id"])).status_code, 410)
        self.assertEqual(attempted, [])
        app.config["TASKS_MODULE_ENABLED"] = False
        self.assertEqual(client.get("/api/v1/tasks-module/tasks").status_code, 503)
        self.assertEqual(client.get("/api/v1/tasks").status_code, 410)

    def test_old_implementation_assets_and_release_manifest_are_absent(self):
        root = Path(web.__file__).parents[1]
        for name in ("app/services/tasks.py", "app/task_errors.py", "app/templates/tasks.html",
                     "app/static/js/tasks.js", "app/static/css/tasks.css", "ops/tasks-release-classification.json"):
            self.assertFalse((root / name).exists(), name)
        for name in ("app/domain_schema_migrations.py", "app/collaboration_schema.py", "ops/recovery-schema-contract.json"):
            self.assertTrue((root / name).exists(), name)


if __name__ == "__main__":
    unittest.main()
