#!/usr/bin/env python3
"""Stages A/B.1: exact-runtime tests in an isolated source copy, never live ERP."""

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import sqlite3
import sys
import tempfile
import unittest
import weakref
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
STAGE_A = "a806ef287792524e730ff2074d3137eb7aee8664"
STAGE_B_BASE = "6d06b8f34381665aed37deeacb643ae18ab12d49"
STAGE_B1_BASE = "c5c1762785d0ce0f840da05f37456e0c3004e941"
STAGE_A_PYTHON = (
    "app/collaboration_schema.py", "app/navigation_badges.py", "app/schema_migrations.py",
    "app/services/collaboration.py", "app/services/tasks.py", "app/task_errors.py",
    "app/tasks/__init__.py", "app/tasks/error_boundary.py", "app/tasks/migrations.py",
    "app/tasks/permissions.py", "app/tasks/repository.py", "app/tasks/routes.py",
    "app/tasks/services.py", "app/tasks_boundary.py", "app/web.py",
    "scripts/migrate_tasks_module.py", "scripts/run_backend_tests.py",
    "tests/test_tasks_isolation.py", "tests/test_tasks_module.py",
    "app/auth.py", "app/tasks/domain.py", "tests/test_tasks_core.py", "tests/test_tasks_core_api.py",
    "app/tasks/schema.py", "tests/test_tasks_core_schema.py",
)
PATTERNS = (
    "test_tasks_isolation.py", "test_tasks_module.py", "test_tasks_runtime_compat.py",
    "test_tasks_core*.py",
    "test_tasks.py", "test_tasks_api.py", "test_collaboration*.py",
    "test_navigation_preferences.py", "test_orders_navigation_performance.py",
    "test_sidebar_visual_contract.py", "test_user_notifications.py",
)


def is_inside(path, directory):
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    arguments = parser.parse_args()
    if sys.version_info[:3] != (3, 6, 8) or sqlite3.sqlite_version != "3.7.17" or sys.platform != "linux":
        parser.error("requires Linux, Python 3.6.8 and SQLite 3.7.17; no modern-runtime fallback")
    if os.geteuid() == 0:
        parser.error("run as an unprivileged user in a disposable source copy")
    if not is_inside(ROOT, Path("/tmp")):
        parser.error("source copy must be under /tmp, never the live checkout")
    for parent in (ROOT,) + tuple(ROOT.parents):
        if (parent / ".env").exists():
            parser.error(".env found in source ancestry; refusing to import ERP")
    report_path = Path(arguments.report).resolve()
    if not is_inside(report_path, ROOT.parent):
        parser.error("report must remain inside the disposable workspace")
    os.chdir(str(ROOT))
    sys.path.insert(0, str(ROOT))

    from scripts.test_services import validate_environment
    validate_environment()
    if os.environ.get("ERP_TASKS_MODULE_DATABASE"):
        parser.error("ERP_TASKS_MODULE_DATABASE must be unset before the isolated runner")

    report = {
        "stage": "B.1", "stage_a_commit": STAGE_A, "stage_b_base": STAGE_B_BASE,
        "stage_b1_base": STAGE_B1_BASE,
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version, "platform": platform.platform(),
        "euid": os.geteuid(), "source_root": str(ROOT),
    }
    compiled = []
    source_hashes = {}
    for relative in STAGE_A_PYTHON + (
            "scripts/validate_tasks_runtime.py", "tests/test_tasks_runtime_compat.py", "tests/test_tasks.py"):
        path = ROOT / relative
        # Compile with the actual 3.6.8 interpreter, without writing pyc.
        # Missing files are an error, never a silently reduced validation.
        compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        compiled.append(relative)
        source_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    report["compiled_python_files"] = compiled
    report["source_sha256"] = source_hashes

    import flask
    import werkzeug
    import jinja2
    report.update(flask=flask.__version__, werkzeug=werkzeug.__version__, jinja=jinja2.__version__)
    from app.schema_migrations import validate_known_sql_compatibility
    report["sql_compatibility_files"] = validate_known_sql_compatibility(ROOT)

    original_connect = sqlite3.connect
    original_tempdir = tempfile.tempdir
    original_socket_connect = socket.socket.connect
    original_dns = socket.getaddrinfo
    original_discover = unittest.defaultTestLoader.discover
    original_run = unittest.TextTestRunner.run
    original_argv = sys.argv[:]
    stats = {"connections": 0, "attachments": 0, "denied_paths": []}
    outcomes = []

    def no_network(*args, **kwargs):
        raise OSError("runtime validation: all network access disabled")

    with tempfile.TemporaryDirectory(prefix="tasks-runtime-fixtures-") as temporary:
        fixture_root = Path(temporary).resolve()

        def check_database(database):
            raw = str(database)
            if raw == ":memory:":
                return
            path = Path(unquote(urlsplit(raw).path)) if raw.startswith("file:") else Path(raw)
            if not is_inside(path, fixture_root):
                stats["denied_paths"].append(raw)
                raise AssertionError("database outside synthetic fixtures: " + raw)

        class FixtureConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                self._checked_attach = False
                if re.match(r"\s*ATTACH\b", sql, re.I):
                    if not re.match(r"\s*ATTACH\s+(?:DATABASE\s+)?\?\s+AS\s+\w+\s*$", sql, re.I):
                        raise AssertionError("fixture runner accepts only parameterized ATTACH")
                    check_database(parameters[0])
                    self._checked_attach = True
                try:
                    return super().execute(sql, parameters)
                finally:
                    self._checked_attach = False

            def set_authorizer(self, callback):
                connection_ref = weakref.ref(self)

                def authorize(action, first, second, database, trigger):
                    if action == sqlite3.SQLITE_ATTACH:
                        # SQLite 3.7.17 authorizes ATTACH before binding '?' and
                        # reports None. Validate the bound path in execute first.
                        if first is not None:
                            check_database(first)
                        elif not getattr(connection_ref(), "_checked_attach", False):
                            raise AssertionError("ATTACH path was not validated")
                        stats["attachments"] += 1
                    return callback(action, first, second, database, trigger) if callback else sqlite3.SQLITE_OK
                return super().set_authorizer(authorize)

        def guarded_connect(database, *args, **kwargs):
            check_database(database)
            stats["connections"] += 1
            kwargs.setdefault("factory", FixtureConnection)
            connection = original_connect(database, *args, **kwargs)
            connection.set_authorizer(None)
            return connection

        def discover(start_directory, pattern="test*.py", **kwargs):
            suite = unittest.TestSuite()
            for selected in PATTERNS:
                selected_suite = original_discover(start_directory, pattern=selected, **kwargs)
                if not selected_suite.countTestCases():
                    raise AssertionError("required test pattern is empty: " + selected)
                suite.addTests(selected_suite)
            return suite

        def record_result(runner, suite):
            result = original_run(runner, suite)
            outcomes.append({"tests": result.testsRun, "failures": len(result.failures),
                             "errors": len(result.errors), "skipped": len(result.skipped)})
            return result

        try:
            tempfile.tempdir = str(fixture_root)
            sqlite3.connect = guarded_connect
            socket.socket.connect = no_network
            socket.getaddrinfo = no_network
            unittest.defaultTestLoader.discover = discover
            unittest.TextTestRunner.run = record_result
            sys.argv = ["run_backend_tests.py", "--verbosity", "2"]
            from scripts import run_backend_tests
            exit_code = run_backend_tests.main()
            report["tests"] = outcomes
            report["database_guard"] = stats
            report["status"] = "passed" if exit_code == 0 and not stats["denied_paths"] else "failed"
            runtime_tests = sys.modules.get("test_tasks_runtime_compat")
            report["runtime_evidence"] = getattr(runtime_tests, "EVIDENCE", {})
        finally:
            sqlite3.connect = original_connect
            tempfile.tempdir = original_tempdir
            socket.socket.connect = original_socket_connect
            socket.getaddrinfo = original_dns
            unittest.defaultTestLoader.discover = original_discover
            unittest.TextTestRunner.run = original_run
            sys.argv = original_argv
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
