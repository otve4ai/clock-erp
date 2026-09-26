"""Separate API namespace; never replaces /app/tasks or /api/v1/tasks."""

from flask import Blueprint, current_app, jsonify

from .error_boundary import module_boundary
from .permissions import can_inspect_module
from .repository import TasksRepository
from .services import TasksService


def create_blueprint(current_user):
    blueprint = Blueprint("tasks_module", __name__, url_prefix="/api/v1/tasks-module")

    @blueprint.route("/status", methods=["GET"])
    @module_boundary
    def status():
        user = current_user() or {}
        if not user.get("id"):
            return jsonify(code="AUTH_REQUIRED", message="Требуется авторизация."), 401
        if not can_inspect_module(user):
            return jsonify(code="FORBIDDEN", message="Недостаточно прав."), 403
        service = TasksService(TasksRepository(current_app.config["TASKS_MODULE_DATABASE"]))
        response = jsonify(data=service.status())
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint
