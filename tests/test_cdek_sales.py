import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from flask import Flask, abort
from jinja2 import ChoiceLoader, DictLoader

from app.cdek_sales_routes import register_cdek_sales_routes, sales_return
from app.clients.cdek import CdekError
from app.services.cdek_delivery import CdekDelivery, MOSCOW
from app.services.cdek_sales import CdekSales, group_sales, shipment_id, short_status


def sale(number="123", track="1234567890", **changes):
    result = dict(id=number, source_key="tictactoy", order_number=number,
                  track_number=track, product_name="Watch")
    result.update(changes)
    return result


class SalesDeliveryTest(unittest.TestCase):
    def test_group_order_date_uses_earliest_valid_sale_date_in_moscow(self):
        rows = [sale(created_at="bad"), sale(created_at="2026-09-30T22:30:00Z"),
                sale(created_at="2026-09-29T12:00:00+03:00")]
        self.assertEqual(group_sales(rows)[0]["order_date"], "2026-09-29")
        self.assertEqual(group_sales(rows[:2])[0]["order_date"], "2026-10-01")
        self.assertEqual(group_sales(rows[:1])[0]["order_date"], "")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 10, 1, 10, tzinfo=MOSCOW).timestamp()
        self.api = mock.Mock(configured=True)
        self.delivery = CdekDelivery(self.temp.name, client=self.api, clock=lambda: self.now)
        self.service = CdekSales(self.delivery)
        self.sales = [sale()]

    def seed(self, code="ACCEPTED_AT_PICK_UP_POINT", age=5, is_return=False, sales=None):
        sales = sales or self.sales
        group = group_sales(sales)[0]
        at = datetime.fromtimestamp(self.now - age * 86400, MOSCOW).isoformat()
        self.api.get_order.return_value = dict(cdek_number=group["tracking"] or "1234567890", number=group["number"],
            is_return=is_return, statuses=[dict(code=code, name=code, date_time=at)])
        self.delivery.sync(group)
        return group

    def test_grouping_deduplicates_waybills_and_excludes_other_channels(self):
        sales = [sale(), sale(product_name="Second"), sale(source_key="amazon"), sale(source_key="wildberries")]
        previous = copy.deepcopy(sales)
        groups = group_sales(sales)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["items"], ["Watch", "Second"])
        self.assertEqual(sales, previous)
        self.seed(sales=sales)
        self.delivery.sync_pending(groups, sleep=lambda _: None)
        self.assertEqual(self.api.get_order.call_count, 1)

    def test_fallback_and_new_tracking_do_not_reuse_old_status(self):
        self.seed()
        changed = sale(track="9876543210")
        self.assertNotEqual(shipment_id(changed), shipment_id(self.sales[0]))
        self.assertEqual(self.service.rows([changed])[0]["label"], "Нет данных")
        self.assertEqual(self.service.rows([sale(track="")])[0]["label"], "Нет трека")

    def test_pickup_threshold_and_duplicate_events_keep_original_arrival(self):
        group = self.seed(age=2)
        self.assertEqual(self.service.rows(self.sales)[0]["priority"], 0)
        self.now += 86400
        self.seed(age=3)
        self.assertEqual(self.service.rows(self.sales)[0]["priority"], 1)
        self.now += 2 * 86400
        self.seed(age=5)
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row["priority"], 2)
        self.assertIn("5 сут.", row["wait"])
        self.assertIn("срок уточнить", row["wait"])
        self.assertEqual(group["id"], row["id"])

    def test_intermediate_warehouse_return_is_transit(self):
        self.seed("RETURNED_TO_RECIPIENT_CITY_WAREHOUSE", age=0)
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row["label"], "В пути")
        self.assertEqual(row["issues"], [])

    def test_terminal_and_locker_mapping(self):
        self.assertEqual(short_status({"status_code": "POSTOMAT_RECEIVED"}, "1"), ("Вручён", "green"))
        self.assertEqual(short_status({"status_code": "NOT_DELIVERED"}, "1"), ("Не вручён", "red"))
        self.assertEqual(short_status({"status_code": "POSTOMAT_SEIZED"}, "1"), ("Возврат", "red"))

    def test_review_persists_conflict_prevents_lost_update_and_no_network(self):
        group = self.seed()
        calls = self.api.get_order.call_count
        self.service.save_review(group, dict(version=0, work="working", note="Позвонили"), "employee1")
        with self.assertRaises(CdekError) as raised:
            self.service.save_review(group, dict(version=0, work="new", note="Lost"), "employee2")
        self.assertEqual(raised.exception.code, "CDEK_CONFLICT")
        row = CdekSales(self.delivery).rows(self.sales)[0]
        self.assertEqual(row["review"]["note"], "Позвонили")
        self.assertEqual(row["priority"], 2)
        self.assertEqual(self.api.get_order.call_count, calls)

    def test_confirmed_storage_deadline_and_rescheduling(self):
        group = self.seed(age=0)
        for field in ("storage_until", "expected_delivery"):
            with self.assertRaises(CdekError) as raised:
                self.service.save_review(group, dict(version=0, work="new", **{field: "2026-1-1"}), "1")
            self.assertEqual(raised.exception.code, "CDEK_FORM")
        self.service.save_review(group, dict(version=0, work="working", storage_until="2026-10-01"), "1")
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row["priority"], 2)
        self.assertEqual(row["review"]["followup"], "")
        self.service.save_review(group, dict(version=1, work="working", storage_until="2026-10-10"), "1")
        self.assertEqual(self.service.rows(self.sales)[0]["priority"], 0)

    def test_confirmed_delivery_date_and_unknown_status(self):
        group = self.seed("SENT_TO_RECIPIENT_CITY", age=0)
        self.service.save_review(group, dict(version=0, work="working", expected_delivery="2026-09-30"), "1")
        self.assertEqual(self.service.rows(self.sales)[0]["priority"], 2)
        self.now += 61
        self.seed("FUTURE_UNKNOWN_STATUS", age=0)
        self.assertTrue(any(i["category"] == "data" for i in self.service.rows(self.sales)[0]["issues"]))

    def test_repeated_pickup_events_do_not_reset_storage_clock(self):
        group = group_sales(self.sales)[0]
        self.api.get_order.return_value = dict(cdek_number=group["tracking"], statuses=[
            dict(code="ACCEPTED_AT_PICK_UP_POINT", name="В ПВЗ", date_time="2026-09-30T10:00:00+0300"),
            dict(code="ACCEPTED_AT_PICK_UP_POINT", name="В ПВЗ", date_time="2026-09-25T10:00:00+0300"),
            dict(code="SENT_TO_RECIPIENT_CITY", name="В пути", date_time="2026-09-24T10:00:00+0300")])
        self.delivery.sync(group)
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row["priority"], 2)
        self.assertIn("6 сут.", row["wait"])

    def test_legacy_states_keep_notes_and_clear_followups(self):
        group = self.seed(age=0)
        self.service.review_path.mkdir(parents=True, exist_ok=True)
        path = self.service.review_path / (group["id"] + ".json")
        for old, expected in [("today", "new"), ("tomorrow", "new")]:
            path.write_text(json.dumps(dict(version=3, work=old, note="Позвонили", followup="2026-09-01")), encoding="utf-8")
            row = self.service.rows(self.sales)[0]
            self.assertEqual(row["review"]["work"], expected)
            self.assertEqual(row["review"]["note"], "Позвонили")
            self.assertEqual(row["priority"], 0)
            with self.assertRaises(CdekError):
                self.service.save_review(group, dict(version=3, work=old), "1")

    def test_delivered_closes_pickup_problem_and_return_needs_confirmation(self):
        group = self.seed(age=6)
        self.service.save_review(group, dict(version=0, work="new"), "1")
        self.now += 61
        self.seed("DELIVERED", age=0)
        self.assertEqual(self.service.rows(self.sales)[0]["issues"], [])
        self.now += 61
        self.seed("DELIVERED", age=0, is_return=True)
        self.assertEqual(self.service.rows(self.sales)[0]["label"], "Возврат")
        path = self.service.review_path / (group["id"] + ".json")
        path.write_text(json.dumps(dict(version=2, work="closed", note="", closed_event=self.delivery.view(group)["date_display"])), encoding="utf-8")
        self.assertEqual(self.service.rows(self.sales)[0]["issues"], [])

    def test_verified_return_leaves_problems_but_remains_in_all_and_can_reopen(self):
        group = self.seed("DELIVERED", age=0, is_return=True)
        self.service.save_review(group, dict(version=0, work="closed", note="Возврат осмотрен"), "1")
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row["work"], "closed")
        self.assertFalse(row["issues"])
        active, counts = self.service.summary([row])
        self.assertEqual(len(active), 1)
        self.assertEqual(counts["problems"], 0)
        self.service.save_review(group, dict(version=1, work="new"), "1")
        self.assertTrue(self.service.rows(self.sales)[0]["issues"])

    def test_reaction_rules_and_working_override(self):
        for code, age, expected in [("ACCEPTED_AT_PICK_UP_POINT", 6, False),
                                    ("ACCEPTED_AT_PICK_UP_POINT", 7, True),
                                    ("SENT_TO_RECIPIENT_CITY", 8, False),
                                    ("NOT_DELIVERED", 0, True), ("DELIVERED", 0, False)]:
            self.now += 61
            group = self.seed(code, age=age)
            row = self.service.rows(self.sales)[0]
            self.assertEqual(row["needs_reaction"], expected)
            self.assertEqual(row["work"], "new" if expected else "")
        self.now += 61
        group = self.seed(age=0)
        self.service.save_review(group, dict(version=0, work="new", storage_until="2026-10-01"), "1")
        self.assertEqual(self.service.rows(self.sales)[0]["work"], "new")
        self.service.save_review(group, dict(version=1, work="working"), "1")
        self.assertEqual(self.service.rows(self.sales)[0]["work"], "working")
        self.now += 61
        self.seed("DELIVERED", age=0, is_return=True)
        self.assertTrue(self.service.rows(self.sales)[0]["needs_reaction"])

    def test_cannot_close_undelivered_return(self):
        group = self.seed("NOT_DELIVERED")
        with self.assertRaises(CdekError):
            self.service.save_review(group, dict(version=0, work="closed"), "1")

    def test_failed_refresh_preserves_status_and_flags_uncertainty(self):
        group = self.seed(age=0)
        self.now += 61
        self.api.get_order.side_effect = CdekError("CDEK_NETWORK", "Нет связи")
        with self.assertRaises(CdekError):
            self.delivery.sync(group)
        row = self.service.rows(self.sales)[0]
        self.assertEqual(row["label"], "В ПВЗ")
        self.assertTrue(row["stale"])
        self.assertEqual(row["issues"][0]["category"], "data")

    def test_invalid_tracking_no_credentials_and_unknown_status_are_visible(self):
        rows = self.service.rows([sale(track="12345")])
        self.assertTrue(rows[0]["issues"])
        self.api.configured = False
        self.assertIn("не подключён", self.service.rows(self.sales)[0]["issues"][0]["reason"])

    def test_corrupt_review_not_overwritten(self):
        group = self.seed()
        self.service.review_path.mkdir()
        path = self.service.review_path / (group["id"] + ".json")
        path.write_text("broken", encoding="utf-8")
        with self.assertRaises(CdekError):
            self.service.save_review(group, dict(version=0, work="working"), "1")
        self.assertEqual(path.read_text(), "broken")
        self.assertTrue(self.service.rows(self.sales)[0]["review_error"])

    def test_summary_counts_unique_shipments_not_product_rows(self):
        self.seed()
        rows, counts = self.service.summary(self.service.rows(self.sales * 3))
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["problems"], 1)
        self.assertEqual(counts["В ПВЗ"], 1)


class CdekSalesRoutesTest(unittest.TestCase):
    seed = SalesDeliveryTest.seed

    def setUp(self):
        SalesDeliveryTest.setUp(self)
        self.app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "app/templates"))
        self.app.config["TESTING"] = True
        self.app.jinja_loader = ChoiceLoader([DictLoader({"_sidebar.html": "", "_favicon.html": ""}), self.app.jinja_loader])
        self.app.jinja_env.globals["csrf_token"] = lambda: "test-token"
        self.allowed = mock.Mock(return_value=True)
        self.csrf = mock.Mock()
        self.find_orders = mock.Mock(return_value=[])
        self.app.add_url_rule("/order/<int:order_id>", "order_page", lambda order_id: "order")
        self.app.add_url_rule("/orders", "orders_page", lambda: "orders")
        register_cdek_sales_routes(self.app, self.service, lambda: self.sales, self.allowed, self.csrf, lambda: "employee", find_orders=self.find_orders)
        self.client = self.app.test_client()

    def test_manager_options_and_colored_states_match(self):
        group = self.seed(age=7)
        for state, tone, label in [("new", "red", "Нужна реакция"), ("working", "blue", "В работе")]:
            version = self.service.review(group["id"])["version"]
            self.service.save_review(group, dict(version=version, work=state), "1")
            html = self.client.get("/sales/cdek?shipment=" + group["id"] + "&work=" + state).get_data(as_text=True)
            self.assertIn('cdek-badge cdek-' + tone + '">' + label, html)
            for removed in ("today", "tomorrow"):
                self.assertNotIn('value="' + removed + '"', html)
            self.assertNotIn("Повторный контакт", html)

    def test_order_links_resolve_exact_number_without_assuming_it_is_id(self):
        self.find_orders.return_value = [{"id": "99", "number": "123"}, {"id": "123", "number": "456"}]
        response = self.client.get("/sales/cdek/order/123")
        self.assertEqual(response.location, "/order/99")
        self.find_orders.return_value = []
        self.assertIn("/orders?source=tictactoy&q=123", self.client.get("/sales/cdek/order/123").location)
        self.find_orders.return_value = [{"id": "99", "number": "123"}, {"id": "98", "number": "123"}]
        self.assertIn("/orders?", self.client.get("/sales/cdek/order/123").location)
        self.sales = [sale("123"), sale("456")]
        html = self.client.get("/sales/cdek?mode=all").get_data(as_text=True)
        self.assertIn('href="/sales/cdek/order/123"', html)
        self.assertIn('href="/sales/cdek/order/456"', html)
        self.assertIn('class="cdek-button cdek-open"', html)
        self.allowed.return_value = False
        self.assertEqual(self.client.get("/sales/cdek/order/123").status_code, 403)

    def test_page_uses_cache_only_filters_details_escapes_and_returns_to_sales(self):
        group = self.seed()
        self.service.save_review(group, dict(version=0, work="working", note="<script>unsafe</script>"), "1")
        before = self.api.get_order.call_count
        response = self.client.get("/sales/cdek", query_string={"shipment": group["id"], "back": "/sales?source=tictactoy&page=3&q=watch"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"&lt;script&gt;", response.data)
        self.assertIn(b"page=3", response.data)
        self.assertEqual(self.api.get_order.call_count, before)
        self.assertIn("Найдено 0", self.client.get("/sales/cdek?q=missing").get_data(as_text=True))

    def test_pagination_all_and_filters_do_not_change_summary(self):
        self.sales = [sale(str(i), str(10324000000+i)) for i in range(60)]
        response = self.client.get("/sales/cdek?mode=all&page=2&per_page=25")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Страница 2 из 3", response.get_data(as_text=True))
        self.assertIn("Все отправления · 60", response.get_data(as_text=True))

    def test_order_sort_is_numeric_before_pagination_and_keeps_missing_last(self):
        self.sales = [sale(str(i), str(10324000000+i), created_at="2026-09-30T12:00:00")
                      for i in range(30, 0, -1)] + [sale("", "10324999999")]
        html = self.client.get("/sales/cdek?mode=all&sort=order_asc").get_data(as_text=True)
        self.assertLess(html.index(">2</a>"), html.index(">10</a>"))
        self.assertNotIn(">26</a>", html)
        self.assertIn("30.09.2026", html)
        self.assertIn("sort=order_asc", html)
        html = self.client.get("/sales/cdek?mode=all&sort=order_desc&page=2").get_data(as_text=True)
        self.assertLess(html.index(">5</a>"), html.index(">1</a>"))
        self.assertLess(html.index(">1</a>"), html.index(">Без номера заказа</span>"))
        self.assertIn('name="sort" value="order_desc"', self.client.get(
            "/sales/cdek?mode=all&sort=order_desc&shipment=" + shipment_id(self.sales[0])
        ).get_data(as_text=True))

    def test_unknown_sort_falls_back_to_priority(self):
        html = self.client.get("/sales/cdek?sort=unknown").get_data(as_text=True)
        self.assertIn('value="priority" selected', html)
        self.assertIn("сначала срочные", html)

    def test_order_date_sort_precedes_pagination_and_missing_dates_are_last(self):
        self.sales = [sale(str(i), str(10324000000+i), created_at="2026-09-{:02d}".format(i))
                      for i in range(1, 31)] + [sale("undated", "10324999999")]
        for sort, first, second in (("date_desc", 30, 29), ("date_asc", 1, 2)):
            html = self.client.get("/sales/cdek?mode=all&sort=" + sort).get_data(as_text=True)
            self.assertLess(html.index(">{}</a>".format(first)), html.index(">{}</a>".format(second)))
            self.assertNotIn(">undated</a>", html)
            self.assertIn("sort=" + sort, html)
            html = self.client.get("/sales/cdek?mode=all&page=2&sort=" + sort).get_data(as_text=True)
            last = 1 if sort == "date_desc" else 30
            self.assertLess(html.index(">{}</a>".format(last)), html.index(">undated</a>"))

    def test_auth_csrf_and_membership(self):
        group = self.seed()
        self.allowed.return_value = False
        self.assertEqual(self.client.get("/sales/cdek").status_code, 403)
        self.assertEqual(self.client.post("/sales/cdek/{}/review".format(group["id"])).status_code, 403)
        self.allowed.return_value = True
        self.csrf.side_effect = lambda: abort(400)
        self.assertEqual(self.client.post("/sales/cdek/{}/sync".format(group["id"])).status_code, 400)
        self.csrf.side_effect = None
        self.assertEqual(self.client.post("/sales/cdek/unknown/review").status_code, 404)

    def test_review_post_conflict_preserves_submitted_note(self):
        group = self.seed()
        url = "/sales/cdek/{}/review".format(group["id"])
        form = dict(version=0, review_work="working", note="first")
        self.assertEqual(self.client.post(url, data=form).status_code, 302)
        form["note"] = "unsaved <note>"
        response = self.client.post(url, data=form)
        self.assertEqual(response.status_code, 409)
        self.assertIn(b"unsaved &lt;note&gt;", response.data)

    def test_safe_return_url(self):
        for target in ("https://evil.example", "//evil.example", "/sales\\evil", "/settings", "/sales\n"):
            self.assertEqual(sales_return(target), "/sales?source=tictactoy")
        self.assertEqual(sales_return("/sales?q=a&page=2"), "/sales?q=a&page=2")


if __name__ == "__main__":
    unittest.main()
