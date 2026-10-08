"""Shipment read model and manager workflow, independent of sales/stock writes."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta

from app.clients.cdek import CdekError
from app.services.cdek_delivery import MOSCOW, display_time
from app.services.cdek_contacts import contact_states, interaction_history
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
WORK = {"new": "Нужна реакция", "working": "В работе", "closed": "Обработан"}
OUTCOMES = {"collected": "Заказ получен", "refused": "Клиент отказался",
            "unreachable": "Не удалось связаться", "return_checked": "Возврат проверен", "other": "Другое"}
LEGACY_WORK = {"today": "new", "tomorrow": "new"}
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
            "orders": [], "order_ids": [], "sale_ids": [], "items": [], "order_date": ""})
        parsed = parse_erp_datetime(sale.get("created_at"))
        if parsed:
            created = parsed[0]
            if created.tzinfo is not None:
                created = created.astimezone(MOSCOW)
            date = created.strftime("%Y-%m-%d")
            if not group["order_date"] or date < group["order_date"]:
                group["order_date"] = date
        for field, value in (("orders", str(sale.get("order_number") or "")),
                             ("order_ids", str(sale.get("external_order_id") or sale.get("order_id") or "")),
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


def situation(data):
    """Stable business milestone, unaffected by polling, wording or location."""
    code = data.get("status_code", "")
    if data.get("is_return") and code in DELIVERED:
        return "return_received"
    if data.get("is_return") or code == "POSTOMAT_SEIZED":
        return "return_started"
    if code == "NOT_DELIVERED":
        return "refused"
    if code in PICKUP:
        return "pickup"
    return ""


class CdekSales:
    def __init__(self, delivery, clock=None, contacts=None):
        self.delivery = delivery
        self.clock = clock or delivery.clock
        self.review_path = delivery.path / "reviews"
        self.contacts = contacts
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
        if not isinstance(data, dict) or data.get("work") not in set(WORK) | set(LEGACY_WORK) or not isinstance(data.get("version"), int):
            raise CdekError("CDEK_REVIEW", "Повреждены отметки менеджера.")
        for field in ("history", "calls"):
            if field in data and (not isinstance(data[field], list) or any(not isinstance(v, dict) for v in data[field])):
                raise CdekError("CDEK_REVIEW", "Повреждена история менеджера.")
        for call in data.get("calls", []):
            if call.get("result") not in {"no_answer", "connected"} or not isinstance(call.get("at"), (int, float)) or not all(isinstance(call.get(k), str) for k in ("actor", "note")):
                raise CdekError("CDEK_REVIEW", "Повреждена история звонков.")
        for field in ("storage_until", "followup", "expected_delivery"):
            if data.get(field):
                try:
                    if datetime.strptime(data[field], "%Y-%m-%d").strftime("%Y-%m-%d") != data[field]:
                        raise ValueError("Noncanonical date")
                except (TypeError, ValueError):
                    raise CdekError("CDEK_REVIEW", "Повреждены даты в отметках менеджера.") from None
        if data["work"] in LEGACY_WORK:
            data["legacy_followup"] = data.get("followup", "")
            data["followup"] = ""
        data["work"] = LEGACY_WORK.get(data["work"], data["work"])
        if data["work"] == "closed" and not data.get("outcome"):
            data["outcome"] = "return_checked"
            data["closed_situation"] = "return_received"
        return data

    def save_review(self, shipment, payload, actor):
        key = shipment["id"]
        unified = payload.get("form_mode") == "unified"
        request_hash = hashlib.sha256(json.dumps({"actor": str(actor), "payload": dict(payload)}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        def invalid(field, message):
            error = CdekError("CDEK_FORM", message)
            error.field = field
            raise error
        work = str(payload.get("work") or "new")
        note = str(payload.get("note") or "").strip()
        if work not in WORK or len(note) > 2000:
            invalid("note" if len(note) > 2000 else "review_work", "Проверьте статус и длину комментария (до 2000 символов).")
        followup = str(payload.get("followup") or "").strip()
        next_action = str(payload.get("next_action") or "").strip()
        outcome = str(payload.get("outcome") or "").strip()
        outcome_note = note if unified else str(payload.get("outcome_note") or "").strip()
        if len(next_action) > 500 or len(outcome_note) > 2000:
            invalid("next_action" if len(next_action) > 500 else "note", "Слишком длинный комментарий или следующее действие.")
        for field in ("storage_until", "expected_delivery", "followup"):
            value = payload.get(field)
            if not value:
                continue
            try:
                if datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d") != value:
                    raise ValueError("Noncanonical date")
            except (TypeError, ValueError):
                invalid(field, "Укажите корректную дату.")
        try:
            version = int(payload.get("version", -1))
        except (ValueError, TypeError):
            raise CdekError("CDEK_FORM", "Обновите страницу перед сохранением.") from None
        with self.delivery.lock():
            previous = self.review(key)
            if any(entry.get("request_hash") == request_hash for entry in previous.get("history", [])):
                return previous
            if previous["version"] != version:
                raise CdekError("CDEK_CONFLICT", "Другой сотрудник изменил отметку. Обновите страницу.")
            snapshot = self.delivery.view(shipment)
            if work == "closed" and outcome not in OUTCOMES:
                invalid("outcome", "Выберите результат обработки.")
            if work == "closed" and outcome in {"other", "unreachable"} and not outcome_note:
                invalid("note" if unified else "outcome_note", "Добавьте комментарий к результату обработки.")
            if work == "closed" and outcome == "return_checked" and not (snapshot.get("is_return") and snapshot.get("status_code") in DELIVERED):
                invalid("outcome", "Подтвердить возврат можно после его получения по данным СДЭК.")
            history = list(previous.get("history") or [])
            if previous["version"] and not history:
                history.append(dict(previous, action="legacy"))
            calls = list(previous.get("calls") or [])
            call_result = str(payload.get("call_result") or "")
            if call_result:
                if call_result not in {"no_answer", "connected"}:
                    invalid("call_result", "Выберите результат звонка.")
                try:
                    instant = datetime.fromtimestamp(self.clock(), MOSCOW) if unified else datetime.strptime(str(payload.get("call_at") or ""), "%Y-%m-%dT%H:%M").replace(tzinfo=MOSCOW)
                except ValueError:
                    raise CdekError("CDEK_FORM", "Укажите дату и время звонка (МСК).") from None
                if instant.timestamp() > self.clock() + 60:
                    raise CdekError("CDEK_FORM", "Дата звонка не может быть в будущем.")
                comment = note if unified else str(payload.get("call_note") or "").strip()
                if len(comment) > 2000:
                    raise CdekError("CDEK_FORM", "Комментарий звонка: до 2000 символов.")
                calls.append(dict(result=call_result, at=instant.timestamp(), note=comment,
                                  actor=str(actor)[:100], recorded_at=self.clock(), review_version=version + 1))
            data = dict(previous, version=version + 1, work=work, note=note or (previous.get("note", "") if unified else ""),
                        followup=(followup if "followup" in payload else previous.get("followup", "")) if (work == "working" if unified else work != "closed") else "",
                        next_action=(next_action if "next_action" in payload else previous.get("next_action", "")) if (work == "working" if unified else work != "closed") else "",
                        outcome=outcome if work == "closed" else previous.get("outcome", ""),
                        outcome_note=outcome_note if work == "closed" else previous.get("outcome_note", ""),
                        storage_until=payload.get("storage_until", previous.get("storage_until", "")),
                        expected_delivery=payload.get("expected_delivery", previous.get("expected_delivery", "")),
                        updated_at=self.clock(), actor=str(actor)[:100],
                        calls=calls, closed_situation=situation(snapshot) if work == "closed" else previous.get("closed_situation", ""),
                        closed_epoch=(snapshot.get("events") or [{}])[0].get("epoch", 0) if work == "closed" else previous.get("closed_epoch", 0),
                        closed_event=snapshot.get("date_display", "") if work == "closed" else "")
            entry = {k: v for k, v in data.items() if k not in {"history", "calls"}}
            entry.update(request_hash=request_hash, unified=unified, comment=note,
                         call_result=call_result, call_at=calls[-1]["at"] if call_result else 0)
            history.append(entry)
            data["history"] = history
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
        groups = group_sales(sales)
        contacts = self.contacts(groups) if self.contacts else {}
        for group in groups:
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
            milestone = situation(data)
            reopened = (review.get("work") == "closed" and bool(milestone)
                        and latest > review.get("closed_epoch", 0)
                        and milestone != review.get("closed_situation"))
            return_closed = review.get("work") == "closed" and not reopened
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
            needs_reaction = False
            wait = "—"
            if not is_delivered and not return_closed:
                if code and code not in {"REMOVED", "INVALID"} and not data.get("is_return") and data.get("delivery_kind") not in {"pvz", "courier"}:
                    needs_reaction = True
                    add("data", 1, "Проверить способ доставки", "Обновить данные СДЭК и уточнить способ доставки; автоматическое письмо заблокировано")
                if code in PICKUP and not data.get("is_return") and data.get("delivery_kind") == "pvz":
                    # Consecutive pickup events belong to one storage episode.
                    start = latest
                    for event in events:
                        if event["code"] not in PICKUP:
                            break
                        start = min(start, event["epoch"])
                    days = max(0, int((now - start) // DAY))
                    needs_reaction = needs_reaction or days >= 7
                    wait = "{} сут. в ПВЗ".format(days)
                    if days >= self.pvz_warning:
                        add("pvz", 2 if days >= self.pvz_urgent else 1,
                            "Не забирают {} сут.".format(days), "Связаться с получателем")
                    deadline = review.get("storage_until")
                    if deadline:
                        end = datetime.strptime(deadline, "%Y-%m-%d").replace(tzinfo=MOSCOW) + timedelta(days=1)
                        wait += " · до " + deadline
                        if end.timestamp() - now <= DAY:
                            needs_reaction = True
                            add("pvz", 2, "Хранение заканчивается" if end.timestamp() > now else "Срок хранения истёк", "Согласовать получение / продление")
                    else:
                        wait += " · срок уточнить"
                elif code in TRANSIT or code in {"TAKEN_BY_COURIER", "RECEIVED_AT_SHIPMENT_WAREHOUSE", "CREATED", "ACCEPTED"}:
                    wait = "{} сут. без событий".format(age)
                    if age >= self.transit_days:
                        add("delay", 1, "Нет новых событий {} сут.".format(age), "Уточнить у СДЭК")
                if label in {"Возврат", "Не вручён"}:
                    needs_reaction = True
                    add("return", 1, "Возврат получен: нужна проверка" if code in DELIVERED else data.get("status") or label,
                        "Проверить возврат" if code in DELIVERED else "Уточнить причину / получение")
                if code in {"REMOVED", "INVALID"}:
                    add("data", 1, data.get("status") or label, "Проверить накладную")
                if review.get("expected_delivery") and code not in PICKUP and label not in {"Возврат", "Не вручён", "Отменён"}:
                    today = datetime.fromtimestamp(now, MOSCOW).date().isoformat()
                    if review["expected_delivery"] < today:
                        add("delay", 2, "Просрочена подтверждённая дата доставки", "Уточнить срок у СДЭК")
            effective_work = review.get("work", "new")
            if reopened:
                effective_work = "new"
                needs_reaction = True
                if not issues:
                    add("return", 1, "Новое событие доставки", "Проверить ситуацию")
            if effective_work != "closed" and review.get("followup"):
                today = datetime.fromtimestamp(now, MOSCOW).date().isoformat()
                if review["followup"] <= today:
                    effective_work, needs_reaction = "new", True
                    add("followup", 2, "Наступил срок следующего действия", review.get("next_action") or "Проверить ситуацию")
            if return_closed:
                issues, needs_reaction = [], False
            issues.sort(key=lambda issue: -issue["priority"])
            if effective_work == "new" and not needs_reaction:
                effective_work = ""
            contact = contact_states(contacts.get(group["id"], {}), review, is_delivered or code == "REMOVED")
            result.append(dict(group, interactions=interaction_history(review, contacts.get(group["id"], {}), WORK, OUTCOMES), contacts=contact, reopened=reopened, needs_reaction=needs_reaction, delivery=data, label=label, tone=tone, review=review,
                review_error=review_error, issues=issues, priority=max([i["priority"] for i in issues] or [0]),
                wait=wait, work=effective_work, delivered=is_delivered, event_epoch=latest,
                checked_display=display_time(data.get("checked_at", 0)),
                stale=bool(data.get("stale") or data.get("error") or not data.get("checked_at"))))
        return sorted(result, key=lambda r: (-r["priority"], r["work"] == "working", r["event_epoch"], r["id"]))

    def summary(self, rows):
        active = list(rows)
        counts = {label: sum(row["label"] == label and (label != "Вручён" or row["event_epoch"] >= self.clock() - 30 * DAY) for row in active) for label in ("В пути", "В ПВЗ", "У курьера", "Вручён")}
        counts.update(total=len(active), problems=sum(bool(r["issues"]) for r in active),
                      urgent=sum(r["priority"] == 2 for r in active),
                      stale=sum(r["stale"] for r in active))
        return active, counts
