"""Separate API namespace; never replaces /app/tasks or /api/v1/tasks."""

from flask import Blueprint, current_app, jsonify, request

from .domain import TaskError, invalid
from .error_boundary import module_boundary
from .permissions import can_inspect_module
from .repository import TasksRepository
from .services import TasksService


def create_blueprint(current_user, user_lookup=None, csrf_check=None):
    blueprint = Blueprint("tasks_module", __name__, url_prefix="/api/v1/tasks-module")

    def service():
        return TasksService(TasksRepository(current_app.config["TASKS_MODULE_DATABASE"]), user_lookup)

    def data_response(data, status=200):
        response = jsonify(data=data)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    def payload():
        if not csrf_check or not csrf_check():
            raise TaskError("CSRF_INVALID", "Не удалось подтвердить запрос.", 403)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise invalid("body", "Ожидается JSON object.")
        return body

    def query():
        for key in request.args:
            if len(request.args.getlist(key)) != 1:
                raise invalid(key, "Фильтр должен быть указан один раз.")
        return request.args.to_dict()

    @blueprint.route("/status", methods=["GET"])
    @module_boundary
    def status():
        user = current_user() or {}
        if not user.get("id"):
            return jsonify(code="AUTH_REQUIRED", message="Требуется авторизация."), 401
        if not can_inspect_module(user):
            return jsonify(code="FORBIDDEN", message="Недостаточно прав."), 403
        return data_response(service().status())

    @blueprint.route("/tasks", methods=["GET", "POST"])
    @module_boundary
    def tasks():
        user = current_user()
        if request.method == "POST":
            return data_response(service().create(user, payload()), 201)
        return data_response(service().list(user, query()))

    @blueprint.route("/tasks/summary", methods=["GET"])
    @module_boundary
    def summary():
        return data_response(service().list(current_user(), query(), summary=True))

    @blueprint.route("/tasks/<int:task_id>", methods=["GET", "PATCH"])
    @module_boundary
    def task(task_id):
        user = current_user()
        if request.method == "PATCH":
            return data_response(service().mutate(user, task_id, payload()))
        return data_response(service().get(user, task_id))

    @blueprint.route("/tasks/<int:task_id>/status", methods=["POST"])
    @blueprint.route("/tasks/<int:task_id>/delete", methods=["POST"])
    @blueprint.route("/tasks/<int:task_id>/restore", methods=["POST"])
    @module_boundary
    def mutate(task_id):
        operation = request.path.rsplit("/", 1)[-1]
        return data_response(service().mutate(current_user(), task_id, payload(), operation))

    @blueprint.route("/tasks/<int:task_id>/activity", methods=["GET"])
    @module_boundary
    def activity(task_id):
        return data_response(service().activity(current_user(), task_id, query()))

    return blueprint
