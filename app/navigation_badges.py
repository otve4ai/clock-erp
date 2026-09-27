"""Optional, bounded, read-only counters, never part of a page render."""

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import jsonify


def _count(path, query, parameters):
    connection = sqlite3.connect(
        Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05,
    )
    try:
        deadline = time.monotonic() + 0.10
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        return int(connection.execute(query, parameters).fetchone()[0])
    finally:
        connection.close()


def register_navigation_badges(app, current_user):
    def badge(kind):
        user = current_user() or {}
        if not user.get("id"):
            response = jsonify(code="AUTH_REQUIRED", message="Требуется авторизация.")
            response.status_code = 401
        else:
            try:
                if kind == "tasks":
                    today = datetime.now(timezone(timedelta(hours=3))).date().isoformat()
                    count = _count(app.config["TASKS_DATABASE"],
                        "SELECT COUNT(*) FROM tasks WHERE assignee_id=? AND deleted_at IS NULL "
                        "AND status IN ('new','in_progress','waiting') "
                        "AND (due_date<=? OR (status='waiting' AND check_date<=?))",
                        (user["id"], today, today))
                else:
                    count = _count(app.config["TASKS_DATABASE"],
                        "SELECT COUNT(*) FROM inbox_events "
                        "WHERE recipient_user_id=? AND read_at IS NULL", (user["id"],))
                response = jsonify(data={"count": count})
            except Exception:
                app.logger.warning("Optional %s badge unavailable", kind, exc_info=True)
                response = jsonify(code="BADGE_UNAVAILABLE", message="Счётчик недоступен.")
                response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_url_rule("/api/v1/tasks/badge", "task_sidebar_badge", lambda: badge("tasks"))
    app.add_url_rule("/api/v1/inbox/badge", "inbox_sidebar_badge", lambda: badge("inbox"))
