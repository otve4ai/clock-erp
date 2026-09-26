"""Additive UI metadata from the central policy; never opens storage."""

from . import permissions


def present(data, user):
    if not isinstance(data, dict):
        return data
    value = dict(data)
    if {"id", "task_type", "created_by", "assigned_to", "deleted_at"} <= set(value):
        value["permissions"] = permissions.task_capabilities(user, value)
    elif {"id", "owner_id", "archived_at", "name"} <= set(value):
        value["permissions"] = {"manage": permissions.can_manage_project(user, value)}
    elif isinstance(value.get("items"), list):
        value["items"] = [present(item, user) for item in value["items"]]
    return value
