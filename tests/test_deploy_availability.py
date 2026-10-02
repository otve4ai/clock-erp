import subprocess
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = PROJECT_ROOT / "scripts" / "deploy.sh"


class DeployAvailabilityTest(unittest.TestCase):
    def test_deploy_script_is_valid_shell(self):
        completed = subprocess.run(
            ["bash", "-n", str(DEPLOY_SCRIPT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_remote_deploy_script_is_valid_shell(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        marker = "<<'REMOTE_SCRIPT'\n"
        remote = script.split(marker, 1)[1].rsplit("\nREMOTE_SCRIPT", 1)[0]
        completed = subprocess.run(
            ["bash", "-n"],
            input=remote,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_code_only_deploy_uses_full_service_restart(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('CATALOG_MIGRATION_REQUIRED=0', script)
        self.assertIn(
            'systemctl restart "$SERVICE_NAME"',
            script,
        )
        self.assertNotIn(
            'systemctl kill --kill-who=main --signal=HUP "$SERVICE_NAME"',
            script,
        )
        service_start = script.index("SERVICE START: controlled full restart")
        restart = script.index('systemctl restart "$SERVICE_NAME"', service_start)
        health_check = script.index("HEALTH CHECK: public routes", service_start)
        self.assertLess(restart, health_check)

    def test_database_migration_is_blocked_during_active_inventory(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "SELECT COUNT(*) FROM erp_inventory_sessions WHERE status = 'active';",
            script,
        )
        self.assertIn("DEPLOY_BLOCKED:", script)
        self.assertIn("DEPLOY_BLOCKED_DETAILS:", script)
        self.assertIn("SELECT s.id, COUNT(i.id) AS item_count", script)
        self.assertIn("COUNT(i.id)", script)
        self.assertIn(
            'if [[ "$CATALOG_MIGRATION_REQUIRED" == "1" && -f instance/catalog.db ]]',
            script,
        )
        self.assertIn("scripts/migration_preflight.py", script)
        self.assertNotIn("scripts/consolidate_global_categories.py", script)

    def test_deploy_does_not_create_backups_and_keeps_disk_guard(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('readonly MAX_BACKUP_DISK_USAGE=85', script)
        self.assertIn("check_backup_disk_usage", script)
        self.assertNotIn("--create-daily", script)
        self.assertNotIn("--create-temporary", script)
        self.assertNotIn('$BACKUP_DIR/temporary', script)

    def test_failed_preflight_stops_before_application_update_and_restart(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        preflight = script.index("MIGRATION PREFLIGHT: stage release")
        migration_command = script.index("migration_preflight.py\" preflight")
        application_update = script.index("APPLICATION UPDATE: fast-forward")
        merge = script.index('git merge --ff-only "$FETCHED_COMMIT"')
        service_stop = script.index('systemctl stop "$SERVICE_NAME"', merge)
        self.assertLess(preflight, migration_command)
        self.assertLess(migration_command, application_update)
        self.assertLess(application_update, merge)
        self.assertLess(merge, service_stop)

    def test_production_migration_is_service_stopped_and_data_guarded(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        production = script.index("PRODUCTION MIGRATION: stop service")
        stop = script.index('systemctl stop "$SERVICE_NAME"', production)
        backup = script.index("CATALOG_ROLLBACK_BACKUP=", production)
        apply = script.index("migration_preflight.py apply", production)
        compare = script.index('DATA_SNAPSHOT_BEFORE" != "$DATA_SNAPSHOT_AFTER', production)
        start = script.index("SERVICE START: controlled full restart", production)
        self.assertLess(stop, backup)
        self.assertLess(backup, apply)
        self.assertLess(apply, compare)
        self.assertLess(compare, start)

    def test_legacy_migration_script_changes_are_fail_closed(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("UNREGISTERED_MIGRATION_CHANGE=1", script)
        self.assertIn(
            "changed legacy migration script is not registered in production preflight",
            script,
        )

    def test_production_python_only_compiles_runtime_sources(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"$PYTHON_BIN" -m compileall -q app scripts', script)
        self.assertNotIn('"$PYTHON_BIN" -m compileall -q app scripts tests', script)

    def test_services_smoke_uses_a_production_available_utf8_locale(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'LC_ALL=en_US.utf8 LANG=en_US.utf8 "$PYTHON_BIN"',
            script,
        )

    def test_recovery_runtime_keeps_system_tools_on_service_path(self):
        dropin = (PROJECT_ROOT / "ops" / "clock-erp-recovery.conf").read_text(
            encoding="utf-8"
        )
        self.assertIn("/opt/clock-erp/venv/bin", dropin)
        self.assertIn("/usr/bin", dropin)
        self.assertIn("/usr/sbin", dropin)

    def test_services_vault_preflight_is_fail_closed_before_code_update(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        vault_preflight = script.index("SERVICES VAULT PREFLIGHT: protected key")
        application_update = script.index("APPLICATION UPDATE: fast-forward")
        smoke = script.index("scripts/services_production_smoke.py")
        self.assertLess(vault_preflight, application_update)
        self.assertLess(application_update, smoke)
        self.assertIn("scripts/service_vault_preflight.py", script)
        self.assertIn('systemctl show "$SERVICE_NAME" -p EnvironmentFiles', script)
        self.assertIn('--database "$PROJECT_DIR/instance/services.db"', script)
        self.assertIn("SERVICE_VAULT_PROCESS_KEY_OK", script)
        self.assertNotIn("os.urandom", script)
        self.assertNotIn("SERVICE_VAULT_KEY_CREATED", script)
        self.assertNotIn("printf '%s' \"$SERVICE_VAULT_KEY\"", script)

    def test_deploy_uses_run_scoped_workdir_and_removes_it(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('mktemp -d /run/clock-erp-deploy.XXXXXX', script)
        self.assertIn('cleanup_workdir', script)
        self.assertIn('rm -rf -- "$DEPLOY_WORKDIR"', script)
        self.assertNotIn('BACKUP_TOOL_SOURCE', script)
        self.assertNotIn('deploy-safety-', script)
        self.assertNotIn('production-migration-XXXXXX', script)

    def test_services_smoke_covers_credentials_cleanup_and_data_guard(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("scripts/services_production_smoke.py", script)
        self.assertIn("ERP_PRODUCTION_SERVICES_SMOKE=confirmed", script)
        self.assertIn("DATA_BEFORE=", script)
        self.assertIn("DATA_AFTER=", script)
        self.assertIn("POST-SMOKE DATA SAFETY", script)
        smoke = (PROJECT_ROOT / "scripts" / "services_production_smoke.py").read_text(
            encoding="utf-8"
        )
        for stage in (
            'stage = "page"', 'stage = "create"',
            'stage = "masked-list"', 'stage = "reveal"',
            'stage = "update"', 'stage = "archive"', 'stage = "log-security"',
        ):
            self.assertIn(stage, smoke)
        self.assertIn("object_label_snapshot,object_secondary_snapshot", smoke)
        self.assertIn('connection.execute("DELETE FROM services', smoke)
        self.assertIn('"DELETE FROM erp_audit_events', smoke)
        self.assertIn('cleanup_audit("instance/catalog.db")', smoke)
        self.assertIn("secrets.token_urlsafe", smoke)
        self.assertIn("app.config.update(TESTING=True, AUTH_TESTING=True)", smoke)

    def test_release_replaces_tracked_instance_directory_with_runtime_link(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        release = script.index('APPLICATION_RELEASE="$RELEASE_ROOT/$CURRENT_COMMIT"')
        remove_instance = script.index('rm -rf -- "$release_pending/instance"', release)
        link_instance = script.index(
            'ln -s "$PROJECT_DIR/instance" "$release_pending/instance"', release
        )
        self.assertLess(remove_instance, link_instance)
        self.assertIn("RELEASE_RUNTIME_QUARANTINED", script)

    def test_customer_backfill_does_not_expand_an_empty_array_on_bash_42(self):
        script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('customer_rebuild_argument[@]', script)
        self.assertIn('backfill_customers.py --apply --rebuild --backup-dir', script)
        self.assertIn('backfill_customers.py --apply --backup-dir', script)


if __name__ == "__main__":
    unittest.main()
