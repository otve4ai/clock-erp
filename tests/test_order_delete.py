import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flask import render_template

from app import web
from app.domain_schema_migrations import apply_domain_migrations
from app.services.orders_snapshot import OrdersSnapshotStore


def order(order_id="21166"):
    return {
        "id": order_id,
        "external_id": order_id,
        "external_order_id": order_id,
        "number": order_id,
        "source": "tictactoy",
        "status": "N",
        "customer": "Тестовый покупатель",
        "created_at": "2026-09-23 04:42:00",
        "order_total": 39102,
        "products": [{"id": "line-1", "name": "RAY Blue", "quantity": 1}],
    }


class OrderDeleteTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = OrdersSnapshotStore(
            Path(self.temporary.name) / "orders.db"
        )
        apply_domain_migrations(self.store.path, "orders", "test")
        self.store.replace([order()], 1000)
        self.original_config = dict(web.app.config)
        web.app.config.update(TESTING=True, AUTH_TESTING=False)
        self.client = web.app.test_client()

    def tearDown(self):
        web.app.config.clear()
        web.app.config.update(self.original_config)
        self.temporary.cleanup()

    def test_local_delete_is_permanent_across_source_sync(self):
        deleted = self.store.delete_local(
            "tictactoy", "21166", actor_id="1", actor_name="Администратор"
        )

        self.assertEqual(deleted["number"], "21166")
        self.assertIsNone(self.store.get_by_identity("tictactoy", "21166"))
        self.assertEqual(self.store.deleted_external_ids("tictactoy"), {"21166"})

        result = self.store.replace([order()], 1001)

        self.assertEqual(result["skipped"], 1)
        self.assertIsNone(self.store.get("21166"))

    def test_admin_delete_succeeds_without_touching_related_data(self):
        inventory = mock.Mock()
        inventory.find_active_sale.return_value = None
        journal = mock.Mock()
        with (
            mock.patch.object(web, "auth_is_enabled", return_value=True),
            mock.patch.object(
                web, "current_auth_user",
                return_value={"id": 1, "role": "admin", "email": "admin@example.test"},
            ),
            mock.patch.object(web, "require_csrf_when_authenticated"),
            mock.patch.object(web, "OrdersSnapshotStore", return_value=self.store),
            mock.patch.object(web, "SalesInventory", return_value=inventory),
            mock.patch.object(web, "has_legacy_order_stock_writeoff", return_value=False),
            mock.patch.object(web, "current_sales_user_name", return_value="Администратор"),
            mock.patch.object(web, "AuditJournal", return_value=journal),
            mock.patch.object(web, "_forget_tictactoy_order_cache"),
        ):
            response = self.client.delete("/api/orders/tictactoy/21166")
            repeated = self.client.delete("/api/orders/tictactoy/21166")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["result"]["deleted"])
        self.assertEqual(repeated.status_code, 404)
        self.assertIsNone(self.store.get("21166"))
        journal.record.assert_called_once()

    def test_employee_cannot_see_or_call_delete(self):
        with (
            web.app.test_request_context("/app/orders"),
            mock.patch.object(web, "auth_is_enabled", return_value=True),
            mock.patch.object(
                web, "current_auth_user", return_value={"id": 2, "role": "employee"}
            ),
        ):
            self.assertFalse(web.can_delete_orders())
            html = render_template(
                "_orders_list_results.html",
                orders=[order()], selected_order=None, orders_query_args={},
                orders_total=1, orders_page=1, orders_page_size=50,
                orders_page_sizes=(20, 50, 100, 200), orders_page_count=1,
                orders_page_items=[1], order_kpis={}, order_source_counts={},
                order_status_counts={}, sync_error="", exact_search=None,
                orders_clear_url="/app/orders", can_delete_orders=False,
            )
        self.assertNotIn("Удалить заказ", html)

        with (
            mock.patch.object(web, "auth_is_enabled", return_value=True),
            mock.patch.object(
                web, "current_auth_user", return_value={"id": 2, "role": "employee"}
            ),
        ):
            response = self.client.delete("/api/orders/tictactoy/21166")
        self.assertEqual(response.status_code, 403)
        self.assertIsNotNone(self.store.get("21166"))

    def test_active_sale_blocks_delete_with_reason(self):
        inventory = mock.Mock()
        inventory.find_active_sale.return_value = {"id": "sale-1"}
        with (
            mock.patch.object(web, "auth_is_enabled", return_value=False),
            mock.patch.object(web, "OrdersSnapshotStore", return_value=self.store),
            mock.patch.object(web, "SalesInventory", return_value=inventory),
            mock.patch.object(web, "has_legacy_order_stock_writeoff", return_value=False),
        ):
            response = self.client.delete("/api/orders/tictactoy/21166")

        self.assertEqual(response.status_code, 409)
        self.assertIn("продаж", response.get_json()["error"]["message"])
        self.assertIsNotNone(self.store.get("21166"))

    def test_storage_error_keeps_order_and_returns_safe_message(self):
        inventory = mock.Mock()
        inventory.find_active_sale.return_value = None
        with (
            mock.patch.object(web, "auth_is_enabled", return_value=False),
            mock.patch.object(web, "OrdersSnapshotStore", return_value=self.store),
            mock.patch.object(web, "SalesInventory", return_value=inventory),
            mock.patch.object(web, "has_legacy_order_stock_writeoff", return_value=False),
            mock.patch.object(
                self.store, "delete_local",
                side_effect=sqlite3.OperationalError("disk failure"),
            ),
        ):
            response = self.client.delete("/api/orders/tictactoy/21166")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.get_json()["error"]["message"],
            "Не удалось удалить заказ. Заказ остался в ERP.",
        )
        self.assertIsNotNone(self.store.get("21166"))

    def test_admin_link_and_confirm_dialog_contract(self):
        with web.app.test_request_context("/app/orders"):
            html = render_template(
                "_orders_list_results.html",
                orders=[order()], selected_order=None, orders_query_args={},
                orders_total=1, orders_page=1, orders_page_size=50,
                orders_page_sizes=(20, 50, 100, 200), orders_page_count=1,
                orders_page_items=[1], order_kpis={}, order_source_counts={},
                order_status_counts={}, sync_error="", exact_search=None,
                orders_clear_url="/app/orders", can_delete_orders=True,
            )
        self.assertIn("Удалить заказ", html)
        self.assertIn("data-open-order-delete", html)

        template = (Path(web.app.root_path) / "templates" / "orders.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("Удалить заказ №${deleteTarget.number}?", template)
        self.assertIn(
            "Заказ исчезнет из списка. Это действие нельзя отменить.", template
        )
        self.assertIn("if(!deleteTarget||deleteConfirm.disabled)return", template)


if __name__ == "__main__":
    unittest.main()
