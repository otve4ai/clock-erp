#!/usr/bin/env python3
"""Run the isolated local checks for the Working Services section."""

from __future__ import print_function

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_SERVICES_DATABASE = (PROJECT_ROOT / "instance" / "services.db").resolve()
DATABASE_ENVIRONMENT = (
    "CATALOG_DATABASE_PATH",
    "ERP_AUTH_DATABASE",
    "ORDERS_DATABASE_PATH",
    "ERP_TASKS_DATABASE",
    "ERP_PURCHASES_DATABASE",
    "CUSTOMERS_DATABASE_PATH",
    "ERP_SMS_DATABASE",
    "ERP_MAIL_DATABASE",
    "ERP_SERVICES_DATABASE",
)
EXTERNAL_ENVIRONMENT = (
    "MOYSKLAD_TOKEN",
    "BITRIX_LOGIN",
    "BITRIX_PASSWORD",
    "BITRIX_EXCHANGE_URL",
    "BITRIX_CATALOG_URL",
    "BITRIX_CATALOG_TOKEN",
    "BITRIX_ORDERS_TOKEN",
    "BITRIX_ORDER_COMMENTS_TOKEN",
    "UPDATE_ORDER_STATUS_TOKEN",
    "WB_API_TOKEN",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMSBLISS_LOGIN",
    "SMSBLISS_PASSWORD",
)
PROFILE_PATTERN = "test_service*.py" if os.name != "nt" else "test_services*.py"
PROFILE_MODULES = (
    "tests/test_services_ui_contract.py",
    "tests/test_services_vault.py",
) + (() if os.name == "nt" else ("tests/test_service_vault_preflight.py",))


class UnsafeTestEnvironment(RuntimeError):
    pass


def _nonempty(name):
    return bool(str(os.environ.get(name, "")).strip())


def validate_environment():
    if (PROJECT_ROOT / ".env").is_file():
        raise UnsafeTestEnvironment("repository .env is present")

    flask_environment = str(os.environ.get("FLASK_ENV", "")).strip().casefold()
    if flask_environment and flask_environment != "testing":
        raise UnsafeTestEnvironment("FLASK_ENV must be empty or 'testing'")

    test_mode = str(os.environ.get("ERP_TEST_MODE", "")).strip()
    if test_mode and test_mode != "1":
        raise UnsafeTestEnvironment("ERP_TEST_MODE must be empty or '1'")

    configured_databases = [name for name in DATABASE_ENVIRONMENT if _nonempty(name)]
    if configured_databases:
        raise UnsafeTestEnvironment(
            "database environment must be unset: {}".format(", ".join(configured_databases))
        )

    configured_integrations = [name for name in EXTERNAL_ENVIRONMENT if _nonempty(name)]
    if configured_integrations:
        raise UnsafeTestEnvironment(
            "external credentials/endpoints must be unset: {}".format(
                ", ".join(configured_integrations)
            )
        )


def configure_isolated_environment(test_root):
    database_paths = {
        "CATALOG_DATABASE_PATH": test_root / "catalog.db",
        "ERP_AUTH_DATABASE": test_root / "auth.db",
        "ORDERS_DATABASE_PATH": test_root / "orders.db",
        "ERP_TASKS_DATABASE": test_root / "tasks.db",
        "ERP_PURCHASES_DATABASE": test_root / "purchases.db",
        "CUSTOMERS_DATABASE_PATH": test_root / "customers.db",
        "ERP_SMS_DATABASE": test_root / "sms.db",
        "ERP_MAIL_DATABASE": test_root / "mail.db",
        "ERP_SERVICES_DATABASE": test_root / "services.db",
    }
    services_database = database_paths["ERP_SERVICES_DATABASE"].resolve()
    if services_database == PRODUCTION_SERVICES_DATABASE:
        raise UnsafeTestEnvironment("temporary services database resolved to production path")
    if PROJECT_ROOT in services_database.parents:
        raise UnsafeTestEnvironment("temporary services database is inside the repository")

    os.environ.update({name: str(path) for name, path in database_paths.items()})
    os.environ.update({
        "FLASK_ENV": "testing",
        "ERP_TEST_MODE": "1",
        "ERP_TEST_ROOT": str(test_root),
        "ERP_MAIL_ATTACHMENT_ROOT": str(test_root / "mail-attachments"),
        "ERP_MAIL_SECRET_KEY": "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        "ERP_SECRET_KEY": "isolated-services-test-secret",
        "SERVICE_VAULT_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "TEMP": str(test_root),
        "TMP": str(test_root),
    })
    for name in EXTERNAL_ENVIRONMENT:
        os.environ[name] = ""


def run_backend_profile():
    arguments = [
        "run_backend_tests.py",
        "--start-directory", "tests",
        "--pattern", PROFILE_PATTERN,
        "--verbosity", "2",
    ]
    runner = PROJECT_ROOT / "scripts" / "run_backend_tests.py"
    if os.name != "nt":
        completed = subprocess.run(
            [sys.executable, str(runner)] + arguments[1:],
            cwd=str(PROJECT_ROOT), env=dict(os.environ), check=False,
        )
        return completed.returncode

    bootstrap = """
import asyncio
import os
import runpy
import sys
import tempfile
import types

fcntl = types.ModuleType('fcntl')
fcntl.LOCK_EX = 2
fcntl.LOCK_NB = 4
fcntl.LOCK_UN = 8
fcntl.flock = lambda _handle, _operation: None
sys.modules['fcntl'] = fcntl
if not hasattr(os, 'getuid'):
    os.getuid = lambda: 0

original_temporary_directory = tempfile.TemporaryDirectory
def windows_temporary_directory(*args, **kwargs):
    kwargs.setdefault('ignore_cleanup_errors', True)
    return original_temporary_directory(*args, **kwargs)
tempfile.TemporaryDirectory = windows_temporary_directory

sys.argv = {arguments}
runpy.run_path({runner}, run_name='__main__')
""".format(
        arguments=json.dumps(arguments),
        runner=repr(str(runner)),
    )
    completed = subprocess.run(
        [sys.executable, "-c", bootstrap],
        cwd=str(PROJECT_ROOT), env=dict(os.environ), check=False,
    )
    return completed.returncode


def run_browser_profile_if_available():
    frontend = PROJECT_ROOT / "frontend"
    playwright = frontend / "node_modules" / "@playwright" / "test"
    pnpm = shutil.which("pnpm") or shutil.which("pnpm.cmd")
    if not playwright.is_dir() or not pnpm:
        print("BROWSER: NOT RUN - local Playwright dependencies are not installed; CI remains required.")
        return 0
    if os.name == "nt":
        print("BROWSER: NOT RUN - the existing services Playwright webServer command is POSIX-only.")
        return 0
    print("BROWSER: pnpm run test:e2e:services")
    completed = subprocess.run(
        [pnpm, "run", "test:e2e:services"],
        cwd=str(frontend),
        env=dict(os.environ),
        check=False,
    )
    return completed.returncode


def run_failure_probe():
    import unittest

    class DeliberateFailure(unittest.TestCase):
        def runTest(self):
            self.fail("deliberate safe failure probe")

    result = unittest.TextTestRunner(verbosity=2).run(DeliberateFailure())
    return 0 if result.wasSuccessful() else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--self-test-failure",
        action="store_true",
        help="run an in-memory failing unittest to verify non-zero propagation",
    )
    arguments = parser.parse_args()

    try:
        validate_environment()
    except UnsafeTestEnvironment as error:
        print("TEST-SERVICES REFUSED: {}".format(error), file=sys.stderr)
        return 2

    print("TEST-SERVICES")
    print("Environment: isolated testing")
    print("External network: blocked by scripts/run_backend_tests.py")
    print("Tests:")
    for module in PROFILE_MODULES:
        print("  - {}".format(module))
    if os.name == "nt":
        print("  - tests/test_service_vault_preflight.py: NOT RUN (POSIX permissions only)")

    with tempfile.TemporaryDirectory(prefix="vechasu-test-services-") as root:
        test_root = Path(root).resolve()
        try:
            configure_isolated_environment(test_root)
        except UnsafeTestEnvironment as error:
            print("TEST-SERVICES REFUSED: {}".format(error), file=sys.stderr)
            return 2
        print("Services database: temporary ({})".format(test_root.name))
        if arguments.self_test_failure:
            return run_failure_probe()
        backend_exit = run_backend_profile()
        if backend_exit != 0:
            return backend_exit
        return run_browser_profile_if_available()


if __name__ == "__main__":
    raise SystemExit(main())
