"""The only policy layer: services check actions, repositories receive filters."""

from .domain import TaskError, invalid, not_found


def require_actor(user):
    if not user or type(user.get("id")) is not int or user["id"] <= 0 or user.get("active", 1) != 1:
        raise TaskError("AUTH_REQUIRED", "Требуется авторизация.", 401)
    return user


def can_inspect_module(user):
    return bool(user and user.get("id") and user.get("role") == "admin")


def _owner(user, task):
    return can_inspect_module(user) or user["id"] == task["created_by"]


def can_view(user, task, project_access=False):
    return _owner(user, task) or user["id"] == task["assigned_to"] or project_access


def can_edit(user, task):
    return task["deleted_at"] is None and (_owner(user, task) or user["id"] == task["assigned_to"])


def can_change_status(user, task):
    return can_edit(user, task)


def can_accept(user, task):
    return (task["deleted_at"] is None and task["task_type"] == "normal"
            and task["status"] != "done" and user["id"] == task["assigned_to"])


def can_reassign(user, task):
    return task["deleted_at"] is None and _owner(user, task)


def can_delete(user, task):
    return task["deleted_at"] is None and _owner(user, task)


def can_restore(user, task):
    return task["deleted_at"] is not None and _owner(user, task)


def require_view(user, task, project_access=False):
    require_actor(user)
    if task is None or not can_view(user, task, project_access):
        raise not_found()


def require_action(user, task, action, project_access=False):
    require_view(user, task, project_access)
    checks = {"edit": can_edit, "change_status": can_change_status, "accept": can_accept,
              "reassign": can_reassign, "delete": can_delete, "restore": can_restore}
    if not checks[action](user, task):
        raise TaskError("FORBIDDEN", "Недостаточно прав для этого действия.", 403)


def list_scope(user, scope):
    require_actor(user)
    result = {"visibility": task_visibility(user), "actor_id": user["id"]}
    if scope == "my":
        result["assigned_to"] = user["id"]
    elif scope == "created":
        result["created_by"] = user["id"]
    elif scope not in ("all", "team"):
        raise invalid("scope", "Допустимы my, created, team, all.")
    return result


def project_visibility(user):
    require_actor(user)
    if can_inspect_module(user):
        return "1=1", []
    return ("(task_projects.owner_id=? OR EXISTS (SELECT 1 FROM task_project_members pm "
            "WHERE pm.project_id=task_projects.id AND pm.user_id=?))", [user["id"], user["id"]])


def task_visibility(user):
    """One parameterized visibility predicate for direct IDs and every collection.

    Repositories execute this policy; they never interpret roles themselves.
    EXISTS avoids duplicate tasks when the actor has multiple kinds of access.
    """
    project_sql, project_params = project_visibility(user)
    if can_inspect_module(user):
        return "1=1", []
    return ("(tasks.created_by=? OR tasks.assigned_to=? OR EXISTS (SELECT 1 FROM task_projects "
            "WHERE task_projects.id=tasks.project_id AND " + project_sql + "))",
            [user["id"], user["id"]] + project_params)


def require_project(user, project, manage=False):
    # project was fetched using project_visibility in the same local snapshot.
    require_actor(user)
    if project is None:
        raise TaskError("PROJECT_NOT_FOUND", "Проект не найден.", 404)
    if manage and not can_manage_project(user, project):
        raise TaskError("FORBIDDEN", "Недостаточно прав для этого действия.", 403)


def can_manage_project(user, project):
    return can_inspect_module(user) or project["owner_id"] == user["id"]


def task_capabilities(user, task):
    return {"edit": can_edit(user, task), "change_status": can_change_status(user, task),
            "accept": can_accept(user, task),
            "reassign": can_reassign(user, task), "delete": can_delete(user, task),
            "restore": can_restore(user, task)}
