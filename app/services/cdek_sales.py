"""Shipment read model and manager workflow, independent of sales/stock writes."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta

from app.clients.cdek import CdekError
from app.services.cdek_delivery import MOSCOW, display_time
from app.time_ranking import parse_erp_datetime

DAY = 86400
PICKUP = {"ACCEPTED_AT_PICK_UP_POINT", "POSTOMAT_POSTED"}
DELIVERED = {"DELIVERED", "POSTOMAT_RECEIVED"}
TRANSIT = {
    "READY_FOR_SHIPMENT_IN_SENDER_CITY", "TAKEN_BY_TRANSPORTER_FROM_SENDER_CITY",
    "SENT_TO_RECIPIENT_CITY", "ACCEPTED_IN_RECIPIENT_CITY",
    "ACCEPTED_AT_RECIPIENT_CITY_WAREHOUSE", "ACCEPTED_AT_TRANSIT_WAREHOUSE",
    "RETURNED_TO_SENDER_CITY_WAREHOUSE", "RETURNED_TO_TRANSIT_WAREHOUSE",
    "RETURNED_TO_RECIPIENT_CITY_WAREHOUSE", "READY_FOR_SHIPMENT_IN_TRANSIT_CITY",
    "TAKEN_BY_TRANSPORTER_FROM_TRANSIT_CITY", "SENT_TO_TRANSIT_CITY",
    "ACCEPTED_IN_TRANSIT_CITY", "SENT_TO_SENDER_CITY", "ACCEPTED_IN_SENDER_CITY",
    "ENTERED_TO_TRANSIT_WAREHOUSE", "ENTERED_TO_RECIPIENT_CITY_WAREHOUSE",
    "ENTERED_TO_PICK_UP_POINT", "IN_CUSTOMS_INTERNATIONAL", "SHIPPED_TO_DESTINATION",
    "PASSED_TO_TRANSIT_CARRIER", "IN_CUSTOMS_LOCAL", "CUSTOMS_COMPLETE",
}
LABELS = {
    "ACCEPTED": ("Создан", "yellow"), "CREATED": ("Создан", "yellow"),
    "RECEIVED_AT_SHIPMENT_WAREHOUSE": ("Принят", "yellow"),
    "TAKEN_BY_COURIER": ("У курьера", "yellow"),
    "REMOVED": ("Отменён", "red"), "INVALID": ("Нет данных", "gray"),
    "NOT_DELIVERED": ("Не вручён", "red"),
    "POSTOMAT_SEIZED": ("Возврат", "red"),
}
WORK = {"new": "Нужна реакция", "working": "В работе", "today": "Связаться сегодня",
        "tomorrow": "Связаться завтра", "closed": "Возврат проверен"}
CATEGORIES = {"pvz": "Не забирают", "delay": "Задержки", "return": "Возвраты",
              "data": "Ошибки данных"}


def shipment_id(sale):
    """Never use the ERP sale id when a waybill is available."""
    source = str(sale.get("source_key") or sale.get("source") or "").strip().lower()
    if source not in ("tictactoy", "битрикс", "заказ битрикс"):
        return None
    track = str(sale.get("track_number") or "").strip()
    number = str(sale.get("order_number") or "").strip()
    reference = "track:" + track if track else "order:" + number if number else "sale:" + str(sale.get("id"))
    return hashlib.sha256(reference.encode("utf-8")).hexdigest()


def group_sales(sales):
    groups = {}
    for sale in sales:
        key = shipment_id(sale)
        if key is None:
            continue
        # A cancelled sale with no physical shipment is not a delivery task.
        if sale.get("is_cancelled") and not sale.get("track_number"):
            continue
        group = groups.setdefault(key, {"id": key, "source": "tictactoy",
            "number": str(sale.get("order_number") or "").strip(),
            "tracking": str(sale.get("track_number") or "").strip(),
            "orders": [], "sale_ids": [], "items": [], "order_date": ""})
        parsed = parse_erp_datetime(sale.get("created_at"))
        if parsed:
            created = parsed[0]
            if created.tzinfo is not None:
                created = created.astimezone(MOSCOW)
            date = created.strftime("%Y-%m-%d")
            if not group["order_date"] or date < group["order_date"]:
                group["order_date"] = date
        for field, value in (("orders", str(sale.get("order_number") or "")),
                             ("sale_ids", str(sale.get("id") or "")),
                             ("items", str(sale.get("product_name") or ""))):
            if value and value not in group[field]:
                group[field].append(value)
    return list(groups.values())


def order_number_key(row):
    """Natural number order: 2 before 10, including prefixed references."""
    def natural(value):
        return tuple((0, int(part)) if part.isdecimal() else (1, part.casefold())
                     for part in re.split(r"(\d+)", value) if part)
    return min((natural(number) for number in row["orders"]), default=())


def short_status(data, track):
    code = data.get("status_code", "")
    if code in DELIVERED:
        return ("Возврат", "red") if data.get("is_return") else ("Вручён", "green")
    if data.get("is_return"):
        return "Возврат", "red"
    if code in PICKUP:
        return "В ПВЗ", "yellow"
    if code in TRANSIT:
        return "В пути", "blue"
    return LABELS.get(code, ("Нет данных", "gray") if track else ("Нет трека", "gray"))


class CdekSales:
    def __init__(self, delivery, clock=None):
        self.delivery = delivery
        self.clock = clock or delivery.clock
        self.review_path = delivery.path / "reviews"
        self.pvz_warning = self._threshold("CDEK_PVZ_WARNING_DAYS", 3)
        self.pvz_urgent = max(self.pvz_warning, self._threshold("CDEK_PVZ_URGENT_DAYS", 5))
        self.transit_days = self._threshold("CDEK_TRANSIT_WARNING_DAYS", 3)

    @staticmethod
    def _threshold(name, default):
        try:
            return max(1, min(60, int(os.getenv(name, str(default)))))
        except ValueError:
            return default

    def review(self, key):
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise CdekError("CDEK_REFERENCE", "Некорректная накладная.")
        path = self.review_path / (key + ".json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 0, "work": "new", "note": "", "followup": "", "storage_until": ""}
        except (OSError, ValueError):
            raise CdekError("CDEK_REVIEW", "Не удалось прочитать отметки менеджера.") from None
        if not isinstance(data, dict) or data.get("work") not in WORK or not isinstance(data.get("version"), int):
            raise CdekError("CDEK_REVIEW", "Повреждены отметки менеджера.")
        for field in ("storage_until", "followup", "expected_delivery"):
            if data.get(field):
                try:
                    if datetime.strptime(data[field], "%Y-%m-%d").strftime("%Y-%m-%d") != data[field]:
                        raise ValueError("Noncanonical date")
                except (TypeError, ValueError):
                    raise CdekError("CDEK_REVIEW", "Повреждены даты в отметках менеджера.") from None
        return data

    def save_review(self, shipment, payload, actor):
        key = shipment["id"]
        work = str(payload.get("work") or "new")
        note = str(payload.get("note") or "").strip()
        if work not in WORK or len(note) > 2000:
            raise CdekError("CDEK_FORM", "Проверьте отметку и длину заметки (до 2000 символов).")
        storage_until = str(payload.get("storage_until") or "").strip()
        expected_delivery = str(payload.get("expected_delivery") or "").strip()
        for value in (storage_until, expected_delivery):
            if not value:
                continue
            try:
                if datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
                    raise ValueError("Noncanonical date")
            except ValueError:
                raise CdekError("CDEK_FORM", "Некорректная подтверждённая дата.") from None
        try:
            version = int(payload.get("version", -1))
        except (ValueError, TypeError):
            raise CdekError("CDEK_FORM", "Обновите страницу перед сохранением.") from None
        now = datetime.fromtimestamp(self.clock(), MOSCOW)
        followup = (now.date() + timedelta(days=1 if work == "tomorrow" else 0)).isoformat() if work in ("today", "tomorrow") else ""
        with self.delivery.lock():
            previous = self.review(key)
            if previous["version"] != version:
                raise CdekError("CDEK_CONFLICT", "Другой сотрудник изменил отметку. Обновите страницу.")
            snapshot = self.delivery.view(shipment)
            if work == "closed" and not (snapshot.get("is_return") and snapshot.get("status_code") in DELIVERED):
                raise CdekError("CDEK_FORM", "Закрыть возврат можно после его получения по данным СДЭК.")
            data = dict(version=version + 1, work=work, note=note, followup=followup,
                        storage_until=storage_until, expected_delivery=expected_delivery,
                        updated_at=self.clock(), actor=str(actor)[:100],
                        closed_event=snapshot.get("date_display", "") if work == "closed" else "")
            self.review_path.mkdir(parents=True, exist_ok=True)
            import tempfile
            fd, temporary = tempfile.mkstemp(prefix=".review-", dir=str(self.review_path))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(data, stream, ensure_ascii=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, str(self.review_path / (key + ".json")))
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return data

    def rows(self, sales):
        result = []
        now = self.clock()
        for group in group_sales(sales):
            data = self.delivery.view(group)
            label, tone = short_status(data, group["tracking"])
            try:
                review = self.review(group["id"])
                review_error = ""
            except CdekError as error:
                review = {"version": -1, "work": "new", "note": ""}
                review_error = str(error)
            issues = []
            def add(category, priority, reason, action):
                issues.append(dict(category=category, priority=priority, reason=reason, action=action))
            code = data.get("status_code", "")
            events = data.get("events") or []
            latest = events[0].get("epoch", now) if events else now
            age = max(0, int((now - latest) // DAY))
            is_delivered = code in DELIVERED and not data.get("is_return")
            return_closed = review.get("work") == "closed" and review.get("closed_event") == data.get("date_display")
            if review_error:
                add("data", 1, review_error, "Проверить хранилище отметок")
            if not data.get("configured"):
                add("data", 1, "СДЭК не подключён", "Подключить интеграцию")
            elif data.get("error") or data.get("stale"):
                add("data", 1, data.get("error") or "Данные СДЭК устарели", "Обновить статус")
            elif not code:
                add("data", 1, "Нет трека" if not group["tracking"] else "Статус ещё не получен", "Проверить данные / обновить")
            elif label == "Нет данных":
                add("data", 1, "Неизвестный или некорректный статус СДЭК", "Проверить накладную")
            wait = "—"
            if not is_delivered and not return_closed:
                if code in PICKUP and not data.get("is_return"):
                    # Consecutive pickup events belong to one storage episode.
                    start = latest
                    for event in events:
                        if event["code"] not in PICKUP:
                            break
                        start = min(start, event["epoch"])
                    days = max(0, int((now - start) // DAY))
                    wait = "{} сут. в ПВЗ".format(days)
                    if days >= self.pvz_warning:
                        add("pvz", 2 if days >= self.pvz_urgent else 1,
                            "Не забирают {} сут.".format(days), "Связаться с получателем")
                    deadline = review.get("storage_until")
                    if deadline:
                        end = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=MOSCOW) + timedelta(days=1)
                        wait += " · до " + deadline
                        if end.timestamp() - now <= DAY:
                            add("pvz", 2, "Хранение заканчивается" if end.timestamp() > now else "Срок хранения истёк", "Согласовать получение / продление")
                    else:
                        wait += " · срок уточнить"
                elif code in TRANSIT or code in {"TAKEN_BY_COURIER", "RECEIVED_AT_SHIPMENT_WAREHOUSE", "CREATED", "ACCEPTED"}:
                    wait = "{} сут. без событий".format(age)
                    if age >= self.transit_days:
                        add("delay", 1, "Нет новых событий {} сут.".format(age), "Уточнить у СДЭК")
                if label in {"Возврат", "Не вручён"}:
                    add("return", 1, "Возврат получен: нужна проверка" if code in DELIVERED else data.get("status") or label,
                        "Проверить возврат" if code in DELIVERED else "Уточнить причину / получение")
                if code in {"REMOVED", "INVALID"}:
                    add("data", 1, data.get("status") or label, "Проверить накладную")
                if review.get("expected_delivery") and code not in PICKUP and label not in {"Возврат", "Не вручён", "Отменён"}:
                    today = datetime.fromtimestamp(now, MOSCOW).date().isoformat()
                    if review["expected_delivery"] < today:
                        add("delay", 2, "Просрочена подтверждённая дата доставки", "Уточнить срок у СДЭК")
                if review.get("followup"):
                    today = datetime.fromtimestamp(now, MOSCOW).date().isoformat()
                    if review["followup"] <= today:
                        add("pvz" if code in PICKUP else "delay", 2 if review["followup"] < today else 1,
                            "Просрочен контакт" if review["followup"] < today else "Связаться сегодня", "Связаться с получателем")
            issues.sort(key=lambda issue: -issue["priority"])
            effective_work = review.get("work", "new")
            if review.get("followup") and review["followup"] <= datetime.fromtimestamp(now, MOSCOW).date().isoformat():
                effective_work = "today"
            result.append(dict(group, delivery=data, label=label, tone=tone, review=review,
                review_error=review_error, issues=issues, priority=max([i["priority"] for i in issues] or [0]),
                wait=wait, work=effective_work, delivered=is_delivered, event_epoch=latest,
                checked_display=display_time(data.get("checked_at", 0)),
                stale=bool(data.get("stale") or data.get("error") or not data.get("checked_at"))))
        return sorted(result, key=lambda r: (-r["priority"], r["work"] == "working", r["event_epoch"], r["id"]))

    def summary(self, rows):
        active = [row for row in rows if row["issues"] or not row["delivered"] or row["event_epoch"] >= self.clock() - 30 * DAY]
        counts = {label: sum(row["label"] == label for row in active) for label in ("В пути", "В ПВЗ", "У курьера", "Вручён")}
        counts.update(total=len(active), problems=sum(bool(r["issues"]) for r in active),
                      urgent=sum(r["priority"] == 2 for r in active),
                      stale=sum(r["stale"] for r in active))
        return active, counts
