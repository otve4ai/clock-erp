"""Failures of optional Tasks views stay in their own HTTP response."""

from functools import wraps

from flask import current_app, jsonify
from werkzeug.exceptions import HTTPException

from .domain import TaskError


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
        except TaskError as error:
            body = {"code": error.code, "message": error.message}
            if error.fields:
                body["fields"] = error.fields
            response = jsonify(body)
            response.status_code = error.status
            response.headers["Cache-Control"] = "no-store"
            return response
        except HTTPException as error:
            # Preserve the HTTP status, never expose description/response details.
            response = jsonify(code="HTTP_ERROR", message="Не удалось обработать запрос.")
            response.status_code = error.code or 500
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception:
            current_app.logger.exception("Tasks module request failed")
            return unavailable()
    return wrapped
