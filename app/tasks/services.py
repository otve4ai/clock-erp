"""Application operations: one policy layer, one local transaction per mutation."""

from . import permissions
from .domain import (TaskError, business_today, conflict, invalid, list_options,
                     positive_integer, task_values, utc_now)


class TasksService:
    def __init__(self, repository, user_lookup=None, now=utc_now, today=business_today):
        self.repository = repository
        self.user_lookup, self.now, self.today = user_lookup, now, today

    def status(self):
        return self.repository.status()

    def _assignee(self, user_id):
        # This external read is deliberately completed BEFORE a local write
        # transaction. Failure must never leave a task or its history half saved.
        try:
            user = self.user_lookup(user_id) if self.user_lookup else None
        except Exception as error:
            raise TaskError("USER_LOOKUP_UNAVAILABLE", "Проверка исполнителя недоступна.", 503) from error
        if not user or user.get("id") != user_id or user.get("active") != 1:
            raise invalid("assigned_to", "Нужен существующий активный исполнитель.")

    def create(self, user, payload):
        permissions.require_actor(user)
        values = task_values(payload, creating=True)
        assigned_to = values.get("assigned_to", user["id"])
        self._assignee(assigned_to)
        now = self.now()
        task = {"task_type": "normal", "description": "", "status": "new", "priority": "normal",
                "created_by": user["id"], "assigned_to": assigned_to, "deadline_date": None,
                "created_at": now, "updated_at": now, "completed_at": None, "version": 1,
                "deleted_at": None, "related_entity_type": None, "related_entity_id": None,
                "related_entity_label": None}
        task.update(values)
        if task["status"] == "done":
            task["completed_at"] = now
        with self.repository.transaction(write=True) as session:
            task = session.create(task)
            session.add_activity(task, user["id"], "created", now, {"task": task})
            if task["status"] == "done":
                session.add_activity(task, user["id"], "completed", now, {"completed_at": now})
            return task

    def get(self, user, task_id):
        permissions.require_actor(user)
        positive_integer(task_id, "id")
        with self.repository.transaction() as session:
            task = session.get(task_id)
            permissions.require_view(user, task)
            return task

    def activity(self, user, task_id, options=None):
        permissions.require_actor(user)
        positive_integer(task_id, "id")
        options = options or {}
        if set(options) - {"limit", "offset"}:
            raise invalid("query")
        paging = list_options(options)
        with self.repository.transaction() as session:
            permissions.require_view(user, session.get(task_id))
            return {"items": session.activity(task_id, paging["limit"], paging["offset"]),
                    "limit": paging["limit"], "offset": paging["offset"]}

    def list(self, user, options=None, summary=False):
        permissions.require_actor(user)
        options = list_options(options or {})
        scope = permissions.list_scope(user, options.get("scope", "my"))
        today = self.today()
        with self.repository.transaction() as session:
            return session.summary(scope, options, today) if summary else session.list(scope, options, today)

    @staticmethod
    def _authorize_mutation(user, task, version, operation, values):
        permissions.require_view(user, task)
        if task["version"] != version:
            raise conflict()
        if operation == "patch":
            permissions.require_action(user, task, "edit")
            if "status" in values:
                permissions.require_action(user, task, "change_status")
            if "assigned_to" in values:
                permissions.require_action(user, task, "reassign")
            # Future ERP reference metadata is managed by the creator/admin;
            # it is never followed or resolved by this module.
            if any(field.startswith("related_") for field in values):
                permissions.require_action(user, task, "reassign")
        else:
            permissions.require_action(user, task, {"status": "change_status",
                                                   "delete": "delete", "restore": "restore"}[operation])

    def mutate(self, user, task_id, payload, operation="patch"):
        permissions.require_actor(user)
        positive_integer(task_id, "id")
        if operation not in ("patch", "status", "delete", "restore"):
            raise invalid("operation")
        if not isinstance(payload, dict):
            raise invalid("body")
        version = positive_integer(payload.get("version"), "version")
        if operation == "patch":
            values = task_values(payload)
        elif operation == "status":
            if set(payload) != {"version", "status"}:
                raise invalid("body", "Нужны только status и version.")
            values = task_values(payload)
        else:
            if set(payload) != {"version"}:
                raise invalid("body", "Нужна только version.")
            values = {}
        if "assigned_to" in values:
            # First authorize, then consult auth outside any Tasks transaction.
            # Recheck rights/version on the fresh snapshot under the write lock.
            task = self.get(user, task_id)
            self._authorize_mutation(user, task, version, operation, values)
            self._assignee(values["assigned_to"])
        with self.repository.transaction(write=True) as session:
            before = session.get(task_id)
            self._authorize_mutation(user, before, version, operation, values)
            now = self.now()
            if operation in ("delete", "restore"):
                values["deleted_at"] = now if operation == "delete" else None
            changes = {field: value for field, value in values.items() if before[field] != value}
            if not changes:
                raise invalid("body", "Задача уже содержит эти значения.")
            if "status" in changes:
                changes["completed_at"] = now if changes["status"] == "done" else None
            changes["updated_at"] = now
            task = session.update(task_id, version, changes)
            groups = (("content_changed", ("title", "description")),
                      ("reassigned", ("assigned_to",)), ("deadline_changed", ("deadline_date",)),
                      ("priority_changed", ("priority",)), ("status_changed", ("status",)),
                      ("related_reference_changed", ("related_entity_type", "related_entity_id", "related_entity_label")))
            for event_type, fields in groups:
                diff = {field: {"before": before[field], "after": task[field]} for field in fields if field in changes}
                if diff:
                    session.add_activity(task, user["id"], event_type, now, diff)
            if "status" in changes and (before["status"] == "done" or task["status"] == "done"):
                event = "completed" if task["status"] == "done" else "reopened"
                session.add_activity(task, user["id"], event, now,
                                     {"completed_at": {"before": before["completed_at"], "after": task["completed_at"]}})
            if operation in ("delete", "restore"):
                session.add_activity(task, user["id"], "deleted" if operation == "delete" else "restored", now,
                                     {"deleted_at": {"before": before["deleted_at"], "after": task["deleted_at"]}})
            return task
