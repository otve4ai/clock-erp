import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from werkzeug.security import check_password_hash, generate_password_hash

from app import auth, web
from app.domain_schema_migrations import apply_domain_migrations
from app.services.audit_journal import AuditJournal


PASSWORD = "correct horse battery"


class TeamManagementTest(unittest.TestCase):
    def setUp(self):
        self.original = dict(web.app.config)
        self.temp = tempfile.TemporaryDirectory()
        self.auth_path = Path(self.temp.name) / "auth.db"
        apply_domain_migrations(self.auth_path, "auth", "team-test")
        web.app.config.update(TESTING=True, AUTH_TESTING=True,
                              AUTH_DATABASE=str(self.auth_path),
                              SESSION_COOKIE_SECURE=False)
        web.app.extensions["auth_stores"] = {}
        self.store = auth.AuthStore(self.auth_path)
        self.admin = self.insert("owner", "owner@example.test", "admin")
        self.employee = self.insert("staff", "staff@example.test", "employee")
        self.client = web.app.test_client()

    def tearDown(self):
        web.app.config.clear()
        web.app.config.update(self.original)
        self.temp.cleanup()

    def insert(self, login, email, role):
        now = int(time.time())
        with self.store.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO users(first_name,last_name,login,login_normalized,email,"
                "email_normalized,password_hash,role,active,created_at,email_verified_at,"
                "updated_at,session_version) VALUES (?, '', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 1)",
                (login.title(), login, auth.normalize_login(login), email,
                 auth.normalize_email(email), generate_password_hash(
                     PASSWORD, method=auth.PASSWORD_HASH_METHOD), role, now, now, now),
            )
        return cursor.lastrowid

    def session_as(self, user_id):
        user = self.store.get_team_user(user_id)
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["session_version"] = user["session_version"]
            session["_csrf_token"] = "team-csrf"

    def payload(self, **changes):
        data = {"name": "Новый сотрудник", "login": "new-user",
                "email": "new@example.test", "role": "employee",
                "password": PASSWORD, "password_confirmation": PASSWORD}
        data.update(changes)
        return data

    def request(self, method, path, data=None, csrf=True):
        headers = {"X-CSRF-Token": "team-csrf"} if csrf else {}
        return self.client.open(path, method=method, json=data, headers=headers)

    def test_admin_can_view_and_search_team_employee_cannot(self):
        self.session_as(self.admin)
        page = self.client.get("/app/team?q=staff")
        self.assertEqual(page.status_code, 200)
        self.assertIn("staff@example.test", page.get_data(as_text=True))
        detail = self.client.get("/app/team/{}?period=30".format(self.employee))
        self.assertEqual(detail.status_code, 200)
        self.assertIn("Профиль и доступ", detail.get_data(as_text=True))
        self.assertIn("Журнал действий", detail.get_data(as_text=True))
        self.session_as(self.employee)
        self.assertEqual(self.client.get("/app/team").status_code, 403)

    def test_create_validates_and_hashes_password(self):
        self.session_as(self.admin)
        response = self.request("POST", "/api/v1/team/users", self.payload())
        self.assertEqual(response.status_code, 201)
        created = self.store.get_user_by_email("new@example.test")
        self.assertNotEqual(created["password_hash"], PASSWORD)
        self.assertTrue(check_password_hash(created["password_hash"], PASSWORD))
        duplicate = self.request("POST", "/api/v1/team/users", self.payload(email="other@example.test"))
        self.assertEqual(duplicate.status_code, 409)
        duplicate_email = self.request(
            "POST", "/api/v1/team/users",
            self.payload(login="another-user", email="staff@example.test"),
        )
        self.assertEqual(duplicate_email.status_code, 409)
        invalid = self.request("POST", "/api/v1/team/users", self.payload(login="unique", email="bad"))
        self.assertEqual(invalid.status_code, 422)

    def test_update_name_login_email_role_and_password(self):
        self.session_as(self.admin)
        reset_token = self.store.create_token(self.employee, auth.TOKEN_PASSWORD_RESET, 1800)
        response = self.request("PUT", "/api/v1/team/users/{}".format(self.employee),
                                self.payload(name="Владимир", login="ops", email="ops@example.test",
                                             role="admin", password="replacement password",
                                             password_confirmation="replacement password"))
        self.assertEqual(response.status_code, 200)
        user = self.store.get_team_user(self.employee)
        self.assertEqual((user["first_name"], user["login"], user["email"], user["role"]),
                         ("Владимир", "ops", "ops@example.test", "admin"))
        self.assertIsNone(self.store.authenticate("staff", PASSWORD))
        self.assertIsNotNone(self.store.authenticate("ops", "replacement password"))
        self.assertFalse(self.store.reset_password(reset_token, "third password"))

    def test_employee_cannot_create_update_or_delete_by_direct_request(self):
        self.session_as(self.employee)
        for method, path, data in (
            ("POST", "/api/v1/team/users", self.payload()),
            ("PUT", "/api/v1/team/users/{}".format(self.admin), self.payload(role="employee")),
            ("DELETE", "/api/v1/team/users/{}".format(self.admin), None),
        ):
            self.assertEqual(self.request(method, path, data).status_code, 403)

    def test_csrf_is_required(self):
        self.session_as(self.admin)
        self.assertEqual(self.request("POST", "/api/v1/team/users", self.payload(), csrf=False).status_code, 403)

    def test_self_delete_and_last_admin_are_blocked(self):
        self.session_as(self.admin)
        self.assertEqual(self.request("DELETE", "/api/v1/team/users/{}".format(self.admin)).status_code, 409)
        self.assertEqual(self.request("PUT", "/api/v1/team/users/{}".format(self.admin),
                                     self.payload(name="Owner", login="owner", email="owner@example.test",
                                                  role="employee", password="", password_confirmation="")).status_code, 409)

    def test_delete_is_soft_and_preserves_identity(self):
        self.session_as(self.admin)
        reset_token = self.store.create_token(
            self.employee, auth.TOKEN_PASSWORD_RESET, 1800
        )
        response = self.request("DELETE", "/api/v1/team/users/{}".format(self.employee))
        self.assertEqual(response.status_code, 200)
        user = self.store.get_team_user(self.employee)
        self.assertEqual(user["active"], 0)
        self.assertEqual(user["login"], "staff")
        self.assertIsNone(self.store.authenticate("staff", PASSWORD))
        self.assertFalse(self.store.reset_password(reset_token, "replacement password"))

    def test_audit_is_preserved_and_contains_no_password(self):
        self.session_as(self.admin)
        created = self.request("POST", "/api/v1/team/users", self.payload(login="audited", email="audited@example.test"))
        user_id = created.get_json()["data"]["user_id"]
        self.request("PUT", "/api/v1/team/users/{}".format(user_id),
                     self.payload(login="audited", email="audited@example.test",
                                  password="replacement password",
                                  password_confirmation="replacement password"))
        self.request("DELETE", "/api/v1/team/users/{}".format(user_id))
        listing = AuditJournal().list_events(subject_user=str(user_id), limit=20)
        self.assertGreaterEqual(len(listing["events"]), 3)
        serialized = str(listing["events"]).casefold()
        self.assertNotIn(PASSWORD.casefold(), serialized)
        self.assertNotIn("replacement password", serialized)

    def test_new_reset_request_invalidates_previous_token_without_exposing_email(self):
        first = self.store.create_token(self.employee, auth.TOKEN_PASSWORD_RESET, 1800)
        second = self.store.create_token(self.employee, auth.TOKEN_PASSWORD_RESET, 1800)
        self.assertFalse(self.store.reset_password(first, "replacement password"))
        self.assertTrue(self.store.reset_password(second, "replacement password"))
        known = self.client.post("/forgot-password", data={"email": "staff@example.test"})
        unknown = self.client.post("/forgot-password", data={"email": "missing@example.test"})
        self.assertEqual((known.status_code, known.get_data()), (unknown.status_code, unknown.get_data()))

    def test_full_password_recovery_uses_current_email_and_is_one_time(self):
        self.session_as(self.admin)
        response = self.request(
            "PUT", "/api/v1/team/users/{}".format(self.employee),
            self.payload(
                name="Staff", login="staff", email="current@example.test",
                password="", password_confirmation="",
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.client = web.app.test_client()
        self.client.get("/forgot-password")
        with self.client.session_transaction() as session:
            csrf = session["_csrf_token"]
        with mock.patch.object(auth, "_send_password_reset", return_value=True) as send:
            reset_request = self.client.post(
                "/forgot-password",
                data={"email": "current@example.test", "csrf_token": csrf},
            )
        self.assertEqual(reset_request.status_code, 200)
        self.assertEqual(send.call_count, 1)
        mailed_user, token = send.call_args.args
        self.assertEqual(mailed_user["email"], "current@example.test")
        new_password = "replacement password"
        changed = self.client.post(
            "/reset-password/{}".format(token),
            data={
                "password": new_password,
                "password_confirmation": new_password,
                "csrf_token": csrf,
            },
        )
        self.assertEqual(changed.status_code, 302)
        self.assertIn("password-changed", changed.location)
        self.assertEqual(
            self.client.get("/reset-password/{}".format(token)).status_code,
            400,
        )
        self.assertIsNone(self.store.authenticate("staff", PASSWORD))
        self.assertIsNotNone(self.store.authenticate("staff", new_password))

    def test_expired_reset_link_is_rejected_without_sending_email(self):
        token = self.store.create_token(
            self.employee, auth.TOKEN_PASSWORD_RESET, -1
        )
        self.assertEqual(
            self.client.get("/reset-password/{}".format(token)).status_code,
            400,
        )
        with mock.patch.object(auth, "_send_password_reset") as send:
            self.client.get("/forgot-password")
            with self.client.session_transaction() as session:
                csrf = session["_csrf_token"]
            self.client.post(
                "/forgot-password",
                data={"email": "missing@example.test", "csrf_token": csrf},
            )
        send.assert_not_called()

    def test_journal_filters_paginates_and_hides_deleted_links(self):
        journal = AuditJournal()
        for index in range(27):
            journal.record(
                "sale", str(1000 + index),
                "deleted" if index == 0 else "updated",
                "Продажа {}".format(index),
                actor_id=str(self.employee), actor_name="Staff",
            )
        self.session_as(self.admin)
        page = self.client.get(
            "/app/team/{}?category=sales&period=30".format(self.employee)
        )
        html = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Показать ещё", html)
        self.assertIn("/app/sales?q=", html)
        self.assertNotIn('href="/app/sales?q=1000"', html)


if __name__ == "__main__":
    unittest.main()
