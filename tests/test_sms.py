import json
import gc
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import requests

from app.clients.smsbliss import (
    SmsBlissClient,
    SmsBlissInvalidResponse,
    SmsBlissNotConfigured,
    SmsBlissSecurityError,
    SmsBlissUnavailable,
    SmsBlissUnknownDelivery,
)
from app.services.sms import (
    SmsService,
    SmsStore,
    SmsValidationError,
    internal_status,
    normalize_phone,
    render_template_text,
    sms_segments,
)
from app.sms_migrations import migrate_database, verify_database


class FakeProvider:
    configured = True
    masked_login = "u***r"
    queue_name = "erpQueue"

    def __init__(self, send_status="accepted", error=None):
        self.send_status = send_status
        self.error = error
        self.calls = 0

    def send(self, client_message_id, phone, text, sender="", scheduled_at=""):
        self.calls += 1
        if self.error:
            raise self.error("provider failure")
        return {"status": "ok", "messages": [{
            "clientId": client_message_id,
            "smscId": "ABC" + client_message_id[-24:],
            "status": self.send_status,
            "smsCount": 2,
            "msgCost": "5.40",
        }]}

    def statuses(self, messages):
        return {"status": "ok", "messages": [{
            "clientId": messages[0]["clientId"],
            "smscId": messages[0]["smscId"],
            "status": "delivered",
        }]}


class FakeResponse:
    def __init__(self, payload=None, status=200, json_error=False):
        self.payload = payload
        self.status_code = status
        self.json_error = json_error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("rejected")

    def json(self):
        if self.json_error:
            raise ValueError("invalid")
        return self.payload


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.last_url = ""
        self.last_json = None

    def post(self, url, json=None, timeout=None):
        self.last_url, self.last_json = url, json
        if self.error:
            raise self.error("network")
        return self.response


class SmsDomainTests(unittest.TestCase):
    def test_cdek_template_alias_missing_name_and_required_tracking(self):
        body = "{client_name}, Ваш заказ TicTacToy.ru ожидает получения ❤ Трекинг {Накладная} Отследить cdek.ru."
        self.assertTrue(render_template_text(body, {"tracking_number": "10313114965"}).startswith("Ваш заказ"))
        self.assertTrue(render_template_text(body, {"client_name": "Вячеслав", "tracking_number": "10313114965"}).startswith("Вячеслав, Ваш"))
        for value in ("", "1031,1032", "{Накладная}"):
            with self.assertRaises(SmsValidationError):
                render_template_text(body, {"tracking_number": value})

    def test_saved_template_reads_edits_and_honors_disabled_state(self):
        row = self.store.save_template(None, "СДЭК", "Трекинг {Накладная}", True, self.actor)
        values = {"tracking_number": "10313114965"}
        self.assertEqual(self.store.render_saved_template(row["id"], values), "Трекинг 10313114965")
        self.store.save_template(row["id"], "СДЭК", "Получите заказ: {tracking_number}", True, self.actor)
        self.assertEqual(self.store.render_saved_template(row["id"], values), "Получите заказ: 10313114965")
        self.store.save_template(row["id"], "СДЭК", "Трекинг {Накладная}", False, self.actor)
        with self.assertRaises(SmsValidationError):
            self.store.render_saved_template(row["id"], values)

    def test_unresolved_variables_never_reach_provider(self):
        provider = FakeProvider()
        with self.assertRaises(SmsValidationError):
            SmsService(self.store, provider).send(self.payload(text="Трекинг {Накладная}"), self.actor)
        self.assertEqual(provider.calls, 0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sms.db"
        migrate_database(self.path)
        self.store = SmsStore(self.path)
        self.actor = {"id": "17", "name": "Мария Иванова"}

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, client_id="msg-1", **values):
        result = {
            "client_message_id": client_id,
            "phone": "8 (999) 123-45-67",
            "text": "Ваш заказ готов",
            "sender": "Tictactoy",
            "customer_id": 12,
            "customer_name": "Клиент",
            "order_id": "551",
            "order_number": "551",
        }
        result.update(values)
        return result

    def test_migration_is_repeatable_and_quick_check_passes(self):
        migrate_database(self.path)
        verify_database(self.path)
        with closing(sqlite3.connect(str(self.path))) as connection:
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sms_templates").fetchone()[0], 5)

    def test_schema_avoids_post_sqlite_3717_features(self):
        source = (Path(__file__).parents[1] / "app" / "sms_migrations.py").read_text(encoding="utf-8").upper()
        for forbidden in (" ON CONFLICT ", " RETURNING ", " WITHOUT ROWID ", " STRICT "):
            self.assertNotIn(forbidden, source)
        self.assertNotRegex(source, r"CREATE\s+(UNIQUE\s+)?INDEX[^;]+\sWHERE\s")

    def test_phone_normalization_e164(self):
        self.assertEqual(normalize_phone("8 (999) 123-45-67"), "+79991234567")
        self.assertEqual(normalize_phone("9991234567"), "+79991234567")
        self.assertEqual(normalize_phone("+44 20 7946 0958"), "+442079460958")
        with self.assertRaises(SmsValidationError):
            normalize_phone("123")

    def test_gsm_and_unicode_segmentation(self):
        self.assertEqual(sms_segments("A" * 160)["segments"], 1)
        self.assertEqual(sms_segments("A" * 161)["segments"], 2)
        self.assertEqual(sms_segments("{" * 81)["segments"], 2)
        self.assertEqual(sms_segments("Я" * 70)["segments"], 1)
        self.assertEqual(sms_segments("Я" * 71)["segments"], 2)
        self.assertEqual(sms_segments("line\nline")["encoding"], "GSM-7")

    def test_template_variables_and_unknown_variable(self):
        text = render_template_text(
            "{client_name}: заказ {order_number}, {amount}; ремонт {repair_number}",
            {"client_name": "Анна", "order_number": "7", "amount": "100 ₽", "repair_number": "R-1"},
        )
        self.assertEqual(text, "Анна: заказ 7, 100 ₽; ремонт R-1")
        with self.assertRaises(SmsValidationError):
            render_template_text("{password}", {})

    def test_accepted_response_persists_provider_truth_and_author(self):
        message, submitted = SmsService(self.store, FakeProvider()).send(self.payload(), self.actor)
        self.assertTrue(submitted)
        self.assertEqual(message["status"], "accepted")
        self.assertEqual(message["smsc_id"], "ABCmsg-1")
        self.assertEqual(message["segments"], 2)
        self.assertEqual(message["cost"], "5.40")
        self.assertEqual(message["created_by_id"], "17")
        self.assertEqual(message["sent_by_name"], "Мария Иванова")

    def test_provider_rejections_have_failed_status(self):
        for index, status in enumerate(("not enough balance", "invalid mobile phone", "sender address invalid"), 1):
            message, _ = SmsService(self.store, FakeProvider(status)).send(self.payload("reject-{}".format(index)), self.actor)
            self.assertEqual(message["status"], "failed")
            self.assertTrue(message["error_description"])

    def test_timeout_invalid_json_and_unavailable_become_unknown_without_retry(self):
        for index, error in enumerate((SmsBlissUnknownDelivery, SmsBlissInvalidResponse, SmsBlissUnavailable), 1):
            provider = FakeProvider(error=error)
            message, _ = SmsService(self.store, provider).send(self.payload("unknown-{}".format(index)), self.actor)
            self.assertEqual(message["status"], "unknown")
            duplicate, submitted = SmsService(self.store, provider).send(self.payload("unknown-{}".format(index)), self.actor)
            self.assertFalse(submitted)
            self.assertEqual(provider.calls, 1)
            self.assertEqual(duplicate["id"], message["id"])

    def test_double_post_and_double_click_are_idempotent(self):
        provider = FakeProvider()
        service = SmsService(self.store, provider)
        first, first_submitted = service.send(self.payload(), self.actor)
        second, second_submitted = service.send(self.payload(), self.actor)
        self.assertTrue(first_submitted)
        self.assertFalse(second_submitted)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(provider.calls, 1)

    def test_delivered_status_sync_is_terminal(self):
        provider = FakeProvider()
        message, _ = SmsService(self.store, provider).send(self.payload(), self.actor)
        result = SmsService(self.store, provider).sync_statuses()
        self.assertEqual(result, {"checked": 1, "updated": 1})
        self.assertEqual(self.store.get(message_id=message["id"])["status"], "delivered")
        self.assertEqual(self.store.pending(), [])

    def test_filters_pagination_and_links(self):
        for index in range(25):
            SmsService(self.store, FakeProvider()).send(self.payload(
                "filter-{}".format(index), text="Заказ готов {}".format(index),
                customer_id=99, order_id="ORDER-9", order_number="ORDER-9",
            ), self.actor)
        listing = self.store.list({"q": "ORDER-9", "status": "accepted", "customer_id": 99, "page": 2, "per_page": 20})
        self.assertEqual(listing["total"], 25)
        self.assertEqual(listing["page"], 2)
        self.assertEqual(len(listing["rows"]), 5)

    def test_template_delete_deactivates_used_template(self):
        template = self.store.templates(active_only=True)[0]
        SmsService(self.store, FakeProvider()).send(self.payload(template_id=template["id"]), self.actor)
        self.assertFalse(self.store.delete_template(template["id"]))
        row = next(item for item in self.store.templates() if item["id"] == template["id"])
        self.assertEqual(row["active"], 0)


class SmsBlissClientTests(unittest.TestCase):
    def test_requires_credentials_and_https(self):
        with self.assertRaises(SmsBlissSecurityError):
            SmsBlissClient(login="x", password="y", base_url="http://api.smsbliss.net/messages/v2")
        client = SmsBlissClient(login="", password="", base_url="https://api.smsbliss.net/messages/v2")
        with self.assertRaises(SmsBlissNotConfigured):
            client.version()

    def test_server_posts_json_to_https_and_parses_success(self):
        session = FakeSession(FakeResponse({"status": "ok", "version": 2}))
        client = SmsBlissClient("login", "password", "https://api.smsbliss.net/messages/v2", session=session)
        self.assertEqual(client.version()["version"], 2)
        self.assertTrue(session.last_url.startswith("https://"))
        self.assertEqual(session.last_json["login"], "login")

    def test_timeout_and_corrupt_json_are_classified(self):
        timeout = SmsBlissClient("a", "b", session=FakeSession(error=requests.Timeout))
        with self.assertRaises(SmsBlissUnknownDelivery):
            timeout.send("id", "+79991234567", "text")
        corrupt = SmsBlissClient("a", "b", session=FakeSession(FakeResponse(json_error=True)))
        with self.assertRaises(SmsBlissInvalidResponse):
            corrupt.balance()


class SmsWebTests(unittest.TestCase):
    def test_manual_recipient_name_is_used_in_preview_and_send_including_empty(self):
        template = SmsStore(self.path).save_template(None, "Имя", "{client_name}, заказ готов", True, {"id": "1", "name": "Тест"})
        with self.client.session_transaction() as state:
            state["sms_cdek_selections"] = {"selected": {"name": "Из СДЭК", "tracking": "1234567890", "number": "7", "created_at": self.web.time.time()}}
        for source in ({}, {"order_id": "551"}, {"cdek_selection": "selected"}):
            for name in ("Мария", ""):
                with self.subTest(source=source, name=name), mock.patch.object(self.web, "OrdersSnapshotStore") as snapshots, mock.patch.object(self.web, "api_sales_records", return_value=[]), mock.patch.object(self.web, "sms_client", return_value=FakeProvider()):
                    snapshots.return_value.get.return_value = {"id": "551", "number": "551", "customer": "Из заказа"}
                    message_id = "manual-{}-{}".format(len(source), source.get("order_id") or source.get("cdek_selection") or "phone") + ("-name" if name else "-empty")
                    payload = dict(source, recipient_name_override=name, template_id=template["id"], phone="+79991234567", client_message_id=message_id)
                    preview = self.client.post("/api/v1/sms/templates/preview", json=payload)
                    expected = "Мария, заказ готов" if name else "заказ готов"
                    self.assertEqual(preview.get_json()["data"]["text"], expected)
                    self.assertEqual(self.client.post("/api/v1/sms/messages", json=payload).status_code, 200)
                    message = SmsStore(self.path).get(client_message_id=message_id)
                    self.assertEqual(message["message_text"], expected)
                    self.assertEqual(message["customer_name"], name)

    def test_cdek_without_erp_order_supplies_recipient_and_trusted_template_values(self):
        entity = {"cdek_number": "1234567890", "number": "SHOP-7", "recipient": {
            "name": "Анна", "phones": [{"number": "+79991234567"}]}}
        with mock.patch.object(self.web.CDEK_DELIVERY.client, "get_order", return_value=entity) as lookup:
            result = self.client.get("/api/v1/sms/cdek?number=1234567890")
            self.assertEqual(result.status_code, 200)
            data = result.get_json()["data"]
            self.assertEqual(data["phone"], "+79991234567")
            self.assertEqual(data["id"], "")
            lookup.assert_called_once_with(cdek_number="1234567890")
        template = SmsStore(self.path).save_template(None, "СДЭК", "{client_name}, накладная {tracking_number}", True, {"id": "1", "name": "Тест"})
        payload = {"cdek_selection": data["cdek_selection"], "template_id": template["id"],
                   "phone": data["phone"], "tracking_number": "FORGED", "customer_name": "FORGED",
                   "client_message_id": "cdek-direct-test"}
        provider = FakeProvider()
        with mock.patch.object(self.web, "sms_client", return_value=provider), mock.patch.object(self.web, "OrdersSnapshotStore") as orders:
            preview = self.client.post("/api/v1/sms/templates/preview", json=payload)
            self.assertEqual(preview.get_json()["data"]["text"], "Анна, накладная 1234567890")
            self.assertEqual(provider.calls, 0)
            self.assertEqual(self.client.post("/api/v1/sms/messages", json=payload).status_code, 200)
            orders.assert_not_called()
        payload["order_id"] = "551"
        self.assertEqual(self.client.post("/api/v1/sms/templates/preview", json=payload).status_code, 422)
        payload.pop("order_id")
        with self.client.session_transaction() as state:
            saved = state["sms_cdek_selections"]
            saved[data["cdek_selection"]]["created_at"] = 0
            state["sms_cdek_selections"] = saved
        self.assertEqual(self.client.post("/api/v1/sms/templates/preview", json=payload).status_code, 422)

    def test_cdek_lookup_missing_multiple_phones_and_errors(self):
        from app.clients.cdek import CdekError
        with mock.patch.object(self.web.CDEK_DELIVERY.client, "get_order") as lookup:
            self.assertEqual(self.client.get("/api/v1/sms/cdek?number=abc").status_code, 422)
            lookup.assert_not_called()
            for phones in ([], [{"number": "+79991234567"}, {"number": "+79991234568"}]):
                lookup.return_value = {"cdek_number": "1234567890", "recipient": {"phones": phones}}
                data = self.client.get("/api/v1/sms/cdek?number=1234567890").get_json()["data"]
                self.assertEqual(data["phone"], "")
            lookup.return_value = {"cdek_number": "9999999999"}
            self.assertEqual(self.client.get("/api/v1/sms/cdek?number=1234567890").status_code, 422)
            lookup.side_effect = CdekError("CDEK_NOT_FOUND", "Не найдено")
            self.assertEqual(self.client.get("/api/v1/sms/cdek?number=1234567890").status_code, 404)

    def test_order_search_uses_number_without_customer_and_preserves_local_id(self):
        from app.domain_schema_migrations import apply_domain_migrations
        from app.services.orders_snapshot import OrdersSnapshotStore
        snapshots = OrdersSnapshotStore(Path(self.temp.name) / "orders.db")
        apply_domain_migrations(snapshots.path, "orders", "sms-test")
        with mock.patch("app.services.orders_snapshot.link_order_safely", return_value={"customer_id": None}), mock.patch("app.services.orders_snapshot.publish_orders"):
            snapshots.replace([
                {"id": "local-1", "number": "00551", "phone": "+79991234567", "customer": "Анна", "created_at": "2026-10-01"},
                {"id": "local-2", "number": "005519", "phone": "", "created_at": "2026-10-01"},
                {"id": "local-3", "number": "888", "customer": "00551", "created_at": "2026-10-01"},
            ], 1000)
        with mock.patch.object(self.web, "OrdersSnapshotStore", return_value=snapshots), mock.patch.object(self.web, "customer_store") as customers:
            result = self.client.get("/api/v1/sms/orders", query_string={"q": "№00551"})
            self.assertEqual(result.status_code, 200)
            rows = result.get_json()["data"]
            self.assertEqual([row["id"] for row in rows], ["local-1", "local-2"])
            self.assertEqual(rows[0]["phone"], "+79991234567")
            self.assertEqual(rows[0]["name"], "Анна")
            self.assertEqual(rows[1]["name"], "")
            self.assertEqual(self.client.get("/api/v1/sms/orders?q=%25").get_json()["data"], [])
            self.assertEqual(self.client.get("/api/v1/sms/orders").get_json()["data"], [])
            self.assertEqual(self.client.get("/api/v1/sms/orders?order_id=missing").status_code, 404)
            self.assertEqual(self.client.get("/api/v1/sms/orders?order_id=local-1").get_json()["data"][0]["number"], "00551")
            customers.assert_not_called()

    def test_order_without_name_can_preview_and_send_without_customer(self):
        row = SmsStore(self.path).save_template(None, "Заказ", "{client_name}, заказ {order_number}", True, {"id": "1", "name": "Тест"})
        payload = {"client_message_id": "order-no-name", "order_id": "551", "phone": "+79991234567", "template_id": row["id"], "customer_name": "Старое имя"}
        provider = FakeProvider()
        with mock.patch.object(self.web, "OrdersSnapshotStore") as snapshots, mock.patch.object(self.web, "api_sales_records", return_value=[]), mock.patch.object(self.web, "sms_client", return_value=provider):
            snapshots.return_value.get.return_value = {"id": "551", "number": "551"}
            preview = self.client.post("/api/v1/sms/templates/preview", json=payload)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.get_json()["data"]["text"], "заказ 551")
            self.assertEqual(provider.calls, 0)
            self.assertEqual(self.client.post("/api/v1/sms/messages", json=payload).status_code, 200)
        self.assertEqual(SmsStore(self.path).get(client_message_id="order-no-name")["customer_name"], "")

    def test_order_search_error_is_visible(self):
        with mock.patch.object(self.web, "OrdersSnapshotStore") as snapshots:
            snapshots.return_value.search_by_number.side_effect = sqlite3.OperationalError("unavailable")
            self.assertEqual(self.client.get("/api/v1/sms/orders?q=551").status_code, 503)

    def test_cdek_preview_and_send_use_saved_template_and_source_order(self):
        store = SmsStore(self.path)
        row = store.save_template(None, "СДЭК", "{client_name}, Трекинг {Накладная}", True, {"id":"1", "name":"Тест"})
        payload = {"client_message_id":"cdek-test", "phone":"+79991234567", "order_id":"551",
                   "template_id":row["id"], "text":"FORGED", "client_name":"FORGED", "tracking_number":"99999"}
        provider = FakeProvider()
        order = {"id":"551", "number":"551", "customer":"Вячеслав", "tracking":"10313114965"}
        with mock.patch.object(self.web, "OrdersSnapshotStore") as snapshots, mock.patch.object(self.web, "sms_client", return_value=provider):
            snapshots.return_value.get.return_value = order
            preview = self.client.post("/api/v1/sms/templates/preview", json=payload)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.get_json()["data"]["text"], "Вячеслав, Трекинг 10313114965")
            self.assertEqual(provider.calls, 0)
            # An edit after preview must be picked up on send, rather than using browser text.
            store.save_template(row["id"], "СДЭК", "{client_name}, получите заказ {tracking_number}", True, {"id":"1", "name":"Тест"})
            result = self.client.post("/api/v1/sms/messages", json=payload)
            self.assertEqual(result.status_code, 200)
        self.assertEqual(store.get(client_message_id="cdek-test")["message_text"], "Вячеслав, получите заказ 10313114965")
        self.assertEqual(provider.calls, 1)

    def test_cdek_missing_source_tracking_cannot_use_browser_value(self):
        row = SmsStore(self.path).save_template(None, "СДЭК", "Трек {Накладная}", True, {"id":"1", "name":"Тест"})
        provider = FakeProvider()
        with mock.patch.object(self.web, "OrdersSnapshotStore") as snapshots, mock.patch.object(self.web, "sms_client", return_value=provider), mock.patch.object(self.web, "api_sales_records", return_value=[]):
            snapshots.return_value.get.return_value = {"id":"551", "customer":"Анна"}
            for route in ("/api/v1/sms/templates/preview", "/api/v1/sms/messages"):
                response = self.client.post(route, json={"order_id":"551", "template_id":row["id"], "tracking_number":"10313114965"})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(provider.calls, 0)

    def test_cdek_tracking_from_sales_rejects_ambiguous_shipments(self):
        row = SmsStore(self.path).save_template(None, "СДЭК", "Трек {Накладная}", True, {"id":"1", "name":"Тест"})
        sale = {"source":"tictactoy", "order_number":"551", "track_number":"10325754515"}
        with mock.patch.object(self.web, "OrdersSnapshotStore") as snapshots, mock.patch.object(self.web, "api_sales_records") as sales:
            snapshots.return_value.get.return_value = {"id":"551", "number":"551", "customer":"Анна"}
            sales.return_value = [sale, dict(sale), dict(sale, source="wildberries", track_number="99999")]
            payload = {"order_id":"551", "template_id":row["id"]}
            result = self.client.post("/api/v1/sms/templates/preview", json=payload)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()["data"]["text"], "Трек 10325754515")
            sales.return_value.append(dict(sale, track_number="10313114965"))
            self.assertEqual(self.client.post("/api/v1/sms/templates/preview", json=payload).status_code, 422)

    @classmethod
    def setUpClass(cls):
        cls.runtime = tempfile.TemporaryDirectory()
        root = Path(cls.runtime.name)
        os.environ["CATALOG_DATABASE_PATH"] = str(root / "catalog.db")
        os.environ["ERP_AUTH_DATABASE"] = str(root / "auth.db")
        os.environ["ERP_SMS_DATABASE"] = str(root / "sms-global.db")
        from app.schema_migrations import apply_migrations
        from app.domain_schema_migrations import apply_domain_migrations
        apply_migrations(root / "catalog.db", app_commit="sms-test")
        apply_domain_migrations(root / "auth.db", "auth", "sms-test")
        migrate_database(root / "sms-global.db")

    @classmethod
    def tearDownClass(cls):
        gc.collect()  # Release SQLite connection cycles before Windows removes test files.
        cls.runtime.cleanup()

    def setUp(self):
        from app import web
        self.web = web
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sms.db"
        migrate_database(self.path)
        web.app.config.update(TESTING=True, AUTH_TESTING=False, SMS_DATABASE=str(self.path))
        self.client = web.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_page_is_available_without_credentials_and_contains_no_secrets(self):
        with mock.patch.dict(os.environ, {
            "SMSBLISS_LOGIN": "", "SMSBLISS_PASSWORD": "",
            "SMSBLISS_API_BASE_URL": "https://api.smsbliss.net/messages/v2",
        }, clear=False):
            response = self.client.get("/app/sms")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Центр SMS".encode("utf-8"), response.data)
        self.assertNotIn(b"actual-provider-password", response.data.lower())
        self.assertIn(b"max-width: 720px", (Path(__file__).parents[1] / "app/static/css/sms.css").read_bytes())

    def test_send_endpoint_uses_session_actor_and_is_idempotent(self):
        provider = FakeProvider()
        payload = {
            "client_message_id": "web-idempotent",
            "phone": "+79991234567",
            "text": "Сервисное сообщение",
        }
        with mock.patch.object(self.web, "sms_client", return_value=provider):
            first = self.client.post("/api/v1/sms/messages", json=payload)
            second = self.client.post("/api/v1/sms/messages", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.get_json()["meta"]["duplicate"])
        self.assertTrue(second.get_json()["meta"]["duplicate"])
        self.assertEqual(provider.calls, 1)
        self.assertEqual(SmsStore(self.path).get(client_message_id="web-idempotent")["created_by_id"], "system")

    def test_missing_credentials_do_not_create_message(self):
        provider = FakeProvider()
        provider.configured = False
        with mock.patch.object(self.web, "sms_client", return_value=provider):
            response = self.client.post("/api/v1/sms/messages", json={
                "client_message_id": "no-creds", "phone": "+79991234567", "text": "test",
            })
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(SmsStore(self.path).get(client_message_id="no-creds"))

    def test_role_permissions_are_separate(self):
        with self.web.app.test_request_context("/app/sms"), mock.patch.object(self.web, "auth_is_enabled", return_value=True):
            employee = self.web.sms_permissions({"role": "employee"})
            owner = self.web.sms_permissions({"role": "admin"})
        self.assertTrue(employee["view"] and employee["send"])
        self.assertFalse(employee["manage_templates"] or employee["view_integration"])
        self.assertTrue(all(owner.values()))

    def test_response_does_not_expose_provider_credentials(self):
        store = SmsStore(self.path)
        message, _ = SmsService(store, FakeProvider()).send({
            "client_message_id": "detail-safe", "phone": "+79991234567", "text": "Не секрет",
        }, {"id": "1", "name": "Сотрудник"})
        response = self.client.get("/api/v1/sms/messages/{}".format(message["id"]))
        body = json.dumps(response.get_json(), ensure_ascii=False)
        self.assertNotIn("SMSBLISS_PASSWORD", body)
        self.assertNotIn("ABC123-password", body)


if __name__ == "__main__":
    unittest.main()
