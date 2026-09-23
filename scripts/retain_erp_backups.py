#!/usr/bin/env python3
"""Create Clock ERP backups and enforce bounded backup retention."""

import argparse
import datetime as dt
import fcntl
import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path


LEGACY_DAILY_BACKUP = re.compile(r"^clock-erp-(\d{8})-(\d{6})\.tar\.gz$")
DAILY_BACKUP = re.compile(r"^clock-erp-daily-(\d{8})-(\d{6})\.tar\.gz$")
MANUAL_BACKUP = re.compile(r"^clock-erp-manual-(\d{8})-(\d{6})-[0-9a-f]{32}\.tar\.gz$")
SAFETY_BACKUP = re.compile(r"^clock-erp-safety-(\d{8})-(\d{6})-[0-9a-f]{32}\.tar\.gz$")
RUNTIME_DATABASE_BACKUP = re.compile(
    r"^[A-Za-z0-9_.-]+\.db\.backup-[A-Za-z0-9_.-]+$"
)
GZIP_HEADER = b"\x1f\x8b"


def _parse_timestamp(match):
    return dt.datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S")


def _valid_tar_backup(path):
    try:
        with path.open("rb") as backup_file:
            if not backup_file.read(16).startswith(GZIP_HEADER):
                return False
        found_runtime = False
        with tarfile.open(str(path), "r:gz") as archive:
            for member in archive:
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                name = name.lstrip("/")
                if name == ".env" or name == "instance" or name.startswith("instance/"):
                    found_runtime = True
        return found_runtime
    except (OSError, EOFError, tarfile.TarError):
        return False


def _is_valid_backup(path, kind):
    if path.is_symlink():
        return False
    if not path.is_file():
        return False
    if kind in ("daily", "weekly", "monthly", "manual", "safety"):
        return _valid_tar_backup(path)
    return False


def _discover(directory, pattern, kind):
    backups = []
    if not directory.is_dir():
        return backups
    for path in directory.iterdir():
        match = pattern.match(path.name)
        if match is None:
            continue
        try:
            timestamp = _parse_timestamp(match)
        except ValueError:
            continue
        backups.append((timestamp, path, _is_valid_backup(path, kind)))
    return backups


def discover_backups(backup_root):
    daily = _discover(backup_root, LEGACY_DAILY_BACKUP, "daily")
    daily.extend(_discover(backup_root / "daily", DAILY_BACKUP, "daily"))
    manual = _discover(backup_root / "manual", MANUAL_BACKUP, "manual")
    safety = _discover(backup_root / "safety", SAFETY_BACKUP, "safety")
    return {
        "daily": daily,
        "weekly": [item for item in daily if item[0].weekday() == 6],
        "monthly": [item for item in daily if item[0].day == 1],
        "manual": manual,
        "safety": safety,
    }


def retention_plan(backups, now, policy="daily"):
    ordered = sorted(backups, key=lambda item: (item[0], str(item[1])), reverse=True)
    keep = set()
    limits = {"daily": 14, "weekly": 8, "monthly": 12}
    if policy in limits:
        retained_periods = set()
        for timestamp, path, is_valid in ordered:
            if not is_valid or timestamp > now:
                keep.add(path)
                continue
            period = (
                timestamp.date() if policy == "daily" else
                timestamp.isocalendar()[:2] if policy == "weekly" else
                (timestamp.year, timestamp.month)
            )
            if period not in retained_periods and len(retained_periods) < limits[policy]:
                retained_periods.add(period)
                keep.add(path)
    elif policy in ("manual", "safety"):
        keep.update(path for _timestamp, path, _is_valid in ordered)
    else:
        raise ValueError("unknown retention policy: {}".format(policy))

    actions = []
    for timestamp, path, is_valid in ordered:
        if not is_valid:
            action = "SKIP_INVALID"
        elif timestamp > now:
            action = "KEEP_FUTURE"
        elif path in keep:
            action = "KEEP"
        else:
            action = "DELETE"
        actions.append((action, timestamp, path))
    return actions


def _path_size(path):
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def apply_plan(actions, backup_root, apply_changes):
    root = backup_root.resolve()
    counts = {}
    bytes_deleted = 0
    bytes_selected = 0
    for action, timestamp, path in actions:
        counts[action] = counts.get(action, 0) + 1
        size = _path_size(path) if path.exists() else 0
        print("{}|{}|{}|{}".format(action, timestamp.isoformat(), size, path))
        if action == "DELETE":
            bytes_selected += size
        if action != "DELETE" or not apply_changes:
            continue
        resolved = path.resolve()
        if root not in resolved.parents:
            raise RuntimeError("refusing to delete outside backup root: {}".format(path))
        try:
            if path.is_dir():
                shutil.rmtree(str(path))
            else:
                path.unlink()
            relative = str(resolved.relative_to(root))
            backup_id = hashlib.sha256(
                relative.encode("utf-8")
            ).hexdigest()[:24]
            metadata = root / "metadata" / (backup_id + ".json")
            if metadata.is_file() and not metadata.is_symlink():
                metadata.unlink()
            bytes_deleted += size
        except OSError as error:
            counts["DELETE_FAILED"] = counts.get("DELETE_FAILED", 0) + 1
            print("DELETE_FAILED|{}|{}|{}".format(timestamp.isoformat(), path, type(error).__name__))
    print(
        "SUMMARY|mode={}|keep={}|delete={}|delete_failed={}|skip_invalid={}|bytes_selected={}|bytes_deleted={}".format(
            "apply" if apply_changes else "dry-run",
            counts.get("KEEP", 0) + counts.get("KEEP_FUTURE", 0),
            counts.get("DELETE", 0),
            counts.get("DELETE_FAILED", 0),
            counts.get("SKIP_INVALID", 0),
            bytes_selected,
            bytes_deleted,
        )
    )


def _backup_sqlite_database(source, destination):
    with sqlite3.connect(str(source)) as source_connection:
        backup = getattr(source_connection, "backup", None)
        if callable(backup):
            with sqlite3.connect(str(destination)) as destination_connection:
                backup(destination_connection)
            return

    sqlite_binary = shutil.which("sqlite3")
    if not sqlite_binary:
        raise RuntimeError(
            "SQLite backup requires the sqlite3 CLI on this Python version"
        )
    subprocess.run(
        [sqlite_binary, str(source), ".backup '{}'".format(
            str(destination).replace("'", "''")
        )],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _runtime_copy_ignore(instance):
    def ignore(directory, names):
        if Path(directory) != instance:
            return []
        return [
            name for name in names
            if name == "backups"
            or RUNTIME_DATABASE_BACKUP.match(name)
            or name.endswith((".db-journal", ".db-wal", ".db-shm"))
        ]
    return ignore


def _copy_runtime_data(project_root, staging):
    copied = False
    env_path = project_root / ".env"
    if env_path.is_file():
        shutil.copy2(str(env_path), str(staging / ".env"))
        copied = True

    instance = project_root / "instance"
    if instance.is_dir():
        staged_instance = staging / "instance"
        shutil.copytree(
            str(instance),
            str(staged_instance),
            symlinks=True,
            ignore=_runtime_copy_ignore(instance),
        )
        for source in instance.glob("*.db"):
            destination = staged_instance / source.name
            for suffix in ("", "-journal", "-wal", "-shm"):
                candidate = Path(str(destination) + suffix)
                if candidate.exists() or candidate.is_symlink():
                    candidate.unlink()
            _backup_sqlite_database(source, destination)
            with sqlite3.connect(str(destination)) as destination_connection:
                result = destination_connection.execute(
                    "PRAGMA quick_check"
                ).fetchone()
                if not result or result[0] != "ok":
                    raise sqlite3.DatabaseError(
                        "SQLite backup quick_check failed: {}".format(source)
                    )
        copied = True
    if not copied:
        raise RuntimeError("no .env or instance directory found in {}".format(project_root))


def create_backup(project_root, backup_root, now, kind, operation_id=None, apply_changes=False):
    if kind == "daily":
        existing = [
            item for item in discover_backups(backup_root)["daily"]
            if item[0].date() == now.date() and item[2]
        ]
        if existing:
            newest = max(existing, key=lambda item: item[0])
            print("CREATE_SKIPPED|daily backup already exists|{}".format(newest[1]))
            return newest[1]
        directory = backup_root / "daily"
        filename = "clock-erp-daily-{}.tar.gz".format(now.strftime("%Y%m%d-%H%M%S"))
    elif kind == "manual":
        if not re.fullmatch(r"[0-9a-f]{32}", str(operation_id or "")):
            raise ValueError("manual backup operation id is invalid")
        directory = backup_root / "manual"
        filename = "clock-erp-manual-{}-{}.tar.gz".format(
            now.strftime("%Y%m%d-%H%M%S"), operation_id
        )
    elif kind == "safety":
        if not re.fullmatch(r"[0-9a-f]{32}", str(operation_id or "")):
            raise ValueError("safety backup operation id is invalid")
        directory = backup_root / "safety"
        filename = "clock-erp-safety-{}-{}.tar.gz".format(
            now.strftime("%Y%m%d-%H%M%S"), operation_id
        )
    else:
        raise ValueError("unsupported backup kind: {}".format(kind))
    target = directory / filename
    if not apply_changes:
        print("WOULD_CREATE|{}|{}".format(kind, target))
        return target

    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(str(directory), 0o700)
    staging = Path(tempfile.mkdtemp(prefix=".backup-stage.", dir=str(backup_root)))
    pending = directory / ("." + filename + ".pending")
    try:
        _copy_runtime_data(project_root, staging)
        with tarfile.open(str(pending), "w:gz") as archive:
            for name in (".env", "instance"):
                source = staging / name
                if source.exists():
                    archive.add(str(source), arcname=name, recursive=True)
        os.chmod(str(pending), 0o600)
        if not _valid_tar_backup(pending):
            raise RuntimeError("created archive failed validation: {}".format(pending))
        pending.replace(target)
        print("CREATED|{}|{}|{}".format(kind, target.stat().st_size, target))
        return target
    finally:
        if pending.exists():
            pending.unlink()
        shutil.rmtree(str(staging), ignore_errors=True)


def automatic_retention_categories(timestamp):
    categories = ["daily"]
    if timestamp.weekday() == 6:
        categories.append("weekly")
    if timestamp.day == 1:
        categories.append("monthly")
    return categories


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_recovery_metadata(project_root, backup_root, archive_path, backup_type,
                            reason=None, retention_categories=None, strict=False):
    """Attach Recovery V2 metadata without making backup creation Flask-dependent."""
    import json
    import sys

    root_text = str(project_root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    try:
        from app.services.backup_admin import BackupAdminService, _atomic_json_write
        current_release = Path("/opt/clock-erp-current")
        runtime_root = current_release if current_release.is_symlink() else project_root
        service = BackupAdminService(
            project_root, backup_root, Path(__file__), remote_check=False,
            recovery_contract=runtime_root / "ops" / "recovery-schema-contract.json",
            current_release=current_release,
        )
        relative = str(archive_path.relative_to(backup_root))
        backup_id = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
        candidate = next(
            item for item in service.list_backups(public=False)
            if item["backup_id"] == backup_id
        )
        expected = sorted(path.name for path in (project_root / "instance").glob("*.db"))
        databases = service._verify_archive(archive_path, expected)
        contract_hash, database_manifest, file_manifest = service._capture_recovery_metadata(
            archive_path
        )
        git = service.git_status(history_limit=1)
        commit = git.get("commit")
        branch = git.get("branch")
        if not commit or not branch:
            raise RuntimeError("deployed Git version is unavailable")
        metadata = {
            "metadata_version": 2, "backup_id": backup_id,
            "timestamp": candidate["timestamp"], "type": backup_type,
            "created_at": candidate["timestamp"], "status": "verified",
            "backup_type": backup_type,
            "retention_categories": list(retention_categories or [backup_type]),
            "reason": reason or ("manual" if backup_type == "manual" else
                                   "pre_restore" if backup_type == "safety" else
                                   "scheduled"),
            "size": archive_path.stat().st_size, "git_commit": commit,
            "checksum_sha256": _sha256_file(archive_path),
            "git_branch": branch, "app_version": commit[:12],
            "schema_versions": dict(
                (name, item["user_version"]) for name, item in database_manifest.items()
            ),
            "database_manifest": database_manifest, "file_manifest": file_manifest,
            "recovery_contract": contract_hash, "integrity_status": "verified",
            "databases": databases,
        }
        _atomic_json_write(service._metadata_path(backup_id), metadata)
        print("METADATA_VERIFIED|{}".format(backup_id))
        return metadata
    except Exception as error:
        print("METADATA_FAILED|{}".format(type(error).__name__), file=sys.stderr)
        if strict:
            raise
        return None


def combined_retention_plan(streams, now):
    """Delete a shared automatic archive only after every category expires."""
    decisions = {}
    timestamps = {}
    validity = {}
    for policy in ("daily", "weekly", "monthly"):
        for action, timestamp, path in retention_plan(streams[policy], now, policy):
            decisions.setdefault(path, {})[policy] = action
            timestamps[path] = timestamp
            validity[path] = action != "SKIP_INVALID"
    actions = []
    for path in sorted(timestamps, key=lambda item: (timestamps[item], str(item)), reverse=True):
        path_decisions = decisions[path]
        if not validity[path]:
            action = "SKIP_INVALID"
        elif any(value == "KEEP_FUTURE" for value in path_decisions.values()):
            action = "KEEP_FUTURE"
        elif any(value == "KEEP" for value in path_decisions.values()):
            action = "KEEP"
        else:
            action = "DELETE"
        actions.append((action, timestamps[path], path))
    return actions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-root", type=Path, required=True)
    parser.add_argument("--project-root", type=Path)
    creation = parser.add_mutually_exclusive_group()
    creation.add_argument("--create-daily", action="store_true")
    creation.add_argument("--create-manual", metavar="OPERATION_ID")
    creation.add_argument("--create-safety", metavar="OPERATION_ID")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--now", help=argparse.SUPPRESS)
    arguments = parser.parse_args()

    backup_root = arguments.backup_root.resolve()
    if not backup_root.is_dir():
        parser.error("backup root does not exist: {}".format(backup_root))
    if (
        arguments.create_daily
        or arguments.create_manual
        or arguments.create_safety
    ) and not arguments.project_root:
        parser.error("--project-root is required when creating a backup")

    lock_path = backup_root / ".retention.lock"
    lock_file = lock_path.open("a+")
    os.chmod(str(lock_path), 0o600)
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        parser.error("another backup/retention process is already running")

    now = (
        dt.datetime.strptime(arguments.now, "%Y-%m-%dT%H:%M:%S")
        if arguments.now
        else dt.datetime.now()
    )
    streams = discover_backups(backup_root)
    print("STREAM|automatic")
    apply_plan(combined_retention_plan(streams, now), backup_root, arguments.apply)
    for stream in ("manual", "safety"):
        print("STREAM|{}".format(stream))
        apply_plan(retention_plan(streams[stream], now, policy=stream), backup_root,
                   arguments.apply)

    if arguments.create_daily:
        daily_before = set(path for _timestamp, path, _valid in discover_backups(backup_root)["daily"])
        created = create_backup(
            arguments.project_root.resolve(), backup_root, now, "daily",
            apply_changes=arguments.apply,
        )
        if (
            arguments.apply and created and created.is_file()
            and created not in daily_before
        ):
            write_recovery_metadata(
                arguments.project_root.resolve(), backup_root, created, "daily",
                reason="scheduled",
                retention_categories=automatic_retention_categories(now),
            )
    elif arguments.create_manual:
        created = create_backup(
            arguments.project_root.resolve(), backup_root, now, "manual",
            operation_id=arguments.create_manual, apply_changes=arguments.apply,
        )
        if arguments.apply and created and created.is_file():
            write_recovery_metadata(
                arguments.project_root.resolve(), backup_root, created, "manual",
                reason="manual", retention_categories=["manual"],
            )
    elif arguments.create_safety:
        created = create_backup(
            arguments.project_root.resolve(), backup_root, now, "safety",
            operation_id=arguments.create_safety, apply_changes=arguments.apply,
        )
        if arguments.apply and created and created.is_file():
            write_recovery_metadata(
                arguments.project_root.resolve(), backup_root, created, "safety",
                reason="pre_restore", retention_categories=["safety"], strict=True,
            )


if __name__ == "__main__":
    main()
