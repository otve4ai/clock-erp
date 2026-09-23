import datetime as dt
import io
import sqlite3
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.retain_erp_backups import (
    _backup_sqlite_database,
    apply_plan,
    combined_retention_plan,
    create_backup,
    discover_backups,
    retention_plan,
    write_recovery_metadata,
)


class BackupRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "backups"
        self.root.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def full_backup(self, timestamp, directory=None, prefix="clock-erp"):
        directory = directory or self.root
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "{}-{}.tar.gz".format(
            prefix, timestamp.strftime("%Y%m%d-%H%M%S")
        )
        payload = b"backup"
        with tarfile.open(str(path), "w:gz") as backup:
            member = tarfile.TarInfo("./instance/catalog.db")
            member.size = len(payload)
            backup.addfile(member, io.BytesIO(payload))
        return path

    def test_daily_policy_keeps_latest_fourteen_daily_copies(self):
        now = dt.datetime(2026, 8, 21, 16, 0, 0)
        same_day_old = self.full_backup(dt.datetime(2026, 8, 21, 10, 0, 0))
        same_day_new = self.full_backup(dt.datetime(2026, 8, 21, 15, 0, 0))
        retained = [self.full_backup(now - dt.timedelta(days=offset)) for offset in range(1, 14)]
        expired = self.full_backup(now - dt.timedelta(days=14))

        actions = retention_plan(discover_backups(self.root)["daily"], now)
        by_path = {path: action for action, _timestamp, path in actions}

        self.assertEqual(by_path[same_day_new], "KEEP")
        self.assertEqual(by_path[same_day_old], "DELETE")
        self.assertTrue(all(by_path[path] == "KEEP" for path in retained))
        self.assertEqual(by_path[expired], "DELETE")

    def test_shared_archive_is_kept_until_every_category_expires(self):
        now = dt.datetime(2026, 9, 23, 16, 0, 0)
        monthly = self.full_backup(dt.datetime(2026, 9, 1, 3, 17, 0),
                                   self.root / "daily", "clock-erp-daily")
        for offset in range(14):
            self.full_backup(now - dt.timedelta(days=offset), self.root / "daily",
                             "clock-erp-daily")

        actions = combined_retention_plan(discover_backups(self.root), now)
        by_path = {path: action for action, _timestamp, path in actions}

        self.assertEqual(by_path[monthly], "KEEP")

    def test_weekly_and_monthly_limits_are_independent(self):
        now = dt.datetime(2026, 9, 23, 16, 0, 0)
        weekly = []
        for offset in range(9):
            weekly.append(self.full_backup(
                dt.datetime(2026, 9, 20, 3, 17, 0) - dt.timedelta(weeks=offset),
                self.root / "daily", "clock-erp-daily",
            ))
        monthly = []
        for offset in range(13):
            month_index = 2026 * 12 + 8 - offset
            year = month_index // 12
            month = month_index % 12 + 1
            monthly.append(self.full_backup(
                dt.datetime(year, month, 1, 3, 17, 0),
                self.root / "daily", "clock-erp-daily",
            ))
        streams = discover_backups(self.root)
        weekly_actions = {path: action for action, _timestamp, path in
                          retention_plan(streams["weekly"], now, "weekly")}
        monthly_actions = {path: action for action, _timestamp, path in
                           retention_plan(streams["monthly"], now, "monthly")}
        self.assertEqual(weekly_actions[weekly[0]], "KEEP")
        self.assertEqual(weekly_actions[weekly[-1]], "DELETE")
        self.assertEqual(monthly_actions[monthly[0]], "KEEP")
        self.assertEqual(monthly_actions[monthly[-1]], "DELETE")

    def test_manual_backups_are_never_rotated(self):
        manual = self.root / "manual"
        backup = self.full_backup(
            dt.datetime(2020, 1, 1, 12, 0, 0), manual, "clock-erp-manual"
        )
        target = manual / (
            "clock-erp-manual-20200101-120000-"
            "0123456789abcdef0123456789abcdef.tar.gz"
        )
        backup.rename(target)

        actions = retention_plan(
            discover_backups(self.root)["manual"],
            dt.datetime(2026, 8, 21, 16, 0, 0),
            policy="manual",
        )

        self.assertEqual(actions[0][0], "KEEP")
        apply_plan(actions, self.root, apply_changes=True)
        self.assertTrue(target.is_file())

    def test_safety_backups_are_never_rotated(self):
        safety = self.root / "safety"
        backup = self.full_backup(
            dt.datetime(2020, 1, 1, 12, 0, 0), safety, "clock-erp-safety"
        )
        target = safety / (
            "clock-erp-safety-20200101-120000-"
            "0123456789abcdef0123456789abcdef.tar.gz"
        )
        backup.rename(target)
        actions = retention_plan(discover_backups(self.root)["safety"],
                                 dt.datetime(2026, 8, 21), "safety")
        self.assertEqual(actions[0][0], "KEEP")

    def test_unknown_and_invalid_files_are_never_delete_candidates(self):
        unknown = self.root / "site-backup-20260821.tar.gz"
        unknown.write_bytes(b"site")
        invalid = self.root / "clock-erp-20260821-150000.tar.gz"
        invalid.write_bytes(b"not a backup")

        streams = discover_backups(self.root)
        actions = retention_plan(streams["daily"], dt.datetime(2026, 8, 21, 16, 0, 0))

        self.assertNotIn(unknown, [path for _action, _timestamp, path in actions])
        self.assertEqual(actions[0][0], "SKIP_INVALID")

    def test_daily_creation_is_idempotent_and_sqlite_is_valid(self):
        project = Path(self.temp.name) / "project"
        instance = project / "instance"
        instance.mkdir(parents=True)
        database = instance / "catalog.db"
        with sqlite3.connect(str(database)) as connection:
            connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO products DEFAULT VALUES")
        now = dt.datetime(2026, 8, 21, 3, 17, 0)

        first = create_backup(project, self.root, now, "daily", apply_changes=True)
        second = create_backup(
            project, self.root, now.replace(hour=18), "daily", apply_changes=True
        )

        self.assertEqual(first, second)
        self.assertTrue(first.is_file())
        self.assertEqual(len(discover_backups(self.root)["daily"]), 1)
        with tarfile.open(str(first), "r:gz") as backup:
            self.assertIn("instance/catalog.db", backup.getnames())

    def test_daily_creation_excludes_nested_runtime_backups(self):
        project = Path(self.temp.name) / "project"
        instance = project / "instance"
        nested = instance / "backups" / "old"
        nested.mkdir(parents=True)
        (nested / "catalog.db").write_bytes(b"old")
        (instance / "catalog.db.backup-strap-20260723").write_bytes(b"old")
        with sqlite3.connect(str(instance / "catalog.db")) as connection:
            connection.execute("CREATE TABLE products (id INTEGER)")

        backup = create_backup(
            project,
            self.root,
            dt.datetime(2026, 8, 21, 3, 17, 0),
            "daily",
            apply_changes=True,
        )

        with tarfile.open(str(backup), "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("instance/catalog.db", names)
        self.assertNotIn("instance/backups/old/catalog.db", names)
        self.assertNotIn("instance/catalog.db.backup-strap-20260723", names)

    def test_backup_excludes_transient_sqlite_sidecars(self):
        project = Path(self.temp.name) / "project"
        instance = project / "instance"
        instance.mkdir(parents=True)
        database = instance / "catalog.db"
        with sqlite3.connect(str(database)) as connection:
            connection.execute("CREATE TABLE products (id INTEGER)")
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(database) + suffix).write_bytes(b"transient")

        backup = create_backup(
            project,
            self.root,
            dt.datetime(2026, 8, 21, 3, 17, 0),
            "daily",
            apply_changes=True,
        )

        with tarfile.open(str(backup), "r:gz") as archive:
            names = archive.getnames()
        self.assertIn("instance/catalog.db", names)
        self.assertFalse(any(
            name.endswith((".db-journal", ".db-wal", ".db-shm"))
            for name in names
        ))

    def test_manual_creation_uses_dedicated_directory(self):
        project = Path(self.temp.name) / "manual-project"
        instance = project / "instance"
        instance.mkdir(parents=True)
        with sqlite3.connect(str(instance / "catalog.db")) as connection:
            connection.execute("CREATE TABLE products (id INTEGER)")

        target = create_backup(
            project, self.root, dt.datetime(2026, 8, 21, 12, 0, 0),
            "manual", operation_id="0123456789abcdef0123456789abcdef",
            apply_changes=True,
        )

        self.assertEqual(target.parent, self.root / "manual")
        self.assertEqual(len(discover_backups(self.root)["manual"]), 1)

    def test_legacy_python_uses_sqlite_cli_backup(self):
        class LegacyConnection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        source = Path(self.temp.name) / "source.db"
        destination = Path(self.temp.name) / "destination.db"
        with mock.patch(
            "scripts.retain_erp_backups.sqlite3.connect",
            return_value=LegacyConnection(),
        ), mock.patch(
            "scripts.retain_erp_backups.shutil.which",
            return_value="/usr/bin/sqlite3",
        ), mock.patch(
            "scripts.retain_erp_backups.subprocess.run"
        ) as run:
            _backup_sqlite_database(source, destination)

        run.assert_called_once_with(
            [
                "/usr/bin/sqlite3",
                str(source),
                ".backup '{}'".format(destination),
            ],
            check=True,
            stdout=mock.ANY,
            stderr=mock.ANY,
        )

    def test_new_daily_backup_gets_exact_recovery_metadata(self):
        project = Path(self.temp.name) / "project-metadata"
        instance = project / "instance"
        contract = project / "ops" / "recovery-schema-contract.json"
        contract.parent.mkdir(parents=True)
        instance.mkdir()
        contract.write_text(json.dumps({
            "contract_version": 2,
            "databases": {"catalog.db": ["products"]},
        }), encoding="utf-8")
        with sqlite3.connect(str(instance / "catalog.db")) as connection:
            connection.execute("CREATE TABLE products (id INTEGER PRIMARY KEY)")
        (project / ".gitignore").write_text("instance/\n", encoding="utf-8")
        subprocess.run(["git", "init", "-b", "main"], cwd=str(project), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(project), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(project), check=True)
        subprocess.run(["git", "add", "."], cwd=str(project), check=True)
        subprocess.run(["git", "commit", "-m", "schema"], cwd=str(project), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        archive = create_backup(
            project, self.root, dt.datetime(2026, 8, 21, 3, 17, 0),
            "daily", apply_changes=True,
        )

        write_recovery_metadata(
            project, self.root, archive, "daily", reason="scheduled",
            retention_categories=["daily"],
        )

        metadata_files = list((self.root / "metadata").glob("*.json"))
        self.assertEqual(len(metadata_files), 1)
        metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
        self.assertEqual(metadata["metadata_version"], 2)
        self.assertEqual(metadata["type"], "daily")
        self.assertEqual(metadata["backup_type"], "daily")
        self.assertEqual(metadata["retention_categories"], ["daily"])
        self.assertEqual(metadata["reason"], "scheduled")
        self.assertEqual(metadata["created_at"], metadata["timestamp"])
        self.assertEqual(metadata["status"], "verified")
        self.assertRegex(metadata["checksum_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(metadata["integrity_status"], "verified")
        self.assertEqual(metadata["git_branch"], "main")
        self.assertIn("catalog.db", metadata["database_manifest"])
        self.assertIn("catalog.db", metadata["file_manifest"])


if __name__ == "__main__":
    unittest.main()
