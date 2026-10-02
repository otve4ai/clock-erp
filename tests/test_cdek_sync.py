import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from app.clients.cdek import CdekError
from app.services.cdek_delivery import CdekDelivery
from app.services.cdek_sync import CdekSync


class CdekSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.delivery = CdekDelivery(self.temp.name, client=mock.Mock(configured=True), clock=lambda: 1000)
        self.sync = CdekSync(self.delivery)

    def test_automatic_and_manual_share_lock_and_last_success(self):
        lock, data = self.sync.begin("automatic")
        self.assertEqual(self.sync.summary()["outcome"], "running")
        with self.assertRaises(CdekError):
            self.sync.begin("manual")
        self.delivery.sync_pending = mock.Mock(return_value=dict(updated=2, errors=0, skipped=1))
        self.sync.execute(lambda: [], lock, data)
        self.assertEqual(self.sync.summary()["last_success_at"], 1000)
        self.assertEqual(self.sync.summary()["outcome"], "success")
        with self.assertRaises(CdekError):
            self.sync.begin("manual")
        self.delivery.clock = lambda: 1061
        self.sync.run(lambda: [], source="manual")
        self.assertTrue(self.delivery.sync_pending.call_args[1]["manual"])

    def test_interruption_failure_and_missing_credentials_are_visible(self):
        lock, data = self.sync.begin("automatic")
        lock.release()
        self.assertEqual(self.sync.summary()["outcome"], "error")
        self.delivery.clock = lambda: 1100
        self.delivery.sync_pending = mock.Mock(side_effect=RuntimeError("private details"))
        self.sync.run(lambda: [])
        summary = self.sync.summary()
        self.assertEqual(summary["outcome"], "error")
        self.assertNotIn("private details", summary["message"])
        self.delivery.client.configured = False
        with self.assertRaises(CdekError):
            self.sync.begin("manual")

    def test_partial_failure_preserves_last_success(self):
        self.sync.write(dict(outcome="success", last_success_at=500))
        self.delivery.sync_pending = mock.Mock(return_value=dict(updated=1, errors=1, skipped=0))
        self.sync.run(lambda: [])
        self.assertEqual(self.sync.summary()["last_success_at"], 500)
        self.assertEqual(self.sync.summary()["outcome"], "error")
