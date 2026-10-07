"""Real temporary mail queue, fake CDEK/SMTP only; never contacts a recipient."""
import copy
import gc
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest import mock

from app.clients.cdek import CdekClient, CdekError
from app.services.cdek_delivery import CdekDelivery
from app.services.cdek_reminders import CdekReminders, DELAY
from app.services.mail import MailSynchronizer, MailValidationError
from tests import test_mail as mail_fixtures
from tests.test_mail import FakeTransport, FakeSMTP
from tests.test_cdek_delivery import response


class ReminderTest(unittest.TestCase):
    setUp_mail = mail_fixtures.MailServiceTest.setUp

    def tearDown(self):
        self.api.reset_mock(side_effect=True)
        FakeTransport.smtp_client = FakeSMTP()
        gc.collect()
        self.temp.cleanup()

    def setUp(self):
        self.setUp_mail()
        self.start = datetime(2026, 10, 1, 10, tzinfo=timezone.utc).timestamp()
        self.now = self.start + DELAY
        self.entity = dict(cdek_number="1234567890", number="42", is_return=False,
            delivery_point="MSK1", recipient={"name": "Тест", "email": "client@example.test"},
            statuses=[dict(code="ACCEPTED_AT_PICK_UP_POINT", date_time="2026-10-01T10:00:00Z")])
        self.api = mock.Mock(configured=True)
        self.api.get_order.side_effect = lambda **kw: copy.deepcopy(self.entity)
        self.api.get_delivery_point.return_value = dict(code="MSK1", work_time="Пн–Вс 10–20",
            location={"address_full": "Тестовый город, Тестовая улица, 1"})
        self.delivery = CdekDelivery(self.root / "cdek", self.api, clock=lambda: self.now)
        self.reminders = CdekReminders(self.delivery, self.store, clock=lambda: self.now)
        self.shipment = dict(id="shipment", source="tictactoy", tracking="1234567890", order_ids=["42"])
        self.delivery.sync(self.shipment)
        FakeTransport.smtp_client = FakeSMTP()

    def worker(self, guarded=True):
        return MailSynchronizer(self.store, self.box, FakeTransport,
            cdek_reminders=self.reminders if guarded else None)

    def row(self):
        with self.store.connect() as db:
            return dict(db.execute("SELECT * FROM mail_outbox ORDER BY id LIMIT 1").fetchone())

    def test_exact_three_days_optional_hours_and_no_invented_storage(self):
        self.now -= 1
        self.assertIsNone(self.reminders.build("1234567890"))
        self.now += 1
        payload = self.reminders.build("1234567890")
        self.assertIn("Тестовая улица", payload["text_body"])
        self.assertIn("Пн–Вс", payload["text_body"])
        self.assertNotIn("Забрать до", payload["text_body"])
        self.api.get_delivery_point.return_value.pop("work_time")
        self.assertNotIn("Режим работы", self.reminders.build("1234567890")["text_body"])

    def test_repeated_pickup_status_does_not_reset_timer(self):
        self.entity["statuses"].append(dict(code="ACCEPTED_AT_PICK_UP_POINT", date_time="2026-10-03T10:00:00Z"))
        self.assertIsNotNone(self.reminders.build("1234567890"))

    def test_terminal_return_and_unknown_are_not_eligible(self):
        for status in ("DELIVERED", "POSTOMAT_RECEIVED", "NOT_DELIVERED", "REMOVED", "UNKNOWN", "POSTOMAT_POSTED"):
            self.entity["statuses"][0]["code"] = status
            self.assertIsNone(self.reminders.build("1234567890"))
        self.entity["statuses"][0]["code"] = "ACCEPTED_AT_PICK_UP_POINT"
        self.entity["is_return"] = True
        self.assertIsNone(self.reminders.build("1234567890"))

    def test_missing_email_and_address_block_queue(self):
        self.entity["recipient"]["email"] = ""
        self.assertEqual(self.reminders.prepare([self.shipment])["errors"], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0], 0)
        self.entity["recipient"]["email"] = "client@example.test"
        self.api.get_delivery_point.return_value = {"location": {}}
        with self.assertRaises(CdekError):
            self.reminders.build("1234567890")

    def test_queue_links_duplicate_and_sent_status(self):
        self.assertEqual(self.reminders.prepare([self.shipment])["queued"], 1)
        self.assertEqual(self.reminders.prepare([self.shipment])["skipped"], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT entity_id FROM mail_links WHERE entity_type='order'").fetchone()[0], "42")
        self.assertEqual(self.worker().deliver()["sent"], 1)
        self.assertEqual(self.row()["state"], "sent")
        self.worker().deliver()
        self.assertEqual(len(FakeTransport.smtp_client.messages), 1)

    def test_collected_after_queue_suppresses_smtp(self):
        self.reminders.prepare([self.shipment])
        self.entity["statuses"].append(dict(code="DELIVERED", date_time="2026-10-04T09:00:00Z"))
        self.worker().deliver()
        self.assertEqual(self.row()["error_code"], "CDEK_REMINDER_NOT_APPLICABLE")
        self.assertEqual(FakeTransport.smtp_client.messages, [])

    def test_api_failure_defers_and_default_worker_cannot_bypass_guard(self):
        self.reminders.prepare([self.shipment])
        self.worker(guarded=False).deliver()
        self.assertEqual(self.row()["state"], "queued")
        self.api.get_order.side_effect = CdekError("CDEK_NETWORK", "Нет связи")
        self.worker().deliver()
        self.assertEqual(self.row()["state"], "queued")
        self.assertEqual(FakeTransport.smtp_client.messages, [])

    def test_latest_api_address_and_email_used_before_send(self):
        self.reminders.prepare([self.shipment])
        self.entity["recipient"]["email"] = "updated@example.test"
        self.api.get_delivery_point.return_value["location"]["address_full"] = "Новый адрес"
        self.worker().deliver()
        message, recipients = FakeTransport.smtp_client.messages[0]
        self.assertEqual(recipients, ["updated@example.test"])
        self.assertIn("Новый адрес", message.get_content())

    def test_smtp_ambiguous_result_is_not_retried(self):
        self.reminders.prepare([self.shipment])
        FakeTransport.smtp_client = FakeSMTP(TimeoutError())
        self.worker().deliver()
        self.assertEqual(self.row()["state"], "unknown")
        self.assertEqual(self.reminders.prepare([self.shipment])["skipped"], 1)

    def test_office_api_requires_exact_code_and_list_response(self):
        session = mock.Mock()
        session.request.side_effect = [response({"access_token": "test", "expires_in": 3600}),
                                      response([{"code": "MSK1", "work_time": "10–20"}])]
        client = CdekClient("test", "test", session=session)
        self.assertEqual(client.get_delivery_point("MSK1")["code"], "MSK1")
        self.assertEqual(session.request.call_args.kwargs["params"], {"code": "MSK1"})
        session.request.side_effect = [response([{"code": "OTHER"}])]
        with self.assertRaises(CdekError):
            client.get_delivery_point("MSK1")

    def test_cursor_moves_past_invalid_recipient(self):
        second = dict(self.shipment, id="shipment2", tracking="9999999999")
        self.entity["cdek_number"] = "9999999999"
        self.delivery.sync(second)
        good = copy.deepcopy(self.entity)
        bad = copy.deepcopy(good)
        bad.update(cdek_number="1234567890", recipient={})
        self.api.get_order.side_effect = lambda cdek_number: good if cdek_number == "9999999999" else bad
        self.assertEqual(self.reminders.prepare([self.shipment, second], limit=1)["errors"], 1)
        self.assertEqual(self.reminders.prepare([self.shipment, second], limit=1)["queued"], 1)

    def test_concurrent_queue_attempts_create_one_message_and_link(self):
        payload = self.reminders.build("1234567890")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: self.store.queue_outbox(
                payload, 0, payload["key"], order_ids=["42"]), range(2)))
        self.assertEqual(sum(created for row, created in results), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mail_outbox").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mail_links").fetchone()[0], 1)

    def test_disabled_reminders_do_not_block_manual_mail(self):
        self.reminders.prepare([self.shipment])
        self.store.queue_outbox({"to": "manual@example.test", "subject": "Ручное", "text_body": "Текст"}, 1, "manual")
        self.assertEqual(self.worker(guarded=False).deliver()["sent"], 1)
        self.assertEqual(self.row()["state"], "queued")

    def test_new_arrival_does_not_send_old_queued_reminder(self):
        self.reminders.prepare([self.shipment])
        self.entity["statuses"] += [dict(code="SENT_TO_RECIPIENT_CITY", date_time="2026-10-02T10:00:00Z"),
                                   dict(code="ACCEPTED_AT_PICK_UP_POINT", date_time="2026-10-03T10:00:00Z")]
        self.now += DELAY
        self.worker().deliver()
        self.assertEqual(FakeTransport.smtp_client.messages, [])

    def test_worker_loads_only_allowed_settings_and_requires_private_file(self):
        from scripts.mail_worker import load_environment
        path = self.root / "test-config"
        path.write_text("CDEK_ACCOUNT=test-account\nCDEK_EMAIL_REMINDERS_ENABLED=1\nERP_MAIL_SECRET_KEY=test-key\nUNRELATED_SETTING=forbidden\n", encoding="utf-8")
        with mock.patch("scripts.mail_worker.Path.stat", return_value=mock.Mock(st_uid=0, st_mode=0o100600)), mock.patch.dict(os.environ, {}, clear=True):
            load_environment(path)
            self.assertEqual(os.getenv("CDEK_EMAIL_REMINDERS_ENABLED"), "1")
            self.assertEqual(os.getenv("ERP_MAIL_SECRET_KEY"), "test-key")
            self.assertNotIn("UNRELATED_SETTING", os.environ)
        with mock.patch("scripts.mail_worker.Path.stat", return_value=mock.Mock(st_uid=0, st_mode=0o100644)):
            with self.assertRaises(ValueError):
                load_environment(path)


if __name__ == "__main__":
    unittest.main()
