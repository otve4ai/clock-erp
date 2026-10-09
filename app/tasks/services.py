"""Application operations: one policy layer, one local transaction per mutation."""

from datetime import timedelta

from . import permissions
from .domain import (TaskError, business_today, conflict, invalid, list_options,
                     positive_integer, task_values, utc_now, utc_instant,
                     micro_values, micro_options, not_found)


def require_active_user(lookup, user_id, field):
    try:
        user = lookup(user_id) if lookup else None
    except Exception as error:
        raise TaskError("USER_LOOKUP_UNAVAILABLE", "Проверка пользователя недоступна.", 503) from error
    if not user or user.get("id") != user_id or user.get("active") != 1:
        raise invalid(field, "Нужен существующий активный пользователь.")


class TasksService:
    def __init__(self, repository, user_lookup=None, now=utc_now, today=business_today):
        self.repository = repository
        self.user_lookup, self.now, self.today = user_lookup, now, today

    def status(self):
        return self.repository.status()

    def _assignee(self, user_id):
        # This external read is deliberately completed BEFORE a local write
        # transaction. Failure must never leave a task or its history half saved.
        require_active_user(self.user_lookup, user_id, "assigned_to")

    @staticmethod
    def _task(session, user, task_id):
        task = session.get_visible(task_id, permissions.task_visibility(user))
        # The same centralized predicate is used by GET, list, search and count.
        permissions.require_view(user, task, project_access=True)
        return task

    @staticmethod
    def _project(session, user, project_id):
        if project_id is not None:
            project = session.get_project(project_id, permissions.project_visibility(user))
            permissions.require_project(user, project)
            if project["archived_at"] is not None:
                raise invalid("project_id", "Восстановите проект перед добавлением задач.")

    def create(self, user, payload):
        permissions.require_actor(user)
        values = task_values(payload, creating=True)
        return self._create(user, values, "normal")

    def create_micro(self, user, payload):
        permissions.require_actor(user)
        return self._create(user, micro_values(payload, creating=True), "micro")

    def create_automated_micro(self, user, title, source_key, source_label):
        """Trusted worker entry point, never exposed through the generic API."""
        permissions.require_actor(user)
        values = micro_values({"title": title}, creating=True)
        values.update(related_entity_type="cdek_call", related_entity_id=source_key,
                      related_entity_label=source_label)
        return self._create(user, values, "micro", source_key=source_key)

    def _create(self, user, values, task_type, source_key=None):
        assigned_to = values.get("assigned_to", user["id"])
        self._assignee(assigned_to)
        now = self.now()
        micro_deadline = None
        if task_type == "micro":
            instant = utc_instant(now)
            now = instant.isoformat(timespec="microseconds")
            micro_deadline = (instant + timedelta(hours=24)).isoformat(timespec="microseconds")
        task = {"task_type": task_type, "description": "", "status": "new", "priority": "normal",
                "created_by": user["id"], "assigned_to": assigned_to, "deadline_date": None,
                "created_at": now, "updated_at": now, "completed_at": None, "version": 1,
                "deleted_at": None, "related_entity_type": None, "related_entity_id": None,
                "related_entity_label": None, "project_id": None, "micro_deadline_at": micro_deadline}
        task.update(values)
        if task_type == "normal" and assigned_to != user["id"] and task["status"] == "new":
            task["status"] = "waiting"
        if task["status"] == "done":
            task["completed_at"] = now
        with self.repository.transaction(write=True) as session:
            if source_key:
                # Include completed, deleted and converted tasks: never recreate.
                existing = session.connection.execute(
                    "SELECT * FROM tasks WHERE related_entity_type='cdek_call' AND related_entity_id=? LIMIT 1",
                    (source_key,)).fetchone()
                if existing:
                    session.cdek_assignment_event(dict(existing), now)
                    return dict(existing)
            self._project(session, user, task["project_id"])
            task = session.create(task)
            session.add_activity(task, user["id"], "created", now, {"task": task})
            if task["status"] == "done":
                session.add_activity(task, user["id"], "completed", now, {"completed_at": now})
            if source_key:
                session.cdek_assignment_event(task, now)
            else:
                session.assignment_event(task, user["id"], now)
            return task

    def repair_cdek_inbox(self, user):
        """Trusted worker repair; no task writes and no generic API route."""
        permissions.require_actor(user)
        self._assignee(user['id'])
        with self.repository.transaction(write=True) as session:
            rows = session.connection.execute(
                "SELECT * FROM tasks WHERE related_entity_type='cdek_call' "
                "AND task_type='micro' AND deleted_at IS NULL AND status!='done' AND assigned_to=?",
                (user['id'],)).fetchall()
            return sum(session.cdek_assignment_event(dict(row), self.now()) for row in rows)

    def get(self, user, task_id, include_inbox=False):
        permissions.require_actor(user)
        positive_integer(task_id, "id")
        with self.repository.transaction() as session:
            task = self._task(session, user, task_id)
            if include_inbox:
                task["inbox_pending"] = session.pending_assignment(task_id, user["id"]) is not None
            return task

    def activity(self, user, task_id, options=None):
        permissions.require_actor(user)
        positive_integer(task_id, "id")
        options = options or {}
        if set(options) - {"limit", "offset"}:
            raise invalid("query")
        paging = list_options(options)
        with self.repository.transaction() as session:
            self._task(session, user, task_id)
            return {"items": session.activity(task_id, paging["limit"], paging["offset"]),
                    "limit": paging["limit"], "offset": paging["offset"]}

    def list(self, user, options=None, summary=False, include_inbox=False):
        permissions.require_actor(user)
        # Pagination does not select a different statistical population.
        dashboard = summary and not (set(options or {}) - {"limit", "offset"})
        options = list_options(options or {})
        default_scope = "created" if not summary and options.get("view") == "delegated_waiting" else "my"
        scope = permissions.list_scope(user, options.get("scope", default_scope))
        today = self.today()
        with self.repository.transaction() as session:
            if dashboard:
                return session.dashboard_summary(scope, permissions.list_scope(user, "created"), today)
            return session.filtered_summary(scope, options, today) if summary else session.list(scope, options, today, include_inbox)

    @staticmethod
    def _authorize_mutation(user, task, version, operation, values):
        # Caller has fetched task through task_visibility in a current snapshot.
        permissions.require_view(user, task, project_access=True)
        if task["version"] != version:
            raise conflict()
        if task["task_type"] == "micro" and operation in ("patch", "status"):
            micro_values(dict(values, version=version))
        if operation == "patch":
            permissions.require_action(user, task, "edit", project_access=True)
            if "status" in values:
                permissions.require_action(user, task, "change_status", project_access=True)
            if "assigned_to" in values:
                permissions.require_action(user, task, "reassign", project_access=True)
            # Future ERP reference metadata is managed by the creator/admin;
            # it is never followed or resolved by this module.
            if any(field.startswith("related_") for field in values):
                permissions.require_action(user, task, "reassign", project_access=True)
        else:
            permissions.require_action(user, task, {"status": "change_status", "accept": "accept",
                                                   "delete": "delete", "restore": "restore",
                                                   "convert": "edit"}[operation], project_access=True)

    def mutate(self, user, task_id, payload, operation="patch", expected_type=None):
        permissions.require_actor(user)
        positive_integer(task_id, "id")
        if operation not in ("patch", "status", "delete", "restore", "convert", "accept"):
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
            before = self._task(session, user, task_id)
            if expected_type is not None and before["task_type"] != expected_type:
                raise not_found()
            self._authorize_mutation(user, before, version, operation, values)
            if operation == "accept":
                if session.pending_assignment(task_id, user["id"]) is None:
                    raise conflict()
                values = {"status": "in_progress"}
            if operation == "convert":
                if before["task_type"] != "micro":
                    raise invalid("task_type", "Это уже обычная задача.")
                values = {"task_type": "normal", "micro_deadline_at": None}
            if "project_id" in values and values["project_id"] != before["project_id"]:
                self._project(session, user, values["project_id"])
            now = self.now()
            if operation in ("delete", "restore"):
                values["deleted_at"] = now if operation == "delete" else None
            if (before["task_type"] == "normal" and "assigned_to" in values
                    and values["assigned_to"] != before["assigned_to"]
                    and values.get("status", before["status"]) != "done"):
                values["status"] = "new" if values["assigned_to"] == user["id"] else "waiting"
            changes = {field: value for field, value in values.items() if before[field] != value}
            if not changes and operation != "accept":
                raise invalid("body", "Задача уже содержит эти значения.")
            if "status" in changes:
                changes["completed_at"] = now if changes["status"] == "done" else None
            changes["updated_at"] = now
            task = session.update(task_id, version, changes)
            groups = (("content_changed", ("title", "description")),
                      ("reassigned", ("assigned_to",)), ("deadline_changed", ("deadline_date",)),
                      ("priority_changed", ("priority",)), ("status_changed", ("status",)),
                      ("related_reference_changed", ("related_entity_type", "related_entity_id", "related_entity_label")),
                      ("project_changed", ("project_id",)))
            for event_type, fields in groups:
                diff = {field: {"before": before[field], "after": task[field]} for field in fields if field in changes}
                if diff:
                    if operation == "accept":
                        diff["action"] = "accepted"
                    session.add_activity(task, user["id"], event_type, now, diff)
            if operation == "accept" and "status" not in changes:
                session.add_activity(task, user["id"], "status_changed", now,
                                     {"action": "accepted", "status": {"before": before["status"], "after": task["status"]}})
            if (task["task_type"] == "normal" and (operation == "accept" or
                    ("status" in changes and task["status"] in ("in_progress", "done")))):
                session.finish_inbox(task_id, now)
            if "status" in changes and (before["status"] == "done" or task["status"] == "done"):
                event = "completed" if task["status"] == "done" else "reopened"
                session.add_activity(task, user["id"], event, now,
                                     {"completed_at": {"before": before["completed_at"], "after": task["completed_at"]}})
            if operation in ("delete", "restore"):
                session.add_activity(task, user["id"], "deleted" if operation == "delete" else "restored", now,
                                     {"deleted_at": {"before": before["deleted_at"], "after": task["deleted_at"]}})
            if "assigned_to" in changes:
                session.assignment_event(task, user["id"], now, before["assigned_to"])
            if operation == "convert":
                session.add_activity(task, user["id"], "converted_to_normal", now,
                                     {"micro_deadline_at": before["micro_deadline_at"]})
            return task

    def micros(self, user, options=None, summary=False):
        permissions.require_actor(user)
        options = options or {}
        if summary and options:
            raise invalid("query")
        options = micro_options(options)
        now = utc_instant(self.now()).isoformat(timespec="microseconds")
        with self.repository.transaction() as session:
            if summary:
                return session.micro_summary({name: permissions.list_scope(user, name)
                                              for name in ("my", "created", "team")}, now)
            return session.micros(permissions.list_scope(user, options.get("scope", "my")), options, now)
