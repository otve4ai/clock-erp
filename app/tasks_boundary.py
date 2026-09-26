"""ERP-owned optional loader. No Tasks imports or database access when disabled."""

import importlib
import os
from pathlib import Path


def register_tasks_module(app, project_root, current_user):
    app.config.setdefault("TASKS_MODULE_ENABLED", os.getenv("ERP_TASKS_MODULE_ENABLED", "0"))
    app.config.setdefault("TASKS_MODULE_DATABASE", os.getenv("ERP_TASKS_MODULE_DATABASE", "").strip()
                          or str(Path(project_root) / "instance" / "tasks-module.db"))
    enabled = str(app.config["TASKS_MODULE_ENABLED"]).lower() in {"1", "true", "yes", "on"}
    state = {"enabled": enabled, "registered": False}
    app.extensions["tasks_module"] = state
    if not enabled:
        return False
    try:
        module = importlib.import_module("app.tasks.routes")
        blueprint = module.create_blueprint(current_user)
        app.register_blueprint(blueprint)
        state["registered"] = True
        return True
    except Exception:
        # Even a failed/partial registration cannot expose functional new routes:
        # every view also checks this state through its local error boundary.
        app.logger.exception("Tasks module disabled after registration failure")
        return False
