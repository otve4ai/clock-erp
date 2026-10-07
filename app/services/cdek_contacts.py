"""Read-only projection of existing mail/SMS journals; never sends messages."""
import logging
import sqlite3
from pathlib import Path

from app.services.cdek_delivery import MOSCOW, display_time
from app.time_ranking import parse_erp_datetime

STATES = {
    "unknown": ("Нет данных", "gray", "?"),
    "pending": ("Не отправлено", "gray", "−"),
    "sent": ("Отправлено", "green", "✓"),
    "error": ("Ошибка", "red", "!"),
    "na": ("Неприменимо", "gray", "∅"),
    "not_called": ("Не звонили", "gray", "−"),
    "no_answer": ("Не ответил", "yellow", "!"),
    "connected": ("Связались", "green", "✓"),
}


def timestamp(value):
    if isinstance(value, (float, int)):
        return value
    parsed = parse_erp_datetime(value) if value else None
    if not parsed:
        return 0
    dt = parsed[0]
    return (dt if dt.tzinfo else dt.replace(tzinfo=MOSCOW)).timestamp()


def state(key, at=0, detail=""):
    label, tone, mark = STATES[key]
    date = display_time(timestamp(at))
    return dict(state=key, label=label, tone=tone, mark=mark, date=date,
                detail=detail, tooltip=" · ".join(filter(None, (label, date + " МСК" if date else "Дата неизвестна", detail))))


def contact_states(records, review, not_applicable=False):
    result = {}
    for channel in ("email", "sms"):
        item = records.get(channel)
        if item:
            raw = item["status"]
            key = ("error" if raw == "failed" else "sent" if raw in
                   {"sent", "accepted", "queued_sms", "smsc_submit", "delivered"}
                   else "pending" if raw in {"draft", "created", "queued", "sending", "cancelled"}
                   else "unknown")
            result[channel] = state(key, item.get("at"), "Отправлено не означает доставлено или прочитано" if key == "sent" else "")
        else:
            result[channel] = state("na" if not_applicable else "unknown", detail=(
                "Контакт по текущей ситуации не требуется" if not_applicable else "Нет однозначно связанной записи в журнале отправки"))
    calls = review.get("calls") or []
    last = max(calls, key=lambda c: (c["at"], c.get("recorded_at", 0))) if calls else None
    result["call"] = state(last["result"], last["at"], " · ".join(filter(None, (last["actor"], last["note"])))) if last else state("na" if not_applicable else "not_called")
    return result


class ContactJournals:
    def __init__(self, sms_path, mail_path):
        self.sms_path, self.mail_path = sms_path, mail_path

    def __call__(self, groups):
        result = {g["id"]: {} for g in groups}
        # A journal linked only to an order cannot identify one of several waybills.
        owners = {}
        for group in groups:
            for order_id in group["order_ids"]:
                owners.setdefault(order_id, set()).add(group["id"])
        unique = {order_id: next(iter(keys)) for order_id, keys in owners.items() if len(keys) == 1}
        if not unique:
            return result
        for channel, path in (("sms", self.sms_path), ("email", self.mail_path)):
            if not path or not Path(path).is_file():
                continue
            try:
                connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
                try:
                    connection.row_factory = sqlite3.Row
                    ids = list(unique)
                    for offset in range(0, len(ids), 400):
                        batch = ids[offset:offset + 400]
                        placeholders = ",".join("?" for _ in batch)
                        if channel == "sms":
                            sql = "SELECT id,order_id,status,sent_at,updated_at,created_at FROM sms_messages WHERE order_id IN (" + placeholders + ") AND repair_id IS NULL"
                        else:
                            sql = "SELECT o.id,l.entity_id AS order_id,o.state AS status,o.sent_at,o.updated_at,o.created_at FROM mail_outbox o JOIN mail_links l ON l.thread_id=o.thread_id WHERE l.entity_type='order' AND l.entity_id IN (" + placeholders + ")"
                        for row in connection.execute(sql, batch):
                            key = unique[row["order_id"]]
                            rank = (timestamp(row["created_at"]), row["id"])
                            previous = result[key].get(channel)
                            if previous and previous["rank"] >= rank:
                                continue
                            raw = row["status"]
                            result[key][channel] = dict(rank=rank, status="queued_sms" if channel == "sms" and raw == "queued" else raw,
                                at=row["sent_at"] if raw in {"sent", "accepted", "queued", "smsc_submit", "delivered"} and row["sent_at"] else row["updated_at"])
                finally:
                    connection.close()
            except (sqlite3.Error, OSError, ValueError):
                logging.getLogger(__name__).warning("CDEK contact journal unavailable: %s", channel)
                for item in result.values():
                    item.pop(channel, None)
        return result
