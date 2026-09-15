import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from werkzeug.security import generate_password_hash

from app import auth, web
from app.services.backup_admin import (
    BackupAdminService,
    BackupBusyError,
    BackupNotFoundError,
)


PASSWORD = "correct horse battery"


class BackupAdminServiceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.instance = self.project / "instance"
        self.backups = self.root / "backups"
        self.daily = self.backups / "daily"
        self.instance.mkdir(parents=True)
        self.daily.mkdir(parents=True)
        self.service = BackupAdminService(
            self.project, self.backups, "/bin/false",
            cron_path=self.root / "cron",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def archive(self, name, size_marker=b"payload"):
        path = self.daily / name
        with tarfile.open(str(path), "w:gz") as archive:
            info = tarfile.TarInfo("instance/settings.json")
            info.size = len(size_marker)
            archive.addfile(info, io.BytesIO(size_marker))
        return path

    def test_backup_listing_uses_real_files_and_sorts_newest_first(self):
        older = self.archive("clock-erp-daily-20260913-031701.tar.gz")
        newer = self.archive("clock-erp-daily-20260915-031701.tar.gz")
        (self.daily / "demo.tar.gz").write_bytes(b"not a backup")

        listing = self.service.list_backups()

        self.assertEqual([item["timestamp"][:10] for item in listing], ["2026-09-15", "2026-09-13"])
        self.assertEqual([item["size"] for item in listing], [newer.stat().st_size, older.stat().st_size])
        self.assertEqual([item["status"] for item in listing], ["ready", "ready"])

    def test_old_backup_without_metadata_is_not_restore_point(self):
        self.archive("clock-erp-daily-20260915-031701.tar.gz")
        with mock.patch.object(self.service, "service_status", return_value={"active": True}), \
             mock.patch.object(self.service, "git_status", return_value={"available": True, "history": []}), \
             mock.patch.object(self.service, "storage_status", return_value={"state": "ok"}):
            status = self.service.status()
        self.assertEqual(status["restore_points"], [])

    def test_verified_metadata_links_backup_and_commit(self):
        path = self.archive("clock-erp-daily-20260915-031701.tar.gz")
        backup = self.service.list_backups(public=False)[0]
        self.service.metadata_root.mkdir()
        self.service._metadata_path(backup["backup_id"]).write_text(json.dumps({
            "backup_id": backup["backup_id"], "type": "manual",
            "timestamp": backup["timestamp"],
            "integrity_status": "verified", "git_commit": "a" * 40,
            "git_branch": "main", "schema_versions": {"catalog.db": 4},
            "size": path.stat().st_size,
        }), encoding="utf-8")
        with mock.patch.object(self.service, "service_status", return_value={"active": True}), \
             mock.patch.object(self.service, "git_status", return_value={"available": True, "history": []}), \
             mock.patch.object(self.service, "storage_status", return_value={"state": "ok"}):
            status = self.service.status()
        self.assertEqual(status["backups"][0]["status"], "verified")
        self.assertEqual(status["restore_points"][0]["git_commit"], "a" * 40)

    def test_unknown_and_traversal_backup_ids_are_rejected(self):
        self.archive("clock-erp-daily-20260915-031701.tar.gz")
        for backup_id in ("../../etc/passwd", "f" * 24):
            with self.assertRaises(BackupNotFoundError):
                self.service.resolve_backup(backup_id)

    def test_storage_percent_is_computed_from_filesystem_values(self):
        usage = shutil_usage(total=1000, used=720, free=280)
        with mock.patch("app.services.backup_admin.shutil.disk_usage", return_value=usage), \
             mock.patch.object(self.service, "_cached_du", return_value=None):
            result = self.service.storage_status()
        self.assertEqual(result["percent"], 72.0)
        self.assertEqual(result["state"], "warning")

    def test_unavailable_storage_is_not_reported_as_zero(self):
        with mock.patch("app.services.backup_admin.shutil.disk_usage", side_effect=OSError), \
             mock.patch.object(self.service, "_cached_du", return_value=None):
            result = self.service.storage_status()
        self.assertIsNone(result["total"])
        self.assertIsNone(result["percent"])
        self.assertEqual(result["state"], "unknown")

    def test_parallel_backup_is_blocked(self):
        self.service._set_operation({
            "id": "running", "active": True, "status": "creating",
            "updated_epoch": time.time(),
        })
        with self.assertRaises(BackupBusyError):
            self.service.start_manual_backup({"id": 1, "email": "owner@example.com"})

    def test_backup_failure_changes_status_and_releases_operation(self):
        database = self.instance / "auth.db"
        connection = sqlite3.connect(str(database))
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        with mock.patch.object(self.service, "storage_status", return_value={"free": 10 ** 9}), \
             mock.patch.object(self.service, "_cached_du", return_value=4096):
            self.service.start_manual_backup({"id": 1, "email": "owner@example.com"})
        deadline = time.time() + 3
        status = self.service.operation_status()
        while status.get("active") and time.time() < deadline:
            time.sleep(0.02)
            status = self.service.operation_status()
        self.assertFalse(status["active"])
        self.assertEqual(status["status"], "error")
        self.assertEqual(status["message"], "Не удалось создать бэкап")

    def test_archive_verification_checks_sqlite_and_rejects_traversal(self):
        database = self.instance / "auth.db"
        connection = sqlite3.connect(str(database))
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        valid = self.root / "valid.tar.gz"
        with tarfile.open(str(valid), "w:gz") as archive:
            archive.add(str(database), arcname="instance/auth.db")
        self.assertEqual(self.service._verify_archive(valid, ["auth.db"]), ["auth.db"])

        unsafe = self.root / "unsafe.tar.gz"
        with tarfile.open(str(unsafe), "w:gz") as archive:
            info = tarfile.TarInfo("../outside.db")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"data"))
        with self.assertRaises(Exception):
            self.service._verify_archive(unsafe, [])

    def test_manual_backup_writes_verified_code_and_schema_metadata(self):
        database = self.instance / "auth.db"
        connection = sqlite3.connect(str(database))
        connection.execute("PRAGMA user_version = 7")
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        operation = {"id": "abc123", "active": True, "status": "queued"}

        def create_archive(*_args, **_kwargs):
            temporary = self.backups / "temporary"
            temporary.mkdir()
            target = temporary / "clock-erp-temp-20260915-121500-manual-abc123.tar.gz"
            with tarfile.open(str(target), "w:gz") as archive:
                archive.add(str(database), arcname="instance/auth.db")
            return subprocess.CompletedProcess([], 0, "CREATED", "")

        with mock.patch.object(self.service, "_run", side_effect=create_archive), \
             mock.patch.object(self.service, "git_status", return_value={
                 "commit": "a" * 40, "short": "a" * 8, "branch": "main",
             }):
            self.service._backup_worker(operation, {"id": 1, "email": "owner@example.com"})
        current = self.service.operation_status()
        self.assertEqual(current["status"], "complete")
        backup = self.service.list_backups()[0]
        self.assertEqual(backup["status"], "verified")
        self.assertEqual(backup["git_commit"], "a" * 40)
        self.assertEqual(backup["schema_versions"]["auth.db"], 7)

    def test_schedule_is_read_from_cron(self):
        self.service.cron_path.write_text(
            "17 3 * * * root backup --create-daily\n", encoding="utf-8"
        )
        self.assertEqual(self.service.schedule_status()["label"], "Ежедневно в 03:17")


class shutil_usage:
    def __init__(self, total, used, free):
        self.total = total
        self.used = used
        self.free = free


class BackupAdminAuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(web.app.config)
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        backup_root = root / "backups"
        backup_root.mkdir()
        cron = root / "cron"
        cron.write_text(
            "17 3 * * * root /usr/local/sbin/clock-erp-backup-retention --create-daily\n",
            encoding="utf-8",
        )
        web.app.config.update(
            TESTING=True, AUTH_TESTING=True, SESSION_COOKIE_SECURE=False,
            ERP_BACKUP_ROOT=str(backup_root), ERP_BACKUP_SCRIPT="/bin/false",
            ERP_BACKUP_CRON=str(cron),
            ERP_BACKUP_REMOTE_CHECK=False,
        )
        web.app.extensions.pop("backup_admin_service", None)
        self.store = auth.AuthStore(web.app.config["AUTH_DATABASE"])
        self.client = web.app.test_client()

    def tearDown(self):
        web.app.extensions.pop("backup_admin_service", None)
        web.app.config.clear()
        web.app.config.update(self.original_config)
        self.temporary.cleanup()

    def insert_user(self, email, role):
        now = int(time.time())
        normalized = auth.normalize_email(email)
        with self.store.connect() as connection:
            connection.execute("DELETE FROM users WHERE email_normalized = ?", (normalized,))
            connection.execute(
                """
                INSERT INTO users (
                    first_name, last_name, email, email_normalized,
                    password_hash, role, active, created_at,
                    email_verified_at, updated_at, session_version
                ) VALUES ('', '', ?, ?, ?, ?, 1, ?, ?, ?, 1)
                """,
                (email, normalized, generate_password_hash(PASSWORD, method=auth.PASSWORD_HASH_METHOD),
                 role, now, now, now),
            )

    def csrf(self):
        self.client.get("/login")
        with self.client.session_transaction() as session_data:
            return session_data["_csrf_token"]

    def login(self, email):
        return self.client.post("/login", data={
            "csrf_token": self.csrf(), "email": email, "password": PASSWORD,
        })

    def test_owner_can_open_page_and_read_api(self):
        self.insert_user("backup-owner@example.com", "admin")
        self.login("backup-owner@example.com")
        page = self.client.get("/app/backups")
        api = self.client.get("/api/v1/backups/status")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Хранилище сервера", page.get_data(as_text=True))
        self.assertEqual(api.status_code, 200)

    def test_employee_cannot_open_page_or_call_api_directly(self):
        self.insert_user("backup-employee@example.com", "employee")
        self.login("backup-employee@example.com")
        self.assertEqual(self.client.get("/app/backups").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/backups/status").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/backups").status_code, 403)

    def test_destructive_get_is_absent_and_csrf_is_required(self):
        self.insert_user("backup-security@example.com", "admin")
        self.login("backup-security@example.com")
        self.assertEqual(self.client.get("/api/v1/backups/" + "f" * 24 + "/restore").status_code, 405)
        response = self.client.post("/api/v1/backups")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "CSRF_INVALID")

    def test_unknown_backup_id_is_rejected_after_confirmation(self):
        self.insert_user("backup-unknown@example.com", "admin")
        self.login("backup-unknown@example.com")
        response = self.client.post(
            "/api/v1/backups/" + "f" * 24 + "/restore",
            json={"confirmation": "ВОССТАНОВИТЬ"},
            headers={"X-CSRF-Token": self.csrf()},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["code"], "BACKUP_NOT_FOUND")

    def test_restore_without_verified_safety_architecture_is_blocked(self):
        self.insert_user("backup-restore@example.com", "admin")
        self.login("backup-restore@example.com")
        service = web._backup_admin_service()
        daily = service.backup_root / "daily"
        daily.mkdir()
        archive_path = daily / "clock-erp-daily-20260915-031701.tar.gz"
        with tarfile.open(str(archive_path), "w:gz") as archive:
            info = tarfile.TarInfo("instance/settings.json")
            info.size = 2
            archive.addfile(info, io.BytesIO(b"{}"))
        backup_id = service.list_backups()[0]["backup_id"]
        response = self.client.post(
            "/api/v1/backups/{}/restore".format(backup_id),
            json={"confirmation": "ВОССТАНОВИТЬ"},
            headers={"X-CSRF-Token": self.csrf()},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "RESTORE_BLOCKED")

    def test_code_rollback_is_blocked_for_dirty_tree(self):
        self.insert_user("backup-rollback@example.com", "admin")
        self.login("backup-rollback@example.com")
        fake = mock.Mock()
        fake.git_status.return_value = {"dirty": True}
        with mock.patch.object(web, "_backup_admin_service", return_value=fake):
            response = self.client.post(
                "/api/v1/backups/code/{}/rollback".format("a" * 40),
                json={"confirmation": "ОТКАТИТЬ КОД"},
                headers={"X-CSRF-Token": self.csrf()},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "ROLLBACK_DIRTY_TREE")
        fake.blocked_restore_attempt.assert_not_called()

    def test_navigation_is_owner_only(self):
        self.insert_user("backup-nav-owner@example.com", "admin")
        self.login("backup-nav-owner@example.com")
        page = self.client.get("/app/backups").get_data(as_text=True)
        self.assertIn('data-navigation-key="backups"', page)

        employee = web.app.test_client()
        self.client = employee
        self.insert_user("backup-nav-employee@example.com", "employee")
        self.login("backup-nav-employee@example.com")
        settings = self.client.get("/app/settings").get_data(as_text=True)
        self.assertNotIn('data-navigation-key="backups"', settings)


if __name__ == "__main__":
    unittest.main()
