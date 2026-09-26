"""Failures of optional Tasks views stay in their own HTTP response."""

from functools import wraps

from flask import current_app, jsonify


def unavailable():
    response = jsonify(code="TASKS_MODULE_UNAVAILABLE", message="Модуль задач недоступен.")
    response.status_code = 503
    response.headers["Cache-Control"] = "no-store"
    return response


def module_boundary(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        enabled = str(current_app.config.get("TASKS_MODULE_ENABLED", False)).lower() in {
            "1", "true", "yes", "on",
        }
        if not enabled or not current_app.extensions.get("tasks_module", {}).get("registered"):
            return unavailable()
        try:
            return view(*args, **kwargs)
        except Exception:
            current_app.logger.exception("Tasks module request failed")
            return unavailable()
    return wrapped
