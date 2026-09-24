from pathlib import Path
import unittest
from unittest import mock

import test_sales_inventory as sales_tests
import test_order_tictactoy_sale as order_tests
from app import web
from app.services.required_straps import RequiredStraps
from app.services.sales_inventory import SalesInventoryError, InsufficientStockError
from app.services.shared_catalog import SharedCatalog
from app.services.component_inventory import ComponentInventory, balance


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RequiredStrapUiContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template = (PROJECT_ROOT / "app/templates/warehouse.html").read_text(
            encoding="utf-8"
        )
        cls.styles = (PROJECT_ROOT / "app/static/css/warehouse.css").read_text(
            encoding="utf-8"
        )
        cls.script = (PROJECT_ROOT / "app/static/js/required-strap.js").read_text(
            encoding="utf-8"
        )

    def test_setting_is_collapsed_and_only_visible_while_editing(self):
        self.assertIn(
            'class="product-additional-settings" data-product-additional-settings',
            self.template,
        )
        self.assertNotIn(
            'data-product-additional-settings open', self.template
        )
        self.assertIn("Дополнительные настройки", self.template)
        self.assertIn("Ремешок обязателен при продаже", self.template)
        self.assertIn("Для продажи из заказа", self.template)
        self.assertIn(
            ".product-inline-form.is-editing .product-additional-settings",
            self.styles,
        )
        self.assertIn(
            "#editDrawer .product-setting-row", self.styles
        )
        self.assertIn("@media (max-width: 430px)", self.styles)

    def test_toggle_keeps_immediate_persistence(self):
        self.assertIn("checkbox.addEventListener('change'", self.script)
        self.assertIn("method: 'PUT'", self.script)
        self.assertIn("Сохранено сразу.", self.script)
        self.assertIn("checkbox.disabled = !canManage", self.script)
        self.assertIn("if (!status) return", self.script)


class RequiredStrapInventoryTest(unittest.TestCase):
    setUp = sales_tests.SalesInventoryTest.setUp
    tearDown = sales_tests.SalesInventoryTest.tearDown
    create_product = sales_tests.SalesInventoryTest.create_product
    stock = sales_tests.SalesInventoryTest.stock
    payload = staticmethod(sales_tests.SalesInventoryTest.payload)

    def setup_body(self, strap_stock=4):
        body = self.create_product(5, "Корпус", "BODY")
        strap = self.catalog.create_product(name="Ремешок", article="STRAP",
                                            brand="Brand", category="Ремешки", stock=strap_stock)
        RequiredStraps(self.database).configure(body["id"], True)
        return body, strap

    def sell(self, body, strap=None, quantity=1, **kwargs):
        return self.inventory.create_sale_batch(self.payload(body), [{
            "product_id": body["id"], "quantity": quantity, "unit_price": 100,
            "strap_product_id": strap["id"] if strap else None,
        }], **kwargs)

    def test_opt_in_and_server_validation(self):
        body, strap = self.setup_body()
        with self.assertRaisesRegex(SalesInventoryError, "Выберите ремешок"):
            self.sell(body)
        wrong = self.create_product(5, "Не ремешок", "WRONG")
        with self.assertRaises(SalesInventoryError):
            self.sell(body, wrong)
        with self.assertRaises(SalesInventoryError):
            self.sell(body, body)
        RequiredStraps(self.database).configure(body["id"], False)
        with self.assertRaises(SalesInventoryError):
            self.sell(body, strap)
        self.assertEqual(self.inventory.list_sales(), [])
        self.assertEqual(self.stock(body["id"]), 5)

    def test_quantity_single_price_repeat_and_historical_cancel(self):
        body, strap = self.setup_body()
        sale = self.sell(body, strap, 2, idempotency_key="required")
        repeated = self.sell(body, strap, 2, idempotency_key="required")
        self.assertEqual(sale["id"], repeated["id"])
        self.assertEqual((self.stock(body["id"]), self.stock(strap["id"])), (3, 2))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*), SUM(quantity*unit_price) FROM erp_sale_items").fetchone()[:], (1, 200))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM erp_sale_component_snapshots").fetchone()[0], 2)
        RequiredStraps(self.database).configure(body["id"], False)
        self.inventory.cancel_sale(sale["id"])
        self.inventory.cancel_sale(sale["id"])
        self.assertEqual((self.stock(body["id"]), self.stock(strap["id"])), (5, 4))

    def test_shortage_rolls_back_body_and_sale(self):
        body, strap = self.setup_body(0)
        with self.assertRaises(InsufficientStockError):
            self.sell(body, strap)
        self.assertEqual(self.stock(body["id"]), 5)
        self.assertEqual(self.inventory.list_sales(), [])

    def test_shared_strap_shortage_is_atomic(self):
        body, strap = self.setup_body(1)
        with self.assertRaises(InsufficientStockError):
            self.inventory.create_sale_batch(self.payload(body), [
                {"product_id": body["id"], "quantity": 1, "unit_price": 100, "strap_product_id": strap["id"]},
                {"product_id": strap["id"], "quantity": 1, "unit_price": 20},
            ])
        self.assertEqual((self.stock(body["id"]), self.stock(strap["id"])), (5, 1))

    def test_partial_return_uses_snapshot_after_flag_change(self):
        body, strap = self.setup_body()
        sale = self.sell(body, strap, 2)
        RequiredStraps(self.database).configure(body["id"], False)
        self.inventory.return_sale(sale["id"], 1)
        self.assertEqual((self.stock(body["id"]), self.stock(strap["id"])), (4, 3))

    def test_multiple_bodies_and_distinct_straps(self):
        body, strap = self.setup_body()
        other = self.create_product(2, "Другой корпус", "BODY-2")
        other_strap = self.catalog.create_product(name="Другой ремешок", article="STRAP-2",
                                                  brand="Brand", category="Ремешки", stock=2)
        RequiredStraps(self.database).configure(other["id"], True)
        sale = self.inventory.create_sale_batch(self.payload(body), [
            {"product_id": body["id"], "quantity": 2, "unit_price": 100, "strap_product_id": strap["id"]},
            {"product_id": other["id"], "quantity": 1, "unit_price": 200, "strap_product_id": other_strap["id"]},
        ])
        self.assertEqual([self.stock(p["id"]) for p in [body, strap, other, other_strap]], [3, 2, 1, 1])
        self.inventory.cancel_sale(sale["id"])
        self.assertEqual([self.stock(p["id"]) for p in [body, strap, other, other_strap]], [5, 4, 2, 2])

    def test_picker_excludes_accessories_and_archived_straps(self):
        body, strap = self.setup_body()
        self.catalog.create_product(name="Аксессуар", article="ACCESSORY", brand="Brand", category="Аксессуары", stock=4)
        products = SharedCatalog(self.database).list_products(product_kind="strap", in_stock=True)
        self.assertEqual([int(p["id"]) for p in products], [strap["id"]])
        with self.database.transaction() as connection:
            connection.execute("UPDATE catalog_excel_products SET active=0 WHERE id=?", (strap["id"],))
        with self.assertRaises(SalesInventoryError):
            self.sell(body, strap)
        self.assertEqual(self.stock(body["id"]), 5)

    def test_physical_balances_remain_separate_from_legacy(self):
        body, strap = self.setup_body()
        with self.database.transaction() as connection:
            for product in (body, strap):
                connection.execute("INSERT INTO erp_component_inventory(product_id,updated_at) VALUES (?,'test')", (product["id"],))
        for product in (body, strap):
            ComponentInventory(self.database).confirm(product["id"], 2)
        sale = self.sell(body, strap)
        with self.database.connect() as connection:
            self.assertEqual([balance(connection, p["id"]) for p in (body, strap)], [1, 1])
        self.inventory.cancel_sale(sale["id"])
        with self.database.connect() as connection:
            self.assertEqual([balance(connection, p["id"]) for p in (body, strap)], [2, 2])
            self.assertEqual([connection.execute("SELECT stock FROM catalog_excel_products WHERE id=?", (p["id"],)).fetchone()[0] for p in (body, strap)], [5, 4])


class RequiredStrapOrderTest(unittest.TestCase):
    setUp = order_tests.OrderTictactoySaleTest.setUp
    tearDown = order_tests.OrderTictactoySaleTest.tearDown
    patches = order_tests.OrderTictactoySaleTest.patches
    render_order = order_tests.OrderTictactoySaleTest.render_order
    conduct = order_tests.OrderTictactoySaleTest.conduct

    def test_order_requires_strap_and_preserves_commercial_rows(self):
        RequiredStraps(self.database).configure(self.watch["id"], True)
        html = self.render_order().get_data(as_text=True)
        self.assertIn('name="required_strap_0_product_id"', html)
        self.assertNotIn('name="required_strap_1_product_id"', html)
        self.assertNotIn('data-open-strap-replacement>', html)
        self.conduct()
        self.assertEqual(self.inventory.list_sales(), [])
        self.conduct(required_strap_0_product_id=str(self.strap["id"]))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*), SUM(quantity*unit_price) FROM erp_sale_items").fetchone()[:], (2, 17400))
            self.assertEqual(connection.execute("SELECT stock FROM catalog_excel_products WHERE id=?", (self.strap["id"],)).fetchone()[0], 0)

    def test_employee_cannot_change_flag(self):
        with mock.patch.object(web, "auth_is_enabled", return_value=True), mock.patch.object(web, "current_auth_user", return_value={"role": "employee"}), mock.patch.object(web, "ExcelProductCatalog"), mock.patch("app.services.required_straps.CatalogDatabase", return_value=self.database):
            with web.app.test_request_context(method="PUT", json={"requires_strap": True}):
                result = web.api_product_required_strap(self.watch["id"])
                self.assertEqual(result[1], 403)
        self.assertEqual(RequiredStraps(self.database).product_ids(), set())

    def test_admin_can_configure_and_csrf_is_checked(self):
        with mock.patch.object(web, "auth_is_enabled", return_value=True), mock.patch.object(web, "current_auth_user", return_value={"role": "admin", "id": 1}), mock.patch.object(web, "require_csrf_when_authenticated") as csrf, mock.patch.object(web, "ExcelProductCatalog"), mock.patch("app.services.required_straps.CatalogDatabase", return_value=self.database):
            with web.app.test_request_context(method="PUT", json={"requires_strap": True}):
                result = web.api_product_required_strap(self.watch["id"])
                self.assertTrue(result[0].get_json()["data"]["requires_strap"])
                csrf.assert_called_once()
        self.assertEqual(RequiredStraps(self.database).product_ids(), {self.watch["id"]})
