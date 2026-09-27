"""Stage D: real SQLite atomicity, micro deadlines, Inbox and concurrent delivery."""

import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from flask import Flask, request

from app.tasks import migrations
from app.tasks.domain import TaskError, utc_instant
from app.tasks.inbox_repository import InboxQueries
from app.tasks.inbox_services import InboxService
from app.tasks.repository import TasksRepository, TaskSession, validate_connection
from app.tasks.schema import LEDGER_DDL, V3_CORE_DDL, CORE_DDL
from app.tasks.services import TasksService
from app.tasks_boundary import register_tasks_module


BASE = "/api/v1/tasks-module"


class MicroFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks-module.db"
        migrations.migrate_database(self.path)
        self.users = {i: {"id": i, "role": "admin" if i == 4 else "employee", "active": int(i != 5)} for i in range(1, 6)}
        self.now = "2026-09-27T20:59:59.123456+00:00"
        self.repo = TasksRepository(self.path)
        self.tasks = TasksService(self.repo, self.users.get, now=lambda: self.now)
        self.inbox = InboxService(self.repo, now=lambda: self.now)

    def micro(self, actor=1, **values):
        return self.tasks.create_micro(self.users[actor], dict(title="Micro Кириллица 日本語 🚀", **values))

    def change(self, task, actor=1, operation="patch", **values):
        return self.tasks.mutate(self.users[actor], task["id"], dict(version=task["version"], **values), operation)

    def error(self, code, call, *args, **kwargs):
        with self.assertRaises(TaskError) as failure:
            call(*args, **kwargs)
        self.assertEqual(failure.exception.status, code)

    def events(self, actor=2):
        return self.inbox.list(self.users[actor])["items"]


class MicroDomainTest(MicroFixture):
    def test_exact_elapsed_24h_and_fixed_utc_representation(self):
        task = self.micro()
        self.assertEqual((task["task_type"], task["assigned_to"], task["version"]), ("micro", 1, 1))
        self.assertEqual(task["created_at"], self.now)
        self.assertEqual((utc_instant(task["micro_deadline_at"]) - utc_instant(task["created_at"])).total_seconds(), 86400)
        self.assertEqual(task["micro_deadline_at"], "2026-09-28T20:59:59.123456+00:00")
        self.assertIsNone(task["deadline_date"])
        self.assertIsNone(task["project_id"])

    def test_zero_fraction_is_normalized_and_overdue_exact_boundary(self):
        self.now = "2026-09-27T21:00:00+00:00"
        task = self.micro()
        self.assertEqual(task["created_at"], "2026-09-27T21:00:00.000000+00:00")
        for now, count in (("2026-09-28T20:59:59.999999+00:00", 0),
                           ("2026-09-28T21:00:00+00:00", 0),
                           ("2026-09-28T21:00:00.000001+00:00", 1)):
            self.now = now
            self.assertEqual(self.tasks.micros(self.users[1], {"overdue": "true"})["total"], count)
            self.assertEqual(self.tasks.micros(self.users[1], summary=True)["my_overdue"], count)

    def test_host_timezone_does_not_influence_deadline(self):
        original_tz = os.environ.get("TZ")
        try:
            results = []
            for zone in ("UTC", "Pacific/Honolulu", "Asia/Tokyo"):
                os.environ["TZ"] = zone
                if hasattr(time, "tzset"):
                    time.tzset()
                results.append(self.micro()["micro_deadline_at"])
            self.assertEqual(len(set(results)), 1)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            if hasattr(time, "tzset"):
                time.tzset()

    def test_nearest_summary_excludes_overdue_but_includes_exact_boundary(self):
        past = self.micro()
        self.now = "2026-09-28T21:00:00+00:00"
        summary = self.tasks.micros(self.users[1], summary=True)
        self.assertEqual((summary["my_overdue"], summary["nearest_my_deadline_at"]), (1, None))
        future = self.micro()
        summary = self.tasks.micros(self.users[1], summary=True)
        self.assertEqual((summary["my_overdue"], summary["nearest_my_deadline_at"]), (1, future["micro_deadline_at"]))
        self.now = past["micro_deadline_at"]
        summary = self.tasks.micros(self.users[1], summary=True)
        self.assertEqual((summary["my_overdue"], summary["nearest_my_deadline_at"]), (0, past["micro_deadline_at"]))

    def test_rename_reassign_complete_reopen_never_extend_deadline(self):
        task = self.micro(assigned_to=2)
        deadline, created = task["micro_deadline_at"], task["created_at"]
        self.now = "2026-09-28T22:00:00+00:00"
        for values in ({"title": "Renamed"}, {"assigned_to": 3}, {"status": "done"}, {"status": "new"}):
            task = self.change(task, **values)
            self.assertEqual((task["micro_deadline_at"], task["created_at"]), (deadline, created))
            self.assertEqual(task["completed_at"] is None, task["status"] != "done")
        self.assertEqual(task["version"], 5)

    def test_creation_rejects_normal_and_server_fields(self):
        for values in ({"title": " "}, {"title": "\ud800"}, {"assigned_to": 5}, {"assigned_to": 999},
                       {"status": "done"}, {"project_id": None}, {"deadline_date": None},
                       {"priority": "normal"}, {"description": ""}, {"created_by": 2},
                       {"micro_deadline_at": self.now}, {"task_type": "micro"}, {"version": 1}):
            payload = {"title": "Valid"}
            payload.update(values)
            self.error(422, self.tasks.create_micro, self.users[1], payload)
        self.assertEqual(self.tasks.micros(self.users[4], {"scope": "all"})["total"], 0)

    def test_generic_patch_cannot_bypass_micro_shape(self):
        task = self.micro(assigned_to=2)
        for values in ({"description": "hidden"}, {"priority": "high"}, {"deadline_date": None},
                       {"project_id": None}, {"related_entity_id": "ERP"}, {"status": "waiting"},
                       {"status": "in_progress"}, {"task_type": "normal"}, {"micro_deadline_at": self.now}):
            self.error(422, self.change, task, **values)
        self.assertEqual(self.tasks.get(self.users[1], task["id"]), task)

    def test_permissions_same_as_normal_including_mixed_patch(self):
        task = self.micro(assigned_to=2)
        for actor in (1, 2, 4):
            self.assertEqual(self.tasks.get(self.users[actor], task["id"]), task)
        self.error(404, self.tasks.get, self.users[3], task["id"])
        for values in ({"assigned_to": 3}, {"title": "Partial", "assigned_to": 3}):
            self.error(403, self.change, task, actor=2, **values)
        self.error(403, self.change, task, actor=2, operation="delete")
        self.assertEqual(self.tasks.get(self.users[1], task["id"]), task)
        task = self.change(task, actor=2, title="Assignee edits", status="done")
        task = self.change(task, actor=4, status="new")
        self.assertIsNone(task["completed_at"])

    def test_scopes_search_summary_and_soft_delete(self):
        mine = self.micro(assigned_to=2)
        assigned = self.micro(actor=2, assigned_to=1)
        hidden = self.micro(actor=3)
        for scope, expected in (("my", {assigned["id"]}), ("created", {mine["id"]}),
                                ("team", {mine["id"], assigned["id"]}), ("all", {mine["id"], assigned["id"]})):
            result = self.tasks.micros(self.users[1], {"scope": scope, "search": "кирИллица"})
            self.assertEqual({item["id"] for item in result["items"]}, expected)
        self.assertEqual(self.tasks.micros(self.users[4], {"scope": "all"})["total"], 3)
        self.assertEqual(self.tasks.micros(self.users[1], summary=True),
                         {"my_active": 1, "created_active": 1, "team_active": 2, "my_overdue": 0,
                          "nearest_my_deadline_at": assigned["micro_deadline_at"]})
        deleted = self.change(mine, operation="delete")
        self.assertEqual(self.tasks.micros(self.users[1], {"scope": "all"})["total"], 1)
        self.change(deleted, operation="restore")
        self.assertEqual(self.tasks.micros(self.users[1], {"scope": "all"})["total"], 2)

    def test_order_filters_and_pagination(self):
        first = self.micro(assigned_to=2)
        self.now = "2026-09-28T21:00:00+00:00"
        second = self.micro(assigned_to=2)
        third = self.micro(actor=2)
        result = self.tasks.micros(self.users[2], {"created_by": "1", "assigned_to": "2", "limit": "1", "offset": "1"})
        self.assertEqual((result["total"], result["items"][0]["id"]), (2, second["id"]))
        self.assertEqual(self.tasks.micros(self.users[2], {"overdue": "true"})["items"][0]["id"], first["id"])

    def test_normal_lists_summary_archive_never_include_micro(self):
        active = self.micro(assigned_to=2)
        done = self.change(self.micro(), status="done")
        normal = self.tasks.create(self.users[1], {"title": "Normal"})
        for options in ({"scope": "all"}, {"scope": "all", "search": "Micro"}, {"scope": "all", "view": "archive"}):
            result = self.tasks.list(self.users[4], options)
            self.assertFalse({active["id"], done["id"]} & {item["id"] for item in result["items"]})
        self.assertEqual(self.tasks.list(self.users[4], {"scope": "all"}, summary=True)["total"], 1)
        self.assertIsNone(normal["micro_deadline_at"])
        self.assertEqual(self.tasks.micros(self.users[1], {"status": "done"})["items"][0]["id"], done["id"])

    def test_conversion_preserves_identity_and_history_active_and_done(self):
        for done in (False, True):
            task = self.micro(assigned_to=2)
            if done:
                task = self.change(task, status="done")
            history = self.tasks.activity(self.users[1], task["id"])["items"]
            converted = self.change(task, actor=2, operation="convert")
            for field in ("id", "title", "created_at", "created_by", "assigned_to", "completed_at", "status"):
                self.assertEqual(converted[field], task[field])
            self.assertEqual(converted["version"], task["version"] + 1)
            self.assertEqual(converted["task_type"], "normal")
            self.assertIsNone(converted["micro_deadline_at"])
            activity = self.tasks.activity(self.users[1], task["id"])["items"]
            self.assertEqual(activity[:-1], history)
            self.assertEqual(activity[-1]["event_type"], "converted_to_normal")
            self.assertEqual(self.tasks.micros(self.users[2], {"status": "done" if done else "new"})["total"], 0)
            normal = self.tasks.list(self.users[2], {"view": "archive"} if done else {})
            self.assertIn(converted["id"], {row["id"] for row in normal["items"]})

    def test_conversion_stale_former_assignee_and_rollback(self):
        task = self.micro(assigned_to=2)
        with mock.patch.object(TaskSession, "add_activity", side_effect=sqlite3.OperationalError("history")):
            with self.assertRaises(sqlite3.Error):
                self.change(task, operation="convert")
        self.assertEqual(self.tasks.get(self.users[1], task["id"]), task)
        changed = self.change(task, assigned_to=3)
        self.error(409, self.change, task, operation="convert")
        self.error(404, self.change, changed, actor=2, operation="convert")

    def test_concurrent_micro_update_does_not_silently_overwrite(self):
        task = self.micro()
        barrier = threading.Barrier(2)
        def update(title):
            barrier.wait()
            try:
                self.change(task, title=title)
                return 200
            except TaskError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(update, ("A", "B"))), [200, 409])

    def test_malformed_server_time_never_writes_task(self):
        before = self.path.read_bytes()
        for value in ("bad", "2026-09-27T21:00:00", "2026-09-27T21:00:00+03:00"):
            self.now = value
            with self.assertRaises(ValueError):
                self.micro()
        self.assertEqual(self.path.read_bytes(), before)


class InboxDomainTest(MicroFixture):
    def test_inbox_orders_by_event_time_then_id(self):
        first = self.micro(assigned_to=2)
        self.now = "2026-09-26T20:59:59.123456+00:00"
        second = self.micro(assigned_to=2)
        third = self.micro(assigned_to=2)
        self.assertEqual([item["task_id"] for item in self.events()], [first["id"], third["id"], second["id"]])

    def test_other_assignment_creates_event_normal_and_micro_self_is_silent(self):
        self.micro()
        self.tasks.create(self.users[2], {"title": "Self"})
        self.assertEqual(self.events(1) + self.events(2), [])
        self.micro(assigned_to=2)
        normal = self.tasks.create(self.users[1], {"title": "Normal", "assigned_to": 2})
        events = self.events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["task_id"], normal["id"])
        self.assertEqual({row["event_type"] for row in events}, {"task_assigned"})
        self.assertEqual(self.tasks.list(self.users[2], summary=True)["inbox"], 2)

    def test_assignment_and_activity_and_event_rollback_together(self):
        with mock.patch.object(InboxQueries, "assignment_event", side_effect=sqlite3.OperationalError("inbox")):
            with self.assertRaises(sqlite3.Error):
                self.micro(assigned_to=2)
        with self.repo.transaction() as session:
            for table in ("tasks", "task_activity", "task_inbox_events"):
                self.assertEqual(session.connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0], 0)

    def test_reassign_new_event_retire_old_and_return_is_new_generation(self):
        task = self.micro(assigned_to=2)
        first = self.events()[0]
        task = self.change(task, assigned_to=3)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.inbox.claim(self.users[2], {})["items"], [])
        self.assertEqual(self.events(3)[0]["event_type"], "task_reassigned")
        task = self.change(task, assigned_to=2)
        self.assertEqual(len(self.events()), 1)
        self.assertNotEqual(self.events()[0]["id"], first["id"])
        self.assertEqual(self.events(3), [])
        self.change(task, assigned_to=1)
        self.assertEqual(self.events(1) + self.events(2), [])

    def test_duplicate_generation_no_duplicate_event_and_stale_retry_conflicts(self):
        task = self.micro(assigned_to=2)
        with self.repo.transaction(write=True) as session:
            session.assignment_event(task, 1, self.now)
        self.assertEqual(len(self.events()), 1)
        self.change(task, assigned_to=3)
        self.error(409, self.change, task, assigned_to=3)
        self.assertEqual(len(self.events(3)), 1)

    def test_reassign_inbox_failure_restores_task_activity_and_old_event(self):
        task = self.micro(assigned_to=2)
        before = self.path.read_bytes()
        original = InboxQueries.assignment_event
        def fail(session, *args, **kwargs):
            original(session, *args, **kwargs)
            raise sqlite3.OperationalError("after events")
        with mock.patch.object(InboxQueries, "assignment_event", fail):
            with self.assertRaises(sqlite3.Error):
                self.change(task, assigned_to=3)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.tasks.get(self.users[1], task["id"]), task)
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events(3), [])

    def test_recipient_only_even_admin_cannot_read_another_inbox(self):
        self.micro(assigned_to=2)
        event = self.events()[0]
        for actor in (1, 3, 4):
            self.assertEqual(self.events(actor), [])
            self.error(404, self.inbox.read, self.users[actor], event["id"], {})
            self.assertEqual(self.inbox.claim(self.users[actor], {})["items"], [])

    def test_task_get_and_activity_are_read_only_for_inbox(self):
        task = self.micro(assigned_to=2)
        before = self.path.read_bytes()
        self.tasks.get(self.users[2], task["id"])
        self.tasks.activity(self.users[2], task["id"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.events()[0]["handled_at"])

    def test_read_of_finished_event_is_idempotent_and_never_notifies(self):
        task = self.micro(assigned_to=2)
        event = self.events()[0]
        self.error(409, self.inbox.read, self.users[2], event["id"], {})
        self.change(task, status="done")
        handled = self.inbox.read(self.users[2], event["id"], {})
        self.now = "2026-09-28T00:00:00+00:00"
        self.assertEqual(self.inbox.read(self.users[2], event["id"], {}), handled)
        self.assertEqual(self.inbox.list(self.users[2], badge=True), {"count": 0, "normal": 0, "micro": 0})
        self.assertEqual(len(self.inbox.claim(self.users[2], {})["items"]), 0)

    def test_completion_and_soft_delete_hide_but_preserve_event(self):
        task = self.micro(assigned_to=2)
        task = self.change(task, status="done")
        self.change(task, operation="delete")
        self.assertEqual(len(self.events()), 0)
        with self.repo.transaction() as session:
            self.assertEqual(session.connection.execute("SELECT COUNT(*) FROM task_inbox_events").fetchone()[0], 1)

    def test_claim_once_preserves_inbox_and_task(self):
        task = self.micro(assigned_to=2)
        claimed = self.inbox.claim(self.users[2], {})["items"]
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["title"], task["title"])
        self.assertEqual(self.inbox.claim(self.users[2], {})["items"], [])
        self.assertEqual(len(self.events()), 1)
        self.assertIsNone(self.events()[0]["handled_at"])
        self.assertEqual(self.tasks.get(self.users[2], task["id"]), task)

    def test_two_concurrent_claims_never_duplicate(self):
        for index in range(4):
            self.micro(assigned_to=2)
        barrier = threading.Barrier(2)
        def claim(_):
            barrier.wait()
            return self.inbox.claim(self.users[2], {"limit": 2})["items"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = list(pool.map(claim, (0, 1)))
        self.assertEqual(len(first) + len(second), 4)
        self.assertFalse({row["id"] for row in first} & {row["id"] for row in second})

    def test_claim_failure_rolls_back_delivery_only_and_can_retry(self):
        task = self.micro(assigned_to=2)
        original = InboxQueries.claim_notifications
        def fail(session, *args):
            original(session, *args)
            raise sqlite3.OperationalError("delivery")
        with mock.patch.object(InboxQueries, "claim_notifications", fail):
            with self.assertRaises(sqlite3.Error):
                self.inbox.claim(self.users[2], {})
        self.assertIsNone(self.events()[0]["notified_at"])
        self.assertIsNone(self.events()[0]["handled_at"])
        self.assertEqual(self.tasks.get(self.users[2], task["id"]), task)
        self.assertEqual(len(self.inbox.claim(self.users[2], {})["items"]), 1)

    def test_batch_and_recipient_filters_cannot_be_spoofed(self):
        for payload in ({"limit": 0}, {"limit": 11}, {"limit": True}, {"limit": "1"}, {"recipient_id": 2}):
            self.error(422, self.inbox.claim, self.users[1], payload)
        self.error(422, self.inbox.list, self.users[1], {"recipient_id": "2"})
        self.error(422, self.inbox.list, self.users[1], {"search": "secret"})
        self.error(404, self.inbox.read, self.users[1], 999, {})


class MicroStorageTest(MicroFixture):
    def v3(self, invalid_fk=False):
        path = Path(self.temp.name) / "v3" / "tasks-module.db"
        path.parent.mkdir()
        connection = sqlite3.connect(str(path))
        connection.execute(LEDGER_DDL)
        for sql in V3_CORE_DDL:
            connection.execute(sql)
        connection.executemany("INSERT INTO tasks_module_migrations VALUES(?,?,'now','fixture')",
                               ((1, "tasks-module-foundation-v1"), (2, "tasks-module-core-v2"), (3, "tasks-module-projects-v3")))
        connection.execute("INSERT INTO task_projects(id,name,owner_id,created_at,updated_at) VALUES(9,'Project',1,'now','now')")
        connection.execute("INSERT INTO task_project_members VALUES(9,2,'now')")
        connection.execute("INSERT INTO task_project_activity VALUES(8,9,1,'created','now',1,'{}')")
        connection.execute("INSERT INTO tasks(id,title,created_by,assigned_to,created_at,updated_at,project_id) VALUES(42,'v3',1,2,'now','now',9)")
        connection.execute("INSERT INTO task_activity VALUES(17,?,1,'created','now',1,'{}')", (999 if invalid_fk else 42,))
        connection.commit()
        connection.close()
        return path

    def test_v3_upgrade_preserves_all_own_records_and_idempotence(self):
        path = self.v3()
        migrations.migrate_database(path)
        with TasksRepository(path).transaction() as session:
            self.assertEqual((session.get(42)["title"], session.get(42)["project_id"]), ("v3", 9))
            self.assertIsNone(session.get(42)["micro_deadline_at"])
            self.assertEqual(session.activity(42, 50, 0)[0]["id"], 17)
            self.assertEqual(session.members(9, 50, 0)["items"][0]["user_id"], 2)
            self.assertEqual(session.project_activity(9, 50, 0)[0]["id"], 8)
            self.assertEqual(session.inbox_badge(2), 0)  # No retroactive event migration.
        before = path.read_bytes()
        migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)

    def test_v3_ddl_failure_rolls_back_entire_original_database(self):
        path = self.v3()
        before = path.read_bytes()
        with mock.patch.object(migrations, "CORE_DDL", CORE_DDL + ("INVALID SQL",)):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)
        connection = sqlite3.connect(str(path))
        try:
            validate_connection(connection, version=3)
        finally:
            connection.close()

    def test_v3_bad_fk_upgrade_rolls_back(self):
        path = self.v3(invalid_fk=True)
        before = path.read_bytes()
        with self.assertRaises(sqlite3.IntegrityError):
            migrations.migrate_database(path)
        self.assertEqual(path.read_bytes(), before)

    def test_db_constraints_block_invalid_micro_combinations_and_duplicate_event(self):
        task = self.micro(assigned_to=2)
        for sql in ("UPDATE tasks SET task_type='normal'", "UPDATE tasks SET micro_deadline_at=NULL",
                    "UPDATE tasks SET status='waiting'", "UPDATE tasks SET deadline_date='2026-09-27'",
                    "UPDATE tasks SET project_id=123"):
            with self.assertRaises(sqlite3.IntegrityError):
                with self.repo.transaction(write=True) as session:
                    session.connection.execute(sql)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.repo.transaction(write=True) as session:
                session.connection.execute("INSERT INTO task_inbox_events(recipient_id,task_id,actor_id,event_type,created_at,dedupe_key,payload) "
                                           "SELECT recipient_id,task_id,actor_id,event_type,created_at,dedupe_key,payload FROM task_inbox_events")

    def test_all_new_operations_open_only_tasks_database(self):
        original = sqlite3.connect
        opened = []
        def guard(path, *args, **kwargs):
            self.assertIn("tasks-module.db?mode=", str(path))
            opened.append(str(path))
            return original(path, *args, **kwargs)
        with mock.patch("sqlite3.connect", side_effect=guard):
            task = self.micro(assigned_to=2)
            task = self.change(task, assigned_to=3)
            self.tasks.micros(self.users[3], {"scope": "all"})
            self.tasks.micros(self.users[3], summary=True)
            event = self.events(3)[0]
            self.inbox.claim(self.users[3], {})
            self.error(409, self.inbox.read, self.users[3], event["id"], {})
            self.change(task, operation="convert")
        self.assertGreater(len(opened), 5)

    def test_micro_and_inbox_schema_drift_fails_without_repair(self):
        from app.tasks.schema import TABLE_DDL
        defects = (("tasks", "micro_deadline_at TEXT", "micro_deadline_at INTEGER"),
                   ("tasks", "AND project_id IS NULL", ""),
                   ("task_inbox_events", "recipient_id INTEGER NOT NULL", "recipient_id INTEGER"),
                   ("task_inbox_events", "dedupe_key TEXT NOT NULL UNIQUE", "dedupe_key TEXT NOT NULL"),
                   ("task_inbox_events", " REFERENCES tasks(id)", ""))
        for index, (table, before, after) in enumerate(defects):
            path = Path(self.temp.name) / str(index) / "tasks-module.db"
            path.parent.mkdir()
            connection = sqlite3.connect(str(path))
            connection.execute(LEDGER_DDL)
            for sql in CORE_DDL:
                connection.execute(sql.replace(before, after) if sql == TABLE_DDL[table] else sql)
            connection.executemany("INSERT INTO tasks_module_migrations VALUES(?,?,'now','fixture')",
                                   ((1, "tasks-module-foundation-v1"), (2, "tasks-module-core-v2"),
                                    (3, "tasks-module-projects-v3"), (4, "tasks-module-microtasks-v4")))
            connection.commit()
            connection.close()
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                TasksRepository(path).status()
            self.assertEqual(path.read_bytes(), original)


class MicroApiTest(MicroFixture):
    def setUp(self):
        super().setUp()
        self.actor = self.users[1]
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, TASKS_MODULE_ENABLED=True, TASKS_MODULE_DATABASE=str(self.path), MAX_CONTENT_LENGTH=2048)
        self.assertTrue(register_tasks_module(self.app, Path(self.temp.name), lambda: self.actor, self.users.get,
                                             lambda: request.headers.get("X-CSRF-Token") == "test"))
        self.client = self.app.test_client()
        self.headers = {"X-CSRF-Token": "test"}

    def post(self, path, payload, expected=200):
        response = self.client.post(BASE + path, json=payload, headers=self.headers)
        self.assertEqual(response.status_code, expected, response.get_json())
        return response.get_json().get("data")

    def test_full_micro_inbox_and_notification_http_workflow(self):
        task = self.post("/microtasks", {"title": "HTTP", "assigned_to": 2}, 201)
        self.actor = self.users[2]
        self.assertEqual(self.client.get(BASE + "/microtasks").get_json()["data"]["total"], 1)
        self.assertEqual(self.client.get(BASE + "/microtasks/summary").get_json()["data"]["my_active"], 1)
        event = self.client.get(BASE + "/inbox").get_json()["data"]["items"][0]
        self.assertEqual(self.client.get(BASE + "/inbox/badge").get_json()["data"], {"count": 1, "normal": 0, "micro": 1})
        self.post("/notifications/claim", {})
        self.post("/inbox/{}/read".format(event["id"]), {}, 409)
        self.post("/microtasks/{}/complete".format(task["id"]), {"version": 1})
        self.post("/microtasks/{}/reopen".format(task["id"]), {"version": 2})
        converted = self.post("/microtasks/{}/convert".format(task["id"]), {"version": 3})
        self.assertEqual((converted["id"], converted["version"], converted["task_type"]), (task["id"], 4, "normal"))
        self.post("/microtasks/{}/complete".format(task["id"]), {"version": 4}, 404)

    def test_csrf_bad_json_size_and_http_semantics_remain_intact(self):
        self.assertEqual(self.client.post(BASE + "/notifications/claim", json={}).status_code, 403)
        for raw, code in (("{", 400), ("x" * 2049, 413)):
            response = self.client.post(BASE + "/microtasks", data=raw, content_type="application/json", headers=self.headers)
            self.assertEqual(response.status_code, code)
        self.post("/microtasks", {"title": "\ud800"}, 422)

    def test_claim_storage_error_safe_and_following_task_get_works(self):
        task = self.micro(assigned_to=2)
        self.actor = self.users[2]
        with mock.patch.object(InboxQueries, "claim_notifications", side_effect=RuntimeError("private SQL path")):
            response = self.client.post(BASE + "/notifications/claim", json={}, headers=self.headers)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private", response.get_data(as_text=True))
        self.assertEqual(self.client.get(BASE + "/tasks/" + str(task["id"])).status_code, 200)
        self.assertEqual(len(self.events()), 1)

    def test_http_requests_do_not_run_migrations(self):
        with mock.patch.object(migrations, "migrate_database", side_effect=AssertionError("HTTP migration")):
            self.post("/microtasks", {"title": "No repair", "assigned_to": 2}, 201)
            self.actor = self.users[2]
            self.client.get(BASE + "/inbox/badge")
            self.post("/notifications/claim", {})

    def test_feature_off_new_routes_never_initialize_storage(self):
        app = Flask("micro-off")
        app.config["TASKS_MODULE_ENABLED"] = False
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("OFF storage")):
            with mock.patch("app.tasks_boundary.importlib.import_module", side_effect=AssertionError("OFF import")):
                self.assertFalse(register_tasks_module(app, Path(self.temp.name), lambda: self.actor))
            client = app.test_client()
            for route in ("/microtasks", "/inbox", "/inbox/badge"):
                self.assertEqual(client.get(BASE + route).status_code, 404)
            self.assertEqual(client.post(BASE + "/notifications/claim", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
