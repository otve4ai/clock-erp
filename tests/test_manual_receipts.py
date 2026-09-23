import tempfile
import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.catalog_db import CatalogDatabase
from app.services.excel_product_catalog import ExcelProductCatalog
from app.services.manual_receipts import ManualReceipts, ManualReceiptError
from app.services.supplies import SupplyEngine


class ManualReceiptTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = CatalogDatabase(Path(self.temp.name) / "catalog.db")
        self.db.initialize()
        self.manual = ManualReceipts(self.db)
        self.supplies = SupplyEngine(self.db)
        with self.db.transaction() as connection:
            connection.execute(
                "INSERT INTO erp_warehouses(id,code,name,active,is_default,created_at,updated_at) "
                "VALUES('hong-kong','HK','Гонконг',1,0,'2026-09-23','2026-09-23')"
            )

    def tearDown(self):
        self.temp.cleanup()

    def product(self, external="1", stock=10):
        product = self.supplies.resolve_bitrix({
            "external_product_id": external,
            "name": "Watch " + external,
            "external_sku": "SKU-" + external,
            "brand": "Casio",
        })
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET stock=? WHERE id=?", (stock, product["id"])
            )
        return int(product["id"])

    def total(self, product_id):
        with self.db.connect() as connection:
            return float(connection.execute(
                "SELECT stock FROM catalog_excel_products WHERE id=?", (product_id,)
            ).fetchone()[0])

    def warehouse(self, warehouse_id, product_id):
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT quantity FROM erp_warehouse_stocks WHERE warehouse_id=? AND product_id=?",
                (warehouse_id, product_id),
            ).fetchone()
            return float(row[0]) if row else 0.0

    def draft(self, items, warehouse="hong-kong", reason="stock_adjustment", comment=""):
        return self.manual.create(
            warehouse, reason,
            [{"product_id": product, "quantity": quantity} for product, quantity in items],
            comment, "Максим",
        )

    def test_draft_does_not_change_stock_and_can_be_edited_or_deleted(self):
        product = self.product()
        receipt = self.draft([(product, 5)])
        self.assertEqual(self.total(product), 10)
        self.assertEqual(self.warehouse("hong-kong", product), 0)
        updated = self.manual.update(
            receipt["id"], "hong-kong", "found",
            [{"product_id": product, "quantity": 3}], "", "Editor",
        )
        self.assertEqual(updated["items"][0]["quantity"], 3)
        self.manual.delete(receipt["id"], "Editor")
        self.assertEqual(self.total(product), 10)
        with self.assertRaises(ManualReceiptError):
            self.manual.get(receipt["id"])

    def test_post_is_atomic_scoped_and_idempotent_for_multiple_products(self):
        first = self.product("1", 10)
        second = self.product("2", 4)
        receipt = self.draft([(first, 5), (second, 2)])
        posted = self.manual.post(receipt["id"], "Poster")
        self.manual.post(receipt["id"], "Poster")
        self.assertEqual(posted["status"], "posted")
        self.assertEqual((self.total(first), self.total(second)), (15, 6))
        self.assertEqual((self.warehouse("hong-kong", first), self.warehouse("hong-kong", second)), (5, 2))
        self.assertEqual(self.warehouse("default", first), 10)
        with self.db.connect() as connection:
            movements = connection.execute(
                "SELECT source_type,warehouse_id,COUNT(*) FROM catalog_stock_movements "
                "WHERE receipt_id=? GROUP BY source_type,warehouse_id", (receipt["id"],)
            ).fetchone()
        self.assertEqual(tuple(movements), ("manual_receipt", "hong-kong", 2))

    def test_double_submit_and_parallel_receipts_have_no_lost_update(self):
        product = self.product(stock=10)
        one = self.draft([(product, 5)])
        two = self.draft([(product, 3)])
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(self.manual.post, one["id"], "A"),
                pool.submit(self.manual.post, one["id"], "A"),
                pool.submit(self.manual.post, two["id"], "B"),
            ]
            for future in futures:
                future.result()
        self.assertEqual(self.total(product), 18)
        self.assertEqual(self.warehouse("hong-kong", product), 8)

    def test_cancel_reverses_once_and_blocks_insufficient_warehouse_stock(self):
        product = self.product(stock=10)
        receipt = self.draft([(product, 5)])
        self.manual.post(receipt["id"])
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE erp_warehouse_stocks SET quantity=1 "
                "WHERE warehouse_id='hong-kong' AND product_id=?", (product,)
            )
        with self.assertRaisesRegex(ManualReceiptError, "доступно 1"):
            self.manual.cancel(receipt["id"])
        self.assertEqual(self.total(product), 15)
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE erp_warehouse_stocks SET quantity=5 "
                "WHERE warehouse_id='hong-kong' AND product_id=?", (product,)
            )
        cancelled = self.manual.cancel(receipt["id"], "Canceller")
        self.manual.cancel(receipt["id"], "Canceller")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.total(product), 10)
        self.assertEqual(self.warehouse("hong-kong", product), 0)
        with self.assertRaises(ManualReceiptError):
            self.manual.update(receipt["id"], "hong-kong", "found", [{"product_id": product, "quantity": 1}])
        with self.assertRaises(ManualReceiptError):
            self.manual.delete(receipt["id"])

    def test_validation_numbering_and_transaction_rollback(self):
        product = self.product()
        for quantity in (0, -1, 1.5, None):
            with self.subTest(quantity=quantity), self.assertRaises(ManualReceiptError):
                self.draft([(product, quantity)])
        with self.assertRaises(ManualReceiptError):
            self.manual.create("missing", "found", [{"product_id": product, "quantity": 1}])
        with self.assertRaises(ManualReceiptError):
            self.manual.create("hong-kong", "other", [{"product_id": product, "quantity": 1}])
        numbers = {self.draft([(product, 1)])["number"] for _ in range(3)}
        self.assertEqual(len(numbers), 3)
        with ThreadPoolExecutor(max_workers=4) as pool:
            concurrent = list(pool.map(lambda _: self.draft([(product, 1)])["number"], range(4)))
        self.assertEqual(len(set(concurrent)), 4)
        receipt = self.draft([(product, 2)])
        with self.assertRaises(RuntimeError):
            self.manual.post(receipt["id"], failure_hook=lambda _: (_ for _ in ()).throw(RuntimeError("fail")))
        self.assertEqual(self.total(product), 10)
        self.assertEqual(self.manual.get(receipt["id"])["status"], "draft")

    def test_supplies_and_manual_receipts_are_explicitly_separate(self):
        product = self.product()
        supply = self.supplies.create("Поставка", items=[{"product_id": product, "quantity": 2}])
        manual = self.draft([(product, 3)])
        with self.db.connect() as connection:
            rows = dict(connection.execute(
                "SELECT id,operation_type FROM erp_receipts WHERE id IN (?,?)",
                (supply["id"], manual["id"]),
            ).fetchall())
        self.assertEqual(rows[supply["id"]], "supply")
        self.assertEqual(rows[manual["id"]], "manual_receipt")
        self.assertEqual([row["id"] for row in self.supplies.list()], [supply["id"]])
        self.assertEqual([row["id"] for row in self.manual.list()], [manual["id"]])

    def test_http_routes_expose_tabs_warehouses_and_lifecycle(self):
        import app.web as web

        product = self.product()
        web.app.config["TESTING"] = True
        with patch.dict(os.environ, {"CATALOG_DATABASE_PATH": str(self.db.path)}):
            client = web.app.test_client()
            page = client.get("/app/receipts")
            self.assertEqual(page.status_code, 200)
            html = page.get_data(as_text=True)
            for text in ("Поступления", "Все записи", "Поставки", "Приходы", "Отмены продаж", "+ Добавить"):
                self.assertIn(text, html)
            warehouses = client.get("/api/v1/receipts/warehouses").get_json()["data"]
            self.assertEqual({row["id"] for row in warehouses}, {"default", "hong-kong"})
            created = client.post("/api/v1/receipts/manual", json={
                "warehouse_id": "hong-kong", "reason_code": "found",
                "items": [{"product_id": product, "quantity": 2}],
            })
            self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
            receipt = created.get_json()["data"]
            self.assertEqual(client.post(
                "/api/v1/receipts/manual/{}/post".format(receipt["id"])
            ).status_code, 200)
            documents = client.get("/api/v1/receipts/documents").get_json()["data"]
            self.assertEqual([row["source_type"] for row in documents], ["manual_receipt"])
            with patch("app.supply_routes.auth_is_enabled", return_value=True), patch(
                "app.supply_routes.current_auth_user", return_value={"role": "viewer"}
            ):
                self.assertEqual(client.post(
                    "/api/v1/receipts/manual/{}/cancel".format(receipt["id"]),
                    json={}, headers={"X-CSRF-Token": "test"},
                ).status_code, 403)


if __name__ == "__main__":
    unittest.main()
