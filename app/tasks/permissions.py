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


def can_view(user, task):
    return _owner(user, task) or user["id"] == task["assigned_to"]


def can_edit(user, task):
    return task["deleted_at"] is None and (_owner(user, task) or user["id"] == task["assigned_to"])


def can_change_status(user, task):
    return can_edit(user, task)


def can_reassign(user, task):
    return task["deleted_at"] is None and _owner(user, task)


def can_delete(user, task):
    return task["deleted_at"] is None and _owner(user, task)


def can_restore(user, task):
    return task["deleted_at"] is not None and _owner(user, task)


def require_view(user, task):
    require_actor(user)
    if task is None or not can_view(user, task):
        raise not_found()


def require_action(user, task, action):
    require_view(user, task)
    checks = {"edit": can_edit, "change_status": can_change_status,
              "reassign": can_reassign, "delete": can_delete, "restore": can_restore}
    if not checks[action](user, task):
        raise TaskError("FORBIDDEN", "Недостаточно прав для этого действия.", 403)


def list_scope(user, scope):
    require_actor(user)
    if scope == "my":
        return {"assigned_to": user["id"]}
    if scope == "created":
        return {"created_by": user["id"]}
    if scope == "all":
        if not can_inspect_module(user):
            raise TaskError("FORBIDDEN", "Общий список доступен администратору.", 403)
        return {}
    raise invalid("scope", "Допустимы my, created, all (admin).")
