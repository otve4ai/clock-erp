"""Real SQLite regression tests for acceptance and persistent microtasks."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest import mock

import test_tasks_microtasks as fixtures
from test_tasks_microtasks import MicroFixture, BASE
from app.tasks.repository import TaskSession
from scripts.prepare_tasks_inbox_workflow import prepare


class InboxWorkflowTest(MicroFixture):
    def normal(self, **values):
        return self.tasks.create(self.users[1], dict(title="Поручение", assigned_to=2, **values))

    def test_other_waits_self_is_planned(self):
        self.assertEqual(self.normal()["status"], "waiting")
        self.assertEqual(self.tasks.create(self.users[1], {"title": "Себе"})["status"], "new")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.events(1), [])

    def test_preview_does_not_change_any_data(self):
        for task in (self.normal(), self.micro(assigned_to=2)):
            before = self.path.read_bytes()
            self.tasks.get(self.users[2], task["id"])
            self.tasks.activity(self.users[2], task["id"])
            self.events()
            self.assertEqual(self.path.read_bytes(), before)

    def test_accept_atomic_history_version_and_badge(self):
        task = self.normal()
        accepted = self.change(task, actor=2, operation="accept")
        self.assertEqual((accepted["status"], accepted["version"]), ("in_progress", 2))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.inbox.claim(self.users[2], {})["items"], [])
        history = self.tasks.activity(self.users[1], task["id"])["items"]
        self.assertEqual(history[-1]["payload"]["action"], "accepted")
        self.assertEqual(history[-1]["actor"], 2)
        self.error(409, self.change, task, actor=2, operation="accept")
        self.error(409, self.change, accepted, actor=2, operation="accept")

    def test_only_current_assignee_can_accept(self):
        task = self.normal()
        for actor, code in ((1, 403), (3, 404), (4, 403)):
            self.error(code, self.change, task, actor=actor, operation="accept")
        current = self.change(task, assigned_to=3)
        self.assertEqual(current["status"], "waiting")
        self.error(404, self.change, current, actor=2, operation="accept")
        self.assertEqual(len(self.events(3)), 1)

    def test_accept_failure_rolls_back_task_history_and_inbox(self):
        task = self.normal()
        before = self.path.read_bytes()
        original = TaskSession.finish_inbox
        def failure(session, *args):
            original(session, *args)
            raise sqlite3.OperationalError("synthetic")
        with mock.patch.object(TaskSession, "finish_inbox", failure):
            with self.assertRaises(sqlite3.Error):
                self.change(task, actor=2, operation="accept")
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_accepts_one_commit(self):
        task = self.normal()
        barrier = threading.Barrier(2)
        def accept(_):
            barrier.wait()
            try:
                self.change(task, actor=2, operation="accept")
                return 200
            except Exception as error:
                return getattr(error, "status", 500)
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(accept, range(2))), [200, 409])

    def test_active_read_endpoint_cannot_bypass_workflow(self):
        self.normal()
        self.micro(assigned_to=2)
        for event in self.events():
            self.error(409, self.inbox.read, self.users[2], event["id"], {})
        self.assertEqual(len(self.events()), 2)

    def test_accept_races_reassign_delete_and_complete(self):
        for operation, values in (("patch", {"assigned_to": 3}), ("delete", {}), ("status", {"status": "done"})):
            task = self.normal()
            barrier = threading.Barrier(2)
            def run(accept):
                barrier.wait()
                try:
                    if accept:
                        self.change(task, actor=2, operation="accept")
                    else:
                        self.change(task, operation=operation, **values)
                    return 200
                except Exception as error:
                    return getattr(error, "status", 500)
            with ThreadPoolExecutor(max_workers=2) as pool:
                codes = list(pool.map(run, (True, False)))
            self.assertEqual(codes.count(200), 1)
            self.assertTrue(set(codes) <= {200, 404, 409})
            current = self.tasks.get(self.users[1], task["id"])
            self.assertEqual(current["version"], 2)
            self.assertNotIn(task["id"], [item["task_id"] for item in self.events()])

    def test_micro_stays_overdue_until_done_without_accept(self):
        task = self.micro(assigned_to=2)
        deadline = task["micro_deadline_at"]
        self.error(403, self.change, task, actor=2, operation="accept")
        self.now = "2026-10-01T12:00:00+00:00"
        self.assertEqual(len(self.events()), 1)
        done = self.change(task, actor=2, status="done")
        self.assertEqual(done["micro_deadline_at"], deadline)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.inbox.claim(self.users[2], {})["items"], [])
        reopened = self.change(done, actor=2, status="new")
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(reopened["micro_deadline_at"], deadline)

    def test_delete_restore_reassign_and_self_assignment(self):
        task = self.micro(assigned_to=2)
        removed = self.change(task, operation="delete")
        self.assertEqual(self.events(), [])
        restored = self.change(removed, operation="restore")
        self.assertEqual(len(self.events()), 1)
        other = self.change(restored, assigned_to=3)
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.events(3)), 1)
        self.change(other, assigned_to=1)
        self.assertEqual(self.events(1) + self.events(3), [])

    def test_finished_micro_read_and_reassign_do_not_lose_reopened_assignment(self):
        task = self.micro(assigned_to=2)
        event = self.events()[0]
        done = self.change(task, status="done")
        self.inbox.read(self.users[2], event["id"], {})
        active = self.change(done, status="new")
        self.assertEqual(len(self.events()), 1)
        done = self.change(active, status="done")
        reassigned = self.change(done, assigned_to=3)
        self.assertEqual(self.events(3), [])
        self.change(reassigned, status="new")
        self.assertEqual(self.events(), [])
        self.assertEqual(len(self.events(3)), 1)

    def test_list_and_counts_use_current_type_title_and_lifecycle(self):
        normal = self.normal()
        micro = self.micro(assigned_to=2)
        self.change(micro, title="Исправленное название")
        counts = {"count": 2, "normal": 1, "micro": 1}
        self.assertEqual(self.inbox.list(self.users[2], badge=True), counts)
        self.assertEqual(self.tasks.list(self.users[2], summary=True)["inbox_counts"], counts)
        self.assertEqual(self.events()[0]["title"], "Исправленное название")
        self.change(normal, status="done")
        self.assertEqual(self.inbox.list(self.users[2], badge=True), {"count": 1, "normal": 0, "micro": 1})

    def test_accept_opens_only_module_database(self):
        task = self.normal()
        connect = sqlite3.connect
        def guard(path, *args, **kwargs):
            self.assertEqual(str(path), self.path.as_uri() + "?mode=rw")
            return connect(path, *args, **kwargs)
        with mock.patch("sqlite3.connect", side_effect=guard):
            self.change(task, actor=2, operation="accept")


class InboxWorkflowApiTest(fixtures.MicroApiTest):
    def test_accept_http_csrf_idor_and_version(self):
        task = self.post("/tasks", {"title": "HTTP assignment", "assigned_to": 2}, 201)
        route = "/tasks/{}/accept".format(task["id"])
        self.post(route, {"version": 1}, 403)
        self.actor = self.users[2]
        self.assertEqual(self.client.post(BASE + route, json={"version": 1}).status_code, 403)
        self.post(route, {}, 422)
        self.assertEqual(self.post(route, {"version": 1})["status"], "in_progress")
        refreshed = self.client.get(BASE + "/tasks/{}".format(task["id"])).get_json()["data"]
        self.assertFalse(refreshed["permissions"]["accept"])
        self.post(route, {"version": 1}, 409)
        self.assertEqual(self.client.get(BASE + "/inbox").get_json()["data"]["total"], 0)


class InboxPreparationTest(MicroFixture):
    def test_dry_run_apply_idempotent_and_no_schema_changes(self):
        task = self.tasks.create(self.users[1], {"title": "Old waiting", "assigned_to": 2, "status": "waiting"})
        micro = self.micro(assigned_to=2)
        event = self.events()[0]
        with self.repo.transaction(write=True) as session:
            session.connection.execute("UPDATE task_inbox_events SET handled_at=? WHERE id=?", (self.now, event["id"]))
        original = self.path.read_bytes()
        plan = prepare(self.repo, 4, self.now)
        self.assertEqual(plan["waiting_to_in_progress"], [task["id"]])
        self.assertEqual(plan["micro_inbox_restored"], [event["id"]])
        self.assertEqual(self.path.read_bytes(), original)
        prepare(self.repo, 4, self.now, True)
        self.assertEqual(self.tasks.get(self.users[2], task["id"])["status"], "in_progress")
        self.assertEqual(self.tasks.get(self.users[2], micro["id"])["micro_deadline_at"], micro["micro_deadline_at"])
        self.assertEqual(len(self.events()), 2)
        self.assertEqual(self.inbox.claim(self.users[2], {})["items"][0]["task_id"], task["id"])
        after = self.path.read_bytes()
        self.assertEqual(prepare(self.repo, 4, self.now, True)["waiting_to_in_progress"], [])
        self.assertEqual(self.path.read_bytes(), after)
        self.assertEqual(self.repo.status()["schema_version"], 4)

    def test_preparation_excludes_later_task_and_retired_self_assignment(self):
        task = self.micro(assigned_to=2)
        task = self.change(task, assigned_to=1)
        # Emulate a later silent self-assignment matching an old recipient.
        # Its newer reassignment Activity must prevent resurrecting that event.
        with self.repo.transaction(write=True) as session:
            session.connection.execute("UPDATE tasks SET assigned_to=2 WHERE id=?", (task["id"],))
        old_time = self.now
        self.now = "2026-10-02T12:00:00+00:00"
        future = self.tasks.create(self.users[1], {"title": "Future", "assigned_to": 2})
        plan = prepare(self.repo, 4, old_time, True)
        self.assertEqual(plan["micro_inbox_restored"], [])
        self.assertNotIn(future["id"], plan["waiting_to_in_progress"])

    def test_preparation_activity_failure_rolls_back(self):
        self.tasks.create(self.users[1], {"title": "Old", "assigned_to": 2})
        before = self.path.read_bytes()
        with mock.patch.object(TaskSession, "add_activity", side_effect=sqlite3.OperationalError("fixture")):
            with self.assertRaises(sqlite3.Error):
                prepare(self.repo, 4, self.now, True)
        self.assertEqual(self.path.read_bytes(), before)
