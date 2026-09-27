"""Project permissions, membership and history in a single local transaction."""

from . import permissions
from .domain import business_today, invalid, list_options, positive_integer, project_name, project_options, utc_now
from .project_repository import project_conflict
from .services import require_active_user


class ProjectsService:
    def __init__(self, repository, user_lookup=None, now=utc_now, today=business_today):
        self.repository, self.user_lookup, self.now, self.today = repository, user_lookup, now, today

    @staticmethod
    def _project(session, user, project_id, manage=False):
        positive_integer(project_id, "project_id")
        project = session.get_project(project_id, permissions.project_visibility(user))
        permissions.require_project(user, project, manage)
        return project

    @staticmethod
    def _version(project, version):
        if project["version"] != version:
            raise project_conflict()

    def create(self, user, payload):
        permissions.require_actor(user)
        if not isinstance(payload, dict) or set(payload) != {"name"}:
            raise invalid("body", "Нужно только название проекта.")
        name = project_name(payload["name"])
        now = self.now()
        with self.repository.transaction(write=True) as session:
            project = session.create_project(name, user["id"], now)
            session.add_project_activity(project, user["id"], "created", now, {"project": project})
            return project

    def get(self, user, project_id):
        permissions.require_actor(user)
        with self.repository.transaction() as session:
            return self._project(session, user, project_id)

    def list(self, user, options=None):
        visibility = permissions.project_visibility(user)
        options = project_options(options or {})
        with self.repository.transaction() as session:
            return session.projects(visibility, options, self.today())

    def mutate(self, user, project_id, payload, operation="rename", member_id=None):
        permissions.require_actor(user)
        fields = {"rename": {"name", "version"}, "archive": {"version"}, "restore": {"version"},
                  "member_add": {"user_id", "version"}, "member_remove": {"version"}}
        if operation not in fields or not isinstance(payload, dict) or set(payload) != fields[operation]:
            raise invalid("body")
        version = positive_integer(payload["version"], "version")
        name = project_name(payload["name"]) if operation == "rename" else None
        if operation in ("member_add", "member_remove"):
            member_id = positive_integer(payload.get("user_id", member_id), "user_id")
        if operation == "member_add":
            # No auth connection inside the Tasks transaction. Recheck both
            # membership authority and project version after the external read.
            with self.repository.transaction() as session:
                before = self._project(session, user, project_id, manage=True)
                self._version(before, version)
            require_active_user(self.user_lookup, member_id, "user_id")
        with self.repository.transaction(write=True) as session:
            before = self._project(session, user, project_id, manage=True)
            self._version(before, version)
            now = self.now()
            changes, diff = {"updated_at": now}, {}
            if operation == "rename":
                if before["name"] == name:
                    raise invalid("name", "Название уже установлено.")
                changes["name"] = name
                event, diff = "renamed", {"name": {"before": before["name"], "after": name}}
            elif operation in ("archive", "restore"):
                if (before["archived_at"] is not None) == (operation == "archive"):
                    raise invalid("operation", "Проект уже в этом состоянии.")
                changes["archived_at"] = now if operation == "archive" else None
                event = "archived" if operation == "archive" else "restored"
                diff = {"archived_at": {"before": before["archived_at"], "after": changes["archived_at"]}}
            else:
                exists = session.has_member(project_id, member_id)
                if member_id == before["owner_id"] or exists == (operation == "member_add"):
                    raise invalid("user_id", "Участник уже в этом состоянии; владелец имеет доступ автоматически.")
                if operation == "member_add":
                    session.add_member(project_id, member_id, now)
                    event = "member_added"
                else:
                    session.remove_member(project_id, member_id)
                    event = "member_removed"
                diff = {"user_id": member_id}
            project = session.update_project(project_id, version, changes)
            session.add_project_activity(project, user["id"], event, now, diff)
            return project

    def details(self, user, project_id, kind, options=None):
        permissions.require_actor(user)
        options = options or {}
        if set(options) - {"limit", "offset"} or kind not in ("members", "activity", "summary"):
            raise invalid("query")
        paging = list_options(options)
        with self.repository.transaction() as session:
            project = self._project(session, user, project_id)
            if kind == "summary":
                return session.project_counters(project_id, self.today())
            if kind == "members":
                result = session.members(project_id, paging["limit"], paging["offset"])
                result.update(owner_id=project["owner_id"], version=project["version"])
                return result
            return {"items": session.project_activity(project_id, paging["limit"], paging["offset"]),
                    "limit": paging["limit"], "offset": paging["offset"]}
