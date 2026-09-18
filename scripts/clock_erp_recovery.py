#!/usr/bin/env python3
"""Fixed-scope server recovery entry point; usable when ERP is unavailable."""

import argparse
import fcntl
import hashlib
import json
import os
import sys
import traceback
from pathlib import Path


SOURCE_ROOT = Path("/opt/clock-erp")
if SOURCE_ROOT.is_dir():
    sys.path.insert(0, str(SOURCE_ROOT))
else:
    SOURCE_ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(SOURCE_ROOT))

from app.services.recovery_v2 import (  # noqa: E402
    OPERATION_ID_RE, RecoveryEngine, RecoveryError, create_operation_record,
)


def build_engine():
    production = SOURCE_ROOT == Path("/opt/clock-erp")
    backup_root = Path("/opt/clock-erp-backups") if production else SOURCE_ROOT / "instance" / "backups"
    backup_script = (
        Path("/usr/local/sbin/clock-erp-backup-retention")
        if production else SOURCE_ROOT / "scripts" / "retain_erp_backups.py"
    )
    failure = os.environ.get("ERP_RECOVERY_TEST_FAILURE") if not production else None
    return RecoveryEngine(
        SOURCE_ROOT, backup_root, backup_script,
        (Path("/opt/clock-erp-current") if production else SOURCE_ROOT)
        / "ops" / "recovery-schema-contract.json",
        test_mode=not production and bool(failure), failure_stage=failure,
    )


def create_console_operation(engine, kind, backup_id, target_commit, idempotency_key):
    """Serialize CLI admission so a second request cannot hide an active operation."""
    engine.backup_root.mkdir(parents=True, exist_ok=True)
    guard = engine.operation_lock.open("a+")
    try:
        fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
        current = engine.backups.operation_status()
        if current.get("active"):
            if idempotency_key:
                digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
                request_path = (
                    engine.backup_root / "recovery" / "operations"
                    / ("request-" + digest + ".json")
                )
                try:
                    with request_path.open("r", encoding="utf-8") as stream:
                        request = json.load(stream)
                except (OSError, ValueError, TypeError):
                    request = {}
                if request.get("operation_id") == current.get("id"):
                    return current, False
            raise RecoveryError("OPERATION_BUSY", "Другая recovery-операция уже выполняется")
        return create_operation_record(
            engine.backup_root, kind,
            {"id": "server-console", "email": "server-console"},
            backup_id=backup_id, target_commit=target_commit,
            idempotency_key=idempotency_key,
        )
    finally:
        guard.close()


def main():
    parser = argparse.ArgumentParser(description="Vechasu ERP Recovery V2")
    parser.add_argument(
        "command",
        choices=("run", "status", "restore-data", "rollback-code", "restore-system"),
    )
    parser.add_argument("operation_id", nargs="?")
    parser.add_argument("--backup-id")
    parser.add_argument("--commit")
    parser.add_argument("--idempotency-key")
    arguments = parser.parse_args()
    engine = build_engine()
    if arguments.command in ("run", "status"):
        if not OPERATION_ID_RE.fullmatch(str(arguments.operation_id or "")):
            parser.error("invalid operation id")
    if arguments.command == "run":
        try:
            result = engine.run(arguments.operation_id)
        except Exception as error:
            try:
                result = engine._load_operation(arguments.operation_id)
                result.update({
                    "active": False, "stage": "failed", "status": "failed",
                    "message": "Recovery helper аварийно завершился",
                    "error_code": type(error).__name__,
                })
                engine._write_operation(result)
            except Exception:
                pass
            raise
    elif arguments.command == "status":
        result = engine._load_operation(arguments.operation_id)
    else:
        kind = {
            "restore-data": "data_restore", "rollback-code": "code_rollback",
            "restore-system": "full_restore",
        }[arguments.command]
        if kind in ("data_restore", "full_restore") and not arguments.backup_id:
            parser.error("--backup-id is required")
        if kind == "code_rollback" and not arguments.commit:
            parser.error("--commit is required")
        operation, created = create_console_operation(
            engine, kind, arguments.backup_id, arguments.commit,
            arguments.idempotency_key,
        )
        result = engine.run(operation["id"]) if created else operation
    safe = dict(result)
    safe.pop("previous_instance", None)
    safe.pop("previous_release", None)
    safe.pop("staging_path", None)
    safe.pop("safety_backup_path", None)
    print(json.dumps(safe, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") not in ("failed", "critical") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        traceback.print_exc()
        print("RECOVERY_HELPER_FAILED:{}".format(type(error).__name__), file=sys.stderr)
        sys.exit(1)
