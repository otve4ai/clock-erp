"""Validated, auditable Recovery V2 engine.

The engine is intentionally usable without Flask.  HTTP handlers only create a
validated operation record; this module performs all filesystem, Git and
service work from fixed server-side roots.
"""

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

from app.services.backup_admin import (
    BackupAdminError,
    BackupAdminService,
    BackupBusyError,
    BackupNotFoundError,
    _atomic_json_write,
    _safe_json_read,
    _utc_now,
)


OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,96}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
KINDS = ("data_restore", "code_rollback", "full_restore")
ACTIVE_STAGES = (
    "pending", "preflight", "staging_restore", "staging_check", "maintenance",
    "production_restore", "service_restart", "health_check",
)
STAGE_LABELS = {
    "pending": "Ожидает запуска",
    "preflight": "Предварительная проверка",
    "staging_restore": "Распаковка в staging",
    "staging_check": "Проверка staging",
    "maintenance": "Режим обслуживания",
    "production_restore": "Восстановление",
    "service_restart": "Перезапуск ERP",
    "health_check": "Проверка ERP",
    "completed": "Готово",
    "failed": "Ошибка",
    "critical": "Критическая ошибка",
}
WRITER_UNITS = (
    "vechasu-wb-sync.timer", "vechasu-wb-full-sync.timer",
    "vechasu-wb-sync.service", "vechasu-wb-full-sync.service",
)
WRITER_LOCKS = (
    "/run/lock/clock-erp-customers-cron.lock",
    "/run/lock/clock-erp-sms-sync.lock",
    "/run/lock/clock-erp-mail.lock",
)


class RecoveryError(BackupAdminError):
    def __init__(self, code, message, production_changed=False):
        super().__init__(message)
        self.code = str(code)
        self.production_changed = bool(production_changed)


def _json_hash(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        raise RecoveryError("CONTRACT_UNAVAILABLE", "Контракт совместимости недоступен")
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), value


def _schema_digest(connection):
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    normalized = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def inspect_instance(instance_root, contract, require_all=True):
    """Return a secret-free schema manifest and validate critical tables."""
    root = Path(instance_root)
    databases = contract.get("databases") if isinstance(contract, dict) else None
    if not isinstance(databases, dict) or not databases:
        raise RecoveryError("CONTRACT_INVALID", "Контракт совместимости некорректен")
    result = {}
    for name, required_tables in sorted(databases.items()):
        path = root / name
        if not path.is_file() or path.is_symlink():
            if require_all:
                raise RecoveryError("DATABASE_MISSING", "Отсутствует обязательная база {}".format(name))
            continue
        try:
            connection = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
            try:
                check = connection.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise RecoveryError("SQLITE_CHECK_FAILED", "База {} повреждена".format(name))
                tables = set(row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall())
                missing = sorted(set(required_tables or []) - tables)
                if missing:
                    raise RecoveryError(
                        "CRITICAL_TABLE_MISSING",
                        "В базе {} отсутствуют критичные таблицы".format(name),
                    )
                for table in required_tables or []:
                    quoted = str(table).replace('"', '""')
                    connection.execute('SELECT 1 FROM "{}" LIMIT 1'.format(quoted)).fetchone()
                user_version = int(connection.execute(
                    "PRAGMA {}".format("user_version")
                ).fetchone()[0])
                digest = _schema_digest(connection)
            finally:
                connection.close()
        except RecoveryError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise RecoveryError(
                "DATABASE_UNREADABLE",
                "Не удалось безопасно проверить базу {}: {}".format(name, type(error).__name__),
            )
        result[name] = {
            "size": path.stat().st_size,
            "user_version": user_version,
            "schema_digest": digest,
            "critical_tables": sorted(required_tables or []),
        }
    return result


def inspect_file_manifest(instance_root):
    """Hash every persistent regular file without following links."""
    root = Path(instance_root).resolve()
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RecoveryError("UNSAFE_PERSISTENT_FILE", "Persistent data содержит symlink")
        if not path.is_file():
            continue
        relative = str(path.relative_to(root))
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            result[relative] = {"size": path.stat().st_size, "sha256": digest.hexdigest()}
        except OSError as error:
            raise RecoveryError(
                "PERSISTENT_FILE_UNREADABLE",
                "Не удалось проверить persistent-файл: {}".format(type(error).__name__),
            )
    return result


class RecoveryEngine:
    """Runs one recovery operation while holding both recovery and backup locks."""

    def __init__(
        self, project_root, backup_root, backup_script, contract_path,
        release_root=None, current_link=None, maintenance_path=None,
        service_name="clock-erp", command_timeout=30, test_mode=False,
        failure_stage=None, system_actions=True,
    ):
        self.project_root = Path(project_root).resolve()
        self.backup_root = Path(backup_root).resolve()
        self.backup_script = Path(backup_script).resolve()
        self.contract_path = Path(contract_path).resolve()
        self.release_root = Path(release_root or "/opt/clock-erp-releases")
        self.current_link = Path(current_link or "/opt/clock-erp-current")
        self.maintenance_path = Path(maintenance_path or "/run/clock-erp-maintenance.json")
        self.service_name = str(service_name)
        self.command_timeout = int(command_timeout)
        self.test_mode = bool(test_mode)
        self.failure_stage = str(failure_stage or "") if self.test_mode else ""
        self.system_actions = bool(system_actions)
        self.operations_root = self.backup_root / "recovery" / "operations"
        self.logs_root = self.backup_root / "recovery" / "logs"
        self.staging_root = self.backup_root / "recovery" / "staging"
        self.operation_path = self.backup_root / ".backup-admin-operation.json"
        self.operation_lock = self.backup_root / ".backup-admin-operation.lock"
        self.retention_lock = self.backup_root / ".retention.lock"
        self.backups = BackupAdminService(
            self.project_root, self.backup_root, self.backup_script,
            service_name=self.service_name, remote_check=False,
        )
        self._writer_guards = []

    def _heartbeat_path(self, operation_id):
        return self.operations_root / (operation_id + ".heartbeat.json")

    def _write_heartbeat(self, operation_id):
        _atomic_json_write(self._heartbeat_path(operation_id), {
            "operation_id": operation_id,
            "pid": os.getpid(),
            "updated_at": _utc_now(),
            "updated_epoch": time.time(),
        })

    def _heartbeat_worker(self, operation_id, stopped):
        while not stopped.is_set():
            try:
                self._write_heartbeat(operation_id)
            except OSError:
                pass
            stopped.wait(5)

    def _run(self, args, cwd=None, timeout=None, input_data=None):
        return subprocess.run(
            list(args), cwd=str(cwd or self.project_root), check=False,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout or self.command_timeout,
            input=input_data,
        )

    def _operation_file(self, operation_id):
        if not OPERATION_ID_RE.fullmatch(str(operation_id or "")):
            raise RecoveryError("OPERATION_NOT_FOUND", "Неизвестная операция")
        return self.operations_root / (operation_id + ".json")

    def _load_operation(self, operation_id):
        operation = _safe_json_read(self._operation_file(operation_id))
        if not operation or operation.get("id") != operation_id:
            raise RecoveryError("OPERATION_NOT_FOUND", "Неизвестная операция")
        return operation

    def _write_operation(self, operation):
        operation["updated_at"] = _utc_now()
        operation["updated_epoch"] = time.time()
        _atomic_json_write(self._operation_file(operation["id"]), operation)
        _atomic_json_write(self.operation_path, operation)

    def _log(self, operation, event, details=None):
        record = {
            "timestamp": _utc_now(), "operation_id": operation["id"],
            "kind": operation["kind"], "stage": operation.get("stage"),
            "owner_id": operation.get("owner_id"),
            "owner": operation.get("owner"),
            "backup_id": operation.get("backup_id"),
            "source_commit": operation.get("source_commit"),
            "target_commit": operation.get("target_commit"),
            "event": str(event)[:80], "details": str(details or "")[:500] or None,
        }
        self.logs_root.mkdir(parents=True, exist_ok=True)
        path = self.logs_root / (operation["id"] + ".jsonl")
        descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def _stage(self, operation, stage, message=None):
        operation["stage"] = stage
        operation["status"] = stage
        operation["active"] = stage not in ("completed", "failed", "critical")
        operation["message"] = message or STAGE_LABELS.get(stage, stage)
        progress = operation.setdefault("progress", [])
        if not progress or progress[-1].get("stage") != stage:
            progress.append({"stage": stage, "label": STAGE_LABELS.get(stage, stage), "at": _utc_now()})
        self._write_operation(operation)
        self._log(operation, "stage")
        if self.failure_stage == stage:
            raise RecoveryError("INJECTED_FAILURE", "Искусственная ошибка на этапе {}".format(stage))

    def _inject(self, point):
        if self.failure_stage == point:
            raise RecoveryError("INJECTED_FAILURE", "Искусственная ошибка {}".format(point))

    def _contract_for_commit(self, commit):
        if not COMMIT_RE.fullmatch(str(commit or "")):
            raise RecoveryError("COMMIT_NOT_FOUND", "Неизвестная версия кода")
        exists = self._run(["git", "cat-file", "-e", commit + "^{commit}"])
        if exists.returncode != 0:
            raise RecoveryError("COMMIT_NOT_FOUND", "Неизвестная версия кода")
        ancestor = self._run(["git", "merge-base", "--is-ancestor", commit, "origin/main"])
        if ancestor.returncode != 0:
            raise RecoveryError("COMMIT_NOT_ALLOWED", "Commit не принадлежит разрешённой истории main")
        result = self._run(["git", "show", commit + ":ops/recovery-schema-contract.json"])
        if result.returncode != 0:
            raise RecoveryError("CONTRACT_UNAVAILABLE", "Для версии кода нет Recovery V2 контракта")
        try:
            contract = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise RecoveryError("CONTRACT_INVALID", "Контракт выбранной версии некорректен")
        normalized = json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest(), contract

    @staticmethod
    def _manifest_compatible(left, right):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return False
        if set(left) != set(right):
            return False
        return all(
            left[name].get("user_version") == right[name].get("user_version")
            and left[name].get("schema_digest") == right[name].get("schema_digest")
            for name in left
        )

    def _preflight(self, operation):
        kind = operation["kind"]
        contract_hash, contract = _json_hash(self.contract_path)
        current_manifest = inspect_instance(self.project_root / "instance", contract)
        current_files = inspect_file_manifest(self.project_root / "instance")
        operation["source_commit"] = self.backups.git_status(history_limit=1).get("commit")
        status_result = self._run(["git", "status", "--porcelain", "--untracked-files=normal"])
        if status_result.returncode != 0 or status_result.stdout.strip():
            raise RecoveryError("DIRTY_GIT", "Production содержит незакоммиченные изменения")
        try:
            free_values = [
                shutil.disk_usage(str(self.backup_root)).free,
                shutil.disk_usage(str(self.project_root.parent)).free,
                shutil.disk_usage(str(self.release_root.parent)).free,
            ]
        except OSError:
            raise RecoveryError("DISK_UNKNOWN", "Не удалось определить свободное место")
        if self.system_actions:
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                raise RecoveryError("HELPER_PERMISSION_DENIED", "Recovery helper не имеет требуемых ограниченных прав")
            if not os.access(str(self.project_root.parent), os.W_OK) or not os.access(str(self.backup_root), os.W_OK):
                raise RecoveryError("HELPER_PERMISSION_DENIED", "Recovery helper не может подготовить atomic swap")
            service = self._run(["systemctl", "show", self.service_name, "--property=LoadState"], cwd="/")
            if service.returncode != 0 or b"LoadState=loaded" not in service.stdout:
                raise RecoveryError("HELPER_UNAVAILABLE", "Systemd service recovery недоступен")

        backup = None
        metadata = None
        if kind in ("data_restore", "full_restore"):
            backup = self.backups.resolve_backup(operation.get("backup_id"))
            metadata = _safe_json_read(self.backups._metadata_path(backup["backup_id"])) or {}
            if backup.get("integrity_status") != "verified" or metadata.get("metadata_version") != 2:
                raise RecoveryError("BACKUP_UNVERIFIED", "Бэкап не проверен Recovery V2")
            if not isinstance(metadata.get("file_manifest"), dict) or not metadata["file_manifest"]:
                raise RecoveryError("BACKUP_METADATA_INVALID", "Metadata не подтверждает состав persistent data")
            if metadata.get("recovery_contract") != contract_hash and kind == "data_restore":
                raise RecoveryError("INCOMPATIBLE_SCHEMA", "Версия данных несовместима с текущим кодом")
            if kind == "data_restore" and not self._manifest_compatible(
                metadata.get("database_manifest"), current_manifest
            ):
                raise RecoveryError("INCOMPATIBLE_SCHEMA", "Схема backup несовместима с текущими данными")

        target_commit = operation.get("target_commit")
        runtime_contract = contract
        if kind == "full_restore":
            target_commit = metadata.get("git_commit")
            operation["target_commit"] = target_commit
        if kind in ("code_rollback", "full_restore"):
            target_hash, target_contract = self._contract_for_commit(target_commit)
            if target_hash != contract_hash and kind == "code_rollback":
                raise RecoveryError("INCOMPATIBLE_SCHEMA", "Код несовместим с текущей схемой данных")
            if kind == "full_restore":
                if metadata.get("recovery_contract") != target_hash:
                    raise RecoveryError("INCOMPATIBLE_SCHEMA", "Точка восстановления несовместима с кодом")
                if set((metadata.get("database_manifest") or {})) != set(target_contract.get("databases") or {}):
                    raise RecoveryError("INCOMPATIBLE_SCHEMA", "Metadata не подтверждает все базы выбранного кода")
                runtime_contract = target_contract
            if not self.current_link.is_symlink():
                raise RecoveryError("RELEASE_RUNTIME_UNAVAILABLE", "Атомарный release runtime не настроен")
            try:
                current_requirements = (self.current_link.resolve() / "requirements.txt").read_bytes()
            except OSError:
                raise RecoveryError(
                    "RUNTIME_COMPATIBILITY_UNKNOWN",
                    "Не удалось подтвердить зависимости текущего runtime",
                )
            target_requirements = self._run([
                "git", "show", target_commit + ":requirements.txt"
            ])
            if (
                target_requirements.returncode != 0
                or target_requirements.stdout != current_requirements
            ):
                raise RecoveryError(
                    "INCOMPATIBLE_RUNTIME",
                    "Зависимости выбранного кода отличаются от production runtime",
                )

        current_size = sum(item.get("size", 0) for item in current_files.values())
        target_size = sum(
            item.get("size", 0) for item in (metadata or {}).get("file_manifest", {}).values()
        )
        release_size = 0
        if target_commit:
            tree = self._run(["git", "ls-tree", "-lr", target_commit])
            if tree.returncode != 0:
                raise RecoveryError("COMMIT_NOT_FOUND", "Не удалось оценить размер выбранного release")
            for line in tree.stdout.decode("utf-8", "replace").splitlines():
                fields = line.split(None, 4)
                if len(fields) >= 4 and fields[3].isdigit():
                    release_size += int(fields[3])
        required_space = current_size + target_size + release_size
        free = min(free_values)
        if any(value <= required_space for value in free_values):
            raise RecoveryError("INSUFFICIENT_SPACE", "Недостаточно места для staging восстановления")
        operation["space_required"] = required_space
        operation["space_free"] = free

        operation["contract_hash"] = contract_hash
        operation["current_manifest"] = current_manifest
        self._write_operation(operation)
        return backup, metadata, contract, runtime_contract

    def _safe_extract(self, archive_path, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=False)
        total = 0
        try:
            free = shutil.disk_usage(str(destination)).free
        except OSError:
            shutil.rmtree(str(destination), ignore_errors=True)
            raise RecoveryError("DISK_UNKNOWN", "Не удалось определить место для staging")
        try:
            archive_size = archive_path.stat().st_size
        except OSError as error:
            shutil.rmtree(str(destination), ignore_errors=True)
            raise RecoveryError(
                "ARCHIVE_UNREADABLE", "Бэкап недоступен: {}".format(type(error).__name__)
            )
        expansion_limit = min(free // 2, max(1024 * 1024, archive_size * 200))
        try:
            with tarfile.open(str(archive_path), "r:gz") as archive:
                for member in archive:
                    normalized = member.name[2:] if member.name.startswith("./") else member.name
                    parts = Path(normalized).parts
                    if (
                        member.name.startswith("/") or ".." in parts or not parts
                        or member.issym() or member.islnk() or member.isdev()
                    ):
                        raise RecoveryError("ARCHIVE_TRAVERSAL", "Архив содержит небезопасный объект")
                    if parts[0] == ".env":
                        continue
                    if parts[0] != "instance":
                        raise RecoveryError("ARCHIVE_LAYOUT_INVALID", "Архив содержит неожиданный путь")
                    target = destination.joinpath(*parts)
                    resolved_parent = target.parent.resolve()
                    if destination.resolve() not in (resolved_parent,) + tuple(resolved_parent.parents):
                        raise RecoveryError("ARCHIVE_TRAVERSAL", "Архив выходит за пределы staging")
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        os.chmod(str(target), int(member.mode) & 0o777 or 0o700)
                        continue
                    if not member.isfile():
                        raise RecoveryError("ARCHIVE_SPECIAL_FILE", "Архив содержит специальный файл")
                    total += int(member.size)
                    if total > expansion_limit:
                        raise RecoveryError("ARCHIVE_TOO_LARGE", "Распакованный архив превышает безопасный размер")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = archive.extractfile(member)
                    if source is None:
                        raise RecoveryError("ARCHIVE_UNREADABLE", "Не удалось прочитать файл из архива")
                    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as output:
                        shutil.copyfileobj(source, output)
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(str(target), int(member.mode) & 0o777 or 0o600)
        except RecoveryError:
            shutil.rmtree(str(destination), ignore_errors=True)
            raise
        except (OSError, EOFError, tarfile.TarError) as error:
            shutil.rmtree(str(destination), ignore_errors=True)
            raise RecoveryError("ARCHIVE_UNREADABLE", "Бэкап не удалось распаковать: {}".format(type(error).__name__))
        instance = destination / "instance"
        if not instance.is_dir():
            shutil.rmtree(str(destination), ignore_errors=True)
            raise RecoveryError("ARCHIVE_LAYOUT_INVALID", "В архиве отсутствует instance")
        return instance

    def _stage_backup(self, operation, backup, metadata, contract):
        target = self.staging_root / operation["id"]
        if target.exists():
            shutil.rmtree(str(target))
        instance = self._safe_extract(backup["_path"], target)
        manifest = inspect_instance(instance, contract)
        if not self._manifest_compatible(manifest, metadata.get("database_manifest")):
            raise RecoveryError("STAGING_MISMATCH", "Staging не соответствует metadata backup")
        if inspect_file_manifest(instance) != metadata.get("file_manifest"):
            raise RecoveryError("STAGING_FILE_MISMATCH", "Состав staging не соответствует metadata backup")
        operation["staging_path"] = str(target.relative_to(self.backup_root))
        self._write_operation(operation)
        return target, instance

    def _maintenance_on(self, operation):
        self.maintenance_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(self.maintenance_path, {
            "operation_id": operation["id"], "started_at": _utc_now(),
            "message": "ERP временно недоступна — выполняется восстановление.",
        })
        if self.system_actions:
            deadline = time.monotonic() + 30
            for lock_path in WRITER_LOCKS:
                guard = open(lock_path, "a+")
                while True:
                    try:
                        fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        self._writer_guards.append(guard)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            guard.close()
                            raise RecoveryError("WRITER_BUSY", "Не удалось остановить фоновую запись")
                        time.sleep(0.1)
            for unit in WRITER_UNITS:
                result = self._run(["systemctl", "stop", unit], cwd="/")
                if result.returncode != 0:
                    raise RecoveryError("WRITER_STOP_FAILED", "Не удалось остановить фоновый writer")
            result = self._run(["systemctl", "stop", self.service_name], cwd="/")
            if result.returncode != 0:
                raise RecoveryError("SERVICE_STOP_FAILED", "Не удалось остановить ERP")

    def _maintenance_off(self):
        writers_started = True
        if self.system_actions:
            for unit in ("vechasu-wb-sync.timer", "vechasu-wb-full-sync.timer"):
                result = self._run(["systemctl", "start", unit], cwd="/")
                writers_started = result.returncode == 0 and writers_started
        for guard in self._writer_guards:
            guard.close()
        self._writer_guards = []
        if writers_started:
            try:
                self.maintenance_path.unlink()
            except OSError:
                pass
        return writers_started

    def _swap_instance(self, operation, staged_instance):
        instance = self.project_root / "instance"
        incoming = self.project_root.parent / (".clock-erp-instance-incoming-" + operation["id"])
        previous = self.project_root.parent / (".clock-erp-instance-before-" + operation["id"])
        if incoming.exists() or previous.exists():
            raise RecoveryError("SWAP_PATH_EXISTS", "Временный путь предыдущей recovery-операции не очищен")
        try:
            shutil.copytree(str(staged_instance), str(incoming), symlinks=False)
            old_stat = instance.stat()
            os.chmod(str(incoming), old_stat.st_mode & 0o777)
            if hasattr(os, "chown"):
                os.chown(str(incoming), old_stat.st_uid, old_stat.st_gid)
            descriptor = os.open(str(incoming), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except Exception:
            shutil.rmtree(str(incoming), ignore_errors=True)
            raise
        os.rename(str(instance), str(previous))
        operation["previous_instance"] = str(previous)
        try:
            self._write_operation(operation)
            os.rename(str(incoming), str(instance))
        except Exception:
            try:
                os.rename(str(previous), str(instance))
                operation.pop("previous_instance", None)
            except Exception:
                raise RecoveryError(
                    "INSTANCE_SWAP_CRITICAL",
                    "Не удалось завершить или отменить замену persistent data",
                    True,
                )
            shutil.rmtree(str(incoming), ignore_errors=True)
            raise
        return previous

    def _restore_previous_instance(self, operation):
        previous_value = operation.get("previous_instance")
        if not previous_value:
            return True
        previous = Path(previous_value)
        current = self.project_root / "instance"
        failed = self.project_root.parent / (".clock-erp-instance-failed-" + operation["id"])
        if not previous.is_dir() or failed.exists():
            return False
        moved_current = False
        try:
            if current.exists():
                os.rename(str(current), str(failed))
                moved_current = True
            os.rename(str(previous), str(current))
        except Exception:
            if moved_current and failed.exists() and not current.exists():
                try:
                    os.rename(str(failed), str(current))
                except Exception:
                    pass
            return False
        if moved_current:
            shutil.rmtree(str(failed), ignore_errors=True)
        return True

    def _create_release(self, commit):
        self.release_root.mkdir(parents=True, exist_ok=True)
        release = self.release_root / commit
        if release.is_dir():
            return release
        temporary = Path(tempfile.mkdtemp(prefix=".release-", dir=str(self.release_root)))
        process = subprocess.Popen(
            ["git", "archive", "--format=tar", commit], cwd=str(self.project_root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    parts = Path(member.name).parts
                    if member.name.startswith("/") or ".." in parts or member.issym() or member.islnk():
                        raise RecoveryError("RELEASE_ARCHIVE_UNSAFE", "Git release содержит небезопасный путь")
                    archive.extract(member, str(temporary))
            process.stdout.close()
            stderr = process.stderr.read()
            process.stderr.close()
            if process.wait() != 0:
                del stderr
                raise RecoveryError("RELEASE_CREATE_FAILED", "Не удалось подготовить выбранный release")
            for name in ("instance", "venv", ".env"):
                source = self.project_root / name
                if source.exists():
                    target = temporary / name
                    if target.is_symlink() or target.is_file():
                        target.unlink()
                    elif target.is_dir():
                        shutil.rmtree(str(target))
                    os.symlink(str(source), str(target))
            contract_hash, _ = self._contract_for_commit(commit)
            _atomic_json_write(temporary / ".erp-release.json", {
                "commit": commit, "contract_hash": contract_hash, "created_at": _utc_now(),
            })
            os.rename(str(temporary), str(release))
        except Exception:
            if process.poll() is None:
                process.kill()
                process.wait()
            shutil.rmtree(str(temporary), ignore_errors=True)
            raise
        return release

    def _switch_release(self, operation, commit):
        previous = os.path.realpath(str(self.current_link))
        release = self._create_release(commit)
        operation["previous_release"] = previous
        operation["target_release"] = str(release)
        self._write_operation(operation)
        temporary = self.current_link.parent / ("." + self.current_link.name + "." + operation["id"])
        try:
            os.symlink(str(release), str(temporary))
            os.replace(str(temporary), str(self.current_link))
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return previous

    def _restore_previous_release(self, operation):
        previous = operation.get("previous_release")
        if not previous or not Path(previous).is_dir():
            return True
        temporary = self.current_link.parent / ("." + self.current_link.name + ".rollback." + operation["id"])
        try:
            os.symlink(previous, str(temporary))
            os.replace(str(temporary), str(self.current_link))
            return True
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            return False

    def _restart_service(self):
        if self.system_actions:
            result = self._run(["systemctl", "restart", self.service_name], cwd="/")
            if result.returncode != 0:
                raise RecoveryError("SERVICE_RESTART_FAILED", "Не удалось перезапустить ERP", True)
            active = self._run(["systemctl", "is-active", "--quiet", self.service_name], cwd="/")
            if active.returncode != 0:
                raise RecoveryError("SERVICE_INACTIVE", "ERP не запущена после восстановления", True)

    def _health(self, contract, operation=None):
        inspect_instance(self.project_root / "instance", contract)
        if self.system_actions:
            try:
                request = Request("http://127.0.0.1:5000/login", headers={"User-Agent": "clock-erp-recovery/2"})
                with urlopen(request, timeout=10) as response:
                    if response.getcode() != 200:
                        raise RecoveryError("HTTP_HEALTH_FAILED", "HTTP health-check ERP не пройден", True)
            except RecoveryError:
                raise
            except Exception:
                raise RecoveryError("HTTP_HEALTH_FAILED", "HTTP health-check ERP не пройден", True)
            since = (operation or {}).get("_health_since") or next((
                item.get("at") for item in reversed((operation or {}).get("progress") or [])
                if item.get("stage") == "service_restart" and item.get("at")
            ), "-2 minutes")
            journal = self._run([
                "journalctl", "-u", self.service_name, "--since", since,
                "--priority=err", "--no-pager", "--quiet",
            ], cwd="/")
            if journal.returncode != 0 or journal.stdout.strip():
                raise RecoveryError("APPLICATION_ERRORS", "ERP сообщила об ошибках после восстановления", True)

    def run(self, operation_id):
        operation = self._load_operation(operation_id)
        if operation.get("kind") not in KINDS:
            raise RecoveryError("OPERATION_INVALID", "Недопустимый тип операции")
        self.backup_root.mkdir(parents=True, exist_ok=True)
        operation_guard = self.operation_lock.open("a+")
        retention_guard = self.retention_lock.open("a+")
        production_changed = False
        maintenance = False
        heartbeat_stopped = threading.Event()
        heartbeat_thread = None
        try:
            try:
                fcntl.flock(operation_guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(retention_guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise BackupBusyError("Другая backup/recovery операция уже выполняется")
            operation["worker_pid"] = os.getpid()
            operation["started_at"] = _utc_now()
            self._write_operation(operation)
            heartbeat_thread = threading.Thread(
                target=self._heartbeat_worker,
                args=(operation_id, heartbeat_stopped),
                name="erp-recovery-heartbeat-" + operation_id[:8],
            )
            heartbeat_thread.daemon = True
            heartbeat_thread.start()
            self._stage(operation, "preflight")
            backup, metadata, _current_contract, runtime_contract = self._preflight(operation)
            staged_instance = None
            if operation["kind"] in ("data_restore", "full_restore"):
                self._stage(operation, "staging_restore")
                _staging, staged_instance = self._stage_backup(operation, backup, metadata, runtime_contract)
                self._inject("after_staging")
                self._stage(operation, "staging_check")
            self._stage(operation, "maintenance")
            maintenance = True
            self._maintenance_on(operation)
            self._stage(operation, "production_restore")
            if operation["kind"] in ("data_restore", "full_restore"):
                self._swap_instance(operation, staged_instance)
                production_changed = True
            if operation["kind"] in ("code_rollback", "full_restore"):
                self._switch_release(operation, operation["target_commit"])
                production_changed = True
            self._inject("after_production_swap")
            self._stage(operation, "service_restart")
            self._restart_service()
            self._inject("after_restart")
            self._stage(operation, "health_check")
            self._health(runtime_contract, operation)
            self._stage(operation, "completed", "Восстановление успешно завершено")
            operation["finished_at"] = _utc_now()
            self._write_operation(operation)
            if not self._maintenance_off():
                raise RecoveryError(
                    "WRITER_START_FAILED", "Не удалось возобновить фоновые процессы", True
                )
            maintenance = False
            self.backups._audit(
                {"id": operation.get("owner_id"), "email": operation.get("owner")},
                operation["kind"], "success", backup_id=operation.get("backup_id"),
                source_commit=operation.get("source_commit"),
                target_commit=operation.get("target_commit"),
                operation_id=operation["id"],
            )
            previous_instance = operation.get("previous_instance")
            if previous_instance:
                shutil.rmtree(previous_instance, ignore_errors=True)
            staging_path = operation.get("staging_path")
            if staging_path:
                shutil.rmtree(str(self.backup_root / staging_path), ignore_errors=True)
            return operation
        except Exception as error:
            recovery_error = error if isinstance(error, RecoveryError) else RecoveryError(
                "RECOVERY_FAILED", "Recovery завершился с ошибкой {}".format(type(error).__name__), production_changed
            )
            production_changed = bool(
                production_changed or recovery_error.production_changed
                or operation.get("previous_instance") or operation.get("previous_release")
            )
            rollback_attempted = production_changed
            rollback_ok = True
            if rollback_attempted:
                if self.failure_stage == "automatic_rollback":
                    rollback_ok = False
                else:
                    rollback_ok = self._restore_previous_release(operation)
                    rollback_ok = self._restore_previous_instance(operation) and rollback_ok
                if rollback_ok:
                    try:
                        operation["_health_since"] = _utc_now()
                        self._restart_service()
                        self._health(_json_hash(self.contract_path)[1], operation)
                    except Exception:
                        rollback_ok = False
            elif maintenance:
                try:
                    operation["_health_since"] = _utc_now()
                    self._restart_service()
                    self._health(_json_hash(self.contract_path)[1], operation)
                except Exception:
                    rollback_ok = False
            operation.pop("_health_since", None)
            if maintenance and rollback_ok and not self._maintenance_off():
                rollback_ok = False
                recovery_error = RecoveryError(
                    "WRITER_START_FAILED", "Не удалось возобновить фоновые процессы", True
                )
            staging_path = operation.get("staging_path")
            if staging_path:
                shutil.rmtree(str(self.backup_root / staging_path), ignore_errors=True)
            operation.update({
                "active": False,
                "stage": "failed" if rollback_ok else "critical",
                "status": "failed" if rollback_ok else "critical",
                "message": str(recovery_error),
                "error_code": recovery_error.code,
                "automatic_rollback": {
                    "attempted": rollback_attempted,
                    "result": "completed" if rollback_attempted and rollback_ok else "failed" if rollback_attempted else "not_needed",
                },
                "finished_at": _utc_now(),
            })
            operation.setdefault("progress", []).append({
                "stage": operation["status"],
                "label": STAGE_LABELS[operation["status"]],
                "at": _utc_now(),
            })
            self._write_operation(operation)
            self._log(
                operation, "automatic_rollback",
                operation["automatic_rollback"]["result"],
            )
            self._log(operation, "failed", recovery_error.code)
            self.backups._audit(
                {"id": operation.get("owner_id"), "email": operation.get("owner")},
                operation["kind"], operation["status"], backup_id=operation.get("backup_id"),
                source_commit=operation.get("source_commit"),
                target_commit=operation.get("target_commit"), error=recovery_error.code,
                operation_id=operation["id"],
            )
            return operation
        finally:
            heartbeat_stopped.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=6)
            retention_guard.close()
            operation_guard.close()


def create_operation_record(root, kind, actor, backup_id=None, target_commit=None,
                            idempotency_key=None):
    if kind not in KINDS:
        raise RecoveryError("OPERATION_INVALID", "Недопустимый тип операции")
    if idempotency_key and not IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key):
        raise RecoveryError("IDEMPOTENCY_INVALID", "Некорректный ключ идемпотентности")
    operations_root = Path(root) / "recovery" / "operations"
    operations_root.mkdir(parents=True, exist_ok=True)
    if idempotency_key:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        request_path = operations_root / ("request-" + digest + ".json")
        existing = _safe_json_read(request_path)
        if existing:
            operation = _safe_json_read(operations_root / (existing.get("operation_id", "") + ".json"))
            if operation:
                return operation, False
    operation_id = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
    operation = {
        "id": operation_id, "kind": kind, "active": True,
        "stage": "pending", "status": "pending", "message": STAGE_LABELS["pending"],
        "owner_id": str(actor.get("id") or "unknown"),
        "owner": str(actor.get("email") or "unknown")[:160],
        "backup_id": backup_id, "target_commit": target_commit,
        "created_at": _utc_now(), "started_at": None,
        "updated_at": _utc_now(), "updated_epoch": time.time(),
        "progress": [{"stage": "pending", "label": STAGE_LABELS["pending"], "at": _utc_now()}],
    }
    _atomic_json_write(operations_root / (operation_id + ".json"), operation)
    _atomic_json_write(Path(root) / ".backup-admin-operation.json", operation)
    if idempotency_key:
        _atomic_json_write(request_path, {"operation_id": operation_id})
    return operation, True
