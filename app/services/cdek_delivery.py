"""Separate delivery cache: never changes Bitrix order, payment or stock state."""

import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.clients.cdek import CdekClient, CdekError
from app.time_ranking import parse_erp_datetime


MOSCOW = timezone(timedelta(hours=3))
TERMINAL = frozenset(("DELIVERED", "POSTOMAT_RECEIVED", "NOT_DELIVERED", "REMOVED", "INVALID"))


def display_time(epoch):
    return datetime.fromtimestamp(epoch, MOSCOW).strftime("%d.%m.%Y %H:%M") if epoch else ""


def delivery_reference(order, tracking=None):
    if order.get("source") not in (None, "", "tictactoy"):
        raise CdekError("CDEK_SOURCE", "Синхронизация СДЭК доступна для заказов Tictactoy.")
    track = str(tracking if tracking is not None else order.get("tracking") or order.get("track_number") or "").strip()
    if track:
        if not re.fullmatch(r"[0-9]{6,20}", track):
            raise CdekError("CDEK_REFERENCE", "Укажите одну числовую накладную СДЭК в поле трекинга.")
        return {"cdek_number": track}
    number = str(order.get("number") or order.get("id") or order.get("ID") or "").strip()
    if not number or len(number) > 255:
        raise CdekError("CDEK_REFERENCE", "Не указан номер заказа магазина.")
    return {"im_number": number}


def normalize_delivery(entity):
    events = []
    statuses = entity.get("statuses")
    if not isinstance(statuses, list):
        raise CdekError("CDEK_RESPONSE", "СДЭК не вернул историю доставки.")
    for item in statuses:
        if not isinstance(item, dict) or item.get("deleted"):
            continue
        try:
            parsed = parse_erp_datetime(item.get("date_time"))
        except (TypeError, ValueError, OverflowError):
            parsed = None
        if not parsed or not item.get("code"):
            raise CdekError("CDEK_RESPONSE", "Некорректная история доставки СДЭК.")
        instant = parsed[0]
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        epoch = instant.timestamp()
        events.append({
            "code": str(item["code"])[:100],
            "name": str(item.get("name") or item["code"])[:250],
            "city": str(item.get("city") or "")[:200],
            "epoch": epoch, "date_display": display_time(epoch),
        })
    events.sort(key=lambda item: item["epoch"], reverse=True)
    current = events[0] if events else {}
    return {
        "cdek_number": str(entity["cdek_number"]),
        "shop_number": str(entity.get("number") or ""),
        "is_return": bool(entity.get("is_return")),
        "status": current.get("name", "Статус ещё не получен"),
        "status_code": current.get("code", ""),
        "date_display": current.get("date_display", ""),
        "city": current.get("city", ""), "events": events,
    }


class CdekDelivery:
    def __init__(self, path=None, client=None, clock=None):
        self.path = Path(path or os.getenv("CDEK_CACHE_DIR") or "instance/cdek")
        self.client = client if client is not None else CdekClient()
        self.clock = clock or time.time

    def _path(self, order_id):
        digest = hashlib.sha256(str(order_id).encode("utf-8")).hexdigest()
        return self.path / (digest + ".json")

    def read(self, order_id):
        try:
            data = json.loads(self._path(order_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise CdekError("CDEK_CACHE", "Не удалось прочитать сохранённые данные СДЭК.") from None
        if not isinstance(data, dict) or data.get("order_id") != str(order_id):
            raise CdekError("CDEK_CACHE", "Повреждены сохранённые данные СДЭК.")
        for field in ("attempted_at", "checked_at"):
            value = data.get(field, 0)
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise CdekError("CDEK_CACHE", "Повреждены сохранённые данные СДЭК.")
        if not isinstance(data.get("reference"), dict) or (data.get("error") and not data.get("error_code")):
            raise CdekError("CDEK_CACHE", "Повреждены сохранённые данные СДЭК.")
        return data

    def view(self, order, tracking=None):
        if not order or order.get("source") == "wildberries":
            return {}
        order_id = order.get("id") or order.get("ID")
        try:
            reference = delivery_reference(order, tracking)
            data = self.read(order_id)
            if data.get("reference") != reference:
                data = {}
        except CdekError as error:
            return {"configured": self.client.configured, "error": str(error)}
        data = dict(data)
        data["configured"] = self.client.configured
        max_age = 90000 if data.get("status_code") in TERMINAL else 1200
        data["stale"] = bool(data.get("checked_at") and self.clock() - data["checked_at"] > max_age)
        return data

    @contextmanager
    def lock(self):
        self.path.mkdir(parents=True, exist_ok=True)
        handle = (self.path / ".sync.lock").open("a+b")
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, 2)
                if not handle.tell():
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                except OSError:
                    raise CdekError("CDEK_BUSY", "Синхронизация СДЭК уже выполняется.") from None
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    raise CdekError("CDEK_BUSY", "Синхронизация СДЭК уже выполняется.") from None
            yield
        finally:
            if acquired:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def _write(self, order_id, data):
        fd, temporary = tempfile.mkstemp(prefix=".cdek-", dir=str(self.path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, str(self._path(order_id)))
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def sync(self, order, tracking=None):
        reference = delivery_reference(order, tracking)
        order_id = str(order.get("id") or order.get("ID") or "")
        if not order_id:
            raise CdekError("CDEK_REFERENCE", "Не указан идентификатор заказа ERP.")
        with self.lock():
            previous = self.read(order_id)
            if previous.get("reference") != reference:
                previous = {}
            # Duplicate clicks reuse a recent result; errors retain their backoff too.
            if previous and self.clock() - previous.get("attempted_at", 0) < 60:
                if previous.get("error"):
                    raise CdekError(previous["error_code"], previous["error"])
                return previous
            data = dict(previous)
            data.update(order_id=order_id, reference=reference, attempted_at=self.clock())
            try:
                entity = self.client.get_order(**reference)
                data.update(normalize_delivery(entity))
            except CdekError as error:
                data.update(error=str(error), error_code=error.code)
                self._write(order_id, data)
                raise
            data.update(checked_at=self.clock(), checked_display=display_time(self.clock()), error="", error_code="")
            self._write(order_id, data)
            return data

    def sync_pending(self, orders, limit=100, budget=180, sleep=time.sleep):
        """Fair, bounded polling; completed shipments are rechecked daily."""
        if not self.client.configured:
            raise CdekError("CDEK_NOT_CONFIGURED", "Ключ API СДЭК ещё не подключён к ERP.")
        candidates = []
        result = {"updated": 0, "errors": 0, "skipped": 0}
        for order in orders:
            if order.get("source") not in (None, "", "tictactoy"):
                continue
            try:
                delivery_reference(order)
            except CdekError:
                result["skipped"] += 1
                continue
            data = self.view(order)
            attempted = data.get("attempted_at", 0)
            interval = 86400 if data.get("status_code") in TERMINAL else 3600 if data.get("error") else 600
            if attempted and self.clock() - attempted < interval:
                result["skipped"] += 1
            else:
                candidates.append((attempted, order))
        candidates.sort(key=lambda item: item[0])
        started = time.monotonic()
        for _, order in candidates[:max(1, min(int(limit), 100))]:
            if time.monotonic() - started >= budget:
                break
            try:
                self.sync(order)
                result["updated"] += 1
            except CdekError as error:
                result["errors"] += 1
                result["last_error"] = error.code
                if error.code in {"CDEK_UNAUTHORIZED", "CDEK_NETWORK", "CDEK_RATE_LIMIT", "CDEK_BUSY", "CDEK_CACHE"}:
                    break
            sleep(0.3)
        return result
