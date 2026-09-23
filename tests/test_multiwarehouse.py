import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.catalog_db import CatalogDatabase
from app.multiwarehouse_migration import apply_multiwarehouse_migration
from app.schema_migrations import apply_migrations
from app.services.bitrix_stock_sync import BitrixStockSync
from app.services.brand_inventory import BrandInventory
from app.services.excel_product_catalog import ExcelProductBatchService
from app.services.product_bundles import ProductBundles
from app.services.receipt_inventory import ReceiptInventory
from app.services.sales_inventory import SalesInventory
from app.services.writeoffs import Writeoffs
from app.services.warehouse_stock import (
    InsufficientWarehouseStock,
    WarehouseStockError,
    WarehouseStockService,
    default_warehouse_id,
    get_balance,
    set_balance,
)


def excel_row(number, name, article, stock):
    return {
        "excel_row": number,
        "excel_name": name,
        "excel_name_raw": name,
        "excel_article": article,
        "excel_brand": "Test",
        "category": "Часы",
        "stock": stock,
        "stock_valid": True,
        "cell": "A-1",
        "match_status": "not_found",
        "match_method": "none",
        "confidence": 0,
        "alternatives": [],
    }


class MultiwarehouseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "catalog.db"
        apply_migrations(self.path, app_commit="multiwarehouse-test")
        self.database = CatalogDatabase(self.path)
        ExcelProductBatchService(self.database).apply(
            [
                excel_row(2, "Head", "HEAD", 10),
                excel_row(3, "Strap", "STRAP", 4),
                excel_row(4, "Bundle", "BUNDLE", 0),
                excel_row(5, "Ordinary", "ORDINARY", 5),
            ],
            "a" * 64,
            "multiwarehouse.xlsx",
        )
        with self.database.connect() as connection:
            self.products = {
                row["excel_article"]: int(row["id"])
                for row in connection.execute(
                    "SELECT id,excel_article FROM catalog_excel_products"
                ).fetchall()
            }
        self.stock = WarehouseStockService(self.database)
        self.warehouses = {
            row["code"]: row for row in self.stock.list_warehouses()
        }
        self.udelnaya = int(self.warehouses["udelnaya"]["id"])
        self.hong_kong = int(self.warehouses["hong-kong"]["id"])

    def tearDown(self):
        self.temporary.cleanup()

    def set_stock(self, article, warehouse_id, value):
        with self.database.transaction() as connection:
            set_balance(connection, self.products[article], warehouse_id, value)

    def test_initial_warehouses_creation_and_uniqueness(self):
        self.assertEqual(set(self.warehouses), {"udelnaya", "hong-kong"})
        self.assertEqual(self.warehouses["udelnaya"]["name"], "Удельная")
        self.assertEqual(self.warehouses["hong-kong"]["name"], "Гонконг")
        with self.assertRaises(WarehouseStockError):
            self.stock.create_warehouse("Удельная", "another-code")
        with self.assertRaises(WarehouseStockError):
            self.stock.create_warehouse("Another", "udelnaya")
        created = self.stock.create_warehouse("Резерв", "reserve")
        with self.database.transaction() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO erp_product_warehouse_stock "
                "(product_id,warehouse_id,quantity,initialized_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (self.products["ORDINARY"], created["id"], 1, "now", "now"),
            )
            connection.execute(
                "INSERT INTO erp_product_warehouse_stock "
                "(product_id,warehouse_id,quantity,initialized_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (self.products["ORDINARY"], created["id"], 2, "now", "now"),
            )

    def test_balances_sum_increase_decrease_and_negative_guard(self):
        product_id = self.products["ORDINARY"]
        self.assertEqual(self.stock.get_balance(product_id, self.udelnaya), 5)
        self.set_stock("ORDINARY", self.hong_kong, 3)
        self.assertEqual(self.stock.selected_total(
            product_id, [self.udelnaya, self.hong_kong]
        ), 8)
        self.stock.increase(product_id, self.hong_kong, 2)
        self.stock.decrease(product_id, self.udelnaya, 4)
        self.assertEqual(self.stock.get_balance(product_id, self.hong_kong), 5)
        self.assertEqual(self.stock.get_balance(product_id, self.udelnaya), 1)
        with self.assertRaises(InsufficientWarehouseStock):
            self.stock.decrease(product_id, self.udelnaya, 2)
        self.assertEqual(self.stock.get_balance(product_id, self.udelnaya), 1)

    def test_transfer_is_atomic_idempotent_and_rolls_back(self):
        product_id = self.products["ORDINARY"]
        transfer = self.stock.transfer(
            product_id, self.udelnaya, self.hong_kong, 2,
            idempotency_key="transfer-1",
        )
        repeated = self.stock.transfer(
            product_id, self.udelnaya, self.hong_kong, 2,
            idempotency_key="transfer-1",
        )
        self.assertEqual(transfer["id"], repeated["id"])
        self.assertEqual((
            self.stock.get_balance(product_id, self.udelnaya),
            self.stock.get_balance(product_id, self.hong_kong),
        ), (3, 2))

        def fail(_connection):
            raise RuntimeError("forced rollback")

        with self.assertRaises(RuntimeError):
            self.stock.transfer(
                product_id, self.udelnaya, self.hong_kong, 1,
                failure_hook=fail,
            )
        self.assertEqual((
            self.stock.get_balance(product_id, self.udelnaya),
            self.stock.get_balance(product_id, self.hong_kong),
        ), (3, 2))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM catalog_stock_movements WHERE transfer_id=?",
                (transfer["id"],),
            ).fetchone()[0], 2)

    def test_bundle_availability_is_calculated_per_warehouse(self):
        bundles = ProductBundles(self.database)
        bundles.configure(self.products["BUNDLE"], [
            {"component_id": self.products["HEAD"], "quantity": 1},
            {"component_id": self.products["STRAP"], "quantity": 1},
        ])
        self.set_stock("HEAD", self.hong_kong, 2)
        self.set_stock("STRAP", self.hong_kong, 8)
        result = bundles.get(
            self.products["BUNDLE"], [self.udelnaya, self.hong_kong]
        )
        self.assertEqual(result["available_by_warehouse"], {
            self.udelnaya: 4, self.hong_kong: 2,
        })
        self.assertEqual(result["available_to_assemble"], 6)
        self.assertEqual(
            bundles.get(self.products["BUNDLE"], [self.udelnaya])[
                "available_to_assemble"
            ],
            4,
        )

    def test_bundle_cannot_mix_components_between_warehouses(self):
        bundles = ProductBundles(self.database)
        bundles.configure(self.products["BUNDLE"], [
            {"component_id": self.products["HEAD"], "quantity": 1},
            {"component_id": self.products["STRAP"], "quantity": 1},
        ])
        self.set_stock("STRAP", self.udelnaya, 0)
        self.set_stock("HEAD", self.hong_kong, 0)
        self.set_stock("STRAP", self.hong_kong, 8)
        result = bundles.get(
            self.products["BUNDLE"], [self.udelnaya, self.hong_kong]
        )
        self.assertEqual(result["available_by_warehouse"], {
            self.udelnaya: 0, self.hong_kong: 0,
        })
        self.assertEqual(result["available_to_assemble"], 0)

    def test_legacy_default_and_historical_document_backfill(self):
        with self.database.transaction() as connection:
            self.assertEqual(default_warehouse_id(connection), self.udelnaya)
            self.assertEqual(get_balance(
                connection, self.products["ORDINARY"],
                allow_legacy_default=True,
            ), 5)
            now = "2026-09-23T10:00:00+00:00"
            connection.execute(
                "INSERT INTO erp_receipts "
                "(id,status,receipt_date,metadata_json,created_at,updated_at,warehouse_id) "
                "VALUES (?,'draft',?,'{}',?,?,NULL)",
                ("legacy-receipt", now[:10], now, now),
            )
            apply_multiwarehouse_migration(connection)
            self.assertEqual(connection.execute(
                "SELECT warehouse_id FROM erp_receipts WHERE id='legacy-receipt'"
            ).fetchone()[0], self.udelnaya)

    def test_migration_copies_legacy_canonical_stock_and_verifies_totals(self):
        product_id = self.products["ORDINARY"]
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET stock=COALESCE(("
                "SELECT quantity FROM erp_product_warehouse_stock s "
                "WHERE s.product_id=catalog_excel_products.id AND s.warehouse_id=?"
                "),0)",
                (self.udelnaya,),
            )
            connection.execute(
                "UPDATE catalog_excel_products SET stock=7 WHERE id=?", (product_id,)
            )
            connection.execute("DELETE FROM erp_multiwarehouse_migration_audit")
            connection.execute("DELETE FROM erp_product_warehouse_stock")
            verification = apply_multiwarehouse_migration(connection)
            self.assertEqual(get_balance(
                connection, product_id, self.udelnaya
            ), 7)
            self.assertEqual(verification["legacy_total"], verification["warehouse_total"])
            self.assertEqual(verification["legacy_hash"], verification["warehouse_hash"])

    def test_bitrix_updates_only_legacy_default_warehouse(self):
        product_id = self.products["ORDINARY"]
        self.set_stock("ORDINARY", self.hong_kong, 3)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET bitrix_external_product_id='42' WHERE id=?",
                (product_id,),
            )
        incoming = [{
            "external_product_id": "42",
            "external_xml_id": "xml-42",
            "external_sku": "ORDINARY",
            "name": "Ordinary",
            "brand": "Test",
            "stock": 7,
            "stock_source_field": "CCatalogProduct.QUANTITY",
            "properties": [],
        }]
        report = BitrixStockSync(self.database).synchronize(
            incoming, apply=True, source_generated_at="source-time"
        )
        self.assertEqual(report["updated"], 1)
        self.assertEqual(self.stock.get_balance(product_id, self.udelnaya), 7)
        self.assertEqual(self.stock.get_balance(product_id, self.hong_kong), 3)
        self.assertEqual(report["source"]["store_id"], "udelnaya")

    def test_documents_mutate_and_record_the_selected_warehouse(self):
        product_id = self.products["ORDINARY"]
        self.set_stock("ORDINARY", self.hong_kong, 5)
        sale = SalesInventory(self.database).create_sale(
            {"id": "sale-hk", "source": "Test", "warehouse_id": self.hong_kong},
            product_id, 1, 100,
        )
        self.assertEqual(self.stock.get_balance(product_id, self.hong_kong), 4)
        receipt = ReceiptInventory(self.database).create_receipt(
            {"id": "receipt-hk", "warehouse_id": self.hong_kong},
            [{"product_id": product_id, "quantity": 2}],
        )
        self.assertEqual(self.stock.get_balance(product_id, self.hong_kong), 6)
        writeoff = Writeoffs(self.database).create(
            {
                "product_id": product_id,
                "quantity": 1,
                "reason": "Брак",
                "warehouse_id": self.hong_kong,
            },
            {"actor_id": "test", "actor_name": "Test"},
        )
        self.assertEqual(self.stock.get_balance(product_id, self.hong_kong), 5)
        self.assertEqual(receipt["warehouse_id"], self.hong_kong)
        self.assertEqual(writeoff["warehouse_id"], self.hong_kong)
        with self.database.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT warehouse_id FROM erp_sale_items WHERE sale_id=?",
                (sale["id"],),
            ).fetchone()[0], self.hong_kong)
            warehouses = {
                row[0] for row in connection.execute(
                    "SELECT warehouse_id FROM catalog_stock_movements "
                    "WHERE source_id IN (?,?,?) OR sale_id=? OR receipt_id=?",
                    (sale["id"], receipt["id"], writeoff["id"], sale["id"], receipt["id"]),
                ).fetchall()
            }
        self.assertEqual(warehouses, {self.hong_kong})

    def test_inventory_snapshot_and_confirmation_are_warehouse_scoped(self):
        with self.database.connect() as connection:
            brand_id = connection.execute(
                "SELECT brand_id FROM catalog_excel_products WHERE id=?",
                (self.products["ORDINARY"],),
            ).fetchone()[0]
        inventory = BrandInventory(self.database)
        session, created = inventory.start(
            brand_id, user_name="Test", warehouse_id=self.hong_kong
        )
        self.assertTrue(created)
        item = next(
            row for row in inventory.list_items(session["id"])
            if int(row["product_id"]) == self.products["ORDINARY"]
        )
        inventory.confirm(
            session["id"], item["id"], 2, user_name="Test",
            idempotency_key="inventory-hk",
        )
        self.assertEqual(
            self.stock.get_balance(self.products["ORDINARY"], self.hong_kong), 2
        )
        self.assertEqual(
            self.stock.get_balance(self.products["ORDINARY"], self.udelnaya), 5
        )

    def test_services_do_not_write_legacy_stock_columns(self):
        root = Path(__file__).resolve().parents[1]
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for folder in (root / "app", root / "scripts")
            for path in folder.rglob("*.py")
        )
        forbidden = (
            r"UPDATE\s+catalog_excel_products\s+SET\s+stock",
            r"UPDATE\s+erp_component_inventory\s+SET\s+physical_stock",
            r"stock\s*=\s*stock\s*[+-]",
        )
        for pattern in forbidden:
            self.assertIsNone(re.search(pattern, source, re.IGNORECASE), pattern)


if __name__ == "__main__":
    unittest.main()
