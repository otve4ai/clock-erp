#!/usr/bin/env python3
"""Explicit offline data preparation; dry-run by default, no schema migration.

Run BEFORE enabling the new workflow, with a verified backup and no writers.
Never imported by the application. Only this module's database is opened.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tasks.domain import positive_integer, utc_instant, utc_now
from app.tasks.repository import TasksRepository


def prepare(repository, actor_id, before, apply=False):
    positive_integer(actor_id, "actor_id")
    cutoff = utc_instant(before)
    result = {"apply": apply, "waiting_to_in_progress": [], "micro_inbox_restored": []}
    with repository.transaction(write=apply) as session:
        rows = session.connection.execute("SELECT * FROM tasks WHERE task_type='normal' AND status='waiting'").fetchall()
        for row in rows:
            task = dict(row)
            if utc_instant(task["updated_at"]) > cutoff:
                continue
            result["waiting_to_in_progress"].append(task["id"])
            if apply:
                now = utc_now()
                task = session.update(task["id"], task["version"], {"status": "in_progress", "updated_at": now})
                session.add_activity(task, actor_id, "status_changed", now,
                    {"action": "inbox_workflow_preparation", "status": {"before": "waiting", "after": "in_progress"}})
        # A read in the previous UI did not mean completion. Restore only the
        # current assignment generation, never a retired/self assignment.
        rows = session.connection.execute(
            "SELECT e.* FROM task_inbox_events e JOIN tasks t ON t.id=e.task_id "
            "WHERE t.task_type='micro' AND t.status!='done' AND t.deleted_at IS NULL "
            "AND t.assigned_to=e.recipient_id AND e.handled_at IS NOT NULL "
            "AND e.id=(SELECT MAX(last.id) FROM task_inbox_events last WHERE last.task_id=t.id)").fetchall()
        for row in rows:
            if utc_instant(row["created_at"]) > cutoff or utc_instant(row["handled_at"]) > cutoff:
                continue
            event_version = int(row["dedupe_key"].rsplit(":", 1)[-1])
            later_assignment = session.connection.execute(
                "SELECT 1 FROM task_activity WHERE task_id=? AND event_type='reassigned' AND task_version>? LIMIT 1",
                (row["task_id"], event_version)).fetchone()
            if later_assignment:
                continue
            result["micro_inbox_restored"].append(row["id"])
            if apply:
                session.connection.execute(
                    "UPDATE task_inbox_events SET handled_at=NULL,notified_at=COALESCE(notified_at,?) WHERE id=?",
                    (utc_now(), row["id"]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--actor-id", type=int, required=True, help="ERP administrator performing this preparation")
    parser.add_argument("--before", required=True, help="UTC timestamp captured before the workflow release")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prepare(TasksRepository(args.database), args.actor_id, args.before, args.apply), sort_keys=True))


if __name__ == "__main__":
    main()
