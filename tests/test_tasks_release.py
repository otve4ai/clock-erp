"""Offline release classification and real backup/staging on synthetic data."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

from scripts.tasks_release_preflight import MANIFEST, NON_SCHEMA_PATHS, schema_changes, tree_digest
from scripts.retain_erp_backups import _copy_runtime_data
from app.services.backup_admin import BackupAdminService
from app.services.recovery_v2 import (RecoveryEngine, RecoveryError, inspect_instance,
                                     inspect_file_manifest)
from app.tasks.migrations import migrate_database
from app.tasks.repository import TasksRepository
from app.tasks.project_services import ProjectsService
from app.tasks.services import TasksService

ROOT = Path(__file__).resolve().parents[1]


class ReleaseClassificationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init")
        self.git("config", "user.name", "Offline Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        for path in NON_SCHEMA_PATHS:
            self.write(path, "before\n")
        self.git("add", "-A", ".")
        self.git("commit", "-m", "synthetic base")
        self.base = self.git("rev-parse", "HEAD")
        for path in NON_SCHEMA_PATHS:
            self.write(path, "after\n")
        self.write("ops/recovery-schema-contract.json", '{"contract_version":2}\n')
        self.git("add", "-A", ".")
        self.manifest = {"version": 1, "base_commit": self.base,
                         "candidate_tree_sha256": tree_digest(self.root, self.git("write-tree")),
                         "non_schema_paths": list(NON_SCHEMA_PATHS)}
        self.candidate = self.stage_manifest()

    def git(self, *args):
        return subprocess.check_output(["git"] + list(args), cwd=str(self.root),
                                       stderr=subprocess.PIPE).decode("utf-8").strip()

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def stage_manifest(self):
        self.write(MANIFEST, json.dumps(self.manifest))
        self.git("add", "-A", ".")
        return self.git("write-tree")

    def test_exact_tree_classifies_only_two_reviewed_files(self):
        result = schema_changes(self.root, self.base, self.candidate)
        self.assertNotIn("app/auth.py", result)
        self.assertNotIn("app/schema_migrations.py", result)
        self.assertIn("ops/recovery-schema-contract.json", result)
        self.assertIn(MANIFEST, result)

    def test_one_byte_schema_change_or_extra_migration_or_contract_change_fails(self):
        for path in ("app/auth.py", "app/schema_migrations.py", "scripts/migrate_other.py",
                     "ops/recovery-schema-contract.json", "app/unrelated.py"):
            with self.subTest(path=path):
                old = (self.root / path).read_text() if (self.root / path).exists() else None
                self.write(path, (old or "") + "x")
                self.git("add", "-A", ".")
                with self.assertRaises(ValueError):
                    schema_changes(self.root, self.base, self.git("write-tree"))
                if old is None:
                    (self.root / path).unlink()
                else:
                    self.write(path, old)
                self.git("add", "-A", ".")

    def test_wrong_base_wrong_candidate_and_expanded_exemption_fail(self):
        self.manifest["base_commit"] = "0" * 40
        with self.assertRaises(ValueError):
            schema_changes(self.root, self.base, self.stage_manifest())
        self.manifest["base_commit"] = self.base
        self.manifest["candidate_tree_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            schema_changes(self.root, self.base, self.stage_manifest())
        self.manifest["non_schema_paths"].append("scripts/migrate_other.py")
        with self.assertRaises(ValueError):
            schema_changes(self.root, self.base, self.stage_manifest())

    def test_same_tree_after_squash_has_same_classification(self):
        self.git("commit", "-m", "candidate")
        commit = self.git("rev-parse", "HEAD")
        self.assertEqual(schema_changes(self.root, self.base, self.candidate),
                         schema_changes(self.root, self.base, commit))

    def test_without_manifest_general_schema_detection_is_unchanged(self):
        (self.root / MANIFEST).unlink()
        self.git("add", "-A", ".")
        result = schema_changes(self.root, self.base, self.git("write-tree"))
        self.assertIn("app/auth.py", result)
        self.assertIn("app/schema_migrations.py", result)

    def test_deploy_classification_precedes_schema_gate_and_update(self):
        source = (ROOT / "scripts/deploy.sh").read_text(encoding="utf-8")
        classify = source.index('schema_changed_files=')
        self.assertLess(classify, source.index('CATALOG_MIGRATION_REQUIRED=1'))
        self.assertLess(classify, source.index('DOMAIN_MIGRATION_REQUIRED=1'))
        self.assertLess(classify, source.index('APPLICATION UPDATE:'))
        self.assertIn('schema-changing deploy must update Recovery V2 contract', source)


class OptionalTasksRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.instance = self.project / "instance"
        self.instance.mkdir(parents=True)
        self.path = self.instance / "tasks-module.db"
        self.contract = {"contract_version": 2, "databases": {"required.db": ["sentinel"]},
                         "optional_databases": json.loads((ROOT / "ops/recovery-schema-contract.json")
                                                          .read_text())["optional_databases"],
                         "optional_database_contracts": json.loads((ROOT / "ops/recovery-schema-contract.json")
                                                                   .read_text())["optional_database_contracts"]}
        connection = sqlite3.connect(str(self.instance / "required.db"))
        connection.execute("CREATE TABLE sentinel(id INTEGER PRIMARY KEY)")
        connection.commit()
        connection.close()
        self.contract_path = self.project / "ops/recovery-schema-contract.json"
        self.contract_path.parent.mkdir()
        self.contract_path.write_text(json.dumps(self.contract), encoding="utf-8")
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.engine = RecoveryEngine(self.project, self.backups, "unused", self.contract_path,
                                     release_root=self.root / "releases", current_link=self.root / "current",
                                     maintenance_path=self.root / "maintenance", system_actions=False)

    def seed(self):
        migrate_database(self.path)
        users = {i: {"id": i, "role": "employee", "active": 1} for i in (1, 2)}
        now = lambda: "2026-09-27T20:59:59.123456+00:00"
        repository = TasksRepository(self.path)
        projects = ProjectsService(repository, users.get, now=now)
        project = projects.create(users[1], {"name": "Recovery 日本語"})
        projects.mutate(users[1], project["id"], {"version": 1, "user_id": 2}, "member_add")
        tasks = TasksService(repository, users.get, now=now)
        normal = tasks.create(users[1], {"title": "Обычная 🚀", "assigned_to": 2,
                                        "deadline_date": "2026-10-01", "project_id": project["id"]})
        tasks.mutate(users[1], normal["id"], {"version": 1, "title": "Версия 2"})
        tasks.create_micro(users[1], {"title": "24h", "assigned_to": 2})

    def snapshot(self, path):
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return {table: connection.execute('SELECT * FROM "' + table + '" ORDER BY rowid').fetchall()
                    for table in self.contract["optional_databases"]["tasks-module.db"]}
        finally:
            connection.close()

    def archive(self):
        staging = self.root / "backup-staging"
        staging.mkdir()
        _copy_runtime_data(self.project, staging)  # Real SQLite backup, not file copy of an open DB.
        target = self.backups / "copy.tar.gz"
        with tarfile.open(str(target), "w:gz") as archive:
            archive.add(str(staging / "instance"), arcname="instance")
        return target

    def test_absent_is_optional_and_backup_does_not_create_or_import_it(self):
        with mock.patch("app.tasks.repository.validate_connection", side_effect=AssertionError("unexpected")):
            manifest = inspect_instance(self.instance, self.contract)
            self.assertNotIn("tasks-module.db", manifest)
            archive = self.archive()
        with tarfile.open(str(archive)) as saved:
            self.assertFalse(any("tasks-module.db" in item.name for item in saved))
        self.assertFalse(self.path.exists())
        missing_required = self.root / "missing-required"
        missing_required.mkdir()
        with self.assertRaises(RecoveryError):
            inspect_instance(missing_required, self.contract)

    def test_present_roundtrip_preserves_every_record_and_status(self):
        self.seed()
        before = self.snapshot(self.path)
        original_bytes = self.path.read_bytes()
        archive = self.archive()
        with tarfile.open(str(archive)) as saved:
            self.assertEqual([item.name for item in saved].count("instance/tasks-module.db"), 1)
        service = BackupAdminService(self.project, self.backups, "unused", remote_check=False,
                                     recovery_contract=self.contract_path)
        contract_hash, manifest, files = service._capture_recovery_metadata(archive)
        self.assertIn("tasks-module.db", manifest)
        metadata = {"database_manifest": manifest, "file_manifest": files}
        restored = self.engine._safe_extract(archive, self.root / "restored")
        self.assertEqual(inspect_instance(restored, self.contract), metadata["database_manifest"])
        self.assertEqual(inspect_file_manifest(restored), metadata["file_manifest"])
        restored_path = restored / "tasks-module.db"
        self.assertEqual(TasksRepository(restored_path).status()["schema_version"], 4)
        self.assertEqual(self.snapshot(restored_path), before)
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(before["tasks"][0][12], 2)  # Actual mutation version survived.
        self.assertTrue(before["task_inbox_events"])
        self.assertTrue(before["task_project_members"])
        self.assertTrue(before["task_activity"])
        self.assertTrue(contract_hash)

    @unittest.skipIf(os.name == "nt", "Recovery operation journal requires POSIX fchmod")
    def test_actual_recovery_staging_validates_optional_database(self):
        self.seed()
        before = self.snapshot(self.path)
        archive = self.archive()
        service = BackupAdminService(self.project, self.backups, "unused", remote_check=False,
                                     recovery_contract=self.contract_path)
        _, manifest, files = service._capture_recovery_metadata(archive)
        _, restored = self.engine._stage_backup({"id": "a" * 32}, {"_path": archive},
                                              {"database_manifest": manifest, "file_manifest": files}, self.contract)
        self.assertEqual(self.snapshot(restored / "tasks-module.db"), before)

    def test_present_corrupt_or_wrong_schema_is_never_skipped(self):
        self.path.write_bytes(b"not sqlite")
        with self.assertRaises(RecoveryError):
            inspect_instance(self.instance, self.contract, require_all=False)
        self.path.unlink()
        connection = sqlite3.connect(str(self.path))
        connection.execute("CREATE TABLE wrong(id INTEGER)")
        connection.close()
        with self.assertRaises(RecoveryError):
            inspect_instance(self.instance, self.contract)

    def test_contract_detects_constraints_types_defaults_indexes_and_ledger(self):
        from app.tasks.schema import CORE_DDL, LEDGER_DDL
        defects = (("status TEXT NOT NULL", "status TEXT"),
                   ("created_by INTEGER", "created_by TEXT"),
                   ("DEFAULT 'new'", "DEFAULT 'waiting'"),
                   ("CHECK(version>0)", ""),
                   ("id INTEGER PRIMARY KEY", "id INTEGER"),
                   (" REFERENCES tasks(id)", ""),
                   ("ON tasks(assigned_to,deleted_at,deadline_date,id)", "ON tasks(assigned_to,id)"))
        for i, (before, after) in enumerate(defects):
            with self.subTest(defect=before):
                directory = self.root / ("drift-" + str(i))
                directory.mkdir()
                path = directory / "tasks-module.db"
                connection = sqlite3.connect(str(path))
                connection.execute(LEDGER_DDL)
                statements = list(CORE_DDL)
                position = next(j for j, sql in enumerate(statements) if before in sql)
                statements[position] = statements[position].replace(before, after, 1)
                for sql in statements:
                    connection.execute(sql)
                connection.executemany(
                    "INSERT INTO tasks_module_migrations VALUES(?,?,'now','fixture')",
                    self.contract["optional_database_contracts"]["tasks-module.db"]["migration_ledger"])
                connection.commit()
                connection.close()
                original = path.read_bytes()
                with self.assertRaises(RecoveryError) as failure:
                    inspect_instance(directory, self.contract, require_all=False)
                self.assertIn("schema differs", str(failure.exception))
                self.assertEqual(original, path.read_bytes())
        self.seed()
        connection = sqlite3.connect(str(self.path))
        connection.execute("UPDATE tasks_module_migrations SET signature='wrong' WHERE version=4")
        connection.commit()
        connection.close()
        with self.assertRaises(RecoveryError):
            inspect_instance(self.instance, self.contract)

    def test_selected_contract_validates_without_current_tasks_implementation(self):
        self.seed()
        with mock.patch("app.tasks.repository.validate_connection", side_effect=AssertionError("current code")):
            self.assertIn("tasks-module.db", inspect_instance(self.instance, self.contract))
        newer_contract = json.loads(json.dumps(self.contract))
        newer_contract["optional_database_contracts"]["tasks-module.db"]["schema_fingerprint"] = "0" * 64
        with self.assertRaises(RecoveryError):
            inspect_instance(self.instance, newer_contract)

    def test_old_contract_ignores_extra_tasks_database_without_touching_it(self):
        self.seed()
        original = self.path.read_bytes()
        old_contract = {"contract_version": 2, "databases": self.contract["databases"]}
        self.assertEqual(set(inspect_instance(self.instance, old_contract)), {"required.db"})
        self.assertEqual(original, self.path.read_bytes())

    def test_foreign_key_corruption_is_detected_read_only(self):
        self.seed()
        connection = sqlite3.connect(str(self.path))
        connection.execute("UPDATE tasks SET project_id=999 WHERE task_type='normal'")
        connection.commit()
        connection.close()
        before = self.path.read_bytes()
        with self.assertRaises(RecoveryError):
            inspect_instance(self.instance, self.contract)
        self.assertEqual(before, self.path.read_bytes())

    def test_backup_with_active_reader_is_consistent_and_restored_db_is_writable(self):
        self.seed()
        before = self.snapshot(self.path)
        reader = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT * FROM tasks").fetchone()
            archive = self.archive()
        finally:
            reader.close()
        restored = self.engine._safe_extract(archive, self.root / "restored-reader")
        target = restored / "tasks-module.db"
        self.assertEqual(self.snapshot(target), before)
        connection = sqlite3.connect(str(target), isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE tasks SET title='journal check' WHERE id=1")
            connection.rollback()
        finally:
            connection.close()
        self.assertEqual(self.snapshot(target), before)
        if os.name != "nt":
            self.assertEqual(target.stat().st_uid, os.geteuid())
            self.assertEqual(target.stat().st_mode & 0o777, self.path.stat().st_mode & 0o777)

    def test_optional_policy_cannot_weaken_other_databases(self):
        self.contract["optional_databases"]["catalog.db"] = []
        with self.assertRaises(RecoveryError):
            inspect_instance(self.instance, self.contract)

    def test_restore_can_recover_missing_optional_but_never_drop_present_one(self):
        absent = inspect_instance(self.instance, self.contract)
        self.seed()
        present = inspect_instance(self.instance, self.contract)
        self.assertTrue(self.engine._restore_manifest_compatible(present, absent, self.contract))
        self.assertFalse(self.engine._restore_manifest_compatible(absent, present, self.contract))
        self.assertTrue(self.engine._restore_manifest_compatible(present, present, self.contract))
        altered = dict(present, **{"unexpected.db": present["required.db"]})
        self.assertFalse(self.engine._restore_manifest_compatible(altered, absent, self.contract))

    @unittest.skipIf(os.name == "nt", "POSIX symlink check runs in Linux validation")
    def test_broken_symlink_is_not_optional_absence(self):
        self.path.symlink_to(self.instance / "missing.db")
        with self.assertRaises(RecoveryError):
            inspect_instance(self.instance, self.contract)


if __name__ == "__main__":
    unittest.main()
