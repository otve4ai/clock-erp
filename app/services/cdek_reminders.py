"""Automatic pickup email only; carrier data is read afresh, SMS is never sent."""
import json
import re
import time
from datetime import datetime

from app.clients.cdek import CdekError
from app.services.cdek_delivery import MOSCOW, normalize_delivery
from app.services.mail import MailValidationError, parse_addresses

PREFIX = "cdek-pvz-v1:"
DELAY = 3 * 86400
# Parcel lockers have a different collection workflow; this email is for a PVZ.
PICKUP = {"ACCEPTED_AT_PICK_UP_POINT"}


def arrival(data):
    if data.get("is_return") or data.get("status_code") not in PICKUP:
        return None
    start = None
    for event in data.get("events", []):
        if event["code"] not in PICKUP:
            break
        start = event["epoch"]
    return start


def reminder_key(number, start):
    return "{}{}:{}".format(PREFIX, number, int(start))


class CdekReminders:
    def __init__(self, delivery, mail_store, clock=None):
        self.delivery, self.store = delivery, mail_store
        self.client = delivery.client
        self.clock = clock or time.time

    def build(self, number, expected_key=None):
        if not re.fullmatch(r"[0-9]{6,20}", str(number)):
            raise CdekError("CDEK_REFERENCE", "Некорректная накладная напоминания.")
        entity = self.client.get_order(cdek_number=number)
        data = normalize_delivery(entity)
        if data["cdek_number"] != number:
            raise CdekError("CDEK_MISMATCH", "СДЭК вернул другую накладную.")
        if data["delivery_kind"] != "pvz":
            return None
        start = arrival(data)
        if start is None or self.clock() - start < DELAY:
            return None
        key = reminder_key(number, start)
        if expected_key and expected_key != key:
            return None
        recipient = entity.get("recipient") or {}
        if not isinstance(recipient, dict):
            raise CdekError("CDEK_RECIPIENT", "Нет получателя СДЭК.")
        email = str(recipient.get("email") or "").strip()
        if any(ord(char) < 32 for char in email):
            raise MailValidationError("Некорректный email получателя СДЭК.")
        addresses = parse_addresses(email)
        if len(addresses) != 1:
            raise MailValidationError("Нет однозначного email получателя СДЭК.")
        point = self.client.get_delivery_point(entity.get("delivery_point"))
        # A pickup status or office code alone does not prove PVZ delivery.
        if point.get("type") != "PVZ":
            raise CdekError("CDEK_POINT", "СДЭК не подтвердил тип пункта выдачи.")
        location = point.get("location") or {}
        if not isinstance(location, dict):
            raise CdekError("CDEK_POINT", "Некорректный адрес пункта выдачи.")
        address = str(location.get("address_full") or "").strip()
        if not address:
            address = ", ".join(str(location.get(key) or "").strip() for key in ("city", "address") if location.get(key))
        if not address:
            raise CdekError("CDEK_POINT", "СДЭК не вернул адрес пункта выдачи.")
        name = " ".join(str(recipient.get("name") or "").split())
        order = " ".join(str(entity.get("number") or "").split())
        greeting = "Здравствуйте, {}!".format(name) if name else "Здравствуйте!"
        lines = [greeting, "", "Напоминаем, что ваш заказ{} уже прибыл в пункт выдачи СДЭК и ожидает получения.".format(" №" + order if order else ""),
                 "", "Где забрать заказ:", address]
        hours = str(point.get("work_time") or "").strip()
        if hours:
            lines += ["", "Режим работы:", hours]
        lines += ["", "Номер отправления: " + number,
                  "Ожидает в пункте выдачи с: " + datetime.fromtimestamp(start, MOSCOW).strftime("%d.%m.%Y"),
                  "", "Пожалуйста, заберите заказ в удобное для вас время. Посмотреть актуальный статус можно на сайте СДЭК по номеру отправления: https://www.cdek.ru/ru/", "",
                  "Спасибо, что выбрали нас ❤", "Команда TicTacToy.ru"]
        # No documented, verified storage deadline is available in this response.
        # Never derive it from a default number of days or the ERP manager notes.
        return {"key": key, "to": email, "subject": "Ваш заказ TicTacToy.ru ждёт вас в СДЭК ❤", "text_body": "\n".join(lines)}

    def preflight(self, row):
        parts = row["idempotency_key"].split(":")
        if len(parts) != 3:
            raise CdekError("CDEK_REFERENCE", "Некорректное напоминание.")
        return self.build(parts[1], expected_key=row["idempotency_key"])

    def prepare(self, shipments, limit=20):
        """Use cache only to shortlist; every queued email uses a fresh API read.

        Cursor contains shipment hashes only and avoids starvation on missing emails.
        The mail worker's existing process lock serializes this bounded pass.
        """
        candidates = []
        for shipment in shipments:
            data = self.delivery.view(shipment)
            start = arrival(data)
            if start is not None and self.clock() - start >= DELAY:
                number = data.get("cdek_number")
                if number:
                    candidates.append((shipment, number, start))
        candidates.sort(key=lambda entry: entry[0]["id"])
        cursor_path = self.delivery.path / "email-cursor.json"
        try:
            cursor = json.loads(cursor_path.read_text(encoding="utf-8"))["after"]
        except FileNotFoundError:
            cursor = ""
        except (ValueError, KeyError, TypeError):
            raise CdekError("CDEK_CACHE", "Повреждён курсор напоминаний.") from None
        candidates = [x for x in candidates if x[0]["id"] > cursor] + [x for x in candidates if x[0]["id"] <= cursor]
        result = {"queued": 0, "skipped": 0, "errors": 0}
        deadline = time.monotonic() + 60
        for shipment, number, start in candidates[:limit]:
            if time.monotonic() >= deadline:
                break
            try:
                with self.store.connect() as db:
                    existing = db.execute("SELECT id FROM mail_outbox WHERE idempotency_key=?", (reminder_key(number, start),)).fetchone()
                if existing:
                    result["skipped"] += 1
                    continue
                payload = self.build(number)
                if payload is None:
                    result["skipped"] += 1
                    continue
                _, created = self.store.queue_outbox(payload, 0, payload["key"], order_ids=shipment.get("order_ids", ()))
                result["queued" if created else "skipped"] += 1
            except (CdekError, MailValidationError):
                result["errors"] += 1
            finally:
                self.delivery.path.mkdir(parents=True, exist_ok=True)
                temporary = cursor_path.with_suffix(".tmp")
                temporary.write_text(json.dumps({"after": shipment["id"]}), encoding="utf-8")
                temporary.replace(cursor_path)
        return result
