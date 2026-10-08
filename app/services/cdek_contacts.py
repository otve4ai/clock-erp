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
    "error": ("Ошибка отправки", "red", "×"),
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
            result[channel].update(url=item.get("url", ""), text=item.get("text", ""))
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
                    table = "sms_messages" if channel == "sms" else "mail_outbox"
                    columns = {r[1] for r in connection.execute("PRAGMA table_info(" + table + ")")}
                    optional = ("message_text", "sent_by_name", "created_by_name") if channel == "sms" else ("author_id", "idempotency_key")
                    prefix = "" if channel == "sms" else "o."
                    extras = "".join(", " + (prefix + name if name in columns else "NULL") + " AS " + name for name in optional)
                    seen = set()
                    ids = list(unique)
                    for offset in range(0, len(ids), 400):
                        batch = ids[offset:offset + 400]
                        placeholders = ",".join("?" for _ in batch)
                        if channel == "sms":
                            sql = "SELECT id,order_id,status,sent_at,updated_at,created_at" + extras + " FROM sms_messages WHERE order_id IN (" + placeholders + ") AND repair_id IS NULL"
                        else:
                            sql = "SELECT o.id,o.thread_id,l.entity_id AS order_id,o.state AS status,o.sent_at,o.updated_at,o.created_at" + extras + " FROM mail_outbox o JOIN mail_links l ON l.thread_id=o.thread_id WHERE l.entity_type='order' AND l.entity_id IN (" + placeholders + ")"
                        for row in connection.execute(sql, batch):
                            key = unique[row["order_id"]]
                            identity = (key, row["id"])
                            if identity in seen:
                                continue
                            seen.add(identity)
                            raw = row["status"]
                            status = "queued_sms" if channel == "sms" and raw == "queued" else raw
                            accepted = status in {"sent", "accepted", "queued_sms", "smsc_submit", "delivered"}
                            at = row["sent_at"] if accepted and row["sent_at"] else row["updated_at"]
                            if channel == "sms":
                                author = row["sent_by_name"] or row["created_by_name"] or "Автор неизвестен"
                                url, body = "", row["message_text"] or ""
                            else:
                                automatic = str(row["idempotency_key"] or "").startswith("cdek-pvz-v1:")
                                author = "Автоматически" if automatic else "Сотрудник №{}".format(row["author_id"]) if row["author_id"] else "Автор неизвестен"
                                url, body = "/app/mail?thread={}".format(int(row["thread_id"])), ""
                            item = dict(rank=(timestamp(row["created_at"]), row["id"]), status=status,
                                        at=at, url=url, text=body)
                            previous = result[key].get(channel)
                            if not previous or previous["rank"] < item["rank"]:
                                result[key][channel] = item
                            if accepted:
                                result[key].setdefault("events", []).append(dict(channel=channel,
                                    at=timestamp(at), actor=author, title="Email отправлен" if channel == "email" else "SMS отправлено",
                                    text=body, url=url))
                finally:
                    connection.close()
            except (sqlite3.Error, OSError, ValueError):
                logging.getLogger(__name__).warning("CDEK contact journal unavailable: %s", channel)
                for item in result.values():
                    item.pop(channel, None)
                    item["events"] = [e for e in item.get("events", []) if e["channel"] != channel]
        return result


def interaction_history(review, records, work_labels, outcomes):
    """Join one saved action with its call; preserve legacy notes and timestamps."""
    result = []
    calls = review.get("calls") or []
    used = set()
    history = review.get("history") or ([review] if review.get("version") else [])
    previous = {}
    for entry in history:
        at = entry.get("updated_at", 0)
        actor = entry.get("actor") or "Автор неизвестен"
        linked = []
        for i, call in enumerate(calls):
            if i in used:
                continue
            if (call.get("review_version") == entry.get("version") and call.get("review_version") is not None) or (call.get("review_version") is None and call.get("recorded_at") == at and call.get("actor") == actor):
                linked.append(call)
                used.add(i)
        parts = []
        for call in linked:
            label = "Звонок: " + STATES[call["result"]][0].lower()
            if abs(call["at"] - at) > 60:
                label += " · " + display_time(call["at"]) + " МСК"
            parts.append(label)
        changed_work = entry.get("work") != previous.get("work") or entry.get("outcome") != previous.get("outcome")
        if changed_work:
            label = work_labels.get(entry.get("work"), "Обработка")
            if entry.get("work") == "closed":
                label += ": " + outcomes.get(entry.get("outcome"), "Результат не указан").lower()
            parts.append(label)
        comments = []
        note = entry.get("comment", "") if entry.get("unified") else entry.get("note", "")
        for value in [note] + [c.get("note", "") for c in linked] + ([entry.get("outcome_note", "")] if entry.get("work") == "closed" else []):
            if value and value not in comments:
                comments.append(value)
        if (entry.get("next_action"), entry.get("followup")) != (previous.get("next_action"), previous.get("followup")) and (entry.get("next_action") or entry.get("followup")):
            parts.append("Далее: " + " · ".join(filter(None, (entry.get("next_action"), entry.get("followup")))))
        result.append(dict(at=at, actor=actor, title=" · ".join(parts) or "Комментарий", text="\n".join(comments), url=""))
        previous = entry
    for i, call in enumerate(calls):
        if i not in used:
            result.append(dict(at=call["at"], actor=call["actor"], title="Звонок: " + STATES[call["result"]][0].lower(), text=call["note"], url=""))
    result.extend(records.get("events", []))
    return [item for _, item in sorted(enumerate(result), key=lambda pair: (pair[1].get("at", 0), pair[0]), reverse=True)]
