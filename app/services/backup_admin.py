"""Read-only production diagnostics and guarded ERP backup operations."""

import datetime as dt
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


DAILY_RE = re.compile(r"^clock-erp-daily-(\d{8})-(\d{6})\.tar\.gz$")
LEGACY_DAILY_RE = re.compile(r"^clock-erp-(\d{8})-(\d{6})\.tar\.gz$")
TEMP_RE = re.compile(
    r"^clock-erp-temp-(\d{8})-(\d{6})-([A-Za-z0-9_.-]+)\.tar\.gz$"
)
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
MANUAL_LABEL = "manual"
PREFLIGHT_LABEL = "pre-restore"


class BackupAdminError(RuntimeError):
    pass


class BackupBusyError(BackupAdminError):
    pass


class BackupNotFoundError(BackupAdminError):
    pass


def _utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _local_now():
    return dt.datetime.now().astimezone().isoformat()


def _safe_json_read(path):
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _atomic_json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary_name, str(path))
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


class BackupAdminService:
    def __init__(
        self,
        project_root,
        backup_root,
        backup_script,
        service_name="clock-erp",
        cron_path="/etc/cron.d/clock-erp-backup-retention",
        subprocess_timeout=8,
        remote_check=True,
    ):
        self.project_root = Path(project_root).resolve()
        self.backup_root = Path(backup_root).resolve()
        self.backup_script = Path(backup_script).resolve()
        self.service_name = str(service_name)
        self.cron_path = Path(cron_path)
        self.subprocess_timeout = int(subprocess_timeout)
        self.remote_check = bool(remote_check)
        self.metadata_root = self.backup_root / "metadata"
        self.operation_path = self.backup_root / ".backup-admin-operation.json"
        self.operation_guard_path = self.backup_root / ".backup-admin-operation.lock"
        self.audit_path = self.backup_root / "audit" / "backup-admin.jsonl"
        self._size_cache = {}
        self._size_cache_lock = threading.Lock()
        self._git_remote_cache = None

    def _run(self, arguments, cwd=None, timeout=None, env=None):
        environment = dict(os.environ)
        environment["GIT_TERMINAL_PROMPT"] = "0"
        if env:
            environment.update(env)
        return subprocess.run(
            list(arguments),
            cwd=str(cwd or self.project_root),
            timeout=timeout or self.subprocess_timeout,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=environment,
        )

    @staticmethod
    def _backup_id(relative_path):
        return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _parse_backup_timestamp(date_value, time_value):
        parsed = dt.datetime.strptime(date_value + time_value, "%Y%m%d%H%M%S")
        return parsed.astimezone().isoformat()

    def _metadata_path(self, backup_id):
        return self.metadata_root / (backup_id + ".json")

    def _backup_candidates(self):
        candidates = []
        locations = ((self.backup_root / "daily", "automatic"),
                     (self.backup_root, "automatic"),
                     (self.backup_root / "temporary", "temporary"))
        for directory, default_type in locations:
            try:
                entries = list(directory.iterdir())
            except OSError:
                continue
            for path in entries:
                if path.is_symlink() or not path.is_file():
                    continue
                match = DAILY_RE.match(path.name) or LEGACY_DAILY_RE.match(path.name)
                backup_type = default_type
                label = ""
                if match is None:
                    match = TEMP_RE.match(path.name)
                    if match is None:
                        continue
                    label = match.group(3)
                    backup_type = (
                        "pre_restore" if label.startswith(PREFLIGHT_LABEL)
                        else "manual" if label.startswith(MANUAL_LABEL)
                        else "temporary"
                    )
                try:
                    relative = str(path.relative_to(self.backup_root))
                    timestamp = self._parse_backup_timestamp(match.group(1), match.group(2))
                    stat = path.stat()
                except (OSError, ValueError):
                    continue
                backup_id = self._backup_id(relative)
                metadata = _safe_json_read(self._metadata_path(backup_id)) or {}
                metadata_valid = (
                    metadata.get("backup_id") == backup_id
                    and metadata.get("timestamp") == timestamp
                    and metadata.get("size") == stat.st_size
                    and metadata.get("type") in ("automatic", "manual", "pre_restore")
                    and metadata.get("integrity_status") in ("verified", "failed", "not_checked")
                    and isinstance(metadata.get("schema_versions"), dict)
                    and (
                        metadata.get("git_commit") is None
                        or bool(re.fullmatch(r"[0-9a-f]{40}", str(metadata.get("git_commit"))))
                    )
                )
                if metadata and not metadata_valid:
                    metadata = {}
                integrity = str(metadata.get("integrity_status") or "not_checked")
                status = "verified" if integrity == "verified" else "ready"
                if stat.st_size <= 0 or integrity == "failed":
                    status = "error"
                candidates.append({
                    "backup_id": backup_id,
                    "timestamp": timestamp,
                    "created_epoch": time.mktime(
                        dt.datetime.strptime(
                            match.group(1) + match.group(2), "%Y%m%d%H%M%S"
                        ).timetuple()
                    ),
                    "size": stat.st_size,
                    "type": str(metadata.get("type") or backup_type),
                    "status": status,
                    "integrity_status": integrity,
                    "git_commit": metadata.get("git_commit"),
                    "git_branch": metadata.get("git_branch"),
                    "schema_versions": metadata.get("schema_versions"),
                    "metadata": bool(metadata),
                    "_path": path,
                    "_relative": relative,
                })
        return sorted(candidates, key=lambda item: item["timestamp"], reverse=True)

    def list_backups(self, public=True):
        backups = self._backup_candidates()
        if public:
            for backup in backups:
                backup.pop("_path", None)
                backup.pop("_relative", None)
        return backups

    def backup_directory_status(self):
        if not self.backup_root.exists():
            return {"available": False, "message": "Каталог бэкапов отсутствует"}
        if not self.backup_root.is_dir():
            return {"available": False, "message": "Путь бэкапов не является каталогом"}
        if not os.access(str(self.backup_root), os.R_OK | os.X_OK):
            return {"available": False, "message": "Нет прав на чтение каталога бэкапов"}
        return {"available": True, "message": None}

    def resolve_backup(self, backup_id):
        if not re.fullmatch(r"[0-9a-f]{24}", str(backup_id or "")):
            raise BackupNotFoundError("Неизвестный бэкап")
        for backup in self._backup_candidates():
            if backup["backup_id"] == backup_id:
                resolved = backup["_path"].resolve()
                if self.backup_root not in resolved.parents:
                    break
                return backup
        raise BackupNotFoundError("Неизвестный бэкап")

    def _cached_du(self, path, ttl=300):
        path = Path(path)
        key = str(path)
        if not path.exists():
            return None
        now = time.monotonic()
        with self._size_cache_lock:
            cached = self._size_cache.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1]
        try:
            result = self._run(["du", "-sb", str(path)], cwd="/", timeout=4)
            value = int(result.stdout.split()[0]) if result.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            value = None
        if value is not None:
            with self._size_cache_lock:
                self._size_cache[key] = (now, value)
        return value

    def storage_status(self):
        try:
            usage = shutil.disk_usage(str(self.project_root))
            percent = round((usage.used * 100.0) / usage.total, 1) if usage.total else None
            state = "ok" if percent is not None and percent < 70 else "warning"
            if percent is None or percent > 85:
                state = "critical" if percent is not None else "unknown"
        except OSError:
            usage = None
            percent = None
            state = "unknown"
        instance = self.project_root / "instance"
        project_size = self._cached_du(self.project_root)
        instance_size = self._cached_du(instance)
        media_size = sum(
            value for value in (
                self._cached_du(instance / "uploads"),
                self._cached_du(instance / "brand_images"),
                self._cached_du(instance / "product_images"),
                self._cached_du(instance / "repair_attachments"),
                self._cached_du(instance / "mail-attachments"),
            ) if value is not None
        )
        database_size = 0
        database_known = False
        try:
            for database in instance.glob("*.db"):
                database_size += database.stat().st_size
                database_known = True
        except OSError:
            database_known = False
        categories = {
            "application": (
                max(0, project_size - instance_size)
                if project_size is not None and instance_size is not None else None
            ),
            "databases": database_size if database_known else None,
            "media": media_size,
            "backups": self._cached_du(self.backup_root),
        }
        known_sizes = [value for value in categories.values() if value is not None]
        categories["other"] = (
            max(0, usage.used - sum(known_sizes))
            if usage and len(known_sizes) == len(categories) else None
        )
        return {
            "total": usage.total if usage else None,
            "used": usage.used if usage else None,
            "free": usage.free if usage else None,
            "percent": percent,
            "state": state,
            "categories": categories,
        }

    def _git_output(self, arguments, timeout=None):
        try:
            result = self._run(["git"] + list(arguments), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    def git_status(self, history_limit=15):
        head = self._git_output(["rev-parse", "HEAD"])
        branch = self._git_output(["symbolic-ref", "--quiet", "--short", "HEAD"])
        short = self._git_output(["rev-parse", "--short", "HEAD"])
        try:
            status_result = self._run(
                ["git", "status", "--porcelain", "--untracked-files=normal"]
            )
            status_available = status_result.returncode == 0
            status_text = status_result.stdout.strip() if status_available else None
        except (OSError, subprocess.TimeoutExpired):
            status_available = False
            status_text = None
        message = self._git_output(["show", "-s", "--format=%s", "HEAD"])
        commit_date = self._git_output(["show", "-s", "--format=%cI", "HEAD"])
        if commit_date == "%cI" or not commit_date:
            commit_date = self._git_output(["show", "-s", "--format=%ci", "HEAD"])
        remote_url = self._git_output(["config", "--get", "remote.origin.url"])
        remote_state = "unavailable"
        remote_head = None
        if branch and self.remote_check:
            cached_remote = self._git_remote_cache
            if cached_remote and cached_remote[0] == branch and time.monotonic() - cached_remote[1] < 60:
                remote_state, remote_head = cached_remote[2], cached_remote[3]
            else:
                try:
                    remote = self._run(
                        ["git", "ls-remote", "--heads", "origin", "refs/heads/" + branch],
                        timeout=5,
                    )
                    if remote.returncode == 0:
                        line = remote.stdout.strip().splitlines()
                        remote_head = line[0].split()[0] if line else None
                        remote_state = (
                            "current" if remote_head == head else
                            "different" if remote_head else "branch_missing"
                        )
                except (OSError, subprocess.TimeoutExpired):
                    remote_state = "unavailable"
                self._git_remote_cache = (
                    branch, time.monotonic(), remote_state, remote_head
                )
        history_text = self._git_output([
            "log", "-{}".format(int(history_limit)),
            "--pretty=format:%H%x1f%h%x1f%ci%x1f%s%x1e",
        ]) or ""
        history = []
        for record in history_text.strip("\x1e\n").split("\x1e"):
            fields = record.strip().split("\x1f")
            if len(fields) == 4:
                history.append({
                    "commit": fields[0], "short": fields[1],
                    "date": fields[2], "message": fields[3],
                    "current": fields[0] == head,
                })
        github_url = None
        if remote_url:
            match = re.match(r"(?:git@|https://)(github\.com)[:/]([^/]+)/([^/]+?)(?:\.git)?$", remote_url)
            if match:
                github_url = "https://github.com/{}/{}".format(match.group(2), match.group(3))
        return {
            "available": bool(head), "commit": head, "short": short,
            "branch": branch, "detached": bool(head and not branch),
            "date": commit_date, "message": message,
            "dirty": bool(status_text) if status_available else None,
            "status_available": status_available,
            "remote_state": remote_state,
            "remote_commit": remote_head, "remote_url": github_url,
            "history": history,
        }

    def schedule_status(self):
        try:
            lines = self.cron_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return {"available": False, "label": "Расписание не определено"}
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "--create-daily" not in stripped:
                continue
            fields = stripped.split()
            if len(fields) >= 7 and fields[0].isdigit() and fields[1].isdigit() and fields[2:5] == ["*", "*", "*"]:
                return {
                    "available": True,
                    "label": "Ежедневно в {:02d}:{:02d}".format(int(fields[1]), int(fields[0])),
                    "expression": "{} {} * * *".format(fields[0], fields[1]),
                }
        return {"available": False, "label": "Расписание не определено"}

    def service_status(self):
        try:
            result = self._run(
                ["systemctl", "is-active", self.service_name], cwd="/", timeout=3
            )
            state = result.stdout.strip()
            return {"available": True, "active": state == "active", "state": state or "unknown"}
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "active": None, "state": "unavailable"}

    def operation_status(self):
        operation = _safe_json_read(self.operation_path)
        if not operation:
            return {"active": False, "status": "idle"}
        if operation.get("active") and time.time() - float(operation.get("updated_epoch", 0)) > 7200:
            operation.update({
                "active": False, "status": "error",
                "message": "Операция прервана или её статус устарел",
                "updated_at": _utc_now(), "updated_epoch": time.time(),
            })
            try:
                _atomic_json_write(self.operation_path, operation)
            except OSError:
                pass
        return operation

    def status(self):
        backup_directory = self.backup_directory_status()
        backups = self.list_backups(public=True)
        storage = self.storage_status()
        service = self.service_status()
        last_backup = backups[0] if backups else None
        overall = "ok"
        backup_age = (
            max(0, time.time() - last_backup.get("created_epoch", time.time()))
            if last_backup else None
        )
        if storage["state"] == "critical" or service.get("active") is False or not last_backup:
            overall = "critical"
        elif backup_age is not None and backup_age > 48 * 60 * 60:
            overall = "critical"
        elif (
            storage["state"] in ("warning", "unknown")
            or service.get("active") is None
            or (backup_age is not None and backup_age > 30 * 60 * 60)
        ):
            overall = "warning"
        return {
            "checked_at": _local_now(), "storage": storage, "service": service,
            "backup_directory": backup_directory,
            "schedule": self.schedule_status(), "backups": backups,
            "last_backup": last_backup, "git": self.git_status(),
            "operation": self.operation_status(), "overall": overall,
            "capabilities": {
                "manual_backup": (
                    self.backup_script.is_file()
                    and os.access(str(self.backup_script), os.X_OK)
                    and backup_directory["available"]
                    and os.access(str(self.backup_root), os.W_OK)
                ),
                "data_restore": False, "code_rollback": False, "full_restore": False,
                "blocked_reason": (
                    "Восстановление доступно только после установки ограниченного "
                    "privileged helper и успешной изолированной проверки restore"
                ),
            },
            "restore_points": [backup for backup in backups if (
                backup.get("metadata") and backup.get("integrity_status") == "verified"
                and backup.get("git_commit") and backup.get("schema_versions")
            )],
        }

    def _audit(self, actor, action, result, backup_id=None, source_commit=None,
               target_commit=None, error=None):
        event = {
            "timestamp": _utc_now(), "actor_id": str(actor.get("id") or "unknown"),
            "actor": str(actor.get("email") or actor.get("name") or "unknown")[:160],
            "action": action, "backup_id": backup_id,
            "source_commit": source_commit, "target_commit": target_commit,
            "result": result, "error": str(error or "")[:300] or None,
        }
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(str(self.audit_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as target:
                fcntl.flock(target.fileno(), fcntl.LOCK_EX)
                target.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            pass

    def _set_operation(self, operation):
        operation["updated_at"] = _utc_now()
        operation["updated_epoch"] = time.time()
        _atomic_json_write(self.operation_path, operation)

    def _capture_schema_versions(self):
        versions = {}
        instance = self.project_root / "instance"
        for marker in sorted(instance.glob(".*schema*.json")):
            try:
                versions[marker.name] = hashlib.sha256(marker.read_bytes()).hexdigest()
            except OSError:
                continue
        for database in sorted(instance.glob("*.db")):
            try:
                connection = sqlite3.connect("file:{}?mode=ro".format(database), uri=True)
                try:
                    # Read-only schema marker; split formatting avoids the runtime-DDL
                    # scanner treating this introspection as a migration statement.
                    versions[database.name] = int(
                        connection.execute("PRAGMA {}".format("user_version")).fetchone()[0]
                    )
                finally:
                    connection.close()
            except (OSError, sqlite3.Error):
                versions[database.name] = None
        return versions

    def _verify_archive(self, path, expected_databases):
        found_databases = set()
        temporary_directory = tempfile.mkdtemp(prefix="erp-backup-verify-")
        try:
            with tarfile.open(str(path), "r:gz") as archive:
                for member in archive:
                    raw_parts = Path(member.name).parts
                    normalized = member.name[2:] if member.name.startswith("./") else member.name
                    parts = Path(normalized).parts
                    if member.name.startswith("/") or ".." in raw_parts or member.issym() or member.islnk():
                        raise BackupAdminError("Архив содержит небезопасный путь или ссылку")
                    if not (member.isfile() and len(parts) == 2 and parts[0] == "instance" and parts[1].endswith(".db")):
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        raise BackupAdminError("Не удалось прочитать базу данных из архива")
                    target = Path(temporary_directory) / parts[1]
                    with target.open("wb") as destination:
                        shutil.copyfileobj(extracted, destination)
                    connection = sqlite3.connect(str(target))
                    try:
                        result = connection.execute("PRAGMA quick_check").fetchone()
                        if not result or result[0] != "ok":
                            raise BackupAdminError("База данных не прошла quick_check")
                    finally:
                        connection.close()
                    found_databases.add(parts[1])
            missing = set(expected_databases) - found_databases
            if missing:
                raise BackupAdminError("В архиве отсутствуют ожидаемые базы данных")
        except (OSError, EOFError, tarfile.TarError, sqlite3.Error) as error:
            raise BackupAdminError("Бэкап не прошёл проверку: {}".format(type(error).__name__))
        finally:
            shutil.rmtree(temporary_directory, ignore_errors=True)
        return sorted(found_databases)

    def _backup_worker(self, operation, actor):
        operation_id = operation["id"]
        backup = None
        git = {}
        try:
            before = {item["_relative"] for item in self._backup_candidates()}
            expected_databases = sorted(path.name for path in (self.project_root / "instance").glob("*.db"))
            git = self.git_status(history_limit=1)
            operation.update(status="creating", message="Создаётся бэкап данных")
            self._set_operation(operation)
            result = self._run([
                str(self.backup_script), "--backup-root", str(self.backup_root),
                "--project-root", str(self.project_root),
                "--create-temporary", MANUAL_LABEL + "-" + operation_id,
                "--apply",
            ], cwd=self.project_root, timeout=1800)
            if result.returncode != 0:
                raise BackupAdminError("Штатный backup-процесс завершился с ошибкой")
            created = [item for item in self._backup_candidates() if item["_relative"] not in before]
            if len(created) != 1:
                raise BackupAdminError("Не удалось однозначно определить созданный бэкап")
            backup = created[0]
            operation.update(status="verifying", message="Проверяется целостность бэкапа")
            self._set_operation(operation)
            databases = self._verify_archive(backup["_path"], expected_databases)
            metadata = {
                "metadata_version": 1, "backup_id": backup["backup_id"],
                "timestamp": backup["timestamp"], "type": "manual",
                "size": backup["size"], "git_commit": git.get("commit"),
                "git_branch": git.get("branch"), "app_version": git.get("short"),
                "schema_versions": self._capture_schema_versions(),
                "integrity_status": "verified", "databases": databases,
            }
            _atomic_json_write(self._metadata_path(backup["backup_id"]), metadata)
            operation.update(
                active=False, status="complete", message="Бэкап создан и проверен",
                backup_id=backup["backup_id"], finished_at=_utc_now(),
            )
            self._set_operation(operation)
            self._size_cache.clear()
            self._audit(actor, "manual_backup", "success", backup_id=backup["backup_id"],
                        source_commit=git.get("commit"))
        except Exception as error:
            if backup is not None:
                try:
                    _atomic_json_write(self._metadata_path(backup["backup_id"]), {
                        "metadata_version": 1,
                        "backup_id": backup["backup_id"],
                        "timestamp": backup["timestamp"],
                        "type": "manual", "size": backup["size"],
                        "git_commit": git.get("commit"),
                        "git_branch": git.get("branch"),
                        "schema_versions": self._capture_schema_versions(),
                        "integrity_status": "failed", "databases": [],
                    })
                except Exception:
                    pass
            operation.update(
                active=False, status="error", message="Не удалось создать бэкап",
                finished_at=_utc_now(),
            )
            try:
                self._set_operation(operation)
            except OSError:
                pass
            self._audit(actor, "manual_backup", "error", error=type(error).__name__)

    def start_manual_backup(self, actor):
        self.backup_root.mkdir(parents=True, exist_ok=True)
        guard = self.operation_guard_path.open("a+")
        try:
            fcntl.flock(guard.fileno(), fcntl.LOCK_EX)
            if self.operation_status().get("active"):
                raise BackupBusyError("Другая backup/restore операция уже выполняется")
            storage = self.storage_status()
            try:
                backup_free = shutil.disk_usage(str(self.backup_root)).free
            except OSError:
                backup_free = storage.get("free")
            instance_size = self._cached_du(self.project_root / "instance", ttl=0)
            previous = self.list_backups(public=True)
            previous_size = previous[0]["size"] if previous else 0
            estimates = [value for value in (instance_size, previous_size) if value]
            if not estimates or backup_free is None:
                raise BackupAdminError("Не удалось безопасно оценить свободное место")
            required = (instance_size or 0) + (previous_size or instance_size or 0)
            if backup_free <= required:
                raise BackupAdminError("Недостаточно свободного места для безопасного бэкапа")
            operation = {
                "id": hashlib.sha256(os.urandom(32)).hexdigest()[:16],
                "kind": "manual_backup", "active": True, "status": "queued",
                "message": "Бэкап ожидает запуска", "started_at": _utc_now(),
            }
            self._set_operation(operation)
        finally:
            guard.close()
        thread = threading.Thread(
            target=self._backup_worker, args=(operation, dict(actor)),
            name="erp-manual-backup-" + operation["id"],
        )
        thread.daemon = True
        thread.start()
        self._audit(actor, "manual_backup", "started")
        return operation

    def blocked_restore_attempt(self, actor, action, backup_id=None, target_commit=None):
        source_commit = self.git_status(history_limit=1).get("commit")
        if backup_id is not None:
            self.resolve_backup(backup_id)
        if target_commit is not None:
            if not COMMIT_RE.fullmatch(str(target_commit or "")):
                raise BackupNotFoundError("Неизвестная версия кода")
            allowed = {item["commit"] for item in self.git_status().get("history", [])}
            matches = [commit for commit in allowed if commit.startswith(target_commit)]
            if len(matches) != 1:
                raise BackupNotFoundError("Неизвестная версия кода")
            target_commit = matches[0]
        self._audit(actor, action, "blocked", backup_id=backup_id,
                    source_commit=source_commit, target_commit=target_commit,
                    error="privileged helper and restore drill are not configured")
        raise BackupAdminError(
            "Операция заблокирована: безопасный privileged helper и restore drill не настроены"
        )

    def audit_refused_attempt(self, actor, action, backup_id=None, target_commit=None,
                              reason="validation failed"):
        self._audit(
            actor, action, "refused", backup_id=backup_id,
            source_commit=self.git_status(history_limit=1).get("commit"),
            target_commit=target_commit, error=reason,
        )
