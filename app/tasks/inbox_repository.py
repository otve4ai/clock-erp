"""Recipient-owned assignment events, using only the caller's Tasks transaction."""

import json

from .domain import TaskError


class InboxQueries:
    def assignment_event(self, task, actor, timestamp, previous_assignee=None):
        if previous_assignee is not None:
            # Retire old pending assignments and unsent toasts atomically with
            # reassignment. A former assignee must not get a stale new-task toast.
            self.connection.execute(
                "UPDATE task_inbox_events SET handled_at=COALESCE(handled_at,?),"
                "notified_at=COALESCE(notified_at,?) WHERE task_id=? AND recipient_id=?",
                (timestamp, timestamp, task["id"], previous_assignee))
        if task["assigned_to"] == actor:
            return
        event = "task_assigned" if previous_assignee is None else "task_reassigned"
        key = "{}:{}:{}:{}".format(task["id"], event, task["assigned_to"], task["version"])
        if self.connection.execute("SELECT id FROM task_inbox_events WHERE dedupe_key=?", (key,)).fetchone():
            return
        self.connection.execute(
            "INSERT INTO task_inbox_events(recipient_id,task_id,actor_id,event_type,created_at,dedupe_key,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (task["assigned_to"], task["id"], actor, event, timestamp, key,
             json.dumps({"title": task["title"], "task_type": task["task_type"]}, ensure_ascii=False)))

    @staticmethod
    def _inbox_row(row):
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value.pop("dedupe_key", None)
        return value

    def inbox_badge(self, recipient):
        return self.connection.execute(
            "SELECT COUNT(*) FROM task_inbox_events WHERE recipient_id=? AND handled_at IS NULL",
            (recipient,)).fetchone()[0]

    def inbox(self, recipient, limit, offset):
        rows = self.connection.execute(
            "SELECT * FROM task_inbox_events WHERE recipient_id=? AND handled_at IS NULL "
            "ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (recipient, limit, offset)).fetchall()
        return {"items": [self._inbox_row(row) for row in rows], "total": self.inbox_badge(recipient),
                "limit": limit, "offset": offset}

    def read_inbox(self, recipient, event_id, timestamp):
        row = self.connection.execute("SELECT * FROM task_inbox_events WHERE id=? AND recipient_id=?",
                                      (event_id, recipient)).fetchone()
        if row is None:
            raise TaskError("INBOX_EVENT_NOT_FOUND", "Событие не найдено.", 404)
        self.connection.execute(
            "UPDATE task_inbox_events SET handled_at=COALESCE(handled_at,?) WHERE id=? AND recipient_id=?",
            (timestamp, event_id, recipient))
        result = self._inbox_row(row)
        result["handled_at"] = result["handled_at"] or timestamp
        return result

    def claim_notifications(self, recipient, timestamp, limit):
        # BEGIN IMMEDIATE is held by the service: concurrent claims serialize.
        rows = self.connection.execute(
            "SELECT * FROM task_inbox_events WHERE recipient_id=? AND notified_at IS NULL "
            "ORDER BY id LIMIT ?", (recipient, limit)).fetchall()
        result = []
        for row in rows:
            self.connection.execute("UPDATE task_inbox_events SET notified_at=? WHERE id=? AND recipient_id=?",
                                    (timestamp, row["id"], recipient))
            item = self._inbox_row(row)
            result.append({"id": item["id"], "task_id": item["task_id"], "actor_id": item["actor_id"],
                           "title": item["payload"]["title"], "task_type": item["payload"]["task_type"]})
        return {"items": result}
