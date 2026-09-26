"""Separate API namespace; never replaces /app/tasks or /api/v1/tasks."""

from flask import Blueprint, current_app, json, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from .domain import TaskError, invalid, business_today, utc_now
from .error_boundary import module_boundary
from .permissions import can_inspect_module
from .repository import TasksRepository
from .services import TasksService
from .project_services import ProjectsService
from .inbox_services import InboxService
from .permissions import require_actor
from .presentation import present


def create_blueprint(current_user, user_lookup=None, csrf_check=None, user_directory=None):
    blueprint = Blueprint("tasks_module", __name__, url_prefix="/api/v1/tasks-module")

    def service():
        return TasksService(TasksRepository(current_app.config["TASKS_MODULE_DATABASE"]), user_lookup)

    def projects_service():
        return ProjectsService(TasksRepository(current_app.config["TASKS_MODULE_DATABASE"]), user_lookup)

    def inbox_service():
        return InboxService(TasksRepository(current_app.config["TASKS_MODULE_DATABASE"]))

    def data_response(data, status=200):
        response = jsonify(data=present(data, current_user()))
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        return response

    def payload():
        if not csrf_check or not csrf_check():
            raise TaskError("CSRF_INVALID", "Не удалось подтвердить запрос.", 403)
        if not request.is_json:
            raise BadRequest()
        maximum = current_app.config.get("MAX_CONTENT_LENGTH")
        if maximum is None:
            body = request.get_json()
        else:
            # Werkzeug 2.0 does not enforce this limit when reading JSON.
            # Bound even a terminated WSGI stream without Content-Length.
            if request.content_length is not None and request.content_length > maximum:
                raise RequestEntityTooLarge()
            # get_data(cache=True) stores bytes here in Werkzeug 2.0 and 3.x.
            # An empty cached body is still cached; do not reread its stream.
            raw = getattr(request, "_cached_data", None)
            if raw is None:
                # New Werkzeug may clamp request.stream at exactly the maximum,
                # hiding the extra byte. A terminated WSGI stream is safe to read
                # directly with our own explicit bound, including at exact limit.
                stream = request.input_stream if request.environ.get("wsgi.input_terminated") else request.stream
                raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise RequestEntityTooLarge()
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BadRequest()
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

    @blueprint.route("/directory", methods=["GET"])
    @module_boundary
    def directory():
        require_actor(current_user())
        if query():
            raise invalid("query")
        if user_directory is None:
            raise TaskError("USER_LOOKUP_UNAVAILABLE", "Список сотрудников недоступен.", 503)
        return data_response({"items": user_directory(), "business_date": business_today(), "server_now": utc_now()})

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

    @blueprint.route("/projects", methods=["GET", "POST"])
    @module_boundary
    def projects():
        user = current_user()
        if request.method == "POST":
            return data_response(projects_service().create(user, payload()), 201)
        return data_response(projects_service().list(user, query()))

    @blueprint.route("/projects/<int:project_id>", methods=["GET", "PATCH"])
    @module_boundary
    def project(project_id):
        if request.method == "PATCH":
            return data_response(projects_service().mutate(current_user(), project_id, payload()))
        return data_response(projects_service().get(current_user(), project_id))

    @blueprint.route("/projects/<int:project_id>/archive", methods=["POST"])
    @blueprint.route("/projects/<int:project_id>/restore", methods=["POST"])
    @module_boundary
    def project_state(project_id):
        return data_response(projects_service().mutate(current_user(), project_id, payload(), request.path.rsplit("/", 1)[-1]))

    @blueprint.route("/projects/<int:project_id>/members", methods=["GET", "POST"])
    @module_boundary
    def project_members(project_id):
        if request.method == "POST":
            return data_response(projects_service().mutate(current_user(), project_id, payload(), "member_add"))
        return data_response(projects_service().details(current_user(), project_id, "members", query()))

    @blueprint.route("/projects/<int:project_id>/members/<int:user_id>", methods=["DELETE"])
    @module_boundary
    def project_member_remove(project_id, user_id):
        return data_response(projects_service().mutate(current_user(), project_id, payload(), "member_remove", user_id))

    @blueprint.route("/projects/<int:project_id>/summary", methods=["GET"])
    @blueprint.route("/projects/<int:project_id>/activity", methods=["GET"])
    @module_boundary
    def project_details(project_id):
        return data_response(projects_service().details(current_user(), project_id, request.path.rsplit("/", 1)[-1], query()))

    @blueprint.route("/microtasks", methods=["GET", "POST"])
    @module_boundary
    def microtasks():
        if request.method == "POST":
            return data_response(service().create_micro(current_user(), payload()), 201)
        return data_response(service().micros(current_user(), query()))

    @blueprint.route("/microtasks/summary", methods=["GET"])
    @module_boundary
    def micro_summary():
        return data_response(service().micros(current_user(), query(), summary=True))

    @blueprint.route("/microtasks/<int:task_id>", methods=["PATCH"])
    @module_boundary
    def micro_patch(task_id):
        return data_response(service().mutate(current_user(), task_id, payload(), expected_type="micro"))

    @blueprint.route("/microtasks/<int:task_id>/complete", methods=["POST"])
    @blueprint.route("/microtasks/<int:task_id>/reopen", methods=["POST"])
    @blueprint.route("/microtasks/<int:task_id>/convert", methods=["POST"])
    @module_boundary
    def micro_action(task_id):
        operation = request.path.rsplit("/", 1)[-1]
        values = payload()
        if set(values) != {"version"}:
            raise invalid("body", "Нужна только version.")
        if operation != "convert":
            values["status"] = "done" if operation == "complete" else "new"
            operation = "status"
        return data_response(service().mutate(current_user(), task_id, values, operation, expected_type="micro"))

    @blueprint.route("/inbox", methods=["GET"])
    @blueprint.route("/inbox/badge", methods=["GET"])
    @module_boundary
    def inbox():
        return data_response(inbox_service().list(current_user(), query(), badge=request.path.endswith("/badge")))

    @blueprint.route("/inbox/<int:event_id>/read", methods=["POST"])
    @module_boundary
    def inbox_read(event_id):
        return data_response(inbox_service().read(current_user(), event_id, payload()))

    @blueprint.route("/notifications/claim", methods=["POST"])
    @module_boundary
    def notifications_claim():
        return data_response(inbox_service().claim(current_user(), payload()))

    return blueprint
