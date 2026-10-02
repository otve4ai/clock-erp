"""Shared, bounded manual/automatic CDEK batch and its persisted status."""
import json
import os
import tempfile
import threading
from pathlib import Path

from app.clients.cdek import CdekError
from app.services.cdek_delivery import display_time
from app.services.bitrix_site_status_sync import SiteStatusSyncLock


class CdekSync:
    def __init__(self, delivery):
        self.delivery = delivery
        self.directory = delivery.path / "batch"
        self.path = self.directory / "status.json"

    def lock(self):
        return SiteStatusSyncLock(path=self.directory / "run.lock")

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError()
            return data
        except FileNotFoundError:
            return {"outcome": "unknown"}
        except (OSError, ValueError):
            raise CdekError("CDEK_CACHE", "Не удалось прочитать результат синхронизации.") from None

    def write(self, data):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".run-", dir=str(self.directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, str(self.path))
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def summary(self):
        data = self.read()
        if data.get("outcome") == "running":
            lock = self.lock()
            if lock.acquire():
                try:
                    data = self.read()
                    if data.get("outcome") == "running":
                        data = dict(data, outcome="error", message="Обновление прервано. Повторите запуск.")
                finally:
                    lock.release()
        return dict(data, configured=self.delivery.client.configured,
                    last_attempt_display=display_time(data.get("started_at", 0)),
                    last_success_display=display_time(data.get("last_success_at", 0)))

    def begin(self, source):
        if not self.delivery.client.configured:
            raise CdekError("CDEK_NOT_CONFIGURED", "API СДЭК не подключён.")
        lock = self.lock()
        if not lock.acquire():
            raise CdekError("CDEK_BUSY", "Обновление уже выполняется.")
        try:
            old = self.read()
            if source == "manual" and self.delivery.clock() - old.get("started_at", 0) < 60:
                raise CdekError("CDEK_BUSY", "Повторный запуск доступен через минуту.")
            data = dict(outcome="running", source=source, started_at=self.delivery.clock(),
                        last_success_at=old.get("last_success_at", 0), message="")
            self.write(data)
            return lock, data
        except Exception:
            lock.release()
            raise

    def execute(self, load_orders, lock, data):
        try:
            result = self.delivery.sync_pending(load_orders(), manual=data["source"] == "manual")
            data.update(result)
            data.update(outcome="error" if result["errors"] else "partial" if result.get("remaining") else "success",
                        finished_at=self.delivery.clock(), message="Есть ошибки получения статусов." if result["errors"] else "Проверена часть отправлений. Остальные — в следующем запуске." if result.get("remaining") else "")
            if not result["errors"] and not result.get("remaining"):
                data["last_success_at"] = self.delivery.clock()
            self.write(data)
            return result
        except Exception as error:
            data.update(outcome="error", finished_at=self.delivery.clock(),
                        message=str(error) if isinstance(error, CdekError) else "Не удалось завершить обновление.")
            self.write(data)
            return {"updated": 0, "errors": 1, "skipped": 0}
        finally:
            lock.release()

    def run(self, load_orders, source="automatic"):
        lock, data = self.begin(source)
        return self.execute(load_orders, lock, data)

    def start(self, load_orders, app):
        lock, data = self.begin("manual")
        def worker():
            with app.app_context():
                self.execute(load_orders, lock, data)
        try:
            threading.Thread(target=worker, name="cdek-sync", daemon=True).start()
        except Exception:
            lock.release()
            raise
