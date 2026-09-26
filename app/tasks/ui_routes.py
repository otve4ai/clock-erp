"""Flag-gated new UI shell, separate from legacy /app/tasks. No Tasks DB reads."""

from flask import Blueprint, make_response, render_template

from .error_boundary import module_boundary
from .permissions import require_actor


def create_ui_blueprint(current_user, csrf_token):
    blueprint = Blueprint("tasks_module_ui", __name__)

    @blueprint.route("/app/tasks-module")
    @module_boundary
    def workspace():
        user = require_actor(current_user())
        response = make_response(render_template("tasks-module.html", tasks_user_id=user["id"], tasks_csrf=csrf_token()))
        response.headers["Cache-Control"] = "no-store"
        return response

    return blueprint
