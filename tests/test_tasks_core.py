"""Stage B domain, policy, atomicity, concurrency and database isolation."""

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from app.tasks import migrations, permissions
from app.tasks.domain import TaskError, business_today
from app.tasks.repository import FOUNDATION_SIGNATURE, TaskSession, TasksRepository
from app.tasks.services import TasksService


class TasksCoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "tasks-module.db"
        migrations.migrate_database(self.path)
        self.creator = {"id": 1, "role": "employee", "active": 1}
        self.assignee = {"id": 2, "role": "employee", "active": 1}
        self.stranger = {"id": 3, "role": "employee", "active": 1}
        self.admin = {"id": 4, "role": "admin", "active": 1}
        self.users = {user["id"]: user for user in (self.creator, self.assignee, self.stranger, self.admin)}
        self.users[5] = {"id": 5, "active": 0}
        self.repository = TasksRepository(self.path)
        self.service = TasksService(self.repository, self.users.get,
                                    now=lambda: "2026-09-26T12:00:00+00:00", today=lambda: "2026-09-26")

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **values):
        payload = {"title": "  Новая задача  ", "assigned_to": 2}
        payload.update(values)
        return self.service.create(self.creator, payload)

    def error(self, expected_status, action, *args, **kwargs):
        with self.assertRaises(TaskError) as raised:
            action(*args, **kwargs)
        self.assertEqual(raised.exception.status, expected_status)
        return raised.exception

    def update(self, task, user=None, operation="patch", **values):
        values["version"] = task["version"]
        return self.service.mutate(user or self.creator, task["id"], values, operation)

    def test_creator_creates_normal_task_and_defaults(self):
        task = self.create()
        self.assertEqual((task["created_by"], task["assigned_to"], task["version"]), (1, 2, 1))
        self.assertEqual((task["task_type"], task["title"], task["priority"], task["status"]),
                         ("normal", "Новая задача", "normal", "new"))
        for field in ("deadline_date", "completed_at", "deleted_at", "related_entity_type", "related_entity_id", "related_entity_label"):
            self.assertIsNone(task[field])
        self.assertEqual(self.service.create(self.creator, {"title": "Mine"})["assigned_to"], 1)

    def test_created_by_and_server_fields_cannot_be_spoofed(self):
        for field in ("created_by", "id", "version", "created_at", "updated_at", "completed_at", "deleted_at"):
            with self.subTest(field=field):
                self.error(422, self.create, **{field: 123})
        self.assertEqual(self.service.list(self.admin, {"scope": "all"})["total"], 0)

    def test_assignee_must_exist_and_be_active(self):
        for user_id in (5, 99, None, 0, -1, True, "2", 2.0, 9223372036854775808):
            with self.subTest(user_id=user_id):
                self.error(422, self.create, assigned_to=user_id)
        task = self.create()
        for user_id in (5, 99):
            self.error(422, self.update, task, assigned_to=user_id)
        self.assertEqual(self.service.get(self.creator, task["id"]), task)

    def test_lookup_failure_leaves_task_database_unchanged(self):
        task = self.create()
        before = self.path.read_bytes()
        self.service.user_lookup = mock.Mock(side_effect=RuntimeError("secret external error"))
        self.error(503, self.create)
        self.error(503, self.update, task, assigned_to=3)
        self.assertEqual(self.path.read_bytes(), before)

    def test_lookup_runs_outside_tasks_transaction_and_rights_are_rechecked(self):
        task = self.create()
        events = []
        original = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(lambda sql: events.append(sql))
            return connection

        def lookup(user_id):
            events.append("USER_LOOKUP")
            # A competing reassignment changes the version after the initial
            # authorization, before the second writer acquires its transaction.
            self.service.user_lookup = self.users.get
            self.update(task, self.admin, assigned_to=4)
            return self.users.get(user_id)

        self.service.user_lookup = lookup
        with mock.patch("sqlite3.connect", side_effect=connect):
            self.error(409, self.update, task, assigned_to=3)
        at_lookup = events.index("USER_LOOKUP")
        self.assertEqual(events[at_lookup - 1], "COMMIT")
        self.assertEqual(self.service.get(self.creator, task["id"])["assigned_to"], 4)

    def test_view_creator_assignee_admin_and_deny_stranger(self):
        task = self.create()
        for user in (self.creator, self.assignee, self.admin):
            self.assertEqual(self.service.get(user, task["id"]), task)
        self.error(404, self.service.get, self.stranger, task["id"])
        self.error(404, self.service.get, self.stranger, 987654)

    def test_idor_all_service_operations_and_history(self):
        task = self.create()
        for operation, body in (("patch", {"title": "Leak"}), ("status", {"status": "done"}),
                                ("delete", {}), ("restore", {})):
            self.error(404, self.update, task, self.stranger, operation, **body)
        self.error(404, self.service.activity, self.stranger, task["id"])

    def test_scope_search_summary_do_not_reveal_other_tasks(self):
        self.create(title="Secret", description="hidden phrase")
        for options in ({}, {"scope": "created"}, {"search": "Secret"}, {"search": "hidden phrase"}):
            self.assertEqual(self.service.list(self.stranger, options)["total"], 0)
            self.assertEqual(self.service.list(self.stranger, options, summary=True)["total"], 0)
        self.assertEqual(self.service.list(self.stranger, {"scope": "all"})["total"], 0)
        self.assertEqual(self.service.list(self.stranger, {"scope": "all"}, summary=True)["total"], 0)
        self.assertEqual(self.service.list(self.creator, {"scope": "created"})["total"], 1)
        self.assertEqual(self.service.list(self.creator)["total"], 0)
        self.assertEqual(self.service.list(self.assignee)["total"], 1)
        self.assertEqual(self.service.list(self.admin, {"scope": "all"})["total"], 1)

    def test_creator_and_assignee_edit_work_content_and_status(self):
        task = self.create()
        for user in (self.creator, self.assignee):
            task = self.update(task, user, title="Title " + str(user["id"]),
                               description="Text " + str(user["id"]), priority="high", deadline_date="2026-10-01")
            task = self.update(task, user, "status", status="done")
            self.assertIsNotNone(task["completed_at"])
            task = self.update(task, user, "status", status="new")
            self.assertIsNone(task["completed_at"])

    def test_assignee_cannot_reassign_delete_or_restore(self):
        task = self.create()
        with mock.patch.object(self.service, "_assignee") as lookup:
            self.error(403, self.update, task, self.assignee, assigned_to=3)
        lookup.assert_not_called()
        self.error(403, self.update, task, self.assignee, "delete")
        task = self.update(task, operation="delete")
        self.error(403, self.update, task, self.assignee, "restore")

    def test_creator_reassigns_and_former_assignee_loses_all_access(self):
        task = self.update(self.create(), assigned_to=3)
        self.assertEqual(self.service.get(self.stranger, task["id"]), task)
        self.error(404, self.service.get, self.assignee, task["id"])
        self.error(404, self.service.activity, self.assignee, task["id"])
        self.error(404, self.update, task, self.assignee, title="Rejected")
        self.assertEqual(self.service.list(self.assignee)["total"], 0)

    def test_creator_equals_assignee_keeps_creator_permissions(self):
        task = self.create(assigned_to=1)
        task = self.update(task, assigned_to=2)
        task = self.update(task, operation="delete")
        self.assertIsNone(self.update(task, operation="restore")["deleted_at"])

    def test_admin_manages_another_users_task(self):
        task = self.create()
        task = self.update(task, self.admin, title="Admin", assigned_to=3)
        task = self.update(task, self.admin, "status", status="done")
        task = self.update(task, self.admin, "delete")
        self.assertIsNone(self.update(task, self.admin, "restore")["deleted_at"])

    def test_version_increments_for_each_mutation_and_stale_conflicts(self):
        task = self.create()
        steps = (("patch", {"title": "V2"}), ("status", {"status": "done"}), ("delete", {}), ("restore", {}))
        for operation, values in steps:
            previous = task
            task = self.update(task, operation=operation, **values)
            self.assertEqual(task["version"], previous["version"] + 1)
            error = self.error(409, self.update, previous, operation=operation, **values)
            self.assertEqual(error.code, "VERSION_CONFLICT")

    def test_concurrent_connections_never_silently_overwrite(self):
        task = self.create()
        barrier = threading.Barrier(2)

        def change(title):
            barrier.wait(timeout=5)
            try:
                result = self.update(task, title=title)
                return 200, result["title"]
            except TaskError as error:
                return error.status, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ("Winner A", "Winner B")))
        self.assertEqual(sorted(status for status, title in results), [200, 409])
        winner = next(title for status, title in results if status == 200)
        saved = self.service.get(self.creator, task["id"])
        self.assertEqual((saved["title"], saved["version"]), (winner, 2))
        self.assertEqual(len(self.service.activity(self.creator, task["id"])["items"]), 2)

    def test_repository_compare_and_swap_protects_against_stale_version(self):
        task = self.create()
        with self.repository.transaction(write=True) as session:
            self.error(409, session.update, task["id"], 9, {"title": "Lost"})
        self.assertEqual(self.service.get(self.creator, task["id"]), task)

    def test_done_creation_and_reopening_completion_timestamp(self):
        task = self.create(status="done")
        self.assertEqual(task["completed_at"], self.service.now())
        task = self.update(task, description="Keep completion")
        self.assertIsNotNone(task["completed_at"])
        for status in ("waiting", "in_progress", "new"):
            task = self.update(task, status=status)
            self.assertIsNone(task["completed_at"])
            task = self.update(task, status="done")
            self.assertIsNotNone(task["completed_at"])

    def test_overdue_today_date_only_and_null_deadlines(self):
        past = self.create(deadline_date="2026-09-25")
        done = self.create(deadline_date="2026-09-25", status="done")
        today = self.create(deadline_date="2026-09-26")
        self.create(deadline_date="2026-09-27")
        self.create()
        for filters, expected in (({"overdue": "true"}, [past["id"]]), ({"today": "1"}, [today["id"]]),
                                  ({"deadline_date": "2026-09-25"}, [past["id"]])):
            self.assertEqual([task["id"] for task in self.service.list(self.assignee, filters)["items"]], expected)
        self.assertEqual(self.service.list(self.assignee, {"overdue": "false"})["total"], 3)
        summary = self.service.list(self.assignee, summary=True)
        self.assertEqual((summary["total"], summary["overdue"], summary["today"], summary["done"]), (5, 1, 1, 1))

    def test_unicode_search_literal_wildcards_filters_and_paging(self):
        a = self.create(title="ПОЗВОНИТЬ клиенту", description="Скидка 10%_", priority="high", status="waiting")
        self.create(title="Second", description="позвонить завтра")
        self.assertEqual(self.service.list(self.assignee, {"search": "позвонить"})["total"], 2)
        result = self.service.list(self.assignee, {"search": "%_", "status": "waiting", "priority": "high"})
        self.assertEqual([task["id"] for task in result["items"]], [a["id"]])
        self.assertEqual(self.service.list(self.assignee, {"limit": "1", "offset": "1"})["total"], 2)
        self.assertEqual(len(self.service.list(self.assignee, {"limit": "1", "offset": "1"})["items"]), 1)
        self.assertEqual(self.service.list(self.assignee, {"search": "' OR 1=1 --"})["total"], 0)

    def test_soft_delete_hides_search_summary_and_restore_returns_task(self):
        task = self.create(title="Secret")
        task = self.update(task, operation="delete")
        self.assertIsNotNone(task["deleted_at"])
        self.assertEqual(self.service.list(self.assignee)["total"], 0)
        self.assertEqual(self.service.list(self.creator, {"scope": "created", "search": "Secret"})["total"], 0)
        self.assertEqual(self.service.list(self.admin, {"scope": "all"}, summary=True)["total"], 0)
        self.assertTrue(permissions.can_view(self.assignee, task))
        self.assertFalse(permissions.can_edit(self.assignee, task))
        self.error(403, self.update, task, title="Deleted")
        with self.repository.transaction() as session:
            self.assertIsNotNone(session.get(task["id"]))
        task = self.update(task, operation="restore")
        self.assertEqual(self.service.list(self.assignee)["total"], 1)
        self.assertIsNone(task["deleted_at"])

    def test_activity_has_actor_versions_timestamps_and_diffs(self):
        task = self.create()
        task = self.update(task, title="Changed", description="Content", assigned_to=3, priority="high",
                           deadline_date="2026-10-01", status="done")
        task = self.update(task, status="waiting")
        task = self.update(task, operation="delete")
        self.update(task, operation="restore")
        events = self.service.activity(self.creator, task["id"])["items"]
        self.assertEqual([event["event_type"] for event in events],
                         ["created", "content_changed", "reassigned", "deadline_changed", "priority_changed",
                          "status_changed", "completed", "status_changed", "reopened", "deleted", "restored"])
        self.assertEqual(events[1]["payload"]["title"], {"before": "Новая задача", "after": "Changed"})
        for event in events:
            self.assertEqual((event["actor"], event["task_id"], event["timestamp"]), (1, task["id"], self.service.now()))
        self.assertEqual(events[-1]["task_version"], 5)

    def test_activity_failure_rolls_back_create_and_every_mutation(self):
        task = self.create()
        steps = (("patch", {"title": "No"}), ("status", {"status": "done"}), ("delete", {}))
        for operation, values in steps:
            before_history = self.service.activity(self.creator, task["id"])
            with mock.patch.object(TaskSession, "add_activity", side_effect=sqlite3.OperationalError("history unavailable")):
                with self.assertRaises(sqlite3.OperationalError):
                    self.update(task, operation=operation, **values)
            self.assertEqual(self.service.get(self.creator, task["id"]), task)
            self.assertEqual(self.service.activity(self.creator, task["id"]), before_history)
        task = self.update(task, operation="delete")
        with mock.patch.object(TaskSession, "add_activity", side_effect=sqlite3.OperationalError("history unavailable")):
            with self.assertRaises(sqlite3.OperationalError):
                self.update(task, operation="restore")
            with self.assertRaises(sqlite3.OperationalError):
                self.create()
        self.assertEqual(self.service.get(self.creator, task["id"]), task)
        with self.repository.transaction() as session:
            self.assertEqual(session.connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)

    def test_failure_after_first_history_event_rolls_back_all_events(self):
        task = self.create()
        original = TaskSession.add_activity

        def fail_completion(session, task, actor, event, timestamp, payload):
            if event == "completed":
                raise sqlite3.OperationalError("second event failure")
            return original(session, task, actor, event, timestamp, payload)

        with mock.patch.object(TaskSession, "add_activity", fail_completion):
            with self.assertRaises(sqlite3.OperationalError):
                self.update(task, title="Rolled back", status="done")
        self.assertEqual(self.service.get(self.creator, task["id"]), task)
        self.assertEqual(len(self.service.activity(self.creator, task["id"])["items"]), 1)

    def test_validation_matrix(self):
        for values in ({"title": " \t\n"}, {"title": None}, {"title": "x" * 501}, {"description": None},
                       {"status": "overdue"}, {"priority": "urgent"}, {"task_type": "micro"},
                       {"deadline_date": "2026-02-29"}, {"deadline_date": "2026-1-01"},
                       {"deadline_date": "2026-09-26T23:59:59"}, {"deadline_date": "0000-01-01"},
                       {"related_entity_id": {}}, {"title": "null\x00byte"}):
            with self.subTest(values=values):
                self.error(422, self.create, **values)
        self.assertEqual(self.create(deadline_date="2028-02-29")["deadline_date"], "2028-02-29")
        task = self.create()
        for version in (None, "1", True, 0, -1):
            self.error(422, self.service.mutate, self.creator, task["id"], {"title": "x", "version": version})
        self.error(422, self.update, task, title=task["title"])
        self.error(422, self.update, task)

    def test_invalid_filters_and_no_team_scope(self):
        for options in ({"scope": "unknown"}, {"assigned_to": "bad"}, {"created_by": "bad"}, {"status": "overdue"},
                        {"today": "yes"}, {"limit": "101"}, {"offset": "-1"}, {"search": "x" * 501},
                        {"deadline_date": "wrong"}, {"deleted": "true"}):
            self.error(422, self.service.list, self.creator, options)

    def test_unauthenticated_and_inactive_actors_are_rejected(self):
        for actor in (None, {}, {"id": True}, {"id": 1, "active": 0}):
            self.error(401, self.service.create, actor, {"title": "Denied"})
            self.error(401, self.service.list, actor)
            self.error(401, self.service.get, actor, 1)

    def test_tasks_crud_never_opens_legacy_catalog_or_auth_storage(self):
        original = sqlite3.connect
        opened = []

        def guard(database, *args, **kwargs):
            opened.append(str(database))
            if "tasks-module.db" not in str(database):
                raise AssertionError("Tasks opened an external database: " + str(database))
            return original(database, *args, **kwargs)

        with mock.patch("sqlite3.connect", side_effect=guard):
            task = self.create(related_entity_type="order", related_entity_id="never-resolve", related_entity_label="Plain text")
            task = self.update(task, title="Independent")
            self.service.get(self.creator, task["id"])
            self.service.list(self.assignee)
            self.service.list(self.assignee, summary=True)
            self.service.activity(self.creator, task["id"])
            task = self.update(task, operation="delete")
            self.update(task, operation="restore")
        self.assertTrue(opened)

    def test_repository_forbids_attach_and_missing_storage_is_not_created(self):
        with self.repository.transaction() as session:
            with self.assertRaises(sqlite3.DatabaseError):
                session.connection.execute("ATTACH DATABASE ? AS forbidden", (str(self.path.parent / "catalog.db"),))
        self.assertFalse((self.path.parent / "catalog.db").exists())
        absent = self.path.parent / "absent" / "tasks-module.db"
        with self.assertRaises(sqlite3.OperationalError):
            TasksService(TasksRepository(absent), self.users.get).create(self.creator, {"title": "Missing"})
        self.assertFalse(absent.exists())

    def test_schema_constraints_foreign_key_and_wrong_schema_fail_closed(self):
        task = self.create()
        with self.repository.transaction(write=True) as session:
            for sql in ("UPDATE tasks SET priority='urgent'", "UPDATE tasks SET status='done'",
                        "UPDATE tasks SET task_type='micro'", "UPDATE tasks SET version=0"):
                with self.assertRaises(sqlite3.IntegrityError):
                    session.connection.execute(sql)
            with self.assertRaises(sqlite3.IntegrityError):
                session.connection.execute("INSERT INTO task_activity VALUES(99,999,1,'created','now',1,'{}')")
        self.assertEqual(self.service.get(self.creator, task["id"]), task)
        connection = sqlite3.connect(str(self.path))
        connection.execute("DROP INDEX tasks_assignee_active")
        connection.close()
        with self.assertRaises(ValueError):
            self.repository.status()

    def test_offline_migration_idempotent_preserves_new_core_records(self):
        task = self.create()
        before = self.path.read_bytes()
        migrations.migrate_database(self.path)
        migrations.migrate_database(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.service.get(self.creator, task["id"]), task)

    def test_foundation_upgrade_and_ddl_failure_rollback(self):
        path = self.path.parent / "foundation" / "tasks-module.db"
        path.parent.mkdir()
        connection = sqlite3.connect(str(path))
        connection.execute("CREATE TABLE tasks_module_migrations(version INTEGER PRIMARY KEY,signature TEXT NOT NULL,applied_at TEXT NOT NULL,app_commit TEXT NOT NULL)")
        connection.execute("INSERT INTO tasks_module_migrations VALUES(1,?,'now','stage-a')", (FOUNDATION_SIGNATURE,))
        connection.commit()
        connection.close()
        before = path.read_bytes()
        with mock.patch.object(migrations, "CORE_DDL", migrations.CORE_DDL + ("INVALID SQL",)):
            with self.assertRaises(sqlite3.OperationalError):
                migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(migrations.migrate_database(path)["schema_version"], 4)
        self.assertEqual(TasksService(TasksRepository(path)).list(self.admin, {"scope": "all"})["total"], 0)

    def test_mixed_patch_cannot_partially_save_content_before_forbidden_reassign(self):
        task = self.create()
        history = self.service.activity(self.creator, task["id"])
        self.error(403, self.update, task, user=self.assignee, title="Must not save", assigned_to=3)
        self.assertEqual(self.service.get(self.creator, task["id"]), task)
        self.assertEqual(self.service.activity(self.creator, task["id"]), history)

    def test_reassign_activity_failure_rolls_back_task_and_all_history(self):
        task = self.create()
        history = self.service.activity(self.creator, task["id"])
        original = TaskSession.add_activity

        def fail_reassignment(session, updated, actor, event, timestamp, payload):
            if event == "reassigned":
                raise sqlite3.OperationalError("reassignment history failed")
            return original(session, updated, actor, event, timestamp, payload)

        with mock.patch.object(TaskSession, "add_activity", fail_reassignment):
            with self.assertRaises(sqlite3.OperationalError):
                self.update(task, title="Also roll back", assigned_to=3)
        self.assertEqual(self.service.get(self.creator, task["id"]), task)
        self.assertEqual(self.service.activity(self.creator, task["id"]), history)

    def test_today_and_overdue_at_moscow_midnight(self):
        current = self.create(deadline_date="2026-09-26")
        following = self.create(deadline_date="2026-09-27")
        self.create(deadline_date="2026-09-26", status="done")
        self.create()
        self.service.today = business_today
        for instant, day, today_ids, overdue_ids in (
                (datetime(2026, 9, 26, 20, 59, 59, tzinfo=timezone.utc), "2026-09-26", [current["id"]], []),
                (datetime(2026, 9, 26, 21, 0, 0, tzinfo=timezone.utc), "2026-09-27", [following["id"]], [current["id"]])):
            with self.subTest(instant=instant), mock.patch("app.tasks.domain.datetime") as clock:
                clock.now.side_effect = lambda zone: instant.astimezone(zone)
                self.assertEqual(business_today(), day)
                today = self.service.list(self.assignee, {"today": "true", "status": "new"})
                overdue = self.service.list(self.assignee, {"overdue": "true"})
                self.assertEqual([row["id"] for row in today["items"]], today_ids)
                self.assertEqual([row["id"] for row in overdue["items"]], overdue_ids)
                self.assertEqual(self.service.list(self.assignee, summary=True)["overdue"], len(overdue_ids))

    def test_repeated_delete_and_restore_preserve_versions_and_history(self):
        original = self.create()
        deleted = self.update(original, operation="delete")
        history = self.service.activity(self.creator, original["id"])
        self.error(409, self.update, original, operation="delete")
        self.error(403, self.update, deleted, operation="delete")
        self.assertEqual(self.service.get(self.creator, original["id"]), deleted)
        self.assertEqual(self.service.activity(self.creator, original["id"]), history)
        restored = self.update(deleted, operation="restore")
        history = self.service.activity(self.creator, original["id"])
        self.error(409, self.update, deleted, operation="restore")
        self.error(403, self.update, restored, operation="restore")
        self.assertEqual(self.service.get(self.creator, original["id"]), restored)
        self.assertEqual(self.service.activity(self.creator, original["id"]), history)
        self.assertEqual([event["event_type"] for event in history["items"]], ["created", "deleted", "restored"])
        self.assertEqual(restored["version"], 3)
        self.assertIsNone(restored["deleted_at"])

    def test_invalid_unicode_is_rejected_before_task_or_activity_changes(self):
        task = self.create()
        history = self.service.activity(self.creator, task["id"])
        for field in ("title", "description", "related_entity_type", "related_entity_id", "related_entity_label"):
            for value in ("\ud800", "\udfff", "text\ud800text"):
                with self.subTest(field=field, value=repr(value)):
                    self.error(422, self.create, **{field: value})
                    self.error(422, self.update, task, **{field: value})
        self.error(422, self.service.list, self.assignee, {"search": "\ud800"})
        self.assertEqual(self.service.get(self.creator, task["id"]), task)
        self.assertEqual(self.service.activity(self.creator, task["id"]), history)
        self.assertEqual(self.service.list(self.assignee)["total"], 1)

    def test_valid_unicode_survives_sqlite_and_activity(self):
        for text in ("Plain text", "Задача: позвонить", "日本語のタスク", "مرحبا", "Ready \U0001f680 \U0001f600", "Cafe\u0301"):
            with self.subTest(text=text):
                task = self.create(title=text, description=text, related_entity_label=text)
                saved = self.service.get(self.creator, task["id"])
                self.assertEqual((saved["title"], saved["description"], saved["related_entity_label"]), (text, text, text))
                self.assertEqual(self.service.activity(self.creator, task["id"])["items"][0]["payload"]["task"]["title"], text)


if __name__ == "__main__":
    unittest.main()
