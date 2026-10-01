import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests
from flask import Flask, abort, render_template

from app.cdek_routes import register_cdek_routes
from app.clients.cdek import CdekClient, CdekError
from app.services.cdek_delivery import CdekDelivery, delivery_reference, normalize_delivery
from scripts.cdek_delivery_sync import tracked_cards


def response(payload, status=200):
    result = mock.Mock(status_code=status)
    result.json.return_value = payload
    return result


def shipment(number="1234567890", shop="123"):
    return {
        "cdek_number": number, "number": shop, "is_return": False,
        "recipient": {"name": "PRIVATE", "phones": ["PRIVATE"]},
        "statuses": [
            {"code": "CREATED", "name": "Создан", "date_time": "2026-09-30T10:00:00+0000"},
            {"code": "DELIVERED", "name": "Вручен", "date_time": "2026-10-01T10:00:00+0000", "city": "Москва"},
            {"code": "REMOVED", "name": "Удалён", "date_time": "2026-10-02T10:00:00+0000", "deleted": True},
        ],
    }


class CdekClientTest(unittest.TestCase):
    def setUp(self):
        self.session = mock.Mock()
        self.token = response({"access_token": "token-secret", "expires_in": 3600})
        self.session.request.side_effect = [self.token, response({"entity": shipment()})]
        self.client = CdekClient("account-secret", "password-secret", session=self.session, clock=lambda: 100)

    def test_auth_and_order_reads_use_fixed_origin_body_and_no_redirects(self):
        result = self.client.get_order(cdek_number="1234567890")
        self.assertEqual(result["number"], "123")
        auth, read = self.session.request.call_args_list
        self.assertEqual(auth.args, ("POST", "https://api.cdek.ru/v2/oauth/token"))
        self.assertEqual(auth.kwargs["data"]["client_secret"], "password-secret")
        self.assertNotIn("params", auth.kwargs)
        self.assertEqual(read.args, ("GET", "https://api.cdek.ru/v2/orders"))
        self.assertEqual(read.kwargs["params"], {"cdek_number": "1234567890"})
        self.assertFalse(read.kwargs["allow_redirects"])
        self.assertFalse(auth.kwargs["allow_redirects"])

    def test_shop_lookup_reuses_token(self):
        self.session.request.side_effect = [self.token, response({"entity": shipment()}), response({"entity": shipment()})]
        self.client.get_order(im_number="123")
        self.client.get_order(im_number="123")
        self.assertEqual(self.session.request.call_count, 3)
        self.assertEqual(self.session.request.call_args.kwargs["params"], {"im_number": "123"})

    def test_expired_access_token_is_refreshed_once(self):
        self.session.request.side_effect = [self.token, response({}, 401), self.token, response({"entity": shipment()})]
        self.client.get_order(im_number="123")
        self.assertEqual(self.session.request.call_count, 4)

    def test_wrong_order_is_rejected(self):
        with self.assertRaises(CdekError) as raised:
            self.client.get_order(im_number="456")
        self.assertEqual(raised.exception.code, "CDEK_MISMATCH")

    def test_http_failures_are_safe_and_not_retried_indefinitely(self):
        for status in (301, 400, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.session.request.side_effect = None
                self.session.request.return_value = response({"secret": "DO-NOT-PRINT"}, status)
                client = CdekClient("x", "y", session=self.session)
                with self.assertRaises(CdekError) as raised:
                    client.get_order(im_number="123")
                self.assertNotIn("DO-NOT-PRINT", str(raised.exception))

    def test_network_errors_and_invalid_json_do_not_disclose_secrets(self):
        self.session.request.side_effect = requests.Timeout("password-secret")
        with self.assertRaises(CdekError) as raised:
            self.client.get_order(im_number="123")
        self.assertNotIn("password-secret", str(raised.exception))
        bad = response({})
        bad.json.side_effect = ValueError("token-secret")
        self.session.request.side_effect = [bad]
        with self.assertRaises(CdekError) as raised:
            self.client.get_order(im_number="123")
        self.assertNotIn("token-secret", str(raised.exception))

    def test_not_configured_makes_no_requests(self):
        with self.assertRaises(CdekError):
            CdekClient("", "", session=self.session).get_order(im_number="123")
        self.session.request.assert_not_called()

    def test_missing_entity_is_not_a_success(self):
        self.session.request.side_effect = [self.token, response({"requests": [{"errors": [{}]}]})]
        with self.assertRaises(CdekError):
            self.client.get_order(im_number="123")

    def test_nonfinite_token_expiration_is_rejected(self):
        self.session.request.side_effect = [response({"access_token": "token-secret", "expires_in": "NaN"})]
        with self.assertRaises(CdekError):
            self.client.get_order(im_number="123")


class CdekDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = mock.Mock(configured=True)
        self.api.get_order.return_value = shipment()
        self.now = 100000.0
        self.service = CdekDelivery(self.temp.name, self.api, lambda: self.now)
        self.order = {"id": "123", "number": "123", "source": "tictactoy", "status": "A", "paid": True}

    def test_newest_non_deleted_event_and_moscow_time(self):
        data = normalize_delivery(shipment())
        self.assertEqual(data["status_code"], "DELIVERED")
        self.assertEqual(data["date_display"], "01.10.2026 13:00")
        self.assertEqual(len(data["events"]), 2)
        self.assertNotIn("PRIVATE", json.dumps(data))

    def test_invalid_history_does_not_produce_a_false_status(self):
        entity = shipment()
        entity["statuses"][1]["date_time"] = "unknown"
        with self.assertRaises(CdekError):
            normalize_delivery(entity)

    def test_sync_keeps_business_order_unchanged_and_minimizes_cache(self):
        original = copy.deepcopy(self.order)
        self.service.sync(self.order)
        self.assertEqual(self.order, original)
        self.assertEqual(self.service.view(self.order)["status"], "Вручен")
        self.assertNotIn("PRIVATE", next(Path(self.temp.name).glob("*.json")).read_text())

    def test_read_view_never_calls_api_and_duplicate_sync_is_coalesced(self):
        self.assertFalse(self.service.view(self.order).get("checked_at"))
        self.api.get_order.assert_not_called()
        self.service.sync(self.order)
        self.service.sync(self.order)
        self.assertEqual(self.api.get_order.call_count, 1)

    def test_error_preserves_last_success_and_sets_visible_warning(self):
        self.service.sync(self.order)
        self.now += 100
        self.api.get_order.side_effect = CdekError("CDEK_NETWORK", "Недоступен")
        with self.assertRaises(CdekError):
            self.service.sync(self.order)
        view = self.service.view(self.order)
        self.assertEqual(view["status"], "Вручен")
        self.assertEqual(view["error"], "Недоступен")
        self.assertEqual(view["checked_at"], 100000)

    def test_changed_waybill_hides_old_status_and_uses_new_reference(self):
        self.service.sync(self.order)
        self.order["tracking"] = "9876543210"
        self.assertNotIn("status", self.service.view(self.order))
        self.api.get_order.return_value = shipment("9876543210")
        self.service.sync(self.order)
        self.api.get_order.assert_called_with(cdek_number="9876543210")

    def test_invalid_track_and_wrong_source_never_query_api(self):
        for change in ({"tracking": "https://example.com"}, {"source": "wildberries"}):
            with self.assertRaises(CdekError):
                self.service.sync(dict(self.order, **change))
        self.api.get_order.assert_not_called()

    def test_corrupt_cache_is_preserved(self):
        path = self.service._path("123")
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(CdekError):
            self.service.sync(self.order)
        self.assertEqual(path.read_text(), "broken")
        self.api.get_order.assert_not_called()

    def test_lock_prevents_concurrent_poll_and_manual_refresh(self):
        other = CdekDelivery(self.temp.name, self.api)
        with self.service.lock():
            with self.assertRaises(CdekError) as raised:
                other.sync(self.order)
        self.assertEqual(raised.exception.code, "CDEK_BUSY")
        self.api.get_order.assert_not_called()

    def test_valid_json_with_corrupt_timestamp_is_preserved(self):
        path = self.service._path("123")
        content = json.dumps({"order_id": "123", "reference": {"im_number": "123"}, "attempted_at": "broken"})
        path.write_text(content, encoding="utf-8")
        self.assertIn("Повреждены", self.service.view(self.order)["error"])
        with self.assertRaises(CdekError):
            self.service.sync(self.order)
        self.assertEqual(path.read_text(), content)

    def test_polling_backoff_and_terminal_daily_recheck(self):
        self.service.sync(self.order)
        self.now += 700
        result = self.service.sync_pending([self.order], sleep=lambda _: None)
        self.assertEqual(result["skipped"], 1)
        self.now += 86400
        self.service.sync_pending([self.order], sleep=lambda _: None)
        self.assertEqual(self.api.get_order.call_count, 2)

    def test_polling_fairness_and_invalid_references_do_not_starve_valid_orders(self):
        self.api.get_order.side_effect = CdekError("CDEK_NOT_FOUND", "Нет отправления")
        orders = [dict(self.order, id=str(i), number=str(i)) for i in range(3)]
        invalid = dict(self.order, tracking="not-a-waybill")
        self.service.sync_pending([invalid] + orders, limit=1, sleep=lambda _: None)
        self.service.sync_pending([invalid] + orders, limit=1, sleep=lambda _: None)
        self.assertEqual(self.api.get_order.call_args_list, [mock.call(im_number="0"), mock.call(im_number="1")])

    def test_rate_limit_stops_batch(self):
        self.api.get_order.side_effect = CdekError("CDEK_RATE_LIMIT", "Лимит")
        self.service.sync_pending([self.order, dict(self.order, id="124")], sleep=lambda _: None)
        self.assertEqual(self.api.get_order.call_count, 1)


class TrackedCardsTest(unittest.TestCase):
    def test_archive_is_not_enqueued_and_sales_cache_is_not_a_card(self):
        with tempfile.TemporaryDirectory() as folder:
            delivery = CdekDelivery(folder, client=mock.Mock(configured=True))
            load_order = mock.Mock(return_value={"id": "123", "source": "tictactoy"})
            self.assertEqual(tracked_cards(delivery, load_order), [])
            load_order.assert_not_called()
            delivery.client.get_order.return_value = shipment()
            delivery.sync({"id": "123", "tracking": "1234567890"})
            delivery.sync({"id": "a" * 64, "tracking": "1234567890"})
            self.assertEqual(tracked_cards(delivery, load_order), [load_order.return_value])
            load_order.assert_called_once_with("123")

    def test_corrupt_cache_is_not_silently_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "broken.json").write_text("broken", encoding="utf-8")
            with self.assertRaises(CdekError):
                tracked_cards(CdekDelivery(folder), mock.Mock())


class CdekRoutesTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "app/templates"))
        self.app.config["TESTING"] = True
        self.service = mock.Mock()
        self.can_view = mock.Mock(return_value=True)
        self.csrf = mock.Mock()
        self.load = mock.Mock(return_value={"id": "123", "number": "123"})
        self.app.add_url_rule("/order/<int:order_id>", "order_page", lambda order_id: "Order")
        register_cdek_routes(self.app, self.service, self.can_view, self.csrf, self.load, lambda order: "1234567890")
        self.client = self.app.test_client()

    def test_refresh_checks_access_csrf_and_redirects_to_card(self):
        result = self.client.post("/order/123/cdek/sync")
        self.assertEqual(result.status_code, 302)
        self.csrf.assert_called_once()
        self.service.sync.assert_called_once_with(self.load.return_value, tracking="1234567890")
        self.assertIn("/order/123?", result.location)

    def test_denied_role_and_csrf_cannot_call_cdek(self):
        self.can_view.return_value = False
        self.assertEqual(self.client.post("/order/123/cdek/sync").status_code, 403)
        self.can_view.return_value = True
        self.csrf.side_effect = lambda: abort(400)
        self.assertEqual(self.client.post("/order/123/cdek/sync").status_code, 400)
        self.service.sync.assert_not_called()

    def test_unknown_order_and_get_cannot_sync(self):
        self.load.return_value = None
        self.assertEqual(self.client.post("/order/123/cdek/sync").status_code, 404)
        self.assertEqual(self.client.get("/order/123/cdek/sync").status_code, 405)
        self.service.sync.assert_not_called()

    def test_template_escapes_external_text_and_shows_cached_error(self):
        data = normalize_delivery(shipment())
        data.update(configured=True, checked_at=100, checked_display="today", error="Нет связи", status="<script>bad</script>")
        with self.app.test_request_context():
            markup = render_template("_cdek_delivery.html", cdek_delivery=data, is_wb=False, detail_id=123, csrf_token=lambda: "csrf")
        self.assertIn("&lt;script&gt;", markup)
        self.assertIn("Показаны ранее полученные данные", markup)
        self.assertIn('name="csrf_token"', markup)


if __name__ == "__main__":
    unittest.main()
