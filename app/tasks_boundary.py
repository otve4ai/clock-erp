"""ERP-owned optional loader. No Tasks imports or database access when disabled."""

import importlib
import os
from pathlib import Path


def register_tasks_module(app, project_root, current_user, user_lookup=None, csrf_check=None, user_directory=None):
    app.config.setdefault("TASKS_MODULE_ENABLED", os.getenv("ERP_TASKS_MODULE_ENABLED", "0"))
    app.config.setdefault("TASKS_MODULE_DATABASE", os.getenv("ERP_TASKS_MODULE_DATABASE", "").strip()
                          or str(Path(project_root) / "instance" / "tasks-module.db"))
    enabled = str(app.config["TASKS_MODULE_ENABLED"]).lower() in {"1", "true", "yes", "on"}
    state = {"enabled": enabled, "registered": False}
    app.extensions["tasks_module"] = state
    if not enabled:
        return False
    try:
        # Auth is an ERP-owned adapter, not an app/tasks dependency. No database
        # access here; lookups run only when an enabled mutation needs one.
        from .auth import csrf_is_valid, csrf_token, get_auth_store
        module = importlib.import_module("app.tasks.routes")
        blueprint = module.create_blueprint(
            current_user,
            user_lookup or (lambda user_id: get_auth_store().get_active_user_identity(user_id)),
            csrf_check or csrf_is_valid,
            user_directory or (lambda: get_auth_store().list_active_task_identities()))
        app.register_blueprint(blueprint)
        ui_module = importlib.import_module("app.tasks.ui_routes")
        app.register_blueprint(ui_module.create_ui_blueprint(current_user, csrf_token))
        state["registered"] = True
        return True
    except Exception:
        # Even a failed/partial registration cannot expose functional new routes:
        # every view also checks this state through its local error boundary.
        app.logger.exception("Tasks module disabled after registration failure")
        return False
