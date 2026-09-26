"""Stage C: real SQLite transactions, project visibility, views and API contracts."""

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from flask import Flask, request

from app.tasks import migrations, permissions
from app.tasks.domain import TaskError, business_today, list_options
from app.tasks.project_repository import ProjectQueries
from app.tasks.project_services import ProjectsService
from app.tasks.repository import TasksRepository, TaskSession, validate_connection
from app.tasks.schema import V2_CORE_DDL, LEDGER_DDL, CORE_DDL
from app.tasks.services import TasksService
from app.tasks_boundary import register_tasks_module


EVIDENCE = {}
BASE = "/api/v1/tasks-module"


class ProjectsFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks-module.db"
        migrations.migrate_database(self.path)
        self.users = {i: {"id": i, "active": 0 if i == 7 else 1,
                          "role": "admin" if i == 6 else "employee"} for i in range(1, 9)}
        self.repo = TasksRepository(self.path)
        self.now = "2026-09-26T12:00:00+00:00"
        self.projects = ProjectsService(self.repo, self.users.get, now=lambda: self.now, today=lambda: "2026-09-26")
        self.tasks = TasksService(self.repo, self.users.get, now=lambda: self.now, today=lambda: "2026-09-26")

    def error(self, status, action, *args, **kwargs):
        with self.assertRaises(TaskError) as error:
            action(*args, **kwargs)
        self.assertEqual(error.exception.status, status)

    def project(self, owner=1, members=()):
        project = self.projects.create(self.users[owner], {"name": "  Project  "})
        for member in members:
            project = self.projects.mutate(self.users[owner], project["id"],
                                           {"version": project["version"], "user_id": member}, "member_add")
        return project

    def task(self, creator=2, **values):
        payload = {"title": "Task", "assigned_to": 3}
        payload.update(values)
        return self.tasks.create(self.users[creator], payload)

    def ids(self, actor, **options):
        return {task["id"] for task in self.tasks.list(self.users[actor], options)["items"]}

    def mutate_project(self, project, operation="rename", actor=1, **fields):
        return self.projects.mutate(self.users[actor], project["id"], dict(version=project["version"], **fields), operation)


class ProjectsDomainTest(ProjectsFixture):
    def test_user_and_admin_create_and_own_project(self):
        for owner in (1, 6):
            project = self.project(owner)
            self.assertEqual((project["owner_id"], project["version"], project["name"]), (owner, 1, "Project"))
            self.assertEqual(self.projects.details(self.users[owner], project["id"], "members")["items"], [])
            self.assertEqual(self.projects.details(self.users[owner], project["id"], "activity")["items"][0]["event_type"], "created")

    def test_project_validation_and_owner_cannot_be_spoofed(self):
        for payload in ({}, {"name": " "}, {"name": "\ud800"}, {"name": "x" * 201},
                        {"name": "x", "owner_id": 6}, {"name": None}):
            self.error(422, self.projects.create, self.users[1], payload)
        self.assertEqual(self.projects.create(self.users[1], {"name": "Проект 日本語 🚀"})["name"], "Проект 日本語 🚀")

    def test_owner_renames_admin_manages_member_only_views(self):
        project = self.project(members=(4,))
        project = self.mutate_project(project, name="Renamed")
        self.assertEqual(project["name"], "Renamed")
        for action, fields in (("rename", {"name": "Blocked"}), ("archive", {}), ("member_add", {"user_id": 5})):
            self.error(403, self.mutate_project, project, action, actor=4, **fields)
        project = self.mutate_project(project, "archive", actor=6)
        project = self.mutate_project(project, "restore", actor=6)
        self.assertIsNone(project["archived_at"])

    def test_member_add_remove_and_history_are_versioned(self):
        project = self.project()
        project = self.mutate_project(project, "member_add", user_id=4)
        self.assertEqual(self.projects.get(self.users[4], project["id"]), project)
        self.assertEqual(self.projects.details(self.users[4], project["id"], "members")["items"][0]["user_id"], 4)
        project = self.projects.mutate(self.users[1], project["id"], {"version": project["version"]}, "member_remove", 4)
        self.error(404, self.projects.get, self.users[4], project["id"])
        events = self.projects.details(self.users[1], project["id"], "activity")["items"]
        self.assertEqual([row["event_type"] for row in events], ["created", "member_added", "member_removed"])
        self.assertEqual([row["project_version"] for row in events], [1, 2, 3])

    def test_stranger_cannot_get_list_search_history_members_or_counters(self):
        project = self.project()
        self.error(404, self.projects.get, self.users[5], project["id"])
        for kind in ("members", "activity", "summary"):
            self.error(404, self.projects.details, self.users[5], project["id"], kind)
        self.assertEqual(self.projects.list(self.users[5], {"search": "Project"})["total"], 0)
        self.error(404, self.mutate_project, project, name="IDOR", actor=5)

    def test_member_must_be_active_and_cannot_duplicate_owner(self):
        project = self.project()
        for user_id in (7, 999, True, "4", 1):
            self.error(422, self.mutate_project, project, "member_add", user_id=user_id)
        project = self.mutate_project(project, "member_add", user_id=4)
        self.error(422, self.mutate_project, project, "member_add", user_id=4)
        self.error(422, self.projects.mutate, self.users[1], project["id"], {"version": project["version"]}, "member_remove", 1)

    def test_stale_project_versions_for_all_operations(self):
        original = self.project()
        self.mutate_project(original, name="First")
        for operation, fields in (("rename", {"name": "Stale"}), ("archive", {}), ("restore", {}), ("member_add", {"user_id": 4})):
            self.error(409, self.mutate_project, original, operation, **fields)
        self.error(409, self.projects.mutate, self.users[1], original["id"], {"version": 1}, "member_remove", 4)

    def test_concurrent_project_updates_have_one_winner(self):
        project = self.project()
        barrier = threading.Barrier(2)
        def update(name):
            barrier.wait()
            try:
                return self.mutate_project(project, name=name)["version"]
            except TaskError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, ("First", "Second")))
        self.assertEqual(sorted(results), [2, 409])

    def test_membership_race_rechecks_version_after_lookup(self):
        project = self.project()
        def lookup(user_id):
            self.mutate_project(project, name="Concurrent")
            return self.users[user_id]
        self.projects.user_lookup = lookup
        self.error(409, self.mutate_project, project, "member_add", user_id=4)
        self.assertEqual(self.projects.details(self.users[1], project["id"], "members")["total"], 0)

    def test_lookup_failure_cannot_change_membership(self):
        project = self.project()
        self.projects.user_lookup = mock.Mock(side_effect=RuntimeError("lookup unavailable"))
        self.error(503, self.mutate_project, project, "member_add", user_id=4)
        self.assertEqual(self.projects.get(self.users[1], project["id"]), project)

    def test_membership_interleavings_have_no_partial_writes_or_version_bumps(self):
        for winner in ("rename", "member_add", "member_remove"):
            with self.subTest(winner=winner):
                project = self.project(members=(4,))
                original_lookup = self.projects.user_lookup
                def lookup(user_id):
                    # Second service represents a request completing while the
                    # first request is outside its write transaction for auth.
                    other = ProjectsService(self.repo, self.users.get, now=lambda: self.now)
                    fields = {"name": "Winner"} if winner == "rename" else ({"user_id": 8} if winner == "member_add" else {})
                    other.mutate(self.users[1], project["id"], dict(version=project["version"], **fields), winner, 4)
                    return self.users[user_id]
                self.projects.user_lookup = lookup
                try:
                    self.error(409, self.mutate_project, project, "member_add", user_id=5)
                finally:
                    self.projects.user_lookup = original_lookup
                current = self.projects.get(self.users[1], project["id"])
                self.assertEqual(current["version"], project["version"] + 1)
                members = {row["user_id"] for row in self.projects.details(self.users[1], project["id"], "members")["items"]}
                self.assertEqual(members, {4, 8} if winner == "member_add" else (set() if winner == "member_remove" else {4}))
                events = self.projects.details(self.users[1], project["id"], "activity")["items"]
                self.assertEqual(len(events), 3)
                self.assertEqual(events[-1]["project_version"], current["version"])
        project = self.project(members=(4,))
        baseline = self.projects.details(self.users[1], project["id"], "activity")
        self.error(422, self.mutate_project, project, "member_add", user_id=4)
        self.error(422, self.projects.mutate, self.users[1], project["id"], {"version": project["version"]}, "member_remove", 5)
        self.assertEqual(self.projects.get(self.users[1], project["id"]), project)
        self.assertEqual(self.projects.details(self.users[1], project["id"], "activity"), baseline)

    def test_archive_restore_never_mutate_tasks(self):
        project = self.project(members=(2,))
        task = self.task(project_id=project["id"])
        activity = self.tasks.activity(self.users[2], task["id"])
        project = self.mutate_project(project, "archive")
        self.assertEqual(self.projects.list(self.users[1])["total"], 0)
        self.assertEqual(self.projects.list(self.users[1], {"archived": "true"})["total"], 1)
        self.assertEqual(self.tasks.get(self.users[2], task["id"]), task)
        self.assertEqual(self.tasks.activity(self.users[2], task["id"]), activity)
        self.assertIn(task["id"], self.ids(1, scope="all"))
        restored = self.mutate_project(project, "restore")
        self.assertEqual(self.projects.list(self.users[1])["total"], 1)
        self.assertIsNone(restored["archived_at"])
        self.error(422, self.mutate_project, restored, "restore")

    def test_project_activity_failure_rolls_back_create(self):
        with mock.patch.object(ProjectQueries, "add_project_activity", side_effect=sqlite3.OperationalError("history")):
            with self.assertRaises(sqlite3.OperationalError):
                self.project()
        self.assertEqual(self.projects.list(self.users[1])["total"], 0)

    def test_project_activity_failure_rolls_back_every_mutation(self):
        for operation, fields in (("rename", {"name": "Changed"}), ("archive", {}), ("restore", {}),
                                  ("member_add", {"user_id": 5}), ("member_remove", {})):
            with self.subTest(operation=operation):
                project = self.project(members=(4,))
                if operation == "restore":
                    project = self.mutate_project(project, "archive")
                members = self.projects.details(self.users[1], project["id"], "members")
                activity = self.projects.details(self.users[1], project["id"], "activity")
                with mock.patch.object(ProjectQueries, "add_project_activity", side_effect=sqlite3.OperationalError("history")):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.projects.mutate(self.users[1], project["id"], dict(version=project["version"], **fields), operation, 4)
                self.assertEqual(self.projects.get(self.users[1], project["id"]), project)
                self.assertEqual(self.projects.details(self.users[1], project["id"], "members"), members)
                self.assertEqual(self.projects.details(self.users[1], project["id"], "activity"), activity)


class ProjectTaskVisibilityTest(ProjectsFixture):
    def test_project_visibility_matrix_and_view_is_not_edit(self):
        project = self.project(members=(2, 4))
        task = self.task(project_id=project["id"])
        for actor in (1, 2, 3, 4, 6):
            self.assertEqual(self.tasks.get(self.users[actor], task["id"]), task)
            self.assertEqual(len(self.tasks.activity(self.users[actor], task["id"])["items"]), 1)
        self.error(404, self.tasks.get, self.users[5], task["id"])
        for actor in (1, 4):
            for operation, values in (("patch", {"title": "No"}), ("status", {"status": "done"}), ("delete", {})):
                self.error(403, self.tasks.mutate, self.users[actor], task["id"], dict(version=1, **values), operation)
        changed = self.tasks.mutate(self.users[3], task["id"], {"version": 1, "description": "Work"})
        self.assertEqual(changed["description"], "Work")

    def test_projectless_tasks_remain_private(self):
        self.project(members=(2, 4))
        task = self.task()
        for actor in (2, 3, 6):
            self.assertEqual(self.tasks.get(self.users[actor], task["id"]), task)
        for actor in (1, 4, 5):
            self.error(404, self.tasks.get, self.users[actor], task["id"])
            self.assertEqual(self.ids(actor, scope="all"), set())

    def test_scope_definitions_and_union_has_no_duplicates(self):
        project = self.project(members=(2, 4))
        shared = self.task(project_id=project["id"])
        mine = self.task(assigned_to=4)
        created = self.task(creator=4, assigned_to=5)
        hidden = self.task(creator=5, assigned_to=5)
        self.assertEqual(self.ids(4, scope="my"), {mine["id"]})
        self.assertEqual(self.ids(4, scope="created"), {created["id"]})
        for scope in ("all", "team"):
            self.assertEqual(self.ids(4, scope=scope), {shared["id"], mine["id"], created["id"]})
        self.assertEqual(self.ids(6, scope="all"), {row["id"] for row in (shared, mine, created, hidden)})
        self.assertEqual(self.tasks.list(self.users[2], {"scope": "all"})["total"], 2)

    def test_search_summary_and_project_counters_obey_visibility(self):
        project = self.project(members=(2, 4))
        self.task(project_id=project["id"], title="Needle", status="in_progress", deadline_date="2026-09-25")
        self.task(project_id=project["id"], status="done")
        self.task(creator=5, assigned_to=5, title="Needle", status="in_progress", deadline_date="2026-09-25")
        for actor in (1, 4):
            self.assertEqual(len(self.ids(actor, scope="all", search="Needle")), 1)
            summary = self.tasks.list(self.users[actor], {"scope": "all"}, summary=True)
            self.assertEqual((summary["total"], summary["in_progress"], summary["overdue"], summary["inbox"]), (2, 1, 1, None))
            expected = {"open": 1, "in_progress": 1, "overdue": 1}
            self.assertEqual(self.projects.details(self.users[actor], project["id"], "summary"), expected)
            self.assertEqual(self.projects.list(self.users[actor])["items"][0]["counters"], expected)

    def test_removed_member_loses_access_but_creator_and_assignee_keep_it(self):
        project = self.project(members=(2, 4))
        task = self.task(project_id=project["id"])
        for member in (4, 2):
            project = self.projects.mutate(self.users[1], project["id"], {"version": project["version"]}, "member_remove", member)
        self.error(404, self.tasks.get, self.users[4], task["id"])
        self.assertEqual(self.ids(4, scope="all", search="Task"), set())
        self.assertEqual(self.tasks.list(self.users[4], summary=True)["total"], 0)
        for actor in (2, 3):
            self.assertEqual(self.tasks.get(self.users[actor], task["id"]), task)

    def test_members_create_inside_project_but_strangers_cannot(self):
        project = self.project(members=(2, 4))
        for actor in (1, 2, 4, 6):
            self.assertEqual(self.task(creator=actor, project_id=project["id"])["project_id"], project["id"])
        for actor in (3, 5):
            self.error(404, self.task, creator=actor, project_id=project["id"])

    def test_target_access_is_rechecked_after_assignee_lookup(self):
        project = self.project(members=(2,))
        def lookup(user_id):
            self.projects.mutate(self.users[1], project["id"], {"version": project["version"]}, "member_remove", 2)
            return self.users[user_id]
        self.tasks.user_lookup = lookup
        self.error(404, self.task, project_id=project["id"])
        self.assertEqual(self.ids(6, scope="all"), set())

    def test_task_moves_between_accessible_projects_and_none_with_activity(self):
        first = self.project(members=(2, 3))
        second = self.project(owner=5, members=(3,))
        task = self.task()
        for target in (first["id"], second["id"], None):
            task = self.tasks.mutate(self.users[3], task["id"], {"version": task["version"], "project_id": target})
            self.assertEqual(task["project_id"], target)
        events = self.tasks.activity(self.users[2], task["id"])["items"]
        self.assertEqual([row["event_type"] for row in events], ["created"] + ["project_changed"] * 3)

    def test_forbidden_move_and_mixed_patch_are_atomic(self):
        task = self.task()
        target = self.project(owner=5)
        self.error(404, self.tasks.mutate, self.users[2], task["id"], {"version": 1, "title": "No", "project_id": target["id"]})
        self.assertEqual(self.tasks.get(self.users[2], task["id"]), task)
        self.assertEqual(len(self.tasks.activity(self.users[2], task["id"])["items"]), 1)

    def test_move_history_failure_rolls_back_task_and_prior_content_event(self):
        project = self.project(members=(2,))
        task = self.task()
        original = TaskSession.add_activity
        def fail_move(session, updated, actor, event, timestamp, payload):
            if event == "project_changed":
                raise sqlite3.OperationalError("history")
            return original(session, updated, actor, event, timestamp, payload)
        with mock.patch.object(TaskSession, "add_activity", fail_move):
            with self.assertRaises(sqlite3.OperationalError):
                self.tasks.mutate(self.users[2], task["id"], {"version": 1, "title": "No", "project_id": project["id"]})
        self.assertEqual(self.tasks.get(self.users[2], task["id"]), task)
        self.assertEqual(len(self.tasks.activity(self.users[2], task["id"])["items"]), 1)

    def test_archived_project_rejects_new_work_but_existing_tasks_remain_editable(self):
        archived = self.project(members=(2, 3))
        active = self.project(members=(2, 3))
        existing = self.task(project_id=archived["id"])
        outside = self.task()
        archived = self.mutate_project(archived, "archive")
        for actor in (1, 2, 6):
            self.error(422, self.task, creator=actor, project_id=archived["id"])
        self.error(422, self.tasks.mutate, self.users[2], outside["id"],
                   {"version": 1, "title": "No partial update", "project_id": archived["id"]})
        self.assertEqual(self.tasks.get(self.users[2], outside["id"]), outside)
        self.assertEqual(len(self.tasks.activity(self.users[2], outside["id"])["items"]), 1)
        # Echoing the unchanged project_id must not block ordinary editing.
        existing = self.tasks.mutate(self.users[3], existing["id"],
                                     {"version": 1, "project_id": archived["id"], "description": "Still editable"})
        existing = self.tasks.mutate(self.users[3], existing["id"], {"version": 2, "status": "done"}, "status")
        self.assertEqual(existing["status"], "done")
        existing = self.tasks.mutate(self.users[3], existing["id"], {"version": 3, "project_id": active["id"]})
        self.assertEqual(existing["project_id"], active["id"])
        self.mutate_project(archived, "restore")
        another = self.task(project_id=archived["id"])
        archived = self.projects.get(self.users[1], archived["id"])
        self.mutate_project(archived, "archive")
        another = self.tasks.mutate(self.users[2], another["id"], {"version": 1, "project_id": None})
        self.assertIsNone(another["project_id"])

    def test_archive_race_is_rechecked_inside_task_write_transaction(self):
        project = self.project(members=(2,))
        def lookup(user_id):
            self.mutate_project(project, "archive")
            return self.users[user_id]
        self.tasks.user_lookup = lookup
        self.error(422, self.task, project_id=project["id"])
        self.assertEqual(self.ids(6, scope="all"), set())


class TaskViewsTest(ProjectsFixture):
    def test_default_active_lists_exclude_done_but_explicit_archive_and_status_work(self):
        project = self.project(members=(2, 4))
        task = self.task(project_id=project["id"])
        for actor, scope in ((2, "created"), (3, "my"), (4, "team"), (1, "all"), (6, "all")):
            self.assertIn(task["id"], self.ids(actor, scope=scope))
        self.tasks.mutate(self.users[3], task["id"], {"version": 1, "status": "done"}, "status")
        for actor, scope in ((2, "created"), (3, "my"), (4, "team"), (1, "all"), (6, "all")):
            self.assertEqual(self.ids(actor, scope=scope, search="Task"), set())
            self.assertEqual(self.ids(actor, scope=scope, view="archive"), {task["id"]})
            self.assertEqual(self.ids(actor, scope=scope, status="done"), {task["id"]})

    def test_dashboard_is_personal_and_filtered_summary_keeps_explicit_scope(self):
        project = self.project(members=(2, 4, 6))
        # Visible project work is not any viewer's personal workload.
        self.task(project_id=project["id"], status="in_progress", deadline_date="2026-09-25")
        self.task(project_id=project["id"], deadline_date="2026-09-26")
        self.task(creator=5, assigned_to=5, status="in_progress", deadline_date="2026-09-25")
        for actor in (1, 4, 6):
            self.task(assigned_to=actor, status="in_progress", deadline_date="2026-09-26")
            self.task(assigned_to=actor, deadline_date="2026-09-25")
            self.task(assigned_to=actor, status="done", deadline_date="2026-09-25")
            self.task(creator=actor, assigned_to=8, status="waiting")
            deleted = self.task(creator=actor, assigned_to=8)
            self.tasks.mutate(self.users[actor], deleted["id"], {"version": 1}, "delete")
            dashboard = self.tasks.list(self.users[actor], summary=True)
            self.assertEqual({key: dashboard[key] for key in ("total", "done", "in_progress", "today", "overdue", "delegated_waiting", "inbox")},
                             dict(total=3, done=1, in_progress=1, today=1, overdue=1, delegated_waiting=1, inbox=None))
            explicit = self.tasks.list(self.users[actor], {"scope": "my"}, summary=True)
            self.assertEqual(explicit["delegated_waiting"], 0)
            filtered = self.tasks.list(self.users[actor], {"project_id": str(project["id"])}, summary=True)
            self.assertEqual(filtered["total"], 0)  # Default B scope=my, not all.
            team = self.tasks.list(self.users[actor], {"scope": "team", "project_id": str(project["id"])}, summary=True)
            self.assertEqual((team["total"], team["in_progress"], team["today"], team["overdue"]), (2, 1, 1, 1))

    def test_delegated_waiting_is_not_task_waiting_status(self):
        delegated = [self.task(status=status) for status in ("new", "in_progress", "waiting")]
        self.task(assigned_to=2, status="waiting")
        self.task(creator=3, assigned_to=2, status="waiting")
        self.task(status="done")
        self.assertEqual(self.ids(2, view="delegated_waiting"), {row["id"] for row in delegated})
        summary = self.tasks.list(self.users[2], summary=True)
        self.assertEqual((summary["delegated_waiting"], summary["waiting"]), (3, 2))
        self.tasks.mutate(self.users[3], delegated[0]["id"], {"version": 1, "status": "done"}, "status")
        self.assertEqual(len(self.ids(2, view="delegated_waiting")), 2)

    def test_today_and_overdue_cross_business_midnight(self):
        current = self.task(deadline_date="2026-09-26")
        next_day = self.task(deadline_date="2026-09-27")
        self.task(status="done", deadline_date="2026-09-26")
        self.task(deadline_date=None)
        self.tasks.today = business_today
        for instant, today_ids, overdue_ids in (
                (datetime(2026, 9, 26, 20, 59, 59, tzinfo=timezone.utc), {current["id"]}, set()),
                (datetime(2026, 9, 26, 21, 0, 0, tzinfo=timezone.utc), {next_day["id"]}, {current["id"]})):
            with mock.patch("app.tasks.domain.datetime") as clock:
                clock.now.side_effect = lambda zone: instant.astimezone(zone)
                self.assertEqual(self.ids(3, view="today"), today_ids)
                self.assertEqual(self.ids(3, today="true"), today_ids)
                self.assertEqual(self.ids(3, view="overdue"), overdue_ids)
                summary = self.tasks.list(self.users[3], summary=True)
                self.assertEqual((summary["today"], summary["overdue"]), (len(today_ids), len(overdue_ids)))

    def test_archive_filters_and_completion_period_use_business_date(self):
        project = self.project(members=(2, 4))
        self.now = "2026-09-26T20:59:59+00:00"
        first = self.task(project_id=project["id"], status="done", title="needle")
        self.now = "2026-09-26T21:00:00+00:00"
        second = self.task(project_id=project["id"], status="done", title="needle")
        self.task(creator=5, assigned_to=5, status="done", title="needle")
        private = self.task(status="done")
        self.task(project_id=project["id"])
        filters = dict(scope="all", view="archive", project_id=str(project["id"]), assigned_to="3", created_by="2", search="needle")
        self.assertEqual(self.ids(4, **filters), {first["id"], second["id"]})
        self.assertEqual(self.ids(4, date_from="2026-09-26", date_to="2026-09-26", **filters), {first["id"]})
        self.assertEqual(self.ids(4, date_from="2026-09-27", **filters), {second["id"]})
        self.assertEqual(self.ids(3, view="archive", project="none"), {private["id"]})
        self.assertEqual(self.ids(4, view="archive", scope="all", created_by="5"), set())

    def test_soft_deleted_excluded_from_every_view_summary_and_counters(self):
        project = self.project(members=(2, 4))
        for status in ("new", "done"):
            task = self.task(project_id=project["id"], status=status, deadline_date="2026-09-25")
            self.tasks.mutate(self.users[2], task["id"], {"version": 1}, "delete")
        for scope in ("my", "created", "team", "all"):
            for view in ("today", "overdue", "delegated_waiting", "archive"):
                self.assertEqual(self.ids(2, scope=scope, view=view, search="Task"), set())
        self.assertEqual(self.tasks.list(self.users[4], summary=True)["total"], 0)
        self.assertEqual(self.projects.details(self.users[4], project["id"], "summary"), {"open": 0, "in_progress": 0, "overdue": 0})

    def test_new_filters_reject_invalid_inputs(self):
        for options in ({"project_id": "-1"}, {"project_id": "1 OR 1=1"}, {"project": "all"},
                        {"project": "none", "project_id": "1"}, {"view": "waiting"}, {"assigned_to": "0"},
                        {"date_from": "2026-01-01"}, {"view": "archive", "date_to": "2026-02-30"},
                        {"view": "archive", "date_from": "2026-09-27", "date_to": "2026-09-26"},
                        {"view": "archive", "date_to": "9999-12-31"}):
            self.error(422, self.tasks.list, self.users[2], options)


class ProjectsStorageTest(ProjectsFixture):
    def make_v2(self, filename="v2", invalid=False):
        path = Path(self.temp.name) / filename / "tasks-module.db"
        path.parent.mkdir()
        connection = sqlite3.connect(str(path))
        connection.execute(LEDGER_DDL)
        for sql in V2_CORE_DDL:
            connection.execute(sql)
        connection.executemany("INSERT INTO tasks_module_migrations VALUES(?,?,'now','fixture')",
                               ((1, "tasks-module-foundation-v1"), (2, "tasks-module-core-v2")))
        connection.execute("INSERT INTO tasks(id,title,created_by,assigned_to,created_at,updated_at) VALUES(42,'v2',2,3,'now','now')")
        connection.execute("INSERT INTO task_activity VALUES(17,?,2,'created','now',1,'{}')", (999 if invalid else 42,))
        connection.commit()
        connection.close()
        return path

    def test_v2_upgrade_preserves_new_module_records_and_is_idempotent(self):
        path = self.make_v2()
        migrations.migrate_database(path)
        with TasksRepository(path).transaction() as session:
            task = session.get(42)
            self.assertEqual((task["title"], task["version"], task["project_id"]), ("v2", 1, None))
            self.assertEqual(session.activity(42, 50, 0)[0]["id"], 17)
        before = path.read_bytes()
        migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)

    def test_mid_upgrade_error_rolls_back_old_tables_and_ledger(self):
        path = self.make_v2()
        before = path.read_bytes()
        with mock.patch.object(migrations, "CORE_DDL", CORE_DDL + ("INVALID SQL",)):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)
        connection = sqlite3.connect(str(path))
        try:
            validate_connection(connection, version=2)
        finally:
            connection.close()

    def test_orphan_activity_rejects_upgrade_without_data_loss(self):
        path = self.make_v2(invalid=True)
        before = path.read_bytes()
        with self.assertRaises(sqlite3.IntegrityError):
            migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)

    def test_project_schema_drift_is_rejected_without_repair(self):
        for index, before, after in ((2, "name TEXT NOT NULL", "name TEXT"),
                                    (2, "owner_id INTEGER", "owner_id TEXT"),
                                    (2, "DEFAULT 1", "DEFAULT 2"),
                                    (2, "CHECK(version>0)", ""),
                                    (3, "PRIMARY KEY(project_id,user_id)", "UNIQUE(project_id,user_id)"),
                                    (3, " REFERENCES task_projects(id)", ""),
                                    (4, "payload TEXT NOT NULL", "payload TEXT")):
            with self.subTest(defect=(index, before)):
                path = Path(self.temp.name) / str(index) / str(len(list(Path(self.temp.name).rglob('*.db')))) / 'tasks-module.db'
                path.parent.mkdir(parents=True)
                connection = sqlite3.connect(str(path))
                connection.execute(LEDGER_DDL)
                for position, sql in enumerate(CORE_DDL):
                    connection.execute(sql.replace(before, after) if position == index else sql)
                connection.executemany("INSERT INTO tasks_module_migrations VALUES(?,?,'now','fixture')",
                                       ((1, 'tasks-module-foundation-v1'), (2, 'tasks-module-core-v2'), (3, 'tasks-module-projects-v3')))
                connection.commit()
                connection.close()
                original = path.read_bytes()
                with self.assertRaises(ValueError):
                    TasksRepository(path).status()
                self.assertEqual(path.read_bytes(), original)

    def test_projects_and_tasks_open_only_module_storage(self):
        original = sqlite3.connect
        opened = []
        def guard(path, *args, **kwargs):
            self.assertIn('tasks-module.db?mode=', str(path))
            opened.append(str(path))
            return original(path, *args, **kwargs)
        with mock.patch('sqlite3.connect', side_effect=guard):
            project = self.project(members=(2, 4))
            task = self.task(project_id=project['id'])
            self.ids(4, scope='team', search='Task')
            self.projects.list(self.users[4])
            self.projects.details(self.users[4], project['id'], 'summary')
            self.projects.details(self.users[4], project['id'], 'activity')
            self.tasks.mutate(self.users[2], task['id'], {'version': 1, 'project_id': None})
            project = self.mutate_project(project, 'archive')
            self.mutate_project(project, 'restore')
        self.assertTrue(opened)

    def test_project_lists_have_constant_query_count_and_indexed_joins(self):
        counts, plans = [], []
        for size in (1, 20):
            while self.projects.list(self.users[1])['total'] < size:
                project = self.project(members=(2, 4))
                self.task(project_id=project['id'])
            with self.repo.transaction() as session:
                real = session.connection
                session.connection = mock.Mock(wraps=real)
                result = session.projects(permissions.project_visibility(self.users[4]),
                                          dict(archived=False, limit=50, offset=0), '2026-09-26')
                self.assertEqual(result['total'], size)
                calls = session.connection.execute.call_args_list
                counts.append(len(calls))
                plans = [list(row) for call in calls for row in real.execute('EXPLAIN QUERY PLAN ' + call[0][0], call[0][1])]
        self.assertEqual(counts, [2, 2])
        self.assertIn('tasks_project_active', str(plans))
        self.assertIn('task_project_members', str(plans))
        EVIDENCE.update(project_list_query_counts=counts, project_list_plans=plans, sqlite=sqlite3.sqlite_version)


class ProjectsApiTest(ProjectsFixture):
    def setUp(self):
        super().setUp()
        self.actor = self.users[1]
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, TASKS_MODULE_ENABLED=True, TASKS_MODULE_DATABASE=str(self.path))
        register_tasks_module(self.app, self.path.parent, lambda: self.actor, self.users.get,
                              lambda: request.headers.get('X-CSRF-Token') == 'test')
        self.client = self.app.test_client()
        self.headers = {'X-CSRF-Token': 'test'}

    def test_summary_pagination_preserves_dashboard_and_filters_stay_explicit(self):
        project = self.project(members=(2,))
        self.task(creator=1, assigned_to=3, project_id=project["id"], status="waiting")
        self.task(assigned_to=1, status="in_progress")
        self.task(assigned_to=1, status="waiting")
        url = BASE + "/tasks/summary"
        baseline = self.client.get(url).get_json()["data"]
        self.assertEqual((baseline["total"], baseline["delegated_waiting"]), (2, 1))
        for query in ("?limit=50", "?offset=0", "?limit=50&offset=0", "?limit=1&offset=100"):
            response = self.client.get(url + query)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["data"], baseline)
        for query, total in (("?scope=my", 2), ("?status=waiting", 1),
                             ("?project_id=" + str(project["id"]), 0)):
            response = self.client.get(url + query + "&limit=50&offset=0")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()["data"]
            self.assertEqual((data["total"], data["delegated_waiting"]), (total, 0))
        for query in ("?limit=101", "?offset=-1", "?unknown=1"):
            self.assertEqual(self.client.get(url + query).status_code, 422)

    def test_full_project_api_and_membership_concurrency(self):
        response = self.client.post(BASE + '/projects', json={'name': 'API'}, headers=self.headers)
        self.assertEqual(response.status_code, 201)
        project = response.get_json()['data']
        url = BASE + '/projects/' + str(project['id'])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.get(BASE + '/projects').get_json()['data']['total'], 1)
        calls = [('patch', url, {'name': 'New', 'version': 1}),
                 ('post', url + '/members', {'user_id': 4, 'version': 2}),
                 ('delete', url + '/members/4', {'version': 3}),
                 ('post', url + '/archive', {'version': 4}),
                 ('post', url + '/restore', {'version': 5})]
        for method, path, payload in calls:
            response = getattr(self.client, method)(path, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(response.get_json()['data']['version'], payload['version'] + 1)
        for suffix in ('/members', '/summary', '/activity'):
            self.assertEqual(self.client.get(url + suffix).status_code, 200)
        response = self.client.post(url + '/archive', json={'version': 1}, headers=self.headers)
        self.assertEqual((response.status_code, response.get_json()['code']), (409, 'VERSION_CONFLICT'))

    def test_project_idor_all_routes(self):
        project = self.project()
        self.actor = self.users[5]
        url = BASE + '/projects/' + str(project['id'])
        for method, path, payload in [('get', url, None), ('patch', url, {'version': 1, 'name': 'No'}),
                                      ('post', url + '/archive', {'version': 1}), ('post', url + '/restore', {'version': 1}),
                                      ('post', url + '/members', {'version': 1, 'user_id': 4}),
                                      ('delete', url + '/members/4', {'version': 1}),
                                      ('get', url + '/members', None), ('get', url + '/summary', None), ('get', url + '/activity', None)]:
            response = getattr(self.client, method)(path, json=payload, headers=self.headers)
            self.assertEqual((response.status_code, response.get_json()['code']), (404, 'PROJECT_NOT_FOUND'))

    def test_task_project_visibility_and_views_over_http(self):
        project = self.project(members=(2, 4))
        self.actor = self.users[2]
        response = self.client.post(BASE + '/tasks', json={'title': 'HTTP', 'assigned_to': 3, 'project_id': project['id']}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        task = response.get_json()['data']
        self.actor = self.users[4]
        for scope in ('team', 'all'):
            self.assertEqual(self.client.get(BASE + '/tasks?scope=' + scope).get_json()['data']['total'], 1)
        self.assertEqual(self.client.get(BASE + '/tasks/' + str(task['id'])).status_code, 200)
        response = self.client.patch(BASE + '/tasks/' + str(task['id']), json={'version': 1, 'title': 'No'}, headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.actor = self.users[5]
        self.assertEqual(self.client.get(BASE + '/tasks/' + str(task['id'])).status_code, 404)
        self.assertEqual(self.client.get(BASE + '/tasks?scope=all&view=archive&search=HTTP').get_json()['data']['total'], 0)

    def test_projects_flag_off_and_auth_csrf_fail_before_storage(self):
        project = self.project()
        with mock.patch.object(TasksRepository, 'transaction', side_effect=AssertionError('storage')) as transaction:
            self.assertEqual(self.client.post(BASE + '/projects', json={'name': 'No'}).status_code, 403)
            self.actor = None
            self.assertEqual(self.client.get(BASE + '/projects').status_code, 401)
            self.app.config['TASKS_MODULE_ENABLED'] = False
            for path in ('/projects', '/projects/' + str(project['id']), '/projects/1/summary'):
                self.assertEqual(self.client.get(BASE + path).status_code, 503)
        transaction.assert_not_called()

    def test_project_storage_errors_are_safe_and_never_migrate(self):
        with mock.patch('app.tasks.migrations.migrate_database', side_effect=AssertionError('HTTP migration')) as migrate:
            with mock.patch.object(ProjectQueries, 'add_project_activity', side_effect=sqlite3.OperationalError('secret SQL /path')):
                response = self.client.post(BASE + '/projects', json={'name': 'Failure'}, headers=self.headers)
            self.assertEqual((response.status_code, response.get_json()['code']), (503, 'TASKS_MODULE_UNAVAILABLE'))
            self.assertNotIn('secret', response.get_data(as_text=True))
            self.assertEqual(self.client.get(BASE + '/projects').get_json()['data']['total'], 0)
        migrate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
