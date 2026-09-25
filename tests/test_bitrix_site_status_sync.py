import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

if os.name == "nt" and "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.SimpleNamespace(
        LOCK_EX=1, LOCK_NB=2, LOCK_UN=8, flock=lambda *args: None,
    )

from app.catalog_db import CatalogDatabase
from app.schema_migrations import apply_migrations
from app.services.bitrix_site_status_sync import BitrixSiteStatusSync
from app.services.excel_product_catalog import ExcelProductBatchService


def excel_row(number, name, stock):
    return {
        "excel_row": number,
        "excel_name": name,
        "excel_name_raw": name,
        "excel_article": "SKU-{}".format(number),
        "excel_brand": "Test",
        "category": "Watches",
        "stock": stock,
        "stock_valid": True,
        "cell": "A-1",
        "match_status": "not_found",
        "match_method": "none",
        "confidence": 0,
        "alternatives": [],
    }


class FakeClient:
    def __init__(self, pages, fail_on_page=None):
        self.pages = pages
        self.fail_on_page = fail_on_page

    def get_products_page(self, page, limit, include_inactive=False):
        if page == self.fail_on_page:
            raise OSError("controlled failure")
        products = self.pages[page - 1]
        return {
            "products": products,
            "has_more": page < len(self.pages),
        }


class BitrixSiteStatusSyncTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        database_path = Path(self.temp.name) / "catalog.db"
        apply_migrations(database_path, app_commit="site-status-sync-test")
        self.database = CatalogDatabase(database_path)
        ExcelProductBatchService(self.database).apply(
            [excel_row(2, "In stock", 3), excel_row(3, "No stock", 0)],
            "1" * 64,
            "products.xlsx",
        )
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM catalog_excel_products ORDER BY id"
            ).fetchall()
            self.ids = [row["id"] for row in rows]
            connection.execute(
                "UPDATE catalog_excel_products SET "
                "bitrix_external_product_id='101', bitrix_active=1 "
                "WHERE id=?", (self.ids[0],),
            )
            connection.execute(
                "UPDATE catalog_excel_products SET "
                "bitrix_external_product_id='102', bitrix_active=0 "
                "WHERE id=?", (self.ids[1],),
            )

    def tearDown(self):
        self.temp.cleanup()

    def snapshot(self):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT id, excel_name_raw, stock, cell, updated_at, "
                "bitrix_external_product_id, bitrix_active "
                "FROM catalog_excel_products ORDER BY id"
            ).fetchall()]

    def test_updates_only_site_status_after_complete_read(self):
        before = self.snapshot()
        result = BitrixSiteStatusSync(self.database, client=FakeClient([[
            {"external_product_id": "101", "active": False,
             "active_known": True},
            {"external_product_id": "102", "active": True,
             "active_known": True},
        ]])).run()
        after = self.snapshot()
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["mismatch_count"], 2)
        self.assertEqual([row["bitrix_active"] for row in after], [0, 1])
        for old, new in zip(before, after):
            for field in (
                "id", "excel_name_raw", "stock", "cell", "updated_at",
                "bitrix_external_product_id",
            ):
                self.assertEqual(old[field], new[field])

    def test_failed_later_page_preserves_every_status(self):
        before = self.snapshot()
        client = FakeClient([[
            {"external_product_id": "101", "active": False,
             "active_known": True},
        ], []], fail_on_page=2)
        with self.assertRaises(OSError):
            BitrixSiteStatusSync(self.database, client=client).run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            BitrixSiteStatusSync(self.database).summary()["outcome"], "error"
        )

    def test_unknown_status_is_not_guessed(self):
        before = self.snapshot()
        result = BitrixSiteStatusSync(
            self.database,
            client=FakeClient([[
                {"external_product_id": "101", "active": True,
                 "active_known": False},
            ]]),
        ).run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(result["unknown_source_statuses"], 1)

    def test_indicator_count_excludes_unlinked_and_unknown(self):
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE catalog_excel_products SET bitrix_active=0 WHERE id=?",
                (self.ids[0],),
            )
            connection.execute(
                "UPDATE catalog_excel_products SET "
                "bitrix_external_product_id=NULL, bitrix_active=NULL WHERE id=?",
                (self.ids[1],),
            )
        summary = BitrixSiteStatusSync(self.database).summary()
        self.assertEqual(summary["in_stock_inactive"], 1)
        self.assertEqual(summary["in_stock_unlinked"], 0)
        self.assertEqual(summary["mismatch_count"], 1)


if __name__ == "__main__":
    unittest.main()
