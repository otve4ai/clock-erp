import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.domain_schema_migrations import apply_domain_migrations
from app.services.collaboration import CollaborationStore, CollaborationValidationError
from unittest import mock


AUDIT_SQL = """CREATE TABLE erp_audit_events (
id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,
action TEXT NOT NULL,actor_id TEXT,actor_type TEXT NOT NULL DEFAULT 'user',
actor_display_name_snapshot TEXT NOT NULL,occurred_at TEXT NOT NULL,
object_label_snapshot TEXT NOT NULL,object_secondary_snapshot TEXT NOT NULL DEFAULT '',
changes_json TEXT NOT NULL DEFAULT '{}',metadata_json TEXT NOT NULL DEFAULT '{}',
search_text TEXT NOT NULL DEFAULT '',status_snapshot TEXT NOT NULL DEFAULT '',
source_snapshot TEXT NOT NULL DEFAULT '')"""


class CollaborationStoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.tasks = root / "tasks.db"
        self.auth = root / "auth.db"
        self.catalog = root / "catalog.db"
        apply_domain_migrations(self.tasks, "tasks", "test")
        apply_domain_migrations(self.auth, "auth", "test")
        with sqlite3.connect(str(self.auth)) as connection:
            for user_id, first_name, active in ((1, "Максим", 1), (2, "MRV", 1), (3, "Архив", 0)):
                connection.execute(
                    "INSERT INTO users(id,first_name,last_name,email,email_normalized,password_hash,role,active,created_at,updated_at,session_version) "
                    "VALUES(?,?, '', ?, ?, 'hash', 'employee', ?, 1, 1, 1)",
                    (user_id, first_name, "{}@example.test".format(user_id),
                     "{}@example.test".format(user_id), active),
                )
        with sqlite3.connect(str(self.catalog)) as connection:
            connection.execute(AUDIT_SQL)
        self.store = CollaborationStore(self.tasks, self.auth, self.catalog)
        self.maxim = {"id": 1, "first_name": "Максим", "last_name": "", "email": "1@example.test"}

    def tearDown(self):
        self.temporary.cleanup()

    def test_assignment_is_atomic_audited_and_delivered_only_to_target(self):
        result, created = self.store.assign(
            "order", "21123", 2, self.maxim, "Заказ №21123", "/order/21123",
            "Клиент будет писать вечером", "assign-order-21123",
        )
        self.assertTrue(created)
        self.assertEqual(result["assignment"]["responsible_user_id"], 2)
        self.assertEqual(result["history"][0]["actor_user_id"], 1)
        self.assertEqual(result["history"][0]["new_user_id"], 2)
        self.assertEqual(self.store.unread_count(2), 1)
        self.assertEqual(self.store.unread_count(1), 0)
        with sqlite3.connect(str(self.catalog)) as connection:
            audit = connection.execute(
                "SELECT actor_id,changes_json FROM erp_audit_events"
            ).fetchone()
        self.assertEqual(audit[0], "1")
        self.assertIn('"after": 2', audit[1])

    def test_inactive_nonexistent_self_notification_and_dedupe(self):
        for target in (3, 999):
            with self.assertRaises(CollaborationValidationError):
                self.store.assign("customer", "7", target, self.maxim, "Клиент №7")
        self.store.assign("customer", "7", 1, self.maxim, "Клиент №7",
                          operation_key="self")
        self.assertEqual(self.store.unread_count(1), 0)
        first = self.store.assign("repair", "r-1", 2, self.maxim, "Ремонт №1",
                                  operation_key="same-operation")
        second = self.store.assign("repair", "r-1", 2, self.maxim, "Ремонт №1",
                                   operation_key="same-operation")
        self.assertTrue(first[1])
        self.assertFalse(second[1])
        self.assertEqual(self.store.unread_count(2), 1)

    def test_inbox_isolation_read_mark_all_and_pagination(self):
        for index in range(3):
            self.store.assign("purchase", str(index), 2, self.maxim,
                              "Закупка №{}".format(index), operation_key="purchase-{}".format(index))
        first = self.store.list_inbox(2, page=1, per_page=2)
        self.assertEqual((first["total"], len(first["rows"]), first["pages"]), (3, 2, 2))
        event_id = first["rows"][0]["id"]
        self.assertFalse(self.store.mark_read(event_id, 1))
        self.assertTrue(self.store.mark_read(event_id, 2))
        self.assertEqual(self.store.unread_count(2), 2)
        self.assertEqual(self.store.mark_all_read(2), 2)
        self.assertEqual(self.store.unread_count(2), 0)

    def test_failure_rolls_back_assignment_history_and_inbox(self):
        broken = Path(self.temporary.name) / "broken.db"
        sqlite3.connect(str(broken)).close()
        store = CollaborationStore(self.tasks, self.auth, broken)
        with self.assertRaises(sqlite3.OperationalError):
            store.assign("order", "fail", 2, self.maxim, "Заказ fail", operation_key="fail")
        self.assertIsNone(self.store.get_assignment("order", "fail")["assignment"])
        self.assertEqual(self.store.unread_count(2), 0)

    def test_retired_task_assignments_are_rejected_before_database_access(self):
        with mock.patch("sqlite3.connect", side_effect=AssertionError("no DB")) as connect:
            for action in (
                    lambda: self.store.assign("task", "1", 2, self.maxim, "Legacy"),
                    lambda: self.store.get_assignment("task", "1"),
                    lambda: self.store.assigned_entity_ids("task", 2)):
                with self.assertRaises(CollaborationValidationError):
                    action()
            connect.assert_not_called()

    def test_legacy_task_inbox_events_are_preserved_but_excluded_everywhere(self):
        with sqlite3.connect(str(self.tasks)) as connection:
            legacy_id = connection.execute(
                "INSERT INTO inbox_events(recipient_user_id,actor_user_id,event_type,entity_type,"
                "entity_id,created_at,metadata_json,operation_key) "
                "VALUES(2,1,'task_assigned','task','1','now','{}','old-task')").lastrowid
        self.store.assign("order", "1", 2, self.maxim, "Order", operation_key="new-order")
        self.assertEqual(self.store.list_inbox(2)["total"], 1)
        self.assertEqual(self.store.list_inbox(2, unread_only=True)["rows"][0]["entity_type"], "order")
        self.assertEqual(self.store.unread_count(2), 1)
        self.assertFalse(self.store.mark_read(legacy_id, 2))
        self.assertEqual(self.store.mark_all_read(2), 1)
        self.assertEqual(self.store.unread_count(2), 0)
        with sqlite3.connect(str(self.tasks)) as connection:
            self.assertIsNone(connection.execute("SELECT read_at FROM inbox_events WHERE id=?", (legacy_id,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
