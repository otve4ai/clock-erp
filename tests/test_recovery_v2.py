import fcntl
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.services.backup_admin import BackupAdminError, BackupAdminService, _atomic_json_write
from app.services.recovery_v2 import (
    RecoveryEngine,
    RecoveryError,
    _json_hash,
    create_operation_record,
    inspect_file_manifest,
    inspect_instance,
)
from scripts import clock_erp_recovery as recovery_helper


CONTRACT = {
    "contract_version": 2,
    "databases": {
        "auth.db": ["users", "auth_sessions"],
        "catalog.db": ["catalog_excel_products", "catalog_stock_movements", "erp_sales", "erp_receipts"],
        "customers.db": ["customers", "registry_meta"],
        "mail.db": ["mail_messages", "mail_threads"],
        "orders.db": ["orders_snapshot", "orders_snapshot_meta"],
        "purchases.db": ["purchase_requests", "supplier_orders"],
        "services.db": ["services", "service_accounts"],
        "sms.db": ["sms_messages", "sms_meta"],
        "tasks.db": ["tasks", "task_history"],
    },
}


class RecoveryV2Test(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.instance = self.project / "instance"
        self.backups = self.root / "backups"
        self.contract = self.project / "ops" / "recovery-schema-contract.json"
        self.release_root = self.root / "releases"
        self.current_link = self.root / "current"
        self.maintenance = self.root / "maintenance.json"
        self.contract.parent.mkdir(parents=True)
        self.contract.write_text(json.dumps(CONTRACT), encoding="utf-8")
        self.instance.mkdir()
        self.backups.mkdir()
        self._create_instance(self.instance, "current")
        (self.project / ".gitignore").write_text("instance/\n", encoding="utf-8")
        (self.project / "app.txt").write_text("v1", encoding="utf-8")
        (self.project / "requirements.txt").write_text("flask\n", encoding="utf-8")
        self._git("init", "-b", "main")
        self._git("config", "user.email", "recovery-test@example.com")
        self._git("config", "user.name", "Recovery Test")
        self._git(
            "add", ".gitignore", "app.txt", "requirements.txt",
            "ops/recovery-schema-contract.json",
        )
        self._git("commit", "-m", "release one")
        self.commit_one = self._git("rev-parse", "HEAD").stdout.strip()
        (self.project / "app.txt").write_text("v2", encoding="utf-8")
        self._git("add", "app.txt")
        self._git("commit", "-m", "release two")
        self.commit_two = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("update-ref", "refs/remotes/origin/main", self.commit_two)
        release_two = self.release_root / self.commit_two
        release_two.mkdir(parents=True)
        (release_two / ".erp-release.json").write_text(
            json.dumps({"commit": self.commit_two}), encoding="utf-8"
        )
        (release_two / "requirements.txt").write_text("flask\n", encoding="utf-8")
        os.symlink(str(release_two), str(self.current_link))
        self.service = BackupAdminService(
            self.project, self.backups, "/bin/false", remote_check=False,
            recovery_helper="/usr/bin/true", recovery_contract=self.contract,
            current_release=self.current_link,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _git(self, *args):
        return subprocess.run(
            ["git"] + list(args), cwd=str(self.project), check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )

    @staticmethod
    def _create_instance(root, marker):
        root.mkdir(parents=True, exist_ok=True)
        for name, tables in CONTRACT["databases"].items():
            connection = sqlite3.connect(str(root / name))
            for table in tables:
                connection.execute("CREATE TABLE {} (id INTEGER PRIMARY KEY)".format(table))
            connection.execute("CREATE TABLE recovery_payload (value TEXT)")
            connection.execute("INSERT INTO recovery_payload(value) VALUES (?)", (marker,))
            connection.commit()
            connection.close()
        (root / "settings.json").write_text(json.dumps({"marker": marker}), encoding="utf-8")

    def _make_backup(self, marker="backup", metadata=True, commit=None, mutate=None):
        source = self.root / ("source-" + marker)
        self._create_instance(source, marker)
        if mutate:
            mutate(source)
        daily = self.backups / "daily"
        daily.mkdir(exist_ok=True)
        path = daily / "clock-erp-daily-20260915-031701.tar.gz"
        with tarfile.open(str(path), "w:gz") as archive:
            archive.add(str(source), arcname="instance", recursive=True)
        backup = self.service.list_backups(public=False)[0]
        if metadata:
            contract_hash, contract = _json_hash(self.contract)
            manifest = inspect_instance(source, contract)
            _atomic_json_write(self.service._metadata_path(backup["backup_id"]), {
                "metadata_version": 2, "backup_id": backup["backup_id"],
                "timestamp": backup["timestamp"], "type": "manual",
                "size": path.stat().st_size, "git_commit": commit or self.commit_one,
                "git_branch": "main", "schema_versions": dict(
                    (name, item["user_version"]) for name, item in manifest.items()
                ),
                "database_manifest": manifest, "recovery_contract": contract_hash,
                "file_manifest": inspect_file_manifest(source),
                "integrity_status": "verified", "databases": sorted(manifest),
            })
        return self.service.list_backups(public=False)[0]

    def _operation(self, kind="data_restore", backup=None, target=None, key=None):
        operation, _created = create_operation_record(
            self.backups, kind, {"id": 1, "email": "owner@example.com"},
            backup_id=backup and backup["backup_id"], target_commit=target,
            idempotency_key=key,
        )
        return operation

    def _engine(self, failure=None, cls=RecoveryEngine):
        return cls(
            self.project, self.backups, "/bin/false", self.contract,
            release_root=self.release_root, current_link=self.current_link,
            maintenance_path=self.maintenance, system_actions=False,
            test_mode=bool(failure), failure_stage=failure,
        )

    def _payload(self):
        connection = sqlite3.connect(str(self.instance / "catalog.db"))
        value = connection.execute("SELECT value FROM recovery_payload").fetchone()[0]
        connection.close()
        return value

    def test_successful_data_restore_uses_verified_safety_and_staging(self):
        backup = self._make_backup()
        result = self._engine().run(self._operation(backup=backup)["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self._payload(), "backup")
        self.assertTrue(result["safety_backup_id"])
        safety = self.service.resolve_backup(result["safety_backup_id"])
        safety_metadata = json.loads(
            self.service._metadata_path(safety["backup_id"]).read_text(encoding="utf-8")
        )
        self.assertEqual(safety_metadata["type"], "pre_restore")
        self.assertEqual(safety_metadata["operation_id"], result["id"])
        self.assertEqual(safety_metadata["integrity_status"], "verified")
        self.assertFalse(self.maintenance.exists())
        self.assertEqual(
            [item["stage"] for item in result["progress"]],
            ["pending", "preflight", "safety_backup", "safety_check", "staging_restore",
             "staging_check", "maintenance", "production_restore", "service_restart",
             "health_check", "completed"],
        )
        audit = json.loads(self.service.audit_path.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(audit["operation_id"], result["id"])

    def test_corrupt_backup_is_blocked_before_production_change(self):
        backup = self._make_backup()
        backup["_path"].write_bytes(b"broken")
        result = self._engine().run(self._operation(backup=backup)["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self._payload(), "current")

    def test_traversal_archive_is_rejected_in_staging(self):
        backup = self._make_backup()
        with tarfile.open(str(backup["_path"]), "w:gz") as archive:
            info = tarfile.TarInfo("../outside")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        metadata = json.loads(self.service._metadata_path(backup["backup_id"]).read_text(encoding="utf-8"))
        metadata["size"] = backup["_path"].stat().st_size
        _atomic_json_write(self.service._metadata_path(backup["backup_id"]), metadata)
        result = self._engine().run(self._operation(backup=self.service.list_backups(public=False)[0])["id"])
        self.assertEqual(result["error_code"], "ARCHIVE_TRAVERSAL")
        self.assertFalse((self.root / "outside").exists())
        self.assertFalse((self.backups / "recovery" / "staging" / result["id"]).exists())

    def test_failure_after_service_stop_restarts_current_release(self):
        class Engine(RecoveryEngine):
            def _maintenance_on(self, operation, contract):
                super()._maintenance_on(operation, contract)
                self._service_stopped = True

            def _restart_service(self):
                self.restart_calls = getattr(self, "restart_calls", 0) + 1
                self._service_stopped = False

        backup = self._make_backup()
        engine = self._engine("production_restore", cls=Engine)
        result = engine.run(self._operation(backup=backup)["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(engine.restart_calls, 1)
        self.assertEqual(self._payload(), "current")
        self.assertFalse(self.maintenance.exists())

    def test_insufficient_space_blocks_preflight(self):
        backup = self._make_backup()
        usage = shutil._ntuple_diskusage(100, 99, 1)
        with mock.patch("app.services.recovery_v2.shutil.disk_usage", return_value=usage):
            result = self._engine().run(self._operation(backup=backup)["id"])
        self.assertEqual(result["error_code"], "INSUFFICIENT_SPACE")

    def test_safety_backup_failure_prevents_restore(self):
        backup = self._make_backup()
        class Engine(RecoveryEngine):
            def _create_safety_backup(self, operation, contract):
                raise RecoveryError("SAFETY_BACKUP_FAILED", "Safety backup failed")
        result = self._engine(cls=Engine).run(self._operation(backup=backup)["id"])
        self.assertEqual(result["error_code"], "SAFETY_BACKUP_FAILED")
        self.assertEqual(self._payload(), "current")

    def test_failed_safety_archive_check_is_never_marked_verified(self):
        backup = self._make_backup()

        class Engine(RecoveryEngine):
            def _create_safety_backup(self, operation, contract):
                path, metadata = super()._create_safety_backup(operation, contract)
                path.write_bytes(b"broken safety archive")
                return path, metadata

        result = self._engine(cls=Engine).run(self._operation(backup=backup)["id"])
        metadata = json.loads(
            self.service._metadata_path(result["safety_backup_id"]).read_text(encoding="utf-8")
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotEqual(metadata["integrity_status"], "verified")
        self.assertEqual(self._payload(), "current")

    def test_staging_sqlite_failure_prevents_restore(self):
        backup = self._make_backup()
        replacement = self.root / "invalid"
        replacement.write_bytes(b"not sqlite")
        with tarfile.open(str(backup["_path"]), "w:gz") as archive:
            archive.add(str(replacement), arcname="instance/catalog.db")
        metadata = json.loads(self.service._metadata_path(backup["backup_id"]).read_text(encoding="utf-8"))
        metadata["size"] = backup["_path"].stat().st_size
        _atomic_json_write(self.service._metadata_path(backup["backup_id"]), metadata)
        backup = self.service.list_backups(public=False)[0]
        result = self._engine().run(self._operation(backup=backup)["id"])
        self.assertIn(result["error_code"], ("DATABASE_MISSING", "DATABASE_UNREADABLE"))
        self.assertEqual(self._payload(), "current")

    def test_restart_failure_triggers_automatic_rollback(self):
        backup = self._make_backup()
        class Engine(RecoveryEngine):
            def _restart_service(self):
                if not getattr(self, "failed_once", False):
                    self.failed_once = True
                    raise RecoveryError("SERVICE_RESTART_FAILED", "restart failed", True)
        result = self._engine(cls=Engine).run(self._operation(backup=backup)["id"])
        self.assertEqual(result["automatic_rollback"]["result"], "completed")
        self.assertEqual(self._payload(), "current")
        recovery_log = (
            self.backups / "recovery" / "logs" / (result["id"] + ".jsonl")
        ).read_text(encoding="utf-8")
        self.assertIn('"event": "automatic_rollback"', recovery_log)
        self.assertIn('"details": "completed"', recovery_log)

    def test_health_failure_triggers_automatic_rollback(self):
        backup = self._make_backup()
        class Engine(RecoveryEngine):
            def _health(self, contract, operation=None):
                if not getattr(self, "failed_once", False):
                    self.failed_once = True
                    raise RecoveryError("HTTP_HEALTH_FAILED", "health failed", True)
                return super()._health(contract, operation)
        result = self._engine(cls=Engine).run(self._operation(backup=backup)["id"])
        self.assertEqual(result["automatic_rollback"]["result"], "completed")
        self.assertEqual(self._payload(), "current")

    def test_automatic_rollback_failure_is_critical(self):
        backup = self._make_backup()
        class Engine(RecoveryEngine):
            def _health(self, contract, operation=None):
                raise RecoveryError("HTTP_HEALTH_FAILED", "health failed", True)
            def _restore_previous_instance(self, operation):
                return False
        result = self._engine(cls=Engine).run(self._operation(backup=backup)["id"])
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["automatic_rollback"]["result"], "failed")

    def test_dirty_git_blocks_operation(self):
        backup = self._make_backup()
        (self.project / "app.txt").write_text("dirty", encoding="utf-8")
        result = self._engine().run(self._operation(backup=backup)["id"])
        self.assertEqual(result["error_code"], "DIRTY_GIT")

    def test_unknown_commit_is_blocked(self):
        result = self._engine().run(self._operation("code_rollback", target="f" * 40)["id"])
        self.assertEqual(result["error_code"], "COMMIT_NOT_FOUND")

    def test_changed_dependencies_block_code_rollback(self):
        (self.project / "requirements.txt").write_text("flask\nnew-package\n", encoding="utf-8")
        self._git("add", "requirements.txt")
        self._git("commit", "-m", "change dependencies")
        target = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("update-ref", "refs/remotes/origin/main", target)
        result = self._engine().run(self._operation("code_rollback", target=target)["id"])
        self.assertEqual(result["error_code"], "INCOMPATIBLE_RUNTIME")

    def test_incompatible_schema_is_blocked(self):
        backup = self._make_backup()
        metadata_path = self.service._metadata_path(backup["backup_id"])
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["database_manifest"]["catalog.db"]["schema_digest"] = "0" * 64
        _atomic_json_write(metadata_path, metadata)
        result = self._engine().run(self._operation(backup=backup)["id"])
        self.assertEqual(result["error_code"], "INCOMPATIBLE_SCHEMA")

    def test_parallel_operation_is_blocked(self):
        backup = self._make_backup()
        lock = (self.backups / ".backup-admin-operation.lock").open("a+")
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = self._engine().run(self._operation(backup=backup)["id"])
        finally:
            lock.close()
        self.assertEqual(result["status"], "failed")

    def test_stale_lock_file_does_not_block(self):
        backup = self._make_backup()
        (self.backups / ".backup-admin-operation.lock").touch()
        result = self._engine().run(self._operation(backup=backup)["id"])
        self.assertEqual(result["status"], "completed")

    def test_writer_successfully_stops(self):
        engine = self._engine()
        engine.system_actions = True
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(engine, "_run", return_value=completed) as run:
            engine._stop_writer_units()
        self.assertEqual(run.call_count, 4)
        self.assertTrue(all(call.args[0][:2] == ["systemctl", "stop"] for call in run.call_args_list))

    def test_writer_already_stopped_is_idempotent_success(self):
        engine = self._engine()
        engine.system_actions = True
        inactive = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(engine, "_run", return_value=inactive):
            engine._stop_writer_units()

    def test_writer_stop_timeout_reports_exact_lock_holder(self):
        lock_path = self.root / "busy-writer.lock"
        lock_path.touch()
        engine = RecoveryEngine(
            self.project, self.backups, "/bin/false", self.contract,
            release_root=self.release_root, current_link=self.current_link,
            maintenance_path=self.maintenance, system_actions=True,
            command_timeout=0,
        )
        holder = [{"pid": 26757, "command": "stale lock probe"}]
        with mock.patch("app.services.recovery_v2.WRITER_LOCKS", (str(lock_path),)), \
             mock.patch("app.services.recovery_v2.fcntl.flock", side_effect=BlockingIOError), \
             mock.patch.object(engine, "_lock_holders", return_value=holder):
            with self.assertRaises(RecoveryError) as raised:
                engine._acquire_writer_guards()
        self.assertEqual(raised.exception.code, "WRITER_BUSY")
        self.assertEqual(raised.exception.failure["component"], lock_path.name)
        self.assertIn("PID 26757", str(raised.exception))

    def test_writer_refuses_stop_preserves_command_diagnostics(self):
        engine = self._engine()
        engine.system_actions = True
        refused = subprocess.CompletedProcess([], 1, b"unit output", b"access denied")
        with mock.patch.object(engine, "_run", return_value=refused):
            with self.assertRaises(RecoveryError) as raised:
                engine._stop_writer_units()
        self.assertEqual(raised.exception.code, "WRITER_STOP_FAILED")
        self.assertEqual(raised.exception.failure["return_code"], 1)
        self.assertEqual(raised.exception.failure["stderr"], "access denied")

    def test_active_database_writer_blocks_barrier(self):
        engine = self._engine()
        engine.system_actions = True
        inactive = subprocess.CompletedProcess([], 3, b"inactive\n", b"")
        writer = sqlite3.connect(str(self.instance / "catalog.db"))
        writer.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch.object(engine, "_run", return_value=inactive):
                with self.assertRaises(RecoveryError) as raised:
                    engine._verify_write_barrier(CONTRACT)
        finally:
            writer.rollback()
            writer.close()
        self.assertEqual(raised.exception.code, "DATABASE_WRITER_ACTIVE")

    def test_recovery_does_not_swap_if_writer_remains_active(self):
        class Engine(RecoveryEngine):
            def _maintenance_on(self, operation, contract):
                super()._maintenance_on(operation, contract)
                raise RecoveryError("WRITER_STILL_ACTIVE", "writer active")

            def _swap_instance(self, operation, staged_instance):
                self.swap_called = True
                return super()._swap_instance(operation, staged_instance)

        backup = self._make_backup()
        engine = self._engine(cls=Engine)
        result = engine.run(self._operation(backup=backup)["id"])
        self.assertEqual(result["error_code"], "WRITER_STILL_ACTIVE")
        self.assertFalse(getattr(engine, "swap_called", False))
        self.assertEqual(self._payload(), "current")

    def test_writer_resumes_and_maintenance_clears_after_failed_recovery(self):
        class Engine(RecoveryEngine):
            def _maintenance_on(self, operation, contract):
                super()._maintenance_on(operation, contract)
                raise RecoveryError("WRITER_BUSY", "busy")

            def _maintenance_off(self):
                self.resume_calls = getattr(self, "resume_calls", 0) + 1
                return super()._maintenance_off()

        backup = self._make_backup()
        engine = self._engine(cls=Engine)
        result = engine.run(self._operation(backup=backup)["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(engine.resume_calls, 1)
        self.assertFalse(self.maintenance.exists())

    def test_writer_resumes_before_success_is_persisted(self):
        class Engine(RecoveryEngine):
            def _maintenance_off(self):
                self.resume_calls = getattr(self, "resume_calls", 0) + 1
                return super()._maintenance_off()

        backup = self._make_backup()
        engine = self._engine(cls=Engine)
        result = engine.run(self._operation(backup=backup)["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(engine.resume_calls, 1)
        self.assertFalse(self.maintenance.exists())

    def test_health_journal_uses_legacy_systemd_compatible_epoch(self):
        engine = self._engine()
        engine.system_actions = True
        engine._health_since_epoch = 1234.9
        response = mock.MagicMock()
        response.__enter__.return_value.getcode.return_value = 200
        response.__exit__.return_value = False
        completed = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch("app.services.recovery_v2.urlopen", return_value=response), \
             mock.patch.object(engine, "_run", return_value=completed) as run:
            engine._health(CONTRACT)
        self.assertIn("@1234", run.call_args.args[0])

    def test_release_replaces_tracked_instance_directory_with_runtime_link(self):
        bootstrap = self.instance / "navigation_settings.json"
        bootstrap.write_text("{}\n", encoding="utf-8")
        self._git("add", "-f", "instance/navigation_settings.json")
        self._git("commit", "-m", "tracked instance bootstrap")
        commit = self._git("rev-parse", "HEAD").stdout.strip()
        self._git("update-ref", "refs/remotes/origin/main", commit)

        release = self._engine()._create_release(commit)

        self.assertTrue((release / "instance").is_symlink())
        self.assertEqual(
            os.path.realpath(str(release / "instance")),
            os.path.realpath(str(self.instance)),
        )

    def test_successful_full_restore_switches_exact_release_and_data(self):
        backup = self._make_backup(commit=self.commit_one)
        result = self._engine().run(self._operation("full_restore", backup, self.commit_one)["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self._payload(), "backup")
        self.assertEqual(Path(os.path.realpath(str(self.current_link))).name, self.commit_one)

    def test_successful_code_rollback_switches_release_without_data_change(self):
        result = self._engine().run(
            self._operation("code_rollback", target=self.commit_one)["id"]
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self._payload(), "current")
        self.assertEqual(Path(os.path.realpath(str(self.current_link))).name, self.commit_one)

    def test_legacy_backup_without_metadata_is_blocked(self):
        backup = self._make_backup(metadata=False)
        status = self.service.status()
        item = next(value for value in status["backups"] if value["backup_id"] == backup["backup_id"])
        self.assertFalse(item["can_restore_data"])
        self.assertFalse(item["can_restore_system"])

    def test_idempotency_returns_same_operation(self):
        first, created = create_operation_record(
            self.backups, "data_restore", {"id": 1}, backup_id="a" * 24,
            idempotency_key="request-12345",
        )
        second, created_again = create_operation_record(
            self.backups, "data_restore", {"id": 1}, backup_id="a" * 24,
            idempotency_key="request-12345",
        )
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["id"], second["id"])

    def test_operation_state_survives_new_service_instance(self):
        operation = self._operation()
        reloaded = BackupAdminService(self.project, self.backups, "/bin/false")
        self.assertEqual(reloaded.operation_status()["id"], operation["id"])

    def test_helper_build_engine_returns_configured_engine(self):
        with mock.patch.object(recovery_helper, "SOURCE_ROOT", self.project), \
             mock.patch.dict(os.environ, {"ERP_RECOVERY_TEST_FAILURE": "preflight"}):
            engine = recovery_helper.build_engine()
        self.assertIsInstance(engine, RecoveryEngine)
        self.assertEqual(engine.project_root, self.project.resolve())
        self.assertEqual(engine.backup_root, (self.project / "instance" / "backups").resolve())
        self.assertEqual(engine.failure_stage, "preflight")

    def test_helper_result_is_safe_for_ascii_server_locale(self):
        payload = recovery_helper.serialize_result({"message": "Восстановление завершено"})
        payload.encode("ascii")
        self.assertEqual(
            json.loads(payload)["message"],
            "Восстановление завершено",
        )

    def test_new_operation_has_fresh_persistent_timestamp(self):
        operation = self._operation()
        status = self.service.operation_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["id"], operation["id"])
        self.assertGreater(status["updated_epoch"], time.time() - 10)

    def test_recent_persistent_heartbeat_prevents_false_stale_failure(self):
        operation = self._operation()
        operation["updated_epoch"] = 1
        _atomic_json_write(self.backups / ".backup-admin-operation.json", operation)
        heartbeat = (
            self.backups / "recovery" / "operations"
            / (operation["id"] + ".heartbeat.json")
        )
        _atomic_json_write(heartbeat, {
            "operation_id": operation["id"],
            "pid": 99999999,
            "updated_epoch": time.time(),
        })
        status = self.service.operation_status()
        self.assertTrue(status["active"])
        self.assertEqual(status["status"], "pending")

    def test_production_recovery_runs_in_separate_systemd_unit(self):
        operation_id = "a" * 32
        self.service.project_root = Path("/opt/clock-erp")
        self.service.recovery_helper = Path("/usr/local/sbin/clock-erp-recovery")
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch("app.services.backup_admin.shutil.which", return_value="/usr/bin/systemd-run"), \
             mock.patch("app.services.backup_admin.subprocess.run", return_value=completed) as run:
            self.service._launch_recovery(operation_id)
        command = run.call_args.args[0]
        self.assertIn("--unit=clock-erp-recovery-" + operation_id, command)
        self.assertIn("--service-type=simple", command)
        self.assertNotIn("--scope", command)
        self.assertEqual(command[-3:-1], ["run", operation_id])

    def test_stale_recovery_operation_becomes_visible_failure(self):
        operation = self._operation()
        operation["updated_epoch"] = 1
        _atomic_json_write(self.backups / ".backup-admin-operation.json", operation)
        status = self.service.operation_status()
        persisted = json.loads(
            (self.backups / "recovery" / "operations" / (operation["id"] + ".json"))
            .read_text(encoding="utf-8")
        )
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error_code"], "OPERATION_STALE")
        self.assertEqual(persisted["status"], "failed")

    def test_helper_launch_failure_is_persisted_and_not_left_active(self):
        backup = self._make_backup()
        with mock.patch.object(self.service, "_launch_recovery", side_effect=OSError("offline")):
            with self.assertRaises(BackupAdminError):
                self.service.start_recovery(
                    {"id": 1, "email": "owner@example.com"}, "data_restore",
                    backup_id=backup["backup_id"], idempotency_key="launch-failure-1",
                )
        operation = self.service.operation_status()
        self.assertFalse(operation["active"])
        self.assertEqual(operation.get("error_code"), "HELPER_LAUNCH_FAILED", operation)

    def test_failure_injection_after_swap_rolls_back(self):
        backup = self._make_backup()
        result = self._engine("after_production_swap").run(self._operation(backup=backup)["id"])
        self.assertEqual(result["automatic_rollback"]["result"], "completed")
        self.assertEqual(self._payload(), "current")

    def test_public_operation_status_does_not_expose_paths_or_secret(self):
        operation = self._operation()
        operation.update({
            "staging_path": "/secret/staging", "previous_instance": "/secret/instance",
            "message": "safe error", "internal_secret": "not-written-by-engine",
        })
        _atomic_json_write(self.backups / ".backup-admin-operation.json", operation)
        status = self.service.operation_status()
        self.assertNotIn("staging_path", status)
        self.assertNotIn("previous_instance", status)
        self.assertNotIn("/secret", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
