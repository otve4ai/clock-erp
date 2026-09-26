"""HTTP contract, real auth adapter and ERP isolation for the new namespace."""

import builtins
import io
import os
import runpy
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from flask import Flask, request
from werkzeug.exceptions import BadRequest, Unauthorized, Forbidden, NotFound, RequestEntityTooLarge

from app.tasks.migrations import migrate_database
from app.tasks.repository import TaskSession, TasksRepository
from app.tasks_boundary import register_tasks_module


BASE = "/api/v1/tasks-module/tasks"


class TasksCoreApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "tasks-module.db"
        migrate_database(self.path)
        self.users = {i: {"id": i, "active": 1, "role": "admin" if i == 4 else "employee"} for i in range(1, 5)}
        self.users[5] = {"id": 5, "active": 0}
        self.actor = self.users[1]
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, TASKS_MODULE_ENABLED=True, TASKS_MODULE_DATABASE=str(self.path))
        self.assertTrue(register_tasks_module(self.app, self.root, lambda: self.actor, self.users.get,
                                             lambda: request.headers.get("X-CSRF-Token") == "test-csrf"))
        self.client = self.app.test_client()
        self.headers = {"X-CSRF-Token": "test-csrf"}

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **values):
        payload = {"title": "Core API", "assigned_to": 2}
        payload.update(values)
        response = self.client.post(BASE, json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["data"]

    def test_endpoints_lifecycle_and_version_conflict(self):
        task = self.create()
        url = BASE + "/" + str(task["id"])
        self.assertEqual(self.client.get(url).get_json()["data"], task)
        edited = self.client.patch(url, json={"version": 1, "title": "Edited"}, headers=self.headers)
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(edited.get_json()["data"]["version"], 2)
        stale = self.client.patch(url, json={"version": 1, "description": "Stale"}, headers=self.headers)
        self.assertEqual((stale.status_code, stale.get_json()["code"]), (409, "VERSION_CONFLICT"))
        for operation, version, extra in (("status", 2, {"status": "done"}), ("delete", 3, {}), ("restore", 4, {})):
            response = self.client.post(url + "/" + operation, json=dict(version=version, **extra), headers=self.headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()["data"]["version"], version + 1)
        self.assertEqual(self.client.get(url + "/activity").status_code, 200)
        self.assertEqual(self.client.get(BASE + "?scope=created").get_json()["data"]["total"], 0)
        self.assertEqual(self.client.get(BASE + "?scope=created&view=archive").get_json()["data"]["total"], 1)
        self.assertEqual(self.client.get(BASE + "/summary?scope=created").get_json()["data"]["done"], 1)
        self.assertEqual(self.client.delete(url, headers=self.headers).status_code, 405)

    def test_actor_cannot_be_spoofed_in_post_or_patch(self):
        response = self.client.post(BASE, json={"title": "Spoof", "created_by": 3}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        task = self.create()
        response = self.client.patch(BASE + "/" + str(task["id"]), json={"version": 1, "created_by": 3}, headers=self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(task["created_by"], 1)

    def test_invalid_assignee_rejected_over_http(self):
        for assigned_to in (5, 99, None, True, "2"):
            response = self.client.post(BASE, json={"title": "Invalid", "assigned_to": assigned_to}, headers=self.headers)
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.get_json()["code"], "VALIDATION_ERROR")

    def test_idor_all_http_paths_including_deleted_and_history(self):
        task = self.create()
        url = BASE + "/" + str(task["id"])
        self.actor = self.users[3]
        for method, path, payload in (("get", url, None), ("get", url + "/activity", None),
                                      ("patch", url, {"version": 1, "title": "Secret"}),
                                      ("post", url + "/status", {"version": 1, "status": "done"}),
                                      ("post", url + "/delete", {"version": 1}),
                                      ("post", url + "/restore", {"version": 1})):
            response = getattr(self.client, method)(path, json=payload, headers=self.headers)
            self.assertEqual((response.status_code, response.get_json()["code"]), (404, "TASK_NOT_FOUND"))
            self.assertNotIn("Core API", response.get_data(as_text=True))
        self.actor = self.users[1]
        self.client.post(url + "/delete", json={"version": 1}, headers=self.headers)
        self.actor = self.users[3]
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(url + "/activity").status_code, 404)

    def test_http_list_search_count_hidden_and_admin_scope(self):
        self.create(description="Hidden text")
        self.actor = self.users[3]
        for suffix in ("?search=Hidden", "?scope=created", "/summary", "/summary?search=Hidden"):
            self.assertEqual(self.client.get(BASE + suffix).get_json()["data"]["total"], 0)
        self.assertEqual(self.client.get(BASE + "?scope=all").get_json()["data"]["total"], 0)
        self.assertEqual(self.client.get(BASE + "/summary?scope=all").get_json()["data"]["total"], 0)
        self.actor = self.users[4]
        self.assertEqual(self.client.get(BASE + "?scope=all").get_json()["data"]["total"], 1)

    def test_assignee_work_content_allowed_reassign_and_delete_forbidden(self):
        task = self.create()
        url = BASE + "/" + str(task["id"])
        self.actor = self.users[2]
        self.assertEqual(self.client.patch(url, json={"version": 1, "description": "Working"}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.patch(url, json={"version": 2, "assigned_to": 3}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post(url + "/delete", json={"version": 2}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post(url + "/status", json={"version": 2, "status": "done"}, headers=self.headers).status_code, 200)

    def test_invalid_json_filters_and_missing_versions(self):
        task = self.create()
        url = BASE + "/" + str(task["id"])
        for content in ("[]", "null", '"string"'):
            response = self.client.post(BASE, data=content, content_type="application/json", headers=self.headers)
            self.assertEqual(response.status_code, 422)
        for suffix in ("?status=bad", "?scope=unknown", "?today=yes", "?status=new&status=done", "?include_deleted=true"):
            self.assertEqual(self.client.get(BASE + suffix).status_code, 422)
        for operation in ("status", "delete", "restore"):
            self.assertEqual(self.client.post(url + "/" + operation, json={}, headers=self.headers).status_code, 422)

    def test_csrf_and_authentication_are_required_before_storage(self):
        with mock.patch.object(TasksRepository, "transaction", side_effect=AssertionError("must not open")) as transaction:
            self.assertEqual(self.client.post(BASE, json={"title": "No CSRF"}).status_code, 403)
            self.actor = None
            for path in (BASE, BASE + "/1", BASE + "/1/activity", BASE + "/summary"):
                self.assertEqual(self.client.get(path).status_code, 401)
            self.assertEqual(self.client.post(BASE, json={"title": "No auth"}, headers=self.headers).status_code, 401)
        transaction.assert_not_called()

    def test_malformed_or_truncated_body_is_400_without_storage_access(self):
        with mock.patch.object(TasksRepository, "transaction", side_effect=AssertionError("body opened storage")) as transaction:
            malformed = self.client.post(BASE, data="{broken", content_type="application/json", headers=self.headers)
            truncated = self.client.open(BASE, method="POST", headers=self.headers, environ_overrides={
                "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "100", "wsgi.input": io.BytesIO(b"{}")})
            for response in (malformed, truncated):
                self.assertEqual((response.status_code, response.get_json()["code"]), (400, "HTTP_ERROR"))
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        transaction.assert_not_called()

    def test_configured_request_body_limit_is_413_without_storage_access(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 10
        with mock.patch.object(TasksRepository, "transaction", side_effect=AssertionError("body opened storage")) as transaction:
            response = self.client.post(BASE, json={"title": "x" * 100}, headers=self.headers)
        self.assertEqual((response.status_code, response.get_json()["code"]), (413, "HTTP_ERROR"))
        transaction.assert_not_called()

    def test_body_limit_also_bounds_stream_without_content_length(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 10
        stream = io.BytesIO(b'{"title":"' + b"x" * 100 + b'"}')
        response = self.client.open(BASE, method="POST", headers=self.headers, environ_overrides={
            "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "", "wsgi.input": stream,
            "wsgi.input_terminated": True})
        self.assertEqual((response.status_code, response.get_json()["code"]), (413, "HTTP_ERROR"))
        self.assertLessEqual(stream.tell(), 11)

    def test_json_within_configured_limit_preserves_normal_validation(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 1000
        self.assertEqual(self.create(title="Valid body")["title"], "Valid body")
        response = self.client.post(BASE, data="{broken", content_type="application/json", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post(BASE, json=[], headers=self.headers)
        self.assertEqual(response.status_code, 422)

    def test_stream_exactly_at_body_limit_is_accepted(self):
        raw = b'{"title":"at limit"}'
        self.app.config["MAX_CONTENT_LENGTH"] = len(raw)
        response = self.client.open(BASE, method="POST", headers=self.headers, environ_overrides={
            "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "", "wsgi.input": io.BytesIO(raw),
            "wsgi.input_terminated": True})
        self.assertEqual(response.status_code, 201, response.get_json())

    def cache_body_before_request(self):
        @self.app.before_request
        def cache_body():
            # Modern Werkzeug enforces the limit itself. Temporarily disable it
            # only for the test middleware, so Tasks must check the cached bytes
            # just as it must with the production Werkzeug 2.0 reader.
            with mock.patch.dict(self.app.config, {"MAX_CONTENT_LENGTH": None}):
                request.get_data(cache=True)
            for method in ("read", "readinto"):
                patcher = mock.patch.object(request.input_stream, method,
                                            side_effect=AssertionError("cached body read twice"))
                patcher.start()
                self.addCleanup(patcher.stop)

    def post_body_without_content_length(self, raw):
        return self.client.open(BASE, method="POST", headers=self.headers, environ_overrides={
            "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": "", "wsgi.input": io.BytesIO(raw),
            "wsgi.input_terminated": True})

    def test_cached_json_body_creates_task_without_reading_stream_again(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 1000
        self.cache_body_before_request()
        task = self.create(title="Cached body")
        self.assertEqual(self.client.get(BASE + "/" + str(task["id"])).get_json()["data"], task)

    def test_cached_body_actual_size_over_limit_is_413(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 10
        self.cache_body_before_request()
        with mock.patch.object(TasksRepository, "transaction", side_effect=AssertionError("body opened storage")) as transaction:
            response = self.post_body_without_content_length(b'{"title":"cached oversized body"}')
        self.assertEqual((response.status_code, response.get_json()["code"]), (413, "HTTP_ERROR"))
        transaction.assert_not_called()

    def test_cached_body_exactly_at_limit_is_accepted(self):
        raw = '{"title":"Задача 日本語 🚀"}'.encode("utf-8")
        self.app.config["MAX_CONTENT_LENGTH"] = len(raw)
        self.cache_body_before_request()
        response = self.post_body_without_content_length(raw)
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(response.get_json()["data"]["title"], "Задача 日本語 🚀")

    def test_cached_malformed_json_is_400_without_storage_access(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 1000
        self.cache_body_before_request()
        with mock.patch.object(TasksRepository, "transaction", side_effect=AssertionError("body opened storage")) as transaction:
            response = self.post_body_without_content_length(b'{broken')
        self.assertEqual((response.status_code, response.get_json()["code"]), (400, "HTTP_ERROR"))
        transaction.assert_not_called()

    def test_cached_empty_body_is_400_without_reading_stream_again(self):
        self.app.config["MAX_CONTENT_LENGTH"] = 1000
        self.cache_body_before_request()
        with mock.patch.object(TasksRepository, "transaction", side_effect=AssertionError("body opened storage")) as transaction:
            response = self.post_body_without_content_length(b'')
        self.assertEqual((response.status_code, response.get_json()["code"]), (400, "HTTP_ERROR"))
        transaction.assert_not_called()

    def test_http_exception_statuses_are_preserved_and_details_are_hidden(self):
        for exception, status in ((BadRequest, 400), (Unauthorized, 401), (Forbidden, 403),
                                  (NotFound, 404), (RequestEntityTooLarge, 413)):
            with self.subTest(status=status), mock.patch.object(
                    TasksRepository, "transaction", side_effect=exception(description="secret SQL /internal/path traceback")):
                response = self.client.get(BASE)
            self.assertEqual((response.status_code, response.get_json()["code"]), (status, "HTTP_ERROR"))
            self.assertEqual(set(response.get_json()), {"code", "message"})
            for secret in ("secret", "SQL", "/internal/path", "traceback"):
                self.assertNotIn(secret, response.get_data(as_text=True))

    def test_invalid_unicode_in_http_payload_is_validation_error(self):
        for field in ("title", "description"):
            with self.subTest(field=field):
                payload = {"title": "Unicode"}
                payload[field] = "\ud800"
                response = self.client.post(BASE, json=payload, headers=self.headers)
                self.assertEqual((response.status_code, response.get_json()["code"]), (422, "VALIDATION_ERROR"))
        self.assertEqual(self.client.get(BASE + "?scope=created").get_json()["data"]["total"], 0)

    def test_http_mixed_patch_is_atomic_when_reassignment_is_forbidden(self):
        task = self.create()
        url = BASE + "/" + str(task["id"])
        history = self.client.get(url + "/activity").get_json()
        self.actor = self.users[2]
        response = self.client.patch(url, json={"version": 1, "title": "Not saved", "assigned_to": 3}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get(url).get_json()["data"], task)
        self.assertEqual(self.client.get(url + "/activity").get_json(), history)

    def test_activity_failure_returns_safe_error_and_no_partial_task(self):
        with mock.patch.object(TaskSession, "add_activity", side_effect=sqlite3.OperationalError("secret SQL traceback")):
            response = self.client.post(BASE, json={"title": "Rollback"}, headers=self.headers)
        self.assertEqual((response.status_code, response.get_json()["code"]), (503, "TASKS_MODULE_UNAVAILABLE"))
        self.assertNotIn("secret", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(self.client.get(BASE + "?scope=created").get_json()["data"]["total"], 0)

    def test_locked_storage_fails_bounded_without_creating_task(self):
        connection = sqlite3.connect(str(self.path))
        connection.execute("BEGIN EXCLUSIVE")
        try:
            started = time.monotonic()
            response = self.client.post(BASE, json={"title": "Locked"}, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            connection.rollback()
            connection.close()
        self.assertEqual(self.client.get(BASE + "?scope=created").get_json()["data"]["total"], 0)

    def test_http_never_imports_or_runs_migrations(self):
        original = builtins.__import__

        def imports(name, *args, **kwargs):
            if name == "app.tasks.migrations":
                raise AssertionError("request imported migrations")
            return original(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=imports), \
                mock.patch("app.tasks.migrations.migrate_database", side_effect=AssertionError("HTTP migration")) as migrate:
            task = self.create()
            self.assertEqual(self.client.get(BASE).status_code, 200)
            self.assertEqual(self.client.patch(BASE + "/" + str(task["id"]), json={"version": 1, "title": "Edited"}, headers=self.headers).status_code, 200)
        migrate.assert_not_called()

    def test_runtime_flag_off_closes_registered_api_without_opening_storage(self):
        self.app.config["TASKS_MODULE_ENABLED"] = False
        with mock.patch.object(TasksRepository, "transaction", side_effect=AssertionError("OFF storage")) as transaction:
            for path in (BASE, BASE + "/1", BASE + "/summary", BASE + "/1/activity"):
                self.assertEqual(self.client.get(path).status_code, 503)
            self.assertEqual(self.client.post(BASE, json={"title": "OFF"}, headers=self.headers).status_code, 503)
        transaction.assert_not_called()

    def make_auth_fixture(self):
        from app.auth import AuthStore
        from app.domain_schema_migrations import apply_domain_migrations
        path = self.root / "auth.db"
        apply_domain_migrations(path, "auth", "tasks-core-test")
        connection = sqlite3.connect(str(path))
        try:
            now = int(time.time())
            for user_id in (1, 2, 5):
                email = "{}@example.test".format(user_id)
                connection.execute(
                    "INSERT INTO users(id,first_name,last_name,email,email_normalized,password_hash,role,active,"
                    "created_at,email_verified_at,updated_at,session_version) VALUES(?,'Test','',?,?,'hash','employee',?,?,?,?,1)",
                    (user_id, email, email, 0 if user_id == 5 else 1, now, now, now))
            connection.commit()
        finally:
            connection.close()
        return path, AuthStore(path)

    def test_real_auth_adapter_uses_read_only_uri_and_never_writes(self):
        path, store = self.make_auth_fixture()
        before = path.read_bytes()
        original = sqlite3.connect
        opened = []

        def guard(database, *args, **kwargs):
            opened.append(str(database))
            self.assertTrue(str(database).endswith("auth.db?mode=ro"))
            connection = original(database, *args, **kwargs)
            connection.set_authorizer(lambda action, a, b, c, d: sqlite3.SQLITE_DENY
                                      if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_ATTACH)
                                      else sqlite3.SQLITE_OK)
            return connection

        with mock.patch("sqlite3.connect", side_effect=guard):
            self.assertEqual(store.get_active_user_identity(2), {"id": 2, "active": 1})
            self.assertIsNone(store.get_active_user_identity(5))
            self.assertIsNone(store.get_active_user_identity(999))
        self.assertEqual(len(opened), 3)
        self.assertEqual(path.read_bytes(), before)

    def test_real_erp_auth_csrf_namespace_and_crud_do_not_open_other_erp_stores(self):
        from app import web
        from app.sms_migrations import migrate_database as migrate_sms
        path, store = self.make_auth_fixture()
        sms_path = self.root / "sms.db"
        migrate_sms(sms_path)
        with mock.patch.dict(os.environ, {"ERP_TASKS_MODULE_ENABLED": "1", "ERP_TASKS_MODULE_DATABASE": str(self.path),
                                          "ERP_AUTH_DATABASE": str(path), "ERP_SMS_DATABASE": str(sms_path)}):
            namespace = runpy.run_path(web.__file__, run_name="tasks_core_enabled")
        app = namespace["app"]
        app.config.update(TESTING=True, AUTH_TESTING=True, SESSION_COOKIE_SECURE=False)
        self.assertTrue(app.extensions["tasks_module"]["registered"])
        client = app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=1, session_version=1, _csrf_token="test-csrf")
        original = sqlite3.connect
        opened = []

        def guard(database, *args, **kwargs):
            opened.append(str(database))
            if not any(name in str(database) for name in ("tasks-module.db", "auth.db")):
                raise AssertionError("Unexpected ERP database opened: " + str(database))
            return original(database, *args, **kwargs)

        with mock.patch("sqlite3.connect", side_effect=guard):
            self.assertEqual(client.post(BASE, json={"title": "No CSRF"}).status_code, 403)
            response = client.post(BASE, json={"title": "Real auth", "assigned_to": 2}, headers=self.headers)
            self.assertEqual(response.status_code, 201, response.get_json())
            task = response.get_json()["data"]
            url = BASE + "/" + str(task["id"])
            self.assertEqual(client.get(url).status_code, 200)
            self.assertEqual(client.get(url + "/activity").status_code, 200)
            self.assertEqual(client.get(BASE + "?scope=created").status_code, 200)
            self.assertEqual(client.get(BASE + "/summary?scope=created").status_code, 200)
            self.assertEqual(client.patch(url, json={"version": 1, "title": "Real edit"}, headers=self.headers).status_code, 200)
            for operation, version, fields in (("status", 2, {"status": "done"}), ("delete", 3, {}), ("restore", 4, {})):
                self.assertEqual(client.post(url + "/" + operation, json=dict(version=version, **fields), headers=self.headers).status_code, 200)
            projects_url = "/api/v1/tasks-module/projects"
            response = client.post(projects_url, json={"name": "Real ERP project"}, headers=self.headers)
            self.assertEqual(response.status_code, 201, response.get_json())
            project = response.get_json()["data"]
            project_url = projects_url + "/" + str(project["id"])
            self.assertEqual(client.post(project_url + "/members", json={"version": 1, "user_id": 2}, headers=self.headers).status_code, 200)
            self.assertEqual(client.patch(url, json={"version": 5, "project_id": project["id"]}, headers=self.headers).status_code, 200)
            for path in (projects_url, project_url, project_url + "/summary", project_url + "/members", project_url + "/activity"):
                self.assertEqual(client.get(path).status_code, 200)
            self.assertEqual(client.delete(project_url + "/members/2", json={"version": 2}, headers=self.headers).status_code, 200)
        self.assertTrue(any("auth.db?mode=ro" in name for name in opened))
        self.assertTrue(any("tasks-module.db?mode=rw" in name for name in opened))
        # The existing ERP session middleware still maintains auth sessions;
        # task business operations have no dependency on catalog/audit/legacy.
        rules = {rule.rule for rule in app.url_map.iter_rules()}
        self.assertIn("/api/v1/tasks", rules)
        self.assertIn(BASE, rules)


if __name__ == "__main__":
    unittest.main()
