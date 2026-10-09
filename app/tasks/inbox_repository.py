"""Recipient-owned assignment events, using only the caller's Tasks transaction."""

import json

from .domain import TaskError


class InboxQueries:
    # Use the current task, never a stale event snapshot, for lifecycle/recipient
    # checks. Reading a page cannot acknowledge a task or repair data.
    PENDING = ("e.recipient_id=? AND e.handled_at IS NULL AND "
               "t.assigned_to=e.recipient_id AND t.deleted_at IS NULL AND t.status!='done'")

    def cdek_assignment_event(self, task, timestamp):
        if (task['related_entity_type'] != 'cdek_call' or task['task_type'] != 'micro'
                or task['deleted_at'] is not None or task['status'] == 'done'):
            return False
        # Never resurrect a handled event or duplicate a previous assignment.
        if self.connection.execute(
                'SELECT id FROM task_inbox_events WHERE task_id=? AND recipient_id=? LIMIT 1',
                (task['id'], task['assigned_to'])).fetchone():
            return False
        self.assignment_event(task, task['assigned_to'], timestamp, automated=True)
        return True

    def assignment_event(self, task, actor, timestamp, previous_assignee=None, automated=False):
        if previous_assignee is not None:
            # Retire old pending assignments and unsent toasts atomically with
            # reassignment. A former assignee must not get a stale new-task toast.
            self.connection.execute(
                "UPDATE task_inbox_events SET handled_at=COALESCE(handled_at,?),"
                "notified_at=COALESCE(notified_at,?) WHERE task_id=? AND recipient_id=?",
                (timestamp, timestamp, task["id"], previous_assignee))
        if task["assigned_to"] == actor and not automated:
            return
        event = "task_assigned" if previous_assignee is None else "task_reassigned"
        key = "{}:{}:{}:{}".format(task["id"], event, task["assigned_to"], task["version"])
        if self.connection.execute("SELECT id FROM task_inbox_events WHERE dedupe_key=?", (key,)).fetchone():
            return
        self.connection.execute(
            "INSERT INTO task_inbox_events(recipient_id,task_id,actor_id,event_type,created_at,dedupe_key,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (task["assigned_to"], task["id"], actor, event, timestamp, key,
             json.dumps({"title": task["title"], "task_type": task["task_type"],
                         "source": "cdek" if automated else None}, ensure_ascii=False)))

    @staticmethod
    def _inbox_row(row):
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        value.pop("dedupe_key", None)
        return value

    def inbox_badge(self, recipient):
        return self.inbox_counts(recipient)["count"]

    def inbox_counts(self, recipient):
        row = self.connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(t.task_type='normal'),0),COALESCE(SUM(t.task_type='micro'),0) "
            "FROM task_inbox_events e JOIN tasks t ON t.id=e.task_id WHERE " + self.PENDING,
            (recipient,)).fetchone()
        return dict(zip(("count", "normal", "micro"), row))

    def inbox(self, recipient, limit, offset):
        rows = self.connection.execute(
            "SELECT e.*,t.title,t.task_type,t.status,t.version,t.micro_deadline_at "
            "FROM task_inbox_events e JOIN tasks t ON t.id=e.task_id WHERE " + self.PENDING +
            " ORDER BY (t.task_type='micro') DESC,e.created_at DESC,e.id DESC LIMIT ? OFFSET ?", (recipient, limit, offset)).fetchall()
        counts = self.inbox_counts(recipient)
        return {"items": [self._inbox_row(row) for row in rows], "total": counts["count"],
                "counts": counts, "limit": limit, "offset": offset}

    def pending_assignment(self, task_id, recipient):
        return self.connection.execute(
            "SELECT e.id FROM task_inbox_events e JOIN tasks t ON t.id=e.task_id WHERE " +
            self.PENDING + " AND t.id=? ORDER BY e.id DESC LIMIT 1", (recipient, task_id)).fetchone()

    def finish_inbox(self, task_id, timestamp):
        self.connection.execute(
            "UPDATE task_inbox_events SET handled_at=COALESCE(handled_at,?),"
            "notified_at=COALESCE(notified_at,?) WHERE task_id=?", (timestamp, timestamp, task_id))

    def read_inbox(self, recipient, event_id, timestamp):
        row = self.connection.execute("SELECT * FROM task_inbox_events WHERE id=? AND recipient_id=?",
                                      (event_id, recipient)).fetchone()
        if row is None:
            raise TaskError("INBOX_EVENT_NOT_FOUND", "Событие не найдено.", 404)
        # Old clients must not dismiss a live assignment using the old read API.
        task = self.connection.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
        if (row["handled_at"] is None and task is not None and task["assigned_to"] == recipient
                and task["deleted_at"] is None and task["status"] != "done"):
            raise TaskError("INBOX_ACTION_REQUIRED", "Микрозадачу нужно выполнить, обычную задачу — взять в работу.", 409)
        if task is not None and task["task_type"] == "micro":
            # Completion/deletion hides the event by current task state. A stale
            # read must not prevent it returning after reopen/restore.
            return self._inbox_row(row)
        self.connection.execute(
            "UPDATE task_inbox_events SET handled_at=COALESCE(handled_at,?) WHERE id=? AND recipient_id=?",
            (timestamp, event_id, recipient))
        result = self._inbox_row(row)
        result["handled_at"] = result["handled_at"] or timestamp
        return result

    def claim_notifications(self, recipient, timestamp, limit):
        # BEGIN IMMEDIATE is held by the service: concurrent claims serialize.
        rows = self.connection.execute(
            "SELECT e.* FROM task_inbox_events e JOIN tasks t ON t.id=e.task_id WHERE " + self.PENDING +
            " AND e.notified_at IS NULL ORDER BY e.id LIMIT ?", (recipient, limit)).fetchall()
        result = []
        for row in rows:
            self.connection.execute("UPDATE task_inbox_events SET notified_at=? WHERE id=? AND recipient_id=?",
                                    (timestamp, row["id"], recipient))
            item = self._inbox_row(row)
            result.append({"id": item["id"], "task_id": item["task_id"], "actor_id": item["actor_id"],
                           "title": item["payload"]["title"], "task_type": item["payload"]["task_type"],
                           "source": item["payload"].get("source")})
        return {"items": result}
