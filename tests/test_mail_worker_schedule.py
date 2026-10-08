import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from scripts.mail_worker import sync_due


class MailWorkerScheduleTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        self.store = MagicMock()
        self.account = {"id": 1, "last_sync_status": "ok",
                        "last_sync_at": (self.now - timedelta(minutes=14)).isoformat()}
        self.store.account.return_value = self.account
        self.pending = self.store.connect.return_value.__enter__.return_value.execute.return_value.fetchone
        self.pending.return_value = None

    def test_automatic_sync_waits_fifteen_minutes(self):
        self.assertFalse(sync_due(self.store, self.now))
        self.assertTrue(sync_due(self.store, self.now + timedelta(minutes=1)))

    def test_manual_request_bypasses_interval(self):
        self.pending.return_value = (1,)
        self.assertTrue(sync_due(self.store, self.now))

    def test_failed_attempt_also_waits_fifteen_minutes(self):
        self.account.update(last_sync_status="error", updated_at=self.now.isoformat())
        self.assertFalse(sync_due(self.store, self.now + timedelta(minutes=14)))
        self.assertTrue(sync_due(self.store, self.now + timedelta(minutes=15)))

    def test_initial_sync_runs_immediately(self):
        self.account["last_sync_at"] = None
        self.assertTrue(sync_due(self.store, self.now))

    def test_disabled_account_is_skipped(self):
        self.store.account.return_value = None
        self.assertFalse(sync_due(self.store, self.now))


if __name__ == "__main__":
    unittest.main()
